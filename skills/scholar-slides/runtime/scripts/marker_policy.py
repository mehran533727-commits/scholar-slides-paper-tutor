"""Marker-ledger policy shared by CKPT-1 readiness and deck selection."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from evidence_audit import EvidenceAuditError, resolve_canonical_source_pdf, validate_evidence_audit


class MarkerPolicyError(ValueError):
    """Raised when a marker ledger cannot safely define asset policy."""


def _normal(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _checkpoint_bound_ledger(root: Path) -> Path | None:
    """Find the one current marker ledger sealed in the local CKPT-1 record."""
    record_path = root / "checkpoint-1.json"
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarkerPolicyError(f"cannot read CKPT-1 record: {exc}") from exc
    bundle = record.get("artifact_bundle") if isinstance(record, dict) else None
    if not isinstance(bundle, list):
        raise MarkerPolicyError("CKPT-1 record has no artifact bundle")
    candidates: list[Path] = []
    for entry in bundle:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            continue
        path = Path(entry["path"]).resolve(strict=False)
        try:
            path.relative_to(root.resolve())
        except ValueError:
            raise MarkerPolicyError("checkpoint-bound marker ledger escapes the bundle")
        if not path.is_file() or _sha256(path).casefold() != entry["sha256"].casefold():
            raise MarkerPolicyError("checkpoint-bound marker ledger is stale")
        if path.suffix.casefold() != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == "scholar-slides-ckpt1-markers":
            candidates.append(path)
    if len(candidates) > 1:
        raise MarkerPolicyError("multiple checkpoint-bound CKPT-1 marker ledgers are present")
    return candidates[0] if candidates else None


def _is_checkpoint_bound_ledger_path(root: Path, ledger_path: Path) -> bool:
    """Return whether a fallback ledger is sealed by the current CKPT-1 record."""
    record_path = root / "checkpoint-1.json"
    if not record_path.is_file():
        return False
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarkerPolicyError(f"cannot read CKPT-1 record: {exc}") from exc
    bundle = record.get("artifact_bundle") if isinstance(record, dict) else None
    if not isinstance(bundle, list):
        raise MarkerPolicyError("CKPT-1 record has no artifact bundle")
    target = ledger_path.resolve(strict=False)
    for entry in bundle:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = Path(entry["path"]).resolve(strict=False)
        if path != target:
            continue
        expected_hash = entry.get("sha256")
        if not isinstance(expected_hash, str) or not path.is_file() or _sha256(path).casefold() != expected_hash.casefold():
            raise MarkerPolicyError("checkpoint-bound marker ledger is stale")
        return True
    return False


def _assert_checkpoint_canonical_source(root: Path) -> None:
    """Bind every CKPT-1 direct-selection ledger shape to its canonical PDF."""
    record_path = root / "checkpoint-1.json"
    if not record_path.is_file():
        return
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        digest_path = record.get("artifact", {}).get("path") if isinstance(record, dict) else None
        if not isinstance(digest_path, str):
            raise MarkerPolicyError("checkpoint record has no canonical digest identity")
        digest = json.loads(Path(digest_path).read_text(encoding="utf-8"))
        resolve_canonical_source_pdf(root, digest, record)
    except MarkerPolicyError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerPolicyError(f"cannot read checkpoint canonical source identity: {exc}") from exc
    except EvidenceAuditError as exc:
        raise MarkerPolicyError(f"canonical source PDF is invalid: {exc}") from exc


def _assert_checkpoint_review_marker_identity(root: Path, ledger: dict[str, Any]) -> None:
    """Reject direct selection when a bound review aliases a claim to another marker."""
    if ledger.get("kind") != "scholar-slides-ckpt1-markers":
        return
    record = json.loads((root / "checkpoint-1.json").read_text(encoding="utf-8"))
    bundle = record.get("artifact_bundle", []) if isinstance(record, dict) else []
    payloads: list[dict[str, Any]] = []
    bound_hashes: dict[Path, str] = {}
    for entry in bundle:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = Path(entry["path"])
        if isinstance(entry.get("sha256"), str):
            bound_hashes[path.resolve(strict=False)] = entry["sha256"]
        if path.suffix.casefold() != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    candidates = [payload for payload in payloads if payload.get("kind") == "scholar-slides-ckpt1-review"]
    digest_path = record.get("artifact", {}).get("path") if isinstance(record.get("artifact"), dict) else None
    if len(candidates) != 1 or not isinstance(digest_path, str):
        raise MarkerPolicyError("checkpoint-bound marker ledger has no canonical review/digest identity")
    try:
        digest = json.loads(Path(digest_path).read_text(encoding="utf-8"))
        source_pdf, canonical_source_sha256 = resolve_canonical_source_pdf(root, digest, record)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarkerPolicyError(f"cannot read checkpoint digest for marker ledger identity: {exc}") from exc
    except EvidenceAuditError as exc:
        raise MarkerPolicyError(f"canonical source PDF is invalid: {exc}") from exc
    items = {item.get("marker"): item for item in ledger.get("items", []) if isinstance(item, dict) and isinstance(item.get("marker"), str)}
    for collection in ("proposed_claims", "proposed_contributions", "proposed_experimental_results", "proposed_key_metrics"):
        for evidence in candidates[0].get(collection, []):
            if not isinstance(evidence, dict) or evidence.get("confidence") != "low":
                continue
            marker, source = evidence.get("marker_ref"), evidence.get("evidence")
            if not isinstance(marker, str) or not isinstance(source, dict):
                raise MarkerPolicyError("marker ledger asset identity is missing a low-confidence marker reference")
            matches = {
                asset.get("id") for asset in digest.get("assets", [])
                if isinstance(asset, dict) and asset.get("page", asset.get("source_page")) == source.get("page")
                and isinstance(asset.get("id"), str)
                and any(isinstance(locator, str) and _normal(locator) == _normal(source.get("locator")) for locator in (asset.get("id"), asset.get("locator"), asset.get("label"), asset.get("source_locator")))
            }
            item = items.get(marker)
            claim_ref = evidence.get("audit_ref")
            ledger_ref = item.get("audit_ref") if isinstance(item, dict) else None
            if (
                len(matches) != 1
                or not isinstance(item, dict)
                or item.get("asset_id") != next(iter(matches))
                or not isinstance(claim_ref, dict)
                or not isinstance(ledger_ref, dict)
                or (claim_ref.get("path"), claim_ref.get("sha256")) != (ledger_ref.get("path"), ledger_ref.get("sha256"))
            ):
                raise MarkerPolicyError("marker ledger asset identity does not match its used low-confidence evidence")
            raw_path, expected_hash = claim_ref.get("path"), claim_ref.get("sha256")
            audit_path = (root / raw_path).resolve(strict=False) if isinstance(raw_path, str) else None
            if audit_path not in bound_hashes or bound_hashes[audit_path] != expected_hash or not audit_path.is_file() or _sha256(audit_path) != expected_hash:
                raise MarkerPolicyError("marker ledger audit reference is not current and checkpoint-bound")
            try:
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                validate_evidence_audit(audit, source_pdf=source_pdf, project_root=root, expected_source_sha256=canonical_source_sha256)
            except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, EvidenceAuditError) as exc:
                raise MarkerPolicyError(f"marker ledger audit reference is invalid: {exc}") from exc
            audit_source = audit.get("source") if isinstance(audit, dict) else None
            canonical_asset = next(iter(matches))
            if (
                audit.get("asset_id") != canonical_asset
                or not isinstance(audit_source, dict)
                or audit_source.get("page") != source.get("page")
                or _normal(audit_source.get("locator")) != _normal(source.get("locator"))
            ):
                raise MarkerPolicyError("marker ledger asset identity does not match its used low-confidence evidence")


def load_marker_ledger(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MarkerPolicyError(f"marker ledger is missing: {target}") from exc
    except json.JSONDecodeError as exc:
        raise MarkerPolicyError(f"marker ledger is invalid JSON: {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MarkerPolicyError("marker ledger must be an object")
    if payload.get("kind") == "scholar-slides-ckpt1-markers" and isinstance(payload.get("items"), list):
        return payload
    if not isinstance(payload.get("markers"), list):
        raise MarkerPolicyError("marker ledger must contain a markers or items list")
    return payload


def build_marker_ledger(digest: dict[str, Any], review: dict[str, Any], source_digest_sha256: str) -> dict[str, Any]:
    """Project the canonical review decisions onto every original digest marker.

    The review supplies the decision; this builder supplies stable flag pointers
    and deliberately retains the literal digest markers instead of rewriting the
    extractive artifact.
    """
    flags = digest.get("flags")
    resolutions = review.get("marker_resolutions")
    if not isinstance(flags, list) or not all(isinstance(item, str) and item for item in flags):
        raise MarkerPolicyError("digest flags must be non-empty marker strings")
    if not isinstance(resolutions, list):
        raise MarkerPolicyError("review marker_resolutions must be a list")
    by_marker = {
        item.get("marker"): item for item in resolutions
        if isinstance(item, dict) and isinstance(item.get("marker"), str)
    }
    items: list[dict[str, Any]] = []
    for index, marker in enumerate(flags):
        decision = by_marker.get(marker, {})
        resolution = decision.get("resolution", "unresolved")
        if resolution not in {"unresolved", "resolved_with_audit", "excluded_from_deck"}:
            raise MarkerPolicyError(f"invalid resolution for marker: {marker}")
        classification = decision.get("classification", "critical")
        if classification not in {"critical", "noncritical"}:
            raise MarkerPolicyError(f"invalid classification for marker: {marker}")
        item = {"marker": marker, "source_pointer": decision.get("source_pointer", f"/flags/{index}"),
                "classification": classification, "resolution": resolution,
                "reason": decision.get("reason", "no review resolution recorded")}
        if isinstance(decision.get("audit_ref"), dict):
            item["audit_ref"] = decision["audit_ref"]
        if isinstance(decision.get("asset_id"), str) and decision["asset_id"]:
            item["asset_id"] = decision["asset_id"]
        items.append(item)
    return {"schema_version": 1, "kind": "scholar-slides-ckpt1-markers",
            "source_digest_sha256": source_digest_sha256, "items": items}


def forbidden_assets_for_next_stage(ledger: dict[str, Any]) -> list[str]:
    """Return deduplicated deferred asset IDs; malformed deferred entries fail closed."""
    if ledger.get("kind") == "scholar-slides-ckpt1-markers":
        items = ledger.get("items")
        if not isinstance(items, list):
            raise MarkerPolicyError("marker ledger must contain an items list")
        forbidden = {
            item.get("asset_id") for item in items
            if isinstance(item, dict) and item.get("resolution") == "excluded_from_deck"
            and isinstance(item.get("asset_id"), str) and item["asset_id"]
        }
        return sorted(forbidden)
    markers = ledger.get("markers")
    if not isinstance(markers, list):
        raise MarkerPolicyError("marker ledger must contain a markers list")
    forbidden: set[str] = set()
    for index, item in enumerate(markers, start=1):
        if not isinstance(item, dict):
            raise MarkerPolicyError(f"marker ledger entry {index} must be an object")
        if item.get("status") == "resolved_with_audit":
            continue
        if item.get("can_defer_to_ckpt2") is not True:
            continue
        asset = item.get("asset")
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if not isinstance(asset_id, str) or not asset_id:
            raise MarkerPolicyError(f"deferred marker ledger entry {index} has no asset.id")
        forbidden.add(asset_id)
    return sorted(forbidden)


def assert_assets_allowed(bundle_dir: str | Path, asset_ids: list[str] | tuple[str, ...]) -> None:
    """Reject an explicit deck selection that references a CKPT-1 deferred asset."""
    if not asset_ids:
        return
    root = Path(bundle_dir).resolve()
    bound = _checkpoint_bound_ledger(root)
    if bound is not None:
        candidates = [bound]
    else:
        legacy = root / "ckpt1-markers.json"
        candidates = [legacy] if legacy.is_file() else []
    if not candidates:
        return
    if bound is not None or _is_checkpoint_bound_ledger_path(root, candidates[0]):
        _assert_checkpoint_canonical_source(root)
    ledger = load_marker_ledger(candidates[0])
    if bound is not None:
        _assert_checkpoint_review_marker_identity(root, ledger)
    forbidden = set(forbidden_assets_for_next_stage(ledger))
    requested = {asset_id for asset_id in asset_ids if isinstance(asset_id, str) and asset_id}
    blocked = sorted(requested & forbidden)
    if blocked:
        raise MarkerPolicyError(
            "CKPT-1 deferred asset(s) cannot enter deck generation: " + ", ".join(blocked)
        )
