"""Deterministically project human-confirmed CKPT-1 review evidence for deck consumers."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ckpt1_review import ReviewCandidateError, canonicalize_review_candidate, review_semantic_identity
from evidence_audit import EvidenceAuditError, resolve_canonical_source_pdf, sha256_file


class CKPT1ResolvedViewError(ValueError):
    """A review overlay is not yet eligible for any narrative or deck consumer."""


_BINDINGS = {"source_pdf_sha256", "digest_sha256", "project_options_sha256", "review_json_sha256", "review_markdown_sha256", "marker_ledger_sha256", "readiness_sha256", "evidence_audit_sha256", "candidate_sha256"}


def _entry_by_name(checkpoint: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    entries = checkpoint.get("artifact_bundle")
    if not isinstance(entries, list):
        raise CKPT1ResolvedViewError("confirmed CKPT-1 artifact bundle is missing")
    matches = [entry for entry in entries if isinstance(entry, Mapping) and Path(str(entry.get("path", ""))).name == name]
    if len(matches) != 1:
        raise CKPT1ResolvedViewError(f"confirmed CKPT-1 requires exactly one {name}")
    return matches[0]


def _assert_entry(entry: Mapping[str, Any], expected: str) -> Path:
    path, digest = entry.get("path"), entry.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or digest != expected:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 approval binding does not match its artifact bundle")
    target = Path(path)
    if not target.is_file() or sha256_file(target) != digest:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 approval artifact is stale")
    return target


def _assert_bound_view(digest: Mapping[str, Any], review: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> None:
    bindings = checkpoint.get("approval_bindings")
    if not isinstance(bindings, Mapping) or set(bindings) != _BINDINGS:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 approval bindings are incomplete")
    if checkpoint.get("candidate_sha256") != bindings.get("candidate_sha256"):
        raise CKPT1ResolvedViewError("confirmed CKPT-1 prepared candidate identity does not match its approval binding")
    if not all(isinstance(bindings.get(name), str) and len(bindings[name]) == 64 for name in _BINDINGS - {"evidence_audit_sha256"}):
        raise CKPT1ResolvedViewError("confirmed CKPT-1 approval bindings are invalid")
    digest_path = _assert_entry(_entry_by_name(checkpoint, "digest.json"), bindings["digest_sha256"])
    review_path = _assert_entry(_entry_by_name(checkpoint, "ckpt1-review.json"), bindings["review_json_sha256"])
    for name, binding in (("project-options.json", "project_options_sha256"), ("ckpt1-review.md", "review_markdown_sha256"), ("ckpt1-markers.json", "marker_ledger_sha256"), ("ckpt1-readiness.json", "readiness_sha256")):
        _assert_entry(_entry_by_name(checkpoint, name), bindings[binding])
    if json.loads(digest_path.read_text(encoding="utf-8")) != dict(digest) or json.loads(review_path.read_text(encoding="utf-8")) != dict(review):
        raise CKPT1ResolvedViewError("resolver inputs do not match confirmed CKPT-1 artifacts")
    root = digest_path.parent
    try:
        _, source_sha = resolve_canonical_source_pdf(root, digest, checkpoint)
    except EvidenceAuditError as exc:
        raise CKPT1ResolvedViewError(f"confirmed CKPT-1 source is stale or invalid: {exc}") from exc
    if source_sha != bindings["source_pdf_sha256"]:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 source binding does not match canonical PDF")
    audit_entries = [entry for entry in checkpoint.get("artifact_bundle", []) if isinstance(entry, Mapping) and str(entry.get("path", "")).endswith(".json") and _is_audit(entry)]
    audit_hashes = sorted(_assert_entry(entry, entry.get("sha256")) and entry["sha256"] for entry in audit_entries)
    if not isinstance(bindings["evidence_audit_sha256"], list) or audit_hashes != bindings["evidence_audit_sha256"]:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 evidence-audit bindings are stale")


def _is_audit(entry: Mapping[str, Any]) -> bool:
    try:
        payload = json.loads(Path(str(entry["path"])).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == "scholar-slides-evidence-audit"


def resolve_ckpt1_view(
    digest: Mapping[str, Any], review: Mapping[str, Any], checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only formal, approved review content; never mutate its evidence inputs."""
    if not isinstance(checkpoint, Mapping) or checkpoint.get("checkpoint") != "CKPT-1" or checkpoint.get("status") != "confirmed":
        raise CKPT1ResolvedViewError("CKPT-1 review overlays require a confirmed checkpoint")
    if not isinstance(checkpoint.get("confirmed_by"), str) or not checkpoint["confirmed_by"].strip():
        raise CKPT1ResolvedViewError("CKPT-1 confirmation identity is required")
    if isinstance(checkpoint.get("approval_bindings"), dict):
        _assert_bound_view(digest, review, checkpoint)
    try:
        candidate = canonicalize_review_candidate(review, digest)
    except ReviewCandidateError as exc:
        raise CKPT1ResolvedViewError(f"CKPT-1 review candidate is invalid: {exc}") from exc
    corrections = candidate.get("metadata_corrections") if isinstance(candidate.get("metadata_corrections"), dict) else {}
    metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), dict) else {}
    title = corrections.get("title", {}).get("proposed") if isinstance(corrections.get("title"), dict) else None
    authors = corrections.get("authors", {}).get("proposed") if isinstance(corrections.get("authors"), dict) else None
    if not isinstance(title, str) or not title.strip():
        title = metadata.get("title")
    if not isinstance(authors, list) or not all(isinstance(item, str) and item.strip() for item in authors):
        authors = metadata.get("authors")
    if not isinstance(title, str) or not title.strip() or not isinstance(authors, list) or not all(isinstance(item, str) and item.strip() for item in authors):
        raise CKPT1ResolvedViewError("resolved CKPT-1 metadata is incomplete")
    candidate_identity = review_semantic_identity(candidate)
    if isinstance(checkpoint.get("approval_bindings"), dict) and candidate_identity != checkpoint["approval_bindings"]["candidate_sha256"]:
        raise CKPT1ResolvedViewError("confirmed CKPT-1 candidate identity does not match supplied review")
    reviewed_semantics = candidate.get("reviewed_paper_semantics")
    if not isinstance(reviewed_semantics, Mapping):
        reviewed_semantics = candidate.get("paper_semantics")
    return {
        "schema_version": 1,
        "confirmation_path": "bound" if isinstance(checkpoint.get("approval_bindings"), dict) else "legacy_confirmed",
        "title": title.strip(),
        "authors": list(authors),
        "claims": deepcopy(candidate.get("proposed_claims", [])),
        "contributions": deepcopy(candidate.get("proposed_contributions", [])),
        "experimental_results": deepcopy(candidate.get("proposed_experimental_results", [])),
        "key_metrics": deepcopy(candidate.get("proposed_key_metrics", [])),
        "paper_semantics": deepcopy(reviewed_semantics),
        "extracted_paper_semantics": deepcopy(candidate.get("paper_semantics")),
        "reviewed_paper_semantics": deepcopy(reviewed_semantics),
        "semantic_review_provenance": deepcopy(candidate.get("semantic_review_provenance")),
        "mode_b_narrative_readiness": deepcopy(candidate.get("mode_b_narrative_readiness")),
        "deck_forbidden_assets": sorted(set(candidate.get("deck_forbidden_assets", []))),
        "marker_policy": deepcopy(candidate.get("marker_resolutions", [])),
    }


