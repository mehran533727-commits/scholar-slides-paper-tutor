"""Validate and render generic, file-bound CKPT-1 evidence audits.

An evidence audit is intentionally paper-agnostic.  It binds a source PDF hash,
page, normalized rectangle, rendered crop, and (when relevant) reviewed table
cells.  ``validate_evidence_audit`` is the fail-closed runtime gate; the
separate ``validate_evidence_audit_schema`` is for portable JSON before files
have been materialized.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import tempfile
from typing import Any

from schema_validation import create_schema_validator, resolve_skill_schema_path


class EvidenceAuditError(ValueError):
    """Raised when candidate evidence cannot identify a valid source region."""


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 of a file without loading the full evidence asset at once."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def crop_rendering_recipe(dpi: int = 200) -> dict[str, object]:
    """Return the versioned, deterministic renderer settings bound into an audit."""
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise EvidenceAuditError("crop dpi must be a positive integer")
    import fitz

    return {"renderer": "pymupdf", "renderer_version": str(fitz.VersionBind), "dpi": dpi}


def validate_normalized_bbox(bbox: Mapping[str, Any]) -> None:
    """Reject a normalized ``{x, y, width, height}`` crop outside its source page."""
    required = ("x", "y", "width", "height")
    if not isinstance(bbox, Mapping) or set(bbox) != set(required):
        raise EvidenceAuditError("bbox must contain exactly x, y, width, and height")

    values: dict[str, float] = {}
    for key in required:
        value = bbox[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvidenceAuditError(f"bbox {key} must be numeric")
        values[key] = float(value)

    if not (0 <= values["x"] <= 1 and 0 <= values["y"] <= 1):
        raise EvidenceAuditError("bbox origin must be within normalized page bounds")
    if not (0 < values["width"] <= 1 and 0 < values["height"] <= 1):
        raise EvidenceAuditError("bbox dimensions must be positive and normalized")
    if values["x"] + values["width"] > 1 or values["y"] + values["height"] > 1:
        raise EvidenceAuditError("bbox exceeds normalized page bounds")


def _schema() -> Mapping[str, Any]:
    schema_path = resolve_skill_schema_path("evidence-audit.schema.json", anchor=__file__)
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _validate_schema(audit: Mapping[str, Any]) -> None:
    errors = sorted(create_schema_validator(_schema()).iter_errors(audit), key=lambda error: list(error.path))
    if errors:
        raise EvidenceAuditError(f"evidence audit schema validation failed: {errors[0].message}")


def _portable_relative_path(raw: object) -> PurePosixPath:
    """Accept a Unicode-safe, slash-separated relative path inside an audit root."""
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise EvidenceAuditError("crop path must be a portable relative path")
    portable = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        portable.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or raw in {".", ".."}
        or any(part in {"", ".", ".."} for part in portable.parts)
    ):
        raise EvidenceAuditError("crop path must be a portable relative path")
    return portable


def _resolve_crop_path(project_root: str | Path, raw: object) -> Path:
    """Resolve a declared portable path and prove it remains within ``project_root``."""
    portable = _portable_relative_path(raw)
    root = Path(project_root).resolve(strict=True)
    candidate = root.joinpath(*portable.parts).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvidenceAuditError("crop path must be a portable relative path") from exc
    return candidate


def resolve_canonical_source_pdf(
    project_root: str | Path,
    digest: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> tuple[Path, str]:
    """Return the one source PDF bound by digest and CKPT-1, or fail closed."""
    root = Path(project_root).resolve(strict=True)
    source = digest.get("source") if isinstance(digest, Mapping) else None
    identity = checkpoint.get("source_identity") if isinstance(checkpoint, Mapping) else None
    if not isinstance(source, Mapping) or not isinstance(identity, Mapping):
        raise EvidenceAuditError("digest and checkpoint source identities are required")
    raw_pdf = source.get("pdf")
    digest_sha = source.get("pdf_sha256")
    checkpoint_sha = identity.get("pdf_sha256")
    if not isinstance(raw_pdf, str) or not isinstance(digest_sha, str) or not isinstance(checkpoint_sha, str):
        raise EvidenceAuditError("canonical source PDF path and SHA-256 identities are required")
    pdf_path = _resolve_crop_path(root, raw_pdf)
    if not pdf_path.is_file():
        raise EvidenceAuditError("canonical source PDF is missing")
    actual_sha = sha256_file(pdf_path)
    if actual_sha.casefold() != digest_sha.casefold():
        raise EvidenceAuditError("canonical source PDF SHA-256 does not match the digest")
    if digest_sha.casefold() != checkpoint_sha.casefold():
        raise EvidenceAuditError("canonical source PDF SHA-256 does not match CKPT-1 source identity")
    return pdf_path, actual_sha


def _normalized_cell_value(value: object) -> tuple[str, object]:
    """Produce a conservative comparison form without inventing unit conversions."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise EvidenceAuditError("reviewed cell values must be strings or numbers")
    text = " ".join(str(value).split())
    if not text:
        raise EvidenceAuditError("reviewed cell values cannot be empty")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return ("text", text)
    if not number.is_finite():
        raise EvidenceAuditError("reviewed cell numeric values must be finite")
    return ("number", number)


