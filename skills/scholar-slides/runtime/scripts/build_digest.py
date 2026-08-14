#!/usr/bin/env python3
"""Build a reviewable, extractive paper digest from a Stage-1 source bundle.

This is deliberately *not* a paper-summary generator.  It records only source
metadata, an extractive abstract, and the detected paper-owned figure/table inventory.
Interpretation, claims, and reported metrics remain empty until a reviewer supplies
them from the paper.  Missing and uncertain evidence stays visible in both outputs.

Input:  a bundle written by ``prepare_source.py``.
Output: ``digest.json`` (machine-readable provenance) and ``digest.md`` (CKPT-1
         review artifact) in that bundle, unless ``--out-dir`` is specified.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import paper_metadata
from paper_semantics import REQUIRED_SLOTS, build_paper_semantics


_ABSTRACT_START = re.compile(r"(?im)^\s*abstract\s*(?:[:—–-]\s*)?")
_SECTION_END = re.compile(
    r"(?im)^\s*(?:"
    r"(?:\d+(?:\.\d+)*|[IVXLCDM]+)\.?\s+[A-Z][^\n]{1,120}|"
    r"keywords?\s*[:—–-]|index\s+terms?\s*[:—–-]|"
    # On conference title pages, PyMuPDF emits affiliation footnotes (``1The ...``)
    # and Figure 1 captions between the abstract and ``1. Introduction``.  Neither
    # is abstract text, so regard both line forms as an extractive boundary.
    r"\d+(?=[A-Z][a-z])|(?:fig(?:ure)?|tab(?:le)?)\.?\s+\d+\b"
    r")"
)


def _quantitative_source_inventory(figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose likely quantitative source assets without extracting any values."""
    inventory: list[dict[str, Any]] = []
    cues = ("success", "rate", "latency", "average", "accuracy", "trial", "comparison")
    for figure in figures:
        label = _single_line(figure.get("label"))
        caption = _single_line(figure.get("caption"))
        kind = _single_line(figure.get("kind")).casefold()
        if kind == "table" or label.casefold().startswith("table ") or any(cue in caption.casefold() for cue in cues):
            inventory.append({
                "id": figure.get("id"),
                "label": label,
                "kind": figure.get("kind"),
                "page": figure.get("page"),
                "locator": figure.get("source_ref"),
                "caption": caption,
            })
    return inventory


def _load_json(path: Path, expected_type: type) -> Any:
    """Load a declared source artifact with a clear, fail-closed error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"required Stage-1 artifact is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in Stage-1 artifact {path}: {exc}") from exc
    if not isinstance(value, expected_type):
        raise ValueError(f"unexpected JSON shape in {path}: expected {expected_type.__name__}")
    return value


def _single_line(value: object) -> str:
    """Preserve characters while making PDF line wrapping readable in Markdown."""

    return " ".join(str(value or "").split())


def _page_texts(ingest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return lightweight persisted page text, with a legacy full-text fallback."""

    pages = ingest.get("page_text")
    if isinstance(pages, list):
        clean = []
        for record in pages:
            if isinstance(record, dict) and isinstance(record.get("page"), int):
                clean.append({"page": record["page"], "text": str(record.get("text") or "")})
        if clean:
            return clean
    return [{"page": None, "text": str(ingest.get("full_text") or "")}]


def _extract_abstract(ingest: dict[str, Any]) -> tuple[dict[str, str] | None, list[str]]:
    """Extract the paper's labelled abstract and retain the page where it begins."""

    flags: list[str] = []
    pages = _page_texts(ingest)
    for page in pages:
        text = page["text"]
        start = _ABSTRACT_START.search(text)
        if not start:
            continue
        rest = text[start.end():]
        end = _SECTION_END.search(rest)
        excerpt = _single_line(rest[:end.start()] if end else rest)
        if excerpt:
            source_ref = f"p. {page['page']}" if page["page"] else "PDF text (page unavailable)"
            if page["page"] is None:
                flags.append("[UNVERIFIED: abstract page is unavailable in this legacy bundle]")
            return {"text": excerpt, "source_ref": source_ref}, flags
    flags.append("[MISSING: abstract not found in extracted PDF text]")
    return None, flags


