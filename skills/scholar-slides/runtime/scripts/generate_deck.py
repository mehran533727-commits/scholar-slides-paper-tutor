#!/usr/bin/env python3
"""Generate a checkpoint-ready initial ``deck.json`` from an extractive digest.

The generator intentionally produces a *review draft*, not an asserted interpretation
of the paper.  It can place only material that already exists in ``digest.json`` and
paper-owned PNG crops.  Every semantic gap stays visible as an integrity marker, so
the resulting file cannot be silently treated as a finished scientific presentation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from checkpoint import CheckpointError, require_approved_checkpoint, sha256_file
from audience_text import mask_non_claim_numeric_spans, sanitize_audience_text
from ckpt1_resolved import CKPT1ResolvedViewError, build_confirmed_semantic_digest
from content_policy import validate_visible_content
from deck_types import DeckTypeError, get_deck_contract, resolve_deck_options
from marker_policy import MarkerPolicyError, assert_assets_allowed, forbidden_assets_for_next_stage, load_marker_ledger
from narrative_planner import plan_narrative, quantitative_source_key
from notes_writer import apply_speaker_notes
from quantitative_coverage import (
    QuantitativeCoverageError,
    build_coverage_artifact,
    collect_quantitative_requirements,
    display_lines,
    load_quantitative_audit,
    missing_coverage_tokens,
    visible_text,
)
from semantic_evidence import reviewed_semantic_slot_records
from user_documents import write_presentation_documents


_SPEAKER_ROLE_BY_NARRATIVE = {
    "title": "title", "background": "background", "problem": "research-question",
    "question": "research-question", "method": "method-overview", "process": "experiment",
    "metrics": "concept-or-metric", "contribution": "comparison", "evidence": "comparison",
    "results": "results-table", "analysis": "analysis", "conclusion": "conclusion",
    "discussion": "presenter-discussion", "sources": "references",
}


def _load_digest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"digest artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid digest JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected digest JSON shape in {path}: expected object")
    if payload.get("extractive_only") is not True:
        raise ValueError("digest must declare extractive_only=true before automatic deck generation")
    return payload


def _text(value: object, fallback: str) -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or fallback


def _provenance_record(item: Mapping[str, Any], *, kind: str, asset_id: str | None = None) -> dict[str, Any]:
    """Normalize one reviewed claim or selected visual into an auditable source record."""
    page = item.get("source_page", item.get("page"))
    locator = _text(item.get("locator"), _text(item.get("figure_table_equation"), _text(item.get("label"), "")))
    section = _text(item.get("section"), "Source")
    record: dict[str, Any] = {
        "kind": kind,
        "source_page": page,
        "section": section,
        "locator": locator,
    }
    source_ref = _text(item.get("source_ref"), "")
    if not source_ref and isinstance(page, int) and locator:
        source_ref = f"p. {page} — {section} — {locator}"
    if source_ref:
        record["source_ref"] = source_ref
    if asset_id:
        record["asset_id"] = asset_id
    evidence_id = item.get("evidence_id")
    if isinstance(evidence_id, str) and evidence_id:
        record["evidence_id"] = evidence_id
    return record


def _same_source(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return False
    return (
        left.get("source_page") == right.get("source_page")
        and _text(left.get("locator"), "").casefold() == _text(right.get("locator"), "").casefold()
    )


def _claim_key(value: Any) -> str:
    """Normalize a viewer-facing claim for deterministic within-slide deduping."""
    text = sanitize_audience_text(value, "").casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE)


def _unique_audience_claims(*groups: Sequence[Any]) -> list[list[str]]:
    """Keep the first visible occurrence while preserving per-role group order."""
    seen: set[str] = set()
    projected: list[list[str]] = []
    for group in groups:
        values: list[str] = []
        for value in group:
            text = sanitize_audience_text(value, "")
            key = _claim_key(text)
            if not text or not key or key in seen:
                continue
            seen.add(key)
            values.append(text)
        projected.append(values)
    return projected


def _flow_action_title(planned: Mapping[str, Any], settings: Mapping[str, Any]) -> str:
    """Build a concise audience label from the source-bound endpoint stages."""
    diagram = planned.get("native_diagram") if isinstance(planned.get("native_diagram"), Mapping) else {}
    nodes = diagram.get("nodes") if isinstance(diagram.get("nodes"), list) else []
    labels = [
        sanitize_audience_text(node.get("label"), "")
        for node in nodes
        if isinstance(node, Mapping) and sanitize_audience_text(node.get("label"), "")
    ]
    language = _text(settings.get("language"), "zh").casefold()
    cjk = language.startswith("zh") or any(re.search(r"[\u3400-\u9fff]", value) for value in labels)
    if len(labels) >= 3 and len(labels[0]) <= 44 and len(labels[-1]) <= 44:
        return (
            f"从{labels[0]}到{labels[-1]}的方法流程"
            if cjk else f"Method flow from {labels[0]} to {labels[-1]}"
        )
    role = _text(planned.get("semantic_role"), _text(planned.get("role"), "method")).casefold()
    if role in {"method", "process"}:
        return "方法流程" if cjk else "Method flow"
    return "证据链" if cjk else "Evidence flow"


def _role_action_title(role: str, settings: Mapping[str, Any]) -> str:
    """Return a neutral audience label when the detailed claim lives in the body."""
    language = _text(settings.get("language"), "zh").casefold()
    cjk = language.startswith("zh")
    labels = {
        "background": ("研究背景", "Research context"),
        "problem": ("研究问题", "Research problem"),
        "question": ("研究问题", "Research question"),
        "method": ("方法概览", "Method overview"),
        "process": ("方法流程", "Method flow"),
        "metrics": ("评价指标", "Evaluation metrics"),
        "contribution": ("主要贡献", "Main contribution"),
        "evidence": ("关键证据", "Key evidence"),
        "results": ("主要结果", "Main result"),
        "analysis": ("结果分析", "Result analysis"),
        "conclusion": ("结论", "Conclusion"),
    }
    zh, en = labels.get(role, ("论文证据", "Paper evidence"))
    return zh if cjk else en


def _nonduplicating_action_title(
    role: str, takeaway: str, body_values: Sequence[Any], settings: Mapping[str, Any],
) -> str:
    title_key = _claim_key(takeaway)
    if title_key and any(_claim_key(value) == title_key for value in body_values):
        return _role_action_title(role, settings)
    return takeaway


def _provenance_display(
    claim: Mapping[str, Any] | None,
    visual: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Naturalize claim/visual provenance without changing either binding."""
    if not isinstance(claim, Mapping) or not isinstance(visual, Mapping):
        return None
    claim_ref = _text(claim.get("source_ref"), "")
    visual_ref = _text(visual.get("source_ref"), "")
    if not claim_ref or not visual_ref:
        return None
    language = _text(settings.get("language"), "zh").casefold()
    cjk = language.startswith("zh")
    if _same_source(claim, visual):
        return {
            "entries": [{
                "role": "claim_visual",
                "label": "依据与图示" if cjk else "Evidence and visual",
                "source_ref": claim_ref,
            }]
        }
    return {
        "entries": [
            {
                "role": "claim",
                "label": "主线依据" if cjk else "Claim evidence",
                "source_ref": claim_ref,
            },
            {
                "role": "illustration",
                "label": "示意图" if cjk else "Illustration",
                "source_ref": visual_ref,
            },
        ]
    }


def _figure_caption(asset: Mapping[str, Any], fallback: str = "Figure") -> str:
    """Keep a source-grounded caption concise enough to preserve figure area."""
    label = _text(asset.get("label"), fallback)
    caption = _text(asset.get("caption"), label)
    if len(caption) <= 180:
        return caption
    # Do not split the common ``Fig.`` abbreviation while shortening a long
    # source-grounded caption.  The caption remains evidence-bound; only the
    # presentation length is reduced.
    head = re.split(r"(?<!Fig)\.\s+|[!?。！？]\s*", caption, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return head or label


def _figure_for_role(
    asset: Mapping[str, Any], *, role: str, fallback_caption: str, source_ref: str
) -> dict[str, Any]:
    """Build a source-bound figure spec and reclaim space for stage visuals.

    Method/process figures are explanatory evidence, so the figure itself should
    remain the visual protagonist.  The hero mode folds its caption into the
    provenance footer and moves the longer narrative explanation to notes; this
    prevents a long annotation from shrinking the reviewed figure below the
    projection-legibility floor.  The rule is role- and asset-kind based, not
    tied to a paper, figure number, or domain.
    """
    figure = {
        "src": asset["src"],
        "caption": _figure_caption(asset, fallback_caption),
        "cite": _text(asset.get("source_ref"), source_ref),
        "alt": f"Paper-owned {asset['id']}",
        "fit": "contain",
    }
    kind = _text(asset.get("kind"), "").casefold()
    if role in {"method", "process"} and kind in {"figure", "diagram"}:
        figure["hero"] = True
    return figure


def _reviewed_numbers(values: Sequence[Mapping[str, Any]]) -> list[str]:
    """Collect numeric claims from every reviewed item visibly summarized on a slide."""
    numbers: list[str] = []
    for item in values:
        for field in ("summary", "evidence", "rows", "key_numbers", "speaker_key_values"):
            raw = item.get(field)
            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False) if raw is not None else ""
            # A reviewed sentence may end with a period immediately after its
            # final numeric token.  The token boundary must reject word
            # continuation without treating that terminal punctuation as part
            # of the number.
            for token in re.findall(r"(?<![\w.])\d+(?:\.\d+)?%?(?![\w])", mask_non_claim_numeric_spans(text)):
                if token not in numbers:
                    numbers.append(token)
    return numbers


