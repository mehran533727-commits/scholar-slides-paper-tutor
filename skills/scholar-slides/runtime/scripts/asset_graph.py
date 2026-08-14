"""Build a deterministic, fail-closed graph of the exact inputs used by a deck."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
KIND = "scholar-slides-asset-graph"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_LOGICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MEDIA_TYPES = {".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".gif":"image/gif", ".webp":"image/webp", ".svg":"image/svg+xml", ".mp4":"video/mp4", ".webm":"video/webm", ".pdf":"application/pdf", ".json":"application/json", ".md":"text/markdown", ".txt":"text/plain"}
_NODE_KINDS = {"upstream_digest", "source_pdf", "visible_asset", "audit_record", "audit_crop"}
_EDGE_RELATIONS = {"sources", "declares", "renders", "audits", "evidences"}


class AssetGraphError(RuntimeError):
    """A graph input is unsafe, stale, or insufficiently bound."""

    def __init__(self, finding: dict[str, Any]) -> None:
        self.finding = finding
        super().__init__(
            f"{finding['code']}: {finding['message']}"
            + (f" ({finding['json_pointer']})" if finding.get("json_pointer") else "")
        )


def _fail(code: str, message: str, *, pointer: str = "", path: str | None = None, logical_id: str | None = None) -> None:
    finding = {
        "code": code,
        "severity": "error",
        "message": message,
        "json_pointer": pointer,
        "path": path,
        "logical_id": logical_id,
        "suggested_action": "Use a project-root relative regular file that is actually referenced by the deck.",
    }
    raise AssetGraphError(finding)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_open_fd(fd: int) -> tuple[str, os.stat_result]:
    """Hash one opened regular-file handle and return its final metadata.

    Opening with ``O_NOFOLLOW`` and hashing the same descriptor closes the most
    useful pathname replacement race.  A malicious writer can still mutate an
    already-open file in place; the caller therefore compares metadata before
    and after and treats any detectable change as unsafe.
    """
    digest = hashlib.sha256()
    before = os.fstat(fd)
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    ) or not stat.S_ISREG(after.st_mode):
        _fail("asset-hash-mismatch", "file changed while its content hash was calculated")
    return digest.hexdigest(), after


def _reject_symlink_input(path: Path, *, label: str) -> None:
    target = path.absolute()
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            _fail("asset-path-symlink-escape", f"{label} may not be a symbolic link or reparse point", path=str(path))


def _portable_relative(raw: str, *, pointer: str) -> Path:
    if not isinstance(raw, str) or not raw or raw.startswith("file:") or raw.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(raw):
        _fail("asset-path-absolute", "asset path must be project-root relative", pointer=pointer, path=raw)
    portable = raw.replace("\\", "/")
    parts = tuple(part for part in portable.split("/") if part not in {"", "."})
    if not parts or ".." in parts:
        _fail("asset-path-traversal", "asset path must not traverse above the project root", pointer=pointer, path=raw)
    return Path(*parts)


def _safe_file(root: Path, raw: str, *, pointer: str, kind: str) -> tuple[Path, str, os.stat_result]:
    relative = _portable_relative(raw, pointer=pointer)
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            _fail("asset-file-missing", f"{kind} does not exist", pointer=pointer, path=raw)
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            _fail("asset-path-symlink-escape", f"{kind} path may not contain a symbolic link or reparse point", pointer=pointer, path=raw)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        _fail("asset-file-missing", f"{kind} does not exist", pointer=pointer, path=raw)
    except OSError as exc:
        _fail("asset-path-outside-root", f"cannot resolve {kind}: {exc}", pointer=pointer, path=raw)
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("asset-path-symlink-escape", f"{kind} resolves outside the project root", pointer=pointer, path=raw)
    try:
        before = resolved.stat()
    except OSError:
        _fail("asset-file-missing", f"{kind} does not exist", pointer=pointer, path=raw)
    if not stat.S_ISREG(before.st_mode):
        _fail("asset-file-not-regular", f"{kind} must be a regular file", pointer=pointer, path=raw)
    return resolved, relative.as_posix(), before


def _entry(root: Path, raw: str, *, pointer: str, kind: str, node_kind: str, source_pointers: list[str] | None = None) -> dict[str, Any]:
    resolved, portable, before = _safe_file(root, raw, pointer=pointer, kind=kind)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(resolved, flags)
    except OSError as exc:
        _fail("asset-file-missing", f"cannot open {kind}: {exc}", pointer=pointer, path=raw)
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
        ) or not stat.S_ISREG(opened.st_mode):
            _fail("asset-hash-mismatch", f"{kind} changed before its content hash was calculated", pointer=pointer, path=raw)
        sha256, after = _sha256_open_fd(fd)
    except AssetGraphError:
        raise
    finally:
        os.close(fd)
    entry: dict[str, Any] = {
        "id": f"{node_kind}:{portable}",
        "kind": node_kind,
        "path": portable,
        "sha256": sha256,
        "size_bytes": after.st_size,
        "media_type": _MEDIA_TYPES.get(Path(portable).suffix.lower(), "application/octet-stream"),
    }
    if source_pointers:
        entry["source_pointers"] = sorted(source_pointers)
    return entry


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("asset-reference-unknown", f"cannot read {label}: {exc}")
    if not isinstance(value, dict):
        _fail("asset-reference-unknown", f"{label} must be a JSON object")
    return value


def _pointer(parent: str, token: str | int) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def _visible_references(deck: dict[str, Any]) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    slides = deck.get("slides")
    if not isinstance(slides, list):
        return references
    for index, slide in enumerate(slides):
        pointer = f"/slides/{index}"
        if not isinstance(slide, dict):
            continue
        contexts: list[tuple[Any, str, set[str]]] = []
        if "figure" in slide:
            contexts.append((slide.get("figure"), f"{pointer}/figure", {"src"}))
        if "media" in slide:
            contexts.append((slide.get("media"), f"{pointer}/media", {"src", "poster"}))
        if "background" in slide:
            contexts.append((slide.get("background"), f"{pointer}/background", {"__value__"}))
        if "images" in slide:
            images = slide.get("images")
            if isinstance(images, list):
                for i, image in enumerate(images):
                    contexts.append((image, f"{pointer}/images/{i}", {"asset"}))
            elif images is not None:
                _fail("asset-reference-malformed", "renderer images must be an array", pointer=f"{pointer}/images")
        for value, context_pointer, keys in contexts:
            if "__value__" in keys:
                if not isinstance(value, str) or not value:
                    _fail("asset-reference-malformed", "renderer background must be a non-empty string", pointer=context_pointer)
                # Keep the graph's URI identity aligned with the renderer's
                # normalizeResourceUri semantics (dot/duplicate segments are
                # not distinct resources).
                canonical = _portable_relative(value.replace("\\", "/"), pointer=context_pointer).as_posix()
                references.setdefault(canonical, []).append(context_pointer)
                continue
            if not isinstance(value, dict):
                _fail("asset-reference-malformed", "renderer media/figure entry must be an object", pointer=context_pointer)
            for key in keys:
                if key not in value:
                    continue
                raw = value[key]
                if not isinstance(raw, str) or not raw:
                    _fail("asset-reference-malformed", f"renderer {key} must be a non-empty string", pointer=f"{context_pointer}/{key}")
                ref_pointer = f"{context_pointer}/{key}"
                canonical = _portable_relative(raw.replace("\\", "/"), pointer=ref_pointer).as_posix()
                references.setdefault(canonical, []).append(ref_pointer)
    for pointers in references.values():
        pointers[:] = sorted(set(pointers))
    return references


def _has_native_table(deck: dict[str, Any]) -> bool:
    return any(isinstance(slide, dict) and isinstance(slide.get("table"), dict) for slide in deck.get("slides", []))


def _audit_nodes(root: Path, audit_paths: Sequence[Path], *, has_native_table: bool) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not has_native_table:
        return [], []
    if not audit_paths:
        _fail("asset-audit-binding-missing", "native table requires an explicitly bound audit record")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for audit_path in audit_paths:
        try:
            relative = os.path.relpath(audit_path, root).replace(os.sep, "/")
            _portable_relative(relative, pointer="/audit_paths")
        except (OSError, ValueError):
            _fail("asset-path-outside-root", "audit record must be inside the project root", path=str(audit_path))
        audit_node = _entry(root, relative, pointer="/audit_paths", kind="audit record", node_kind="audit_record")
        audit = _read_json(root / relative, label="audit record")
        asset = audit.get("asset")
        crop = audit.get("crop") if isinstance(audit.get("crop"), dict) else None
        if crop is None:
            crop = asset.get("crop") if isinstance(asset, dict) else None
        crop_path = crop.get("path") if isinstance(crop, dict) else None
        expected_hash = crop.get("sha256") if isinstance(crop, dict) else None
        if not isinstance(crop_path, str) or not isinstance(expected_hash, str):
            _fail("asset-audit-binding-missing", "audit record must bind crop.path and crop.sha256", pointer="/asset/crop")
        crop_node = _entry(root, crop_path, pointer="/asset/crop/path", kind="audit crop", node_kind="audit_crop")
        if crop_node["sha256"] != expected_hash:
            _fail("asset-audit-binding-mismatch", "audit crop hash does not match the referenced crop", pointer="/asset/crop/sha256", path=crop_path)
        nodes.extend((audit_node, crop_node))
        edges.append({"from": audit_node["id"], "to": crop_node["id"], "relation": "evidences"})
    return nodes, edges


def build_asset_graph(
    deck_path: Path,
    digest_path: Path,
    audit_paths: Sequence[Path],
    *,
    forbidden_assets: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the stable graph of only deck-visible and audit-required inputs."""
    _reject_symlink_input(Path(deck_path), label="deck")
    _reject_symlink_input(Path(digest_path), label="upstream digest")
    deck_real = Path(deck_path).resolve(strict=True)
    root = deck_real.parent.resolve()
    deck = _read_json(deck_real, label="deck")
    deck_entry = _entry(root, deck_real.name, pointer="", kind="deck", node_kind="deck")
    digest_real = Path(digest_path).resolve(strict=True)
    digest = _read_json(digest_real, label="digest")
    try:
        digest_relative = digest_real.relative_to(root).as_posix()
    except ValueError:
        _fail("asset-path-outside-root", "upstream digest must be inside the project root", path=str(digest_path))
    digest_node = _entry(root, digest_relative, pointer="/upstream/digest", kind="upstream digest", node_kind="upstream_digest")
    nodes = [digest_node]
    edges = [{"from": deck_entry["id"], "to": digest_node["id"], "relation": "sources"}]
    source = digest.get("source")
    if isinstance(source, dict) and ("pdf" in source or "pdf_sha256" in source):
        source_path = source.get("pdf")
        expected = source.get("pdf_sha256")
        if not isinstance(source_path, str) or not source_path or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            _fail("asset-source-binding-mismatch", "digest source.pdf and source.pdf_sha256 must be valid", pointer="/source")
        source_node = _entry(root, source_path, pointer="/source/pdf", kind="source PDF", node_kind="source_pdf")
        if source_node["sha256"] != expected:
            _fail("asset-source-binding-mismatch", "source PDF hash does not match digest declaration", pointer="/source/pdf_sha256", path=source_path)
        nodes.append(source_node)
        edges.append({"from": digest_node["id"], "to": source_node["id"], "relation": "declares"})
    raw_forbidden = list(forbidden_assets)
    if any(not isinstance(asset, str) or not asset for asset in raw_forbidden):
        _fail("asset-policy-malformed", "forbidden asset logical IDs must be non-empty strings")
    forbidden_ids = sorted(set(raw_forbidden))
    for asset_id in forbidden_ids:
        if not _LOGICAL_ID.fullmatch(asset_id):
            _fail("asset-policy-malformed", "forbidden asset logical ID is malformed", logical_id=asset_id)
    forbidden_hashes: dict[str, str] = {}
    for asset_id in forbidden_ids:
        candidate = root / "figures" / f"{asset_id}.png"
        if candidate.is_file() and not candidate.is_symlink():
            forbidden_hashes[_sha256(candidate.resolve())] = asset_id
    for raw, pointers in _visible_references(deck).items():
        node = _entry(root, raw, pointer=pointers[0], kind="visible asset", node_kind="visible_asset", source_pointers=pointers)
        logical_id = Path(node["path"]).stem
        forbidden_id = logical_id if logical_id in forbidden_ids else forbidden_hashes.get(node["sha256"])
        if forbidden_id:
            _fail(
                "asset-reference-forbidden",
                "deck references an asset forbidden by the sealed CKPT-1 ledger",
                pointer=pointers[0],
                path=node["path"],
                logical_id=forbidden_id,
            )
        nodes.append(node)
    audit_nodes, audit_edges = _audit_nodes(root, audit_paths, has_native_table=_has_native_table(deck))
    nodes.extend(audit_nodes)
    edges.extend(audit_edges)
    if audit_nodes:
        for audit_node in (node for node in audit_nodes if node["kind"] == "audit_record"):
            edges.append({"from": deck_entry["id"], "to": audit_node["id"], "relation": "audits"})
    for node in (node for node in nodes if node["kind"] == "visible_asset"):
        edges.append({"from": deck_entry["id"], "to": node["id"], "relation": "renders"})
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "project_root": ".",
        "deck": {"path": deck_entry["path"], "sha256": deck_entry["sha256"]},
        "nodes": sorted(nodes, key=lambda node: (node["path"], node["kind"], node["id"])),
        "edges": sorted(edges, key=lambda edge: (edge["from"], edge["to"], edge["relation"])),
        "forbidden_assets": forbidden_ids,
        "unresolved": [],
    }


