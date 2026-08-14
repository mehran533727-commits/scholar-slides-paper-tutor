"""Deterministic Markdown projection for canonical CKPT-1 review candidates."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


def _display(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _evidence(item: Mapping[str, Any]) -> str:
    evidence = item.get("evidence")
    if not isinstance(evidence, Mapping):
        return ""
    return f" (p. {evidence.get('page')} — {evidence.get('locator')})"


def _item_line(item: Mapping[str, Any], text_field: str) -> str:
    item_id = f"[{item['id']}] " if isinstance(item.get("id"), str) else ""
    return f"- {item_id}{_display(item.get(text_field, ''))}{_evidence(item)}"


def _section(lines: list[str], title: str, items: object, text_field: str) -> None:
    lines.extend(["", f"## {title}", ""])
    if not isinstance(items, list) or not items:
        lines.append("- None")
        return
    for item in items:
        if isinstance(item, Mapping):
            lines.append(_item_line(item, text_field))
        else:
            lines.append(f"- {_display(item)}")


def _metrics_section(lines: list[str], metrics: object) -> None:
    lines.extend(["", "## Proposed key metrics", ""])
    if not isinstance(metrics, list) or not metrics:
        lines.append("- None")
        return
    for metric in metrics:
        if not isinstance(metric, Mapping):
            continue
        item_id = f"[{metric['id']}] " if isinstance(metric.get("id"), str) else ""
        lines.append(
            f"- {item_id}{_display(metric.get('label', ''))}: {_display(metric.get('value', ''))}{_evidence(metric)}"
        )


def _paper_semantics_section(lines: list[str], semantics: object, readiness: object) -> None:
    lines.extend(["", "## Paper semantics", ""])
    slots = semantics.get("slots") if isinstance(semantics, Mapping) else None
    ordered = (
        "context", "objective_or_research_question", "motivation_or_gap", "problem_setup",
        "approach", "contributions", "experimental_setup", "main_results", "limitations_or_failure_modes",
    )
    if not isinstance(slots, Mapping):
        lines.append("- None")
    else:
        for slot in ordered:
            record = slots.get(slot)
            if not isinstance(record, Mapping):
                lines.append(f"- {slot}: [MISSING: semantic evidence not detected]")
                continue
            lines.append(
                f"- {slot}: {_display(record.get('summary', record.get('text', '')))} "
                f"(type: {_display(record.get('semantic_evidence_type', 'unknown'))}; "
                f"p. {record.get('source_page')} — {_display(record.get('section', ''))} — "
                f"{_display(record.get('locator', ''))}; confidence: {_display(record.get('confidence', 'unknown'))})"
            )
    lines.extend(["", "## Mode-B narrative readiness", ""])
    if not isinstance(readiness, Mapping):
        lines.append("- Status: incomplete")
        lines.append("- Missing slots: [MISSING: readiness record]")
        return
    lines.append(f"- Status: {_display(readiness.get('status', 'incomplete'))}")
    lines.append(f"- Ready: {_display(readiness.get('ready', False))}")
    missing = readiness.get("missing_slots") if isinstance(readiness.get("missing_slots"), list) else []
    lines.append(f"- Missing slots: {', '.join(str(item) for item in missing) if missing else 'None'}")


def _review_overlay_section(lines: list[str], candidate: Mapping[str, Any]) -> None:
    reviewed = candidate.get("reviewed_paper_semantics")
    corrections = candidate.get("semantic_corrections")
    provenance = candidate.get("semantic_review_provenance")
    lines.extend(["", "## Semantic review overlay", ""])
    if isinstance(corrections, list) and corrections:
        for index, correction in enumerate(corrections, start=1):
            if not isinstance(correction, Mapping):
                continue
            refs = correction.get("source_refs") if isinstance(correction.get("source_refs"), list) else []
            ref_text = "; ".join(
                f"p. {ref.get('source_page')} — {ref.get('section')} — {ref.get('locator')}"
                for ref in refs if isinstance(ref, Mapping)
            )
            lines.append(
                f"- correction {index}: {correction.get('slot')} / {correction.get('operation')} — "
                f"{_display(correction.get('reviewed_summary', ''))}; reason: {_display(correction.get('reason', ''))}; "
                f"source refs: {ref_text or 'None'}"
            )
    else:
        lines.append("- Corrections: None")
    if isinstance(provenance, Mapping):
        lines.extend([
            f"- extracted_semantics_sha256: {provenance.get('extracted_semantics_sha256', '')}",
            f"- semantic_corrections_sha256: {provenance.get('semantic_corrections_sha256', '')}",
            f"- reviewed_semantics_sha256: {provenance.get('reviewed_semantics_sha256', '')}",
        ])
    lines.extend(["", "### Reviewed paper semantics", ""])
    slots = reviewed.get("slots") if isinstance(reviewed, Mapping) else None
    if not isinstance(slots, Mapping):
        lines.append("- None")
        return
    for slot in (
        "context", "objective_or_research_question", "motivation_or_gap", "problem_setup",
        "approach", "contributions", "experimental_setup", "main_results", "limitations_or_failure_modes",
    ):
        record = slots.get(slot)
        if isinstance(record, Mapping):
            lines.append(
                f"- {slot}: {_display(record.get('summary', record.get('text', '')))} "
                f"(type: {_display(record.get('semantic_evidence_type', 'unknown'))}; "
                f"p. {record.get('source_page')} — {_display(record.get('section', ''))} — {_display(record.get('locator', ''))})"
            )


def _quantitative_section(lines: list[str], candidate: Mapping[str, Any]) -> None:
    lines.extend(["", "## Quantitative compatibility", ""])
    for field in ("proposed_claims", "proposed_experimental_results", "proposed_key_metrics", "evidence_audits"):
        value = candidate.get(field)
        lines.append(f"- {field}: {len(value) if isinstance(value, list) else 0}")


def project_review_markdown(candidate: Mapping[str, Any]) -> str:
    """Render only values already present in a canonical review candidate."""
    if not isinstance(candidate, Mapping):
        raise ValueError("canonical review candidate must be an object")
    source = candidate.get("source_digest") if isinstance(candidate.get("source_digest"), Mapping) else {}
    lines = [
        "# CKPT-1 review candidate",
        "",
        f"- Source digest: {_display(source.get('path', ''))}",
        f"- Source SHA-256: {_display(source.get('sha256', ''))}",
        f"- Prepared by: {_display((candidate.get('prepared_by') or {}).get('name', '')) if isinstance(candidate.get('prepared_by'), Mapping) else ''}",
    ]
    corrections = candidate.get("metadata_corrections")
    lines.extend(["", "## Metadata corrections", ""])
    if isinstance(corrections, Mapping) and corrections:
        for field in sorted(corrections):
            correction = corrections[field]
            if isinstance(correction, Mapping):
                lines.extend([
                    f"### {field}",
                    f"- Original: {_display(correction.get('original', ''))}",
                    f"- Proposed: {_display(correction.get('proposed', ''))}",
                    f"- Reason: {_display(correction.get('reason', ''))}{_evidence(correction)}",
                    "",
                ])
    else:
        lines.append("- None")
    _paper_semantics_section(lines, candidate.get("paper_semantics"), candidate.get("mode_b_narrative_readiness"))
    _review_overlay_section(lines, candidate)
    _quantitative_section(lines, candidate)
    _section(lines, "Proposed claims", candidate.get("proposed_claims"), "text")
    _section(lines, "Proposed contributions", candidate.get("proposed_contributions"), "text")
    _section(lines, "Proposed experimental results", candidate.get("proposed_experimental_results"), "text")
    _metrics_section(lines, candidate.get("proposed_key_metrics"))
    _section(lines, "Unresolved markers", candidate.get("unresolved_markers"), "text")
    _section(lines, "Deck-forbidden assets", candidate.get("deck_forbidden_assets"), "text")
    return "\n".join(lines) + "\n"
