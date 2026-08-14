"""Prepare a generic CKPT-1 evidence bundle without recording human approval."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import checkpoint
import ckpt1_readiness
from ckpt1_review import ReviewCandidateError, canonicalize_review_candidate, review_semantic_identity
from ckpt1_review_markdown import project_review_markdown
from evidence_audit import EvidenceAuditError, resolve_canonical_source_pdf
from marker_policy import MarkerPolicyError, build_marker_ledger
from project_config import CONFIG_VERSION, DEFAULT_OPTIONS
from user_documents import write_paper_analysis


class PrepareCheckpointError(RuntimeError):
    """A candidate cannot safely become a pending CKPT-1 bundle."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrepareCheckpointError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PrepareCheckpointError(f"{label} must be a JSON object")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise PrepareCheckpointError(f"cannot atomically write {path.name}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_ref(project: Path, reference: object, digest_path: Path) -> None:
    if not isinstance(reference, dict) or reference.get("sha256") != _sha256(digest_path):
        raise PrepareCheckpointError("review input has a stale source digest SHA-256")
    raw = reference.get("path")
    if not isinstance(raw, str) or (project / raw).resolve(strict=False) != digest_path:
        raise PrepareCheckpointError("review input must refer to this project's canonical digest.json")


def _external_audits(project: Path) -> list[Path]:
    root = project / "audits"
    if not root.exists():
        return []
    if not root.is_dir() or root.is_symlink():
        raise PrepareCheckpointError("audits must be a real directory within the project")
    files: list[Path] = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise PrepareCheckpointError(f"audit artifact may not be a symlink: {item}")
        if item.is_file():
            files.append(item.resolve(strict=True))
    return files


def _agent(name: str) -> dict[str, str]:
    cleaned = name.strip()
    if not cleaned:
        raise PrepareCheckpointError("--prepared-by must name the preparing agent")
    return {"kind": "agent", "name": cleaned}


def _attach_digest_semantics(candidate: dict[str, Any], digest: Mapping[str, Any]) -> None:
    """Carry the digest semantic view into a new review without rewriting it."""
    for field in ("paper_semantics", "mode_b_narrative_readiness"):
        source = digest.get(field)
        if source is None:
            continue
        if field not in candidate:
            candidate[field] = deepcopy(source)
        elif candidate[field] != source:
            raise PrepareCheckpointError(f"review input {field} does not match the source digest")


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    """Keep pre-prepare state so a caught installation interruption is recoverable."""
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore(snapshot: dict[Path, bytes | None]) -> None:
    for path, previous in snapshot.items():
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, previous)
        except BaseException:
            # The original exception remains the actionable failure; a future invocation will
            # refuse a stale/partial checkpoint rather than treating it as human confirmation.
            pass


def _has_approval_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "_".join(str(key).casefold().split())
            if normalized in {"approved", "approved_by", "confirmed", "confirmed_by", "reviewed_by", "human_confirmed"}:
                return True
            if _has_approval_field(child):
                return True
    elif isinstance(value, list):
        return any(_has_approval_field(child) for child in value)
    return False


