"""Generic semantic evidence typing and role-compatibility selection.

The classifier is deliberately deterministic and source-bound.  It uses reviewed
semantic annotations when present, then applies generic section/prose cues.  It
never promotes a result, proposal, failure analysis, or limitation into a
context/problem role merely because a lexical similarity score is high.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


EVIDENCE_TYPES = (
    "context",
    "existing_paradigm",
    "motivation",
    "research_question",
    "research_gap",
    "problem_setup",
    "proposal",
    "method",
    "contribution",
    "experimental_setup",
    "result",
    "failure_analysis",
    "limitation",
    "discussion",
    "metadata",
)

ROLE_COMPATIBLE_TYPES: dict[str, tuple[str, ...]] = {
    "background": ("context", "existing_paradigm", "motivation"),
    "problem": ("research_question", "research_gap", "problem_setup", "motivation"),
    "method": ("proposal", "method"),
    "process": ("method", "proposal"),
    "results": ("result",),
    "metrics": ("result",),
    "analysis": ("failure_analysis", "limitation", "discussion", "result"),
    "discussion": ("failure_analysis", "limitation", "discussion", "result"),
    "contribution": ("contribution",),
    "conclusion": ("contribution", "proposal", "method", "result", "limitation", "failure_analysis", "discussion"),
    "sources": ("metadata",),
    "title": ("metadata",),
}

_TYPE_ALIASES = {
    "existing paradigm": "existing_paradigm",
    "existing-paradigm": "existing_paradigm",
    "research gap": "research_gap",
    "research-gap": "research_gap",
    "research question": "research_question",
    "research-question": "research_question",
    "objective": "research_question",
    "problem setup": "problem_setup",
    "problem-setup": "problem_setup",
    "failure analysis": "failure_analysis",
    "failure-analysis": "failure_analysis",
    "result": "result",
    "results": "result",
    "experiment": "result",
    "experiments": "result",
    "limitation": "limitation",
    "limitations": "limitation",
    "proposal": "proposal",
    "proposed approach": "proposal",
    "proposed-approach": "proposal",
    "solution": "proposal",
    "method": "method",
    "contribution": "contribution",
    "contributions": "contribution",
    "contribution summary": "contribution",
    "experimental setup": "experimental_setup",
    "discussion": "discussion",
    "context": "context",
    "background": "context",
    "motivation": "motivation",
    "metadata": "metadata",
}

_SEMANTIC_SLOT_DEFAULT_TYPES: dict[str, str] = {
    "context": "context",
    "objective_or_research_question": "research_question",
    "motivation_or_gap": "research_gap",
    "problem_setup": "problem_setup",
    "approach": "proposal",
    "contributions": "contribution",
    "experimental_setup": "experimental_setup",
    "main_results": "result",
    "limitations_or_failure_modes": "limitation",
}

_ROLE_COMPATIBLE_SLOTS: dict[str, frozenset[str]] = {
    "background": frozenset({"context", "motivation_or_gap"}),
    "problem": frozenset({"objective_or_research_question", "motivation_or_gap", "problem_setup"}),
    "method": frozenset({"approach"}),
    "process": frozenset({"approach"}),
    "results": frozenset({"main_results"}),
    "metrics": frozenset({"main_results"}),
    "analysis": frozenset({"main_results", "limitations_or_failure_modes"}),
    "discussion": frozenset({"main_results", "limitations_or_failure_modes"}),
    "contribution": frozenset({"contributions"}),
    "conclusion": frozenset({"approach", "contributions", "main_results", "limitations_or_failure_modes"}),
}

_ROLE_SLOT_PRIORITY: dict[str, dict[str, int]] = {
    "background": {"context": 40, "motivation_or_gap": 4},
    "problem": {
        "objective_or_research_question": 24,
        "motivation_or_gap": 24,
        "problem_setup": 24,
    },
    "method": {"approach": 40},
    "process": {"approach": 32},
    "results": {"main_results": 32},
    "metrics": {"main_results": 32},
    "contribution": {"contributions": 32},
}

_SOLUTION_TERMS = (
    "we propose",
    "we introduce",
    "our framework",
    "our method",
    "training-free",
    "zero-shot",
    "framework",
    "proposed",
    "proposal",
    "introduces",
    "提出",
    "框架",
    "方法",
)
_FAILURE_TERMS = (
    "failure",
    "failed",
    "fails",
    "error",
    "break",
    "incomplete",
    "execution failure",
    "failure analysis",
    "失败",
    "错误",
    "不完整",
)
_LIMITATION_TERMS = (
    "limitation",
    "limitations",
    "latency limitation",
    "logic error",
    "syntax error",
    "constraint",
    "局限",
    "约束",
    "延迟",
)
_RESULT_TERMS = (
    "result",
    "results",
    "experiment",
    "evaluation",
    "benchmark",
    "comparison",
    "success rate",
    "valid rate",
    "complete rate",
    "table",
    "figure",
    "ablation",
    "结果",
    "实验",
    "评估",
    "比较",
)
_GAP_TERMS = (
    "research gap",
    "gap",
    "cannot",
    "can't",
    "unable",
    "limited",
    "lack",
    "not expressible",
    "finite skill",
    "fixed skill",
    "data-hungry",
    "narrow",
    "remain",
    "缺口",
    "无法",
    "不能",
    "有限技能",
    "固定技能",
    "受限",
    "缺少",
)
_STRONG_GAP_TERMS = (
    "research gap",
    "cannot",
    "can't",
    "unable",
    "not expressible",
    "lack",
    "finite skill",
    "fixed skill",
    "fixed skills",
    "limited expression",
    "data-hungry",
    "narrow",
    "缺口",
    "无法",
    "不能",
    "缺少",
)
_MOTIVATION_TERMS = (
    "motivat",
    "need",
    "why",
    "challenge",
    "open-world",
    "everyday",
    "scalability",
    "deployment cost",
    "generalization",
    "distribution shift",
    "context",
    "动机",
    "需求",
    "挑战",
    "开放世界",
    "泛化",
)
_PARADIGM_TERMS = (
    "existing paradigm",
    "prior work",
    "end-to-end",
    "modular",
    "hierarchical",
    "vision-language-action",
    "paradigm",
    "annotated dataset",
    "prior method",
    "现有",
    "范式",
    "端到端",
    "模块化",
    "分层",
)
_PROBLEM_SETUP_TERMS = (
    "problem setup",
    "task formulation",
    "given",
    "objective",
    "goal",
    "task requires",
    "problem is",
    "任务定义",
    "问题设定",
    "目标",
)

_SECTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("introduction", ("introduction", "intro")),
    ("related_work", ("related work", "literature", "prior work")),
    ("background", ("background",)),
    ("motivation", ("motivation",)),
    ("problem_setup", ("problem setup", "task formulation", "task definition", "research question", "research-question")),
    ("method", ("method", "approach", "architecture", "system", "pipeline", "implementation")),
    ("failure_analysis", ("failure analysis", "failure case", "error analysis")),
    ("limitations", ("limitation", "limitations")),
    ("discussion", ("discussion",)),
    ("results", ("results", "experiments", "evaluation", "benchmark", "comparison", "ablation", "table", "figure")),
    ("abstract", ("abstract",)),
    ("metadata", ("paper metadata", "metadata", "bibliographic")),
)

_SECTION_PRIORS: dict[str, dict[str, int]] = {
    "background": {
        "introduction": 22,
        "related_work": 20,
        "background": 20,
        "motivation": 20,
        "abstract": 6,
    },
    "problem": {
        "problem_setup": 24,
        "introduction": 22,
        "background": 16,
        "motivation": 18,
        "abstract": 4,
    },
    "method": {"method": 24},
    "process": {"method": 20},
    "results": {"results": 24},
    "metrics": {"results": 24},
    "analysis": {"failure_analysis": 24, "limitations": 22, "discussion": 18, "results": 6},
    "discussion": {"failure_analysis": 20, "limitations": 20, "discussion": 18, "results": 6},
}

_EXPECTED_SECTIONS: dict[str, frozenset[str]] = {
    "background": frozenset({"introduction", "related_work", "background", "motivation", "abstract"}),
    "problem": frozenset({"problem_setup", "introduction", "background", "motivation", "abstract"}),
    "method": frozenset({"method"}),
    "process": frozenset({"method"}),
    "results": frozenset({"results"}),
    "metrics": frozenset({"results"}),
    "analysis": frozenset({"failure_analysis", "limitations", "discussion", "results"}),
    "discussion": frozenset({"failure_analysis", "limitations", "discussion", "results"}),
}


def _text(value: Any, fallback: str = "") -> str:
    cleaned = " ".join(str(value or "").split())
    return cleaned or fallback


def _normalize_type(value: Any) -> str:
    raw = re.sub(r"[_\s-]+", " ", _text(value).casefold()).strip()
    return _TYPE_ALIASES.get(raw, raw.replace(" ", "_"))


def reviewed_semantic_slot_records(reviewed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project confirmed reviewed semantic slots into source-bound records."""
    semantics = reviewed.get("reviewed_paper_semantics")
    if not isinstance(semantics, Mapping):
        return []
    slots = semantics.get("slots")
    if not isinstance(slots, Mapping):
        return []
    records: list[dict[str, Any]] = []
    provenance = reviewed.get("semantic_review_provenance")
    reviewed_hash = (
        _text(provenance.get("reviewed_semantics_sha256"))
        if isinstance(provenance, Mapping)
        else ""
    )
    for slot_name, raw in slots.items():
        if not isinstance(slot_name, str) or not isinstance(raw, Mapping):
            continue
        source_refs = [dict(ref) for ref in raw.get("source_refs", []) if isinstance(ref, Mapping)]
        first_ref = source_refs[0] if source_refs else {}
        locator = _text(raw.get("locator")) or _text(first_ref.get("locator"))
        page = raw.get("source_page")
        if not isinstance(page, int) or page < 1:
            page = first_ref.get("source_page")
        if not isinstance(page, int) or page < 1 or not locator:
            continue
        section = _text(raw.get("section")) or _text(first_ref.get("section")) or locator
        source_text = " ".join(
            _text(ref.get("source_text")) for ref in source_refs if _text(ref.get("source_text"))
        )
        evidence = _text(raw.get("evidence")) or source_text or locator
        summary = _text(raw.get("summary")) or _text(raw.get("text")) or source_text or locator
        semantic_type = _normalize_type(raw.get("semantic_evidence_type"))
        if semantic_type not in EVIDENCE_TYPES:
            semantic_type = _normalize_type(raw.get("evidence_type"))
        if semantic_type not in EVIDENCE_TYPES:
            semantic_type = _SEMANTIC_SLOT_DEFAULT_TYPES.get(slot_name, _normalize_type(slot_name))
        records.append({
            "semantic_slot": slot_name,
            "origin_semantic_slot": slot_name,
            "origin_reviewed_semantics_hash": reviewed_hash,
            "summary": summary,
            "text": summary,
            "evidence": evidence,
            "source_page": page,
            "section": section,
            "figure_table_equation": locator,
            "locator": locator,
            "semantic_evidence_type": semantic_type,
            "source_refs": source_refs,
        })
    return records


