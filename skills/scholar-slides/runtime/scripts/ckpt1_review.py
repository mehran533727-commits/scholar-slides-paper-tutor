"""Validate and canonicalize unapproved CKPT-1 review candidates.

This module deliberately handles review evidence only.  It never records human
approval and it never modifies the extractive source digest supplied to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from paper_semantics import semantic_records
from schema_validation import create_schema_validator, resolve_skill_schema_path
from semantic_review import SemanticReviewError, apply_semantic_review


class ReviewCandidateError(ValueError):
    """Raised when a CKPT-1 candidate cannot be grounded or canonicalized."""


_APPROVAL_FIELDS = {
    "approved",
    "approval",
    "approval_status",
    "approved_by",
    "confirmed",
    "confirmed_by",
    "reviewed_by",
    "human_confirmed",
}
_TIMESTAMP_FIELDS = {"timestamp", "prepared_at", "created_at", "updated_at", "generated_at"}
_PAGE_RE = re.compile(r"\bp(?:age)?\.?\s*(\d+)\b", re.IGNORECASE)
_EVIDENCE_COLLECTIONS = (
    "proposed_claims",
    "proposed_contributions",
    "proposed_experimental_results",
    "proposed_key_metrics",
)


def _schema() -> Mapping[str, Any]:
    path = resolve_skill_schema_path("ckpt1-review.schema.json", anchor=__file__)
    return json.loads(path.read_text(encoding="utf-8"))


def _normal_text(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _page_from_ref(value: object) -> int | None:
    match = _PAGE_RE.search(str(value))
    return int(match.group(1)) if match else None


def _add_locator(index: set[tuple[int, str]], page: object, locator: object) -> None:
    if isinstance(page, int) and page >= 1 and isinstance(locator, str) and locator.strip():
        index.add((page, _normal_text(locator)))


def _source_locator_index(digest: Mapping[str, Any]) -> set[tuple[int, str]]:
    """Extract page-bound locators from generic digest metadata and asset inventory."""
    index: set[tuple[int, str]] = set()
    metadata = digest.get("paper_metadata")
    if isinstance(metadata, Mapping):
        evidence = metadata.get("evidence")
        if isinstance(evidence, Mapping):
            for field, record in evidence.items():
                if not isinstance(record, Mapping):
                    continue
                locations = record.get("locations")
                if isinstance(locations, list):
                    for location in locations:
                        page = _page_from_ref(location)
                        _add_locator(index, page, field)
                        _add_locator(index, page, f"{field} block")
    abstract = digest.get("abstract")
    if isinstance(abstract, Mapping):
        _add_locator(index, _page_from_ref(abstract.get("source_ref")), "abstract")
    for field in ("figures", "assets", "source_evidence", "source_locators"):
        records = digest.get(field)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            page = record.get("page", record.get("source_page"))
            for key in ("locator", "label", "id", "source_locator"):
                _add_locator(index, page, record.get(key))
    for record in semantic_records(digest.get("paper_semantics")):
        _add_locator(index, record.get("source_page"), record.get("locator"))
    return index


def _validate_schema(candidate: Mapping[str, Any]) -> None:
    errors = sorted(create_schema_validator(_schema()).iter_errors(candidate), key=lambda error: list(error.path))
    if errors:
        raise ReviewCandidateError(f"review candidate schema validation failed: {errors[0].message}")


def _has_approval_field(candidate: object, pointer: str = "") -> str | None:
    """Reject approval claims at every mapping/list depth in untrusted JSON."""
    if isinstance(candidate, Mapping):
        for key, value in candidate.items():
            normalized = _normal_text(key).replace(" ", "_")
            location = f"{pointer}/{key}" if pointer else str(key)
            if normalized in _APPROVAL_FIELDS or normalized.startswith(("approved_", "confirmed_")):
                return location
            nested = _has_approval_field(value, location)
            if nested:
                return nested
    elif isinstance(candidate, list):
        for index, value in enumerate(candidate):
            nested = _has_approval_field(value, f"{pointer}/{index}" if pointer else str(index))
            if nested:
                return nested
    return None


def _is_numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_numeric_evidence(candidate: Mapping[str, Any]) -> None:
    metrics = candidate.get("proposed_key_metrics")
    if not isinstance(metrics, list):
        return
    for metric in metrics:
        if isinstance(metric, Mapping) and _is_numeric(metric.get("value")) and not metric.get("evidence"):
            raise ReviewCandidateError("numeric metric requires evidence")


def _validate_duplicate_ids(candidate: Mapping[str, Any]) -> None:
    seen: set[str] = set()
    for collection in _EVIDENCE_COLLECTIONS:
        items = candidate.get(collection, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping) or "id" not in item:
                continue
            item_id = item["id"]
            if not isinstance(item_id, str) or not item_id.strip():
                raise ReviewCandidateError(f"{collection} id must be a non-empty string")
            if item_id in seen:
                raise ReviewCandidateError(f"duplicate id: {item_id}")
            seen.add(item_id)


def _validate_locator(evidence: object, index: set[tuple[int, str]]) -> None:
    if not isinstance(evidence, Mapping):
        return
    page, locator = evidence.get("page"), evidence.get("locator")
    if isinstance(page, int) and isinstance(locator, str) and (page, _normal_text(locator)) not in index:
        raise ReviewCandidateError(f"unknown source locator: {locator} on page {page}")


def _validate_source_evidence(candidate: Mapping[str, Any], digest: Mapping[str, Any]) -> None:
    candidate_semantics = candidate.get("paper_semantics")
    digest_semantics = digest.get("paper_semantics")
    if candidate_semantics is not None:
        if not isinstance(digest_semantics, Mapping) or candidate_semantics != digest_semantics:
            raise ReviewCandidateError("paper semantics must match the source digest")
        readiness = candidate.get("mode_b_narrative_readiness")
        digest_readiness = digest.get("mode_b_narrative_readiness")
        if readiness is not None and readiness != digest_readiness:
            raise ReviewCandidateError("Mode-B narrative readiness must match the source digest")
    try:
        apply_semantic_review(candidate, digest)
    except SemanticReviewError as exc:
        raise ReviewCandidateError(str(exc)) from exc
    index = _source_locator_index(digest)
    corrections = candidate.get("metadata_corrections", {})
    if isinstance(corrections, Mapping):
        for field, correction in corrections.items():
            if not isinstance(correction, Mapping):
                continue
            _validate_locator(correction.get("evidence"), index)
            metadata = digest.get("paper_metadata")
            automatic = metadata.get(field) if isinstance(metadata, Mapping) else None
            if automatic is not None and correction.get("original") != automatic:
                raise ReviewCandidateError(f"metadata correction for {field} must preserve the automatic original")
    for collection in _EVIDENCE_COLLECTIONS:
        items = candidate.get(collection, [])
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    _validate_locator(item.get("evidence"), index)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sort_key(value: object) -> tuple[str, str]:
    if isinstance(value, Mapping) and isinstance(value.get("id"), str):
        return ("id", value["id"])
    if isinstance(value, Mapping) and isinstance(value.get("marker"), str):
        return ("marker", value["marker"])
    if isinstance(value, Mapping) and isinstance(value.get("path"), str):
        return ("path", value["path"])
    if isinstance(value, str):
        return ("text", value)
    return ("json", _canonical_json(value))


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _sort_collections(candidate: dict[str, Any]) -> None:
    for field in (*_EVIDENCE_COLLECTIONS, "evidence_audits", "marker_resolutions", "unresolved_markers", "deck_forbidden_assets"):
        if isinstance(candidate.get(field), list):
            candidate[field] = sorted(candidate[field], key=_sort_key)


def canonicalize_review_candidate(candidate: Mapping[str, Any], source_digest: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic, source-grounded CKPT-1 candidate without mutating inputs."""
    if not isinstance(candidate, Mapping):
        raise ReviewCandidateError("review candidate must be an object")
    if not isinstance(source_digest, Mapping):
        raise ReviewCandidateError("source digest must be an object")
    approval_field = _has_approval_field(candidate)
    if approval_field:
        raise ReviewCandidateError(f"approval field is not supported in a review candidate: {approval_field}")
    _validate_numeric_evidence(candidate)
    _validate_schema(candidate)
    _validate_duplicate_ids(candidate)
    _validate_source_evidence(candidate, source_digest)
    canonical = _canonical(deepcopy(dict(candidate)))
    assert isinstance(canonical, dict)
    semantic_overlay_requested = (
        isinstance(source_digest.get("paper_semantics"), Mapping)
        or "semantic_corrections" in canonical
        or "reviewed_paper_semantics" in canonical
        or "semantic_review_provenance" in canonical
    )
    if semantic_overlay_requested:
        try:
            reviewed, provenance = apply_semantic_review(canonical, source_digest)
        except SemanticReviewError as exc:
            raise ReviewCandidateError(str(exc)) from exc
        if "paper_semantics" not in canonical and isinstance(source_digest.get("paper_semantics"), Mapping):
            canonical["paper_semantics"] = _canonical(deepcopy(source_digest["paper_semantics"]))
        supplied_reviewed = canonical.get("reviewed_paper_semantics")
        if supplied_reviewed is not None and supplied_reviewed != _canonical(reviewed):
            raise ReviewCandidateError("reviewed_paper_semantics is not the deterministic overlay result")
        supplied_provenance = canonical.get("semantic_review_provenance")
        if supplied_provenance is not None and supplied_provenance != provenance:
            raise ReviewCandidateError("semantic_review_provenance is not the deterministic overlay result")
        canonical["reviewed_paper_semantics"] = _canonical(reviewed)
        canonical["semantic_review_provenance"] = provenance
    _sort_collections(canonical)
    return canonical


def _without_timestamps(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_timestamps(child)
            for key, child in value.items()
            if str(key).casefold() not in _TIMESTAMP_FIELDS and not str(key).casefold().endswith("_at")
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


def review_semantic_identity(canonical_candidate: Mapping[str, Any]) -> str:
    """Hash semantic review content while deliberately excluding timestamps."""
    if not isinstance(canonical_candidate, Mapping):
        raise ReviewCandidateError("canonical review candidate must be an object")
    semantic = _without_timestamps(canonical_candidate)
    return hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()
