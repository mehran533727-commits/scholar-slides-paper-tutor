"""Collect confirmed quantitative facts as first-class visible coverage requirements.

0.1.8 treats reviewed key metrics, numeric experimental results, and audited
two-row comparisons as required visible deck content.  This module is the single
generic source of those requirements: the narrative planner assigns them, the
deck generator renders them, and semantic QA verifies them.  It contains no
paper-specific identifiers or values.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from audience_text import mask_non_claim_numeric_spans
from evidence_audit import EvidenceAuditError, validate_evidence_audit_schema
from schema_validation import create_schema_validator, resolve_skill_schema_path


SCHEMA_VERSION = 1
KIND = "scholar-slides-quantitative-coverage"

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9.]+")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_QUANTITATIVE_RE = re.compile(
    r"\d+\.\d+|\d+(?:\.\d+)?\s*[%％]|\d+(?:\.\d+)?\s*[×x](?!\w)",
    re.IGNORECASE,
)
_QUANTITATIVE_CONTEXT_RE = re.compile(
    r"\b(?:accuracy|average|complete|coverage|latency|metric|percent|rate|score|success|trial|trials|valid)\b|[%％]",
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "baseline", "by", "data", "for",
        "from", "in", "is", "of", "on", "over", "setting", "settings", "than",
        "the", "text", "to", "versus", "vs", "with",
    }
)
_REQUIRED_FIELDS = (
    "id",
    "kind",
    "label",
    "display_text",
    "required",
    "source",
    "coverage_tokens",
    "priority",
)
_KINDS = ("key_metric", "quantitative_result", "pairwise_audit_comparison", "scientific_result")
_DISPLAY_SEPARATOR = "；"
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_APPENDIX_RE = re.compile(r"\bappendix\b|\bsupplement(?:ary)?\b|附录|补充材料", re.IGNORECASE)
_NEGATIVE_OR_TRADEOFF_RE = re.compile(
    r"\b(?:counterexample|negative result|underperform(?:s|ed)?|below|worse|failure|fails?|"
    r"trade[- ]?off|does not|do not|cannot|limitation)\b|"
    r"反例|负向|低于|不稳定|不保证|未能|无法|失败|权衡|上限|局限",
    re.IGNORECASE,
)
_ROBUSTNESS_RE = re.compile(r"\b(?:multi[- ]?seed|random seeds?|robustness|variance|standard deviation)\b|多随机种子|稳健性|标准差", re.IGNORECASE)
_METHOD_EVIDENCE_OVERLAP = 0.22
_DIMENSION_STOPWORDS = _STOPWORDS | frozenset({
    "figure", "table", "shows", "show", "shown", "result", "results", "method",
    "model", "models", "proposed", "paper", "study", "whether", "which", "how",
    "论文", "结果", "方法", "研究", "显示", "提出", "以及", "通过",
})


class QuantitativeCoverageError(ValueError):
    """Raised when confirmed quantitative evidence cannot be normalized safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any, fallback: str = "") -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return fallback


def _numeric_tokens(value: Any) -> list[str]:
    """Return claim numeric tokens without locator/model/math identifiers."""
    tokens: list[str] = []
    masked = mask_non_claim_numeric_spans(_text(value))
    for match in _NUMBER_RE.finditer(masked):
        token = match.group(0)
        if _YEAR_RE.fullmatch(token):
            continue
        tokens.append(token)
    return sorted(set(tokens))


def _is_quantitative_text(value: Any) -> bool:
    """Recognize decimal/percent/ratio values and integer rate tables.

    Integer-only tables often write the unit once in a heading (for example
    ``success rate (%)``) and then list cells such as ``80/80/60``.  The old
    token rule missed those facts because no integer was immediately followed
    by a percent sign.  Require a quantitative context word as well as a
    non-year numeric token so ordinary prose and publication years remain out.
    """
    text = _text(value)
    return bool(
        _QUANTITATIVE_RE.search(text)
        or (_numeric_tokens(text) and _QUANTITATIVE_CONTEXT_RE.search(text))
    )


def _label_tokens(label: Any) -> list[str]:
    """Return casefolded word tokens that give a number its expected context."""
    return sorted(
        {
            match.group(0)
            for match in _WORD_TOKEN_RE.finditer(_text(label).casefold())
            if match.group(0)
        }
    )


def _context_label(text: str) -> str:
    """Derive the nearest noun-phrase context before the first numeric token."""
    # A bare digit inside an acronym (for example the ``3`` in ``ACME3D``) is
    # not a data value; only decimal, percent, or ratio tokens define the value
    # position whose preceding phrase is the metric context label.
    first = next(_QUANTITATIVE_RE.finditer(text), None)
    if first is None:
        return ""
    words = [match.group(0) for match in _WORD_TOKEN_RE.finditer(text[: first.start()])]
    selected: list[str] = []
    for word in reversed(words):
        if word.casefold() in _STOPWORDS:
            if selected:
                break
            continue
        selected.append(word)
        if len(selected) >= 3:
            break
    return " ".join(reversed(selected))