def _authors_display(metadata: dict[str, Any]) -> str:
    """Expose the shared metadata authors or retain an explicit identity marker."""
    authors = metadata.get("authors")
    if isinstance(authors, list) and all(isinstance(author, str) and author.strip() for author in authors) and authors:
        return ", ".join(authors)
    return "[MISSING: authors not reliably extracted from PDF p. 1]"


def _reviewed_title(digest: dict[str, Any]) -> str:
    """Use the confirmed paper title, falling back only to bound source identity."""
    metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    source = digest.get("source") if isinstance(digest.get("source"), dict) else {}
    for value in (metadata.get("title"), digest.get("title"), source.get("resolved_identifier"), source.get("source_input")):
        candidate = _text(value, "")
        if candidate and not candidate.startswith("[MISSING") and not candidate.startswith("[UNVERIFIED"):
            return candidate
    return "Paper source"


def _grounded_source_identity(source: dict[str, Any]) -> str:
    for value in (source.get("resolved_identifier"), source.get("requested_identifier"), source.get("source_input")):
        candidate = _text(value, "")
        if candidate and not candidate.startswith("[MISSING") and not candidate.startswith("[UNVERIFIED"):
            return candidate
    return "Paper source"


def _safe_source_display_identity(value: Any) -> str:
    """Keep local paths and file URIs out of viewer-facing title metadata."""
    candidate = _text(value, "")
    if not candidate or candidate.startswith("[") or candidate.casefold().startswith("file://"):
        return ""
    if re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2})", candidate) or "\\" in candidate:
        return ""
    if "/" in candidate and not re.match(r"^10\.\d{4,9}/", candidate, re.IGNORECASE):
        return ""
    return candidate


def _safe_bound_source_identity(source: dict[str, Any]) -> str:
    """Prefer a public resolved identifier, then fall back from unsafe paths."""
    for key in ("resolved_identifier", "requested_identifier", "source_input"):
        candidate = _safe_source_display_identity(source.get(key))
        if candidate:
            return candidate
    return ""


def _metadata_version(paper_metadata: dict[str, Any], source: dict[str, Any]) -> str:
    """Use the digest's version identity, then its bound source identity."""
    version = paper_metadata.get("version")
    if isinstance(version, dict):
        for key in ("resolved", "version", "base"):
            value = _text(version.get(key), "")
            if value and not value.startswith("["):
                return value
    value = _text(version, "")
    if value and not value.startswith("["):
        return value
    identifiers = paper_metadata.get("identifiers")
    arxiv = identifiers.get("arxiv") if isinstance(identifiers, dict) else None
    if isinstance(arxiv, dict):
        value = _text(arxiv.get("resolved_id"), "")
        if value and not value.startswith("["):
            return value
    return _grounded_source_identity(source)


def _title_venue(paper_metadata: dict[str, Any], source: dict[str, Any]) -> str:
    """Build a concise venue/version line from source-bound metadata only."""
    venue = _text(paper_metadata.get("venue"), "")
    version = _metadata_version(paper_metadata, source)
    if venue:
        safe_version = _safe_source_display_identity(version)
        return " · ".join(value for value in (venue, safe_version) if value)
    # A source identifier is still useful cover metadata when no venue was
    # verified.  Keep its provenance visible instead of presenting a bare
    # version token that could be mistaken for a venue.
    source_identity = _safe_bound_source_identity(source)
    if source_identity:
        normalized = source_identity.strip()
        if normalized.casefold().startswith("arxiv:"):
            return normalized
        if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", normalized, re.IGNORECASE):
            return f"arXiv:{normalized}"
        return f"Source: {normalized}"
    safe_version = _safe_source_display_identity(version)
    if safe_version:
        normalized = safe_version.strip()
        if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", normalized, re.IGNORECASE):
            return f"arXiv:{normalized}"
        return f"Version: {normalized}"
    return "Paper source"


def _deck_type(settings: dict[str, Any]) -> str:
    """Return only a registered public deck type."""
    return get_deck_contract(settings.get("deck_type")).deck_type.value


def _title_presenter(settings: dict[str, Any], checkpoint_record: dict[str, Any] | None = None) -> str:
    """Expose configured presenter and deck type without template placeholders."""
    presenter = _text(settings.get("presenter"), "")
    if not presenter and isinstance(checkpoint_record, dict):
        presenter = _text(checkpoint_record.get("confirmed_by"), _text(checkpoint_record.get("intended_confirmer"), ""))
    if presenter and not presenter.casefold().startswith("汇报人"):
        presenter = f"汇报人：{presenter}"
    deck_type = _deck_type(settings)
    return " · ".join(value for value in (presenter, deck_type) if value)


def _require_verified_cover_metadata(digest: dict[str, Any], *, resolved_metadata: dict[str, Any] | None = None) -> None:
    """Fail closed when a reviewed deck would put an uncertain identity on its cover.

    After CKPT-1 confirmation the authoritative cover identity comes from the
    resolved view (resolve_ckpt1_view).  Without a confirmed resolved view the
    raw digest evidence status must still be VERIFIED.
    """
    if (
        isinstance(resolved_metadata, dict)
        and isinstance(resolved_metadata.get("title"), str)
        and resolved_metadata["title"].strip()
        and isinstance(resolved_metadata.get("authors"), list)
        and resolved_metadata["authors"]
        and all(isinstance(item, str) and item.strip() for item in resolved_metadata["authors"])
    ):
        return
    metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {}
    for field in ("title", "authors"):
        record = evidence.get(field) if isinstance(evidence, dict) else None
        if not isinstance(record, dict) or record.get("status") != "VERIFIED":
            raise ValueError(f"reviewed deck requires paper_metadata.{field} evidence status VERIFIED")


def _notes(text: str) -> str:
    """Give every draft slide a concise, non-assertive speaking cue."""

    return f"本页仅展示已入库的论文证据。{text}；请将讨论与论文原文明确区分。"


