#!/usr/bin/env python3
"""Build a generic, unapproved CKPT-1 review input from a fresh digest.

The utility only projects already extracted semantic slots and makes explicit
decisions for digest integrity markers.  It never records human approval and
does not reread the source PDF.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_match(flag: str, digest: dict[str, Any]) -> dict[str, Any] | None:
    lowered = flag.casefold()

    def contains_token(token: object) -> bool:
        if not isinstance(token, str) or not token.strip():
            return False
        return re.search(r"(?<![\w-])" + re.escape(token.casefold()) + r"(?![\w-])", lowered) is not None

    candidates: list[dict[str, Any]] = []
    for field in ("figures", "assets", "source_evidence", "source_locators"):
        records = digest.get(field)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                continue
            tokens = (record.get("id"), record.get("label"), record.get("source_ref"), record.get("source_locator"))
            if any(contains_token(token) for token in tokens):
                candidates.append(record)
    unique = {record["id"]: record for record in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _load_review_evidence(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("review evidence input must be an object")
    return payload


def build_candidate(project: str | Path, out: str | Path | None = None, *, evidence_input: str | Path | None = None) -> Path:
    root = Path(project).resolve(strict=True)
    digest_path = root / "digest.json"
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    if not isinstance(digest, dict):
        raise ValueError("digest.json must contain an object")
    review_evidence = _load_review_evidence(evidence_input)
    resolutions: list[dict[str, Any]] = []
    unresolved: list[str] = []
    forbidden: set[str] = set()
    for flag in digest.get("flags", []):
        if not isinstance(flag, str) or not flag:
            continue
        asset = _asset_match(flag, digest)
        if asset is not None:
            asset_id = str(asset["id"])
            resolutions.append({
                "marker": flag,
                "resolution": "excluded_from_deck",
                "classification": "noncritical",
                "asset_id": asset_id,
                "reason": "asset confidence or localisation is insufficient for deck selection; retain it for review but exclude it from deck generation",
            })
            forbidden.add(asset_id)
        else:
            resolutions.append({
                "marker": flag,
                "resolution": "unresolved",
                "classification": "critical",
                "reason": "no generic asset identity or audit was available for this source marker",
            })
            unresolved.append(flag)
    candidate: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scholar-slides-ckpt1-review",
        "status": "pending_human_confirmation",
        "source_digest": {"path": "digest.json", "sha256": _sha256(digest_path)},
        "metadata_corrections": {},
        "proposed_claims": [],
        "proposed_contributions": [],
        "proposed_experimental_results": [],
        "proposed_key_metrics": [],
        "evidence_audits": [],
        "marker_resolutions": resolutions,
        "unresolved_markers": unresolved,
        "deck_forbidden_assets": sorted(forbidden),
    }
    # Preserve reviewer-supplied legacy evidence fields.  The builder does not
    # trust them: prepare_checkpoint still performs source-locator, audit, and
    # marker-policy validation before the candidate can be bound.
    for field in (
        "proposed_claims",
        "proposed_contributions",
        "proposed_experimental_results",
        "proposed_key_metrics",
        "evidence_audits",
        "semantic_corrections",
        "marker_resolutions",
        "unresolved_markers",
        "deck_forbidden_assets",
    ):
        if field in review_evidence:
            value = review_evidence[field]
            if not isinstance(value, list):
                raise ValueError(f"review evidence field {field} must be an array")
            candidate[field] = deepcopy(value)
    if "paper_semantics" in digest:
        candidate["paper_semantics"] = digest["paper_semantics"]
    if "mode_b_narrative_readiness" in digest:
        candidate["mode_b_narrative_readiness"] = digest["mode_b_narrative_readiness"]
    target = Path(out) if out is not None else root / "ckpt1-review.input.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build an unapproved CKPT-1 review input from digest.json.")
    parser.add_argument("project")
    parser.add_argument("--out")
    parser.add_argument("--evidence-input", help="optional source-bound reviewer evidence JSON to project into legacy CKPT-1 fields")
    args = parser.parse_args(argv)
    try:
        print(f"CKPT-1 review input -> {build_candidate(args.project, args.out, evidence_input=args.evidence_input)}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"build_ckpt1_candidate: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