def _coverage_tokens(*parts: Any) -> list[str]:
    tokens: set[str] = set()
    for part in parts:
        if isinstance(part, (list, tuple)):
            for item in part:
                tokens.update(_numeric_tokens(item))
                tokens.update(_label_tokens(item))
        else:
            tokens.update(_numeric_tokens(part))
            tokens.update(_label_tokens(part))
    normalized = {re.sub(r"(?<=\d)[x×]", "", token) for token in tokens}
    return sorted(normalized)


def _source_from_evidence(evidence: Any, *, locator_fallback: str = "", page_fallback: int = 0) -> dict[str, Any]:
    record = evidence if isinstance(evidence, Mapping) else {}
    locator = _text(record.get("locator"), locator_fallback)
    page = record.get("page")
    if not isinstance(page, int) or page < 1:
        page = page_fallback
    return {"locator": locator, "page": page, "section": locator}


def _semantic_tokens(value: Any) -> set[str]:
    """Return deterministic multilingual tokens for scientific relevance only.

    Quantitative coverage keeps its established numeric/label token contract.
    This tokenizer is deliberately separate so adding CJK relevance does not
    make existing native-table coverage stricter.
    """
    text = _text(value).casefold()
    tokens = {
        match.group(0)
        for match in _WORD_TOKEN_RE.finditer(text)
        if len(match.group(0)) >= 3 and match.group(0) not in _DIMENSION_STOPWORDS
    }
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) <= 4 and run not in _DIMENSION_STOPWORDS:
            tokens.add(run)
        for width in (2, 3):
            for index in range(max(0, len(run) - width + 1)):
                token = run[index:index + width]
                if token not in _DIMENSION_STOPWORDS:
                    tokens.add(token)
    return tokens


def _scientific_coverage_tokens(label: str, summary: str) -> list[str]:
    tokens = _semantic_tokens(f"{label} {summary}")
    # Source locators remain part of the audience-facing claim context.  Keep
    # the complete semantic token set because the exact reviewed summary is
    # rendered natively for scientific-result requirements.
    return sorted(tokens or set(_label_tokens(label)) or {"evidence"})


def _source_scope(item: Mapping[str, Any], locator: str) -> str:
    return "appendix" if item.get("appendix") is True or _APPENDIX_RE.search(locator) else "main_text"


def _evidence_form(locator: str) -> str:
    lowered = locator.casefold()
    if re.search(r"\bfig(?:ure)?\b|图\s*\d", lowered):
        return "figure"
    if re.search(r"\btable\b|表\s*\d", lowered):
        return "table"
    if re.search(r"\balgorithm\b|算法\s*\d", lowered):
        return "algorithm"
    return "text"


def _importance(item: Mapping[str, Any]) -> str:
    value = _text(item.get("importance"), "core").casefold().replace("-", "_")
    return value if value in {"core", "supporting", "optional"} else "core"


def _annotate_priority_inputs(
    requirement: Mapping[str, Any], item: Mapping[str, Any],
) -> dict[str, Any]:
    output = dict(requirement)
    source = output.get("source") if isinstance(output.get("source"), Mapping) else {}
    locator = _text(source.get("locator"), "")
    text = " ".join((
        _text(output.get("label")), _text(output.get("display_text")), locator,
    ))
    output.update({
        "source_scope": _source_scope(item, locator),
        "evidence_form": _evidence_form(locator),
        "importance": _importance(item),
        "source_evidence_id": _text(item.get("id"), _text(output.get("id"))),
        "negative_or_tradeoff": bool(_NEGATIVE_OR_TRADEOFF_RE.search(text)),
        "robustness_support": bool(_ROBUSTNESS_RE.search(text)),
    })
    return output


def _scientific_result_requirement(item: Mapping[str, Any], summary: str) -> dict[str, Any] | None:
    locator = _text(item.get("figure_table_equation"), _text(item.get("section"), "scientific result"))
    if not summary or not locator:
        return None
    identity = _text(item.get("id")) or hashlib.sha256(
        f"{locator}|{summary}".encode("utf-8")
    ).hexdigest()[:10]
    requirement = {
        "id": f"qscience-{re.sub(r'[^a-z0-9]+', '-', identity.casefold()).strip('-') or hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}",
        "kind": "scientific_result",
        "label": locator,
        "display_text": summary,
        "required": True,
        "source": _source_from_evidence(
            {"locator": item.get("figure_table_equation"), "page": item.get("source_page")},
            locator_fallback=_text(item.get("section"), ""),
            page_fallback=0,
        ),
        "coverage_tokens": _scientific_coverage_tokens(locator, summary),
        "priority": 30,
        "speaker_focus": f"{locator} 的核心科学发现",
        "speaker_key_values": _numeric_tokens(summary),
    }
    return _annotate_priority_inputs(requirement, item)