def bundle_from_asset_graph(graph: Mapping[str, Any]) -> list[Path]:
    """Return project-relative files in canonical bundle order without directory expansion."""
    if not isinstance(graph, Mapping) or graph.get("schema_version") != SCHEMA_VERSION or graph.get("kind") != KIND:
        _fail("asset-graph-schema", "unsupported asset graph schema")
    deck = graph.get("deck")
    if not isinstance(deck, Mapping) or set(deck) != {"path", "sha256"} or not isinstance(deck.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(deck.get("sha256", ""))):
        _fail("asset-graph-schema", "asset graph requires deck.path and deck.sha256")
    deck_path = _portable_relative(deck["path"], pointer="/deck/path")
    if set(graph) != {"schema_version", "kind", "project_root", "deck", "nodes", "edges", "forbidden_assets", "unresolved"}:
        _fail("asset-graph-schema", "asset graph has unknown or missing top-level fields")
    if graph.get("project_root") != ".":
        _fail("asset-graph-schema", "asset graph project_root must be '.'")
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        _fail("asset-graph-schema", "asset graph requires nodes and edges arrays")
    forbidden = graph.get("forbidden_assets")
    unresolved = graph.get("unresolved")
    if not isinstance(forbidden, list) or not all(isinstance(item, str) and _LOGICAL_ID.fullmatch(item) for item in forbidden) or forbidden != sorted(set(forbidden)):
        _fail("asset-graph-schema", "asset graph forbidden_assets policy is invalid")
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        _fail("asset-graph-schema", "asset graph policy fields must be arrays")
    paths = [deck_path]
    node_ids = {f"deck:{deck_path.as_posix()}"}
    seen_paths: set[str] = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping) or not all(key in node for key in ("id", "kind", "path", "sha256", "size_bytes", "media_type")):
            _fail("asset-graph-schema", "asset graph node is incomplete", pointer=f"/nodes/{index}")
        if not all(isinstance(node.get(key), str) for key in ("id", "kind", "path", "media_type")) or node["kind"] not in _NODE_KINDS:
            _fail("asset-graph-schema", "asset graph node fields have invalid types", pointer=f"/nodes/{index}")
        expected_keys = {"id", "kind", "path", "sha256", "size_bytes", "media_type"}
        if node["kind"] == "visible_asset":
            expected_keys.add("source_pointers")
        if set(node) != expected_keys:
            _fail("asset-graph-schema", "asset graph node has unknown or missing fields", pointer=f"/nodes/{index}")
        relative = _portable_relative(node["path"], pointer=f"/nodes/{index}/path")
        if not re.fullmatch(r"[0-9a-f]{64}", str(node["sha256"])) or not isinstance(node["size_bytes"], int) or isinstance(node["size_bytes"], bool) or node["size_bytes"] < 0:
            _fail("asset-graph-schema", "asset graph node identity is invalid", pointer=f"/nodes/{index}")
        if node["id"] in node_ids or node["id"] != f"{node['kind']}:{relative.as_posix()}":
            _fail("asset-graph-schema", "asset graph node ID is invalid or duplicated", pointer=f"/nodes/{index}/id")
        node_ids.add(node["id"])
        if relative.as_posix() in seen_paths:
            _fail("asset-graph-schema", "asset graph contains duplicate node paths", pointer=f"/nodes/{index}/path")
        seen_paths.add(relative.as_posix())
        if "source_pointers" in node:
            pointers = node["source_pointers"]
            if (not isinstance(pointers, list) or not all(isinstance(item, str) and item.startswith("/") for item in pointers)
                    or pointers != sorted(set(pointers))):
                _fail("asset-graph-schema", "asset graph source_pointers are invalid", pointer=f"/nodes/{index}/source_pointers")
        paths.append(relative)
    if nodes != sorted(nodes, key=lambda node: (node["path"], node["kind"], node["id"])):
        _fail("asset-graph-schema", "asset graph nodes must use canonical order")
    if not any(node.get("kind") == "upstream_digest" for node in nodes):
        _fail("asset-graph-schema", "asset graph requires an upstream_digest node")
    if len({node["id"] for node in nodes}) != len(nodes):
        _fail("asset-graph-schema", "asset graph node IDs must be unique")
    has_upstream_edge = False
    for index, edge in enumerate(edges):
        if not isinstance(edge, Mapping) or set(edge) != {"from", "to", "relation"} or not all(isinstance(edge.get(key), str) for key in ("from", "to", "relation")) or edge.get("relation") not in _EDGE_RELATIONS or edge["from"] not in node_ids or edge["to"] not in node_ids:
            _fail("asset-graph-schema", "asset graph edge is invalid", pointer=f"/edges/{index}")
        if edge["from"] == f"deck:{deck_path.as_posix()}" and edge["relation"] == "sources" and edge["to"].startswith("upstream_digest:"):
            has_upstream_edge = True
    edge_keys = [(edge["from"], edge["to"], edge["relation"]) for edge in edges]
    if len(set(edge_keys)) != len(edge_keys):
        _fail("asset-graph-schema", "asset graph edges must be unique")
    if edge_keys != sorted(edge_keys):
        _fail("asset-graph-schema", "asset graph edges must use canonical order")
    if not has_upstream_edge:
        _fail("asset-graph-schema", "asset graph requires deck to source upstream digest")
    return sorted(dict.fromkeys(paths), key=lambda path: (path != deck_path, path.as_posix()))
