#!/usr/bin/env python3
"""Stage 1 orchestrator: source PDF -> digest-input bundle for CKPT-1.

Runs ingest -> detect-figures -> crop into a single output bundle the model reads to
build the typed paper digest (see references/ingestion.md) and that the user reviews at
Checkpoint 1. Accepts a local PDF path, an arXiv id (e.g. 1706.03762), or an arXiv URL.

Output bundle (default out/<stem>/):
    ingest.json      {path, n_pages, meta:{title, arxiv_id}, full_text}
    figures.json     figure/table inventory with bboxes + render_as + confidence
    figures/*.png    cropped figure assets (+ reliable table reference snapshots)
    manifest.json    summary for CKPT-1

This orchestrator only assembles the raw, faithful material — it does NOT synthesize the
digest (that is the model's job, grounded in this bundle) and never invents content.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

import ingest_pdf
import detect_figures
import crop_figure
import paper_metadata

_ARXIV_ID = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$", re.IGNORECASE)
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_input(arg, work_dir):
    """Return a local PDF path. Downloads from arXiv when given an id or arXiv URL."""
    if os.path.isfile(arg):
        return arg
    arxiv_id = None
    m = _ARXIV_ID.match(arg.strip())
    if m:
        arxiv_id = f"{m.group(1)}{m.group(2) or ''}"
    else:
        m = _ARXIV_URL.search(arg)
        if m:
            arxiv_id = f"{m.group(1)}{m.group(2) or ''}"
    if not arxiv_id:
        raise FileNotFoundError(f"not a file, arXiv id, or arXiv URL: {arg!r}")
    import requests

    os.makedirs(work_dir, exist_ok=True)
    dest = os.path.join(work_dir, f"{arxiv_id}.pdf")
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    resp = requests.get(url, timeout=60, headers={"User-Agent": "scholar-slides/0.1"})
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "").casefold()
    if "html" in content_type or not resp.content.startswith(b"%PDF-"):
        raise ValueError(f"arXiv response is not a PDF: {url}")
    with open(dest, "wb") as fh:
        fh.write(resp.content)
    return dest


def prepare(arg, out_dir=None, dpi=200):
    source_input = str(arg)
    source_kind = "local_pdf" if os.path.isfile(arg) else "arxiv_pdf"
    stem = re.sub(r"\W+", "_", os.path.splitext(os.path.basename(str(arg)))[0]) or "source"
    out_dir = out_dir or os.path.join("out", stem)
    os.makedirs(out_dir, exist_ok=True)

    pdf = resolve_input(arg, out_dir)
    # Every downstream checkpoint validates a path inside the bundle.  Copy rather
    # than refer to a caller-owned local path, preserving the exact bytes/hash.
    bound_pdf = os.path.join(out_dir, "source.pdf")
    if os.path.abspath(pdf) != os.path.abspath(bound_pdf):
        shutil.copy2(pdf, bound_pdf)
    pdf = bound_pdf
    ingest = ingest_pdf.extract(pdf)
    # Keep page text, but not layout boxes.  A later digest needs page-level provenance
    # (for example, "abstract on p. 1") while it does not need to duplicate the heavier
    # block geometry that remains available from a direct ingest when needed.
    ingest_payload = {k: v for k, v in ingest.items() if k != "pages"}
    ingest_payload["page_text"] = [
        {"page": page["page"], "text": page["text"]}
        for page in ingest["pages"]
    ]
    metadata_source = {
        "source_input": source_input,
        "source_kind": source_kind,
        "source_sha256": sha256_file(pdf),
        "pdf": pdf,
    }
    ingest_payload["paper_metadata"] = paper_metadata.extract_paper_metadata(ingest, metadata_source)
    with open(os.path.join(out_dir, "ingest.json"), "w", encoding="utf-8") as fh:
        json.dump(ingest_payload, fh, ensure_ascii=False, indent=2)

    figures = detect_figures.detect(pdf)
    with open(os.path.join(out_dir, "figures.json"), "w", encoding="utf-8") as fh:
        json.dump(figures, fh, ensure_ascii=False, indent=2)

    crops = crop_figure.crop_from_inventory(pdf, figures, os.path.join(out_dir, "figures"), dpi=dpi)

    n_fig = sum(1 for f in figures if f["kind"] == "figure")
    n_tab = sum(1 for f in figures if f["kind"] == "table")
    n_loc = sum(1 for f in figures if f["figure_bbox"])
    n_flag = sum(1 for f in figures if not f["figure_bbox"])
    arxiv = ingest_payload["paper_metadata"].get("identifiers", {}).get("arxiv")
    resolved_identifier = f"arXiv:{arxiv['resolved_id']}" if isinstance(arxiv, dict) and arxiv.get("resolved_id") else "source.pdf"
    manifest = {
        "source_input": source_input,
        "source_kind": source_kind,
        "source_sha256": metadata_source["source_sha256"],
        "pdf": "source.pdf",
        "requested_identifier": source_input,
        "resolved_identifier": resolved_identifier,
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "out_dir": out_dir,
        "n_pages": ingest["n_pages"],
        "title": ingest["meta"]["title"],
        "arxiv_id": ingest["meta"]["arxiv_id"],
        "paper_metadata": ingest_payload["paper_metadata"],
        "n_figures": n_fig,
        "n_tables": n_tab,
        "n_localized": n_loc,
        "n_flagged": n_flag,
        "crops": crops,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return manifest


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="Stage 1: build the digest-input bundle for a paper.")
    ap.add_argument("source", help="PDF path, arXiv id (1706.03762), or arXiv URL")
    ap.add_argument("--out-dir", help="bundle output dir (default: out/<stem>)")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args(argv)

    m = prepare(args.source, out_dir=args.out_dir, dpi=args.dpi)
    print(f"Source bundle -> {m['out_dir']}")
    print(f"  title    : {m['title']!r}")
    print(f"  pages    : {m['n_pages']}   arXiv: {m['arxiv_id']}")
    print(f"  figures  : {m['n_figures']}    tables: {m['n_tables']} (rebuilt as data downstream)")
    print(f"  localized: {m['n_localized']}/{m['n_figures'] + m['n_tables']} assets")
    if m["n_flagged"]:
        print(f"  FLAGGED  : {m['n_flagged']} asset(s) without a reliable bbox "
              f"-> confirm/crop manually at CKPT-1")
    print("Next: read references/ingestion.md, build the typed digest from this bundle, "
          "then run CKPT-1 with the user.")


if __name__ == "__main__":
    main(sys.argv[1:])