def _validate_reviewed_cells(audit: Mapping[str, Any]) -> None:
    cells = audit.get("reviewed_cells")
    if not isinstance(cells, list):
        return
    seen: set[tuple[str, str]] = set()
    for cell in cells:
        if not isinstance(cell, Mapping):
            continue
        row, column = cell.get("row_key"), cell.get("column_key")
        if not isinstance(row, str) or not isinstance(column, str):
            continue
        binding = (" ".join(row.split()).casefold(), " ".join(column.split()).casefold())
        if binding in seen:
            raise EvidenceAuditError(f"duplicate reviewed cell binding: row={row!r}, column={column!r}")
        seen.add(binding)
        if _normalized_cell_value(cell.get("value")) != _normalized_cell_value(cell.get("normalized_value")):
            raise EvidenceAuditError("reviewed cell normalized_value does not match value")


def _page_rectangle(source_pdf: str | Path, page_number: int) -> tuple[float, float, float, float]:
    """Return a 1-indexed PDF page rectangle while checking the page is real."""
    import fitz

    path = Path(source_pdf)
    try:
        document = fitz.open(path)
    except (OSError, RuntimeError) as exc:
        raise EvidenceAuditError(f"cannot open source PDF: {path}") from exc
    try:
        if not 1 <= page_number <= len(document):
            raise EvidenceAuditError(f"source PDF page {page_number} is outside its page count")
        rect = document[page_number - 1].rect
        return (rect.x0, rect.y0, rect.x1, rect.y1)
    finally:
        document.close()


def crop_bound_pdf_region(
    source_pdf: str | Path,
    page_number: int,
    bbox: Mapping[str, Any],
    out_path: str | Path,
    *,
    dpi: int = 200,
) -> Path:
    """Render exactly one normalized evidence region from a real source PDF page."""
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise EvidenceAuditError("source PDF page must be a positive integer")
    crop_rendering_recipe(dpi)
    validate_normalized_bbox(bbox)
    page_rect = _page_rectangle(source_pdf, page_number)
    x0, y0, x1, y1 = page_rect
    width, height = x1 - x0, y1 - y0
    rect = (
        x0 + float(bbox["x"]) * width,
        y0 + float(bbox["y"]) * height,
        x0 + (float(bbox["x"]) + float(bbox["width"])) * width,
        y0 + (float(bbox["y"]) + float(bbox["height"])) * height,
    )
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from crop_figure import crop

    crop(str(source_pdf), page_number, rect, str(destination), dpi=dpi, page_rect=page_rect, pad_abs=0.0, pad_frac=0.0)
    return destination


def validate_evidence_audit_schema(audit: Mapping[str, Any]) -> None:
    """Validate portable audit JSON without claiming that on-disk evidence is bound."""
    if not isinstance(audit, Mapping):
        raise EvidenceAuditError("evidence audit must be an object")
    _validate_schema(audit)
    source = audit["source"]
    crop = audit["crop"]
    assert isinstance(source, Mapping) and isinstance(crop, Mapping)
    validate_normalized_bbox(source["bbox"])
    _validate_reviewed_cells(audit)
    _portable_relative_path(crop["path"])


def validate_evidence_audit(
    audit: Mapping[str, Any],
    *,
    source_pdf: str | Path | None = None,
    project_root: str | Path | None = None,
    expected_source_sha256: str | None = None,
) -> None:
    """Fail closed: prove an audit's source, crop bytes, and rendering provenance.

    Both materialized bindings are required.  Call
    :func:`validate_evidence_audit_schema` for the intentionally weaker portable
    JSON-only check used before a crop exists.
    """
    if source_pdf is None or project_root is None:
        raise EvidenceAuditError("source_pdf and project_root are both required for runtime evidence validation")
    if not isinstance(expected_source_sha256, str) or not expected_source_sha256:
        raise EvidenceAuditError("expected_source_sha256 is required for runtime evidence validation")
    validate_evidence_audit_schema(audit)
    source = audit["source"]
    crop = audit["crop"]
    assert isinstance(source, Mapping) and isinstance(crop, Mapping)

    crop_path = _resolve_crop_path(project_root, crop["path"])
    if not crop_path.is_file():
        raise EvidenceAuditError(f"crop file is missing: {crop['path']}")
    if sha256_file(crop_path).casefold() != str(crop["sha256"]).casefold():
        raise EvidenceAuditError("crop SHA-256 does not match the audit")

    pdf_path = Path(source_pdf)
    if not pdf_path.is_file():
        raise EvidenceAuditError(f"source PDF is missing: {pdf_path}")
    actual_source_sha = sha256_file(pdf_path)
    if actual_source_sha.casefold() != expected_source_sha256.casefold():
        raise EvidenceAuditError("source PDF SHA-256 does not match the canonical source")
    if str(source["pdf_sha256"]).casefold() != expected_source_sha256.casefold():
        raise EvidenceAuditError("audit source PDF SHA-256 does not match the canonical source")
    _page_rectangle(pdf_path, source["page"])

    rendering = crop["rendering"]
    assert isinstance(rendering, Mapping)
    expected_recipe = crop_rendering_recipe(rendering["dpi"])
    if dict(rendering) != expected_recipe:
        raise EvidenceAuditError("crop rendering recipe does not match the current renderer")
    with tempfile.TemporaryDirectory(prefix="evidence-audit-") as temporary:
        rendered = Path(temporary) / "bound-region.png"
        crop_bound_pdf_region(pdf_path, source["page"], source["bbox"], rendered, dpi=rendering["dpi"])
        if sha256_file(rendered).casefold() != str(crop["sha256"]).casefold():
            raise EvidenceAuditError("crop provenance does not match the bound PDF page and bbox")
