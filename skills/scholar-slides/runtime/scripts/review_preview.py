#!/usr/bin/env python3
"""Portable, fail-closed utilities for pending-CKPT-2 review previews."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aesthetics_qa import build_aesthetics_report, validate_aesthetics_report
from checkpoint import CheckpointError, _pending_ckpt2_for_review_rebind, _record_artifact_path, _read_record, _validate_record, record_review_bundle, require_approved_checkpoint
from ckpt1_resolved import CKPT1ResolvedViewError, build_confirmed_semantic_digest
from montage import build_montage
from quantitative_coverage import (
    QuantitativeCoverageError,
    build_coverage_artifact,
    collect_quantitative_requirements,
    load_coverage_artifact,
)
from semantic_qa import evaluate_semantic_qa, semantic_qa_is_current
from visual_qa import evaluate_visual_qa


SCHEMA_VERSION = 1
KIND = "scholar-slides-review-manifest"


class ReviewPreviewError(RuntimeError):
    """A review preview is missing, unsafe, or stale."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_file(project_root: Path, candidate: Path, *, label: str) -> tuple[Path, str]:
    root = project_root.resolve(strict=True)
    lexical = candidate if candidate.is_absolute() else root / candidate
    try:
        relative_lexical = lexical.absolute().relative_to(root)
    except ValueError as exc:
        raise ReviewPreviewError(f"{label} must be a regular file below the project root") from exc
    current = root
    for part in relative_lexical.parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ReviewPreviewError(f"{label} must be a regular file below the project root") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise ReviewPreviewError(f"{label} may not traverse a symbolic link or reparse point")
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReviewPreviewError(f"{label} must be a regular file below the project root") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ReviewPreviewError(f"{label} must be a regular file")
    return resolved, relative.as_posix()


def _entry(project_root: Path, candidate: Path, *, label: str) -> dict[str, str]:
    path, portable = _project_file(project_root, candidate, label=label)
    return {"path": portable, "sha256": _sha256(path)}


def build_review_manifest(
    project_root: Path,
    deck_path: Path,
    asset_graph_path: Path,
    review_dir: Path,
    *,
    renderer_version: str,
    digest_path: Path | None = None,
    published_review_root: str | None = None,
    coverage_requirements_path: Path | None = None,
) -> dict[str, Any]:
    """Describe a complete review output without leaking host-specific paths."""
    project_root = project_root.resolve(strict=True)
    review_html, _ = _project_file(project_root, review_dir / "slides-review.html", label="review HTML")
    review_root = review_html.parent
    required = [
        review_root / "slides-review.html",
        review_root / "montage.png",
        review_root / "visual-qa.json",
    ]
    png_dir = review_root / "png"
    if not png_dir.is_dir():
        raise ReviewPreviewError("review PNG directory is missing")
    pngs = sorted(png_dir.glob("slide-*.png"))
    if not pngs:
        raise ReviewPreviewError("review PNG directory has no slide screenshots")
    for path in required:
        _project_file(project_root, path, label=f"required review output {path.name}")
    for path in pngs:
        _project_file(project_root, path, label="review screenshot")
    actual_review_root = review_root.relative_to(project_root).as_posix()
    if published_review_root is not None and published_review_root != "review":
        raise ReviewPreviewError("published review root must be the project review directory")
    manifest_review_root = published_review_root or actual_review_root
    outputs = []
    for path in sorted(review_root.rglob("*")):
        if not path.is_file() or path.name == "review-manifest.json":
            continue
        entry = _entry(project_root, path, label="review output")
        if published_review_root is not None:
            entry["path"] = f"{manifest_review_root}/{path.relative_to(review_root).as_posix()}"
        outputs.append(entry)
    inputs: dict[str, Any] = {
        "deck": _entry(project_root, deck_path, label="deck"),
        "asset_graph": _entry(project_root, asset_graph_path, label="asset graph"),
        "renderer_version": renderer_version,
    }
    if digest_path is not None:
        inputs["digest"] = _entry(project_root, digest_path, label="digest")
    if coverage_requirements_path is not None:
        inputs["coverage_requirements"] = _entry(project_root, coverage_requirements_path, label="coverage requirements")
    manual_review_paths = sorted((project_root / "review-assets").glob("*-manual-review.json"))
    if manual_review_paths:
        inputs["manual_review_artifacts"] = [
            _entry(project_root, path, label="manual review artifact") for path in manual_review_paths
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "inputs": inputs,
        "review_root": manifest_review_root,
        "outputs": outputs,
    }


