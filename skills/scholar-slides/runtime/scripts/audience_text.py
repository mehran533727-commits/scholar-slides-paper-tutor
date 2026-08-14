"""Audience-facing text boundary for evidence records.

Evidence records may contain provenance explanations that are useful for audits but
are not spoken or rendered to a research audience.  This module keeps that boundary
deterministic and paper-agnostic: it removes only recognizable workflow prose and
preserves the factual prefix before the explanation begins.
"""
from __future__ import annotations

import re
from typing import Any


PROVENANCE_LEAK_RE = re.compile(
    r"(?:\bdirect[_ -]?source[_ -]?evidence\b|\blocator\s+index\b|\bsource\s+pointer\b|"
    r"\bjson\s+pointer\b|\baudit\s+hash\b|(?:当前|本次)\s*digest\b|"
    r"\bdigest\s+(?:的|has|contains)\s+(?:可验证的?\s*)?(?:locator|source|index)\b)",
    re.IGNORECASE,
)

# Workflow vocabulary may be useful in review artifacts, but it is not an
# audience-facing result, caption, or provenance label.  Keep this list generic
# and structural: paper titles, asset names, and ordinary page/table locators are
# intentionally absent.
AUDIENCE_INTERNAL_PROCESS_RE = re.compile(
    r"(?:\b(?:audit(?:ed|ing)?\s+(?:evidence|comparison|result)|evidence\s+audit|"
    r"reviewed\s+evidence|bound\s+evidence|coverage\s+(?:requirement|id)|evidence\s+id|"
    r"marker\s+ledger|artifact\s+bundle|json\s+pointer|pending[_ -]?human[_ -]?confirmation|"
    r"resolved[_ -]?with[_ -]?audit|checkpoint|ckpt|sha[- ]?256)\b|"
    r"(?:已审计|审计结果|证据审计|已绑定(?:的)?证据|已确认(?:的)?(?:论文)?证据|"
    r"已选(?:的)?(?:论文)?资产|覆盖要求|覆盖\s*ID|证据\s*ID|标记账本|制品包|待人工确认))",
    re.IGNORECASE,
)

PDF_HYPHENATION_RE = re.compile(r"(?<=[A-Za-z])-\s+(?=[A-Za-z])")
_PROVENANCE_BRACKET_RE = re.compile(
    r"^\s*\[(?:直接证据|direct\s+evidence|source\s+evidence|provenance)[^\]]*\]\s*",
    re.IGNORECASE,
)

# These patterns are deliberately structural rather than paper-specific.  They
# protect mathematical spans while an audience-facing narrative removes isolated
# Latin labels and locator numbers, and they mask only numbers that function as
# source/model identifiers when deriving speaker-note numeric provenance.
REFERENCE_NUMBER_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])(?:figure|fig\.?|table|tab\.?|equation|eq\.?|page|p\.)\s*(?:\(\s*)?\d+(?:\.\d+)*(?:\s*\))?(?:\s*[-\u2013\u2014]\s*\d+)?|"
    r"(?<![A-Za-z0-9_])(?:section|sec\.?|subsection|paragraph|par\.?|appendix|part)\s*\d+(?:\.\d+)*(?:\s*[-\u2013\u2014]\s*\d+)?|"
    r"(?<![A-Za-z0-9_])(?:图|表|公式|第)\s*\d+(?:\.\d+)*(?:\s*[-\u2013\u2014]\s*\d+)?)",
    re.IGNORECASE,
)
_CITATION_NUMBER_RE = re.compile(r"\[\s*\d+(?:\s*[,;]\s*\d+)*\s*\]")
_MODEL_IDENTIFIER_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b|"
    r"\b[A-Za-z]+\d+(?:\.\d+)?(?:[-_][A-Za-z0-9]+)*\b|"
    r"\b[A-Z][A-Za-z]+\s+\d+(?:\.\d+)?(?:[-_][A-Za-z0-9]+)+\b|"
    r"\b[A-Z][A-Za-z]+\s+\d+[A-Za-z]+\b)"
)
_STRUCTURED_MATH_RE = re.compile(
    r"(?:\$(?:\\.|[^$])+\$|\\\((?:\\.|[^)])+\\\)|\\\[(?:\\.|[^]])+\\\]|"
    r"(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9_]*\s*:\s*)?"
    r"\([^\n，。；;]{1,120}\)\s*(?:→|->|↦|=>)\s*"
    r"\[[^\]\n]{1,120}\]\s*(?:\(\s*\d+(?:\.\d+)?\s*\))?)"
)


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def protect_math_spans(value: Any) -> tuple[str, tuple[str, ...]]:
    """Replace structured math with non-alphanumeric sentinels for safe cleanup."""
    text = str(value or "")
    spans: list[str] = []

    def replace(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        # Private-use characters contain no letters or digits, so the caller's
        # prose cleanup cannot delete a variable or an equation number inside it.
        return "\ue000" + ("\ue001" * len(spans)) + "\ue002"

    return _STRUCTURED_MATH_RE.sub(replace, text), tuple(spans)


def restore_math_spans(value: Any, spans: tuple[str, ...]) -> str:
    """Restore spans produced by :func:`protect_math_spans` in source order."""
    text = str(value or "")
    for index, original in enumerate(spans, start=1):
        sentinel = "\ue000" + ("\ue001" * index) + "\ue002"
        text = text.replace(sentinel, original)
    return text


def mask_non_claim_numeric_spans(value: Any) -> str:
    """Hide locator, citation, math, and model-identifier spans before number extraction."""
    protected, spans = protect_math_spans(value)
    masked = REFERENCE_NUMBER_RE.sub(" ", protected)
    masked = _CITATION_NUMBER_RE.sub(" ", masked)
    masked = _MODEL_IDENTIFIER_RE.sub(" ", masked)
    # Do not restore math here: its variables and equation labels are precisely
    # the numeric tokens that must not become speaker claims.
    return masked


def sanitize_audience_text(value: Any, fallback: str = "") -> str:
    """Remove internal provenance explanations while preserving the factual claim."""
    text = _compact(value)
    if not text:
        return fallback
    text = _PROVENANCE_BRACKET_RE.sub("", text)
    match = PROVENANCE_LEAK_RE.search(text)
    if match:
        text = text[: match.start()]
    text = re.sub(r"\s*[，,、：:；;]\s*$", "", text).strip()
    return text or fallback


def repair_pdf_hyphenation(value: Any) -> str:
    """Join extraction-only word breaks without changing intentional compounds."""
    return PDF_HYPHENATION_RE.sub("", _compact(value))


def contains_provenance_language(value: Any) -> bool:
    """Return whether text contains the audience/provenance boundary vocabulary."""
    return bool(PROVENANCE_LEAK_RE.search(_compact(value)))
