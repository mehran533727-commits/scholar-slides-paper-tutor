#!/usr/bin/env python3
"""Deterministic policy evaluation over browser-collected review diagnostics."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


SCHEMA_VERSION = 1
KIND = "scholar-slides-visual-qa"
_PLACEHOLDER = re.compile(r"\b(?:todo|tbd|placeholder|lorem ipsum)\b", re.IGNORECASE)
_INTERNAL_REVIEW_TEXT = re.compile(r"\b(?:ckpt[- ]?\d+|checkpoint|audit(?:[- ]trail)?)\b", re.IGNORECASE)
_PLACEHOLDER_PHRASES = (
    "placeholder",
    "local evidence image",
    "sample image",
    "example image",
    "dummy",
    "lorem ipsum",
    "todo",
    "tbd",
    "待补充",
    "示例图片",
    "占位图",
    "作者信息保留为 pdf 原文",
)

# These are policy knobs, not slide-specific exceptions.  Layouts map to a role and use the
# same thresholds across every deck; warnings are intentionally non-blocking so a human can
# judge deliberate whitespace while errors still gate CKPT-2 readiness.
_ROLE_THRESHOLDS: dict[str, dict[str, float | str]] = {
    "title": {"min_area": 0.018, "min_horizontal": 0.22, "min_vertical": 0.10, "severity": "warning"},
    "standard": {"min_area": 0.10, "min_horizontal": 0.32, "min_vertical": 0.22, "severity": "warning"},
    "table": {"min_area": 0.08, "min_horizontal": 0.28, "min_vertical": 0.18, "severity": "warning"},
    "equation": {"min_area": 0.06, "min_horizontal": 0.24, "min_vertical": 0.14, "severity": "warning"},
    "image": {"min_area": 0.12, "min_horizontal": 0.30, "min_vertical": 0.22, "severity": "warning"},
    "two-column": {"min_area": 0.12, "min_horizontal": 0.55, "min_vertical": 0.22, "severity": "warning"},
    "references": {"min_area": 0.045, "min_horizontal": 0.32, "min_vertical": 0.20, "severity": "warning"},
}
_ROLE_FONT_THRESHOLDS = {"table": 16.0, "equation": 28.0, "references": 18.0}

_HUMAN_REVIEW_CHECKLIST = (
    "叙事顺序是否自然",
    "图表和图片内部文字是否足够清晰",
    "内容密度是否适合现场讲解",
    "页面是否显得过空或过挤",
    "视觉层次是否符合组会汇报",
)


def _issue(
    *,
    code: str,
    severity: str,
    slide_index: int,
    json_pointer: str,
    message: str,
    evidence: dict[str, Any],
    suggested_action: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "slide_index": slide_index,
        "json_pointer": json_pointer,
        "message": message,
        "evidence": dict(sorted(evidence.items())),
        "suggested_action": suggested_action,
    }


def _slide_pointer(slide: dict[str, Any], slide_index: int) -> str:
    pointer = slide.get("json_pointer")
    if isinstance(pointer, str) and pointer.startswith("/"):
        return pointer
    return f"/slides/{max(slide_index - 1, 0)}"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _normalize_phrase(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _placeholder_phrase(value: Any) -> str | None:
    normalized = _normalize_phrase(value)
    for phrase in _PLACEHOLDER_PHRASES:
        candidate = _normalize_phrase(phrase)
        if candidate and candidate in normalized:
            return phrase
    return None


def _text_regions(slide: dict[str, Any]) -> list[dict[str, Any]]:
    regions = slide.get("visible_text_regions")
    if isinstance(regions, list):
        return [region for region in regions if isinstance(region, dict) and str(region.get("text", "")).strip()]
    try:
        slide_number = int(slide.get("slide_index", 0))
    except (TypeError, ValueError):
        slide_number = 0
    pointer = _slide_pointer(slide, slide_number)
    return [
        {"text": text, "json_pointer": pointer, "dom_selector": ""}
        for text in slide.get("visible_text", [])
        if str(text).strip()
    ]


def _layout_role(layout: Any, deck_slide: dict[str, Any] | None = None) -> str:
    value = str(layout or (deck_slide or {}).get("layout", ""))
    if value == "paper-title":
        return "title"
    if value == "references":
        return "references"
    if value == "results-table" or value == "table":
        return "table"
    if value == "equation" or value == "concept-or-metric":
        return "equation"
    if value == "assertion-evidence" or value in {"figure", "image"}:
        return "image"
    if value == "two-column" or value in {"comparison", "two_column"}:
        return "two-column"
    return "standard"


def _semantic_metrics(slide: dict[str, Any], role: str, canvas_width: Any, canvas_height: Any) -> dict[str, Any] | None:
    elements = slide.get("semantic_elements")
    if not isinstance(elements, list) or not isinstance(canvas_width, (int, float)) or not isinstance(canvas_height, (int, float)):
        return None
    boxes: list[tuple[float, float, float, float, dict[str, Any]]] = []
    role_elements = {
        "table": {"table"},
        "equation": {"equation"},
        "image": {"figure", "image"},
        "references": {"body"},
        "two-column": {"body", "figure", "image", "table", "equation"},
    }.get(role)
    for element in elements:
        if not isinstance(element, dict):
            continue
        semantic_role = str(element.get("semantic_role", element.get("role", ""))).casefold()
        if semantic_role in {"footer", "background", "decorative", "page-number"} or (role_elements is not None and semantic_role not in role_elements):
            continue
        values = [_number(element.get(name)) for name in ("x", "y", "width", "height")]
        if any(value is None for value in values) or values[2] <= 0 or values[3] <= 0:
            continue
        boxes.append((values[0], values[1], values[2], values[3], element))
    if not boxes:
        empty_bbox = {"x": 0, "y": 0, "width": 0, "height": 0}
        return {
            "role": role,
            "bbox": empty_bbox,
            "semantic_content_bbox": empty_bbox,
            "semantic_area_ratio": 0.0,
            "horizontal_span_ratio": 0.0,
            "vertical_span_ratio": 0.0,
            "semantic_element_count": 0,
        }
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    width = max(0.0, right - left)
    height = max(0.0, bottom - top)
    bbox = {"x": round(left, 2), "y": round(top, 2), "width": round(width, 2), "height": round(height, 2)}
    return {
        "role": role,
        "bbox": bbox,
        "semantic_content_bbox": bbox,
        "semantic_area_ratio": round((width * height) / (float(canvas_width) * float(canvas_height)), 6),
        "horizontal_span_ratio": round(width / float(canvas_width), 6),
        "vertical_span_ratio": round(height / float(canvas_height), 6),
        "semantic_element_count": len(boxes),
    }


def _deck_asset_is_placeholder(deck_slide: dict[str, Any]) -> tuple[str, str] | None:
    for field in ("figure", "media"):
        asset = deck_slide.get(field)
        if not isinstance(asset, dict):
            continue
        placeholder_flag = asset.get("placeholder") is True or str(asset.get("placeholder", "")).casefold() in {"true", "1", "yes"}
        if placeholder_flag or str(asset.get("asset_role", "")).casefold() in {"placeholder", "visual-placeholder"}:
            return field, str(asset.get("src", asset.get("asset", "")))
        for key in ("alt", "caption", "label"):
            if _placeholder_phrase(asset.get(key, "")):
                return field, str(asset.get("src", asset.get("asset", "")))
    return None


def _overlap_area(first: dict[str, Any], second: dict[str, Any]) -> float:
    ax, ay, aw, ah = (_number(first.get(name)) for name in ("x", "y", "width", "height"))
    bx, by, bw, bh = (_number(second.get(name)) for name in ("x", "y", "width", "height"))
    if None in {ax, ay, aw, ah, bx, by, bw, bh} or aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    return max(0.0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0.0, min(ay + ah, by + bh) - max(ay, by))


def evaluate_visual_qa(
    *,
    deck: dict[str, Any],
    diagnostics: dict[str, Any],
    deck_sha256: str,
    asset_graph_sha256: str,
    renderer_version: str,
    legibility_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn computed browser geometry into stable, approval-relevant findings."""
    issues: list[dict[str, Any]] = []
    requires_cjk = str(deck.get("meta", {}).get("language", "")).casefold().startswith("zh")
    viewport = diagnostics.get("viewport", {})
    canvas_width = viewport.get("width") if isinstance(viewport, dict) else None
    canvas_height = viewport.get("height") if isinstance(viewport, dict) else None
    for finding in legibility_findings or []:
        if not isinstance(finding, dict):
            continue
        required = ("slide", "asset", "source_locator", "measurement", "threshold", "check", "severity", "action")
        if any(key not in finding for key in required):
            continue
        if not isinstance(finding["slide"], int) or not isinstance(finding["check"], str):
            continue
        if not isinstance(finding["measurement"], dict) or not isinstance(finding["threshold"], dict):
            continue
        check = finding["check"]
        if check not in {"figure-text-illegible", "figure-compressed", "vertical-void", "horizontal-void"}:
            continue
        issues.append(_issue(
            code=check,
            severity="error" if check.startswith("figure-") else "warning",
            slide_index=finding["slide"],
            json_pointer=f"/slides/{max(finding['slide'] - 1, 0)}",
            message="deterministic rendered figure legibility or canvas-void finding",
            evidence={key: finding[key] for key in required},
            suggested_action=str(finding["action"]),
        ))
    for url in sorted({str(value) for value in diagnostics.get("network_requests", [])}):
        issues.append(_issue(
            code="review-network-request",
            severity="error",
            slide_index=0,
            json_pointer="/",
            message="the browser attempted a non-local review resource request",
            evidence={"url": url},
            suggested_action="remove the external resource and use a graph-bound local asset",
        ))
    for message in sorted({str(value) for value in diagnostics.get("console_errors", [])}):
        issues.append(_issue(
            code="review-console-error",
            severity="error",
            slide_index=0,
            json_pointer="/",
            message="the browser reported a console error while rendering review HTML",
            evidence={"message": message},
            suggested_action="repair the renderer error before seeking CKPT-2 approval",
        ))
    for message in sorted({str(value) for value in diagnostics.get("page_errors", [])}):
        issues.append(_issue(
            code="review-page-error",
            severity="error",
            slide_index=0,
            json_pointer="/",
            message="the browser raised a page error while rendering review HTML",
            evidence={"message": message},
            suggested_action="repair the page error before seeking CKPT-2 approval",
        ))
    diagnostic_slides = [slide for slide in diagnostics.get("slides", []) if isinstance(slide, dict)]
    diagnostics_by_index = {int(slide.get("slide_index", 0)): slide for slide in diagnostic_slides}
    deck_slides = [slide for slide in deck.get("slides", []) if isinstance(slide, dict)]
    semantic_metrics_by_slide: list[dict[str, Any]] = []
    seen_conclusions: dict[str, int] = {}
    seen_notes: dict[str, int] = {}
    for deck_index, deck_slide in enumerate(deck.get("slides", [])):
        if not isinstance(deck_slide, dict):
            continue
        slide_index = deck_index + 1
        conclusion = deck_slide.get("action_title", deck_slide.get("title"))
        if isinstance(conclusion, str) and conclusion.strip():
            normalized = " ".join(conclusion.casefold().split())
            if normalized in seen_conclusions:
                issues.append(_issue(
                    code="visual-duplicate-conclusion",
                    severity="warning",
                    slide_index=slide_index,
                    json_pointer=f"/slides/{deck_index}/action_title" if "action_title" in deck_slide else f"/slides/{deck_index}/title",
                    message="a slide conclusion duplicates an earlier slide conclusion",
                    evidence={"first_slide": seen_conclusions[normalized], "text": conclusion},
                    suggested_action="differentiate the conclusion or intentionally consolidate the duplicate slides",
                ))
            else:
                seen_conclusions[normalized] = slide_index
        notes = deck_slide.get("notes")
        if isinstance(notes, str) and notes.strip():
            normalized_notes = " ".join(notes.casefold().split())
            if normalized_notes in seen_notes:
                issues.append(_issue(
                    code="visual-duplicate-note",
                    severity="warning",
                    slide_index=slide_index,
                    json_pointer=f"/slides/{deck_index}/notes",
                    message="a presenter note duplicates an earlier note",
                    evidence={"first_slide": seen_notes[normalized_notes]},
                    suggested_action="remove accidental duplicate presenter notes",
                ))
            else:
                seen_notes[normalized_notes] = slide_index
        if ("table" in deck_slide or any(key in deck_slide for key in ("figure", "media", "images"))) and not any(
            isinstance(deck_slide.get(key), str) and deck_slide.get(key).strip()
            for key in ("source_ref", "source", "citation")
        ):
            issues.append(_issue(
                code="visual-source-missing",
                severity="warning",
                slide_index=slide_index,
                json_pointer=f"/slides/{deck_index}",
                message="a slide with a table or visual asset has no visible source reference field",
                evidence={"layout": str(deck_slide.get("layout", ""))},
                suggested_action="add a concise source reference for the visual claim",
            ))
        placeholder_asset = _deck_asset_is_placeholder(deck_slide)
        if placeholder_asset:
            field, asset = placeholder_asset
            issues.append(_issue(
                code="visual-placeholder-asset",
                severity="error",
                slide_index=slide_index,
                json_pointer=f"/slides/{deck_index}/{field}",
                message="a visible visual asset is explicitly marked as a placeholder",
                evidence={"asset": asset, "field": field, "metadata_only": True},
                suggested_action="replace the declared placeholder asset with a graph-bound, reviewed paper visual; image OCR is intentionally not used",
            ))
    for slide in diagnostic_slides:
        slide_index = int(slide.get("slide_index", 0))
        slide_pointer = _slide_pointer(slide, slide_index)
        deck_slide = deck_slides[slide_index - 1] if 0 < slide_index <= len(deck_slides) else {}
        role = _layout_role(slide.get("layout"), deck_slide)
        if requires_cjk and not bool(slide.get("font", {}).get("cjk_ok")):
            issues.append(_issue(
                code="visual-font-cjk-missing",
                severity="error",
                slide_index=slide_index,
                json_pointer=slide_pointer,
                message="the configured CJK font is unavailable for rendered Chinese text",
                evidence={"families": ", ".join(str(name) for name in slide.get("font", {}).get("families", []))},
                suggested_action="install a supported CJK font and rerun the review preview",
            ))
        regions = _text_regions(slide)
        visible_text = [str(text) for text in slide.get("visible_text", []) if str(text).strip()]
        if not visible_text:
            visible_text = [str(region.get("text", "")) for region in regions if str(region.get("text", "")).strip()]
        if not visible_text and not slide.get("elements") and not slide.get("images"):
            issues.append(_issue(
                code="visual-blank-slide",
                severity="error",
                slide_index=slide_index,
                json_pointer=slide_pointer,
                message="the rendered review canvas contains no visible semantic content",
                evidence={},
                suggested_action="add the intended slide content or remove the empty slide",
            ))
        for text in visible_text:
            if "[MISSING:" in str(text) or "[UNVERIFIED:" in str(text):
                issues.append(_issue(
                    code="visual-integrity-marker",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=slide_pointer,
                    message="a visible unresolved integrity marker remains on the slide",
                    evidence={"text": str(text)},
                    suggested_action="resolve the underlying evidence before seeking CKPT-2 approval",
                ))
                break
        for region in regions:
            text = str(region.get("text", "")).strip()
            phrase = _placeholder_phrase(text)
            if not phrase:
                continue
            pointer = str(region.get("json_pointer", slide_pointer))
            if not pointer.startswith("/"):
                pointer = slide_pointer
            evidence = {"dom_selector": str(region.get("dom_selector", "")), "phrase": phrase, "text": text}
            issues.append(_issue(
                code="visual-placeholder-text",
                severity="error",
                slide_index=slide_index,
                json_pointer=pointer,
                message="visible placeholder or fixture text remains in the review canvas",
                evidence=evidence,
                suggested_action="replace the placeholder with reviewed content; ordinary prose mentioning an example is allowed",
            ))
        for text in visible_text:
            if _INTERNAL_REVIEW_TEXT.search(text):
                issues.append(_issue(
                    code="visual-internal-review-text",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=slide_pointer,
                    message="visible checkpoint or audit-process text leaked into the slide",
                    evidence={"text": text},
                    suggested_action="remove internal workflow language from audience-facing slide text",
                ))
                break
        metrics = _semantic_metrics(slide, role, canvas_width, canvas_height)
        if metrics is not None:
            semantic_metrics_by_slide.append({"slide_index": slide_index, **metrics})
            threshold = _ROLE_THRESHOLDS[role]
            underfilled = (
                metrics["semantic_area_ratio"] < float(threshold["min_area"])
                or metrics["horizontal_span_ratio"] < float(threshold["min_horizontal"])
                or metrics["vertical_span_ratio"] < float(threshold["min_vertical"])
            )
            if underfilled:
                code = {
                    "table": "visual-table-underfilled",
                    "equation": "visual-equation-underfilled",
                    "image": "visual-image-underfilled",
                    "two-column": "visual-columns-underfilled",
                    "references": "visual-references-underfilled",
                }.get(role, "visual-content-underfilled")
                evidence = dict(metrics)
                evidence["thresholds"] = {
                    "min_area": threshold["min_area"],
                    "min_horizontal": threshold["min_horizontal"],
                    "min_vertical": threshold["min_vertical"],
                }
                issues.append(_issue(
                    code=code,
                    severity=str(threshold["severity"]),
                    slide_index=slide_index,
                    json_pointer=slide_pointer,
                    message=f"semantic {role} content occupies too little of the review canvas",
                    evidence=evidence,
                    suggested_action="increase the role-appropriate content area or intentionally document the whitespace for human review",
                ))
                bbox = metrics.get("bbox", {})
                if role not in {"title", "references"} and isinstance(bbox, dict) and isinstance(canvas_width, (int, float)):
                    center_ratio = (float(bbox.get("x", 0)) + float(bbox.get("width", 0)) / 2.0) / float(canvas_width)
                    if center_ratio < 0.22 or center_ratio > 0.78:
                        issues.append(_issue(
                            code="visual-layout-unbalanced",
                            severity="warning",
                            slide_index=slide_index,
                            json_pointer=slide_pointer,
                            message="underfilled semantic content is heavily biased toward one canvas edge",
                            evidence={"content_center_ratio": round(center_ratio, 6), **metrics},
                            suggested_action="rebalance the content block or choose a layout whose visual anchor matches the evidence",
                        ))

            semantic_elements = [element for element in slide.get("semantic_elements", []) if isinstance(element, dict)]
            if role == "table":
                table_elements = [element for element in semantic_elements if str(element.get("semantic_role", element.get("role", ""))).casefold() == "table"]
                table = deck_slide.get("table") if isinstance(deck_slide.get("table"), dict) else {}
                rows = table.get("rows") if isinstance(table.get("rows"), list) else []
                columns = table.get("columns") if isinstance(table.get("columns"), list) else []
                expected_cells = len(rows) * len(columns) if rows and columns else 0
                observed_cells = sum(int(element.get("table_cells", 0) or 0) for element in table_elements)
                if expected_cells and observed_cells and observed_cells < expected_cells:
                    issues.append(_issue(
                        code="visual-table-density",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=f"{slide_pointer}/table",
                        message="the rendered table exposes fewer cells than its declared data shape",
                        evidence={"expected_cells": expected_cells, "observed_cells": observed_cells, **metrics},
                        suggested_action="check table row/column density and keep every declared value visible",
                    ))
                table_sizes = [_number(element.get("font_size_px")) for element in table_elements]
                table_sizes = [size for size in table_sizes if size is not None and size > 0]
                if table_sizes and min(table_sizes) < _ROLE_FONT_THRESHOLDS["table"]:
                    issues.append(_issue(
                        code="visual-table-font-small",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=f"{slide_pointer}/table",
                        message="the rendered table font is below the theme readability threshold",
                        evidence={"font_size_px": min(table_sizes), **metrics},
                        suggested_action="increase table font size or split the table before presenting it",
                    ))
            if role == "equation":
                equations = [element for element in semantic_elements if str(element.get("semantic_role", element.get("role", ""))).casefold() == "equation"]
                font_sizes = [_number(element.get("font_size_px")) for element in equations]
                font_sizes = [size for size in font_sizes if size is not None and size > 0]
                if font_sizes and min(font_sizes) < _ROLE_FONT_THRESHOLDS["equation"]:
                    issues.append(_issue(
                        code="visual-equation-font-small",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="the rendered equation font is below the large-format review threshold",
                        evidence={"font_size_px": min(font_sizes), **metrics},
                        suggested_action="increase equation scale or split the equation across slides",
                    ))
                context_fields = ("title", "action_title", "note", "source_ref", "annotation")
                if not any(str(deck_slide.get(field, "")).strip() for field in context_fields):
                    issues.append(_issue(
                        code="visual-equation-context-missing",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="the equation has no nearby explanatory title, note, annotation, or source context",
                        evidence={"equation_count": len(equations), **metrics},
                        suggested_action="add one concise interpretation or provenance cue next to the equation",
                    ))
            if role == "image":
                figure_elements = [element for element in semantic_elements if str(element.get("semantic_role", element.get("role", ""))).casefold() == "figure"]
                figure = deck_slide.get("figure") if isinstance(deck_slide.get("figure"), dict) else {}
                if not figure_elements and not slide.get("images"):
                    issues.append(_issue(
                        code="visual-image-context-missing",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="the image layout has no visible figure element",
                        evidence=dict(metrics),
                        suggested_action="add the reviewed figure asset or use a text layout",
                    ))
                elif not any(str(figure.get(field, "")).strip() for field in ("caption", "cite")) and not str(deck_slide.get("source_ref", "")).strip():
                    issues.append(_issue(
                        code="visual-image-context-missing",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="the visible image has no caption or source context",
                        evidence={"asset": str(figure.get("src", "")), **metrics},
                        suggested_action="add a short caption or source reference that explains the visual claim",
                    ))
            if role == "two-column":
                columns = slide.get("columns")
                if isinstance(columns, list):
                    empty_columns = [column for column in columns if isinstance(column, dict) and not bool(column.get("has_content"))]
                    for column in empty_columns:
                        issues.append(_issue(
                            code="visual-column-empty",
                            severity="warning",
                            slide_index=slide_index,
                            json_pointer=slide_pointer,
                            message="a two-column content region is visibly empty",
                            evidence={"column_index": column.get("index"), "kind": column.get("kind"), **metrics},
                            suggested_action="fill the empty column with evidence or switch to a single-column layout",
                        ))
                    widths = [_number(column.get("width")) for column in columns if isinstance(column, dict)]
                    widths = [width for width in widths if width is not None and width > 0]
                    if len(widths) >= 2 and max(widths) / min(widths) > 2.0:
                        issues.append(_issue(
                            code="visual-columns-unbalanced",
                            severity="warning",
                            slide_index=slide_index,
                            json_pointer=slide_pointer,
                            message="the two visible columns are substantially unbalanced in width",
                            evidence={"widths": widths, "ratio": round(max(widths) / min(widths), 3), **metrics},
                            suggested_action="rebalance the columns or use a layout that matches the evidence shape",
                        ))
            if role == "references":
                reference_sizes = [_number(element.get("font_size_px")) for element in semantic_elements if str(element.get("semantic_role", element.get("role", ""))).casefold() == "body"]
                reference_sizes = [size for size in reference_sizes if size is not None and size > 0]
                if reference_sizes and min(reference_sizes) < _ROLE_FONT_THRESHOLDS["references"]:
                    issues.append(_issue(
                        code="visual-reference-font-small",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="reference text is below the review readability threshold",
                        evidence={"font_size_px": min(reference_sizes), **metrics},
                        suggested_action="reduce the reference count or split references across slides before shrinking the font",
                    ))
        overlap_candidates: list[dict[str, Any]] = []
        for element in slide.get("elements", []):
            if not isinstance(element, dict):
                continue
            client_width = element.get("client_width")
            client_height = element.get("client_height")
            scroll_width = element.get("scroll_width")
            scroll_height = element.get("scroll_height")
            if not all(isinstance(value, (int, float)) for value in (client_width, client_height, scroll_width, scroll_height)):
                continue
            if element.get("overflow_check", True) and (scroll_width > client_width + 1 or scroll_height > client_height + 1):
                issues.append(_issue(
                    code="visual-text-overflow",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=str(element.get("json_pointer", "")),
                    message="text exceeds its rendered width or height",
                    evidence={
                        "client_height": client_height,
                        "client_width": client_width,
                        "scroll_height": scroll_height,
                        "scroll_width": scroll_width,
                    },
                    suggested_action="shorten the text or enlarge its allocated layout region",
                ))
            role = str(element.get("role", "body"))
            font_size = _number(element.get("font_size_px"))
            if role == "title" and isinstance(element.get("line_count"), int) and element["line_count"] > 2:
                issues.append(_issue(
                    code="visual-title-line-count",
                    severity="warning",
                    slide_index=slide_index,
                    json_pointer=str(element.get("json_pointer", slide_pointer)),
                    message="the rendered title spans more than two lines",
                    evidence={"line_count": element["line_count"]},
                    suggested_action="shorten the title or use a layout with more title space",
                ))
            if role == "title" and font_size is not None and font_size < 24:
                issues.append(_issue(
                    code="visual-title-font-too-small",
                    severity="warning",
                    slide_index=slide_index,
                    json_pointer=str(element.get("json_pointer", slide_pointer)),
                    message="the rendered title font is below the review readability threshold",
                    evidence={"font_size_px": font_size},
                    suggested_action="increase the title font size or simplify the title",
                ))
            if role == "table" and font_size is not None and font_size < 16:
                issues.append(_issue(
                    code="visual-table-font-too-small",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=str(element.get("json_pointer", slide_pointer)),
                    message="the rendered table font is below the minimum readable size",
                    evidence={"font_size_px": font_size, "table_cells": element.get("table_cells", 0)},
                    suggested_action="enlarge the table or split it across slides",
                ))
            if bool(element.get("overlap_candidate")):
                overlap_candidates.append(element)
            x, y, width, height = (element.get(name) for name in ("x", "y", "width", "height"))
            if all(isinstance(value, (int, float)) for value in (x, y, width, height, canvas_width, canvas_height)) and (x < -2 or y < -2 or x + width > canvas_width + 2 or y + height > canvas_height + 2):
                issues.append(_issue(
                    code="visual-element-off-canvas",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=str(element.get("json_pointer", "")),
                    message="a semantic slide element extends outside the review canvas",
                    evidence={"height": height, "width": width, "x": x, "y": y},
                    suggested_action="resize or reposition the element inside the slide canvas",
                ))
        for index, first in enumerate(overlap_candidates):
            first_area = (_number(first.get("width")) or 0) * (_number(first.get("height")) or 0)
            for second in overlap_candidates[index + 1:]:
                second_area = (_number(second.get("width")) or 0) * (_number(second.get("height")) or 0)
                overlap = _overlap_area(first, second)
                if overlap <= 0 or overlap < min(first_area, second_area) * 0.2:
                    continue
                issues.append(_issue(
                    code="visual-high-risk-overlap",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=str(second.get("json_pointer", slide_pointer)),
                    message="two independent semantic elements substantially overlap",
                    evidence={"overlap_area": round(overlap, 2), "other_json_pointer": str(first.get("json_pointer", slide_pointer))},
                    suggested_action="separate the overlapping content regions before approval",
                ))
                overlap_candidates = []
                break
            if not overlap_candidates:
                break
        for image in slide.get("images", []):
            if not isinstance(image, dict):
                continue
            if not image.get("complete") or not isinstance(image.get("natural_width"), int) or not isinstance(image.get("natural_height"), int) or image["natural_width"] < 1 or image["natural_height"] < 1:
                issues.append(_issue(
                    code="visual-image-load-failed",
                    severity="error",
                    slide_index=slide_index,
                    json_pointer=slide_pointer,
                    message="a review image did not load with natural dimensions",
                    evidence={"src": str(image.get("src", ""))},
                    suggested_action="restore the graph-bound local image and rerun the review preview",
                ))
                continue
            display_width = _number(image.get("width"))
            display_height = _number(image.get("height"))
            natural_width = _number(image.get("natural_width"))
            natural_height = _number(image.get("natural_height"))
            if None not in {display_width, display_height, natural_width, natural_height}:
                if natural_width < display_width or natural_height < display_height:
                    issues.append(_issue(
                        code="visual-image-low-resolution",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="an image is displayed larger than its natural pixel dimensions",
                        evidence={"display_height": display_height, "display_width": display_width, "natural_height": natural_height, "natural_width": natural_width},
                        suggested_action="use a higher-resolution source image or reduce its displayed size",
                    ))
                natural_ratio = natural_width / natural_height if natural_height else 0
                display_ratio = display_width / display_height if display_height else 0
                if str(image.get("object_fit", "")) == "cover" and natural_ratio and display_ratio and abs(natural_ratio - display_ratio) / display_ratio > 0.15:
                    issues.append(_issue(
                        code="visual-image-unexpected-crop",
                        severity="warning",
                        slide_index=slide_index,
                        json_pointer=slide_pointer,
                        message="a cover-fitted image aspect ratio implies visible cropping",
                        evidence={"display_ratio": round(display_ratio, 4), "natural_ratio": round(natural_ratio, 4)},
                        suggested_action="review the crop or use contain fitting for the full visual",
                    ))
    issues.sort(key=lambda item: (item["slide_index"], item["code"], item["json_pointer"]))
    summary = {
        "errors": sum(item["severity"] == "error" for item in issues),
        "warnings": sum(item["severity"] == "warning" for item in issues),
        "info": sum(item["severity"] == "info" for item in issues),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "error" if summary["errors"] else "pass",
        "inputs": {
            "deck_sha256": deck_sha256,
            "asset_graph_sha256": asset_graph_sha256,
            "renderer_version": renderer_version,
        },
        "render": {
            "slide_count": diagnostics.get("slide_count", len(deck.get("slides", []))),
            "viewport": diagnostics.get("viewport", {}),
            "language": deck.get("meta", {}).get("language", "en"),
            "semantic_metrics": semantic_metrics_by_slide,
        },
        "summary": summary,
        "issues": issues,
        "human_review_required": True,
        "human_review_checklist": list(_HUMAN_REVIEW_CHECKLIST),
    }