def review_is_current(project_root: Path, manifest: dict[str, Any]) -> bool:
    """Return true only while every bound input and core review output is unchanged."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != KIND:
        return False
    inputs = manifest.get("inputs")
    outputs = manifest.get("outputs")
    if not isinstance(inputs, dict) or not isinstance(outputs, list) or not isinstance(inputs.get("renderer_version"), str):
        return False
    entries: list[Any] = []
    for key, value in inputs.items():
        if key == "renderer_version":
            continue
        if isinstance(value, list):
            entries.extend(value)
        else:
            entries.append(value)
    entries.extend(outputs)
    root = Path(project_root)
    try:
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                return False
            path, _ = _project_file(root, root / entry["path"], label="manifest entry")
            if _sha256(path) != entry["sha256"]:
                return False
    except ReviewPreviewError:
        return False
    return True


def _pending_review_context(project_root: Path, checkpoint_path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    try:
        record = _pending_ckpt2_for_review_rebind(_read_record(checkpoint_path))
        required = record["requires"]
        predecessor = Path(required["record_path"])
        predecessor_record = _validate_record(_read_record(predecessor))
        require_approved_checkpoint(predecessor, _record_artifact_path(predecessor_record), expected_checkpoint="CKPT-1")
        digest = _record_artifact_path(predecessor_record)
        graph = record.get("asset_graph")
        if not isinstance(graph, dict) or not isinstance(graph.get("path"), str):
            raise ReviewPreviewError("pending CKPT-2 record has no exact asset graph")
        deck = _record_artifact_path(record)
        asset_graph = Path(graph["path"]).resolve(strict=True)
        deck.relative_to(project_root.resolve(strict=True))
        asset_graph.relative_to(project_root.resolve(strict=True))
        digest.relative_to(project_root.resolve(strict=True))
        return record, deck, asset_graph, digest
    except (CheckpointError, KeyError, OSError, ValueError) as exc:
        raise ReviewPreviewError(f"cannot prepare pending review preview: {exc}") from exc


def _confirmed_semantic_context(
    project_root: Path,
    predecessor_path: Path,
    digest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return (resolved semantic digest, confirmed title/authors) for a confirmed CKPT-1.

    Legacy confirmed records without ``ckpt1-review.json`` return ``None`` so the
    caller keeps the historical raw-digest semantic path.  Any resolution or
    binding failure is fail-closed: it raises instead of silently falling back.
    """
    review_path = project_root / "ckpt1-review.json"
    if not review_path.is_file():
        return None
    try:
        record = _read_record(predecessor_path)
        digest = json.loads(digest_path.read_text(encoding="utf-8"))
        semantic_digest = build_confirmed_semantic_digest(project_root, digest, record)
    except (OSError, json.JSONDecodeError, CheckpointError, CKPT1ResolvedViewError) as exc:
        raise ReviewPreviewError(f"cannot resolve confirmed semantic digest: {exc}") from exc
    metadata = semantic_digest.get("paper_metadata") if isinstance(semantic_digest.get("paper_metadata"), dict) else {}
    title = metadata.get("title")
    authors = metadata.get("authors")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(authors, list)
        or not authors
        or not all(isinstance(author, str) and author.strip() for author in authors)
    ):
        raise ReviewPreviewError("confirmed semantic digest has invalid resolved title/authors")
    return semantic_digest, {"title": title.strip(), "authors": [author.strip() for author in authors]}


