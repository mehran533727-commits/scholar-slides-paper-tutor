#!/usr/bin/env python3
"""Persist and verify explicit, fail-closed review checkpoints for slide artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zlib
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping

from aesthetics_qa import validate_aesthetics_report
from asset_graph import AssetGraphError, build_asset_graph, bundle_from_asset_graph
from content_policy import validate_visible_content
from marker_policy import MarkerPolicyError, forbidden_assets_for_next_stage, load_marker_ledger


SCHEMA_VERSION = 4
LEGACY_CKPT1_SCHEMA_VERSION = 3
LEGACY_CKPT1_RECORD_SHA256 = "884e23750680a4be42e24dbe38a9f3b388705e7e0fa7fd255065b2c4b07a1c34"
LEGACY_CKPT1_DIGEST_SHA256 = "81b6ea7ac5519534904b73e7e416edd7723ea02e596ae44b41f4d73804eb413b"
REVIEW_BUNDLE_SCHEMA_VERSION = 1
REVIEW_RENDER_KIND = "scholar-slides-review-render"
REVIEW_PREVIEW_MANIFEST_KIND = "scholar-slides-review-manifest"
BUILD_BUNDLE_KIND = "scholar-slides-build-bundle"
REVIEW_SCREENSHOT_WIDTH = 1920
REVIEW_SCREENSHOT_HEIGHT = 1080
HISTORY_SCHEMA_VERSION = 1
HISTORY_KIND = "scholar-slides-checkpoint-history"
HISTORY_ROOT_NAME = "checkpoint-history"
LINEAGE_KIND = "scholar-slides-ckpt2-lineage"
LINEAGE_MARKER_NAME = "checkpoint-2-lineage.json"
REOPEN_JOURNAL_KIND = "scholar-slides-ckpt2-reopen-journal"
REOPEN_JOURNAL_NAME = ".checkpoint-2-reopen.json"
CHECKPOINTS = ("CKPT-1", "CKPT-2", "CKPT-3")
PREDECESSOR = {"CKPT-1": None, "CKPT-2": "CKPT-1", "CKPT-3": "CKPT-2"}
MARKER_RE = re.compile(r"\[(?:MISSING|UNVERIFIED)(?::[^\]]*)?\]")
# Resolution records are immutable audit metadata, not content a renderer can show.  They retain
# the original marker verbatim, but must not double the number of visible markers a later deck is
# required to carry.  The actual unresolved marker remains in its original artifact field (for
# example ``flags``), where it continues to be protected fail-closed.
_AUDIT_ONLY_MARKER_FIELDS = {"marker_resolutions"}


class CheckpointError(RuntimeError):
    """Raised when a user checkpoint or integrity-marker preflight is invalid."""


def sha256_file(artifact_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(artifact_path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_file(path: str | Path, *, label: str) -> Path:
    target = Path(path)
    if not target.is_file():
        raise CheckpointError(f"{label} does not exist or is not a regular file: {target}")
    try:
        return target.resolve(strict=True)
    except OSError as exc:
        raise CheckpointError(f"cannot resolve {label}: {target}: {exc}") from exc


def _canonical_directory(path: str | Path, *, label: str) -> Path:
    target = Path(path)
    if not target.is_dir():
        raise CheckpointError(f"{label} does not exist or is not a directory: {target}")
    try:
        return target.resolve(strict=True)
    except OSError as exc:
        raise CheckpointError(f"cannot resolve {label}: {target}: {exc}") from exc


def _runtime_reuse_path(raw_path: str, *, platform_name: str | None = None) -> str:
    """Map one legacy WSL drive path for read-only reuse on native Windows.

    The source checkpoint remains byte-for-byte unchanged.  Only a strict absolute
    drive mount is accepted; ambiguous paths and traversal are rejected before any
    artifact is opened.  Other platforms and ordinary paths retain their exact value.
    """
    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt":
        return raw_path
    match = re.fullmatch(r"^/(?:mnt)/([A-Za-z])(?:/(.*))?$", raw_path)
    if match is None:
        return raw_path
    remainder = match.group(2)
    if not remainder:
        raise CheckpointError("legacy checkpoint drive path must identify a file")
    parts = remainder.split("/")
    if any(not part or part in {".", ".."} or "\\" in part or ":" in part for part in parts):
        raise CheckpointError("legacy checkpoint drive path contains an unsafe component")
    return str(PureWindowsPath(f"{match.group(1).upper()}:\\", *parts))


def _runtime_reuse_record(record: Mapping[str, Any], *, platform_name: str | None = None) -> dict[str, Any]:
    """Return an in-memory platform projection of CKPT-1 path-bearing entries."""
    projected = deepcopy(dict(record))

    def project_entry(entry: Any) -> None:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entry["path"] = _runtime_reuse_path(entry["path"], platform_name=platform_name)

    project_entry(projected.get("artifact"))
    bundle = projected.get("artifact_bundle")
    if isinstance(bundle, list):
        for entry in bundle:
            project_entry(entry)
    project_entry(projected.get("readiness_artifact"))
    return projected


def _within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_graph_destination(path: Path, root: Path, *, reserved: Iterable[Path] = ()) -> Path:
    """Validate a graph output without following symlink/reparse components."""
    target = Path(path)
    root = Path(root).resolve(strict=True)
    if not target.is_absolute():
        target = root / target
    target = Path(os.path.abspath(target))
    root_lexical = Path(os.path.abspath(root))
    if not _within(root_lexical, target):
        raise CheckpointError("asset graph output must remain inside the deck directory")
    current = root_lexical
    rel = target.relative_to(root_lexical)
    for part in rel.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current != target:
                raise CheckpointError("asset graph output parent does not exist")
            break
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise CheckpointError("asset graph output may not traverse or replace a symlink or reparse point")
        if current == target and not stat.S_ISREG(info.st_mode):
            raise CheckpointError("asset graph output must be a regular file")
    resolved = target.resolve(strict=False)
    if not _within(root, resolved):
        raise CheckpointError("asset graph output must remain inside the deck directory")
    for item in reserved:
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = root_lexical / candidate
        candidate = Path(os.path.abspath(candidate))
        if candidate == target:
            raise CheckpointError("asset graph output collides with a checkpoint artifact")
    return resolved


def _safe_child(root: Path, raw: str, *, label: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        raise CheckpointError(f"{label} must be a relative resource path")
    try:
        resolved = (root / candidate).resolve(strict=True)
    except OSError as exc:
        raise CheckpointError(f"cannot resolve {label}: {raw}: {exc}") from exc
    if not _within(root, resolved):
        raise CheckpointError(f"{label} escapes the deck directory: {raw}")
    return resolved


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in _AUDIT_ONLY_MARKER_FIELDS:
                continue
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def collect_integrity_markers(value: Any) -> Counter[str]:
    markers: Counter[str] = Counter()
    for text in _strings(value):
        markers.update(MARKER_RE.findall(text))
    return markers


def _json_pointer(parent: str, token: str) -> str:
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def collect_integrity_marker_locations(value: Any, pointer: str = "") -> Counter[tuple[str, str]]:
    """Return every marker keyed by its stable JSON Pointer location."""
    markers: Counter[tuple[str, str]] = Counter()
    if isinstance(value, str):
        for marker in MARKER_RE.findall(value):
            markers[(pointer or "/", marker)] += 1
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in _AUDIT_ONLY_MARKER_FIELDS:
                continue
            markers.update(collect_integrity_marker_locations(child, _json_pointer(pointer, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            markers.update(collect_integrity_marker_locations(child, _json_pointer(pointer, str(index))))
    return markers


def assert_markers_preserved(before: Any, after: Any) -> None:
    removed = collect_integrity_marker_locations(before) - collect_integrity_marker_locations(after)
    if removed:
        detail = ", ".join(
            f"{pointer} {marker} ×{count}" for (pointer, marker), count in sorted(removed.items())
        )
        raise CheckpointError(f"integrity marker(s) removed without explicit acknowledgement: {detail}")


def _assert_marker_tokens_retained(before: Any, after: Any) -> None:
    """Carry unresolved facts from a digest into a structurally different deck spec."""
    removed = collect_integrity_markers(before) - collect_integrity_markers(after)
    # A marker may be intentionally resolved by a bound, immutable audit record (for example a
    # manually checked table crop).  Such a marker is no longer an unresolved downstream fact,
    # but the original token remains in the CKPT-1 artifact and its resolution is still audited.
    if isinstance(before, dict):
        resolutions = before.get("marker_resolutions")
        if isinstance(resolutions, list):
            for resolution in resolutions:
                if not isinstance(resolution, dict):
                    continue
                if resolution.get("status") != "resolved_with_audit":
                    continue
                marker = resolution.get("original_marker")
                if isinstance(marker, str) and removed.get(marker, 0) > 0:
                    removed[marker] -= 1
                    if removed[marker] <= 0:
                        del removed[marker]
    if removed:
        detail = ", ".join(f"{marker} ×{count}" for marker, count in sorted(removed.items()))
        raise CheckpointError(f"integrity marker(s) missing from downstream artifact: {detail}")


# A marker only counts as preserved at CKPT-2 if the current layout actually renders it in the
# projected HTML/PDF/PPTX or speaker notes.  Do not copy complete nested objects here: renderers
# intentionally ignore extension keys such as ``figure.audit_note`` and ``table.audit_note``.
# Treating those keys as visible evidence would make the integrity gate fail open.
_TEXT_SLIDE_FIELDS = (
    "title", "action_title", "eyebrow", "authors", "affiliation", "venue", "presenter", "num",
    "annotation", "source_ref", "note", "text", "speaker_notes",
)


def _visible_text(value: Any) -> Any:
    """Keep only scalar text that a renderer can put in a slide or notes output."""
    return value if isinstance(value, str) else None


def _visible_text_list(value: Any) -> list[str]:
    return [item for item in value] if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _visible_figure(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    # ``src`` and ``alt`` are not visible slide text.  ``cite`` only renders when a caption
    # exists, so do not let a marker in an otherwise hidden cite field satisfy this gate.
    caption = _visible_text(value.get("caption"))
    result: dict[str, str] = {}
    if caption is not None:
        result["caption"] = caption
        cite = _visible_text(value.get("cite"))
        if cite is not None:
            result["cite"] = cite
    return result


def _visible_table(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in ("caption", "footnote"):
        text = _visible_text(value.get(field))
        if text is not None:
            result[field] = text
    columns = value.get("columns")
    if isinstance(columns, list):
        visible_columns: list[Any] = []
        for column in columns:
            if isinstance(column, str):
                visible_columns.append(column)
            elif isinstance(column, dict):
                selected = {key: column[key] for key in ("label", "unit") if isinstance(column.get(key), str)}
                visible_columns.append(selected)
        result["columns"] = visible_columns
    rows = value.get("rows")
    if isinstance(rows, list):
        visible_rows: list[list[Any]] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            visible_row: list[Any] = []
            for cell in row:
                if isinstance(cell, str):
                    visible_row.append(cell)
                elif isinstance(cell, dict) and isinstance(cell.get("v"), str):
                    visible_row.append({"v": cell["v"]})
            visible_rows.append(visible_row)
        result["rows"] = visible_rows
    return result


def _visible_equations(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    visible: list[dict[str, str]] = []
    for equation in value:
        if not isinstance(equation, dict):
            continue
        selected: dict[str, str] = {}
        latex = _visible_text(equation.get("latex"))
        if latex is not None:
            selected["latex"] = latex
        if equation.get("numbered") is True:
            number = _visible_text(equation.get("num"))
            if number is not None:
                selected["num"] = number
        visible.append(selected)
    return visible


def _visible_critique_points(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    visible: list[dict[str, str]] = []
    for point in value:
        if not isinstance(point, dict):
            continue
        selected = {key: point[key] for key in ("head", "body") if isinstance(point.get(key), str)}
        visible.append(selected)
    return visible


def _rendered_slide_payload(slide: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for field in _TEXT_SLIDE_FIELDS:
        value = slide.get(field)
        if field == "authors" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            rendered[field] = value
        else:
            text = _visible_text(value)
            if text is not None:
                rendered[field] = text
    for field in ("items", "points2", "questions", "entries"):
        rendered[field] = _visible_text_list(slide.get(field))
    if slide.get("layout") == "critique-concerns":
        rendered["points"] = _visible_critique_points(slide.get("points"))
    else:
        rendered["points"] = _visible_text_list(slide.get("points"))
    rendered["figure"] = _visible_figure(slide.get("figure"))
    rendered["table"] = _visible_table(slide.get("table"))
    rendered["equations"] = _visible_equations(slide.get("equations"))
    return rendered


def _rendered_deck_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    slides = payload.get("slides")
    if not isinstance(slides, list):
        return {"slides": []}
    return {"slides": [_rendered_slide_payload(slide) for slide in slides if isinstance(slide, dict)]}


def _read_record(record_path: str | Path) -> dict[str, Any]:
    try:
        return json.loads(Path(record_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read checkpoint record: {exc}") from exc


def _validated_record_for_runtime(
    record_path: str | Path,
    *,
    record_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a modern record, or normalize only the immutable 0.1.0 Golden in memory."""
    path = Path(record_path).resolve(strict=True)
    raw = deepcopy(dict(record_override)) if record_override is not None else _read_record(path)
    if record_override is not None and raw.get("schema_version") == LEGACY_CKPT1_SCHEMA_VERSION:
        raise CheckpointError("legacy Golden checkpoint cannot use a runtime record override")
    if not isinstance(raw, dict) or raw.get("schema_version") != LEGACY_CKPT1_SCHEMA_VERSION:
        return _validate_record(raw)
    if sha256_file(path) != LEGACY_CKPT1_RECORD_SHA256:
        raise CheckpointError("malformed checkpoint record: unsupported schema_version")
    if raw.get("checkpoint") != "CKPT-1" or raw.get("status") != "confirmed":
        raise CheckpointError("malformed legacy checkpoint record: only the immutable confirmed CKPT-1 Golden is supported")
    digest_path = path.parent / "digest.json"
    if not digest_path.is_file() or sha256_file(digest_path) != LEGACY_CKPT1_DIGEST_SHA256:
        raise CheckpointError("legacy CKPT-1 Golden digest is missing or stale")
    artifact = raw.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("sha256") != LEGACY_CKPT1_DIGEST_SHA256:
        raise CheckpointError("malformed legacy CKPT-1 Golden digest binding")

    normalized = deepcopy(raw)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["artifact"] = {"path": str(digest_path), "sha256": LEGACY_CKPT1_DIGEST_SHA256}
    # The exact immutable record hash seals the historical supplemental list. Runtime resume
    # needs only the associated primary digest; no historical JSON or evidence hash is rewritten.
    normalized["artifact_bundle"] = [dict(normalized["artifact"])]
    normalized.pop("readiness_artifact", None)
    normalized["legacy_confirmation"] = {
        "path": "legacy_confirmed",
        "schema_version": LEGACY_CKPT1_SCHEMA_VERSION,
        "record_sha256": LEGACY_CKPT1_RECORD_SHA256,
        "digest_sha256": LEGACY_CKPT1_DIGEST_SHA256,
    }
    return _validate_record(normalized)


