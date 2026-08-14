"""Deterministic, fail-closed evidence for the CKPT-2 aesthetics review."""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


SCHEMA_VERSION = 1
KIND = "scholar-slides-aesthetics-qa"
DIMENSIONS = (
    "hierarchy_focus",
    "typography",
    "space_grid",
    "figures_data_ink",
    "color_contrast",
    "consistency_finish",
)
ALIASES = {
    "dims": "dimensions", "hierarchy": "hierarchy_focus", "space": "space_grid",
    "figures": "figures_data_ink", "color": "color_contrast", "consistency": "consistency_finish",
    "worst": "dimension", "fix": "recommended_fix",
}
CLICHE_CODES = (
    "decorative-emoji", "meaningless-gradient", "default-indigo-violet",
    "decorative-blobs", "repeated-dashboard-cards", "uniform-padding",
)


def _is_score(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4


def _canonical_dimension(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return ALIASES.get(value, value) if ALIASES.get(value, value) in DIMENSIONS else None


def _has_figure(slide: Mapping[str, Any]) -> bool:
    marker_keys = {"figure", "figures", "image", "images", "chart", "charts", "table", "tables", "asset", "assets"}
    return any(key in slide and slide[key] not in (None, [], {}, "") for key in marker_keys)


def _normalize_slide(raw: Any, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        errors.append("slide entry must be an object")
        return None
    slide = raw.get("slide")
    if not isinstance(slide, int) or isinstance(slide, bool) or slide < 1:
        errors.append("slide entry has an invalid slide number")
        return None
    dimensions = raw.get("dimensions", raw.get("dims"))
    if not isinstance(dimensions, Mapping):
        errors.append(f"slide {slide} dimensions are missing or malformed")
        return None
    canonical = {ALIASES.get(key, key): value for key, value in dimensions.items()}
    if set(canonical) != set(DIMENSIONS):
        errors.append(f"slide {slide} dimensions must contain exactly the six rubric dimensions")
        return None
    figure_score = canonical["figures_data_ink"]
    marker_present = _has_figure(raw)
    presence = raw.get("has_figure_or_data", raw.get("figure_present"))
    if presence is None:
        presence = marker_present
    if not isinstance(presence, bool):
        errors.append(f"slide {slide} has_figure_or_data must be boolean")
        return None
    if marker_present and not presence:
        errors.append(f"slide {slide} cannot declare no figure/data when figure/data markers are present")
        return None
    if presence and figure_score is None:
        errors.append(f"slide {slide} requires figures_data_ink when a figure/data element is present")
        return None
    if not presence and figure_score is not None:
        errors.append(f"slide {slide} must set figures_data_ink to null without a figure/data element")
        return None
    if figure_score is not None and not _is_score(figure_score):
        errors.append(f"slide {slide} figures_data_ink must be an integer 0-4 or null")
        return None
    if any(not _is_score(canonical[key]) for key in DIMENSIONS if key != "figures_data_ink"):
        errors.append(f"slide {slide} contains an invalid aesthetics dimension score")
        return None
    out_of = 20 if figure_score is None else 24
    total = sum(value for value in canonical.values() if value is not None)
    if raw.get("outOf") != out_of or raw.get("total") != total:
        errors.append(f"slide {slide} total/outOf do not match its dimensions")
        return None
    return {"slide": slide, "has_figure_or_data": presence, "dimensions": {key: canonical[key] for key in DIMENSIONS}, "total": total, "outOf": out_of}


def _normalize_weakest(raw: Any, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        errors.append("weakest entry must be an object")
        return None
    entry = dict(raw)
    if "dimension" not in entry and "worst" in entry:
        entry["dimension"] = entry["worst"]
    if "recommended_fix" not in entry and "fix" in entry:
        entry["recommended_fix"] = entry["fix"]
    if not isinstance(entry.get("slide"), int) or entry["slide"] < 1:
        errors.append("weakest entry has an invalid slide")
        return None
    dimension = _canonical_dimension(entry.get("dimension"))
    if dimension is None or not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("defect", "severity", "recommended_fix")):
        errors.append("weakest entry requires slide, defect, dimension, severity, and recommended_fix")
        return None
    return {"slide": entry["slide"], "defect": entry["defect"].strip(), "dimension": dimension, "severity": entry["severity"].strip().upper(), "recommended_fix": entry["recommended_fix"].strip()}


def validate_aesthetics_report(report: Any) -> dict[str, Any]:
    """Normalize compatible upstream aliases and return a fail-closed gate decision."""
    errors: list[str] = []
    if not isinstance(report, Mapping):
        return {"schema_version": SCHEMA_VERSION, "kind": KIND, "status": "blocked", "summary": {"slides": 0, "errors": 1, "rework_count": 0}, "slides": [], "rework": [], "weakest3": [], "findings": [], "anti_ai_visual_cliche_findings": [], "errors": ["aesthetics report is missing or malformed"]}
    raw_slides = report.get("slides")
    if not isinstance(raw_slides, list) or not raw_slides:
        errors.append("aesthetics report must score every slide")
        raw_slides = []
    slides = [item for raw in raw_slides if (item := _normalize_slide(raw, errors)) is not None]
    if len(slides) != len(raw_slides) or len({item["slide"] for item in slides}) != len(slides):
        errors.append("aesthetics report slide entries must be unique and valid")
    weakest_raw = report.get("weakest3", report.get("weakest"))
    if not isinstance(weakest_raw, list):
        errors.append("aesthetics report weakest-3 list is missing")
        weakest_raw = []
    weakest = [item for raw in weakest_raw if (item := _normalize_weakest(raw, errors)) is not None]
    if len(slides) >= 3 and len(weakest) < 3:
        errors.append("aesthetics report requires at least three actionable weakest slides")
    required_weakest = min(3, len(slides))
    expected_weakest = [item["slide"] for item in sorted(slides, key=lambda item: (item["total"] / item["outOf"], item["slide"]))[:required_weakest]]
    observed_weakest = [item["slide"] for item in weakest[:required_weakest]]
    if len(set(observed_weakest)) != len(observed_weakest) or observed_weakest != expected_weakest:
        errors.append("weakest entries must be distinct and ordered by normalized score then slide number")
    if any(item["slide"] not in {slide["slide"] for slide in slides} for item in weakest):
        errors.append("weakest entries must reference scored slides")
    cliches = report.get("anti_ai_visual_cliche_findings", report.get("findings", []))
    if not isinstance(cliches, list) or any(not isinstance(item, Mapping) for item in cliches):
        errors.append("anti-AI visual-cliche findings must be a list")
        cliches = []
    rework: list[dict[str, Any]] = []
    for slide in slides:
        threshold = 18 * slide["outOf"] / 24
        low = [key for key, value in slide["dimensions"].items() if value is not None and value <= 2]
        if low or slide["total"] < threshold:
            reason = ", ".join(low) if low else f"total {slide['total']}/{slide['outOf']} below threshold"
            rework.append({"slide": slide["slide"], "reason": reason})
    declared_rework = report.get("rework", [])
    if not isinstance(declared_rework, list):
        errors.append("rework must be a finite list")
    elif any(not isinstance(item, Mapping) or not isinstance(item.get("slide"), int) for item in declared_rework):
        errors.append("rework entries must name a slide")
    elif {item["slide"] for item in declared_rework} != {item["slide"] for item in rework}:
        errors.append("rework must explicitly and exactly report every blocking slide")
    findings = [dict(item) for item in cliches]
    status = "pass" if not errors and not rework else "blocked"
    return {"schema_version": SCHEMA_VERSION, "kind": KIND, "status": status, "summary": {"slides": len(slides), "errors": len(errors), "rework_count": len(rework)}, "slides": slides, "rework": rework, "weakest3": weakest[:3], "findings": findings, "anti_ai_visual_cliche_findings": findings, "errors": errors}


def _cliche_findings(deck: Mapping[str, Any]) -> list[dict[str, Any]]:
    slides = deck.get("slides") if isinstance(deck.get("slides"), list) else []
    findings: list[dict[str, Any]] = []
    padding = []
    cards = 0
    for index, raw in enumerate(slides, start=1):
        slide = raw if isinstance(raw, Mapping) else {}
        text = repr(slide).lower()
        if any(char in repr(slide) for char in "🚀✨🎯⚡😀😃😄"):
            findings.append({"slide": index, "code": "decorative-emoji", "dimension": "hierarchy_focus", "severity": "HIGH", "defect": "Decorative emoji reads as AI-generated ornament.", "recommended_fix": "Use typography or a meaningful data mark instead."})
        if "gradient" in text:
            findings.append({"slide": index, "code": "meaningless-gradient", "dimension": "color_contrast", "severity": "HIGH", "defect": "Unmotivated gradient is a visual cliché.", "recommended_fix": "Use a flat paper field and one intentional accent."})
        if any(color in text for color in ("#6366f1", "#4f46e5", "#8b5cf6", "#7c3aed", "#a855f7")):
            findings.append({"slide": index, "code": "default-indigo-violet", "dimension": "color_contrast", "severity": "HIGH", "defect": "Default indigo/violet styling is an AI visual cliché.", "recommended_fix": "Use the deck's documented academic accent token."})
        if "blob" in text or "wave divider" in text:
            findings.append({"slide": index, "code": "decorative-blobs", "dimension": "consistency_finish", "severity": "HIGH", "defect": "Decorative geometry carries no data.", "recommended_fix": "Remove the ornament or replace it with evidence."})
        if slide.get("dashboard_card") is True or "dashboard-card" in text:
            cards += 1
        if isinstance(slide.get("padding"), str):
            padding.append(slide["padding"])
    if cards >= 3:
        findings.append({"slide": 1, "code": "repeated-dashboard-cards", "dimension": "consistency_finish", "severity": "HIGH", "defect": "Repeated dashboard cards make the deck look templated.", "recommended_fix": "Replace repeated cards with assertion-evidence layouts."})
    if len(padding) >= 3 and len(set(padding)) == 1:
        findings.append({"slide": 1, "code": "uniform-padding", "dimension": "space_grid", "severity": "MEDIUM", "defect": "Uniform symmetric padding flattens pacing.", "recommended_fix": "Vary density deliberately while retaining the grid."})
    return findings


def _visual_issue_dimensions(issue: Mapping[str, Any]) -> tuple[str, ...]:
    code = str(issue.get("code", "")).casefold()
    if "void" in code:
        return ("space_grid",)
    if "title" in code or "line-count" in code:
        return ("typography", "hierarchy_focus")
    if "reference" in code:
        return ("space_grid", "hierarchy_focus")
    if "image" in code or "figure" in code or "legibility" in code:
        return ("figures_data_ink", "space_grid")
    if "font" in code or "text" in code:
        return ("typography",)
    return ("consistency_finish",)


def _visual_findings(visual_qa: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(visual_qa, Mapping):
        return []
    issues = visual_qa.get("issues")
    if not isinstance(issues, list):
        return []
    findings: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        severity = str(issue.get("severity", "WARNING")).upper()
        if severity in {"INFO", "I"}:
            continue
        slide = issue.get("slide", issue.get("slide_index"))
        if not isinstance(slide, int) or slide < 1:
            continue
        code = str(issue.get("code", "visual-finding"))
        defect = str(issue.get("message", issue.get("detail", code))).strip() or code
        fix = str(issue.get("action", issue.get("suggested_action", "Review the rendered slide against the visual evidence."))).strip()
        findings.append({
            "slide": slide,
            "code": f"visual-qa:{code}",
            "dimension": _visual_issue_dimensions(issue)[0],
            "dimensions": list(_visual_issue_dimensions(issue)),
            "severity": severity,
            "defect": defect,
            "recommended_fix": fix,
            "evidence": {key: value for key, value in issue.items() if key in {"code", "measurement", "threshold", "evidence", "message", "action"}},
        })
    return findings


def build_aesthetics_report(deck: Mapping[str, Any], *, visual_qa: Mapping[str, Any] | None = None, inputs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build deterministic normal-path evidence; human approval remains external."""
    raw_slides = deck.get("slides") if isinstance(deck, Mapping) and isinstance(deck.get("slides"), list) else []
    cliches = _cliche_findings(deck if isinstance(deck, Mapping) else {})
    cliche_by_slide: dict[int, list[dict[str, Any]]] = {}
    for finding in cliches:
        cliche_by_slide.setdefault(int(finding["slide"]), []).append(finding)
    visual_findings = _visual_findings(visual_qa)
    visual_by_slide: dict[int, list[dict[str, Any]]] = {}
    for finding in visual_findings:
        visual_by_slide.setdefault(int(finding["slide"]), []).append(finding)
    slides = []
    for number, raw in enumerate(raw_slides, start=1):
        slide = raw if isinstance(raw, Mapping) else {}
        figure = _has_figure(slide)
        dimensions = {key: 4 for key in DIMENSIONS}
        if not figure:
            dimensions["figures_data_ink"] = None
        for finding in cliche_by_slide.get(number, []):
            dimensions[finding["dimension"]] = min(dimensions[finding["dimension"]], 2)
        for finding in visual_by_slide.get(number, []):
            # P3 layout nudges remain actionable findings, but they must not
            # turn a deliberately sparse slide into an automatic rework item.
            # Stronger visual defects retain the full one-point dimension hit.
            floor = 3 if str(finding.get("severity", "")).upper() in {"P3", "WARNING"} else 0
            for dimension in finding.get("dimensions", [finding["dimension"]]):
                if dimension in dimensions and dimensions[dimension] is not None:
                    dimensions[dimension] = max(floor, dimensions[dimension] - 1)
        total = sum(value for value in dimensions.values() if value is not None)
        slides.append({"slide": number, "has_figure_or_data": figure, "dimensions": dimensions, "total": total, "outOf": 24 if figure else 20})
    ordered = sorted(slides, key=lambda item: (item["total"] / item["outOf"], item["slide"]))
    weakest = []
    for slide in ordered[: min(3, len(ordered))]:
        dim = min((key for key, value in slide["dimensions"].items() if value is not None), key=lambda key: slide["dimensions"][key])
        related = [*visual_by_slide.get(slide["slide"], []), *cliche_by_slide.get(slide["slide"], [])]
        finding = related[0] if related else {"defect": "No automated defect; retain editorial scrutiny.", "severity": "LOW", "recommended_fix": "Confirm hierarchy and finish in the human review."}
        weakest.append({"slide": slide["slide"], "defect": finding["defect"], "dimension": dim, "severity": finding["severity"], "recommended_fix": finding["recommended_fix"]})
    rework = [{"slide": slide["slide"], "reason": "aesthetics score requires rework"} for slide in slides if any(value is not None and value <= 2 for value in slide["dimensions"].values()) or slide["total"] < 18 * slide["outOf"] / 24]
    raw_report = {"slides": slides, "weakest3": weakest, "rework": rework, "anti_ai_visual_cliche_findings": cliches, "findings": [*cliches, *visual_findings]}
    result = validate_aesthetics_report(raw_report)
    result["inputs"] = dict(inputs or {})
    return result