def _key_metric_requirement(metric: Mapping[str, Any]) -> dict[str, Any] | None:
    label = _text(metric.get("label"))
    value = metric.get("value")
    if not label or value is None or value == "":
        return None
    metric_id = _text(metric.get("id"))
    if metric_id:
        requirement_id = f"qmetric-{metric_id}"
    else:
        slug = "-".join(_label_tokens(label)) or "metric"
        requirement_id = f"qmetric-{slug}"
    display = f"{label} = {_text(value)}"
    tokens = _coverage_tokens(label, value)
    if not any(re.fullmatch(r"\d+(?:\.\d+)?", token) for token in tokens):
        # A nonnumeric but meaningful metric value still requires visible context.
        tokens = _coverage_tokens(label, _text(value))
    requirement = {
        "id": requirement_id,
        "kind": "key_metric",
        "label": label,
        "display_text": display,
        "required": True,
        "source": _source_from_evidence(metric.get("evidence")),
        "coverage_tokens": tokens,
        "priority": 20,
        "speaker_focus": label,
        "speaker_key_values": _numeric_tokens(value),
    }
    return requirement


def _audit_speaker_values(audit: Mapping[str, Any] | None) -> list[str]:
    values: list[str] = []
    cells = audit.get("reviewed_cells") if isinstance(audit, Mapping) else []
    for cell in cells if isinstance(cells, list) else []:
        if not isinstance(cell, Mapping):
            continue
        value = cell.get("normalized_value", cell.get("value"))
        for token in _numeric_tokens(value):
            if token not in values:
                values.append(token)
    return values


def _quantitative_result_requirement(item: Mapping[str, Any], summary: str, audit: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    numeric = _numeric_tokens(summary)
    if not numeric:
        return None
    locator = _text(item.get("figure_table_equation"), _text(item.get("section"), "quantitative result"))
    label = locator or _context_label(summary) or "quantitative result"
    digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:10]
    requirement = {
        "id": f"qresult-{digest}",
        "kind": "quantitative_result",
        "label": label,
        "display_text": summary,
        "required": True,
        "source": _source_from_evidence(
            {"locator": item.get("figure_table_equation"), "page": item.get("source_page")},
            locator_fallback=_text(item.get("section"), ""),
            page_fallback=0,
        ),
        "coverage_tokens": _coverage_tokens(label, summary, numeric),
        "priority": 10,
        "speaker_focus": f"{label} 的关键定量结果",
        "speaker_key_values": _audit_speaker_values(audit) or numeric,
    }
    return requirement


def _load_audit(project: Path, audit_ref: Mapping[str, Any]) -> dict[str, Any]:
    raw_path = _text(audit_ref.get("path"))
    expected_sha = _text(audit_ref.get("sha256"))
    relative = Path(raw_path)
    if not raw_path or not expected_sha or relative.is_absolute() or ".." in relative.parts:
        raise QuantitativeCoverageError("quantitative audit_ref must be a portable project-relative path")
    full = (project / relative).resolve()
    if full != project.resolve() and project.resolve() not in full.parents:
        raise QuantitativeCoverageError("quantitative audit_ref escapes the project directory")
    if not full.is_file():
        raise QuantitativeCoverageError(f"quantitative audit file is missing: {raw_path}")
    if sha256_file(full) != expected_sha:
        raise QuantitativeCoverageError(f"quantitative audit SHA-256 mismatch: {raw_path}")
    try:
        payload = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuantitativeCoverageError(f"invalid quantitative audit JSON: {raw_path}") from exc
    if not isinstance(payload, dict):
        raise QuantitativeCoverageError(f"quantitative audit must be an object: {raw_path}")
    try:
        validate_evidence_audit_schema(payload)
    except EvidenceAuditError as exc:
        raise QuantitativeCoverageError(f"quantitative audit schema validation failed: {raw_path}: {exc}") from exc
    evidence_type = _text(payload.get("evidence_type"), "table")
    cells = payload.get("reviewed_cells")
    if evidence_type == "table" and (not isinstance(cells, list) or not cells):
        raise QuantitativeCoverageError(f"quantitative table audit has no reviewed_cells: {raw_path}")
    return payload