def _text(value: Any, fallback: str = "") -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or fallback


def reviewed_evidence_from_resolved(items: Sequence[Mapping[str, Any]], *, label: str) -> list[dict[str, Any]]:
    """Adapt confirmed resolved-view evidence items to the planner's reviewed shape.

    This only translates (text, evidence.page, evidence.locator) into the reviewed-item
    fields consumed by the narrative planner and notes writer; it never re-derives
    metadata or re-runs correction logic.
    """
    adapted: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise CKPT1ResolvedViewError(f"{label} must contain objects")
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        page = evidence.get("page")
        locator = _text(evidence.get("locator"), "")
        summary = _text(item.get("text"), "")
        if not summary or not isinstance(page, int) or page < 1 or not locator:
            raise CKPT1ResolvedViewError(f"{label} resolved evidence {index} cannot be grounded")
        adapted_item = {
            "summary": summary,
            "evidence": locator,
            "source_page": page,
            "section": _text(evidence.get("section"), locator),
            "figure_table_equation": locator,
        }
        # Preserve optional, already-reviewed semantic audit metadata in the
        # in-memory projection.  This does not alter checkpoint-1 or recalculate
        # any evidence; it prevents the planner from losing a direct source
        # section/role supplied by the confirmed review.
        for key in (
            "id", "importance", "appendix", "audit_ref",
            "semantic_evidence_type", "evidence_type", "semantic_type", "direct_source_evidence",
        ):
            if key in item:
                adapted_item[key] = deepcopy(item[key])
        adapted.append(adapted_item)
    return adapted


def build_confirmed_semantic_digest(
    project_root: str | Path,
    raw_digest: Mapping[str, Any],
    checkpoint_record: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the enriched in-memory semantic digest from the confirmed CKPT-1 resolved view.

    The raw ``digest.json`` artifact remains the persisted provenance; this function
    only constructs the runtime semantic view shared by deck generation and semantic QA.
    """
    root = Path(project_root)
    review_path = root / "ckpt1-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    view = resolve_ckpt1_view(raw_digest, review, checkpoint_record)
    enriched = dict(raw_digest)
    metadata = dict(enriched.get("paper_metadata", {}))
    metadata["title"] = view["title"]
    metadata["authors"] = view["authors"]
    enriched["paper_metadata"] = metadata
    enriched["reviewed_claims"] = reviewed_evidence_from_resolved(view["claims"], label="reviewed_claims")
    enriched["reviewed_contributions"] = reviewed_evidence_from_resolved(view["contributions"], label="reviewed_contributions")
    enriched["reviewed_experimental_results"] = reviewed_evidence_from_resolved(view["experimental_results"], label="reviewed_experimental_results")
    enriched["reviewed_key_metrics"] = view["key_metrics"]
    if isinstance(view.get("reviewed_paper_semantics"), Mapping) and view["reviewed_paper_semantics"]:
        enriched["paper_semantics"] = deepcopy(view["reviewed_paper_semantics"])
        enriched["reviewed_paper_semantics"] = deepcopy(view["reviewed_paper_semantics"])
    if view.get("semantic_review_provenance") is not None:
        enriched["semantic_review_provenance"] = deepcopy(view["semantic_review_provenance"])
    return enriched
