"""Deterministic, evidence-bound matching of reviewed paper assets to slides."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MINIMUM_SCORE = 15
_UNREVIEWED_CONFIDENCE_FLOOR = 0.70

_TOPIC_MARKERS = {
    "method": frozenset({
        "method", "methods", "framework", "approach", "procedure", "process", "algorithm",
        "theory", "definition", "architecture", "modeling", "parameter", "parameterization",
        "trajectory", "waypoint", "affordance", "constraint", "execution",
    }),
    "results": frozenset({
        "result", "results", "experiment", "experiments", "experimental", "evaluation",
        "comparison", "performance", "metric", "metrics", "accuracy", "benchmark",
        "shift",
    }),
}


@dataclass(frozen=True, slots=True)
class SlideEvidenceContext:
    role: str
    locator: str = ""
    page: int | None = None
    section: str = ""
    caption: str = ""


@dataclass(frozen=True, slots=True)
class AssetCandidate:
    asset_id: str
    kind: str
    locator: str = ""
    page: int | None = None
    section: str = ""
    caption: str = ""
    roles: tuple[str, ...] = ()
    confidence: float = 1.0
    reviewed: bool = True


@dataclass(frozen=True, slots=True)
class AssetMatch:
    candidate: AssetCandidate | None
    score: int
    reasons: tuple[str, ...]
    conflicts: tuple[str, ...]


_NONE_POLICY = {
    "mode": "none",
    "preferred_kinds": (),
    "required_locators": False,
    "allow_no_asset": True,
}
_ROLE_POLICIES = {
    "title": {"mode": "optional", "preferred_kinds": ("figure", "diagram"), "required_locators": False, "allow_no_asset": True},
    "background": _NONE_POLICY,
    "question": _NONE_POLICY,
    "discussion": _NONE_POLICY,
    "conclusion": _NONE_POLICY,
    "sources": _NONE_POLICY,
    "method": {"mode": "required", "preferred_kinds": ("diagram", "figure"), "required_locators": True, "allow_no_asset": False},
    "process": {"mode": "required", "preferred_kinds": ("diagram", "figure"), "required_locators": True, "allow_no_asset": False},
    "metrics": {"mode": "optional", "preferred_kinds": ("table", "figure"), "required_locators": False, "allow_no_asset": True},
    "results": {"mode": "required", "preferred_kinds": ("figure", "table"), "required_locators": True, "allow_no_asset": False},
    "analysis": {"mode": "optional", "preferred_kinds": ("figure", "table"), "required_locators": False, "allow_no_asset": True},
    "problem": {"mode": "optional", "preferred_kinds": ("figure", "table"), "required_locators": False, "allow_no_asset": True},
    "contribution": {"mode": "optional", "preferred_kinds": ("figure", "diagram"), "required_locators": False, "allow_no_asset": True},
    "evidence": {"mode": "optional", "preferred_kinds": ("figure", "table", "diagram"), "required_locators": False, "allow_no_asset": True},
}
_ROLE_ALIASES = {
    "paper-title": "title",
    "method-overview": "method",
    "research-question": "question",
    "experiment": "process",
    "concept-or-metric": "metrics",
    "comparison": "contribution",
    "results-table": "results",
    "presenter-discussion": "discussion",
    "references": "sources",
}
_DEFAULT_POLICY = {"mode": "optional", "preferred_kinds": ("figure", "table", "diagram"), "required_locators": False, "allow_no_asset": True}


def _canonical_role(role: str) -> str:
    """Normalize public narrative aliases before role/topic comparisons."""
    key = role.casefold()
    return _ROLE_ALIASES.get(key, key)


def asset_policy_for_role(role: str) -> dict[str, object]:
    """Return a copy of the paper-agnostic asset policy for a slide role."""
    return dict(_ROLE_POLICIES.get(_canonical_role(role), _DEFAULT_POLICY))


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.casefold()))


def _same_text(left: str, right: str) -> bool:
    return bool(_tokens(left)) and _tokens(left) == _tokens(right)


def _overlap(left: str, right: str) -> bool:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    return bool(left_tokens and right_tokens and left_tokens & right_tokens)


def _topic_markers(*values: str) -> set[str]:
    tokens = set().union(*(_tokens(value) for value in values if value))
    topics = {topic for topic, markers in _TOPIC_MARKERS.items() if tokens & markers}
    # ``representation`` alone is common in method descriptions; require a
    # shift phrase before treating it as an experimental-result marker.
    if {"representation", "shift"} <= tokens:
        topics.add("results")
    return topics


def _semantic_topic_conflicts(context: SlideEvidenceContext, candidate: AssetCandidate) -> tuple[str, ...]:
    """Reject explicit-locator matches whose available topic metadata contradicts the role.

    Exact locators remain authoritative when section/caption metadata is absent or
    language-mismatched.  A conflict is emitted only when the context role and the
    candidate's explicit section/caption (or declared roles) provide incompatible
    high-level topic markers.
    """
    context_role = _canonical_role(context.role)
    context_topic = "method" if context_role in {"method", "process"} else "results" if context_role in {"metrics", "results", "analysis"} else ""
    if not context_topic:
        return ()
    # The negative check is specifically for a candidate that already passed the
    # explicit locator gate.  Keep the existing locator/missing-locator conflict
    # tuples stable for unrelated or unlocated candidates.
    if not context.locator or not candidate.locator or not _same_text(context.locator, candidate.locator):
        return ()
    candidate_topics = _topic_markers(candidate.section, candidate.caption)
    for role in candidate.roles:
        normalized = _canonical_role(role)
        if normalized in {"method", "process"}:
            candidate_topics.add("method")
        elif normalized in {"metrics", "results", "analysis"}:
            candidate_topics.add("results")
    if candidate_topics and context_topic not in candidate_topics:
        return ("semantic-topic-conflict",)
    return ()


def _evaluate(context: SlideEvidenceContext, candidate: AssetCandidate) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    conflicts: list[str] = []
    policy = asset_policy_for_role(context.role)
    if not candidate.reviewed and candidate.confidence < _UNREVIEWED_CONFIDENCE_FLOOR:
        conflicts.append("low-confidence-unreviewed")
    if context.locator and candidate.locator and not _same_text(context.locator, candidate.locator):
        conflicts.append("locator-conflict")
    if context.locator and not candidate.locator and policy["mode"] == "optional":
        # An explicit reviewed locator must not fall through to an unlocated
        # generic asset merely because kind/confidence happen to score.
        conflicts.append("missing-context-locator")
    if policy["required_locators"] and not _same_text(context.locator, candidate.locator):
        conflicts.append("missing-required-locator")
    candidate_roles = {_canonical_role(role) for role in candidate.roles}
    context_role = _canonical_role(context.role)
    if candidate_roles and context_role not in candidate_roles:
        conflicts.append("role-conflict")
    conflicts.extend(_semantic_topic_conflicts(context, candidate))
    if conflicts:
        return 0, (), tuple(conflicts)

    score = 0
    reasons: list[str] = []
    if context.locator and _same_text(context.locator, candidate.locator):
        score += 100
        reasons.append("explicit-locator")
    if context.page is not None and context.page == candidate.page:
        score += 20
        reasons.append("page")
    if _overlap(context.section, candidate.section):
        score += 15
        reasons.append("section")
    if _overlap(context.caption, candidate.caption):
        score += 15
        reasons.append("caption")
    if context_role in candidate_roles:
        score += 10
        reasons.append("role")
    if candidate.kind in policy["preferred_kinds"]:
        score += 5
        reasons.append("kind")
    confidence = min(1.0, max(0.0, candidate.confidence))
    if confidence:
        score += round(confidence * 10)
        reasons.append("confidence")
    return score, tuple(reasons), ()


def match_asset(context: SlideEvidenceContext, candidates: Sequence[AssetCandidate]) -> AssetMatch:
    """Select the strongest non-conflicting candidate, or explicitly select none."""
    policy = asset_policy_for_role(context.role)
    if policy["mode"] == "none" and candidates:
        return AssetMatch(None, 0, (), ("asset-not-allowed",))
    if not candidates:
        if policy["allow_no_asset"]:
            return AssetMatch(None, 0, ("no-asset-allowed",), ())
        return AssetMatch(None, 0, (), ("no-candidate",))

    evaluated = [(candidate, *_evaluate(context, candidate)) for candidate in candidates]
    valid = [item for item in evaluated if not item[3]]
    if not valid:
        conflicts = tuple(sorted({conflict for _, _, _, values in evaluated for conflict in values}))
        return AssetMatch(None, 0, (), conflicts)
    candidate, score, reasons, _ = min(valid, key=lambda item: (-item[1], item[0].asset_id))
    if score < _MINIMUM_SCORE:
        return AssetMatch(None, score, reasons, ("below-threshold",))
    return AssetMatch(candidate, score, reasons, ())