def _figure_record(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Make a compact, traceable inventory entry without changing the source caption."""

    label = _single_line(raw.get("label")) or "[MISSING: figure/table label]"
    page = raw.get("page")
    source_ref = f"p. {page} — {label}" if isinstance(page, int) else f"[UNVERIFIED: page] — {label}"
    confidence = _single_line(raw.get("confidence")) or "unknown"
    flags: list[str] = []
    if confidence.casefold() not in {"high", "reliable"}:
        flags.append(f"[UNVERIFIED: {label} localisation confidence is {confidence}]")
    if not raw.get("figure_bbox"):
        flags.append(f"[MISSING: {label} has no reliable crop bounding box]")
    return {
        "id": _single_line(raw.get("id")) or None,
        "kind": _single_line(raw.get("kind")) or "unknown",
        "label": label,
        "caption": _single_line(raw.get("caption")) or "[MISSING: caption]",
        "page": page if isinstance(page, int) else None,
        "source_ref": source_ref,
        "confidence": confidence,
        "render_as": _single_line(raw.get("render_as")) or None,
        "figure_bbox": raw.get("figure_bbox"),
    }, flags


def _markdown(payload: dict[str, Any]) -> str:
    """Render a human-review artifact while exposing every unresolved flag."""

    source = payload["source"]
    lines = [
        f"# Paper digest — {payload['title']}",
        "",
        "## Source provenance",
        "",
        f"- Input: `{source['source_input']}`",
        f"- Kind: `{source['source_kind']}`",
        f"- SHA-256: `{source['source_sha256']}`",
        f"- Pages: {source['n_pages']}",
    ]
    metadata = payload["paper_metadata"]
    lines += [
        f"- Title: {metadata['title']}",
        f"- Authors: {', '.join(metadata['authors']) if metadata['authors'] else '[MISSING: authors]'}",
    ]
    if source.get("arxiv_id"):
        lines.append(f"- arXiv ID detected in PDF: `{source['arxiv_id']}`")
    lines += ["", "## Extractive abstract", ""]
    abstract = payload["abstract"]
    if abstract:
        lines += [abstract["text"], "", f"Source: {abstract['source_ref']}"]
    else:
        lines.append("[MISSING: abstract not found in extracted PDF text]")
    semantics = payload.get("paper_semantics") if isinstance(payload.get("paper_semantics"), dict) else {}
    slots = semantics.get("slots") if isinstance(semantics.get("slots"), dict) else {}
    lines += ["", "## Paper semantics", ""]
    for slot in REQUIRED_SLOTS:
        record = slots.get(slot)
        if not isinstance(record, dict):
            lines.append(f"- {slot}: [MISSING: semantic evidence not detected]")
            continue
        lines.append(
            f"- {slot}: {record.get('summary', record.get('text', ''))} "
            f"(type: {record.get('semantic_evidence_type', 'unknown')}; "
            f"p. {record.get('source_page')} — {record.get('section')} — {record.get('locator')}; "
            f"confidence: {record.get('confidence', 'unknown')})"
        )
    readiness = payload.get("mode_b_narrative_readiness") if isinstance(payload.get("mode_b_narrative_readiness"), dict) else {}
    lines += [
        "", "## Mode-B narrative readiness", "",
        f"- Status: {readiness.get('status', 'incomplete')}",
        f"- Ready: {readiness.get('ready', False)}",
    ]
    missing = readiness.get("missing_slots") if isinstance(readiness.get("missing_slots"), list) else []
    lines.append(f"- Missing slots: {', '.join(str(item) for item in missing) if missing else 'None'}")
    lines += ["", "## Paper-owned figures and tables", ""]
    if payload["figures"]:
        for figure in payload["figures"]:
            lines += [
                f"- {figure['label']} ({figure['kind']}; {figure['source_ref']}; "
                f"localisation: {figure['confidence']}): {figure['caption']}"
            ]
    else:
        lines.append("[MISSING: no figures or tables detected in source bundle]")
    lines += [
        "",
        "## Reviewer-authored content required before deck generation",
        "",
        "[MISSING: author-reviewed claims]",
        "[MISSING: author-reviewed contributions]",
        "[MISSING: author-reviewed experimental results]",
    ]
    if payload["flags"]:
        lines += ["", "## Integrity flags", "", *payload["flags"]]
    lines += ["", "This initial digest is extractive only. Do not infer, complete, or relabel paper claims from it.", ""]
    return "\n".join(lines)


def build_digest(bundle_dir: str | Path, out_dir: str | Path | None = None) -> dict[str, Path]:
    """Write the two grounded digest artifacts and return their paths."""

    bundle = Path(bundle_dir)
    destination = Path(out_dir) if out_dir is not None else bundle
    ingest = _load_json(bundle / "ingest.json", dict)
    figures_raw = _load_json(bundle / "figures.json", list)
    manifest = _load_json(bundle / "manifest.json", dict)

    abstract, flags = _extract_abstract(ingest)
    figures = []
    for raw in figures_raw:
        if not isinstance(raw, dict):
            flags.append("[UNVERIFIED: malformed figure inventory item was ignored]")
            continue
        figure, figure_flags = _figure_record(raw)
        figures.append(figure)
        flags.extend(figure_flags)
    if not figures:
        flags.append("[MISSING: no figures or tables detected in source bundle]")

    meta = ingest.get("meta") if isinstance(ingest.get("meta"), dict) else {}
    extracted_metadata = ingest.get("paper_metadata")
    metadata = extracted_metadata if isinstance(extracted_metadata, dict) else paper_metadata.extract_paper_metadata(ingest, manifest)
    title = _single_line(metadata.get("title")) or "[MISSING: paper title]"
    metadata_flags = metadata.get("flags") if isinstance(metadata.get("flags"), list) else []
    semantic_view = build_paper_semantics(_page_texts(ingest))
    quantitative_sources = _quantitative_source_inventory(figures)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "extractive_only": True,
        "review_status": "pending_human_confirmation",
        "title": title,
        "paper_metadata": metadata,
        "source": {
            "source_input": _single_line(manifest.get("source_input")) or "[MISSING: source input]",
            "source_kind": _single_line(manifest.get("source_kind")) or "[MISSING: source kind]",
            "source_sha256": _single_line(manifest.get("source_sha256")) or "[MISSING: source SHA-256]",
            "pdf": _single_line(manifest.get("pdf") or ingest.get("path")) or "[MISSING: PDF path]",
            "n_pages": ingest.get("n_pages") if isinstance(ingest.get("n_pages"), int) else "[MISSING: page count]",
            "arxiv_id": (metadata.get("identifiers", {}).get("arxiv") or {}).get("base_id") or _single_line(meta.get("arxiv_id")) or None,
            "requested_identifier": _single_line(manifest.get("requested_identifier")) or "[MISSING: requested identifier]",
            "resolved_identifier": _single_line(manifest.get("resolved_identifier")) or "[MISSING: resolved identifier]",
            "pdf_sha256": _single_line(manifest.get("source_sha256")) or "[MISSING: PDF SHA-256]",
            "fetched_at": _single_line(manifest.get("fetched_at")) or "[MISSING: fetched at]",
        },
        "abstract": abstract,
        "paper_semantics": {
            "schema_version": semantic_view["schema_version"],
            "slots": semantic_view["slots"],
            "selection_audit": semantic_view["selection_audit"],
            "source_evidence": semantic_view.get("source_evidence", []),
        },
        "mode_b_narrative_readiness": semantic_view["mode_b_narrative_readiness"],
        "quantitative_evidence": {
            "expected": bool(quantitative_sources),
            "sources": quantitative_sources,
        },
        "figures": figures,
        # These are intentionally empty.  A human/grounded later stage must attach
        # page references before any claim or number is allowed into a deck.
        "claims": [],
        "key_metrics": [],
        "flags": list(dict.fromkeys([*flags, *[str(flag) for flag in metadata_flags]])),
    }
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "digest.json"
    markdown_path = destination / "digest.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build a source-traceable, extractive digest from a Stage-1 paper bundle."
    )
    parser.add_argument("bundle", help="directory containing ingest.json, figures.json, manifest.json")
    parser.add_argument("--out-dir", help="write digest artifacts here (default: bundle directory)")
    args = parser.parse_args(argv)
    try:
        result = build_digest(args.bundle, args.out_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"build_digest: {exc}", file=sys.stderr)
        return 2
    print(f"Extractive digest JSON -> {result['json']}")
    print(f"Reviewer artifact       -> {result['markdown']}")
    print("Next: review digest.md at Checkpoint 1; unresolved [MISSING]/[UNVERIFIED] flags block export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