def _figure_slides(bundle: Path, digest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return figure slides only for real, local crop files, plus visible missing-crop flags."""

    slides: list[dict[str, Any]] = []
    flags: list[str] = []
    figures = digest.get("figures") if isinstance(digest.get("figures"), list) else []
    for figure in figures[:4]:  # 11 fixed slides + at most 4 figures = 10--15-page MVP.
        if not isinstance(figure, dict):
            continue
        figure_id = _text(figure.get("id"), "")
        label = _text(figure.get("label"), "[MISSING: figure/table label]")
        if not figure_id or not (bundle / "figures" / f"{figure_id}.png").is_file():
            flags.append(f"[MISSING: {label} crop is unavailable for deck generation]")
            continue
        caption = _text(figure.get("caption"), "[MISSING: caption]")
        source_ref = _text(figure.get("source_ref"), f"[UNVERIFIED: source for {label}]")
        slides.append(
            {
                "layout": "assertion-evidence",
                "eyebrow": "论文原始图表",
                "action_title": f"{label}：原始图表证据（解释待人工核验）",
                "figure": {
                    "src": f"figures/{figure_id}.png",
                    "caption": caption,
                    "cite": source_ref,
                    "alt": f"Paper-owned {label}",
                    "fit": "contain",
                },
                "annotation": "[UNVERIFIED: 图表所支持的论点尚未由人工逐页确认]",
                "source_ref": source_ref,
                "speaker_notes": _notes(f"请指向 {label}，先朗读或核对其原始图注，再解释图中信息"),
            }
        )
    return slides, flags


def _reviewed_item(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain objects")
    required = ("summary", "source_page", "section", "figure_table_equation", "evidence")
    missing = [field for field in required if not value.get(field)]
    if missing:
        raise ValueError(f"{label} is incomplete: missing {', '.join(missing)}")
    if not isinstance(value["source_page"], int) or value["source_page"] < 1:
        raise ValueError(f"{label}.source_page must be a positive integer")
    return value


def _reviewed_ref(item: dict[str, Any]) -> str:
    return (
        f"p. {item['source_page']} — {item['section']} — "
        f"{item['figure_table_equation']}"
    )


def _audience_evidence(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project reviewed evidence into audience-safe prose while retaining locators."""
    projected: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        for field in ("summary", "evidence"):
            if isinstance(item.get(field), str):
                item[field] = sanitize_audience_text(item[field], item[field])
        projected.append(item)
    return projected


def _table_evidence(digest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the reviewed evidence item that names the audited native table."""
    for item in digest.get("reviewed_experimental_results", []):
        if not isinstance(item, dict):
            continue
        locator = str(item.get("figure_table_equation", "")).casefold()
        if "table" in locator:
            return item
    return None


def _has_hash_bound_quantitative_audits(requirements: Sequence[Mapping[str, Any]]) -> bool:
    """Prefer CKPT-1-bound evidence audits over stale/pending crop review files."""
    return any(
        isinstance(requirement, Mapping) and isinstance(requirement.get("audit_ref"), Mapping)
        for requirement in requirements
    )


def _load_audited_table(bundle: Path) -> tuple[dict[str, Any], str, str]:
    review_dir = bundle / "review-assets"
    audits = sorted(review_dir.glob("*-manual-review.json")) if review_dir.is_dir() else []
    if len(audits) != 1:
        raise ValueError("reviewed deck requires exactly one explicit native-table audit binding")
    audit_path = audits[0]
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid native-table audit JSON: {exc}") from exc
    if not isinstance(audit, dict) or audit.get("status") != "resolved_with_audit":
        raise ValueError("native-table audit must have status=resolved_with_audit")
    asset = audit.get("asset")
    crop = asset.get("crop") if isinstance(asset, dict) else None
    asset_id = asset.get("id") if isinstance(asset, dict) else None
    crop_path_value = crop.get("path") if isinstance(crop, dict) else None
    expected_hash = crop.get("sha256") if isinstance(crop, dict) else None
    if not isinstance(asset_id, str) or not asset_id or not isinstance(crop_path_value, str) or not crop_path_value or not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError("native-table audit requires asset.id and asset.crop.path/sha256 binding")
    relative_crop = Path(crop_path_value)
    if relative_crop.is_absolute() or ".." in relative_crop.parts:
        raise ValueError("native-table audit crop path must be a portable relative bundle path")
    crop_path = (bundle / relative_crop).resolve()
    if not crop_path.is_file() or bundle.resolve() not in crop_path.parents:
        raise ValueError("native-table audit crop path does not identify a local bundle asset")
    review = audit.get("review")
    if not isinstance(review, dict):
        raise ValueError("native-table audit review object is missing")
    columns = review.get("columns")
    rows = review.get("rows")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(column, str) and column for column in columns)
        or not isinstance(rows, list)
        or not rows
    ):
        raise ValueError("native-table audit must contain non-empty columns and rows")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise ValueError("native-table audit rows do not match the audited columns")
    for row in rows:
        if any(not isinstance(cell, (str, int, float, dict)) for cell in row):
            raise ValueError("native-table audit contains an unsupported cell value")
    crop_hash = sha256_file(crop_path)
    if expected_hash != crop_hash:
        raise ValueError("native-table crop SHA-256 does not match its manual audit")
    table = {
        "caption": _text(
            review.get("caption"),
            "论文原始表格",
        ),
        "columns": [{"label": column} for column in columns],
        "rows": rows,
        "footnote": "逐格人工核对；来源页码与表格编号见本页出处。",
        "locator": _text(asset.get("label"), "Table"),
        "source_page": asset.get("pdf_page"),
        "sha256": crop_hash,
        "provenance": {"artifact": audit_path.relative_to(bundle).as_posix()},
    }
    return table, crop_hash, asset_id


def _write_reviewed_outline(destination: Path, slides: list[dict[str, Any]]) -> Path:
    outline = destination.with_name("deck-outline.md")
    lines = [
        "# CKPT-2 Deck Outline",
        "",
        f"- 页数：{len(slides)}",
        "- 语言：中文（zh-CN）",
        "- 状态：pending（未自动批准）",
        "",
    ]
    for index, slide in enumerate(slides, start=1):
        title = _text(slide.get("title") or slide.get("action_title"), f"第 {index} 页")
        conclusion = _text(slide.get("core_conclusion") or slide.get("action_title"), "未提供")
        assets: list[str] = []
        figure = slide.get("figure")
        if isinstance(figure, dict) and isinstance(figure.get("src"), str):
            assets.append(figure["src"])
        table = slide.get("table")
        if isinstance(table, dict):
            assets.append("native table: audited review-assets record")
        lines.extend(
            [
                f"## {index}. {title}",
                "",
                f"- 核心结论：{conclusion}",
                f"- 使用资产：{'; '.join(assets) if assets else '无新增图表资产'}",
                f"- 来源页码/编号：{_text(slide.get('source_ref'), '论文来源见标题页或引用页')}",
                f"- Speaker-note 草稿：{_text(slide.get('speaker_notes'), '未提供')}",
                "",
            ]
        )
    outline.write_text("\n".join(lines), encoding="utf-8")
    return outline


def _reviewed_assets(bundle: Path, digest: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose only local, paper-owned image assets to the narrative planner."""
    deferred: set[str] = set()
    ledger = bundle / "ckpt1-markers.json"
    if ledger.is_file():
        deferred = set(forbidden_assets_for_next_stage(load_marker_ledger(ledger)))
    assets: list[dict[str, Any]] = []
    for item in digest.get("figures", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        asset_id = item["id"]
        if asset_id not in deferred and (bundle / "figures" / f"{asset_id}.png").is_file():
            assets.append({**item, "id": asset_id, "src": f"figures/{asset_id}.png"})
    return assets


def _load_generation_options(bundle: Path, supplied: dict[str, Any] | None) -> dict[str, Any]:
    if supplied is not None:
        return supplied
    path = bundle / "project-options.json"
    if not path.is_file():
        # Let deck-type resolution provide audience and slide defaults.  Keeping
        # only non-contract fallback values prevents a legacy audience label from
        # overriding the journal-club contract's reading-first intent.
        return {"language": "zh-CN", "theme": "journal-club"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    options = payload.get("options") if isinstance(payload, dict) else None
    if not isinstance(options, dict):
        raise ValueError("project-options.json must contain an options object")
    return options


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


_EMPTY_TABLE_CELL_TOKENS = frozenset({"", "-", "–", "—", "n/a", "na", "none", "null"})
_SPLIT_ROW_EMPTY_TOKENS = frozenset({"", "–", "—", "n/a", "na", "none", "null"})
_INTERNAL_TABLE_COLUMN_RE = re.compile(r"(?:^|[\s_()])(?:unused|padding|spacer|placeholder)(?:$|[\s_()])", re.IGNORECASE)


def _meaningful_table_cell(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in _EMPTY_TABLE_CELL_TOKENS
    return True


def _meaningful_split_row(row: Sequence[Any], *, row_header_width: int) -> bool:
    """Keep a split-panel row when its visible cells carry semantic content.

    A literal hyphen is an author-provided missing-value marker and therefore
    remains visible.  Only blank/null values and typographic dash placeholders
    are considered empty for panel-local row pruning.
    """
    for value in row[row_header_width:]:
        if value is None:
            continue
        if isinstance(value, str) and value.strip().casefold() in _SPLIT_ROW_EMPTY_TOKENS:
            continue
        return True
    return False


def _native_table_from_audit(
    audit: Mapping[str, Any], *, requirements: Sequence[Mapping[str, Any]] = (), locator: str = "",
    audit_ref: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an editable comparison table from hash-bound reviewed cells."""
    cells = audit.get("reviewed_cells") if isinstance(audit, Mapping) else None
    if not isinstance(cells, list) or not cells:
        raise ValueError("native quantitative table requires reviewed_cells")
    rows: list[str] = []
    columns: list[str] = []
    values: dict[tuple[str, str], str] = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        row = _text(cell.get("row_key"), "")
        column = _text(cell.get("column_key"), "")
        # The reviewed ``value`` is the source-facing display representation;
        # it may carry meaningful trailing precision, uncertainty, or a unit.
        # ``normalized_value`` remains in the audit for arithmetic/comparison.
        value = cell.get("value", cell.get("normalized_value"))
        if not row or not column or value is None:
            continue
        if row not in rows:
            rows.append(row)
        if column not in columns:
            columns.append(column)
        values[(row, column)] = str(value)
    if not rows or not columns:
        raise ValueError("native quantitative table has no complete row/column cells")
    normalized_columns: list[str] = []
    for column in columns:
        column_values = [values.get((row, column)) for row in rows]
        meaningful = any(_meaningful_table_cell(value) for value in column_values)
        if _INTERNAL_TABLE_COLUMN_RE.search(column) and meaningful:
            raise ValueError(f"native quantitative table contains a populated internal schema column: {column}")
        if meaningful:
            normalized_columns.append(column)
    columns = normalized_columns
    if not columns:
        raise ValueError("native quantitative table has no audience-visible semantic columns")
    table_rows = [[row, *[values.get((row, column), "—") for column in columns]] for row in rows]
    context: list[str] = []
    for requirement in requirements:
        label = _text(requirement.get("label"), "")
        display = _text(requirement.get("display_text"), "")
        if requirement.get("kind") == "key_metric" and display:
            context.append(display)
        if re.search(r"\b\d+\s*trials?\b", display, re.IGNORECASE):
            match = re.search(r"\b\d+\s*trials?\b", display, re.IGNORECASE)
            if match:
                context.append(match.group(0))
        if "cached/non-cached" in display.casefold():
            context.append("Ours 的数值顺序为 cached/non-cached")
        if "其他方法取自" in display:
            context.append("其他方法数值取自其原论文")
    flattened = " ".join(str(cell) for row in table_rows for cell in row)
    if "✗" in flattened:
        context.append("✗ 表示论文中的未完成标记，不改写为 0%。")
    table = {
        "caption": f"{locator}：{_text((requirements[0] if requirements else {}).get('label'), '定量比较')}",
        "columns": [{"label": "方案"}, *[{"label": column} for column in columns]],
        "rows": table_rows,
        "row_header": True,
    }
    if context:
        table["footnote"] = "；".join(dict.fromkeys(context))
    caption_contexts: list[str] = []
    for requirement in requirements:
        display = _text(requirement.get("display_text"), "")
        if not display:
            continue
        head = re.split(r"[:：=]", display, maxsplit=1)[0].strip()
        if head and head not in caption_contexts:
            caption_contexts.append(head)
    if caption_contexts:
        table["caption"] = f"{locator}：{'；'.join(caption_contexts)}"
    source = audit.get("source") if isinstance(audit.get("source"), Mapping) else {}
    source_locator = _text(source.get("locator"), locator)
    source_page = source.get("page")
    if source_locator:
        table["locator"] = source_locator
    if isinstance(source_page, int) and source_page > 0:
        table["source_page"] = source_page
    crop = audit.get("crop") if isinstance(audit.get("crop"), Mapping) else {}
    crop_hash = _text(crop.get("sha256"), "")
    if crop_hash:
        # Semantic QA validates the native table against the audit crop linked
        # from the asset graph; this is metadata only and is not rendered.
        table["source_sha256"] = crop_hash
    audit_path = _text(audit_ref.get("path"), "") if isinstance(audit_ref, Mapping) else ""
    if audit_path:
        table["provenance"] = {"artifact": audit_path}
    return table


def _native_table_for_requirements(bundle: Path, requirements: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    refs = [requirement.get("audit_ref") for requirement in requirements if isinstance(requirement.get("audit_ref"), Mapping)]
    refs = [ref for ref in refs if isinstance(ref, Mapping)]
    unique = {(str(ref.get("path")), str(ref.get("sha256"))): ref for ref in refs}
    if len(unique) != 1:
        return None
    audit_ref = next(iter(unique.values()))
    audit = load_quantitative_audit(bundle, audit_ref)
    if _text(audit.get("evidence_type"), "table") == "non_table":
        return None
    locator = _text((requirements[0].get("source") if requirements and isinstance(requirements[0].get("source"), Mapping) else {}).get("locator"), "")
    return _native_table_from_audit(audit, requirements=requirements, locator=locator, audit_ref=audit_ref)


def _native_table_variants(table: Mapping[str, Any], *, max_columns: int = 8) -> list[dict[str, Any]]:
    """Split a wide editable table into readable column panels.

    The split is structural only: every panel keeps the same bound locator,
    source page, crop hash, and provenance metadata.  A row-header column is
    repeated on each panel so the panels remain independently interpretable.
    """
    columns = table.get("columns")
    rows = table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or len(columns) <= max_columns:
        return [dict(table)]
    row_header_width = 1 if table.get("row_header") is True else 0
    data_width = max_columns - row_header_width
    if data_width < 1:
        return [dict(table)]
    data_columns = columns[row_header_width:]
    total_panels = (len(data_columns) + data_width - 1) // data_width
    variants: list[dict[str, Any]] = []
    for panel_index, start in enumerate(range(0, len(data_columns), data_width), start=1):
        end = start + data_width
        panel = dict(table)
        panel["columns"] = [*columns[:row_header_width], *data_columns[start:end]]
        panel_rows: list[list[Any]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            panel_row = [*row[:row_header_width], *row[row_header_width + start:row_header_width + end]]
            if table.get("preserve_empty_rows") is True or table.get("row_group_continuity") is True or _meaningful_split_row(panel_row, row_header_width=row_header_width):
                panel_rows.append(panel_row)
        panel["rows"] = panel_rows
        caption = _text(table.get("caption"), "")
        if caption:
            panel["caption"] = f"{caption} ({panel_index}/{total_panels})"
        footnote = _text(table.get("footnote"), "")
        panel["footnote"] = (
            f"{footnote}; column panel {panel_index}/{total_panels}"
            if footnote else f"column panel {panel_index}/{total_panels}"
        )
        variants.append(panel)
    return variants or [dict(table)]


def _quantitative_group_slide_counts(
    bundle: Path,
    requirements: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], int]:
    """Predict rendered panel counts from the same bound audits used by rendering."""
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for requirement in requirements:
        grouped.setdefault(quantitative_source_key(requirement), []).append(requirement)
    counts: dict[tuple[str, int], int] = {}
    for key, group in grouped.items():
        table = _native_table_for_requirements(bundle, group)
        counts[key] = len(_native_table_variants(table)) if table is not None else 1
    return counts


def _quantitative_panel_label(panel_index: int) -> str:
    label = ""
    while panel_index:
        panel_index, remainder = divmod(panel_index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def _validate_quantitative_rendering(
    deck: Mapping[str, Any],
    requirements: Sequence[Mapping[str, Any]],
) -> None:
    """Fail closed when any required quantitative fact is unassigned or invisible."""
    assigned: dict[str, list[Mapping[str, Any]]] = {}
    for slide in deck.get("slides", []) or []:
        if not isinstance(slide, Mapping):
            continue
        for requirement_id in slide.get("coverage_requirement_ids", []) or []:
            assigned.setdefault(str(requirement_id), []).append(slide)
    unassigned = [requirement for requirement in requirements if requirement["id"] not in assigned]
    if unassigned:
        raise ValueError(
            "quantitative coverage failure: unassigned requirements "
            + ", ".join(str(requirement["id"]) for requirement in unassigned)
        )
    for requirement in requirements:
        missing = missing_coverage_tokens(requirement, visible_text(assigned.get(requirement["id"], [])))
        if missing:
            raise ValueError(
                f"quantitative coverage failure: requirement {requirement['id']} "
                f"missing visible tokens: {', '.join(missing)}"
            )


def _slides_from_narrative(
    plan: dict[str, Any], digest: dict[str, Any], table: dict[str, Any] | None, settings: dict[str, Any],
    *, checkpoint_record: dict[str, Any] | None = None,
    quantitative_requirements: Sequence[Mapping[str, Any]] = (),
    bundle: Path | None = None,
) -> list[dict[str, Any]]:
    """Translate planner roles into editable deck layouts without paper-specific content."""
    requirements_by_id = {
        str(requirement["id"]): requirement
        for requirement in quantitative_requirements
        if isinstance(requirement, Mapping)
    }
    paper_metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    source = digest.get("source") if isinstance(digest.get("source"), dict) else {}
    title = _reviewed_title(digest)
    authors = _authors_display(paper_metadata)
    raw_table_evidence = _table_evidence(digest) if table is not None else None
    table_evidence = _audience_evidence([raw_table_evidence])[0] if raw_table_evidence is not None else None
    table_host_role = (
        "analysis"
        if table_evidence is not None and any(item.get("role") == "analysis" for item in plan["slides"])
        else "results"
    )
    output: list[dict[str, Any]] = []
    has_explicit_table_host = any(bool(item.get("table_host")) for item in plan["slides"])
    for position, planned in enumerate(plan["slides"]):
        role = planned["role"]
        archetypes = list(planned.get("archetypes", ()))
        table_host = bool(planned.get("table_host")) or (
            not has_explicit_table_host and role == table_host_role and table is not None
        )
        speaker_role = _SPEAKER_ROLE_BY_NARRATIVE.get(role)
        if speaker_role is None:
            raise ValueError(f"unknown narrative role for speaker notes: {role}")
        evidence = _audience_evidence(planned["evidence"])
        binding_evidence = evidence
        if table_host and table is not None and table_evidence is not None:
            binding_evidence = [{
                "summary": _text(table_evidence.get("summary"), "论文表格结果"),
                "evidence": _text(table_evidence.get("evidence"), "已绑定表格审计证据"),
                "source_page": table_evidence.get("source_page"),
                "section": _text(table_evidence.get("section"), "结果"),
                "locator": _text(table_evidence.get("figure_table_equation"), "Table"),
            }]
        refs = "；".join(
            f"p. {entry.get('source_page')} — {entry.get('section')} — {entry.get('locator')}"
            for entry in binding_evidence if entry.get("source_page")
        ) or "论文 PDF / reviewed evidence"
        takeaway = sanitize_audience_text(planned["takeaway"], planned["takeaway"])
        slide: dict[str, Any]
        quantitative_ids = planned.get("coverage_requirement_ids") or []
        if "section-divider" in archetypes:
            slide = {"layout": "section", "title": takeaway}
        elif {"limitations", "future-work", "backup", "appendix"} & set(archetypes):
            body_points = [entry["summary"] for entry in evidence] or [takeaway]
            slide = {
                "layout": "bullets", "eyebrow": role,
                "action_title": _nonduplicating_action_title(role, takeaway, body_points, settings),
                "core_conclusion": takeaway,
                "points": body_points,
            }
        elif quantitative_ids:
            lines: list[str] = []
            quantitative_kinds: list[str] = []
            key_numbers: list[str] = []
            technical_terms: list[str] = []
            group_requirements: list[Mapping[str, Any]] = []
            for requirement_id in quantitative_ids:
                requirement = requirements_by_id.get(str(requirement_id))
                if requirement is not None:
                    group_requirements.append(requirement)
                    lines.extend(display_lines(requirement))
                    quantitative_kinds.append(str(requirement.get("kind", "")))
                    for value in requirement.get("speaker_key_values", []) or []:
                        if str(value) not in key_numbers:
                            key_numbers.append(str(value))
                    for value in (requirement.get("speaker_focus"), requirement.get("label")):
                        if isinstance(value, str) and value.strip():
                            technical_terms.append(value)
            native_table = _native_table_for_requirements(bundle, group_requirements) if bundle is not None else None
            planned_panel_label = ""
            if native_table is not None and "quantitative_panel_index" in planned:
                variants = _native_table_variants(native_table)
                panel_index = planned.get("quantitative_panel_index")
                panel_count = planned.get("quantitative_panel_count")
                if (
                    not isinstance(panel_index, int)
                    or isinstance(panel_index, bool)
                    or not isinstance(panel_count, int)
                    or isinstance(panel_count, bool)
                    or panel_count != len(variants)
                    or not 0 <= panel_index < panel_count
                ):
                    raise ValueError("quantitative panel plan does not match the bound native table")
                native_table = variants[panel_index]
                planned_panel_label = _quantitative_panel_label(panel_index + 1)
            slide = {
                "layout": "results-table" if native_table is not None else "bullets",
                "eyebrow": "定量结果",
                "action_title": takeaway,
                "core_conclusion": takeaway,
                "coverage_requirement_ids": [str(item) for item in quantitative_ids],
                "quantitative_kinds": quantitative_kinds,
                "quantitative_index": planned.get("quantitative_index"),
                "quantitative_focus": planned.get("quantitative_focus", "关键定量结果"),
                "quantitative_key_numbers": key_numbers,
                "speaker_technical_terms": technical_terms,
                "source_ref": refs,
                "speaker_evidence_binding": {
                    "kind": "quantitative-coverage",
                    "source_page": binding_evidence[0].get("source_page") if binding_evidence else None,
                    "section": binding_evidence[0].get("section") if binding_evidence else "",
                    "locator": binding_evidence[0].get("locator") if binding_evidence else "",
                    "summary": "；".join(lines),
                    "evidence": binding_evidence[0].get("evidence") if binding_evidence else "",
                    "key_numbers": key_numbers,
                    "focus": planned.get("quantitative_focus", "关键定量结果"),
                    "technical_terms": technical_terms,
                },
                "speaker_notes": "本页数值来自已确认的论文证据，仅展示已入库的数字；讨论不得改写为未证实的新数字。",
            }
            if native_table is not None:
                slide["table"] = native_table
                # Keep a concise, native-text context block alongside the
                # editable matrix.  It is rendered by the results-table layout
                # and gives semantic QA a stable, human-readable projection of
                # the bound requirements without replacing the table.
                slide["points"] = lines or ["已绑定的定量结果见上方原生表格。"]
            if native_table is not None:
                # A core non-table scientific finding can share a source with
                # an audited native table.  Keep that reviewed interpretation
                # visible above the matrix; numeric-only requirements remain
                # represented by the editable cells/caption/footnote.
                slide["points"] = [
                    line
                    for requirement in group_requirements
                    if requirement.get("kind") == "scientific_result"
                    for line in display_lines(requirement)
                ]
                if planned_panel_label:
                    slide["action_title"] = f"{takeaway}（分栏 {planned_panel_label}）"
                    slide["quantitative_panel_group"] = (
                        f"{_text(native_table.get('locator'), 'table')}:"
                        f"{_text(native_table.get('source_sha256'), '')}"
                    )
                    slide["quantitative_panel_label"] = planned_panel_label
            else:
                slide["points"] = lines or ["[MISSING: quantitative display text]"]
        elif role == "title":
            slide = {"layout": "paper-title", "title": title, "authors": authors,
                     "venue": _title_venue(paper_metadata, source),
                     "presenter": _title_presenter(settings, checkpoint_record)}
        elif role == "background" and "problem" in planned.get("merged_roles", []):
            merged_takeaways = planned.get("merged_role_takeaways") if isinstance(planned.get("merged_role_takeaways"), Mapping) else {}
            merged_evidence = planned.get("merged_role_evidence") if isinstance(planned.get("merged_role_evidence"), Mapping) else {}
            problem_takeaway = _text(merged_takeaways.get("problem"), "研究问题证据待确认。")
            problem_evidence = _audience_evidence(merged_evidence.get("problem", []))
            background_claims, problem_claims = _unique_audience_claims(
                [entry["summary"] for entry in evidence[:1]] or [takeaway],
                [entry["summary"] for entry in problem_evidence[:1]] or [problem_takeaway],
            )
            visible_claims = [*background_claims, *problem_claims]
            slide = {
                "layout": "two-column",
                "eyebrow": "背景与问题",
                "action_title": "从研究背景到待解决问题",
                "core_conclusion": "；".join(visible_claims),
                "points": background_claims,
                "points2": problem_claims,
            }
        elif isinstance(planned.get("native_representation"), dict) and not table_host:
            representation = dict(planned["native_representation"])
            labels = [
                _text(node.get("label"), "")
                for node in representation.get("nodes", [])
                if isinstance(node, Mapping) and _text(node.get("label"), "")
            ]
            if representation.get("type") == "comparison" and len(labels) >= 2:
                split = (len(labels) + 1) // 2
                slide = {
                    "layout": "two-column", "eyebrow": role,
                    "action_title": _nonduplicating_action_title(role, takeaway, labels, settings),
                    "core_conclusion": takeaway,
                    "points": labels[:split], "points2": labels[split:],
                    "native_representation": representation,
                }
            else:
                slide = {
                    "layout": "bullets", "eyebrow": role,
                    "action_title": _nonduplicating_action_title(role, takeaway, labels, settings),
                    "core_conclusion": takeaway,
                    "points": labels or [takeaway],
                    "native_representation": representation,
                }
        elif isinstance(planned.get("native_diagram"), dict) and not table_host:
            slide = {"layout": "evidence-flow", "eyebrow": role, "action_title": _flow_action_title(planned, settings),
                     "core_conclusion": takeaway, "native_diagram": dict(planned["native_diagram"])}
        elif role == "sources":
            slide = {"layout": "references", "title": "论文来源与证据", "entries": [
                f"{title}. Authors: {authors}.",
                "正文中的出处标注对应论文页码、章节与图表，便于会后复核。",
            ]}
        elif role == "discussion":
            conclusion_points = [
                _text(value, "")
                for value in planned.get("conclusion_points", [])
                if _text(value, "")
            ]
            questions = list(planned.get("discussion_questions", [takeaway, "哪些证据还需要进一步验证？", "哪些解释属于汇报者讨论而非作者结论？"]))
            if conclusion_points:
                # A merged conclusion/discussion slide must remain readable. Keep
                # two synthesis points, the strongest critique question, and the
                # author/presenter boundary instead of overflowing the canvas.
                selected_discussion = (
                    [f"{questions[0]} {questions[-1]}"]
                    if len(questions) > 1
                    else questions[:1]
                )
                questions = [
                    *[f"结论综合：{point}" for point in conclusion_points[:3]],
                    *selected_discussion,
                ]
            slide = {
                "layout": "discussion-questions",
                "title": "结论与讨论" if conclusion_points else "讨论问题",
                "questions": questions,
                "discussion_grounding": _text(planned.get("discussion_grounding"), "scientific_critique"),
                "discussion_categories": list(planned.get("discussion_categories", [])),
            }
        elif role == "conclusion":
            points = [
                _text(value, "") for value in planned.get("conclusion_points", [])
                if _text(value, "")
            ]
            slide = {
                "layout": "bullets", "eyebrow": role, "action_title": takeaway,
                "core_conclusion": takeaway, "points": points or [takeaway],
            }
        elif role == "analysis" and not table_host:
            slide = {"layout": "critique-concerns", "action_title": takeaway,
                     "points": [{"head": "作者证据", "body": evidence[0]["summary"] if evidence else "[MISSING: reviewed evidence]"},
                                {"head": "汇报者讨论", "body": "将证据的适用范围和替代解释留给讨论。"}]}
        elif role == "method":
            method_points = [takeaway, sanitize_audience_text(planned["process_explanation"], planned["process_explanation"])]
            slide = {"layout": "two-column", "eyebrow": "方法",
                     "action_title": _nonduplicating_action_title(role, takeaway, method_points, settings),
                     "core_conclusion": takeaway, "points": method_points,
                     "points2": [entry["evidence"] for entry in evidence] or ["[MISSING: reviewed method evidence]"]}
        elif role == "process":
            process_explanation = sanitize_audience_text(planned["process_explanation"], planned["process_explanation"])
            process_points = [process_explanation]
            process_evidence = [entry["summary"] for entry in evidence] or ["[MISSING: reviewed process evidence]"]
            slide = {"layout": "two-column", "eyebrow": "方法过程",
                     "action_title": _nonduplicating_action_title(role, takeaway, [*process_points, *process_evidence], settings),
                     "core_conclusion": takeaway, "points": [process_explanation],
                     "points2": process_evidence}
        elif table_host and table is not None:
            # Audit crops establish provenance; the native table is the only visible representation.
            table_takeaway = (
                f"{binding_evidence[0]['locator']}：逐格复核的结果对比。"
                if table_evidence is not None else takeaway
            )
            slide = {"layout": "results-table", "eyebrow": "结果", "action_title": table_takeaway,
                     "core_conclusion": table_takeaway, "table": table}
            table = None
        else:
            body_points = [entry["summary"] for entry in evidence] or [takeaway]
            slide = {"layout": "bullets", "eyebrow": role,
                     "action_title": _nonduplicating_action_title(role, takeaway, body_points, settings),
                     "core_conclusion": takeaway, "points": body_points}
        selected_assets = planned["recommended_assets"]
        selected_asset: Mapping[str, Any] | None = None
        if selected_assets and role != "title" and slide.get("layout") != "results-table":
            asset = selected_assets[0]
            selected_asset = asset
            figure = _figure_for_role(asset, role=role, fallback_caption=asset["id"], source_ref=refs)
            slide.update({"layout": "assertion-evidence", "eyebrow": role, "action_title": takeaway,
                          "core_conclusion": takeaway, "figure": figure})
        if role == "title" and selected_assets:
            asset = selected_assets[0]
            selected_asset = asset
            slide["figure"] = {
                "src": asset["src"], "caption": _figure_caption(asset, ""),
                "cite": _text(asset.get("source_ref"), refs),
                "alt": f"Paper-owned {asset['id']}", "fit": "contain", "hero": True,
            }
        claim_source = _provenance_record(binding_evidence[0], kind="claim") if binding_evidence else None
        claim_locators: list[str] = []
        if claim_source is not None:
            slide["claim_source"] = claim_source
            claim_locators = [
                _text(entry.get("locator"), "")
                for entry in binding_evidence
                if _text(entry.get("locator"), "")
            ]
            if claim_locators:
                # Evidence locators describe the claim, never the optional visual
                # chosen to illustrate it.
                slide["evidence_locators"] = list(dict.fromkeys(claim_locators))
        if selected_asset is not None and isinstance(slide.get("figure"), Mapping):
            visual_source = _provenance_record(selected_asset, kind="visual", asset_id=_text(selected_asset.get("id"), ""))
            visual_source["support_type"] = "claim_support" if _same_source(claim_source, visual_source) else "illustrative_support"
            slide["visual_source"] = visual_source
            slide["speaker_visual_source"] = dict(visual_source)
            slide["figure"] = {**dict(slide["figure"]), "provenance_role": visual_source["support_type"]}
            provenance_display = _provenance_display(claim_source, visual_source, settings)
            if provenance_display is not None:
                slide["provenance_display"] = provenance_display
        slide["source_ref"] = refs
        slide["role"] = speaker_role
        slide["semantic_role"] = planned["semantic_role"]
        slide["asset_policy"] = dict(planned["asset_policy"])
        slide["evidence_locators"] = list(planned["evidence_locators"])
        if claim_locators:
            # The planner may carry the selected visual's locator for asset
            # ranking.  Public evidence bindings remain claim-only.
            slide["evidence_locators"] = list(dict.fromkeys(claim_locators))
        slide["asset_selection"] = dict(planned["asset_selection"])
        for semantic_key in (
            "role_selection", "semantic_evidence_type", "evidence_section", "role_compatibility_score",
            "merged_roles", "merged_role_takeaways", "merged_role_evidence", "merged_role_selections",
            "conclusion_components",
        ):
            if semantic_key in planned:
                value = planned[semantic_key]
                slide[semantic_key] = dict(value) if isinstance(value, Mapping) else list(value) if isinstance(value, list) else value
        slide["archetypes"] = archetypes
        slide["audience"] = _text(planned.get("audience"), _text(settings.get("audience"), ""))
        slide["density"] = _text(planned.get("density"), _text(settings.get("density"), ""))
        if planned.get("role_overlap_reason"):
            slide["role_overlap_reason"] = _text(planned.get("role_overlap_reason"), "")
        if slide.get("layout") == "results-table" and table_evidence is not None:
            # A native table is a results artifact even when the adaptive planner
            # hosted it on its analysis slot.  Keep its locator binding exact.
            slide["role"] = "results-table"
            slide["semantic_role"] = "results"
            table_locator = _text(table_evidence.get("figure_table_equation"), "Table")
            slide["evidence_locators"] = [table_locator]
            selection = dict(slide["asset_selection"])
            selection.update({"candidate_id": None, "conflicts": [], "evidence_locator": table_locator})
            slide["asset_selection"] = selection
        if isinstance(planned.get("native_diagram"), dict) and not table_host:
            slide["native_diagram"] = dict(planned["native_diagram"])
        if binding_evidence:
            selected = binding_evidence[0]
            slide["speaker_allowed_numbers"] = _reviewed_numbers(binding_evidence)
            if slide.get("speaker_evidence_binding", {}).get("kind") != "quantitative-coverage":
                slide["speaker_evidence_binding"] = {
                    "source_page": selected.get("source_page"), "section": selected.get("section"),
                    "locator": selected.get("locator"), "summary": selected.get("summary"),
                }
        role_selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
        if str(role_selection.get("status", "")).casefold() == "missing":
            # A fail-closed role selection must not carry the fallback evidence
            # item into the generated deck or later speaker-note projection.
            slide["speaker_evidence_binding"] = None
            speaker_content = slide.get("speaker_content")
            if isinstance(speaker_content, Mapping):
                cleaned_content = dict(speaker_content)
                cleaned_content["source_refs"] = []
                slide["speaker_content"] = cleaned_content
        if position:
            previous = plan["slides"][position - 1]["takeaway"]
            slide["narrative_previous"] = sanitize_audience_text(previous, previous)
        if position + 1 < len(plan["slides"]):
            following = plan["slides"][position + 1]["takeaway"]
            slide["narrative_next"] = sanitize_audience_text(following, following)
        table_variants = (
            _native_table_variants(slide["table"])
            if slide.get("layout") == "results-table" and isinstance(slide.get("table"), Mapping)
            else [None]
        )
        if len(table_variants) == 1 and table_variants[0] is None:
            output.append(slide)
        else:
            for panel_index, panel in enumerate(table_variants, start=1):
                panel_slide = dict(slide)
                panel_slide["table"] = panel
                if len(table_variants) > 1:
                    panel_label = _quantitative_panel_label(panel_index)
                    panel_slide["action_title"] = (
                        f"{_text(slide.get('action_title'), 'Quantitative results')} "
                        f"（分栏 {panel_label}）"
                    )
                    panel_slide["quantitative_panel_group"] = (
                        f"{_text(panel.get('locator'), 'table')}:{_text(panel.get('source_sha256'), '')}"
                    )
                    panel_slide["quantitative_panel_label"] = panel_label
                output.append(panel_slide)
    return output




def _generate_reviewed_deck(
    bundle: Path,
    digest: dict[str, Any],
    destination: Path,
    options: dict[str, Any] | None = None,
    resolved_metadata: dict[str, Any] | None = None,
) -> Path:
    """Generate a reviewed deck from the adaptive, generic narrative plan."""
    checkpoint_path = bundle / "checkpoint-1.json"
    checkpoint_record: dict[str, Any] = {}
    try:
        checkpoint_record = require_approved_checkpoint(checkpoint_path, bundle / "digest.json", expected_checkpoint="CKPT-1")
    except (CheckpointError, FileNotFoundError) as exc:
        raise ValueError(f"reviewed deck requires confirmed CKPT-1: {exc}") from exc
    _require_verified_cover_metadata(digest, resolved_metadata=resolved_metadata)
    for field in ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results"):
        for item in digest.get(field, []):
            _reviewed_item(item, label=field)
    if not any(digest.get(field) for field in ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results")):
        raise ValueError("reviewed deck requires at least one reviewed evidence item")
    try:
        settings = resolve_deck_options(_load_generation_options(bundle, options))
    except DeckTypeError as exc:
        raise ValueError(f"invalid effective deck-type options: {exc}") from exc
    count = settings.get("slide_count")
    budget = settings["deck_type_contract"]["time_to_slide_budget"]["slide_count"]
    if not isinstance(count, int) or not budget["min"] <= count <= budget["max"]:
        raise ValueError(
            f"effective slide_count must be an integer from {budget['min']} through {budget['max']} for {settings['deck_type']}"
        )
    assets = _reviewed_assets(bundle, digest)
    try:
        requirements = collect_quantitative_requirements(project_dir=bundle, semantic_digest=digest)
    except QuantitativeCoverageError as exc:
        raise ValueError(f"cannot collect quantitative coverage requirements: {exc}") from exc
    coverage_artifact = build_coverage_artifact(
        project_dir=bundle,
        semantic_digest=digest,
        requirements=requirements,
        digest_sha256=sha256_file(bundle / "digest.json"),
        checkpoint_sha256=sha256_file(checkpoint_path),
        review_sha256=(
            sha256_file(bundle / "ckpt1-review.json")
            if (bundle / "ckpt1-review.json").is_file()
            else None
        ),
    )
    quantitative_group_slide_counts = _quantitative_group_slide_counts(bundle, requirements)
    plan = plan_narrative(
        digest,
        assets,
        _deck_type(settings),
        (count, count),
        quantitative_requirements=requirements,
        audience=_text(settings.get("audience"), ""),
        density=_text(settings.get("density"), ""),
        quantitative_group_slide_counts=quantitative_group_slide_counts,
    )
    selected_ids = [asset["id"] for planned in plan["slides"] for asset in planned["recommended_assets"]]
    assert_assets_allowed(bundle, selected_ids)
    table: dict[str, Any] | None = None
    table_asset_id: str | None = None
    if not _has_hash_bound_quantitative_audits(requirements):
        try:
            table, _, table_asset_id = _load_audited_table(bundle)
        except ValueError:
            if (bundle / "review-assets").is_dir() and list((bundle / "review-assets").glob("*-manual-review.json")):
                raise
            # Tables without a full audit stay out of visible content; their markers remain in the digest.
            pass
    if table_asset_id is not None:
        assert_assets_allowed(bundle, [table_asset_id])
    slides = _slides_from_narrative(
        plan,
        digest,
        table,
        settings,
        checkpoint_record=checkpoint_record,
        quantitative_requirements=requirements,
        bundle=bundle,
    )
    speaker_evidence = {
        key: digest.get(key, [])
        for key in ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results")
    }
    speaker_evidence["reviewed_semantic_slots"] = reviewed_semantic_slot_records(digest)
    paper_metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    title = _reviewed_title(digest)
    metadata_evidence = paper_metadata.get("evidence") if isinstance(paper_metadata.get("evidence"), dict) else {}
    title_evidence = metadata_evidence.get("title") if isinstance(metadata_evidence.get("title"), dict) else {}
    title_locations = title_evidence.get("locations") if isinstance(title_evidence.get("locations"), list) else []
    title_locator = next((value for value in title_locations if isinstance(value, str) and value.strip()), "p. 1")
    title_page_match = re.search(r"(?:p(?:age)?\.?\s*)(\d+)", title_locator, re.IGNORECASE)
    speaker_evidence["paper_metadata_evidence"] = [{
        "summary": title,
        "evidence": title,
        "source_page": int(title_page_match.group(1)) if title_page_match else 1,
        "section": "Paper metadata",
        "figure_table_equation": title_locator,
    }]
    if table is not None:
        # The writer may repeat a table number only when the reviewed deck contains
        # the native table bound by the manual audit above.
        speaker_evidence["audited_table_evidence"] = [table]
    slides = apply_speaker_notes(slides, speaker_evidence, _text(settings.get("language"), "zh-CN"))
    source = digest.get("source") if isinstance(digest.get("source"), dict) else {}
    deck = {
        "meta": {"title": title, "deck_type": _deck_type(settings), "language": _text(settings.get("language"), "zh-CN"), "speaker_notes_schema": "speaker-content-v1",
                 "theme": _text(settings.get("theme"), "journal-club"), "figures_dir": "figures", "checkpoint": "CKPT-2", "status": "pending",
                 "audience": plan["audience"], "density": plan["density"],
                 "options": {key: value for key, value in settings.items() if key != "deck_type_contract"},
                 "deck_type_contract": settings["deck_type_contract"],
                 "source": {"title": title, "paper_metadata": paper_metadata,
                            "requested_identifier": _text(source.get("requested_identifier"), _text(source.get("source_input"), "论文 PDF")),
                            "resolved_identifier": _text(source.get("resolved_identifier"), _text(source.get("source_input"), "论文 PDF")),
                            "input": _text(source.get("source_input"), "论文 PDF"),
                            "sha256": _text(source.get("pdf_sha256"), _text(source.get("source_sha256"), "[MISSING: PDF SHA-256]"))},
                 "narrative": plan},
        "slides": slides,
    }
    deck["meta"]["scientific_priority"] = coverage_artifact.get("scientific_priority", {})
    findings = validate_visible_content(deck)
    if findings:
        detail = "; ".join(finding.detail for finding in findings)
        raise ValueError(f"reviewed deck has forbidden visible content: {detail}")
    _validate_quantitative_rendering(deck, requirements)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(deck, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_reviewed_outline(destination, slides)
    _write_json_atomic(bundle / "coverage-requirements.json", coverage_artifact)
    write_presentation_documents(bundle)
    return destination


def generate_deck(
    bundle_dir: str | Path,
    out_path: str | Path | None = None,
    include_assets: list[str] | tuple[str, ...] = (),
    options: dict[str, Any] | None = None,
) -> Path:
    """Write a source-traceable, 10--15-slide draft deck and return its path."""

    bundle = Path(bundle_dir)
    assert_assets_allowed(bundle, include_assets)
    digest = _load_digest(bundle / "digest.json")
    record_path, review_path = bundle / "checkpoint-1.json", bundle / "ckpt1-review.json"
    if record_path.is_file() and review_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            digest = build_confirmed_semantic_digest(bundle, digest, record)
        except (OSError, json.JSONDecodeError, CKPT1ResolvedViewError) as exc:
            raise ValueError(f"deck generation requires a current confirmed CKPT-1 resolved view: {exc}") from exc
        resolved_metadata = {"title": digest["paper_metadata"]["title"], "authors": digest["paper_metadata"]["authors"]}
    else:
        resolved_metadata = None
    if any(
        isinstance(digest.get(field), list) and digest.get(field)
        for field in ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results")
    ):
        destination = Path(out_path) if out_path is not None else bundle / "deck.json"
        return _generate_reviewed_deck(bundle, digest, destination, options, resolved_metadata=resolved_metadata)
    source = digest.get("source") if isinstance(digest.get("source"), dict) else {}
    try:
        legacy_settings = resolve_deck_options(_load_generation_options(bundle, options))
    except DeckTypeError as exc:
        raise ValueError(f"invalid effective deck-type options: {exc}") from exc
    paper_metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    title = _text(paper_metadata.get("title"), _text(digest.get("title"), "[MISSING: paper title]"))
    authors = _authors_display(paper_metadata)
    source_input = _text(source.get("source_input"), "[MISSING: source input]")
    source_sha = _text(source.get("source_sha256"), "[MISSING: source SHA-256]")
    arxiv_id = _text(source.get("arxiv_id"), "")
    abstract = digest.get("abstract") if isinstance(digest.get("abstract"), dict) else None
    abstract_text = _text(abstract.get("text") if abstract else None,
                          "[MISSING: abstract not found in extracted PDF text]")
    abstract_ref = _text(abstract.get("source_ref") if abstract else None,
                         "[UNVERIFIED: abstract page is unavailable]")
    integrity_flags = [str(flag) for flag in digest.get("flags", []) if isinstance(flag, str)]
    figure_slides, figure_flags = _figure_slides(bundle, digest)
    integrity_flags = list(dict.fromkeys([*integrity_flags, *figure_flags]))
    if not integrity_flags:
        integrity_flags = ["[MISSING: reviewer must confirm source-to-deck fidelity]"]

    arxiv_ref = f"arXiv:{arxiv_id}" if arxiv_id else source_input
    slides: list[dict[str, Any]] = [
        {
            "layout": "paper-title",
            "title": title,
            "authors": authors,
            "venue": arxiv_ref,
            "presenter": "论文汇报草案（需要 CKPT-2 人工确认）",
            "speaker_notes": _notes("本页标识本次报告所绑定的论文；作者、会议信息须逐页核验"),
        },
        {
            "layout": "outline-agenda",
            "title": "汇报结构（证据驱动草案）",
            "items": ["论文与来源", "原文摘要", "原始图表", "待核验的方法与结果", "讨论"],
            "current": 0,
            "speaker_notes": _notes("本页只说明汇报顺序，不主张论文结论"),
        },
        {
            "layout": "bullets",
            "eyebrow": "论文原文摘录",
            "action_title": "摘要内容直接来自 PDF 文本，尚未改写为结论",
            "points": [abstract_text],
            "source_ref": abstract_ref,
            "speaker_notes": _notes("请以 PDF 原文为准核验摘要；不要将自动摘录改写为未证实的中文结论"),
        },
        {
            "layout": "bullets",
            "eyebrow": "可追溯性",
            "action_title": "本 deck 绑定到一份确定的论文输入和内容哈希",
            "points": [
                f"输入：{source_input}",
                f"PDF SHA-256：{source_sha}",
                f"页数：{source.get('n_pages', '[MISSING: page count]')}",
                "图表仅在存在本地论文裁剪文件时才会引用。",
            ],
            "source_ref": source_input,
            "speaker_notes": _notes("这页用于确认本次报告没有悄悄更换论文版本或替换图表"),
        },
        {
            "layout": "bullets",
            "eyebrow": "需人工审阅",
            "action_title": "研究问题与贡献必须从正文逐项核验后再陈述",
            "points": [
                "[MISSING: author-reviewed research question]",
                "[MISSING: author-reviewed contributions]",
                "[MISSING: source page references for each contribution]",
            ],
            "source_ref": source_input,
            "speaker_notes": _notes("不要把摘要中的措辞自动当作贡献列表；需回到引言和方法部分标注页码"),
        },
        {
            "layout": "two-column",
            "eyebrow": "需人工审阅",
            "action_title": "方法、模型与公式必须以正文或原始 LaTeX 为准",
            "points": [
                "[MISSING: author-reviewed method summary]",
                "[MISSING: verified equation transcription]",
            ],
            "points2": [
                "[MISSING: method source pages]",
                "[UNVERIFIED: no automatic formula reconstruction was performed]",
            ],
            "source_ref": source_input,
            "speaker_notes": _notes("公式不从 OCR 或猜测中生成；应从原 PDF 或 LaTeX 逐式抄录并复核"),
        },
    ]
    slides.extend(figure_slides)
    slides.extend(
        [
            {
                "layout": "bullets",
                "eyebrow": "需人工审阅",
                "action_title": "实验设置与结果不能在缺少证据时自动补全",
                "points": [
                    "[MISSING: author-reviewed experimental setup]",
                    "[MISSING: author-reviewed experimental results]",
                    "[MISSING: exact numbers and source pages]",
                ],
                "source_ref": source_input,
                "speaker_notes": _notes("所有实验数字、数据集和比较对象须从论文表格或图注逐项核对"),
            },
            {
                "layout": "bullets",
                "eyebrow": "完整性检查",
                "action_title": "以下未解决项必须保留到人工 checkpoint 处理",
                "points": integrity_flags,
                "source_ref": source_input,
                "speaker_notes": _notes("逐项说明仍未解决的标记；这些标记不能在导出前被静默删除"),
            },
            {
                "layout": "critique-concerns",
                "action_title": "局限性与批评需要区分论文事实和汇报者分析",
                "points": [
                    {"head": "论文已述局限", "body": "[MISSING: author-reviewed limitations with source pages]"},
                    {"head": "汇报者分析", "body": "[MISSING: reviewer analysis after reading the full paper]"},
                ],
                "source_ref": source_input,
                "speaker_notes": _notes("明确哪些话是作者陈述，哪些是汇报者自己的分析，避免混为一谈"),
            },
            {
                "layout": "discussion-questions",
                "title": "讨论问题（待 CKPT-2 确认）",
                "questions": [
                    "论文的核心主张由哪一页、哪张图或哪张表直接支持？",
                    "哪些结论仍需要复核原始公式、实验设置或引用？",
                    "是否应保留、替换或删除未定位的图表？",
                ],
                "speaker_notes": _notes("以可验证性为中心组织讨论，不把待核验内容升级为结论"),
            },
            {
                "layout": "references",
                "title": "论文来源",
                "entries": [f"{title}. Authors: {authors}. {arxiv_ref}. 输入：{source_input}。PDF SHA-256: {source_sha}."],
                "speaker_notes": _notes("这是一条输入论文的可追溯引用；完整 BibTeX/Crossref 解析属于后续支撑模块"),
            },
        ]
    )
    if _deck_type(legacy_settings) == "conference":
        slides = [slide for slide in slides if slide.get("layout") != "critique-concerns"]
    elif _deck_type(legacy_settings) == "thesis-defense" and len(slides) < 15:
        slides.insert(-1, {
            "layout": "bullets",
            "eyebrow": "Backup",
            "action_title": "备份材料保留待核验的补充证据。",
            "points": ["[MISSING: author-reviewed appendix evidence]"],
            "source_ref": source_input,
            "speaker_notes": _notes("本页作为答辩备份材料，仍须以论文原文逐项核验。"),
            "archetypes": ["backup", "appendix"],
        })
    target_count = legacy_settings["slide_count"]
    if _deck_type(legacy_settings) == "thesis-defense":
        while len(slides) < target_count:
            slides.insert(-1, {
                "layout": "bullets",
                "eyebrow": "Backup",
                "action_title": f"答辩备份材料 {len(slides)}：待逐项核验的论文证据。",
                "points": ["[MISSING: author-reviewed thesis appendix evidence]"],
                "source_ref": source_input,
                "speaker_notes": _notes("本页保留为答辩备份材料，须以论文原文逐项核验。"),
                "archetypes": ["backup", "appendix"],
            })
    budget = legacy_settings["deck_type_contract"]["time_to_slide_budget"]["slide_count"]
    if not budget["min"] <= len(slides) <= budget["max"]:
        raise RuntimeError(
            f"internal deck-length invariant failed: generated {len(slides)} slides for {legacy_settings['deck_type']}"
        )
    deck = {
        "meta": {
            "title": title,
            "deck_type": _deck_type(legacy_settings),
            "language": _text(legacy_settings.get("language"), "zh-CN"),
            "speaker_notes_schema": "legacy-v1",
            "theme": _text(legacy_settings.get("theme"), "journal-club"),
            "figures_dir": "figures",
            "audience": legacy_settings["audience"],
            "density": legacy_settings["density"],
            "options": {key: value for key, value in legacy_settings.items() if key != "deck_type_contract"},
            "deck_type_contract": legacy_settings["deck_type_contract"],
            "source": {
                "title": title,
                "paper_metadata": paper_metadata,
                "arxiv_id": arxiv_id or None,
                "input": source_input,
                "sha256": source_sha,
            },
        },
        "slides": slides,
    }
    destination = Path(out_path) if out_path is not None else bundle / "deck.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(deck, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a grounded, checkpoint-ready draft deck from digest.json."
    )
    parser.add_argument("bundle", help="directory containing digest.json and optional figures/")
    parser.add_argument("--out", help="deck.json path (default: <bundle>/deck.json)")
    parser.add_argument("--options", help="effective project-options.json written by the facade")
    parser.add_argument(
        "--include-asset",
        action="append",
        default=[],
        help="explicit paper asset to include; CKPT-1 deferred assets are rejected",
    )
    args = parser.parse_args(argv)
    try:
        options: dict[str, Any] | None = None
        if args.options:
            payload = json.loads(Path(args.options).read_text(encoding="utf-8"))
            options = payload.get("options") if isinstance(payload, dict) else None
            if not isinstance(options, dict):
                raise ValueError("--options must point to project-options.json with an options object")
        deck_path = generate_deck(args.bundle, args.out, args.include_asset, options)
    except (FileNotFoundError, MarkerPolicyError, ValueError, RuntimeError) as exc:
        print(f"generate_deck: {exc}", file=sys.stderr)
        return 2
    print(f"Checkpoint-ready draft deck -> {deck_path}")
    print("Next: review the generated content at CKPT-2 before invoking a renderer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