def _formal_paths(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    return (root / "ckpt1-review.json", root / "ckpt1-review.md", root / "ckpt1-markers.json", root / "ckpt1-readiness.json", root / "checkpoint-1.json")


def _project_options_bytes(root: Path, requested: Mapping[str, Any] | None) -> bytes:
    """Return the one canonical options artifact public preparation will seal."""
    path = root / "project-options.json"
    if requested is None and path.is_file():
        payload = _read_json(path, "project options")
        if not isinstance(payload.get("options"), dict):
            raise PrepareCheckpointError("project-options.json must contain an options object")
        return path.read_bytes()
    payload: Mapping[str, Any] = requested if requested is not None else {
        "config_version": CONFIG_VERSION,
        "options": DEFAULT_OPTIONS,
    }
    if payload.get("config_version") != CONFIG_VERSION or not isinstance(payload.get("options"), dict):
        raise PrepareCheckpointError("resolved project options are invalid")
    serialized = (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if path.is_file() and path.read_bytes() != serialized:
        raise PrepareCheckpointError("resolved project options differ from project-options.json")
    return serialized


def _existing_state(root: Path, digest: dict[str, Any], candidate_sha256: str, agent: dict[str, str], external_audits: list[Path]) -> bool:
    record_path = root / "checkpoint-1.json"
    if not record_path.exists():
        return False
    try:
        raw = checkpoint._read_record(record_path)
        record = checkpoint._validate_record(raw)
        checkpoint._validate_record_runtime(record)
    except checkpoint.CheckpointError as exc:
        raise PrepareCheckpointError(f"existing checkpoint record is invalid: {exc}") from exc
    if _has_approval_field(record):
        raise PrepareCheckpointError("existing checkpoint record contains an approval field")
    status = record.get("status")
    if status in {"confirmed", "approved"}:
        raise PrepareCheckpointError("confirmed CKPT-1 cannot be prepared again")
    if status != "pending":
        if status != "pending_human_confirmation":
            raise PrepareCheckpointError("existing checkpoint must be pending or pending_human_confirmation")
        review_path, markdown_path, ledger_path, readiness_path, _ = _formal_paths(root)
        options = root / "project-options.json"
        if not options.is_file():
            return False
        expected = {root / "digest.json", options, review_path, markdown_path, ledger_path, readiness_path, *external_audits}
        actual = {Path(entry["path"]).resolve(strict=True) for entry in record["artifact_bundle"]}
        if actual != {path.resolve(strict=True) for path in expected}:
            raise PrepareCheckpointError("existing checkpoint formal artifact bundle is incomplete or does not bind current audits/**")
        if record.get("prepared_by") != agent:
            raise PrepareCheckpointError("existing checkpoint preparer identity does not match this candidate")
        bound_candidate = _read_json(review_path, "checkpoint-bound canonical review")
        try:
            bound_canonical = canonicalize_review_candidate(bound_candidate, digest)
        except ReviewCandidateError as exc:
            raise PrepareCheckpointError(f"checkpoint-bound canonical review is invalid: {exc}") from exc
        if record.get("candidate_sha256") != candidate_sha256 or review_semantic_identity(bound_canonical) != candidate_sha256:
            return False
        try:
            ckpt1_readiness.check_readiness(root, record_path, require_readiness_artifact=True)
        except ckpt1_readiness.ReadinessError as exc:
            raise PrepareCheckpointError(f"existing checkpoint readiness is invalid: {exc}") from exc
        return True
    return False


def _validate_dry_run(root: Path, digest_path: Path, agent: dict[str, str], review_bytes: bytes, markdown_bytes: bytes, ledger_bytes: bytes, options_bytes: bytes, candidate_sha256: str) -> None:
    """Run the real CKPT-1 construction/readiness path in a private clone, never in the project."""
    with tempfile.TemporaryDirectory(prefix="scholar-slides-prepare-") as raw_stage:
        stage = Path(raw_stage) / "project"
        shutil.copytree(root, stage, symlinks=True)
        review_path, markdown_path, ledger_path, readiness_path, record_path = _formal_paths(stage)
        _atomic_write(review_path, review_bytes)
        _atomic_write(markdown_path, markdown_bytes)
        _atomic_write(ledger_path, ledger_bytes)
        options = stage / "project-options.json"
        _atomic_write(options, options_bytes)
        attachments = [review_path, markdown_path, ledger_path, options, *_external_audits(stage)]
        checkpoint.create_checkpoint("CKPT-1", stage / digest_path.name, record_path, supplemental_artifacts=attachments, prepared_by=agent, candidate_sha256=candidate_sha256)
        readiness = ckpt1_readiness.check_readiness(stage, record_path)
        _atomic_write(readiness_path, (json.dumps(readiness, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        checkpoint.create_checkpoint("CKPT-1", stage / digest_path.name, record_path, supplemental_artifacts=[*attachments, readiness_path], prepared_by=agent, candidate_sha256=candidate_sha256)
        ckpt1_readiness.check_readiness(stage, record_path, require_readiness_artifact=True)


def prepare_checkpoint(
    project: str | Path,
    review_input: str | Path,
    *,
    checkpoint_name: str,
    prepared_by: str,
    dry_run: bool = False,
    project_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a bound pending-human-confirmation CKPT-1 artifact set."""
    if checkpoint_name != "CKPT-1":
        raise PrepareCheckpointError("prepare-checkpoint only supports CKPT-1")
    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise PrepareCheckpointError("--project must be an existing project directory")
    digest_path = root / "digest.json"
    input_path = Path(review_input).resolve(strict=True)
    digest = _read_json(digest_path, "digest")
    raw_candidate = _read_json(input_path, "review input")
    _canonical_ref(root, raw_candidate.get("source_digest"), digest_path)
    _attach_digest_semantics(raw_candidate, digest)
    try:
        resolve_canonical_source_pdf(root, digest, {"source_identity": dict(digest.get("source", {}))})
    except EvidenceAuditError as exc:
        raise PrepareCheckpointError(f"canonical source PDF is stale or invalid: {exc}") from exc
    raw_candidate["prepared_by"] = _agent(prepared_by)
    raw_candidate["prepared_at"] = datetime.now(timezone.utc).isoformat()
    try:
        candidate = canonicalize_review_candidate(raw_candidate, digest)
        ledger = build_marker_ledger(digest, candidate, _sha256(digest_path))
    except (ReviewCandidateError, MarkerPolicyError) as exc:
        raise PrepareCheckpointError(f"invalid review input: {exc}") from exc
    review_bytes = (json.dumps(candidate, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    # Re-running identical evidence has a new preparation timestamp, not a new human-review
    # candidate.  Use the canonical semantic identity so that resume is a true no-op.
    candidate_sha256 = review_semantic_identity(candidate)
    agent = _agent(prepared_by)
    external_audits = _external_audits(root)
    options_bytes = _project_options_bytes(root, project_options)
    if _existing_state(root, digest, candidate_sha256, agent, external_audits):
        write_paper_analysis(root)
        return {"ok": True, "changed": False, "checkpoint": "CKPT-1", "status": "pending_human_confirmation", "project": str(root)}
    markdown_bytes = project_review_markdown(candidate).encode("utf-8")
    ledger_bytes = (json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if dry_run:
        try:
            _validate_dry_run(root, digest_path, agent, review_bytes, markdown_bytes, ledger_bytes, options_bytes, candidate_sha256)
        except (checkpoint.CheckpointError, ckpt1_readiness.ReadinessError, OSError) as exc:
            raise PrepareCheckpointError(f"dry-run cannot prepare CKPT-1: {exc}") from exc
        return {"ok": True, "changed": True, "dry_run": True, "checkpoint": "CKPT-1", "status": "pending_human_confirmation", "project": str(root)}

    review_path, markdown_path, ledger_path, readiness_path, record_path = _formal_paths(root)
    options = root / "project-options.json"
    snapshot = _snapshot([options, review_path, markdown_path, ledger_path, readiness_path, record_path])
    temporary_record = root / ".checkpoint-1.prepare.json"
    try:
        _atomic_write(options, options_bytes)
        _atomic_write(review_path, review_bytes)
        _atomic_write(markdown_path, markdown_bytes)
        _atomic_write(ledger_path, ledger_bytes)
        attachments = [review_path, markdown_path, ledger_path, options, *external_audits]
        checkpoint.create_checkpoint("CKPT-1", digest_path, temporary_record, supplemental_artifacts=attachments, prepared_by=agent, candidate_sha256=candidate_sha256)
        readiness = ckpt1_readiness.check_readiness(root, temporary_record)
        readiness_bytes = (json.dumps(readiness, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _atomic_write(readiness_path, readiness_bytes)
        checkpoint.create_checkpoint("CKPT-1", digest_path, record_path, supplemental_artifacts=[*attachments, readiness_path], prepared_by=agent, candidate_sha256=candidate_sha256)
        ckpt1_readiness.check_readiness(root, record_path, require_readiness_artifact=True)
        write_paper_analysis(root)
    except (checkpoint.CheckpointError, ckpt1_readiness.ReadinessError) as exc:
        _restore(snapshot)
        raise PrepareCheckpointError(f"cannot prepare CKPT-1: {exc}") from exc
    except PrepareCheckpointError:
        _restore(snapshot)
        raise
    except BaseException:
        _restore(snapshot)
        raise
    finally:
        temporary_record.unlink(missing_ok=True)
    return {"ok": True, "changed": True, "checkpoint": "CKPT-1", "status": "pending_human_confirmation", "project": str(root)}
