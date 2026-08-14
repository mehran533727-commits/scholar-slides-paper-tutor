#!/usr/bin/env python3
"""Fail-closed, dependency-driven CKPT-1 readiness checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import checkpoint
from ckpt1_review import ReviewCandidateError, canonicalize_review_candidate
from ckpt1_quantitative import check_quantitative_compatibility
from evidence_audit import EvidenceAuditError, resolve_canonical_source_pdf, validate_evidence_audit
from marker_policy import MarkerPolicyError, build_marker_ledger, forbidden_assets_for_next_stage, load_marker_ledger
from paper_metadata import validate_metadata_for_ckpt1_preparation


class ReadinessError(RuntimeError):
    """Raised when CKPT-1 has a blocker other than human approval."""


_IDENTITY_FIELDS = ("requested_identifier", "resolved_identifier", "pdf_sha256", "fetched_at")
_EVIDENCE_COLLECTIONS = ("proposed_claims", "proposed_contributions", "proposed_experimental_results", "proposed_key_metrics")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{label} must be a JSON object")
    return value


def _within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _bound_paths(record: dict[str, Any], root: Path) -> set[Path]:
    paths: set[Path] = set()
    for entry in record.get("artifact_bundle", []):
        raw = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            raise ReadinessError("checkpoint artifact bundle has an invalid path")
        path = Path(raw).resolve(strict=True)
        if not _within(root, path):
            raise ReadinessError(f"checkpoint artifact bundle path escapes paper bundle: {path}")
        paths.add(path)
    return paths


def _json_artifacts(paths: set[Path]) -> list[tuple[Path, dict[str, Any]]]:
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(paths):
        if path.suffix.casefold() != ".json":
            continue
        try:
            result.append((path, _read_json(path, f"checkpoint artifact {path.name}")))
        except ReadinessError:
            continue
    return result


def _one_kind(artifacts: list[tuple[Path, dict[str, Any]]], kind: str, label: str) -> tuple[Path, dict[str, Any]]:
    matches = [(path, value) for path, value in artifacts if value.get("kind") == kind]
    if len(matches) != 1:
        raise ReadinessError(f"checkpoint artifact bundle requires exactly one {label}")
    return matches[0]


def _resolve_ref(root: Path, bound: set[Path], reference: Any, label: str) -> Path:
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or not isinstance(reference.get("sha256"), str):
        raise ReadinessError(f"{label} must contain path and SHA-256")
    path = (root / reference["path"]).resolve(strict=False)
    if not _within(root, path) or not path.is_file() or path not in bound:
        raise ReadinessError(f"{label} is missing, unbound, or escapes the paper bundle")
    if _sha256(path).casefold() != reference["sha256"].casefold():
        raise ReadinessError(f"{label} SHA-256 is stale")
    return path


def _validate_source(digest: dict[str, Any], record: dict[str, Any], root: Path) -> tuple[Path, str]:
    source, identity = digest.get("source"), record.get("source_identity")
    if not isinstance(source, dict) or not isinstance(identity, dict):
        raise ReadinessError("source identity is required in digest and checkpoint")
    for field in _IDENTITY_FIELDS:
        if not isinstance(source.get(field), str) or identity.get(field) != source[field]:
            raise ReadinessError(f"source identity mismatch for {field}")
    try:
        return resolve_canonical_source_pdf(root, digest, record)
    except EvidenceAuditError as exc:
        raise ReadinessError(str(exc)) from exc


def _normal(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _canonical_asset_id(digest: dict[str, Any], evidence: Any) -> str:
    """Resolve a review locator to one concrete digest asset identity."""
    if not isinstance(evidence, dict) or not isinstance(evidence.get("page"), int) or not isinstance(evidence.get("locator"), str):
        raise ReadinessError("low-confidence evidence has no page-bound asset identity")
    matches: set[str] = set()
    for asset in digest.get("assets", []):
        if not isinstance(asset, dict) or asset.get("page", asset.get("source_page")) != evidence["page"]:
            continue
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        locators = (asset_id, asset.get("locator"), asset.get("label"), asset.get("source_locator"))
        if any(isinstance(locator, str) and _normal(locator) == _normal(evidence["locator"]) for locator in locators):
            matches.add(asset_id)
    if len(matches) != 1:
        raise ReadinessError("low-confidence evidence does not resolve to one canonical asset identity")
    return matches.pop()


def _validate_audits(candidate: dict[str, Any], digest: dict[str, Any], root: Path, bound: set[Path], source_pdf: Path, canonical_source_sha256: str) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str], list[tuple[str, str, tuple[Any, Any]]]]:
    audits: dict[tuple[str, str], dict[str, Any]] = {}
    for reference in candidate.get("evidence_audits", []):
        path = _resolve_ref(root, bound, reference, "evidence audit")
        audit = _read_json(path, "evidence audit")
        try:
            validate_evidence_audit(audit, source_pdf=source_pdf, project_root=root, expected_source_sha256=canonical_source_sha256)
        except EvidenceAuditError as exc:
            raise ReadinessError(f"evidence audit is invalid: {exc}") from exc
        audits[(reference["path"], reference["sha256"])] = audit
    used_assets: set[str] = set()
    used_bindings: list[tuple[str, str, tuple[Any, Any]]] = []
    for collection in _EVIDENCE_COLLECTIONS:
        for item in candidate.get(collection, []):
            if not isinstance(item, dict) or item.get("confidence") != "low":
                continue
            reference = item.get("audit_ref")
            if not isinstance(reference, dict):
                raise ReadinessError(f"used low-confidence evidence in {collection} requires a current audit")
            key = (reference.get("path"), reference.get("sha256"))
            audit = audits.get(key)
            if audit is None:
                raise ReadinessError(f"used low-confidence evidence in {collection} requires a current audit")
            asset_id = _canonical_asset_id(digest, item.get("evidence"))
            source = audit.get("source")
            if (
                not isinstance(source, dict)
                or audit.get("asset_id") != asset_id
                or source.get("page") != item["evidence"].get("page")
                or _normal(source.get("locator")) != _normal(item["evidence"].get("locator"))
            ):
                raise ReadinessError("low-confidence evidence audit asset identity does not match the claim")
            marker = item.get("marker_ref")
            if not isinstance(marker, str) or not marker:
                raise ReadinessError("low-confidence evidence requires a marker_ref for its ledger asset identity")
            used_assets.add(asset_id)
            used_bindings.append((marker, asset_id, key))
    return audits, used_assets, used_bindings


def _validate_ledger(digest: dict[str, Any], candidate: dict[str, Any], ledger: dict[str, Any], digest_path: Path, audits: dict[tuple[str, str], dict[str, Any]], used_assets: set[str], used_bindings: list[tuple[str, str, tuple[Any, Any]]]) -> tuple[int, list[str], list[str]]:
    expected = build_marker_ledger(digest, candidate, _sha256(digest_path))
    if ledger != expected:
        raise ReadinessError("marker ledger does not match the current digest and canonical review decisions")
    expected_unresolved = sorted(item["marker"] for item in ledger["items"] if item["resolution"] == "unresolved")
    if sorted(candidate.get("unresolved_markers", [])) != expected_unresolved:
        raise ReadinessError("review unresolved_markers does not match marker ledger")
    expected_forbidden = forbidden_assets_for_next_stage(ledger)
    if sorted(candidate.get("deck_forbidden_assets", [])) != expected_forbidden:
        raise ReadinessError("review deck_forbidden_assets does not match marker ledger")
    by_marker = {item["marker"]: item for item in ledger["items"]}
    for marker, asset_id, audit_key in used_bindings:
        item = by_marker.get(marker)
        reference = item.get("audit_ref") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("asset_id") != asset_id
            or not isinstance(reference, dict)
            or (reference.get("path"), reference.get("sha256")) != audit_key
        ):
            raise ReadinessError("marker ledger asset identity does not match its used low-confidence evidence")
    resolved, warnings, blockers = 0, [], []
    for item in ledger["items"]:
        resolution = item["resolution"]
        if resolution == "resolved_with_audit":
            reference = item.get("audit_ref")
            if not isinstance(reference, dict) or (reference.get("path"), reference.get("sha256")) not in audits:
                raise ReadinessError(f"resolved marker lacks a current audit: {item['marker']}")
            resolved += 1
        elif resolution == "unresolved":
            (blockers if item["classification"] == "critical" else warnings).append(item["marker"])
    if blockers:
        raise ReadinessError("unresolved CKPT-1 marker blocker(s): " + ", ".join(sorted(blockers)))
    return resolved, sorted(warnings), forbidden_assets_for_next_stage(ledger)


def check_readiness(bundle_dir: str | Path, checkpoint_path: str | Path | None = None, *, require_readiness_artifact: bool = False) -> dict[str, Any]:
    """Validate actual evidence dependencies; never record user approval."""
    root = Path(bundle_dir).resolve(strict=True)
    record_path = Path(checkpoint_path) if checkpoint_path else root / "checkpoint-1.json"
    try:
        record = checkpoint._validate_record(checkpoint._read_record(record_path)); checkpoint._validate_record_runtime(record)
    except checkpoint.CheckpointError as exc:
        raise ReadinessError(str(exc)) from exc
    if record.get("checkpoint") != "CKPT-1" or record.get("status") != "pending_human_confirmation" or record.get("confirmed_by") is not None:
        raise ReadinessError("CKPT-1 must remain pending_human_confirmation and not approved")
    bound = _bound_paths(record, root)
    artifact_path = Path(record["artifact"]["path"]).resolve(strict=True)
    if artifact_path not in bound:
        raise ReadinessError("digest artifact is not bound in checkpoint")
    digest = _read_json(artifact_path, "digest")
    if digest.get("schema_version") != 1 or digest.get("extractive_only") is not True or digest.get("review_status") != "pending_human_confirmation":
        raise ReadinessError("digest is not an extractive pending CKPT-1 artifact")
    metadata = digest.get("paper_metadata")
    if not isinstance(metadata, dict):
        raise ReadinessError("CKPT-1 metadata blocker: paper_metadata is MISSING")
    source_pdf, canonical_source_sha256 = _validate_source(digest, record, root)
    artifacts = _json_artifacts(bound)
    _, candidate = _one_kind(artifacts, "scholar-slides-ckpt1-review", "canonical CKPT-1 review candidate")
    ledger_path, raw_ledger = _one_kind(artifacts, "scholar-slides-ckpt1-markers", "marker ledger")
    try:
        canonical = canonicalize_review_candidate(candidate, digest)
    except ReviewCandidateError as exc:
        raise ReadinessError(f"canonical CKPT-1 review is invalid: {exc}") from exc
    digest_ref = canonical.get("source_digest")
    if _resolve_ref(root, bound, digest_ref, "review source digest") != artifact_path:
        raise ReadinessError("review source digest does not refer to the checkpoint digest")
    try:
        ledger = load_marker_ledger(ledger_path)
    except MarkerPolicyError as exc:
        raise ReadinessError(str(exc)) from exc
    audits, used_assets, used_bindings = _validate_audits(canonical, digest, root, bound, source_pdf, canonical_source_sha256)
    metadata_blockers = validate_metadata_for_ckpt1_preparation(metadata, canonical, audits)
    if metadata_blockers:
        raise ReadinessError("; ".join(metadata_blockers))
    resolved, warnings, forbidden = _validate_ledger(digest, canonical, ledger, artifact_path, audits, used_assets, used_bindings)
    quantitative = check_quantitative_compatibility(digest, canonical)
    if not quantitative["ready"]:
        raise ReadinessError("Mode-B quantitative compatibility is incomplete: " + ", ".join(quantitative["missing_fields"]))
    forbidden_set = set(forbidden)
    blocked_assets = sorted(used_assets & forbidden_set)
    if blocked_assets:
        raise ReadinessError("forbidden CKPT-1 asset referenced by used evidence: " + ", ".join(blocked_assets))
    narrative = digest.get("mode_b_narrative_readiness") if isinstance(digest.get("mode_b_narrative_readiness"), dict) else {}
    missing_narrative = narrative.get("missing_slots") if isinstance(narrative.get("missing_slots"), list) else []
    report = {
        "schema_version": 1,
        "kind": "scholar-slides-ckpt1-readiness",
        "checkpoint": "CKPT-1",
        "status": "ready_for_human_approval",
        "ready_for_human_confirmation": True,
        "approval_status": "not_approved",
        "human_review_required": True,
        "approval_recorded": False,
        "unresolved_blockers": [],
        "resolved_markers": resolved,
        "warnings": warnings,
        "forbidden_assets_for_next_stage": forbidden,
        "mode_b_narrative_ready": narrative.get("ready") is True,
        "mode_b_narrative_missing_slots": list(missing_narrative),
        "mode_b_quantitative_ready": quantitative["ready"],
        "mode_b_quantitative_missing_fields": list(quantitative["missing_fields"]),
        "mode_b_quantitative_sources": quantitative["sources"],
        "mode_b_ready": narrative.get("ready") is True and quantitative["ready"],
    }
    if require_readiness_artifact:
        reports = [value for _, value in artifacts if value.get("kind") == "scholar-slides-ckpt1-readiness"]
        if len(reports) != 1 or reports[0] != report:
            raise ReadinessError("existing readiness report does not match current evidence dependencies")
    return report


def write_readiness(bundle_dir: str | Path, out_path: str | Path | None = None, checkpoint_path: str | Path | None = None) -> Path:
    result = check_readiness(bundle_dir, checkpoint_path)
    target = Path(out_path) if out_path else Path(bundle_dir) / "ckpt1-readiness.json"
    target.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CKPT-1 readiness without recording human approval.")
    parser.add_argument("bundle"); parser.add_argument("--checkpoint"); parser.add_argument("--out"); parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check_readiness(args.bundle, args.checkpoint, require_readiness_artifact=args.verify_existing)
        if args.verify_existing: print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return 0
        print(f"CKPT-1 readiness -> {write_readiness(args.bundle, args.out, args.checkpoint)}")
    except (ReadinessError, OSError) as exc:
        print(f"ckpt1_readiness: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