def _default_node_runner(command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise ReviewPreviewError(
            f"browser review renderer could not start ({exc}); install or configure the independently supported Node runtime"
        ) from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReviewPreviewError(f"browser review renderer failed: {detail or f'exit code {completed.returncode}'}")
    try:
        payload = json.loads(completed.stdout or "")
    except json.JSONDecodeError as exc:
        raise ReviewPreviewError("browser review renderer did not emit JSON diagnostics") from exc
    if not isinstance(payload, dict):
        raise ReviewPreviewError("browser review renderer diagnostics must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _asset_locator(deck_slide: Mapping[str, Any], asset: str) -> str | None:
    """Return only a declared source locator; never synthesize provenance text."""
    for field in ("figure", "media"):
        candidate = deck_slide.get(field)
        if isinstance(candidate, Mapping) and candidate.get("src") == asset:
            for locator_field in ("source_locator", "source_ref", "locator"):
                locator = candidate.get(locator_field)
                if isinstance(locator, str) and locator.strip():
                    return locator
    for image in deck_slide.get("images", []):
        if isinstance(image, Mapping) and image.get("asset") == asset:
            for locator_field in ("source_locator", "source_ref", "locator"):
                locator = image.get(locator_field)
                if isinstance(locator, str) and locator.strip():
                    return locator
    for locator_field in ("source_locator", "source_ref", "source", "citation"):
        locator = deck_slide.get(locator_field)
        if isinstance(locator, str) and locator.strip():
            return locator
    return None


def _legibility_payload(deck: Mapping[str, Any], diagnostics: Mapping[str, Any], figures: list[dict[str, Any]]) -> dict[str, Any]:
    slides: list[dict[str, Any]] = []
    deck_slides = deck.get("slides", [])
    for raw_slide in diagnostics.get("slides", []):
        if not isinstance(raw_slide, Mapping):
            continue
        slide_index = raw_slide.get("slide_index")
        if not isinstance(slide_index, int) or slide_index < 1:
            continue
        deck_slide = deck_slides[slide_index - 1] if isinstance(deck_slides, list) and slide_index <= len(deck_slides) and isinstance(deck_slides[slide_index - 1], Mapping) else {}
        declared_assets: set[str] = set()
        for field in ("figure", "media"):
            value = deck_slide.get(field)
            if isinstance(value, Mapping) and isinstance(value.get("src"), str):
                declared_assets.add(value["src"])
        for value in deck_slide.get("images", []):
            if isinstance(value, Mapping) and isinstance(value.get("asset"), str):
                declared_assets.add(value["asset"])
        body_elements = [
            element for element in raw_slide.get("elements", [])
            if isinstance(element, Mapping) and element.get("role") not in {"title", "footer", "decorative", "page-number"}
        ]
        extents = [
            (element.get("x"), element.get("y"), element.get("width"), element.get("height"))
            for element in body_elements
            if all(isinstance(element.get(key), (int, float)) for key in ("x", "y", "width", "height"))
        ]
        images: list[dict[str, Any]] = []
        locators: dict[str, str] = {}
        for image in raw_slide.get("images", []):
            if not isinstance(image, Mapping) or not isinstance(image.get("src"), str):
                continue
            asset = image["src"]
            if asset not in declared_assets:
                continue
            locator = _asset_locator(deck_slide, asset)
            if locator is not None:
                locators[asset] = locator
            images.append({
                "src": asset,
                "ok": bool(image.get("complete")) and int(image.get("natural_width", 0)) > 0 and int(image.get("natural_height", 0)) > 0,
                "inFigure": True,
                "rendered": image.get("width"),
                "natural": image.get("natural_width"),
                "naturalH": image.get("natural_height"),
            })
        slides.append({
            "slide": slide_index,
            "layout": raw_slide.get("layout", deck_slide.get("layout", "")),
            "body": {
                "hasBody": bool(extents),
                "bodyBottom": max((y + height for _, y, _, height in extents), default=0),
                "bodyRight": max((x + width for x, _, width, _ in extents), default=0),
            },
            "images": images,
            "assetLocators": locators,
        })
    return {"slides": slides, "figures": figures}


def _legibility_findings(temporary: Path, deck: Mapping[str, Any], diagnostics: Mapping[str, Any], figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    request = temporary / "figure-legibility-input.json"
    _write_json(request, _legibility_payload(deck, diagnostics, figures))
    try:
        completed = subprocess.run(
            ["node", str(Path(__file__).with_name("figure_legibility.mjs")), str(request)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        request.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise ReviewPreviewError(f"deterministic figure legibility check failed: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReviewPreviewError("deterministic figure legibility check did not emit JSON") from exc
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ReviewPreviewError("deterministic figure legibility check emitted malformed findings")
    return result


def _semantic_report_is_valid(value: Any) -> bool:
    """Require a complete, passing semantic report before CKPT-2 reuse/readiness."""
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != 1 or value.get("kind") != "scholar-slides-semantic-qa" or value.get("status") != "pass":
        return False
    inputs = value.get("inputs")
    if not isinstance(inputs, dict) or any(not isinstance(inputs.get(key), str) or not inputs[key] for key in ("deck_sha256", "digest_sha256", "asset_graph_sha256")):
        return False
    summary = value.get("summary")
    if not isinstance(summary, dict) or summary.get("errors") != 0:
        return False
    issues = value.get("issues")
    return isinstance(issues, list) and not any(isinstance(issue, dict) and issue.get("severity") == "error" for issue in issues)


def _safe_count(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _pending_manual_reviews(project: Path) -> list[dict[str, Any]]:
    """Return pending manual-review obligations as portable CKPT-2 checklist data."""
    root = project.resolve(strict=True)
    paths = sorted((root / "review-assets").glob("*-manual-review.json"))
    pending: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewPreviewError(f"manual review artifact is invalid: {path.name}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("status") != "pending_human_confirmation":
            continue
        asset = payload.get("asset") if isinstance(payload.get("asset"), Mapping) else {}
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        asset_id = asset.get("id") if isinstance(asset.get("id"), str) else path.stem
        asset_name = asset.get("label") if isinstance(asset.get("label"), str) else asset_id
        source_page = source.get("page") if isinstance(source.get("page"), int) else None
        pending.append({
            "path": path.relative_to(root).as_posix(),
            "asset_id": asset_id,
            "asset_name": asset_name,
            "source_page": source_page,
            "locator": source.get("locator") if isinstance(source.get("locator"), str) else "",
            "confidence": payload.get("confidence") if isinstance(payload.get("confidence"), str) else "pending human confirmation",
            "issue": payload.get("review_note") if isinstance(payload.get("review_note"), str) else "manual review remains pending",
            "status": "pending_human_confirmation",
        })
    return pending


def _ckpt2_readiness(
    record: dict[str, Any],
    qa: dict[str, Any],
    semantic: dict[str, Any] | None = None,
    aesthetics: dict[str, Any] | None = None,
    *,
    pending_manual_reviews: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Serialize the non-approval handoff that accompanies a passing review preview."""
    summary = qa.get("summary") if isinstance(qa.get("summary"), dict) else {}
    checklist = qa.get("human_review_checklist") if isinstance(qa.get("human_review_checklist"), list) else []
    semantic_valid = _semantic_report_is_valid(semantic)
    semantic = semantic if isinstance(semantic, dict) else {"status": "error", "summary": {"errors": 1, "warnings": 0, "info": 0}}
    if not semantic_valid:
        semantic = dict(semantic)
        semantic["status"] = "error"
        semantic_summary = semantic.get("summary") if isinstance(semantic.get("summary"), dict) else {}
        semantic["summary"] = {"errors": max(1, _safe_count(semantic_summary, "errors")), "warnings": _safe_count(semantic_summary, "warnings"), "info": _safe_count(semantic_summary, "info")}
    semantic_summary = semantic.get("summary") if isinstance(semantic.get("summary"), dict) else {}
    visual_pass = qa.get("status") == "pass"
    semantic_pass = semantic_valid
    aesthetics = validate_aesthetics_report(aesthetics)
    aesthetics_pass = aesthetics.get("status") == "pass" and not aesthetics.get("rework")
    semantic_checklist = semantic.get("human_review_checklist") if isinstance(semantic.get("human_review_checklist"), list) else []
    manual_reviews = [dict(item) for item in pending_manual_reviews if isinstance(item, Mapping)]
    manual_checklist = [
        {
            "kind": "pending-manual-review",
            "path": item.get("path"),
            "asset_id": item.get("asset_id"),
            "source_page": item.get("source_page"),
            "locator": item.get("locator"),
            "status": item.get("status"),
            "action": "Confirm this manual quantitative/evidence review during CKPT-2 human review.",
        }
        for item in manual_reviews
    ]
    return {
        "schema_version": 1,
        "kind": "scholar-slides-ckpt2-readiness",
        "checkpoint": "CKPT-2",
        "status": "ready_for_human_approval" if visual_pass and semantic_pass and aesthetics_pass else "blocked",
        "approval_status": "not_approved",
        "human_review_required": bool(qa.get("human_review_required", True)),
        "automated_visual_qa": {
            "status": qa.get("status"),
            "errors": summary.get("errors", 0),
            "warnings": summary.get("warnings", 0),
            "info": summary.get("info", 0),
        },
        "automated_semantic_qa": {
            "status": semantic.get("status"),
            "errors": semantic_summary.get("errors", 0),
            "warnings": semantic_summary.get("warnings", 0),
            "info": semantic_summary.get("info", 0),
            "inputs": semantic.get("inputs", {}),
        },
        "automated_aesthetics_qa": {
            "status": aesthetics.get("status"),
            "errors": aesthetics.get("summary", {}).get("errors", 1),
            "rework_count": aesthetics.get("summary", {}).get("rework_count", 0),
        },
        "pending_manual_reviews": manual_reviews,
        "human_review_checklist": [*checklist, *semantic_checklist, *manual_checklist],
        "deck_sha256": record.get("artifact", {}).get("sha256"),
        "asset_graph_sha256": record.get("asset_graph", {}).get("sha256"),
    }


def _recover_review_publish(project: Path, review: Path) -> None:
    """Recover only a complete staged review after an interrupted directory swap."""
    backup = project / ".review.previous"
    if not (backup.exists() or backup.is_symlink()):
        return
    if backup.is_symlink() or not backup.is_dir():
        raise ReviewPreviewError("interrupted review replacement has an unsafe backup; inspect it manually")
    if not (review.exists() or review.is_symlink()):
        backup.replace(review)
        return
    if review.is_symlink() or not review.is_dir():
        raise ReviewPreviewError("interrupted review replacement has an unsafe review target; inspect it manually")
    if not (review / "review-manifest.json").is_file():
        shutil.rmtree(review)
        backup.replace(review)
        return
    shutil.rmtree(backup)


def _publish_complete_review(project: Path, temporary: Path, review: Path) -> None:
    """Publish a fully materialized review directory with rollback for a failed swap."""
    backup = project / ".review.previous"
    if backup.exists() or backup.is_symlink():
        raise ReviewPreviewError("review output replacement is unsafe; inspect the interrupted .review.previous directory")
    if review.exists() or review.is_symlink():
        if review.is_symlink() or not review.is_dir():
            raise ReviewPreviewError("existing review output must be a directory and may not be a symbolic link")
        review.replace(backup)
        try:
            temporary.replace(review)
        except OSError:
            backup.replace(review)
            raise
        shutil.rmtree(backup)
        return
    temporary.replace(review)


def run_review_preview(
    project_root: Path,
    *,
    checkpoint_path: Path,
    node_runner: Callable[[list[str]], dict[str, Any]] = _default_node_runner,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Create a complete review-only bundle while preserving a pending CKPT-2 state."""
    project = Path(project_root).resolve(strict=True)
    record, deck_path, graph_path, digest_path = _pending_review_context(project, Path(checkpoint_path))
    predecessor = Path(record["requires"]["record_path"])
    semantic_digest: dict[str, Any] | None = None
    confirmed_metadata: dict[str, Any] | None = None
    semantic_context = _confirmed_semantic_context(project, predecessor, digest_path)
    if semantic_context is not None:
        semantic_digest, confirmed_metadata = semantic_context
    try:
        deck_payload = json.loads(deck_path.read_text(encoding="utf-8"))
        digest_payload = json.loads(digest_path.read_text(encoding="utf-8"))
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewPreviewError(f"cannot load semantic review inputs: {exc}") from exc
    if not isinstance(deck_payload, dict) or not isinstance(digest_payload, dict) or not isinstance(graph_payload, dict):
        raise ReviewPreviewError("semantic review inputs must be JSON objects")
    digest_sha256 = _sha256(digest_path)
    deck_sha256 = str(record["artifact"]["sha256"])
    asset_graph_sha256 = str(record["asset_graph"]["sha256"])
    coverage_payload: dict[str, Any] | None = None
    coverage_sha256 = ""
    coverage_path = project / "coverage-requirements.json"
    if semantic_context is not None:
        try:
            loaded = load_coverage_artifact(project)
        except QuantitativeCoverageError as exc:
            raise ReviewPreviewError(f"quantitative coverage artifact is invalid: {exc}") from exc
        if loaded is None:
            raise ReviewPreviewError("confirmed CKPT-1 project requires coverage-requirements.json")
        coverage_payload, coverage_sha256 = loaded
        provenance = coverage_payload.get("provenance") if isinstance(coverage_payload.get("provenance"), Mapping) else {}
        predecessor = Path(record["requires"]["record_path"])
        if (
            provenance.get("digest_sha256") != digest_sha256
            or provenance.get("checkpoint_sha256") != _sha256(predecessor)
        ):
            raise ReviewPreviewError("coverage-requirements.json provenance does not match the confirmed CKPT-1 bundle")
        expected_artifact = build_coverage_artifact(
            project_dir=project,
            semantic_digest=semantic_digest,
            requirements=collect_quantitative_requirements(project_dir=project, semantic_digest=semantic_digest),
            digest_sha256=digest_sha256,
            checkpoint_sha256=_sha256(predecessor),
            review_sha256=(
                _sha256(project / "ckpt1-review.json")
                if (project / "ckpt1-review.json").is_file()
                else None
            ),
        )
        if expected_artifact != coverage_payload:
            raise ReviewPreviewError("coverage-requirements.json does not match the confirmed semantic digest")
    review = project / "review"
    pending_manual_reviews = _pending_manual_reviews(project)
    _recover_review_publish(project, review)
    manifest_path = review / "review-manifest.json"
    if not force_rebuild and manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict) and review_is_current(project, existing):
            qa_path = review / "visual-qa.json"
            semantic_path = review / "semantic-qa.json"
            aesthetics_path = review / "aesthetics-qa.json"
            try:
                qa = json.loads(qa_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                qa = None
            try:
                semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                semantic = None
            try:
                aesthetics = validate_aesthetics_report(json.loads(aesthetics_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                aesthetics = validate_aesthetics_report(None)
            readiness_path = review / "ckpt2-readiness.json"
            try:
                readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                readiness = None
            if (
                isinstance(qa, dict)
                and qa.get("status") == "pass"
                and _semantic_report_is_valid(semantic)
                and aesthetics.get("status") == "pass"
                and not aesthetics.get("rework")
                and semantic_qa_is_current(
                    semantic,
                    deck_sha256=deck_sha256,
                    digest_sha256=digest_sha256,
                    asset_graph_sha256=asset_graph_sha256,
                    coverage_requirements_sha256=coverage_sha256 or None,
                )
                and qa.get("human_review_required") is True
                and isinstance(qa.get("human_review_checklist"), list)
                and isinstance(readiness, dict)
                and isinstance(readiness.get("automated_semantic_qa"), dict)
                and readiness["automated_semantic_qa"].get("status") == "pass"
                and isinstance(readiness.get("automated_aesthetics_qa"), dict)
                and readiness["automated_aesthetics_qa"].get("status") == "pass"
                and readiness.get("pending_manual_reviews", []) == pending_manual_reviews
                and readiness_path.is_file()
            ):
                record_review_bundle(checkpoint_path, manifest_path)
                return {"review_dir": str(review), "manifest": existing, "visual_qa": qa, "semantic_qa": semantic, "aesthetics_qa": aesthetics, "reused": True}
    temporary = project / ".review.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise ReviewPreviewError("temporary review output already exists; inspect or remove the incomplete .review.tmp directory")
    command = ["node", str(Path(__file__).with_name("review_renderer.mjs")), "--capture", str(deck_path), str(graph_path), str(temporary)]
    try:
        diagnostics = node_runner(command)
        expected_slides = len(deck_payload.get("slides", []))
        if diagnostics.get("slide_count") != expected_slides:
            raise ReviewPreviewError("browser review renderer slide count does not match deck.json")
        _write_json(temporary / "renderer-log.json", diagnostics)
        screenshots = sorted((temporary / "png").glob("slide-*.png"))
        if len(screenshots) != expected_slides:
            raise ReviewPreviewError("browser review renderer did not produce one PNG per slide")
        montage = build_montage(screenshots, temporary / "montage.png")
        qa_deck = json.loads(deck_path.read_text(encoding="utf-8"))
        figures_path = project / "figures.json"
        try:
            figures_payload = json.loads(figures_path.read_text(encoding="utf-8")) if figures_path.is_file() else []
        except (OSError, json.JSONDecodeError):
            figures_payload = []
        figures = [figure for figure in figures_payload if isinstance(figure, dict)] if isinstance(figures_payload, list) else []
        legibility_findings = _legibility_findings(temporary, qa_deck, diagnostics, figures)
        qa = evaluate_visual_qa(
            deck=qa_deck,
            diagnostics=diagnostics,
            deck_sha256=record["artifact"]["sha256"],
            asset_graph_sha256=record["asset_graph"]["sha256"],
            renderer_version=str(diagnostics.get("renderer_version", "unknown")),
            legibility_findings=legibility_findings,
        )
        qa["montage"] = montage
        _write_json(temporary / "visual-qa.json", qa)
        semantic = evaluate_semantic_qa(
            deck_payload,
            semantic_digest if semantic_digest is not None else digest_payload,
            graph_payload,
            deck_payload.get("slides", []),
            deck_sha256=deck_sha256,
            digest_sha256=digest_sha256,
            asset_graph_sha256=asset_graph_sha256,
            confirmed_metadata=confirmed_metadata,
            coverage_requirements=(
                coverage_payload.get("requirements") if coverage_payload is not None else None
            ),
            coverage_requirements_sha256=coverage_sha256 or None,
        )
        _write_json(temporary / "semantic-qa.json", semantic)
        aesthetics = build_aesthetics_report(deck_payload, visual_qa=qa, inputs={"deck_sha256": deck_sha256, "asset_graph_sha256": asset_graph_sha256})
        _write_json(temporary / "aesthetics_report.json", aesthetics)
        _write_json(temporary / "aesthetics-qa.json", aesthetics)
        _write_json(temporary / "ckpt2-readiness.json", _ckpt2_readiness(record, qa, semantic, aesthetics, pending_manual_reviews=pending_manual_reviews))
        manifest = build_review_manifest(
            project,
            deck_path,
            graph_path,
            temporary,
            renderer_version=qa["inputs"]["renderer_version"],
            digest_path=digest_path,
            published_review_root="review",
            coverage_requirements_path=coverage_path if coverage_sha256 else None,
        )
        _write_json(temporary / "review-manifest.json", manifest)
        _publish_complete_review(project, temporary, review)
        if semantic.get("status") != "pass" or aesthetics.get("status") != "pass" or aesthetics.get("rework"):
            raise ReviewPreviewError("review preview cannot be bound to pending CKPT-2: semantic or aesthetics QA has blocking errors")
        record_review_bundle(checkpoint_path, review / "review-manifest.json")
        return {"review_dir": str(review), "manifest": manifest, "visual_qa": qa, "semantic_qa": semantic, "aesthetics_qa": aesthetics, "reused": False}
    except CheckpointError as exc:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise ReviewPreviewError(f"review preview cannot be bound to pending CKPT-2: {exc}") from exc
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate pending-CKPT-2 review HTML, screenshots, montage, and visual QA.")
    parser.add_argument("project", help="paper bundle containing deck.json and checkpoint-2.json")
    parser.add_argument("--checkpoint", required=True, help="pending CKPT-2 record")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable result")
    parser.add_argument("--force-rebuild-review", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run_review_preview(Path(args.project), checkpoint_path=Path(args.checkpoint), force_rebuild=args.force_rebuild_review)
        qa = result["visual_qa"]
        semantic = result.get("semantic_qa") if isinstance(result.get("semantic_qa"), dict) else {"status": "error", "summary": {"errors": 1, "warnings": 0, "info": 0}}
        aesthetics = result.get("aesthetics_qa") if isinstance(result.get("aesthetics_qa"), dict) else validate_aesthetics_report(None)
        semantic_valid = _semantic_report_is_valid(semantic)
        aesthetics_valid = aesthetics.get("status") == "pass" and not aesthetics.get("rework")
        readiness = "ready_for_human_approval" if qa.get("status") == "pass" and semantic_valid and aesthetics_valid else "review_complete_with_blocking_errors"
        payload = {
            "ok": qa.get("status") == "pass" and semantic_valid and aesthetics_valid,
            "review_dir": result["review_dir"],
            "reused": result["reused"],
            "readiness": readiness,
            "human_review_required": bool(qa.get("human_review_required", True)),
            "approval_status": "not_approved",
            "visual_qa": qa.get("summary", {}),
            "semantic_qa": semantic.get("summary", {}),
            "aesthetics_qa": aesthetics.get("summary", {}),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"review preview -> {result['review_dir']}")
            print(f"readiness: {readiness}")
        return 0 if payload["ok"] else 2
    except ReviewPreviewError as exc:
        payload = {"ok": False, "error": str(exc), "next_step": "Repair the reported review input or renderer issue, then rerun scholar-slides review."}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"ERROR: {exc}")
            print(f"Next: {payload['next_step']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