def _pairwise_requirement(
    result_item: Mapping[str, Any],
    audit: Mapping[str, Any],
    audit_ref: Mapping[str, Any],
) -> dict[str, Any] | None:
    cells = audit.get("reviewed_cells")
    row_order: list[str] = []
    column_order: list[str] = []
    by_cell: dict[tuple[str, str], str] = {}
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise QuantitativeCoverageError("quantitative audit reviewed_cells must contain objects")
        row = _text(cell.get("row_key"))
        column = _text(cell.get("column_key"))
        value = cell.get("value")
        if value is None:
            value = cell.get("normalized_value")
        if not row or not column or value is None:
            raise QuantitativeCoverageError("quantitative audit cell requires row_key, column_key and value")
        if row not in row_order:
            row_order.append(row)
        if column not in column_order:
            column_order.append(column)
        by_cell[(row, column)] = _text(value)
    if len(row_order) != 2:
        return None
    if not 1 <= len(column_order) <= 2:
        return None
    lines: list[str] = []
    tokens: list[Any] = []
    for column in column_order:
        first = by_cell[(row_order[0], column)]
        second = by_cell[(row_order[1], column)]
        lines.append(f"{column}: {first} → {second}")
        tokens.extend([first, second, column])
    display = _DISPLAY_SEPARATOR.join(lines)
    locator = _text(result_item.get("figure_table_equation"), _text(result_item.get("section"), "Table"))
    identity = hashlib.sha256(
        f"{_text(audit_ref.get('path'))}|{'|'.join(row_order)}|{'|'.join(column_order)}".encode("utf-8")
    ).hexdigest()[:10]
    return {
        "id": f"qpair-{identity}",
        "kind": "pairwise_audit_comparison",
        "label": "、".join(column_order),
        "display_text": display,
        "required": True,
        "source": _source_from_evidence(
            {"locator": locator, "page": result_item.get("source_page")},
            locator_fallback=locator,
            page_fallback=0,
        ),
        "audit_ref": {"path": _text(audit_ref.get("path")), "sha256": _text(audit_ref.get("sha256"))},
        "coverage_tokens": _coverage_tokens(tokens),
        "priority": 10,
        "speaker_focus": f"{locator} 的关键定量结果",
        "speaker_key_values": _audit_speaker_values(audit),
    }


def _load_confirmed_review(project: Path) -> dict[str, Any] | None:
    path = project / "ckpt1-review.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuantitativeCoverageError("confirmed ckpt1-review.json is invalid") from exc
    return payload if isinstance(payload, dict) else None


def _audit_refs_for_results(
    review: Mapping[str, Any] | None,
    semantic_digest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    if review is None:
        return {}
    candidates = review.get("proposed_experimental_results")
    if not isinstance(candidates, list):
        return {}
    mapping: dict[str, Mapping[str, Any]] = {}
    for item in semantic_digest.get("reviewed_experimental_results", []) or []:
        if not isinstance(item, Mapping):
            continue
        locator = _text(item.get("figure_table_equation")).casefold()
        if not locator:
            continue
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), Mapping) else {}
            if _text(evidence.get("locator")).casefold() != locator:
                continue
            audit_ref = candidate.get("audit_ref")
            if isinstance(audit_ref, Mapping) and _text(audit_ref.get("path")) and _text(audit_ref.get("sha256")):
                mapping[locator] = dict(audit_ref)
            break
    return mapping


def _semantic_slots(semantic_digest: Mapping[str, Any]) -> Mapping[str, Any]:
    semantics = semantic_digest.get("reviewed_paper_semantics")
    if not isinstance(semantics, Mapping):
        return {}
    slots = semantics.get("slots")
    return slots if isinstance(slots, Mapping) else {}


def _split_dimension_summary(value: Any) -> list[str]:
    text = _text(value)
    if not text:
        return []
    prefix, separator, remainder = text.partition("：")
    if not separator:
        prefix, separator, remainder = text.partition(":")
    if separator and len(prefix) <= 48 and re.search(
        r"question|objective|contribution|ask|论文|问题|贡献", prefix, re.IGNORECASE
    ):
        text = remainder
    numbered = re.split(r"\s*\((?:i{1,4}|[1-9])\)\s*", text, flags=re.IGNORECASE)
    if len(numbered) > 2:
        pieces = numbered[1:]
    else:
        pieces = re.split(r"\s*[;；]\s*", text)
        if len(pieces) == 1 and "、" in text:
            pieces = re.split(r"\s*(?:、|，?以及|，?并且)\s*", text)
    output: list[str] = []
    for piece in pieces:
        cleaned = re.sub(
            r"^(?:and\s+|or\s+|以及|并且|以及是否|是否)", "", piece.strip(" ,，。;；"),
            flags=re.IGNORECASE,
        ).strip()
        if len(cleaned) >= 6:
            output.append(cleaned)
    return output


def _research_dimensions(semantic_digest: Mapping[str, Any]) -> list[dict[str, Any]]:
    slots = _semantic_slots(semantic_digest)
    dimensions: list[dict[str, Any]] = []
    for slot_name in ("objective_or_research_question", "contributions"):
        slot = slots.get(slot_name)
        if not isinstance(slot, Mapping):
            continue
        clauses = _split_dimension_summary(slot.get("summary", slot.get("text")))
        source_clauses: list[str] = []
        for source_ref in slot.get("source_refs", []) or []:
            if not isinstance(source_ref, Mapping):
                continue
            source_text = _text(source_ref.get("source_text"))
            split_source = _split_dimension_summary(source_text)
            source_clauses.extend(split_source or ([source_text] if source_text else []))
        for clause_index, clause in enumerate(clauses):
            aliases = (
                [source_clauses[clause_index]]
                if len(source_clauses) == len(clauses)
                else []
            )
            tokens = _semantic_tokens(" ".join([clause, *aliases]))
            if not tokens:
                continue
            duplicate = next((
                item for item in dimensions
                if len(tokens & set(item["tokens"])) >= 3
                and len(tokens & set(item["tokens"])) / max(1, min(len(tokens), len(item["tokens"]))) >= 0.50
            ), None)
            if duplicate is not None:
                if slot_name not in duplicate["origin_slots"]:
                    duplicate["origin_slots"].append(slot_name)
                continue
            dimensions.append({
                "id": f"rq-{len(dimensions) + 1}",
                "text": clause,
                "origin_slots": [slot_name],
                "source_aliases": aliases,
                "tokens": sorted(tokens),
            })
    return dimensions


