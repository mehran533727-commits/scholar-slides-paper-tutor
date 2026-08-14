"""Compatibility checks for legacy quantitative CKPT-1 evidence fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


LEGACY_QUANTITATIVE_FIELDS = (
    "proposed_claims",
    "proposed_experimental_results",
    "proposed_key_metrics",
)


def _sources(digest: Mapping[str, Any]) -> list[Any]:
    explicit = digest.get("quantitative_evidence")
    if isinstance(explicit, Mapping):
        sources = explicit.get("sources", [])
        return list(sources) if isinstance(sources, list) else []
    if isinstance(explicit, list):
        return list(explicit)
    inferred: list[Any] = []
    figures = digest.get("figures")
    if isinstance(figures, list):
        for record in figures:
            if not isinstance(record, Mapping):
                continue
            kind = str(record.get("kind", "")).casefold()
            label = str(record.get("label", ""))
            caption = str(record.get("caption", "")).casefold()
            if kind == "table" or label.casefold().startswith("table ") or any(term in caption for term in ("success", "rate", "latency", "average", "accuracy", "trial")):
                inferred.append(dict(record))
    return inferred


def check_quantitative_compatibility(digest: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed when a source advertises quantitative evidence but the legacy
    review inputs consumed by Mode B are empty.

    This is intentionally a compatibility gate, not a migration of the
    quantitative pipeline.  Existing downstream consumers continue to read the
    three legacy arrays and the audit list.
    """
    sources = _sources(digest)
    explicit = digest.get("quantitative_evidence")
    expected = bool(explicit.get("expected")) if isinstance(explicit, Mapping) and "expected" in explicit else bool(sources)
    counts = {field: len(candidate.get(field, [])) if isinstance(candidate.get(field), list) else 0 for field in (*LEGACY_QUANTITATIVE_FIELDS, "evidence_audits")}
    missing_fields = [field for field in LEGACY_QUANTITATIVE_FIELDS if counts[field] == 0] if expected else []
    return {
        "status": "ready" if not missing_fields else "incomplete",
        "expected": expected,
        "ready": not missing_fields,
        "sources": sources,
        "source_count": len(sources),
        "counts": counts,
        "missing_fields": missing_fields,
    }

