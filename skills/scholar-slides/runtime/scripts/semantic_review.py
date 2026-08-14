"""Source-bound CKPT-1 semantic review overlays.

The extracted semantic view in ``digest.json`` is immutable.  This module only
derives a reviewed view from explicit, source-bound human correction records.
It deliberately has no paper-specific vocabulary or facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from typing import Any

from paper_semantics import REQUIRED_SLOTS


class SemanticReviewError(ValueError):
    """A semantic correction is malformed or cannot be grounded in the digest."""


_OPERATIONS = {
    "replace_slot",
    "merge_evidence",
    "add_supporting_evidence",
    "remove_incorrect_support",
}


def _normal(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _sha256(value: object) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_records(digest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for container in (digest, digest.get("paper_semantics")):
        if not isinstance(container, Mapping):
            continue
        for field in ("semantic_source_evidence", "source_evidence", "source_locators"):
            values = container.get(field)
            if isinstance(values, list):
                records.extend(item for item in values if isinstance(item, Mapping))
    semantics = digest.get("paper_semantics")
    if isinstance(semantics, Mapping):
        slots = semantics.get("slots")
        if isinstance(slots, Mapping):
            records.extend(item for item in slots.values() if isinstance(item, Mapping))
    return records


def _source_index(digest: Mapping[str, Any]) -> dict[tuple[int, str, str], list[Mapping[str, Any]]]:
    index: dict[tuple[int, str, str], list[Mapping[str, Any]]] = {}
    for record in _source_records(digest):
        page = record.get("source_page", record.get("page"))
        section = record.get("section")
        locator = record.get("locator", record.get("source_locator"))
        if isinstance(page, int) and page >= 1 and isinstance(section, str) and isinstance(locator, str):
            index.setdefault((page, _normal(section), _normal(locator)), []).append(record)
    return index


def _validate_ref(ref: object, index: Mapping[tuple[int, str, str], list[Mapping[str, Any]]], position: int) -> dict[str, Any]:
    if not isinstance(ref, Mapping):
        raise SemanticReviewError(f"semantic correction source ref {position} must be an object")
    page = ref.get("source_page")
    section = ref.get("section")
    locator = ref.get("locator")
    if not isinstance(page, int) or page < 1 or not isinstance(section, str) or not section.strip() or not isinstance(locator, str) or not locator.strip():
        raise SemanticReviewError(f"semantic correction source ref {position} is incomplete")
    source_text = ref.get("source_text")
    span_identity = ref.get("span_identity")
    if source_text is None and span_identity is None:
        raise SemanticReviewError(f"semantic correction source ref {position} requires source_text or span_identity")
    key = (page, _normal(section), _normal(locator))
    matches = index.get(key, [])
    if not matches:
        raise SemanticReviewError(f"semantic correction source ref {position} is not present in source evidence: {locator} on page {page}")
    if source_text is not None and not any(_normal(source_text) == _normal(record.get("text", record.get("summary"))) for record in matches):
        raise SemanticReviewError(f"semantic correction source ref {position} has a mismatched source_text: {locator} on page {page}")
    if span_identity is not None and not any(record.get("span_identity") == span_identity for record in matches):
        raise SemanticReviewError(f"semantic correction source ref {position} has a mismatched span_identity: {locator} on page {page}")
    return dict(ref)


def _refs(correction: Mapping[str, Any], index: Mapping[tuple[int, str, str], list[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    values = correction.get("source_refs")
    if not isinstance(values, list) or not values:
        raise SemanticReviewError("semantic correction source_refs must be a non-empty array")
    return [_validate_ref(value, index, position) for position, value in enumerate(values, start=1)]


def _record_from_refs(summary: str, evidence_type: str, refs: list[dict[str, Any]]) -> dict[str, Any]:
    first = refs[0]
    return {
        "text": summary,
        "summary": summary,
        "semantic_evidence_type": evidence_type,
        "source_page": first["source_page"],
        "section": first["section"],
        "locator": first["locator"],
        "confidence": "high",
        "source_refs": deepcopy(refs),
    }


def apply_semantic_review(candidate: Mapping[str, Any], source_digest: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Derive ``reviewed_paper_semantics`` and its provenance deterministically."""
    baseline = source_digest.get("paper_semantics")
    if not isinstance(baseline, Mapping):
        if candidate.get("semantic_corrections"):
            raise SemanticReviewError("semantic corrections require an extracted paper_semantics baseline")
        return {}, {"extracted_semantics_sha256": _sha256(None), "semantic_corrections_sha256": _sha256([]), "reviewed_semantics_sha256": _sha256(None)}
    candidate_baseline = candidate.get("paper_semantics")
    if candidate_baseline is not None and candidate_baseline != baseline:
        raise SemanticReviewError("candidate paper_semantics must preserve the extracted digest baseline")
    corrections = candidate.get("semantic_corrections", [])
    if not isinstance(corrections, list):
        raise SemanticReviewError("semantic_corrections must be an array")
    reviewed = deepcopy(dict(baseline))
    slots = reviewed.get("slots")
    if not isinstance(slots, dict):
        raise SemanticReviewError("paper_semantics.slots must be an object")
    index = _source_index(source_digest)
    for correction_index, raw in enumerate(corrections, start=1):
        if not isinstance(raw, Mapping):
            raise SemanticReviewError(f"semantic correction {correction_index} must be an object")
        slot = raw.get("slot")
        operation = raw.get("operation")
        summary = raw.get("reviewed_summary")
        evidence_type = raw.get("semantic_evidence_type")
        reason = raw.get("reason")
        if slot not in REQUIRED_SLOTS:
            raise SemanticReviewError(f"semantic correction {correction_index} has an unknown slot: {slot}")
        if operation not in _OPERATIONS:
            raise SemanticReviewError(f"semantic correction {correction_index} has an unsupported operation: {operation}")
        if not isinstance(summary, str) or (operation in {"replace_slot", "merge_evidence"} and not summary.strip()):
            raise SemanticReviewError(f"semantic correction {correction_index} requires reviewed_summary")
        if not isinstance(evidence_type, str) or not evidence_type.strip() or not isinstance(reason, str) or not reason.strip():
            raise SemanticReviewError(f"semantic correction {correction_index} requires semantic_evidence_type and reason")
        refs = _refs(raw, index)
        current = slots.get(slot)
        if operation == "replace_slot":
            slots[slot] = _record_from_refs(summary.strip(), evidence_type.strip(), refs)
        elif operation == "merge_evidence":
            slots[slot] = _record_from_refs(summary.strip(), evidence_type.strip(), refs)
            slots[slot]["merged_from"] = deepcopy(current) if isinstance(current, Mapping) else None
        elif operation == "add_supporting_evidence":
            if not isinstance(current, Mapping):
                current = _record_from_refs(summary.strip() or refs[0].get("source_text", "Reviewed supporting evidence."), evidence_type.strip(), refs)
                slots[slot] = current
            supporting = current.setdefault("supporting_evidence", [])
            if not isinstance(supporting, list):
                supporting = []
                current["supporting_evidence"] = supporting
            supporting.extend(deepcopy(refs))
            if summary.strip():
                current["summary"] = summary.strip()
                current["text"] = summary.strip()
        else:
            if isinstance(current, Mapping):
                supporting = current.get("supporting_evidence")
                if isinstance(supporting, list):
                    remove_keys = {(item.get("source_page"), _normal(item.get("section")), _normal(item.get("locator"))) for item in refs}
                    current["supporting_evidence"] = [item for item in supporting if not isinstance(item, Mapping) or (item.get("source_page"), _normal(item.get("section")), _normal(item.get("locator"))) not in remove_keys]
                if summary.strip():
                    current["summary"] = summary.strip()
                    current["text"] = summary.strip()
    provenance = {
        "extracted_semantics_sha256": _sha256(baseline),
        "semantic_corrections_sha256": _sha256(corrections),
        "reviewed_semantics_sha256": _sha256(reviewed),
    }
    return reviewed, provenance