def _direct_source(record: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = record.get("direct_source_evidence")
    return direct if isinstance(direct, Mapping) else {}


def evidence_section(record: Mapping[str, Any]) -> str:
    direct = _direct_source(record)
    return _text(direct.get("section")) or _text(record.get("section")) or _text(direct.get("locator"))


def evidence_locator(record: Mapping[str, Any]) -> str:
    direct = _direct_source(record)
    return (
        _text(direct.get("locator"))
        or _text(record.get("figure_table_equation"))
        or _text(record.get("locator"))
        or _text(record.get("evidence"))
    )


def evidence_page(record: Mapping[str, Any]) -> int | None:
    direct = _direct_source(record)
    page = direct.get("page", direct.get("source_page", record.get("source_page")))
    return page if isinstance(page, int) and page > 0 else None


def section_family(section: Any) -> str:
    normalized = _text(section).casefold()
    for family, patterns in _SECTION_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return family
    return "unknown"


def _record_prose(record: Mapping[str, Any]) -> str:
    direct = _direct_source(record)
    values = [
        record.get("summary"),
        record.get("text"),
        record.get("evidence"),
        record.get("section"),
        direct.get("summary"),
        direct.get("text"),
        direct.get("evidence"),
        direct.get("role"),
        direct.get("section"),
    ]
    return " ".join(_text(value) for value in values if _text(value)).casefold()


def _contains_semantic_term(prose: str, term: str) -> bool:
    """Match ASCII semantic cues as tokens, not substrings of unrelated words."""
    if re.fullmatch(r"[a-z0-9 -]+", term):
        pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        return re.search(pattern, prose) is not None
    return term in prose


def _explicit_type(record: Mapping[str, Any]) -> str | None:
    for source in (record, _direct_source(record)):
        for key in ("semantic_evidence_type", "evidence_type", "semantic_type"):
            value = _normalize_type(source.get(key))
            if value in EVIDENCE_TYPES:
                return value
        role = _normalize_type(source.get("role"))
        if role in EVIDENCE_TYPES:
            return role
    return None


def classify_evidence(record: Mapping[str, Any] | None) -> str:
    """Return one canonical semantic evidence type for a reviewed record."""
    if not isinstance(record, Mapping):
        return "metadata"
    explicit = _explicit_type(record)
    if explicit:
        return explicit
    prose = _record_prose(record)
    family = section_family(evidence_section(record))
    if family == "metadata":
        return "metadata"
    if family == "failure_analysis" or any(term in prose for term in _FAILURE_TERMS):
        return "failure_analysis"
    if family == "limitations" or any(term in prose for term in _LIMITATION_TERMS):
        # In an Introduction, a limitation of prior work is a gap/motivation
        # rather than the paper's author-reported limitations section.
        if family in {"introduction", "related_work", "background"}:
            if any(term in prose for term in _GAP_TERMS):
                return "research_gap"
            return "motivation"
        return "limitation"
    if family == "results" or any(_contains_semantic_term(prose, term) for term in _RESULT_TERMS):
        # A method sentence containing "result" is still a proposal/method when
        # its section and solution language identify it as such.
        if family == "method" and any(term in prose for term in _SOLUTION_TERMS):
            return "proposal"
        return "result"
    if family == "problem_setup":
        return "problem_setup"
    if family == "method":
        return "proposal" if any(term in prose for term in _SOLUTION_TERMS) else "method"
    if family == "discussion":
        return "discussion"
    if family in {"introduction", "related_work", "background", "motivation"}:
        if any(term in prose for term in _STRONG_GAP_TERMS):
            return "research_gap"
        if any(term in prose for term in _MOTIVATION_TERMS):
            return "motivation"
        if any(term in prose for term in _PARADIGM_TERMS):
            return "existing_paradigm"
        if any(term in prose for term in _GAP_TERMS):
            return "research_gap"
        # Untyped Introduction prose is conservatively treated as motivation:
        # it is valid for both narrative roles while explicit annotations can
        # still distinguish context from a research gap.
        return "motivation"
    if family == "abstract":
        if any(term in prose for term in _SOLUTION_TERMS):
            return "proposal"
        if any(term in prose for term in _GAP_TERMS):
            return "motivation"
        return "context"
    if any(term in prose for term in _SOLUTION_TERMS):
        return "proposal"
    if any(term in prose for term in _GAP_TERMS):
        return "research_gap"
    if any(term in prose for term in _MOTIVATION_TERMS):
        return "motivation"
    return "context"


def is_section_mismatch(role: str, record: Mapping[str, Any] | None) -> bool:
    """Return whether a typed record is placed in a section foreign to its role."""
    role = _text(role).casefold()
    family = section_family(evidence_section(record or {}))
    expected = _EXPECTED_SECTIONS.get(role)
    return bool(expected and family != "unknown" and family not in expected)


def _candidate_view(record: Mapping[str, Any], assessment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "summary": _text(record.get("summary") or record.get("text"), "[MISSING: reviewed evidence]"),
        "semantic_evidence_type": assessment["semantic_evidence_type"],
        "evidence_section": assessment["evidence_section"],
        "locator": assessment["locator"],
        "source_page": evidence_page(record),
        "role_compatibility_score": assessment["role_compatibility_score"],
        "compatible": assessment["compatible"],
        "origin_semantic_slot": assessment.get("origin_semantic_slot", ""),
        "origin_reviewed_semantics_hash": assessment.get("origin_reviewed_semantics_hash", ""),
        "rejection_reason": assessment.get("rejection_reason", ""),
    }


def role_compatibility(role: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Score one record for a role and explain acceptance or rejection."""
    normalized_role = _text(role).casefold()
    semantic_type = classify_evidence(record)
    section = evidence_section(record)
    family = section_family(section)
    compatible_types = ROLE_COMPATIBLE_TYPES.get(normalized_role, ())
    compatible = semantic_type in compatible_types
    origin_slot = _text(record.get("origin_semantic_slot") or record.get("semantic_slot"))
    allowed_slots = _ROLE_COMPATIBLE_SLOTS.get(normalized_role)
    slot_compatible = not origin_slot or allowed_slots is None or origin_slot in allowed_slots
    compatible = compatible and slot_compatible
    prior = _SECTION_PRIORS.get(normalized_role, {}).get(family, 0)
    score = 0
    reasons: list[str] = []
    if compatible:
        score = 60 + prior
        score += _ROLE_SLOT_PRIORITY.get(normalized_role, {}).get(origin_slot, 0)
        if normalized_role == "background" and semantic_type in {"context", "existing_paradigm", "motivation"}:
            score += 10
        if normalized_role == "problem" and semantic_type in {"research_gap", "problem_setup"}:
            score += 12
        if normalized_role in {"method", "process"} and semantic_type == "method":
            score += 8
        if normalized_role in {"results", "metrics"} and semantic_type == "result":
            score += 8
    else:
        reasons.append(
            f"incompatible semantic evidence type '{semantic_type}' for role '{normalized_role}'"
        )
        if normalized_role == "background" and semantic_type == "proposal":
            reasons.append("solution/proposal evidence is not context, existing-paradigm, or motivation evidence")
        if normalized_role == "problem" and semantic_type in {"proposal", "failure_analysis", "result", "limitation"}:
            reasons.append("solution/result/failure/limitation evidence cannot substitute for a research gap or problem setup")
        if not slot_compatible:
            reasons.append(
                f"confirmed semantic slot '{origin_slot}' is not eligible for role '{normalized_role}'"
            )
    if is_section_mismatch(normalized_role, record):
        reasons.append(f"section family '{family}' is outside the role's section prior")
    if compatible and not reasons:
        reasons.append(f"compatible {semantic_type} evidence with {family or 'unknown'} section prior")
    return {
        "role": normalized_role,
        "semantic_evidence_type": semantic_type,
        "evidence_section": section,
        "locator": evidence_locator(record),
        "source_page": evidence_page(record),
        "section_family": family,
        "section_prior": prior,
        "origin_semantic_slot": origin_slot,
        "origin_reviewed_semantics_hash": _text(record.get("origin_reviewed_semantics_hash")),
        "role_compatibility_score": score,
        "compatible": compatible,
        "rejection_reason": "; ".join(reasons) if not compatible else "",
        "reasons": reasons,
    }


def select_role_evidence(
    role: str,
    records: Sequence[Mapping[str, Any]],
    *,
    exclude_items: Sequence[Mapping[str, Any]] = (),
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    """Select the best compatible reviewed record, or fail closed."""
    excluded = {
        (
            _text(item.get("source_page")),
            _text(item.get("section")),
            _text(item.get("figure_table_equation") or item.get("locator")),
            _text(item.get("summary") or item.get("text")),
        )
        for item in exclude_items
        if isinstance(item, Mapping)
    }
    scored: list[tuple[int, int, Mapping[str, Any], dict[str, Any]]] = []
    rejected: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        identity = (
            _text(record.get("source_page")),
            _text(record.get("section")),
            _text(record.get("figure_table_equation") or record.get("locator")),
            _text(record.get("summary") or record.get("text")),
        )
        if identity in excluded:
            continue
        assessment = role_compatibility(role, record)
        if assessment["compatible"]:
            scored.append((int(assessment["role_compatibility_score"]), -index, record, assessment))
        else:
            rejected.append(_candidate_view(record, assessment))
    if not scored:
        return None, {
            "role": _text(role).casefold(),
            "status": "missing",
            "semantic_evidence_type": "missing",
            "evidence_section": "",
            "locator": "",
            "source_page": None,
            "role_compatibility_score": 0,
            "rejected_candidates": rejected,
            "selected_candidate": None,
            "reason": f"no role-compatible reviewed evidence for '{_text(role).casefold()}'",
        }
    # Preserve reviewed input order for equal scores; only evidence score
    # determines preference, never incidental reverse-index ordering.
    scored.sort(key=lambda value: (-value[0], -value[1]))
    score, _, selected, assessment = scored[0]
    for _, _, candidate, candidate_assessment in scored[1:]:
        view = _candidate_view(candidate, candidate_assessment)
        view["rejection_reason"] = (
            f"compatible but lower role compatibility score than selected candidate ({score})"
        )
        rejected.append(view)
    return selected, {
        "role": _text(role).casefold(),
        "status": "selected",
        "semantic_evidence_type": assessment["semantic_evidence_type"],
        "evidence_section": assessment["evidence_section"],
        "locator": assessment["locator"],
        "source_page": assessment["source_page"],
        "role_compatibility_score": assessment["role_compatibility_score"],
        "selected_candidate": _candidate_view(selected, assessment),
        "rejected_candidates": rejected,
        "reason": assessment["reasons"],
    }
