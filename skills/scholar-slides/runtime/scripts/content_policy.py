"""Policy boundary between renderer-visible slides and review/audit artifacts.

Deck JSON is deliberately extensible, but a review ledger must never be stored under
``slides``: it is too easy for a renderer change to expose it.  This module projects
the small renderer contract and rejects review bookkeeping from the visible payload.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from audience_text import PROVENANCE_LEAK_RE


MARKER_RE = re.compile(r"\[(?:MISSING|UNVERIFIED)(?::[^\]]*)?\]", re.IGNORECASE)
INTERNAL_AUDIT_RE = re.compile(
    r"\b(?:ckpt(?:[- ]?\d+)?|checkpoint|audit(?:ed)?|ledger|marker[ _-]?resolution|"
    r"sha[- ]?256|hash(?:ed|ing)?)\b|"
    + PROVENANCE_LEAK_RE.pattern,
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(
    r"(?:<\s*(?:presenter|presenters|date|name|author|authors|venue|affiliation|"
    r"institute|email|todo|tbd|placeholder|your name)\s*>|\b(?:todo|tbd|placeholder|template)\b)",
    re.IGNORECASE,
)
AUDIT_FIELD_RE = re.compile(r"(?:audit|ledger|marker|checkpoint|hash|review)", re.IGNORECASE)
_INTERNAL_SEMANTIC_FIELDS = frozenset({
    "role_selection", "semantic_evidence_type", "evidence_section", "role_compatibility_score",
    "origin_semantic_slot", "origin_reviewed_semantics_hash",
})


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    detail: str
    slide: int | None = None


_TEXT_FIELDS = (
    "title", "action_title", "eyebrow", "authors", "affiliation", "venue", "presenter", "num",
    "annotation", "source_ref", "note", "text", "speaker_notes",
)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _text_list(value: Any) -> list[str]:
    return list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _figure(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in ("src", "caption", "cite", "alt", "fit") if isinstance(value.get(key), str)}


def _table(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    table: dict[str, Any] = {key: value[key] for key in ("caption", "footnote") if isinstance(value.get(key), str)}
    if isinstance(value.get("columns"), list):
        table["columns"] = [
            column if isinstance(column, str) else {key: column[key] for key in ("label", "unit") if isinstance(column.get(key), str)}
            for column in value["columns"] if isinstance(column, (str, Mapping))
        ]
    if isinstance(value.get("rows"), list):
        table["rows"] = value["rows"]
    return table


def _equations(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {key: item[key] for key in ("latex", "num") if isinstance(item.get(key), str)}
        for item in value if isinstance(item, Mapping)
    ]


def _critique_points(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [{key: item[key] for key in ("head", "body") if isinstance(item.get(key), str)} for item in value if isinstance(item, Mapping)]


def _visible_slide(slide: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {"layout": slide["layout"]} if isinstance(slide.get("layout"), str) else {}
    for field in _TEXT_FIELDS:
        value = slide.get(field)
        if field == "authors" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            projected[field] = list(value)
        elif isinstance(value, str):
            projected[field] = value
    for field in ("items", "points2", "questions", "entries"):
        values = _text_list(slide.get(field))
        if values:
            projected[field] = values
    points = _critique_points(slide.get("points")) if slide.get("layout") == "critique-concerns" else _text_list(slide.get("points"))
    if points:
        projected["points"] = points
    for field, value in (("figure", _figure(slide.get("figure"))), ("table", _table(slide.get("table"))), ("equations", _equations(slide.get("equations")))):
        if value:
            projected[field] = value
    return projected


def project_visible_content(deck: Mapping[str, Any]) -> dict[str, Any]:
    """Return exactly the slide fields that current renderers may display."""
    slides = deck.get("slides")
    return {"slides": [_visible_slide(slide) for slide in slides if isinstance(slide, Mapping)]} if isinstance(slides, list) else {"slides": []}


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _without_internal_semantic_metadata(value: Any) -> Any:
    """Keep unknown extensions auditable while hiding known non-rendered role QA."""
    if isinstance(value, Mapping):
        return {
            key: _without_internal_semantic_metadata(child)
            for key, child in value.items()
            if key not in _INTERNAL_SEMANTIC_FIELDS
        }
    if isinstance(value, list):
        return [_without_internal_semantic_metadata(child) for child in value]
    return value


def _policy_slide(raw_slide: Mapping[str, Any]) -> dict[str, Any]:
    """Project policy text while retaining unknown slide extensions fail-closed.

    Native-table provenance is an integrity binding consumed by semantic QA, not
    a renderer field.  All renderer-visible table fields and every unknown slide
    extension remain in the policy input so actual or newly introduced audience
    text cannot bypass leakage checks.
    """
    projected = _without_internal_semantic_metadata(raw_slide)
    if not isinstance(projected, dict):
        return {}
    table = projected.get("table")
    if isinstance(table, Mapping):
        projected["table"] = {
            key: child
            for key, child in table.items()
            if key != "provenance"
        }
    return projected


def validate_visible_content(deck: Mapping[str, Any]) -> list[Finding]:
    """Find markers, bookkeeping prose, and templates in renderer-visible slides."""
    findings: list[Finding] = []
    slides = deck.get("slides")
    if not isinstance(slides, list):
        return findings
    meta = deck.get("meta")
    if isinstance(meta, Mapping):
        for key in meta:
            if key != "checkpoint" and AUDIT_FIELD_RE.search(str(key)):
                findings.append(Finding("deck-audit-metadata", "P1", f"deck meta contains audit field '{key}'"))
    for index, raw_slide in enumerate(slides, start=1):
        if not isinstance(raw_slide, Mapping):
            continue
        def audit_keys(value: Any):
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if AUDIT_FIELD_RE.search(str(key)):
                        yield str(key)
                    yield from audit_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from audit_keys(child)
        policy_slide = _policy_slide(raw_slide)
        for key in audit_keys(policy_slide):
            findings.append(Finding("slide-audit-metadata", "P1", f"slide {index} contains audit field '{key}'", index))
        visible = _visible_slide(raw_slide)
        visible_strings = list(_strings(visible))
        all_slide_strings = list(_strings(policy_slide))
        audit_prose = [" ".join(text.split()).casefold() for text in visible_strings if INTERNAL_AUDIT_RE.search(text)]
        if len(audit_prose) != len(set(audit_prose)):
            findings.append(Finding("duplicated-audit-prose", "P1", f"slide {index} duplicates audit prose", index))
        for text in all_slide_strings:
            if MARKER_RE.search(text):
                findings.append(Finding("visible-marker", "P1", f"slide {index} exposes an unresolved marker", index))
            if INTERNAL_AUDIT_RE.search(text):
                findings.append(Finding("internal-audit-prose", "P1", f"slide {index} exposes checkpoint or audit prose", index))
            if PLACEHOLDER_RE.search(text):
                findings.append(Finding("placeholder-leak", "P1", f"slide {index} contains a template placeholder", index))
    return findings