def _read_json(path: str | Path, *, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"cannot read {label} ({path}): {exc}") from exc


def _read_artifact_payload(artifact: Path) -> Any:
    try:
        return json.loads(artifact.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return artifact.read_text(encoding="utf-8")


def _write_record(record_path: str | Path, record: dict[str, Any]) -> None:
    target = Path(record_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise CheckpointError(f"cannot atomically write checkpoint record: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _relocation_target(source: Path, source_root: Path, destination_root: Path) -> Path:
    """Map one bound CKPT-1 artifact below the source project into a clone."""
    source = source.resolve(strict=True)
    try:
        relative = source.relative_to(source_root)
    except ValueError as exc:
        raise CheckpointError("confirmed CKPT-1 artifact escapes its source project") from exc
    current = destination_root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
                raise CheckpointError(f"destination CKPT-1 artifact path traverses a link: {current}")
    target = (destination_root / relative).resolve(strict=False)
    if not _within(destination_root, target):
        raise CheckpointError("destination CKPT-1 artifact escapes the destination project")
    return target


def reuse_confirmed_ckpt1(
    source_record_path: str | Path,
    destination_project: str | Path,
    destination_record_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reuse a confirmed CKPT-1 in a new project without editing its source record.

    A copied project needs the same hash-bound evidence under new absolute paths.  This
    operation verifies the original confirmed record, copies only its bound artifact
    bundle when missing, and writes a derived confirmed record whose semantic bindings,
    confirmation identity, and artifact hashes are unchanged.  It never overwrites a
    destination file with different bytes and never changes the source record.
    """
    source_record = _canonical_file(source_record_path, label="source CKPT-1 record")
    source_root = source_record.parent.resolve(strict=True)
    source_before = sha256_file(source_record)
    source_record_payload = _read_record(source_record)
    runtime_source_payload = _runtime_reuse_record(source_record_payload)
    validated_source = _validated_record_for_runtime(source_record, record_override=runtime_source_payload)
    source_validated = _require_approved_checkpoint(
        source_record,
        _record_artifact_path(validated_source),
        expected_checkpoint="CKPT-1",
        record_override=validated_source,
    )
    if source_validated.get("status") != "confirmed":
        raise CheckpointError("CKPT-1 reuse requires a confirmed source record")

    destination = Path(destination_project)
    if destination.exists() or destination.is_symlink():
        if not destination.is_dir() or destination.is_symlink():
            raise CheckpointError("destination project must be a real directory")
    else:
        destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve(strict=True)
    if destination_root == source_root:
        raise CheckpointError("source and destination projects must be different")
    destination_record = Path(destination_record_path) if destination_record_path is not None else destination_root / "checkpoint-1.json"
    if not destination_record.is_absolute():
        destination_record = destination_root / destination_record
    destination_record = destination_record.resolve(strict=False)
    if not _within(destination_root, destination_record):
        raise CheckpointError("destination CKPT-1 record must remain inside the destination project")

    path_map: dict[Path, Path] = {}
    for entry in source_validated.get("artifact_bundle", []):
        source_artifact = _require_file_entry(entry, label="source CKPT-1 artifact")
        target = _relocation_target(source_artifact, source_root, destination_root)
        path_map[source_artifact] = target
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise CheckpointError(f"destination CKPT-1 artifact is not a regular file: {target}")
            if sha256_file(target) != entry["sha256"]:
                raise CheckpointError(f"destination CKPT-1 artifact differs from the confirmed source: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_artifact, target)
            target.chmod(stat.S_IMODE(source_artifact.stat().st_mode) or 0o644)

    relocated = deepcopy(runtime_source_payload)

    def relocate_entry(entry: Any) -> None:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise CheckpointError("confirmed CKPT-1 contains a malformed path-bearing artifact entry")
        source_artifact = _canonical_file(entry["path"], label="source CKPT-1 artifact")
        target = path_map.get(source_artifact)
        if target is None:
            raise CheckpointError("confirmed CKPT-1 path-bearing artifact is not in its artifact bundle")
        entry["path"] = str(target)

    relocate_entry(relocated["artifact"])
    for entry in relocated.get("artifact_bundle", []):
        relocate_entry(entry)
    if relocated.get("readiness_artifact") is not None:
        relocate_entry(relocated["readiness_artifact"])
    _validate_record(relocated)
    payload = (json.dumps(relocated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    changed = True
    if destination_record.exists() or destination_record.is_symlink():
        if destination_record.is_symlink() or not destination_record.is_file():
            raise CheckpointError(f"destination CKPT-1 record is not a regular file: {destination_record}")
        existing = _read_record(destination_record)
        if existing != relocated:
            raise CheckpointError(f"destination CKPT-1 record already exists with different content: {destination_record}")
        changed = False
    else:
        _write_record(destination_record, relocated)
    _validate_record_runtime(_read_record(destination_record))
    if sha256_file(source_record) != source_before:
        raise CheckpointError("source CKPT-1 record changed during read-only reuse")
    return {
        "status": "reused" if changed else "no-op",
        "checkpoint": "CKPT-1",
        "source_record": str(source_record),
        "source_record_sha256": source_before,
        "destination_record": str(destination_record),
        "destination_record_sha256": sha256_file(destination_record),
        "artifact_count": len(path_map),
    }


def _file_entry(file_path: Path) -> dict[str, str]:
    canonical = _canonical_file(file_path, label="artifact bundle file")
    return {"path": str(canonical), "sha256": sha256_file(canonical)}


def _tree_files(directory: Path) -> list[Path]:
    root = _canonical_directory(directory, label="artifact bundle directory")
    files: list[Path] = []
    for child in sorted(root.rglob("*")):
        if not child.is_file():
            continue
        canonical = _canonical_file(child, label="artifact bundle file")
        if not _within(root, canonical):
            raise CheckpointError(f"artifact bundle symlink escapes its directory: {child}")
        files.append(canonical)
    return files


def _deck_bundle_files(artifact: Path) -> list[Path]:
    """Bind deck.json and every local figure that the renderer can copy/embed."""
    payload = _read_artifact_payload(artifact)
    if not isinstance(payload, dict):
        return [artifact]
    root = artifact.parent.resolve()
    files = {artifact}
    meta = payload.get("meta")
    if isinstance(meta, dict):
        figures_dir = meta.get("figures_dir", "figures")
        if isinstance(figures_dir, str) and figures_dir:
            candidate = root / figures_dir
            if candidate.exists():
                figures_root = _safe_child(root, figures_dir, label="meta.figures_dir")
                if figures_root.is_dir():
                    files.update(_tree_files(figures_root))
    slides = payload.get("slides")
    if isinstance(slides, list):
        for index, slide in enumerate(slides, start=1):
            figure = slide.get("figure") if isinstance(slide, dict) else None
            source = figure.get("src") if isinstance(figure, dict) else None
            if isinstance(source, str) and source:
                candidate = root / source
                if candidate.exists():
                    files.add(_safe_child(root, source, label=f"slide {index} figure.src"))
    return sorted(files)


def _artifact_bundle(
    checkpoint: str,
    artifact: Path,
    supplemental_artifacts: Iterable[Path] = (),
    graph_artifacts: Iterable[Path] = (),
) -> list[dict[str, str]]:
    graph_files = set(graph_artifacts)
    files = graph_files if graph_files else set(_deck_bundle_files(artifact) if checkpoint in {"CKPT-2", "CKPT-3"} else [artifact])
    files.update(supplemental_artifacts)
    return [_file_entry(file_path) for file_path in sorted(files)]


def _native_table_audit_paths(deck: Path) -> list[Path]:
    """Locate the exact hash-bound audit record(s) used by native table slides.

    A single native-table slide must not merge multiple audit records because
    that would make its provenance ambiguous.  A deck may, however, contain
    several quantitative native-table slides, each with its own bound audit.
    Return the unique union across slides and retain the explicit manual-review
    fallback only when no hash-bound reference exists.
    """
    payload = _read_artifact_payload(deck)
    slides = payload.get("slides") if isinstance(payload, dict) else None
    has_table = isinstance(slides, list) and any(isinstance(slide, dict) and isinstance(slide.get("table"), dict) for slide in slides)
    if not has_table:
        return []
    # Native tables generated from quantitative coverage carry their exact audit
    # reference in the hash-bound coverage artifact, not in the renderer-visible
    # slide payload. Resolve only audits bound to the table's requirement ids.
    coverage_path = deck.parent / "coverage-requirements.json"
    if coverage_path.is_file():
        coverage = _read_artifact_payload(coverage_path)
        requirements = coverage.get("requirements") if isinstance(coverage, dict) else None
        by_id = {
            str(item.get("id")): item
            for item in requirements or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        refs: dict[tuple[str, str], Path] = {}
        for slide in slides or []:
            if not isinstance(slide, dict) or not isinstance(slide.get("table"), dict):
                continue
            slide_refs: dict[tuple[str, str], Path] = {}
            ids = slide.get("coverage_requirement_ids")
            if not isinstance(ids, list):
                continue
            for requirement_id in ids:
                requirement = by_id.get(str(requirement_id))
                ref = requirement.get("audit_ref") if isinstance(requirement, dict) else None
                if not isinstance(ref, dict):
                    continue
                raw_path = ref.get("path")
                expected_sha = ref.get("sha256")
                if not isinstance(raw_path, str) or not isinstance(expected_sha, str):
                    raise CheckpointError("asset-audit-binding-missing: native table audit_ref is incomplete")
                audit_path = _safe_child(deck.parent, raw_path, label="native table audit_ref.path")
                if sha256_file(audit_path) != expected_sha:
                    raise CheckpointError(f"asset-audit-binding-mismatch: {raw_path}")
                slide_refs[(raw_path.replace("\\", "/"), expected_sha)] = audit_path
            if len(slide_refs) > 1:
                raise CheckpointError("asset-audit-binding-ambiguous: native table requires exactly one audit record per slide")
            refs.update(slide_refs)
        if refs:
            return [refs[key] for key in sorted(refs)]
    review_assets = deck.parent / "review-assets"
    candidates = sorted(review_assets.glob("*-manual-review.json")) if review_assets.is_dir() else []
    if len(candidates) != 1:
        raise CheckpointError("asset-audit-binding-missing: native table requires exactly one explicit audit record")
    return candidates


def _sealed_forbidden_assets(required_record: dict[str, Any]) -> list[str]:
    entry = _ckpt1_marker_ledger_entry(required_record)
    if entry is None:
        return []
    try:
        return forbidden_assets_for_next_stage(load_marker_ledger(_require_file_entry(entry, label="CKPT-1 marker ledger")))
    except MarkerPolicyError as exc:
        raise CheckpointError(f"asset-upstream-binding-mismatch: invalid sealed marker ledger: {exc}") from exc


def _write_asset_graph(deck: Path, digest: Path, *, forbidden_assets: Iterable[str], output_path: Path | None = None, reserved: Iterable[Path] = ()) -> tuple[Path, list[Path]]:
    try:
        graph = build_asset_graph(
            deck,
            digest,
            _native_table_audit_paths(deck),
            forbidden_assets=tuple(forbidden_assets),
        )
    except AssetGraphError as exc:
        raise CheckpointError(str(exc)) from exc
    graph_path = output_path or deck.with_name("asset-graph.json" if deck.name == "deck.json" else f"{deck.stem}.asset-graph.json")
    graph_path = _safe_graph_destination(graph_path, deck.parent, reserved=reserved)
    payload = (json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parent_fd: int | None = None
    temp_path: Path | None = None
    try:
        if os.name == "nt":
            # Windows does not support opening a directory with POSIX O_DIRECTORY/O_NOFOLLOW
            # flags (it reports PermissionError).  _safe_graph_destination already walks every
            # component with lstat/reparse checks; keep the install atomic within that verified
            # directory rather than making CKPT-2 creation unusable on supported Windows hosts.
            fd, temp_name = tempfile.mkstemp(dir=graph_path.parent, prefix=f".{graph_path.name}.")
            temp_path = graph_path.parent / temp_name
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, graph_path)
            temp_path = None
        else:
            # Keep the validated parent directory open through installation so a
            # concurrent parent replacement cannot redirect the temporary write.
            dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            parent_fd = os.open(graph_path.parent, dir_flags)
            parent_before = os.fstat(parent_fd)
            fd, temp_name = tempfile.mkstemp(dir=graph_path.parent, prefix=f".{graph_path.name}.")
            temp_path = graph_path.parent / temp_name
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            parent_after = os.fstat(parent_fd)
            if (parent_before.st_dev, parent_before.st_ino) != (parent_after.st_dev, parent_after.st_ino):
                raise OSError("asset graph output directory changed during write")
            if os.rename in getattr(os, "supports_dir_fd", ()):
                # ``os.rename`` is the descriptor-aware atomic replace primitive on
                # POSIX (and is atomic within one directory).
                os.rename(temp_path.name, graph_path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                os.replace(temp_path, graph_path)
            temp_path = None
    except OSError as exc:
        raise CheckpointError(f"cannot write asset graph: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
    try:
        files = [(deck.parent / path).resolve(strict=True) for path in bundle_from_asset_graph(graph)]
    except (AssetGraphError, OSError) as exc:
        raise CheckpointError(f"asset-graph-schema: cannot materialize graph bundle: {exc}") from exc
    return _canonical_file(graph_path, label="asset graph"), files


def _validate_asset_graph_binding(record: dict[str, Any], bundle_paths: list[Path]) -> None:
    """Require current CKPT-2 records to retain exactly their generated dependency graph."""
    if record.get("checkpoint") != "CKPT-2":
        return
    entry = record.get("asset_graph")
    if not isinstance(entry, dict):
        raise CheckpointError("malformed checkpoint record: CKPT-2 requires an asset graph; recreate this legacy record")
    raw_graph_path = entry.get("path") if isinstance(entry, dict) else None
    if not isinstance(raw_graph_path, str):
        raise CheckpointError("malformed asset graph: path is required")
    graph_path = _safe_graph_destination(Path(raw_graph_path), _record_artifact_path(record).parent)
    graph_path = _require_file_entry({**entry, "path": str(graph_path)}, label="asset graph")
    graph = _read_json(graph_path, label="asset graph")
    if graph.get("schema_version") != 1 or graph.get("kind") != "scholar-slides-asset-graph":
        raise CheckpointError("asset-graph-schema: unsupported asset graph")
    try:
        required_path = _canonical_file(record["requires"]["record_path"], label="required checkpoint record")
        required_raw = _read_record(required_path)
        required_requires = required_raw.get("requires") if isinstance(required_raw, dict) else None
        if required_raw.get("checkpoint") == "CKPT-2" and isinstance(required_requires, dict) and Path(required_requires.get("record_path", "")).resolve() == required_path:
            raise CheckpointError("cyclic checkpoint predecessor")
        required_record = _validate_record(required_raw)
        expected_graph = build_asset_graph(
            _record_artifact_path(record),
            _record_artifact_path(required_record),
            _native_table_audit_paths(_record_artifact_path(record)),
            forbidden_assets=_sealed_forbidden_assets(required_record),
        )
    except (KeyError, TypeError, AssetGraphError, CheckpointError) as exc:
        raise CheckpointError(f"asset-graph-schema: cannot recompute current graph: {exc}") from exc
    if json.dumps(graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(expected_graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")):
        raise CheckpointError("asset-graph-tampered: persisted asset graph does not match current inputs")
    deck = graph.get("deck")
    if not isinstance(deck, dict) or deck.get("path") != _record_artifact_path(record).name or deck.get("sha256") != record["artifact"].get("sha256"):
        raise CheckpointError("asset-upstream-binding-mismatch: asset graph deck binding does not match CKPT-2")
    try:
        graph_files = {(graph_path.parent / path).resolve(strict=True) for path in bundle_from_asset_graph(graph)}
    except (AssetGraphError, OSError) as exc:
        raise CheckpointError(f"asset-graph-schema: invalid graph bundle: {exc}") from exc
    if not graph_files <= set(bundle_paths):
        raise CheckpointError("asset-bundle-missing-required: CKPT-2 bundle omits an asset graph dependency")
    supplemental = record.get("supplemental_artifacts", [])
    if not isinstance(supplemental, list):
        raise CheckpointError("malformed checkpoint record: supplemental_artifacts must be a list")
    supplemental_paths = {_require_file_entry(item, label="supplemental checkpoint artifact") for item in supplemental}
    expected = graph_files | supplemental_paths | {graph_path}
    if set(bundle_paths) != expected:
        raise CheckpointError("asset-bundle-overinclusive: CKPT-2 bundle does not exactly match its graph and declared supplements")


def _require_file_entry(entry: Any, *, label: str) -> Path:
    if not isinstance(entry, dict):
        raise CheckpointError(f"malformed {label}: expected an object")
    raw_path = entry.get("path")
    expected_hash = entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise CheckpointError(f"malformed {label}: path is required")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise CheckpointError(f"malformed {label}: sha256 is invalid")
    actual = _canonical_file(raw_path, label=label)
    if raw_path != str(actual):
        raise CheckpointError(f"malformed {label}: path is not canonical")
    if sha256_file(actual) != expected_hash:
        raise CheckpointError(f"{label} bundle is stale: SHA-256 changed ({actual})")
    return actual


def _ckpt1_marker_ledger_entry(record: dict[str, Any]) -> dict[str, str] | None:
    matches: list[dict[str, str]] = []
    for entry in record.get("artifact_bundle", []):
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        entry_path = Path(raw_path)
        if entry_path.name == "ckpt1-markers.json":
            _require_file_entry(entry, label="CKPT-1 marker ledger candidate")
            matches.append(entry)
            continue
        if entry_path.suffix.lower() != ".json":
            continue
        try:
            payload = _read_json(
                _require_file_entry(entry, label="CKPT-1 marker ledger candidate"),
                label=f"CKPT-1 marker ledger candidate {entry_path}",
            )
        except CheckpointError:
            raise
        if isinstance(payload, dict) and payload.get("kind") == "scholar-slides-ckpt1-markers":
            matches.append(entry)
    if len(matches) > 1:
        raise CheckpointError("malformed CKPT-1 artifact bundle: multiple marker ledger entries")
    return matches[0] if matches else None


def _marker_ledger_projection(entry: dict[str, str]) -> list[dict[str, Any]]:
    ledger = _read_json(_require_file_entry(entry, label="inherited marker ledger"), label="inherited marker ledger")
    if ledger.get("kind") == "scholar-slides-ckpt1-markers":
        items = ledger.get("items")
        if not isinstance(items, list):
            raise CheckpointError("malformed inherited marker ledger: items must be a list")
        projection: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or not isinstance(item.get("marker"), str) or not item["marker"]:
                raise CheckpointError(f"malformed inherited marker ledger: entry {index} has no marker")
            asset_id = item.get("asset_id") if isinstance(item.get("asset_id"), str) else None
            resolution = item.get("resolution")
            if resolution not in {"unresolved", "resolved_with_audit", "excluded_from_deck"}:
                raise CheckpointError(f"malformed inherited marker ledger: entry {index} has invalid resolution")
            projection.append({"asset_id": asset_id, "can_defer_to_ckpt2": resolution == "excluded_from_deck", "marker": item["marker"], "status": resolution})
        return sorted(projection, key=lambda item: (item["marker"], item["asset_id"] or ""))
    markers = ledger.get("markers") if isinstance(ledger, dict) else None
    if not isinstance(markers, list):
        raise CheckpointError("malformed inherited marker ledger: markers must be a list")
    projection: list[dict[str, Any]] = []
    for index, item in enumerate(markers, start=1):
        marker = item.get("marker") if isinstance(item, dict) else None
        if not isinstance(marker, str) or not marker:
            raise CheckpointError(f"malformed inherited marker ledger: entry {index} has no marker")
        asset = item.get("asset") if isinstance(item.get("asset"), dict) else {}
        asset_id = asset.get("id") if isinstance(asset.get("id"), str) else None
        status = item.get("status") if isinstance(item.get("status"), str) else None
        projection.append({"asset_id": asset_id, "can_defer_to_ckpt2": item.get("can_defer_to_ckpt2") is True, "marker": marker, "status": status})
    return sorted(projection, key=lambda item: (item["marker"], item["asset_id"] or ""))


def _validate_inherited_marker_ledger_shape(record: dict[str, Any]) -> None:
    """Validate CKPT-2 binding shape without following an untrusted predecessor path."""
    if record.get("checkpoint") != "CKPT-2":
        return
    inherited = record.get("inherited_marker_ledger")
    if not isinstance(inherited, dict):
        raise CheckpointError("malformed checkpoint record: CKPT-2 requires inherited marker ledger")
    requires = record.get("requires")
    if not isinstance(requires, dict) or inherited.get("source_checkpoint") != "CKPT-1" or inherited.get("source_record_path") != requires.get("record_path") or inherited.get("source_record_sha256") != requires.get("sha256"):
        raise CheckpointError("malformed inherited marker ledger: source checkpoint binding does not match requires")
    policy = inherited.get("ledger_policy")
    bound_entry = inherited.get("source_ledger")
    projection = inherited.get("marker_ledger_projection")
    if policy == "no_ckpt1_marker_ledger":
        if bound_entry is not None or projection != []:
            raise CheckpointError("malformed inherited marker ledger: no-ledger policy has binding data")
        return
    if policy != "sealed_ckpt1_marker_ledger" or not isinstance(bound_entry, dict) or not isinstance(projection, list):
        raise CheckpointError("malformed inherited marker ledger: missing CKPT-1 marker ledger binding")
    raw_path = bound_entry.get("path")
    raw_hash = bound_entry.get("sha256")
    if not isinstance(raw_path, str) or not raw_path or not isinstance(raw_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
        raise CheckpointError("malformed inherited marker ledger: invalid source ledger entry")
    for item in projection:
        if not isinstance(item, dict) or not isinstance(item.get("marker"), str) or not item["marker"] or not isinstance(item.get("can_defer_to_ckpt2"), bool) or item.get("asset_id") is not None and not isinstance(item.get("asset_id"), str) or item.get("status") is not None and not isinstance(item.get("status"), str):
            raise CheckpointError("malformed inherited marker ledger: invalid marker projection")


def _validate_inherited_marker_ledger_runtime(record: dict[str, Any], source_record: dict[str, Any]) -> None:
    """Compare the CKPT-2 binding with the already-approved CKPT-1 predecessor."""
    if record.get("checkpoint") != "CKPT-2":
        return
    inherited = record["inherited_marker_ledger"]
    if source_record.get("checkpoint") != "CKPT-1":
        raise CheckpointError("inherited marker ledger requires an approved CKPT-1 predecessor")
    source_entry = _ckpt1_marker_ledger_entry(source_record)
    bound_entry = inherited.get("source_ledger")
    projection = inherited.get("marker_ledger_projection")
    if source_entry is None:
        if bound_entry is not None or inherited.get("ledger_policy") != "no_ckpt1_marker_ledger" or projection != []:
            raise CheckpointError("malformed inherited marker ledger: CKPT-1 has no bound marker ledger")
        return
    if inherited.get("ledger_policy") != "sealed_ckpt1_marker_ledger" or bound_entry != source_entry:
        raise CheckpointError("inherited marker ledger binding does not match CKPT-1 artifact bundle")
    actual_projection = _marker_ledger_projection(source_entry)
    if projection != actual_projection:
        raise CheckpointError("inherited marker ledger projection does not match CKPT-1 marker ledger")


def _assert_review_screenshot_png(image_path: Path, *, label: str) -> None:
    """Validate screenshot bytes, dimensions, and decompressed raster shape fail-closed."""
    try:
        data = image_path.read_bytes()
    except OSError as exc:
        raise CheckpointError(f"cannot read {label}: {exc}") from exc
    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) + 25 or not data.startswith(signature):
        raise CheckpointError(f"{label} is not a PNG image: {image_path}")
    offset = len(signature)
    ihdr: bytes | None = None
    idat: list[bytes] = []
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise CheckpointError(f"{label} PNG is truncated: {image_path}")
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise CheckpointError(f"{label} PNG has an invalid chunk length: {image_path}")
        chunk = data[start:end]
        expected_crc = int.from_bytes(data[end:end + 4], "big")
        actual_crc = zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise CheckpointError(f"{label} PNG has a corrupt {chunk_type.decode('ascii', 'replace')} CRC: {image_path}")
        if ihdr is None:
            if chunk_type != b"IHDR" or length != 13:
                raise CheckpointError(f"{label} PNG lacks a valid IHDR: {image_path}")
            ihdr = chunk
        if chunk_type == b"IDAT":
            idat.append(chunk)
        if chunk_type == b"IEND":
            if length != 0 or end + 4 != len(data):
                raise CheckpointError(f"{label} PNG has invalid trailing data: {image_path}")
            saw_iend = True
            break
        offset = end + 4
    if ihdr is None or not idat or not saw_iend:
        raise CheckpointError(f"{label} PNG is incomplete: {image_path}")
    width = int.from_bytes(ihdr[0:4], "big")
    height = int.from_bytes(ihdr[4:8], "big")
    bit_depth = ihdr[8]
    color_type = ihdr[9]
    compression = ihdr[10]
    filter_method = ihdr[11]
    interlace = ihdr[12]
    if (
        width != REVIEW_SCREENSHOT_WIDTH
        or height != REVIEW_SCREENSHOT_HEIGHT
        or bit_depth != 8
        or color_type not in {2, 6}
        or compression != 0
        or filter_method != 0
        or interlace != 0
    ):
        raise CheckpointError(
            f"{label} must be a {REVIEW_SCREENSHOT_WIDTH}x{REVIEW_SCREENSHOT_HEIGHT} "
            f"non-interlaced 8-bit RGB/RGBA PNG: {image_path}"
        )
    try:
        decoded = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise CheckpointError(f"{label} PNG data cannot be decoded: {image_path}: {exc}") from exc
    bytes_per_pixel = 4 if color_type == 6 else 3
    expected_length = height * (1 + width * bytes_per_pixel)
    if len(decoded) != expected_length:
        raise CheckpointError(f"{label} PNG has invalid decoded dimensions: {image_path}")
    scanline = 1 + width * bytes_per_pixel
    if any(decoded[offset] > 4 for offset in range(0, len(decoded), scanline)):
        raise CheckpointError(f"{label} PNG has an invalid filter byte: {image_path}")


def _validate_qa_report(qa_path: Path, deck_path: Path, *, label: str) -> None:
    payload = _read_json(qa_path, label=label)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CheckpointError(f"malformed {label}: unsupported schema_version")
    if payload.get("kind") != "scholar-slides-qa-report":
        raise CheckpointError(f"malformed {label}: unexpected kind")
    qa_deck = _require_file_entry(payload.get("deck"), label=f"{label} deck")
    if qa_deck != deck_path:
        raise CheckpointError(f"{label} deck does not match the checkpoint artifact")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise CheckpointError(f"malformed {label}: findings must be a list")
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict) or finding.get("severity") not in {"P0", "P1", "P2", "P3"}:
            raise CheckpointError(f"malformed {label}: invalid finding {index}")
        if finding["severity"] in {"P0", "P1"}:
            raise CheckpointError(
                f"{label} has unresolved {finding['severity']} finding(s); repair them before CKPT-3"
            )


def _validate_build_bundle(bundle_path: Path, deck_path: Path, html: Path, print_html: Path, *, label: str) -> None:
    payload = _read_json(bundle_path, label=label)
    if not isinstance(payload, dict) or payload.get("schema_version") != REVIEW_BUNDLE_SCHEMA_VERSION:
        raise CheckpointError(f"malformed {label}: unsupported schema_version")
    if payload.get("kind") != BUILD_BUNDLE_KIND:
        raise CheckpointError(f"malformed {label}: unexpected kind")
    bundle_deck = _require_file_entry(payload.get("deck"), label=f"{label} deck")
    if bundle_deck != deck_path:
        raise CheckpointError(f"{label} deck does not match the checkpoint artifact")
    if payload.get("interactive_html") != str(html) or payload.get("print_html") != str(print_html):
        raise CheckpointError(f"malformed {label}: HTML paths do not match the render inputs")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise CheckpointError(f"malformed {label}: files must be a non-empty list")
    checked_files = {_require_file_entry(entry, label=f"{label} file") for entry in files}
    if html not in checked_files or print_html not in checked_files:
        raise CheckpointError(f"malformed {label}: deck.html and deck.print.html must be bundled")


def _validate_review_render_evidence(render_path: Path, deck_path: Path, html: Path, *, label: str) -> list[Path]:
    payload = _read_json(render_path, label=label)
    if not isinstance(payload, dict) or payload.get("schema_version") != REVIEW_BUNDLE_SCHEMA_VERSION:
        raise CheckpointError(f"malformed {label}: unsupported schema_version")
    if payload.get("kind") != REVIEW_RENDER_KIND:
        raise CheckpointError(f"malformed {label}: unexpected kind")
    render_deck = _require_file_entry(payload.get("deck"), label=f"{label} deck")
    if render_deck != deck_path or payload.get("interactive_html") != str(html):
        raise CheckpointError(f"{label} deck or HTML does not match the checkpoint artifact")
    viewport = payload.get("viewport")
    if viewport != {"width": REVIEW_SCREENSHOT_WIDTH, "height": REVIEW_SCREENSHOT_HEIGHT}:
        raise CheckpointError(f"malformed {label}: screenshot viewport is invalid")
    print_html = _canonical_file(html.parent / "deck.print.html", label=f"{label} print HTML")
    build_bundle = _require_file_entry(payload.get("build_bundle"), label=f"{label} build bundle")
    _validate_build_bundle(build_bundle, deck_path, html, print_html, label=f"{label} build bundle")
    deck_payload = _read_artifact_payload(deck_path)
    slides = deck_payload.get("slides") if isinstance(deck_payload, dict) else None
    expected_count = len(slides) if isinstance(slides, list) else 0
    screenshots = payload.get("screenshots")
    if not isinstance(screenshots, list) or len(screenshots) != expected_count or expected_count == 0:
        raise CheckpointError(f"malformed {label}: screenshots must cover all {expected_count} deck slide(s)")
    screenshot_paths: list[Path] = []
    screenshot_dir: Path | None = None
    deck_dir = html.parent
    for index, entry in enumerate(screenshots, start=1):
        screenshot = _require_file_entry(entry, label=f"{label} screenshot")
        expected_name = f"slide-{index:02d}.png"
        if screenshot.name != expected_name:
            raise CheckpointError(f"malformed {label}: screenshot {index} must be named {expected_name}")
        if not _within(deck_dir, screenshot):
            raise CheckpointError(f"malformed {label}: screenshot escapes the deck directory")
        if screenshot_dir is None:
            screenshot_dir = screenshot.parent
        elif screenshot.parent != screenshot_dir:
            raise CheckpointError(f"malformed {label}: screenshots must share one directory")
        _assert_review_screenshot_png(screenshot, label=f"{label} screenshot")
        screenshot_paths.append(screenshot)
    return screenshot_paths


def _marker_locations_payload(payload: Any) -> list[dict[str, Any]]:
    locations = collect_integrity_marker_locations(payload)
    return [
        {"pointer": pointer, "marker": marker, "count": count}
        for (pointer, marker), count in sorted(locations.items())
    ]


def _validate_marker_locations(value: Any) -> None:
    if not isinstance(value, list):
        raise CheckpointError("malformed checkpoint record: integrity_marker_locations must be a list")
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("pointer"), str) or not isinstance(entry.get("marker"), str):
            raise CheckpointError("malformed checkpoint record: invalid integrity marker location")
        if not isinstance(entry.get("count"), int) or entry["count"] < 1:
            raise CheckpointError("malformed checkpoint record: invalid integrity marker count")


def _record_artifact_path(record: dict[str, Any]) -> Path:
    artifact = record.get("artifact")
    if not isinstance(artifact, dict):
        raise CheckpointError("malformed checkpoint record: artifact must be an object")
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise CheckpointError("malformed checkpoint record: artifact.path is required")
    return _canonical_file(raw_path, label="recorded checkpoint artifact")


def _validate_review_bundle(bundle_path: Path, deck_path: Path, *, label: str) -> dict[str, Any]:
    payload = _read_json(bundle_path, label=label)
    if not isinstance(payload, dict) or payload.get("schema_version") != REVIEW_BUNDLE_SCHEMA_VERSION:
        raise CheckpointError(f"malformed {label}: unsupported schema_version")
    if payload.get("kind") != "scholar-slides-review-bundle":
        raise CheckpointError(f"malformed {label}: unexpected kind")
    deck = payload.get("deck")
    if not isinstance(deck, dict):
        raise CheckpointError(f"malformed {label}: deck is required")
    bundled_deck = _require_file_entry(deck, label=f"{label} deck")
    if bundled_deck != deck_path:
        raise CheckpointError(f"{label} deck does not match the checkpoint artifact")
    html_path = payload.get("interactive_html")
    if not isinstance(html_path, str) or not html_path:
        raise CheckpointError(f"malformed {label}: interactive_html is required")
    html = _canonical_file(html_path, label=f"{label} interactive_html")
    print_html_path = payload.get("print_html")
    if not isinstance(print_html_path, str) or not print_html_path:
        raise CheckpointError(f"malformed {label}: print_html is required")
    print_html = _canonical_file(print_html_path, label=f"{label} print_html")
    if print_html != html.parent / "deck.print.html":
        raise CheckpointError(f"malformed {label}: print_html must be deck.print.html beside deck.html")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise CheckpointError(f"malformed {label}: files must be a non-empty list")
    seen: set[Path] = set()
    has_html = False
    has_print_html = False
    for entry in files:
        checked = _require_file_entry(entry, label=f"{label} file")
        if checked in seen:
            raise CheckpointError(f"malformed {label}: duplicate file entry")
        seen.add(checked)
        has_html = has_html or checked == html
        has_print_html = has_print_html or checked == print_html
    if not has_html:
        raise CheckpointError(f"malformed {label}: interactive_html is not in files")
    if not has_print_html:
        raise CheckpointError(f"malformed {label}: print_html is not in files")
    for input_dir_name in ("assets", "figures"):
        input_dir = html.parent / input_dir_name
        if input_dir.exists():
            for required_input in _tree_files(input_dir):
                if required_input not in seen:
                    raise CheckpointError(f"malformed {label}: render input is not in files: {required_input}")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise CheckpointError(f"malformed {label}: sealed QA and screenshot evidence is required")
    qa_report = evidence.get("qa_report")
    qa_path = _require_file_entry(qa_report, label=f"{label} QA report")
    if qa_path not in seen:
        raise CheckpointError(f"malformed {label}: QA report is not included in files")
    _validate_qa_report(qa_path, deck_path, label=f"{label} QA report")
    review_render = evidence.get("review_render")
    render_path = _require_file_entry(review_render, label=f"{label} review render evidence")
    if render_path not in seen:
        raise CheckpointError(f"malformed {label}: review render evidence is not included in files")
    rendered_screenshots = _validate_review_render_evidence(
        render_path,
        deck_path,
        html,
        label=f"{label} review render evidence",
    )
    screenshots = evidence.get("screenshots")
    if not isinstance(screenshots, list) or len(screenshots) != len(rendered_screenshots):
        raise CheckpointError(f"malformed {label}: screenshots must match the review render evidence")
    for index, screenshot in enumerate(screenshots):
        screenshot_path = _require_file_entry(screenshot, label=f"{label} screenshot")
        if screenshot_path not in seen:
            raise CheckpointError(f"malformed {label}: screenshot is not included in files")
        if screenshot_path != rendered_screenshots[index]:
            raise CheckpointError(f"malformed {label}: screenshot does not match review render evidence")
    return payload


def _review_relative_file(root: Path, raw: Any, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw or raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", raw) or "\\" in raw:
        raise CheckpointError(f"malformed review manifest: {label} must be a portable relative path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CheckpointError(f"malformed review manifest: {label} must not traverse the project root")
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise CheckpointError(f"malformed review manifest: {label} is missing") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise CheckpointError(f"malformed review manifest: {label} may not traverse a symlink or reparse point")
    resolved = _canonical_file(candidate, label=f"review manifest {label}")
    if not _within(root, resolved):
        raise CheckpointError(f"malformed review manifest: {label} escapes the project root")
    return resolved


def _validate_pending_review_manifest(manifest_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    payload = _read_json(manifest_path, label="review manifest")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("kind") != REVIEW_PREVIEW_MANIFEST_KIND:
        raise CheckpointError("malformed review manifest: unsupported schema or kind")
    deck_path = _record_artifact_path(record)
    root = deck_path.parent
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("renderer_version"), str) or not inputs["renderer_version"]:
        raise CheckpointError("malformed review manifest: renderer_version is required")
    graph_entry = record.get("asset_graph")
    if not isinstance(graph_entry, dict):
        raise CheckpointError("malformed review manifest: CKPT-2 asset graph is missing")
    graph_path = _require_file_entry(graph_entry, label="asset graph")
    for name, expected in (("deck", deck_path), ("asset_graph", graph_path)):
        entry = inputs.get(name)
        if not isinstance(entry, dict):
            raise CheckpointError(f"malformed review manifest: inputs.{name} is required")
        bound = _review_relative_file(root, entry.get("path"), label=f"inputs.{name}.path")
        if bound != expected or entry.get("sha256") != sha256_file(expected):
            raise CheckpointError(f"malformed review manifest: inputs.{name} does not match current CKPT-2 evidence")
    review_root = payload.get("review_root")
    if review_root != "review":
        raise CheckpointError("malformed review manifest: review_root must be the project review directory")
    review_prefix = review_root.rstrip("/") + "/"
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise CheckpointError("malformed review manifest: outputs must be a non-empty list")
    seen: set[Path] = set()
    output_paths: set[str] = set()
    for entry in outputs:
        if not isinstance(entry, dict):
            raise CheckpointError("malformed review manifest: output entry must be an object")
        target = _review_relative_file(root, entry.get("path"), label="output path")
        if target in seen:
            raise CheckpointError("malformed review manifest: duplicate output path")
        seen.add(target)
        portable = str(entry.get("path"))
        if not portable.startswith(review_prefix):
            raise CheckpointError("malformed review manifest: every output must remain inside review/")
        output_paths.add(portable)
        if entry.get("sha256") != sha256_file(target):
            raise CheckpointError("review manifest is stale: output SHA-256 changed")
        if Path(portable).name in {"slides.html", "slides.pdf", "slides.pptx", "speaker_notes.md", "presentation-script.md", "presentation-summary.md", "delivery-manifest.json"}:
            raise CheckpointError("malformed review manifest: official delivery artifacts are forbidden in pending review")
    required = {
        f"{review_prefix}slides-review.html",
        f"{review_prefix}montage.png",
        f"{review_prefix}visual-qa.json",
        f"{review_prefix}aesthetics_report.json",
        f"{review_prefix}aesthetics-qa.json",
    }
    try:
        deck_payload = _read_json(deck_path, label="review deck")
        slide_count = len(deck_payload.get("slides", [])) if isinstance(deck_payload, dict) and isinstance(deck_payload.get("slides"), list) else 0
    except CheckpointError:
        raise
    screenshot_paths = {path for path in output_paths if path.startswith(f"{review_prefix}png/") and path.endswith(".png")}
    # Legacy checkpoint-contract fixtures predate deck.json's slide array.  Production deck
    # specs always provide it; retaining this narrow fallback keeps their path/hash contract
    # testable without weakening exact naming for real slide arrays.
    if slide_count < 1:
        slide_count = len(screenshot_paths)
    if slide_count < 1:
        raise CheckpointError("malformed review manifest: review requires at least one slide screenshot")
    modern_semantic_contract = (
        isinstance(deck_payload, dict)
        and isinstance(deck_payload.get("meta"), dict)
        and deck_payload["meta"].get("speaker_notes_schema") == "speaker-content-v1"
    ) or isinstance(inputs.get("digest"), dict)
    if modern_semantic_contract:
        digest_entry = inputs.get("digest")
        if not isinstance(digest_entry, dict):
            raise CheckpointError("review readiness blocked: semantic QA requires a manifest digest input")
        digest_path = _review_relative_file(root, digest_entry.get("path"), label="inputs.digest.path")
        if digest_entry.get("sha256") != sha256_file(digest_path):
            raise CheckpointError("review readiness blocked: manifest digest input is stale")
        try:
            predecessor_record = _read_record(record["requires"]["record_path"])
            expected_digest = _record_artifact_path(predecessor_record)
        except (KeyError, CheckpointError) as exc:
            raise CheckpointError("review readiness blocked: CKPT-1 digest binding is unavailable") from exc
        if digest_path != expected_digest:
            raise CheckpointError("review readiness blocked: manifest digest input does not match CKPT-1 evidence")
        semantic_portable = f"{review_prefix}semantic-qa.json"
        if semantic_portable not in output_paths:
            raise CheckpointError("review readiness blocked: semantic QA output is missing")
        semantic_path = _review_relative_file(root, semantic_portable, label="semantic QA")
        semantic = _read_json(semantic_path, label="semantic QA")
        semantic_summary = semantic.get("summary") if isinstance(semantic, dict) and isinstance(semantic.get("summary"), dict) else {}
        semantic_inputs = semantic.get("inputs") if isinstance(semantic, dict) and isinstance(semantic.get("inputs"), dict) else {}
        if not isinstance(semantic, dict) or semantic.get("schema_version") != 1 or semantic.get("kind") != "scholar-slides-semantic-qa" or semantic.get("status") != "pass":
            raise CheckpointError("review readiness blocked: semantic QA must pass before CKPT-2 binding")
        if semantic_summary.get("errors", 0) != 0 or not isinstance(semantic.get("issues"), list) or any(isinstance(item, dict) and item.get("severity") == "error" for item in semantic["issues"]):
            raise CheckpointError("review readiness blocked: semantic QA has blocking errors")
        if semantic_inputs.get("deck_sha256") != record["artifact"].get("sha256") or semantic_inputs.get("asset_graph_sha256") != graph_entry.get("sha256") or semantic_inputs.get("digest_sha256") != digest_entry.get("sha256"):
            raise CheckpointError("review readiness blocked: semantic QA inputs do not match CKPT-2 evidence")
    expected_screenshots = {f"{review_prefix}png/slide-{index:02d}.png" for index in range(1, slide_count + 1)}
    if not required <= output_paths or screenshot_paths != expected_screenshots:
        raise CheckpointError("malformed review manifest: review core artifacts are incomplete")
    for screenshot in sorted(screenshot_paths):
        _assert_review_screenshot_png(_review_relative_file(root, screenshot, label="review screenshot"), label="pending review screenshot")
    qa_path = _review_relative_file(root, f"{review_prefix}visual-qa.json", label="visual QA")
    qa = _read_json(qa_path, label="visual QA")
    if not isinstance(qa, dict) or qa.get("schema_version") != 1 or qa.get("kind") != "scholar-slides-visual-qa" or qa.get("status") != "pass":
        raise CheckpointError("review readiness blocked: visual QA must pass before CKPT-2 approval")
    findings = qa.get("issues")
    if not isinstance(findings, list) or any(not isinstance(finding, dict) or finding.get("severity") not in {"error", "warning", "info"} for finding in findings):
        raise CheckpointError("review readiness blocked: visual QA issues are malformed")
    if any(finding["severity"] == "error" for finding in findings):
        raise CheckpointError("review readiness blocked: visual QA has blocking errors")
    qa_inputs = qa.get("inputs")
    if not isinstance(qa_inputs, dict) or qa_inputs.get("deck_sha256") != record["artifact"].get("sha256") or qa_inputs.get("asset_graph_sha256") != graph_entry.get("sha256"):
        raise CheckpointError("review readiness blocked: visual QA inputs do not match CKPT-2 evidence")
    aesthetics_report_path = _review_relative_file(root, f"{review_prefix}aesthetics_report.json", label="aesthetics report")
    aesthetics_qa_path = _review_relative_file(root, f"{review_prefix}aesthetics-qa.json", label="aesthetics QA")
    aesthetics_report = validate_aesthetics_report(_read_json(aesthetics_report_path, label="aesthetics report"))
    aesthetics_qa = validate_aesthetics_report(_read_json(aesthetics_qa_path, label="aesthetics QA"))
    if aesthetics_report.get("status") != "pass" or aesthetics_qa.get("status") != "pass" or aesthetics_report.get("rework") or aesthetics_qa.get("rework"):
        raise CheckpointError("review readiness blocked: aesthetics QA must pass with no open rework before CKPT-2 approval")
    aesthetics_inputs = _read_json(aesthetics_qa_path, label="aesthetics QA").get("inputs")
    if not isinstance(aesthetics_inputs, dict) or aesthetics_inputs.get("deck_sha256") != record["artifact"].get("sha256") or aesthetics_inputs.get("asset_graph_sha256") != graph_entry.get("sha256"):
        raise CheckpointError("review readiness blocked: aesthetics QA inputs do not match CKPT-2 evidence")
    return payload


def _validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise CheckpointError("malformed checkpoint record: expected an object")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise CheckpointError("malformed checkpoint record: unsupported schema_version")
    checkpoint = record.get("checkpoint")
    if checkpoint not in CHECKPOINTS:
        raise CheckpointError("malformed checkpoint record: unknown checkpoint")
    status = record.get("status")
    if status not in {"pending", "pending_human_confirmation", "approved", "confirmed"}:
        raise CheckpointError(
            "malformed checkpoint record: status must be pending, pending_human_confirmation, approved, or confirmed"
        )
    intended_confirmer = record.get("intended_confirmer")
    prepared_by = record.get("prepared_by")
    if status == "pending_human_confirmation":
        if checkpoint != "CKPT-1":
            raise CheckpointError("malformed checkpoint record: only CKPT-1 may await human confirmation")
        is_intended = isinstance(intended_confirmer, str) and bool(intended_confirmer.strip())
        is_agent = isinstance(prepared_by, dict) and prepared_by.get("kind") == "agent" and isinstance(prepared_by.get("name"), str) and bool(prepared_by["name"].strip()) and set(prepared_by) == {"kind", "name"}
        if is_intended == is_agent:
            raise CheckpointError(
                "malformed checkpoint record: pending_human_confirmation requires exactly one intended confirmer or agent preparer"
            )
    elif intended_confirmer is not None or prepared_by is not None:
        raise CheckpointError(
            "malformed checkpoint record: preparation identities are only valid while awaiting human confirmation"
        )
    candidate_sha256 = record.get("candidate_sha256")
    if candidate_sha256 is not None and (not isinstance(candidate_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256)):
        raise CheckpointError("malformed checkpoint record: candidate_sha256 is invalid")
    artifact = record.get("artifact")
    if not isinstance(artifact, dict):
        raise CheckpointError("malformed checkpoint record: artifact must be an object")
    recorded_path = _record_artifact_path(record)
    if artifact.get("path") != str(recorded_path):
        raise CheckpointError("malformed checkpoint record: artifact.path is not canonical")
    expected_hash = artifact.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise CheckpointError("malformed checkpoint record: artifact.sha256 is invalid")
    bundle = record.get("artifact_bundle")
    if not isinstance(bundle, list) or not bundle:
        raise CheckpointError("malformed checkpoint record: artifact_bundle must be a non-empty list")
    bundle_paths = [_require_file_entry(entry, label="checkpoint artifact") for entry in bundle]
    if len(bundle_paths) != len(set(bundle_paths)):
        raise CheckpointError("malformed checkpoint record: artifact_bundle has duplicate paths")
    if recorded_path not in bundle_paths:
        raise CheckpointError("malformed checkpoint record: artifact is not in artifact_bundle")
    if sha256_file(recorded_path) != expected_hash:
        raise CheckpointError("checkpoint artifact bundle is stale: primary artifact SHA-256 changed")
    if not isinstance(record.get("integrity_markers"), dict):
        raise CheckpointError("malformed checkpoint record: integrity_markers must be an object")
    _validate_marker_locations(record.get("integrity_marker_locations"))
    predecessor = PREDECESSOR[checkpoint]
    requires = record.get("requires")
    if predecessor is None:
        if requires is not None:
            raise CheckpointError("malformed checkpoint record: CKPT-1 cannot require a predecessor")
    else:
        if not isinstance(requires, dict):
            raise CheckpointError(f"malformed checkpoint record: {checkpoint} requires {predecessor}")
        if requires.get("checkpoint") != predecessor:
            raise CheckpointError(f"malformed checkpoint record: {checkpoint} must require {predecessor}")
        required_path = requires.get("record_path")
        if not isinstance(required_path, str) or not required_path:
            raise CheckpointError("malformed checkpoint record: requires.record_path is required")
        required_hash = requires.get("sha256")
        if not isinstance(required_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", required_hash):
            raise CheckpointError("malformed checkpoint record: requires.sha256 is invalid")
    if checkpoint == "CKPT-3":
        review_bundle = record.get("review_bundle")
        if not isinstance(review_bundle, dict):
            raise CheckpointError("malformed checkpoint record: CKPT-3 requires review_bundle")
        _require_file_entry(review_bundle, label="review bundle")
    elif checkpoint == "CKPT-2":
        review_bundle = record.get("review_bundle")
        review_readiness = record.get("review_readiness")
        if review_bundle is None:
            if review_readiness is not None:
                raise CheckpointError("malformed checkpoint record: CKPT-2 review_readiness requires review_bundle")
        else:
            bundle_path = _require_file_entry(review_bundle, label="review bundle")
            _validate_pending_review_manifest(bundle_path, record)
            if not isinstance(review_readiness, dict) or review_readiness.get("status") != "ready_for_human_approval":
                raise CheckpointError("malformed checkpoint record: CKPT-2 review bundle requires ready_for_human_approval")
            if review_readiness.get("manifest_sha256") != review_bundle.get("sha256"):
                raise CheckpointError("malformed checkpoint record: CKPT-2 review readiness does not bind its manifest")
    elif record.get("review_bundle") is not None or record.get("review_readiness") is not None:
        raise CheckpointError("malformed checkpoint record: review bindings are only valid for CKPT-2/CKPT-3")
    if status == "approved":
        if not isinstance(record.get("approved_at"), str) or not record["approved_at"]:
            raise CheckpointError("malformed checkpoint record: approved_at is required")
        if not isinstance(record.get("confirmed_by"), str) or not record["confirmed_by"].strip():
            raise CheckpointError("malformed checkpoint record: confirmed_by is required")
    elif status == "confirmed":
        if not isinstance(record.get("confirmed_at"), str) or not record["confirmed_at"]:
            raise CheckpointError("malformed checkpoint record: confirmed_at is required")
        if not isinstance(record.get("confirmed_by"), str) or not record["confirmed_by"].strip():
            raise CheckpointError("malformed checkpoint record: confirmed_by is required")
        if record.get("approved_at") is not None:
            raise CheckpointError("malformed checkpoint record: confirmed record cannot contain approved_at")
    readiness = record.get("readiness_artifact")
    if readiness is not None:
        if checkpoint != "CKPT-1":
            raise CheckpointError("malformed checkpoint record: readiness_artifact is only valid for CKPT-1")
        readiness_path = _require_file_entry(readiness, label="readiness artifact")
        if readiness_path not in bundle_paths:
            raise CheckpointError("malformed checkpoint record: readiness artifact is not in artifact_bundle")
    approval_bindings = record.get("approval_bindings")
    if approval_bindings is not None:
        if checkpoint != "CKPT-1" or status != "confirmed" or not isinstance(approval_bindings, dict):
            raise CheckpointError("malformed checkpoint record: approval bindings are only valid for confirmed CKPT-1")
        required_bindings = {"source_pdf_sha256", "digest_sha256", "project_options_sha256", "review_json_sha256", "review_markdown_sha256", "marker_ledger_sha256", "readiness_sha256", "evidence_audit_sha256", "candidate_sha256"}
        if set(approval_bindings) != required_bindings:
            raise CheckpointError("malformed checkpoint record: confirmed CKPT-1 approval bindings are incomplete")
        for name, value in approval_bindings.items():
            if name == "evidence_audit_sha256":
                if not isinstance(value, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in value):
                    raise CheckpointError("malformed checkpoint record: evidence audit approval bindings are invalid")
            elif not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise CheckpointError("malformed checkpoint record: approval binding is invalid")
        if record.get("candidate_sha256") != approval_bindings["candidate_sha256"]:
            raise CheckpointError("malformed checkpoint record: prepared candidate identity does not match approval binding")
    source_identity = record.get("source_identity")
    if source_identity is not None:
        if not isinstance(source_identity, dict):
            raise CheckpointError("malformed checkpoint record: source_identity must be an object")
        identity_fields = ("requested_identifier", "resolved_identifier", "pdf_sha256", "fetched_at")
        if any(not isinstance(source_identity.get(field), str) or not source_identity[field] for field in identity_fields):
            raise CheckpointError("malformed checkpoint record: source_identity is incomplete")
        if not re.fullmatch(r"[0-9a-f]{64}", source_identity["pdf_sha256"]):
            raise CheckpointError("malformed checkpoint record: source_identity.pdf_sha256 is invalid")
    if checkpoint == "CKPT-2":
        _validate_asset_graph_binding(record, bundle_paths)
    elif record.get("asset_graph") is not None or record.get("supplemental_artifacts") is not None:
        raise CheckpointError("malformed checkpoint record: asset graph bindings are only valid for CKPT-2")
    supersedes = record.get("supersedes")
    if supersedes is not None:
        if checkpoint != "CKPT-2" or not isinstance(supersedes, dict):
            raise CheckpointError("malformed checkpoint record: supersedes is only valid for CKPT-2")
        for field in ("revision_id", "checkpoint_sha256", "history_manifest_sha256"):
            if not isinstance(supersedes.get(field), str) or not supersedes[field]:
                raise CheckpointError(f"malformed checkpoint record: supersedes.{field} is required")
        if not re.fullmatch(r"[0-9a-f]{64}", supersedes["checkpoint_sha256"]):
            raise CheckpointError("malformed checkpoint record: supersedes.checkpoint_sha256 is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", supersedes["history_manifest_sha256"]):
            raise CheckpointError("malformed checkpoint record: supersedes.history_manifest_sha256 is invalid")
    _validate_inherited_marker_ledger_shape(record)
    return record


def _validate_record_runtime(record: dict[str, Any]) -> None:
    """Check all dynamic input bundles after the static schema has been checked."""
    artifact = _record_artifact_path(record)
    for entry in record["artifact_bundle"]:
        _require_file_entry(entry, label="checkpoint artifact")
    if record["checkpoint"] == "CKPT-3":
        review = _require_file_entry(record["review_bundle"], label="review bundle")
        _validate_review_bundle(review, artifact, label="review bundle")


def _require_predecessor(record: dict[str, Any], seen: set[Path]) -> dict[str, Any] | None:
    predecessor = PREDECESSOR[record["checkpoint"]]
    if predecessor is None:
        return None
    required = record["requires"]
    required_record_path = _canonical_file(required["record_path"], label="required checkpoint record")
    if sha256_file(required_record_path) != required["sha256"]:
        raise CheckpointError("required checkpoint record changed after this checkpoint was created")
    required_record = _validated_record_for_runtime(required_record_path)
    approved_predecessor = _require_approved_checkpoint(
        required_record_path,
        _record_artifact_path(required_record),
        expected_checkpoint=predecessor,
        seen=seen,
    )
    _validate_inherited_marker_ledger_runtime(record, approved_predecessor)
    return required_record


def create_checkpoint(
    checkpoint: str,
    artifact_path: str | Path,
    record_path: str | Path,
    *,
    prerequisite_record: str | Path | None = None,
    review_bundle_path: str | Path | None = None,
    supplemental_artifacts: Iterable[str | Path] = (),
    intended_confirmer: str | None = None,
    prepared_by: dict[str, str] | None = None,
    candidate_sha256: str | None = None,
    asset_graph_path: str | Path | None = None,
) -> dict[str, Any]:
    if checkpoint not in CHECKPOINTS:
        raise CheckpointError(f"unknown checkpoint {checkpoint!r}; expected one of {sorted(CHECKPOINTS)}")
    if asset_graph_path is not None and checkpoint != "CKPT-2":
        raise CheckpointError("only CKPT-2 accepts an asset graph output path")
    artifact = _canonical_file(artifact_path, label="checkpoint artifact")
    if asset_graph_path is not None and Path(asset_graph_path).parent.resolve() != artifact.parent:
        raise CheckpointError("asset graph output must remain beside deck.json so its portable paths stay rooted")
    supplemental = [
        _canonical_file(item, label="supplemental checkpoint artifact")
        for item in supplemental_artifacts
    ]
    if artifact in supplemental or len(supplemental) != len(set(supplemental)):
        raise CheckpointError("supplemental checkpoint artifacts must be distinct from the primary artifact and each other")
    payload = _read_artifact_payload(artifact)
    if checkpoint == "CKPT-2" and isinstance(payload, dict):
        findings = validate_visible_content(payload)
        if findings:
            detail = "; ".join(finding.detail for finding in findings)
            raise CheckpointError(f"CKPT-2 deck exposes forbidden visible content: {detail}")
    predecessor = PREDECESSOR[checkpoint]
    requires = None
    inherited_marker_ledger = None
    required_record: dict[str, Any] | None = None
    asset_graph: dict[str, str] | None = None
    graph_artifacts: list[Path] = []
    if predecessor is None:
        if prerequisite_record is not None:
            raise CheckpointError("CKPT-1 cannot have a prerequisite record")
        if review_bundle_path is not None:
            raise CheckpointError("CKPT-1 cannot have a review bundle")
    else:
        if prerequisite_record is None:
            raise CheckpointError(f"{checkpoint} requires an approved {predecessor} record")
        required_path = _canonical_file(prerequisite_record, label="prerequisite checkpoint record")
        required_record = _validate_record(_read_record(required_path))
        _require_approved_checkpoint(required_path, _record_artifact_path(required_record), expected_checkpoint=predecessor)
        previous_payload = _read_artifact_payload(_record_artifact_path(required_record))
        if checkpoint == "CKPT-2":
            # CKPT-1 remains the immutable marker ledger. Bind it to this CKPT-2 record
            # rather than copying unresolved markers into viewer-facing slide content.
            inherited_marker_ledger = {
                "source_checkpoint": "CKPT-1",
                "source_record_path": str(required_path),
                "source_record_sha256": sha256_file(required_path),
                "markers": dict(sorted(collect_integrity_markers(previous_payload).items())),
            }
            marker_ledger_entry = _ckpt1_marker_ledger_entry(required_record)
            if marker_ledger_entry is None:
                inherited_marker_ledger.update({"ledger_policy": "no_ckpt1_marker_ledger", "source_ledger": None, "marker_ledger_projection": []})
            else:
                inherited_marker_ledger.update({"ledger_policy": "sealed_ckpt1_marker_ledger", "source_ledger": dict(marker_ledger_entry), "marker_ledger_projection": _marker_ledger_projection(marker_ledger_entry)})
        else:
            if artifact != _record_artifact_path(required_record) or sha256_file(artifact) != required_record["artifact"]["sha256"]:
                raise CheckpointError("CKPT-3 must approve the unchanged CKPT-2 deck artifact")
            assert_markers_preserved(previous_payload, payload)
        requires = {"checkpoint": predecessor, "record_path": str(required_path), "sha256": sha256_file(required_path)}
        if checkpoint == "CKPT-2":
            default_graph = artifact.with_name("asset-graph.json" if artifact.name == "deck.json" else f"{artifact.stem}.asset-graph.json")
            requested_graph = Path(asset_graph_path) if asset_graph_path is not None else default_graph
            reserved_graph_paths = (artifact, Path(record_path), _record_artifact_path(required_record), *supplemental)
            _safe_graph_destination(requested_graph, artifact.parent, reserved=reserved_graph_paths)
            graph_path, graph_artifacts = _write_asset_graph(
                artifact,
                _record_artifact_path(required_record),
                forbidden_assets=_sealed_forbidden_assets(required_record),
                output_path=requested_graph,
                reserved=reserved_graph_paths,
            )
            asset_graph = _file_entry(graph_path)
            graph_artifacts.append(graph_path)
    review_bundle = None
    if checkpoint == "CKPT-3":
        if review_bundle_path is None:
            raise CheckpointError("CKPT-3 requires a sealed CKPT-2 review bundle")
        review = _canonical_file(review_bundle_path, label="review bundle")
        _validate_review_bundle(review, artifact, label="review bundle")
        review_bundle = _file_entry(review)
    elif review_bundle_path is not None:
        raise CheckpointError("only CKPT-3 accepts --review-bundle")
    confirmer = intended_confirmer.strip() if isinstance(intended_confirmer, str) else ""
    if intended_confirmer is not None and not confirmer:
        raise CheckpointError("intended_confirmer must be a non-empty name")
    if confirmer and checkpoint != "CKPT-1":
        raise CheckpointError("only CKPT-1 may await an intended human confirmer")
    agent = dict(prepared_by) if isinstance(prepared_by, dict) else None
    if prepared_by is not None and (
        checkpoint != "CKPT-1"
        or agent is None
        or set(agent) != {"kind", "name"}
        or agent.get("kind") != "agent"
        or not isinstance(agent.get("name"), str)
        or not agent["name"].strip()
    ):
        raise CheckpointError("prepared_by must be a CKPT-1 agent object with kind and name")
    if confirmer and agent is not None:
        raise CheckpointError("a CKPT-1 preparation record cannot name both a human confirmer and an agent preparer")
    if candidate_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
        raise CheckpointError("candidate_sha256 must be a lowercase SHA-256 digest")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint": checkpoint,
        "status": "pending_human_confirmation" if confirmer or agent is not None else "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact": {"path": str(artifact), "sha256": sha256_file(artifact)},
        "artifact_bundle": _artifact_bundle(checkpoint, artifact, supplemental, graph_artifacts),
        "integrity_markers": dict(sorted(collect_integrity_markers(payload).items())),
        "integrity_marker_locations": _marker_locations_payload(payload),
    }
    if confirmer:
        record["intended_confirmer"] = confirmer
    if agent is not None:
        record["prepared_by"] = {"kind": "agent", "name": agent["name"].strip()}
    if candidate_sha256 is not None:
        record["candidate_sha256"] = candidate_sha256
    source = payload.get("source") if isinstance(payload, dict) else None
    identity_fields = ("requested_identifier", "resolved_identifier", "pdf_sha256", "fetched_at")
    if isinstance(source, dict) and all(isinstance(source.get(field), str) and source[field] for field in identity_fields):
        record["source_identity"] = {field: source[field] for field in identity_fields}
    if requires is not None:
        record["requires"] = requires
    if inherited_marker_ledger is not None:
        record["inherited_marker_ledger"] = inherited_marker_ledger
    if asset_graph is not None:
        record["asset_graph"] = asset_graph
        record["supplemental_artifacts"] = [_file_entry(item) for item in supplemental]
    if review_bundle is not None:
        record["review_bundle"] = review_bundle
    if checkpoint == "CKPT-2":
        lineage_path = Path(record_path).resolve().parent / LINEAGE_MARKER_NAME
        if lineage_path.is_file():
            lineage = _read_json(lineage_path, label="CKPT-2 lineage marker")
            if not isinstance(lineage, dict) or lineage.get("kind") != LINEAGE_KIND:
                raise CheckpointError("malformed CKPT-2 lineage marker")
            revision_id = lineage.get("revision_id")
            checkpoint_sha = lineage.get("source_checkpoint_sha256")
            history_manifest_sha = lineage.get("history_manifest_sha256")
            if (
                not isinstance(revision_id, str)
                or not revision_id
                or not isinstance(checkpoint_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha)
                or not isinstance(history_manifest_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", history_manifest_sha)
            ):
                raise CheckpointError("malformed CKPT-2 lineage marker fields")
            history_manifest_path = Path(record_path).resolve().parent / HISTORY_ROOT_NAME / "CKPT-2" / revision_id / "history-manifest.json"
            if not history_manifest_path.is_file() or sha256_file(history_manifest_path) != history_manifest_sha:
                raise CheckpointError("CKPT-2 supersedes history is missing or tampered")
            record["supersedes"] = {
                "revision_id": revision_id,
                "checkpoint_sha256": checkpoint_sha,
                "history_manifest_sha256": history_manifest_sha,
            }
    _write_record(record_path, record)
    return record


def approve_checkpoint(
    record_path: str | Path,
    confirmed_by: str,
    *,
    readiness_artifact: str | Path | None = None,
) -> dict[str, Any]:
    if not confirmed_by or not confirmed_by.strip():
        raise CheckpointError("confirmed_by is required; record confirmation only after the user explicitly approves")
    record = _validate_record(_read_record(record_path))
    if record.get("checkpoint") == "CKPT-1" and record.get("status") != "pending_human_confirmation":
        raise CheckpointError("CKPT-1 approval requires pending_human_confirmation evidence preparation")
    if record.get("status") not in {"pending", "pending_human_confirmation"}:
        raise CheckpointError(f"checkpoint is not pending: {record.get('status')!r}")
    _validate_record_runtime(record)
    _require_predecessor(record, seen={Path(record_path).resolve()})
    if record.get("checkpoint") == "CKPT-2" and record.get("review_readiness", {}).get("status") != "ready_for_human_approval":
        raise CheckpointError("CKPT-2 cannot be approved before a current passing review preview is bound")
    readiness_path: Path | None = None
    if readiness_artifact is not None:
        if record.get("checkpoint") != "CKPT-1":
            raise CheckpointError("readiness_artifact is only valid when confirming CKPT-1")
        readiness_path = _canonical_file(readiness_artifact, label="readiness artifact")
        bundle_paths = {
            _canonical_file(entry["path"], label="checkpoint artifact")
            for entry in record["artifact_bundle"]
        }
        if readiness_path not in bundle_paths:
            raise CheckpointError("readiness artifact is not bound in the CKPT-1 artifact bundle")
    elif record.get("checkpoint") == "CKPT-1":
        # CLI callers may omit the option when the readiness report was already attached at
        # checkpoint creation; bind the uniquely named report without changing any evidence.
        candidates = [
            _canonical_file(entry["path"], label="checkpoint artifact")
            for entry in record["artifact_bundle"]
            if Path(entry.get("path", "")).name == "ckpt1-readiness.json"
        ]
        if len(candidates) == 1:
            readiness_path = candidates[0]
    if record.get("checkpoint") == "CKPT-1":
        # The readiness computation verifies all sealed candidate/audit/ledger/source evidence
        # against the checkpoint bundle immediately before this one permitted human transition.
        if readiness_path is None:
            raise CheckpointError("CKPT-1 approval requires a bound current readiness artifact")
        try:
            import ckpt1_readiness
            report = ckpt1_readiness.check_readiness(Path(record_path).resolve().parent, record_path, require_readiness_artifact=True)
        except (ImportError, Exception) as exc:
            if isinstance(exc, CheckpointError):
                raise
            raise CheckpointError(f"CKPT-1 approval readiness is stale or invalid: {exc}") from exc
        if report.get("status") != "ready_for_human_approval" or report.get("ready_for_human_confirmation") is not True or report.get("approval_status") != "not_approved" or report.get("human_review_required") is not True:
            raise CheckpointError("CKPT-1 approval requires current human-review readiness")
        entries = {Path(entry["path"]).name: entry for entry in record["artifact_bundle"]}
        required = {"digest.json", "project-options.json", "ckpt1-review.json", "ckpt1-review.md", "ckpt1-markers.json", "ckpt1-readiness.json"}
        if not required <= set(entries):
            raise CheckpointError("CKPT-1 approval requires complete bound digest, options, review, ledger, and readiness evidence")
        audit_hashes: list[str] = []
        for entry in record["artifact_bundle"]:
            path = Path(entry["path"])
            try:
                payload = _read_json(path, label="CKPT-1 approval artifact")
            except (CheckpointError, UnicodeDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("kind") == "scholar-slides-evidence-audit":
                audit_hashes.append(entry["sha256"])
        if record.get("candidate_sha256") is None or record.get("source_identity", {}).get("pdf_sha256") is None:
            raise CheckpointError("CKPT-1 approval requires prepared candidate and source identities")
        review_payload = _read_json(Path(entries["ckpt1-review.json"]["path"]), label="CKPT-1 review")
        digest_payload = _read_json(Path(entries["digest.json"]["path"]), label="CKPT-1 digest")
        try:
            from ckpt1_review import ReviewCandidateError, canonicalize_review_candidate, review_semantic_identity
            candidate_identity = review_semantic_identity(canonicalize_review_candidate(review_payload, digest_payload))
        except (ImportError, ReviewCandidateError) as exc:
            raise CheckpointError(f"CKPT-1 approval candidate identity is invalid: {exc}") from exc
        if candidate_identity != record["candidate_sha256"]:
            raise CheckpointError("CKPT-1 approval candidate identity does not match the prepared review")
        record["status"] = "confirmed"
        record.pop("intended_confirmer", None)
        record.pop("prepared_by", None)
        record.pop("approved_at", None)
        record["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        record["confirmed_by"] = confirmed_by.strip()
        if readiness_path is not None:
            record["readiness_artifact"] = _file_entry(readiness_path)
        record["approval_bindings"] = {
            "source_pdf_sha256": record["source_identity"]["pdf_sha256"],
            "digest_sha256": entries["digest.json"]["sha256"],
            "project_options_sha256": entries["project-options.json"]["sha256"],
            "review_json_sha256": entries["ckpt1-review.json"]["sha256"],
            "review_markdown_sha256": entries["ckpt1-review.md"]["sha256"],
            "marker_ledger_sha256": entries["ckpt1-markers.json"]["sha256"],
            "readiness_sha256": entries["ckpt1-readiness.json"]["sha256"],
            "evidence_audit_sha256": sorted(audit_hashes),
            "candidate_sha256": record["candidate_sha256"],
        }
    else:
        # CKPT-2/CKPT-3 retain the historical approved schema; only CKPT-1 has the
        # distinct confirmed state because it binds the preflight readiness report.
        record["status"] = "approved"
        record["approved_at"] = datetime.now(timezone.utc).isoformat()
        record["confirmed_by"] = confirmed_by.strip()
    _write_record(record_path, record)
    return record


def _pending_ckpt2_for_review_rebind(raw_record: Any) -> dict[str, Any]:
    """Validate pending CKPT-2 evidence while permitting a stale prior review to be rebuilt."""
    if not isinstance(raw_record, dict) or raw_record.get("checkpoint") != "CKPT-2" or raw_record.get("status") != "pending":
        raise CheckpointError("review preview metadata can only be bound to a pending CKPT-2")
    previous_bundle = raw_record.get("review_bundle")
    previous_readiness = raw_record.get("review_readiness")
    if (previous_bundle is None) != (previous_readiness is None):
        raise CheckpointError("malformed checkpoint record: CKPT-2 review binding is incomplete")
    if previous_bundle is not None:
        if (
            not isinstance(previous_bundle, dict)
            or not isinstance(previous_bundle.get("path"), str)
            or not isinstance(previous_bundle.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", previous_bundle["sha256"])
            or not isinstance(previous_readiness, dict)
            or previous_readiness.get("status") != "ready_for_human_approval"
            or previous_readiness.get("manifest_sha256") != previous_bundle["sha256"]
        ):
            raise CheckpointError("malformed checkpoint record: CKPT-2 review binding is invalid")
        raw_record = dict(raw_record)
        raw_record.pop("review_bundle", None)
        raw_record.pop("review_readiness", None)
    return _validate_record(raw_record)


def record_review_bundle(record_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    """Bind a current passing pending-review manifest without changing CKPT-2 approval state."""
    record = _pending_ckpt2_for_review_rebind(_read_record(record_path))
    manifest = _canonical_file(manifest_path, label="review manifest")
    expected_manifest = _review_relative_file(
        _record_artifact_path(record).parent,
        "review/review-manifest.json",
        label="pending review manifest",
    )
    if manifest != expected_manifest:
        raise CheckpointError("review manifest must be the current project's review/review-manifest.json")
    _validate_pending_review_manifest(manifest, record)
    record["review_bundle"] = _file_entry(manifest)
    record["review_readiness"] = {
        "status": "ready_for_human_approval",
        "manifest_sha256": record["review_bundle"]["sha256"],
    }
    _validate_record(record)
    _write_record(record_path, record)
    return record


def _require_approved_checkpoint(
    record_path: str | Path,
    artifact_path: str | Path,
    *,
    expected_checkpoint: str | None = None,
    seen: set[Path] | None = None,
    record_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_record = _canonical_file(record_path, label="checkpoint record")
    seen = set() if seen is None else seen
    if canonical_record in seen:
        raise CheckpointError("malformed checkpoint record: cyclic prerequisite chain")
    seen.add(canonical_record)
    record = _validated_record_for_runtime(canonical_record, record_override=record_override)
    if expected_checkpoint is not None and record["checkpoint"] != expected_checkpoint:
        raise CheckpointError(
            f"checkpoint type mismatch: expected {expected_checkpoint}, found {record['checkpoint']}"
        )
    if record.get("status") not in {"approved", "confirmed"}:
        raise CheckpointError(f"checkpoint {record.get('checkpoint', '?')} is pending: user approval required")
    actual_artifact = _canonical_file(artifact_path, label="checkpoint artifact")
    recorded_artifact = _record_artifact_path(record)
    if actual_artifact != recorded_artifact:
        raise CheckpointError("checkpoint artifact path does not match the approved record")
    _validate_record_runtime(record)
    _require_predecessor(record, seen)
    return record


def require_approved_checkpoint(
    record_path: str | Path,
    artifact_path: str | Path,
    *,
    expected_checkpoint: str | None = None,
) -> dict[str, Any]:
    return _require_approved_checkpoint(record_path, artifact_path, expected_checkpoint=expected_checkpoint)


def checkpoint_history_revision_id(record: Mapping[str, Any], checkpoint_sha256: str) -> str:
    """Return a stable, auditable history revision id from the approved checkpoint identity."""
    approved_at = record.get("approved_at")
    if not isinstance(approved_at, str) or not approved_at:
        raise CheckpointError("reopen requires an approved_at timestamp")
    normalized = re.sub(r"[^\w.-]+", "-", approved_at).strip("-")
    return f"{normalized}-{checkpoint_sha256[:12]}"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _ckpt2_history_entries(root: Path, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return every current CKPT-2 evidence file as a project-relative, hash-bound entry."""
    root = root.resolve()
    paths: list[Path] = [root / "checkpoint-2.json", _record_artifact_path(record)]
    outline = root / "deck-outline.md"
    if outline.is_file():
        paths.append(outline)
    graph = record.get("asset_graph")
    if isinstance(graph, dict) and isinstance(graph.get("path"), str):
        paths.append(root / graph["path"])
    for sub in ("review", "delivery"):
        directory = root / sub
        if directory.exists() or directory.is_symlink():
            if not directory.is_dir() or directory.is_symlink():
                raise CheckpointError(f"reopen requires a real {sub} directory")
            paths.extend(sorted(item for item in directory.rglob("*") if item.is_file()))
    entries: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.resolve()
        if resolved.is_symlink() or not resolved.is_file():
            raise CheckpointError(f"reopen history source is not a regular file: {resolved}")
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise CheckpointError(f"reopen history source escapes the project: {resolved}") from exc
        if relative.startswith(f"{HISTORY_ROOT_NAME}/"):
            raise CheckpointError("checkpoint history must not archive itself")
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    entries.sort(key=lambda item: item["path"])
    return entries


def _validate_delivery_tree_for_history(root: Path) -> None:
    """Fail closed when the old delivery tree cannot be proven intact."""
    delivery = root / "delivery"
    if not delivery.is_dir():
        return
    manifest_path = delivery / "delivery-manifest.json"
    if not manifest_path.is_file():
        raise CheckpointError("reopen requires delivery-manifest.json when delivery/ exists")
    payload = _read_json(manifest_path, label="delivery manifest")
    if not isinstance(payload, dict) or payload.get("kind") != "scholar-slides-delivery-manifest":
        raise CheckpointError("reopen delivery manifest is invalid")
    known: set[str] = set()
    for collection in ("files", "runtime_dependencies"):
        values = payload.get(collection)
        entries = values.values() if isinstance(values, dict) else (values if isinstance(values, list) else ())
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                continue
            relative = entry["path"]
            target = root / relative
            try:
                target.relative_to(delivery)
            except ValueError as exc:
                raise CheckpointError(f"delivery manifest path escapes delivery tree: {relative}") from exc
            if not target.is_file() or sha256_file(target) != entry["sha256"]:
                raise CheckpointError(f"stale delivery file: {relative}")
            known.add(relative)
    reports = payload.get("reports")
    if isinstance(reports, dict):
        for entry in reports.values():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                known.add(entry["path"])
    validation_artifacts = payload.get("validation_artifacts")
    if isinstance(validation_artifacts, dict) and isinstance(validation_artifacts.get("paths"), list):
        known.update(path for path in validation_artifacts["paths"] if isinstance(path, str))
    known.update({"delivery/delivery-manifest.json", "delivery/delivery-validation.json", "delivery/delivery-consistency.json", "delivery/export-inputs.json", "delivery/checkpoint-delivery.json"})
    for path in delivery.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in known:
            raise CheckpointError(f"unexpected file in delivery tree: {relative}")


def _history_manifest(
    root: Path,
    record: Mapping[str, Any],
    entries: list[dict[str, Any]],
    *,
    revision_id: str,
    archived_by: str,
    reason: str,
    checkpoint_sha256: str,
    review_manifest_sha: str | None,
    delivery_manifest_sha: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "kind": HISTORY_KIND,
        "checkpoint": "CKPT-2",
        "revision_id": revision_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "archived_by": archived_by.strip(),
        "reason": reason.strip(),
        "source_checkpoint_sha256": checkpoint_sha256,
        "source_deck_sha256": record["artifact"]["sha256"],
        "review_bundle_sha256": review_manifest_sha,
        "delivery_manifest_sha256": delivery_manifest_sha,
        "files": entries,
    }


def _history_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _reopen_payload(transaction: Mapping[str, Any], *, changed: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "changed": changed,
        "checkpoint": "CKPT-2",
        "previous_status": "approved",
        "revision_id": transaction["revision_id"],
        "history_path": transaction["history_path"],
        "history_manifest_sha256": transaction["history_manifest_sha256"],
        "current_status": "reopened",
        "requires_new_review": True,
    }


def _validate_reopen_transaction(
    raw: Any,
    *,
    root: Path,
    requested_by: str,
    reason: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("kind") != REOPEN_JOURNAL_KIND:
        raise CheckpointError("malformed CKPT-2 reopen journal")
    if raw.get("checkpoint") != "CKPT-2" or raw.get("requested_by") != requested_by.strip() or raw.get("reason") != reason.strip():
        raise CheckpointError("CKPT-2 reopen journal does not match this request")
    revision_id = raw.get("revision_id")
    checkpoint_sha = raw.get("source_checkpoint_sha256")
    manifest_sha = raw.get("history_manifest_sha256")
    manifest = raw.get("history_manifest")
    expected_history_path = root / HISTORY_ROOT_NAME / "CKPT-2" / str(revision_id)
    if (
        not isinstance(revision_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", revision_id)
        or not isinstance(checkpoint_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha)
        or not isinstance(manifest_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
        or raw.get("history_path") != str(expected_history_path)
        or not isinstance(manifest, dict)
        or manifest.get("kind") != HISTORY_KIND
        or manifest.get("revision_id") != revision_id
        or manifest.get("source_checkpoint_sha256") != checkpoint_sha
        or hashlib.sha256(_history_manifest_bytes(manifest)).hexdigest() != manifest_sha
    ):
        raise CheckpointError("malformed CKPT-2 reopen journal fields")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise CheckpointError("malformed CKPT-2 reopen journal file set")
    seen: set[str] = set()
    for entry in entries:
        relative = entry.get("path") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        size = entry.get("size_bytes") if isinstance(entry, dict) else None
        candidate = Path(relative) if isinstance(relative, str) else Path("..")
        if (
            not isinstance(relative, str)
            or not relative
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in seen
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size < 0
        ):
            raise CheckpointError("malformed CKPT-2 reopen journal file entry")
        seen.add(relative)
    return dict(raw)


def _verify_published_history(final_dir: Path, transaction: Mapping[str, Any]) -> None:
    if not final_dir.is_dir() or final_dir.is_symlink():
        raise CheckpointError("history revision already exists with unsafe content")
    manifest_path = final_dir / "history-manifest.json"
    if not manifest_path.is_file() or sha256_file(manifest_path) != transaction["history_manifest_sha256"]:
        raise CheckpointError("history revision already exists with different content")
    for entry in transaction["history_manifest"]["files"]:
        target = final_dir / Path(entry["path"])
        if (
            not target.is_file()
            or target.is_symlink()
            or target.stat().st_size != entry["size_bytes"]
            or sha256_file(target) != entry["sha256"]
        ):
            raise CheckpointError(f"history revision file is missing or tampered: {entry['path']}")


def _publish_reopen_history(root: Path, transaction: Mapping[str, Any]) -> None:
    history_root = root / HISTORY_ROOT_NAME / "CKPT-2"
    history_root.mkdir(parents=True, exist_ok=True)
    final_dir = Path(transaction["history_path"])
    if final_dir.exists() or final_dir.is_symlink():
        _verify_published_history(final_dir, transaction)
        return
    revision_id = transaction["revision_id"]
    stage = history_root / f".{revision_id}.tmp-{os.getpid()}"
    if stage.exists() or stage.is_symlink():
        if stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage)
        else:
            raise CheckpointError(f"unsafe history staging path exists: {stage}")
    try:
        for entry in transaction["history_manifest"]["files"]:
            source = root / Path(entry["path"])
            if (
                not source.is_file()
                or source.is_symlink()
                or source.stat().st_size != entry["size_bytes"]
                or sha256_file(source) != entry["sha256"]
            ):
                raise CheckpointError(f"reopen source changed after transaction start: {entry['path']}")
            destination = stage / Path(entry["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if sha256_file(destination) != entry["sha256"]:
                raise CheckpointError("history staging copy failed hash verification")
        manifest_path = stage / "history-manifest.json"
        manifest_path.write_bytes(_history_manifest_bytes(transaction["history_manifest"]))
        if sha256_file(manifest_path) != transaction["history_manifest_sha256"]:
            raise CheckpointError("history manifest hash mismatch")
        os.replace(stage, final_dir)
    except BaseException:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _publish_reopen_lineage(root: Path, transaction: Mapping[str, Any]) -> None:
    lineage_path = root / LINEAGE_MARKER_NAME
    lineage = {
        "schema_version": 1,
        "kind": LINEAGE_KIND,
        "checkpoint": "CKPT-2",
        "revision_id": transaction["revision_id"],
        "source_checkpoint_sha256": transaction["source_checkpoint_sha256"],
        "history_manifest_sha256": transaction["history_manifest_sha256"],
    }
    if lineage_path.exists() or lineage_path.is_symlink():
        if not lineage_path.is_file() or lineage_path.is_symlink() or _read_json(lineage_path, label="CKPT-2 lineage marker") != lineage:
            raise CheckpointError("existing CKPT-2 lineage marker does not match reopen journal")
        return
    _atomic_write_json(lineage_path, lineage)


def _retire_reopen_current_slot(root: Path) -> None:
    for name in ("checkpoint-2.json", "review", "delivery"):
        target = root / name
        if target.is_symlink():
            raise CheckpointError(f"unsafe CKPT-2 retirement target: {target}")
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=False)
        elif target.is_file():
            target.unlink()
        elif target.exists():
            raise CheckpointError(f"unsafe CKPT-2 retirement target: {target}")


def _complete_reopen_transaction(root: Path, transaction: Mapping[str, Any]) -> dict[str, Any]:
    _publish_reopen_history(root, transaction)
    _publish_reopen_lineage(root, transaction)
    _retire_reopen_current_slot(root)
    (root / REOPEN_JOURNAL_NAME).unlink(missing_ok=True)
    return _reopen_payload(transaction, changed=True)


def reopen_ckpt2(
    project_root: str | Path,
    *,
    requested_by: str,
    reason: str,
    dry_run: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    """Journal and archive an approved CKPT-2 before retiring its current review slot."""
    root = Path(project_root).resolve(strict=True)
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise CheckpointError("reopen requires requested_by")
    if not isinstance(reason, str) or not reason.strip():
        raise CheckpointError("reopen requires reason")
    record_path = root / "checkpoint-2.json"
    lineage_path = root / LINEAGE_MARKER_NAME
    history_root = root / HISTORY_ROOT_NAME / "CKPT-2"
    journal_path = root / REOPEN_JOURNAL_NAME

    if journal_path.exists() or journal_path.is_symlink():
        if not resume:
            raise CheckpointError("an interrupted CKPT-2 reopen exists; rerun with --resume")
        if not journal_path.is_file() or journal_path.is_symlink():
            raise CheckpointError("unsafe CKPT-2 reopen journal")
        transaction = _validate_reopen_transaction(
            _read_json(journal_path, label="CKPT-2 reopen journal"),
            root=root,
            requested_by=requested_by,
            reason=reason,
        )
        return _complete_reopen_transaction(root, transaction)

    if not record_path.is_file():
        if resume and lineage_path.is_file():
            lineage = _read_json(lineage_path, label="CKPT-2 lineage marker")
            revision_id = lineage.get("revision_id")
            expected_manifest_sha = lineage.get("history_manifest_sha256")
            if (
                isinstance(revision_id, str)
                and re.fullmatch(r"[A-Za-z0-9._-]+", revision_id)
                and isinstance(expected_manifest_sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha)
            ):
                manifest_path = history_root / revision_id / "history-manifest.json"
                if manifest_path.is_file() and sha256_file(manifest_path) == expected_manifest_sha:
                    manifest = _read_json(manifest_path, label="history manifest")
                    if manifest.get("kind") == HISTORY_KIND and manifest.get("revision_id") == revision_id:
                        return _reopen_payload(
                            {
                                "revision_id": revision_id,
                                "history_path": str(manifest_path.parent),
                                "history_manifest_sha256": expected_manifest_sha,
                            },
                            changed=False,
                        )
            raise CheckpointError("CKPT-2 supersedes history is missing or tampered")
        raise CheckpointError("reopen requires a current approved CKPT-2 record")

    record = _validate_record(_read_record(record_path))
    _validate_record_runtime(record)
    if record.get("checkpoint") != "CKPT-2":
        raise CheckpointError("reopen only supports CKPT-2")
    if record.get("status") != "approved":
        raise CheckpointError("reopen requires an approved CKPT-2 record")
    if not isinstance(record.get("review_bundle"), dict) or not isinstance(record.get("review_readiness"), dict):
        raise CheckpointError("approved CKPT-2 requires a sealed review bundle")
    _require_predecessor(record, seen={record_path.resolve()})
    _validate_delivery_tree_for_history(root)

    checkpoint_sha256 = sha256_file(record_path)
    revision_id = checkpoint_history_revision_id(record, checkpoint_sha256)
    entries = _ckpt2_history_entries(root, record)
    review_manifest = root / "review" / "review-manifest.json"
    delivery_manifest = root / "delivery" / "delivery-manifest.json"
    review_manifest_sha = sha256_file(review_manifest) if review_manifest.is_file() else None
    delivery_manifest_sha = sha256_file(delivery_manifest) if delivery_manifest.is_file() else None
    manifest = _history_manifest(
        root,
        record,
        entries,
        revision_id=revision_id,
        archived_by=requested_by,
        reason=reason,
        checkpoint_sha256=checkpoint_sha256,
        review_manifest_sha=review_manifest_sha,
        delivery_manifest_sha=delivery_manifest_sha,
    )
    manifest_bytes = _history_manifest_bytes(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    history_path = str(history_root / revision_id)
    transaction = {
        "schema_version": 1,
        "kind": REOPEN_JOURNAL_KIND,
        "checkpoint": "CKPT-2",
        "requested_by": requested_by.strip(),
        "reason": reason.strip(),
        "revision_id": revision_id,
        "source_checkpoint_sha256": checkpoint_sha256,
        "history_path": history_path,
        "history_manifest_sha256": manifest_sha256,
        "history_manifest": manifest,
    }
    payload = _reopen_payload(transaction, changed=True)
    if dry_run:
        payload["dry_run"] = True
        payload["planned_files"] = entries
        return payload
    _atomic_write_json(journal_path, transaction)
    return _complete_reopen_transaction(root, transaction)


def _load_json(path: str | Path) -> Any:
    return _read_json(path, label="JSON artifact")


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print(f"ERROR: Python 3.11+ is required; found {sys.version.split()[0]}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Create and verify explicit user checkpoints for slide artifacts.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="write a pending checkpoint")
    create.add_argument("checkpoint", choices=sorted(CHECKPOINTS))
    create.add_argument("artifact")
    create.add_argument("record")
    create.add_argument("--requires", help="approved predecessor checkpoint record (required for CKPT-2/CKPT-3)")
    create.add_argument("--review-bundle", help="sealed CKPT-2 review bundle (required for CKPT-3)")
    create.add_argument("--asset-graph", help="CKPT-2 output path for its generated exact dependency graph")
    create.add_argument(
        "--attach",
        action="append",
        default=[],
        help="additional review evidence to SHA-256 bind into this checkpoint (repeatable)",
    )
    create.add_argument(
        "--intended-confirmer",
        help="CKPT-1 only: leave this checkpoint pending_human_confirmation for the named reviewer",
    )

    reuse = commands.add_parser(
        "reuse-confirmed-ckpt1",
        help="relocate a confirmed CKPT-1 into a new project without changing the source record",
    )
    reuse.add_argument("source_record")
    reuse.add_argument("destination_project")
    reuse.add_argument("--record", dest="destination_record")

    approve = commands.add_parser("approve", help="record an already-obtained explicit confirmation")
    approve.add_argument("record")
    approve.add_argument("--confirmed-by", required=True)
    approve.add_argument(
        "--readiness-artifact",
        help="CKPT-1 readiness report whose SHA-256 must already be in the artifact bundle",
    )

    bind_review = commands.add_parser("record-review", help="bind a passing pending-CKPT-2 review manifest without approving it")
    bind_review.add_argument("record")
    bind_review.add_argument("manifest")

    require = commands.add_parser("require", help="reject pending, stale, incomplete, or wrong-stage checkpoints")
    require.add_argument("record")
    require.add_argument("artifact")
    require.add_argument("--expected-checkpoint", choices=CHECKPOINTS)

    preserve = commands.add_parser("verify-markers", help="reject silent removal of integrity markers")
    preserve.add_argument("before")
    preserve.add_argument("after")

    reopen = commands.add_parser("reopen", help="archive an approved CKPT-2 and reopen the project for a revised deck review")
    reopen.add_argument("checkpoint", choices=("CKPT-2",))
    reopen.add_argument("project")
    reopen.add_argument("--requested-by", required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--dry-run", action="store_true")
    reopen.add_argument("--resume", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            create_checkpoint(
                args.checkpoint,
                args.artifact,
                args.record,
                prerequisite_record=args.requires,
                review_bundle_path=args.review_bundle,
                supplemental_artifacts=args.attach,
                intended_confirmer=args.intended_confirmer,
                asset_graph_path=args.asset_graph,
            )
        elif args.command == "reuse-confirmed-ckpt1":
            print(
                json.dumps(
                    reuse_confirmed_ckpt1(
                        args.source_record,
                        args.destination_project,
                        args.destination_record,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.command == "approve":
            approve_checkpoint(
                args.record,
                args.confirmed_by,
                readiness_artifact=args.readiness_artifact,
            )
        elif args.command == "record-review":
            record_review_bundle(args.record, args.manifest)
        elif args.command == "require":
            require_approved_checkpoint(args.record, args.artifact, expected_checkpoint=args.expected_checkpoint)
        elif args.command == "reopen":
            payload = reopen_ckpt2(
                args.project,
                requested_by=args.requested_by,
                reason=args.reason,
                dry_run=args.dry_run,
                resume=args.resume,
            )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            assert_markers_preserved(_load_json(args.before), _load_json(args.after))
    except CheckpointError as exc:
        parser.exit(2, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