def _candidate_dimension_scores(
    candidate: Mapping[str, Any], dimensions: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    tokens = _semantic_tokens(
        " ".join((
            _text(candidate.get("label")),
            _text(candidate.get("display_text")),
            _text(candidate.get("source_evidence_id")),
            _text((candidate.get("source") or {}).get("locator") if isinstance(candidate.get("source"), Mapping) else ""),
        ))
    )
    frequency: dict[str, int] = {}
    for dimension in dimensions:
        for token in set(dimension.get("tokens", []) or []):
            frequency[token] = frequency.get(token, 0) + 1
    scores: dict[str, int] = {}
    for dimension in dimensions:
        score = 0
        for token in tokens & set(dimension.get("tokens", []) or []):
            score += 1
            if frequency.get(token, 0) == 1:
                score += 1
            if (bool(_CJK_RUN_RE.fullmatch(token)) and len(token) >= 3) or (
                token.isascii() and len(token) >= 7
            ):
                score += 1
        scores[_text(dimension.get("id"))] = score
    return scores


def _collect_priority_candidates(
    *, project: Path, semantic_digest: Mapping[str, Any], scientific_mode: bool,
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for metric in semantic_digest.get("reviewed_key_metrics", []) or []:
        if not isinstance(metric, Mapping):
            continue
        requirement = _key_metric_requirement(metric)
        if requirement is not None:
            requirements.append(_annotate_priority_inputs(requirement, metric))
    if scientific_mode:
        for item in semantic_digest.get("reviewed_claims", []) or []:
            if not isinstance(item, Mapping):
                continue
            locator = _text(item.get("figure_table_equation"), _text(item.get("section"), ""))
            if _evidence_form(locator) not in {"figure", "table"}:
                continue
            requirement = _scientific_result_requirement(item, _text(item.get("summary")))
            if requirement is not None:
                requirements.append(requirement)
    review = _load_confirmed_review(project)
    audit_refs = _audit_refs_for_results(review, semantic_digest)
    for item in semantic_digest.get("reviewed_experimental_results", []) or []:
        if not isinstance(item, Mapping):
            continue
        summary = _text(item.get("summary"))
        audit_ref = audit_refs.get(_text(item.get("figure_table_equation")).casefold())
        audit = _load_audit(project, audit_ref) if audit_ref is not None else None
        if _is_quantitative_text(summary):
            requirement = _quantitative_result_requirement(item, summary, audit=audit)
            if requirement is not None:
                if audit_ref is not None:
                    requirement["audit_ref"] = {
                        "path": _text(audit_ref.get("path")),
                        "sha256": _text(audit_ref.get("sha256")),
                    }
                requirements.append(_annotate_priority_inputs(requirement, item))
        elif scientific_mode:
            requirement = _scientific_result_requirement(item, summary)
            if requirement is not None:
                if audit_ref is not None:
                    requirement["audit_ref"] = {
                        "path": _text(audit_ref.get("path")),
                        "sha256": _text(audit_ref.get("sha256")),
                    }
                requirements.append(requirement)
        if audit_ref is not None and audit is not None and _text(audit.get("evidence_type"), "table") == "table":
            pairwise = _pairwise_requirement(item, audit, audit_ref)
            if pairwise is not None:
                requirements.append(_annotate_priority_inputs(pairwise, item))
    return _dedupe(requirements)


def _priority_projection(candidate: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "id", "kind", "label", "display_text", "source", "source_scope", "evidence_form",
        "importance", "source_evidence_id", "negative_or_tradeoff", "robustness_support",
        "priority_tier", "scientific_priority_reason", "research_question_dimensions",
    )
    return {field: candidate[field] for field in fields if field in candidate}


def _build_scientific_priority_report(
    *, project: Path, semantic_digest: Mapping[str, Any],
) -> dict[str, Any]:
    dimensions = _research_dimensions(semantic_digest)
    scientific_mode = bool(dimensions)
    candidates = _collect_priority_candidates(
        project=project, semantic_digest=semantic_digest, scientific_mode=scientific_mode
    )
    if not scientific_mode:
        selected = []
        for candidate in candidates:
            item = dict(candidate)
            item.update({
                "priority_tier": "legacy-required",
                "scientific_priority_reason": "legacy confirmed quantitative contract",
                "research_question_dimensions": [],
            })
            selected.append(item)
        return {
            "schema_version": 1,
            "mode": "legacy-quantitative",
            "research_dimensions": [],
            "dimension_coverage": [],
            "selected_requirements": selected,
            "optional_candidates": [],
        }

    scores = {
        _text(candidate.get("id")): _candidate_dimension_scores(candidate, dimensions)
        for candidate in candidates
    }
    approach = _semantic_slots(semantic_digest).get("approach")
    approach_summary = (
        _text(approach.get("summary", approach.get("text")))
        if isinstance(approach, Mapping) else ""
    )
    approach_tokens = _semantic_tokens(approach_summary)
    approach_scores = _candidate_dimension_scores(
        {"label": "approach", "display_text": approach_summary, "source": {}}, dimensions
    )
    method_dimension_ids = {
        _text(dimension.get("id"))
        for dimension in dimensions
        if set(dimension.get("origin_slots", []) or []) == {"contributions"}
        and approach_scores.get(_text(dimension.get("id")), 0) >= 3
    }
    method_like_candidates: set[str] = set()
    for candidate in candidates:
        if candidate.get("kind") != "scientific_result":
            continue
        candidate_tokens = _semantic_tokens(
            f"{_text(candidate.get('label'))} {_text(candidate.get('display_text'))}"
        )
        overlap = len(candidate_tokens & approach_tokens) / max(
            1, min(len(candidate_tokens), len(approach_tokens))
        )
        if overlap >= _METHOD_EVIDENCE_OVERLAP:
            method_like_candidates.add(_text(candidate.get("id")))
    dimension_winners: dict[str, str] = {}
    for dimension in dimensions:
        dimension_id = _text(dimension.get("id"))
        if dimension_id in method_dimension_ids:
            continue
        result_candidates = [
            item for item in candidates
            if _text(item.get("id")) not in method_like_candidates
        ]
        ranked = sorted(
            result_candidates,
            key=lambda item: (
                scores[_text(item.get("id"))].get(dimension_id, 0),
                1 if item.get("source_scope") == "main_text" else 0,
                1 if item.get("importance") == "core" else 0,
                int(item.get("priority", 0)),
                _text(item.get("id")),
            ),
            reverse=True,
        )
        if ranked and scores[_text(ranked[0].get("id"))].get(dimension_id, 0) >= 3:
            dimension_winners[dimension_id] = _text(ranked[0].get("id"))

    selected: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        candidate_id = _text(item.get("id"))
        won_dimensions = sorted(
            dimension_id for dimension_id, winner in dimension_winners.items()
            if winner == candidate_id
        )
        item["research_question_dimensions"] = won_dimensions
        item_tokens = _semantic_tokens(
            f"{_text(item.get('label'))} {_text(item.get('display_text'))}"
        )
        method_overlap = len(item_tokens & approach_tokens) / max(1, min(len(item_tokens), len(approach_tokens)))
        method_like = item.get("kind") == "scientific_result" and method_overlap >= _METHOD_EVIDENCE_OVERLAP
        is_appendix = item.get("source_scope") == "appendix"
        is_negative = bool(item.get("negative_or_tradeoff"))
        no_main_equivalent = bool(won_dimensions) and not any(
            other.get("source_scope") == "main_text"
            and any(scores[_text(other.get("id"))].get(dimension_id, 0) > 0 for dimension_id in won_dimensions)
            for other in candidates
        )
        if is_appendix:
            if is_negative:
                item.update({
                    "priority_tier": "tier-2-main-support",
                    "scientific_priority_reason": "appendix interpretation-changing negative/trade-off evidence",
                    "priority": max(int(item.get("priority", 0)), 22),
                })
                selected.append(item)
            elif no_main_equivalent:
                item.update({
                    "priority_tier": "tier-2-main-support",
                    "scientific_priority_reason": "appendix evidence promoted because no main-text equivalent covers the research dimension",
                    "priority": max(int(item.get("priority", 0)), 21),
                })
                selected.append(item)
            else:
                item.update({
                    "required": False,
                    "priority_tier": "tier-3-appendix-support",
                    "scientific_priority_reason": (
                        "appendix robustness supporting evidence; not automatically required"
                        if item.get("robustness_support")
                        else "appendix supporting evidence; no promotion condition established"
                    ),
                })
                optional.append(item)
            continue
        if won_dimensions:
            item.update({
                "priority_tier": "tier-1-core-question",
                "scientific_priority_reason": "direct evidence for a reviewed research-question/contribution dimension",
                "priority": max(int(item.get("priority", 0)), 40),
            })
            selected.append(item)
        elif item.get("importance") == "core" and not method_like:
            item.update({
                "priority_tier": "tier-2-main-support",
                "scientific_priority_reason": "core main-text supporting evidence",
                "priority": max(int(item.get("priority", 0)), 30),
            })
            selected.append(item)
        else:
            item.update({
                "required": False,
                "priority_tier": "tier-4-optional-backup",
                "scientific_priority_reason": (
                    "method-explanation evidence is covered by the reviewed approach slot"
                    if method_like
                    else "supporting main-text evidence without a core dimension assignment"
                ),
            })
            optional.append(item)

    selected = _dedupe(selected)
    selected_ids = {_text(item.get("id")) for item in selected}
    dimension_coverage: list[dict[str, Any]] = []
    for dimension in dimensions:
        dimension_id = _text(dimension.get("id"))
        winner = dimension_winners.get(dimension_id, "")
        evidence_ids = [winner] if winner in selected_ids else []
        dimension_tokens = set(dimension.get("tokens", []) or [])
        method_covered = approach_scores.get(dimension_id, 0) >= 3
        status = "evidence" if evidence_ids else "method" if method_covered else "missing"
        dimension_coverage.append({
            "dimension_id": dimension_id,
            "text": _text(dimension.get("text")),
            "origin_slots": list(dimension.get("origin_slots", []) or []),
            "status": status,
            "evidence_ids": evidence_ids,
            "source_locators": [
                _text((item.get("source") or {}).get("locator"))
                for item in selected if _text(item.get("id")) in evidence_ids
            ],
        })
    return {
        "schema_version": 1,
        "mode": "research-question-aware",
        "research_dimensions": [
            {key: value for key, value in dimension.items() if key != "tokens"}
            for dimension in dimensions
        ],
        "dimension_coverage": dimension_coverage,
        "selected_requirements": selected,
        "optional_candidates": [_priority_projection(item) for item in optional],
    }


def scientific_priority_report(
    *, project_dir: str | Path, semantic_digest: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the deterministic research-question and appendix-priority audit."""
    project = Path(project_dir).resolve(strict=True)
    if not isinstance(semantic_digest, Mapping):
        raise QuantitativeCoverageError("semantic_digest must be an object")
    return _build_scientific_priority_report(project=project, semantic_digest=semantic_digest)


def collect_quantitative_requirements(
    *,
    project_dir: str | Path,
    semantic_digest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return source-bound scientific facts selected as required visible coverage."""
    project = Path(project_dir).resolve(strict=True)
    if not isinstance(semantic_digest, Mapping):
        raise QuantitativeCoverageError("semantic_digest must be an object")
    report = _build_scientific_priority_report(project=project, semantic_digest=semantic_digest)
    return [dict(item) for item in report["selected_requirements"]]


def _dedupe(requirements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output: list[dict[str, Any]] = []
    for requirement in requirements:
        tokens = " ".join(sorted(requirement.get("coverage_tokens", []) or []))
        key = (str(requirement.get("kind")), tokens)
        if key in seen:
            continue
        seen.add(key)
        output.append(dict(requirement))
    output.sort(key=lambda item: (-int(item.get("priority", 0)), str(item.get("id", ""))))
    return output


def _schema_path() -> Path:
    return resolve_skill_schema_path("quantitative-coverage.schema.json", anchor=__file__)


def _schema() -> dict[str, Any]:
    return json.loads(_schema_path().read_text(encoding="utf-8"))


def validate_quantitative_artifact(payload: Mapping[str, Any]) -> None:
    """Fail closed when a coverage artifact violates the portable schema."""
    if not isinstance(payload, Mapping):
        raise QuantitativeCoverageError("quantitative coverage artifact must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
        raise QuantitativeCoverageError("quantitative coverage artifact has the wrong schema/kind")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        raise QuantitativeCoverageError("quantitative coverage artifact requires a requirements list")
    seen: set[str] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, Mapping):
            raise QuantitativeCoverageError(f"requirements[{index}] must be an object")
        missing = [field for field in _REQUIRED_FIELDS if field not in requirement]
        if missing:
            raise QuantitativeCoverageError(f"requirements[{index}] missing {', '.join(missing)}")
        if requirement.get("kind") not in _KINDS:
            raise QuantitativeCoverageError(f"requirements[{index}] has an unknown kind")
        if not isinstance(requirement.get("coverage_tokens"), list) or not requirement["coverage_tokens"]:
            raise QuantitativeCoverageError(f"requirements[{index}] needs non-empty coverage_tokens")
        requirement_id = str(requirement.get("id"))
        if requirement_id in seen:
            raise QuantitativeCoverageError(f"duplicate requirement id: {requirement_id}")
        seen.add(requirement_id)
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        raise QuantitativeCoverageError("quantitative coverage artifact requires provenance")
    for field in ("digest_sha256", "checkpoint", "checkpoint_sha256"):
        if not _text(provenance.get(field)):
            raise QuantitativeCoverageError(f"quantitative coverage provenance.{field} is required")
    errors = sorted(create_schema_validator(_schema()).iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        raise QuantitativeCoverageError("; ".join(error.message for error in errors[:5]))


def build_coverage_artifact(
    *,
    project_dir: str | Path,
    semantic_digest: Mapping[str, Any],
    requirements: Sequence[Mapping[str, Any]],
    digest_sha256: str,
    checkpoint_sha256: str,
    review_sha256: str | None = None,
) -> dict[str, Any]:
    """Return the persisted, provenance-bound coverage artifact."""
    priority_report = scientific_priority_report(
        project_dir=project_dir, semantic_digest=semantic_digest
    )
    persisted_priority_report = {
        **priority_report,
        "selected_requirements": [
            _priority_projection(item)
            for item in priority_report.get("selected_requirements", [])
            if isinstance(item, Mapping)
        ],
    }
    audit_refs: list[dict[str, Any]] = []
    for requirement in requirements:
        audit_ref = requirement.get("audit_ref")
        if isinstance(audit_ref, Mapping) and audit_ref not in audit_refs:
            audit_refs.append({"path": _text(audit_ref.get("path")), "sha256": _text(audit_ref.get("sha256"))})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "requirements": [dict(requirement) for requirement in requirements],
        "provenance": {
            "digest_sha256": digest_sha256,
            "checkpoint": "CKPT-1",
            "checkpoint_sha256": checkpoint_sha256,
            "ckpt1_review_sha256": review_sha256 or "",
            "audits": audit_refs,
        },
        "scientific_priority": persisted_priority_report,
    }
    validate_quantitative_artifact(payload)
    return payload


def load_coverage_artifact(project_dir: str | Path) -> tuple[dict[str, Any], str] | None:
    """Load and validate the persisted coverage artifact, returning (payload, sha256)."""
    project = Path(project_dir).resolve(strict=True)
    path = project / "coverage-requirements.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuantitativeCoverageError("coverage-requirements.json is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise QuantitativeCoverageError("coverage-requirements.json must be an object")
    validate_quantitative_artifact(payload)
    return payload, sha256_file(path)


def load_quantitative_audit(project_dir: str | Path, audit_ref: Mapping[str, Any]) -> dict[str, Any]:
    """Load one already hash-bound evidence audit for native rendering."""
    return _load_audit(Path(project_dir).resolve(strict=True), audit_ref)


def display_lines(requirement: Mapping[str, Any]) -> list[str]:
    """Return the visible text lines that must render for one requirement."""
    display = _text(requirement.get("display_text"))
    lines = [line.strip() for line in display.split(_DISPLAY_SEPARATOR) if line.strip()]
    return lines or ["[MISSING: quantitative display text]"]


def _visible_slide_strings(slide: Mapping[str, Any]) -> list[str]:
    """Collect renderable slide text while excluding notes and internal selection state."""
    fields = (
        "title", "authors", "venue", "presenter", "eyebrow", "action_title",
        "core_conclusion", "annotation", "source_ref", "points", "points2",
        "items", "questions", "entries",
    )
    values: list[str] = []
    for field in fields:
        value = slide.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value if item is not None)
        elif isinstance(value, Mapping):
            values.extend(str(child) for child in value.values() if child is not None)
    for field in ("figure", "media"):
        asset = slide.get(field)
        if isinstance(asset, Mapping):
            for key in ("caption", "cite", "alt", "label"):
                child = asset.get(key)
                if isinstance(child, str):
                    values.append(child)
    table = slide.get("table")
    if isinstance(table, Mapping):
        for key in ("caption", "footnote"):
            if isinstance(table.get(key), str):
                values.append(table[key])
        columns = table.get("columns")
        if isinstance(columns, list):
            for column in columns:
                if isinstance(column, str):
                    values.append(column)
                elif isinstance(column, Mapping):
                    for key in ("label", "unit"):
                        if isinstance(column.get(key), str):
                            values.append(column[key])
        rows = table.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, (list, tuple)):
                    values.extend(str(cell.get("v") if isinstance(cell, Mapping) and "v" in cell else cell) for cell in row if cell is not None)
    diagram = slide.get("native_diagram")
    if isinstance(diagram, Mapping):
        for node in diagram.get("nodes", []) or []:
            if isinstance(node, Mapping):
                values.extend(str(node.get(key)) for key in ("label",) if node.get(key) is not None)
    return [value for value in values if _text(value)]


def visible_text(slides: Sequence[Mapping[str, Any]]) -> str:
    """Return normalized visible deck text for quantitative coverage matching."""
    chunks: list[str] = []
    for slide in slides:
        chunks.extend(_visible_slide_strings(slide))
    normalized = " ".join(chunks).casefold()
    # A ratio suffix (1.15x / 1.15×) is one visible token; strip the marker
    # so the numeric coverage token still matches after normalization.
    return re.sub(r"(?<=\d)[x×]", "", normalized)


def missing_coverage_tokens(requirement: Mapping[str, Any], visible: str) -> list[str]:
    """Return requirement coverage tokens absent from the visible normalized text."""
    normalized = re.sub(r"(?<=\d)[x×]", "", " ".join(visible.casefold().split()))
    number_tokens = set(_NUMBER_RE.findall(normalized))
    word_tokens = set(_WORD_TOKEN_RE.findall(normalized))
    # Scientific-result requirements use deterministic CJK 2/3-grams. Apply
    # the same tokenizer to visible text so exact rendered claims pass, while
    # omitted clauses still fail closed.
    present = number_tokens | word_tokens | _semantic_tokens(normalized)
    missing: list[str] = []
    for token in sorted(requirement.get("coverage_tokens", []) or []):
        token = str(token).casefold()
        if not token:
            continue
        if token in present:
            continue
        missing.append(token)
    return missing
