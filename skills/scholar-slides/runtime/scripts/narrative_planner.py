"""Evidence-bound adaptive narrative planning for journal-club decks.

The planner deliberately returns a neutral specification rather than presentation
objects.  This makes narrative choices inspectable before the deck renderer turns
them into visible content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from asset_semantics import AssetCandidate, SlideEvidenceContext, asset_policy_for_role, match_asset
from audience_text import AUDIENCE_INTERNAL_PROCESS_RE, repair_pdf_hyphenation, sanitize_audience_text
from deck_types import get_deck_contract, resolve_deck_options
from semantic_evidence import classify_evidence, reviewed_semantic_slot_records, select_role_evidence


def _items(reviewed: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = reviewed.get(key, [])
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _text(value: Any, fallback: str) -> str:
    value = " ".join(str(value or "").split())
    return value or fallback


def _audience(value: Any, fallback: str) -> str:
    return sanitize_audience_text(value, fallback)


def _metadata_evidence(reviewed: Mapping[str, Any]) -> dict[str, Any]:
    """Build a source-bound metadata item without borrowing an experiment record."""
    metadata = reviewed.get("paper_metadata") if isinstance(reviewed.get("paper_metadata"), Mapping) else {}
    title = _text(metadata.get("title"), "Paper metadata")
    metadata_evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
    title_record = metadata_evidence.get("title") if isinstance(metadata_evidence.get("title"), Mapping) else {}
    locations = title_record.get("locations") if isinstance(title_record.get("locations"), list) else []
    location = next((value for value in locations if isinstance(value, str) and value.strip()), "p. 1")
    page_match = re.search(r"(?:p(?:age)?\.?\s*)(\d+)", location, re.IGNORECASE)
    page = int(page_match.group(1)) if page_match else 1
    return {
        "summary": title,
        "evidence": title,
        "source_page": page,
        "section": "Paper metadata",
        "figure_table_equation": location,
    }


def _item_identity(item: Mapping[str, Any] | None) -> tuple[Any, ...]:
    if not isinstance(item, Mapping):
        return ()
    return (
        item.get("source_page"),
        _text(item.get("section"), ""),
        _text(item.get("figure_table_equation"), ""),
        _text(item.get("summary"), ""),
    )


def _result_bound_item(item: Mapping[str, Any]) -> bool:
    locator = _text(item.get("figure_table_equation"), "").casefold()
    section = _text(item.get("section"), "").casefold()
    return bool(re.search(r"\b(?:table|figure|results?|evaluation|comparison|benchmark|ablation)\b", f"{locator} {section}"))


def _context_fallback(items: Sequence[Mapping[str, Any]], fallback: Mapping[str, Any] | None = None) -> Mapping[str, Any] | None:
    """Choose a context-compatible source before using positional fallback."""
    preferred_sections = ("introduction", "background", "motivation", "abstract", "problem")
    excluded = ("limitation", "failure", "error", "latency", "局限", "失败", "错误", "延迟")
    for preferred in preferred_sections:
        for item in items:
            prose = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
            if preferred in _text(item.get("section"), "").casefold() and not any(term in prose for term in excluded):
                return item
    for item in items:
        prose = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
        if not _result_bound_item(item) and not any(term in prose for term in excluded):
            return item
    return fallback


def _role_item(
    items: Sequence[Mapping[str, Any]],
    terms: Sequence[str],
    *,
    fallback: Mapping[str, Any] | None = None,
    preferred_sections: Sequence[str] = (),
    excluded_terms: Sequence[str] = (),
    exclude_items: Sequence[Mapping[str, Any]] = (),
) -> Mapping[str, Any] | None:
    """Select the highest-scoring reviewed item for a semantic role.

    Role selection is source-aware: preferred source sections can outrank list
    order, incompatible limitation/failure evidence is penalized, and a chosen
    method item can be excluded when selecting a later process stage.
    """
    if not items:
        return fallback
    normalized_terms = tuple(term.casefold() for term in terms)
    normalized_sections = tuple(term.casefold() for term in preferred_sections)
    normalized_excluded = tuple(term.casefold() for term in excluded_terms)
    excluded_ids = {_item_identity(item) for item in exclude_items}
    scored: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, item in enumerate(items):
        if _item_identity(item) in excluded_ids:
            continue
        section = _text(item.get("section"), "").casefold()
        prose = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
        score = sum(4 for term in normalized_terms if term in prose)
        if section and any(term in section for term in normalized_terms):
            score += 3
        if normalized_sections:
            score += sum(8 for term in normalized_sections if term in section)
        score -= sum(8 for term in normalized_excluded if term in prose)
        if re.search(r"\b(?:table|figure)\b", section) and not any(term in prose for term in normalized_terms):
            score -= 2
        scored.append((score, -index, item))
    if not scored:
        return fallback
    best = max(scored, key=lambda value: (value[0], value[1]))
    return best[2] if best[0] > 0 else fallback


def _unique_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    identities: set[tuple[Any, ...]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        identity = (item.get("source_page"), _text(item.get("section"), ""), _text(item.get("figure_table_equation"), ""), _text(item.get("summary"), ""))
        if identity in identities:
            continue
        identities.add(identity)
        result.append(item)
    return result


_PROVENANCE_ONLY_TERMS = (
    "only used to support", "not the sole evidence", "仅用于支撑", "唯一证据",
    "evidence locator", "provenance", "审计定位", "证据定位",
)


def _discussion_category(item: Mapping[str, Any]) -> str:
    """Classify evidence for a scientific discussion without paper-specific rules."""
    prose = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
    if any(term.casefold() in prose for term in _PROVENANCE_ONLY_TERMS):
        return "provenance_only"
    if any(term in prose for term in ("failure", "failed", "break", "error", "失败", "错误")):
        return "author_failure_analysis"
    if any(term in prose for term in ("limitation", "constraint", "局限", "约束")):
        return "author_limitations"
    if any(term in prose for term in ("baseline", "comparability", "可比", "对照")):
        return "baseline_comparability"
    if any(term in prose for term in ("protocol", "evaluation setup", "实验协议", "评价协议")):
        return "experimental_design"
    if any(term in prose for term in ("pretrained", "dependency", "dependence", "model", "依赖", "预训练")):
        return "model_dependency"
    if any(term in prose for term in ("latency", "cost", "delay", "延迟", "成本")):
        return "latency_cost"
    if any(term in prose for term in ("generalization", "external validity", "泛化", "外推", "外部有效性")):
        return "generalization"
    if any(term in prose for term in ("data", "dataset", "数据")):
        return "external_validity"
    return "presenter_inference"


def _sentence_fragment(value: Any, fallback: str) -> str:
    """Return a single sentence fragment with terminal punctuation normalized."""
    text = _audience(value, fallback)
    text = re.sub(r"(?:[.!?。！？；;…]|\.{2,})+$", "", text).strip()
    return text or fallback


def _discussion_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    terms = ("limitation", "failure", "error", "latency", "protocol", "baseline", "comparability", "validity", "external", "cost", "dependency", "data", "cache", "reliability", "scalability", "约束", "失败", "错误", "延迟", "局限")
    category_weight = {
        "author_failure_analysis": 12,
        "author_limitations": 11,
        "experimental_design": 10,
        "baseline_comparability": 10,
        "model_dependency": 9,
        "latency_cost": 9,
        "generalization": 8,
        "external_validity": 7,
        "presenter_inference": 1,
    }
    scored: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, item in enumerate(items):
        prose = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
        category = _discussion_category(item)
        if category == "provenance_only":
            continue
        score = sum(2 for term in terms if term.casefold() in prose)
        score += category_weight.get(category, 0)
        if score:
            scored.append((score, -index, item))
    return [entry[2] for entry in sorted(scored, key=lambda value: (value[0], value[1]), reverse=True)[:3]]


def _discussion_questions(items: Sequence[Mapping[str, Any]]) -> tuple[list[str], str]:
    if not items:
        return [
            "当前材料没有提供可用于具体批评的局限、失败或实验协议证据，需要回到论文原文补充。",
            "哪些部分是作者的直接结论，哪些只是汇报者基于现有材料提出的推断？",
        ], "generic_missing_critique"

    questions: list[str] = []
    for item in items:
        summary = _sentence_fragment(item.get("summary"), "论文报告了一项需要限定范围的观察")
        kind = _discussion_category(item)
        if kind == "author_failure_analysis":
            question = f"论文作者的失败分析是：{summary}。这条 failure chain 的最脆弱环节是什么？还需要什么实验才能判断它是否限制长时任务的外推？"
        elif kind == "baseline_comparability":
            question = f"关于 {summary}，基线是否与方法使用同一任务协议和评价口径？哪些协议差异可能改变比较结论？"
        elif kind == "experimental_design":
            question = f"关于实验设计，论文报告：{summary}。评价协议中的哪些控制变量仍需要补充或隔离？"
        elif kind == "latency_cost":
            question = f"论文作者报告：{summary}。这一延迟或可靠性瓶颈会怎样影响可扩展部署？应如何设计实验区分生成时间与执行时间的影响？"
        elif kind == "model_dependency":
            question = f"证据中出现了模型依赖假设：{summary}。如果替换模型、感知组件或数据条件，结论是否仍然成立？"
        elif kind == "generalization":
            question = f"论文证据涉及泛化边界：{summary}。哪些未覆盖的对象、环境或任务会构成最关键的外推风险？"
        elif kind == "external_validity":
            question = f"论文证据涉及外部有效性：{summary}。哪些未覆盖的数据、对象或环境会构成最关键的外推风险？"
        else:
            question = f"论文作者报告的限制是：{summary}。它主要限制可靠性、可扩展性还是外部有效性？需要什么对照实验来定位瓶颈？"
        questions.append(question)
    boundary = "哪些内容是论文作者直接报告的限制，哪些是汇报者基于这些证据提出的可检验推断？"
    # Keep the author/presenter boundary visible within the three-question cap.
    # Otherwise a deck with three evidence items would silently drop the most
    # important distinction from the discussion slide.
    return [*questions[:2], boundary][:3], "scientific_critique"


def _evidence_with_terms(items: Sequence[Mapping[str, Any]], terms: Sequence[str]) -> Mapping[str, Any] | None:
    """Find a reviewed item whose source wording licenses a thesis path."""
    for item in items:
        haystack = " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold()
        if any(term in haystack for term in terms):
            return item
    return None


def _visible_assets(assets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Remove audit/deferred/forbidden material before it can be recommended."""
    visible: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        if asset.get("audit_only") or asset.get("deferred") or asset.get("forbidden") or asset.get("visible") is False:
            continue
        asset_id = asset.get("id")
        if isinstance(asset_id, str) and asset_id:
            visible.append(dict(asset))
    return visible


_ASSET_REF_RE = re.compile(
    r"\b(figure|figures|table|tables)\s*[- ]?(\d+)"
    r"(?:\s*(?:[-–—]|to)\s*(\d+))?",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _locator_asset_ids(value: Any) -> list[str]:
    """Return ordered asset ids mentioned by a human-readable locator.

    A reviewed item may cite a range such as ``Figures 2–3``.  Keeping the
    expansion here lets repeated plan slots bind one unused figure at a time,
    without embedding any paper-specific figure numbers in the planner.
    """
    locator = _text(value, "")
    candidates: list[str] = []
    for match in _ASSET_REF_RE.finditer(locator):
        prefix = match.group(1).lower().rstrip("s")
        start = int(match.group(2))
        end = int(match.group(3) or start)
        if end < start or end - start > 24:
            end = start
        candidates.extend(f"{prefix}-{number}" for number in range(start, end + 1))
    return candidates


def _expanded_evidence_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Expand reviewed figure/table ranges into one source-bound item per member.

    A range such as ``Figures 2--3`` is useful evidence for two distinct result
    views, but a single range-bound slide can only render one selected asset.  The
    expansion is deliberately driven by the reviewed locator text; it does not
    contain paper-specific figure numbers and leaves non-range evidence unchanged.
    """
    expanded: list[dict[str, Any]] = []
    for item in items:
        base = dict(item)
        locator = _text(base.get("figure_table_equation"), "")
        asset_ids = _locator_asset_ids(locator)
        if len(asset_ids) <= 1:
            expanded.append(base)
            continue
        for asset_id in asset_ids:
            prefix, _, number = asset_id.partition("-")
            clone = dict(base)
            clone["figure_table_equation"] = f"{prefix.title()} {number}"
            clone["_binding_item"] = base
            expanded.append(clone)
    return expanded


def _dense_result_items(items: Sequence[Mapping[str, Any]], slots: int) -> list[dict[str, Any]]:
    """Keep distinct reviewed result locators when a 12-slide deck is evidence-rich."""
    expanded = _expanded_evidence_items(items)
    if len(expanded) <= slots:
        return expanded
    # Retain a native table result when trimming, while preserving source order
    # for all other evidence.  The rule is generic and applies to any paper.
    table_items = [item for item in expanded if re.search(r"\btable\b", _text(item.get("figure_table_equation"), ""), re.IGNORECASE)]
    other_items = [item for item in expanded if item not in table_items]
    selected = other_items[: max(0, slots - len(table_items))] + table_items[:slots]
    return selected[:slots]


def _asset_candidate(asset: Mapping[str, Any]) -> AssetCandidate:
    """Adapt digest asset records to the matcher without paper-specific rules."""
    asset_id = str(asset["id"])
    role_values = asset.get("roles", ())
    roles = tuple(value for value in role_values if isinstance(value, str)) if isinstance(role_values, (list, tuple)) else ()
    confidence = asset.get("confidence", 1.0)
    page = asset.get("page", asset.get("source_page"))
    return AssetCandidate(
        asset_id=asset_id,
        kind=_text(asset.get("kind"), asset_id.split("-", 1)[0]),
        locator=_text(asset.get("locator"), _text(asset.get("label"), asset_id.replace("-", " "))),
        page=page if isinstance(page, int) else None,
        section=_text(asset.get("section"), ""),
        caption=_text(asset.get("caption"), ""),
        roles=roles,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else 1.0,
        reviewed=asset.get("reviewed") is not False,
    )


def _confidence_score(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return {
        "high": 1.0,
        "medium": 0.75,
        "low": 0.40,
    }.get(_text(value, "").casefold(), 0.70)


def _method_stage_asset_context(
    process_item: Mapping[str, Any] | None,
    method_item: Mapping[str, Any] | None,
    assets: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Choose a source-grounded later-stage visual for a process slide.

    The ranking uses reviewed confidence and asset caption/role semantics.  It
    intentionally does not name any paper, figure number, or domain-specific
    component.  The selected asset is only an asset context; the process slide
    keeps its reviewed evidence item and speaker binding unchanged.
    """
    method_locator = _text(method_item.get("figure_table_equation") if method_item else None, "").casefold()
    process_locator = _text(process_item.get("figure_table_equation") if process_item else None, "").casefold()
    stage_terms = ("stage", "step", "mechanism", "parameter", "trajectory", "waypoint", "execution", "process", "object", "affordance", "constraint", "example", "result", "阶段", "机制")
    overview_terms = ("overview", "pipeline", "system", "architecture", "overview", "总览", "主线")
    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        kind = _text(asset.get("kind"), "").casefold()
        if kind not in {"figure", "diagram"}:
            continue
        locator = _text(asset.get("label"), _text(asset.get("id"), "")).casefold()
        if method_locator and locator == method_locator:
            continue
        caption = " ".join(_text(asset.get(key), "") for key in ("caption", "section", "roles", "purpose")).casefold()
        stage_hits = sum(1 for term in stage_terms if term in caption)
        if stage_hits == 0 and locator != process_locator:
            continue
        score = _confidence_score(asset.get("confidence")) * 10
        score += stage_hits * 4
        score -= sum(6 for term in overview_terms if term in caption)
        if process_locator and locator == process_locator:
            # The evidence locator already grounds the process claim.  Prefer
            # a distinct later-stage visual when the reviewed asset graph has
            # one, so the slide does not spend its only visual slot repeating
            # the evidence figure at unreadable scale.
            score -= 25
        page = asset.get("page", asset.get("source_page"))
        if isinstance(page, int) and isinstance(method_item.get("source_page") if method_item else None, int) and page > method_item["source_page"]:
            score += 1
        if score > 0:
            ranked.append((score, _text(asset.get("id"), ""), asset))
    if not ranked:
        return process_item
    selected = max(ranked, key=lambda value: (value[0], value[1]))[2]
    context = dict(selected)
    context["figure_table_equation"] = _text(selected.get("label"), _text(selected.get("id"), ""))
    context["source_page"] = selected.get("page", selected.get("source_page"))
    context["section"] = _text(selected.get("section"), "Method stage")
    context["summary"] = _text(selected.get("caption"), context["figure_table_equation"])
    context["evidence"] = context["summary"]
    return context


def _title_asset_context(assets: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Accept only explicit title assets or legacy records lacking rich semantics.

    Rich reviewed assets stay out of the cover unless they declare a title/hero
    role.  Minimal legacy fixtures may still exercise the optional title visual;
    they carry no page/confidence metadata with which to make a stronger claim.
    """
    for asset in assets:
        if not isinstance(asset, Mapping):
            continue
        role_values = asset.get("roles", ())
        roles = {_text(value, "").casefold() for value in role_values} if isinstance(role_values, (list, tuple, set)) else {_text(role_values, "").casefold()}
        explicit = bool(asset.get("title_compatible") or asset.get("hero_compatible") or roles & {"title", "title-page", "overview", "framework", "teaser"})
        legacy_minimal = "page" not in asset and "source_page" not in asset and "confidence" not in asset
        if "appendix" in _text(asset.get("section"), "").casefold() and not explicit:
            continue
        if explicit or legacy_minimal:
            return asset
    return None


def _evidence_context(role: str, item: Mapping[str, Any] | None) -> SlideEvidenceContext:
    return SlideEvidenceContext(
        role=role,
        locator=_text(item.get("figure_table_equation") if item else None, ""),
        page=item.get("source_page") if isinstance(item, Mapping) and isinstance(item.get("source_page"), int) else None,
        section=_text(item.get("section") if item else None, ""),
        caption=_text(item.get("evidence") if item else None, ""),
    )


def _match_context(context: SlideEvidenceContext, candidates: Sequence[AssetCandidate]):
    """Run the shared matcher, including generic expansion of explicit ranges."""
    match = match_asset(context, candidates)
    # A reviewed range (for example, Figures 2--3) licenses its explicitly
    # named members, one per later slide.  Each member still goes through the
    # shared matcher with its concrete locator; no figure number is hard-coded.
    if match.candidate is None:
        candidates_by_id = {candidate.asset_id.casefold(): candidate for candidate in candidates}
        for asset_id in _locator_asset_ids(context.locator):
            candidate = candidates_by_id.get(asset_id)
            if candidate is None:
                continue
            expanded_context = SlideEvidenceContext(
                role=context.role,
                locator=candidate.locator,
                page=context.page,
                section=context.section,
                caption=context.caption,
            )
            expanded_match = match_asset(expanded_context, [candidate])
            if expanded_match.candidate is not None:
                match = expanded_match
                break
    return match


def _reserved_required_asset_ids(
    contexts: Sequence[tuple[str, Mapping[str, Any] | None]], assets: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Reserve matcher-approved assets for required roles before optional roles choose."""
    candidates = [_asset_candidate(asset) for asset in assets]
    return {
        match.candidate.asset_id
        for role, item in contexts
        if (match := _match_context(_evidence_context(role, item), candidates)).candidate is not None
    }


def _asset_choice(
    role: str,
    item: Mapping[str, Any] | None,
    assets: Sequence[Mapping[str, Any]],
    used: set[str],
    reserved: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return one matcher-approved asset and its non-visible decision record."""
    context = _evidence_context(role, item)
    locator = context.locator
    policy = asset_policy_for_role(role)
    has_asset_identity = bool(
        _text(item.get("id"), "") or _text(item.get("label"), "")
    ) if isinstance(item, Mapping) else False
    if item is None or (
        (not _text(item.get("summary") or item.get("text"), "") or not locator)
        and not has_asset_identity
    ):
        return [], {
            "candidate_id": None,
            "score": 0,
            "reasons": ["no source-bound evidence locator is available"],
            "conflicts": [],
            "evidence_locator": locator,
        }
    available = [
        asset for asset in assets
        if str(asset["id"]) not in used
        and (role == "title" or policy["mode"] == "required" or str(asset["id"]) not in reserved)
    ]
    candidates = [_asset_candidate(asset) for asset in available]
    match = _match_context(context, candidates)
    selection = {
        "candidate_id": match.candidate.asset_id if match.candidate else None,
        "score": match.score,
        "reasons": list(match.reasons),
        "conflicts": list(match.conflicts),
        "evidence_locator": locator,
    }
    if match.candidate is None:
        return [], selection
    selected = next(asset for asset in available if str(asset["id"]) == match.candidate.asset_id)
    used.add(match.candidate.asset_id)
    return [dict(selected)], selection


def _final_locator(item: Mapping[str, Any] | None) -> str:
    if not isinstance(item, Mapping):
        return ""
    return _text(
        item.get("figure_table_equation"),
        _text(item.get("locator"), _text(item.get("label"), _text(item.get("id"), ""))),
    )


def _finalize_asset_selection(
    selection: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]],
    bound_items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild source identity from the final visual-or-fallback state.

    Candidate ranking may use a visual context distinct from the factual claim.
    Once ranking is complete, a selected visual owns the selection locator.  If
    no visual survives, the source-bound claim owns the native fallback locator;
    a rejected candidate must not leak through the earlier selection object.
    """
    finalized = dict(selection)
    if selected:
        asset = selected[0]
        candidate_id = _text(asset.get("id"), "")
        locator = _final_locator(asset)
        changed = (
            _text(finalized.get("candidate_id"), "") != candidate_id
            or _text(finalized.get("evidence_locator"), "").casefold() != locator.casefold()
        )
        finalized["candidate_id"] = candidate_id or None
        finalized["evidence_locator"] = locator
        if changed:
            finalized.update({
                "score": 0,
                "reasons": ["selection rebuilt from the final matcher-approved asset"],
                "conflicts": [],
            })
        return finalized

    locator = next((_final_locator(item) for item in bound_items if _final_locator(item)), "")
    finalized.update({
        "candidate_id": None,
        "score": 0,
        "reasons": ["no matcher-approved asset; using the final source-bound state"],
        "conflicts": [],
        "evidence_locator": locator,
    })
    return finalized


_DIRECTIONAL_RELATIONS = frozenset({
    "process", "pipeline", "workflow", "procedure", "stage_sequence", "state_transition", "causal",
})
_COMPARISON_RELATIONS = frozenset({
    "comparison", "ablation", "trade_off", "correlation", "result_pair", "negative_result",
})


def _representation_relation(item: Mapping[str, Any] | None) -> str:
    """Return the source-supported relation encoded by a native representation."""
    if not isinstance(item, Mapping):
        return "assertion"
    explicit = re.sub(
        r"[^a-z0-9]+", "_", _text(item.get("representation_relation"), "").casefold()
    ).strip("_")
    aliases = {
        "stage": "stage_sequence", "sequence": "stage_sequence",
        "causal_chain": "causal", "cause_effect": "causal",
        "independent": "independent_observations", "observations": "independent_observations",
        "tradeoff": "trade_off", "result_comparison": "comparison",
    }
    explicit = aliases.get(explicit, explicit)
    if explicit in _DIRECTIONAL_RELATIONS | _COMPARISON_RELATIONS | {"independent_observations", "assertion"}:
        return explicit

    summary = repair_pdf_hyphenation(_text(item.get("summary"), ""))
    prose = " ".join(
        _text(item.get(key), "") for key in ("summary", "evidence", "section")
    ).casefold()
    semantic_type = classify_evidence(item)
    comparison_terms = (
        " versus ", " vs. ", " vs ", "compared", "comparison", "ablation", "trade-off", "tradeoff",
        "outperform", "underperform", "higher than", "lower than", "remains below", "does not guarantee",
        "比较", "相比", "对比", "消融", "权衡", "优于", "高于", "低于", "不保证",
    )
    if semantic_type in {"result", "failure_analysis", "limitation"}:
        if any(term in f" {prose} " for term in comparison_terms):
            return "comparison"
        return "independent_observations"

    arrow_count = len(re.findall(r"(?:→|↦|=>|->)", summary))
    sequence_terms = (
        "pipeline", "workflow", "procedure", "stage", "sequence", "first", "then", "next", "finally",
        "流程", "链路", "阶段", "首先", "然后", "随后", "最后", "再用", "再将",
    )
    if semantic_type in {"method", "proposal"} and (
        arrow_count >= 1
        or (any(term in prose for term in sequence_terms) and bool(re.search(r"[;；]", summary)))
    ):
        return "process"
    return "assertion"


def _native_representation(item: Mapping[str, Any] | None, locator: str) -> dict[str, Any]:
    """Build an editable native view without inventing order or causality."""
    summary = repair_pdf_hyphenation(
        _audience(item.get("summary") if item else None, "[MISSING: reviewed method]")
    )
    relation = _representation_relation(item)
    arrow_parts = [
        part.strip(" \t\n,，;；:.。")
        for part in re.split(r"\s*(?:→|↦|=>|->)\s*", summary)
        if part.strip(" \t\n,，;；:.。")
    ]
    if relation in _DIRECTIONAL_RELATIONS and len(arrow_parts) >= 2:
        first = arrow_parts[0]
        prefix = re.match(
            r"^.{0,48}?(?:method|pipeline|process|workflow|framework|方法|流程|链路|阶段)\s*[:：]\s*(.+)$",
            first,
            re.IGNORECASE,
        )
        if prefix:
            arrow_parts[0] = prefix.group(1).strip()
        labels = arrow_parts[:6]
    elif relation in _DIRECTIONAL_RELATIONS:
        clause_parts = [
            part.strip(" \t\n,，;；:.。")
            for part in re.split(r"\s*(?:[;；]|\bthen\b|\bnext\b|\bfinally\b|首先|然后|随后|最后|再用|再将)\s*", summary, flags=re.IGNORECASE)
            if part.strip(" \t\n,，;；:.。")
        ]
        labels = clause_parts[:6] if len(clause_parts) >= 2 else [summary]
    else:
        labels = [
            part.strip(" \t\n,，;；:.。")
            for part in re.split(r"\s*[;；]\s*", summary)
            if part.strip(" \t\n,，;；:.。")
        ][:6] or [summary]
    labels = [label if len(label) <= 140 else f"{label[:137].rstrip()}…" for label in labels]
    nodes = [
        {"id": f"stage-{index}", "label": label, "source_locator": locator}
        for index, label in enumerate(labels, start=1)
    ]
    return {
        "type": (
            "evidence-flow" if relation in _DIRECTIONAL_RELATIONS
            else "comparison" if relation in _COMPARISON_RELATIONS
            else "assertion-list"
        ),
        "editable": True,
        "relation_type": relation,
        "semantic_evidence_type": classify_evidence(item),
        "nodes": nodes,
        "edges": ([
            {"from": nodes[index]["id"], "to": nodes[index + 1]["id"]}
            for index in range(len(nodes) - 1)
        ] if relation in _DIRECTIONAL_RELATIONS else []),
    }


def _native_diagram(item: Mapping[str, Any] | None, locator: str) -> dict[str, Any]:
    """Backward-compatible entry point for the relation-aware native view."""
    return _native_representation(item, locator)


def _quantitative_group_title(locator: str, requirements: Sequence[Mapping[str, Any]]) -> str:
    """Return a neutral, source-grounded audience title without audit language."""
    labels = [
        _audience(requirement.get("label"), "")
        for requirement in requirements
        if _audience(requirement.get("label"), "")
    ]
    label = next((value for value in labels if not AUDIENCE_INTERNAL_PROCESS_RE.search(value)), "")
    if locator and label:
        locator_prefix = re.compile(rf"^\s*{re.escape(locator)}\s*[:：\-–—]?\s*", re.IGNORECASE)
        label = locator_prefix.sub("", label).strip()
    if not label:
        cjk = any(re.search(r"[\u3400-\u9fff]", value) for value in labels)
        label = "定量结果" if cjk else "Quantitative results"
    return f"{locator}：{label}" if locator else label


def quantitative_source_key(requirement: Mapping[str, Any]) -> tuple[str, int]:
    """Return the stable source identity used to pack quantitative facts."""
    source = requirement.get("source") if isinstance(requirement.get("source"), Mapping) else {}
    locator = _text(source.get("locator"), "").casefold()
    page = source.get("page") if isinstance(source.get("page"), int) else 0
    if not locator:
        audit_ref = requirement.get("audit_ref") if isinstance(requirement.get("audit_ref"), Mapping) else {}
        locator = _text(
            audit_ref.get("path"),
            _text(requirement.get("id"), "quantitative requirement"),
        ).casefold()
    return locator, page


def _quantitative_requirement_groups(
    requirements: Sequence[Mapping[str, Any]],
) -> list[tuple[tuple[str, int], list[Mapping[str, Any]]]]:
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    order: list[tuple[str, int]] = []
    for requirement in requirements:
        key = quantitative_source_key(requirement)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(requirement)
    return [(key, grouped[key]) for key in order]


def plan_narrative(
    reviewed: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
    deck_type: str,
    slide_budget: tuple[int, int],
    quantitative_requirements: Sequence[Mapping[str, object]] = (),
    *,
    audience: str | None = None,
    density: str | None = None,
    quantitative_group_slide_counts: Mapping[tuple[str, int], int] | None = None,
) -> dict[str, Any]:
    """Plan an evidence-bound story shaped by a supported academic deck contract."""
    effective = resolve_deck_options({
        "deck_type": deck_type,
        "audience": audience,
        "density": density,
        "slide_count": slide_budget[1],
    })
    contract = get_deck_contract(effective["deck_type"])
    low, high = slide_budget
    supported = contract.time_to_slide_budget["slide_count"]
    if low < supported["min"] or high > supported["max"] or low > high:
        raise ValueError(
            f"slide_budget must be within the inclusive {supported['min']}--{supported['max']} range for {contract.deck_type.value}"
        )
    claims = _items(reviewed, "reviewed_claims")
    contributions = _items(reviewed, "reviewed_contributions")
    results = _items(reviewed, "reviewed_experimental_results")
    semantic_slots = reviewed_semantic_slot_records(reviewed)
    all_evidence = [*semantic_slots, *claims, *contributions, *results]
    first = all_evidence[0] if all_evidence else {}
    metadata_item = _metadata_evidence(reviewed)
    visible = _visible_assets(assets)
    title_asset_item = _title_asset_context(visible)
    used: set[str] = set()
    # Background and Problem are selected through the shared semantic contract.
    # The complete reviewed pool is retained so incompatible result/solution/
    # failure candidates can be surfaced as rejected audit records.
    role_pool = [*semantic_slots, *claims, *contributions, *results]
    background, background_selection = select_role_evidence("background", role_pool)
    problem, problem_selection = select_role_evidence(
        "problem",
        role_pool,
        exclude_items=(background,) if background is not None else (),
    )
    role_selections = {"background": background_selection, "problem": problem_selection}
    role_overlap_reason = ""
    if background_selection["status"] == "missing":
        role_overlap_reason = "没有找到与背景角色兼容的 context/existing_paradigm/motivation 证据；需人工确认。"
    elif problem_selection["status"] == "missing":
        role_overlap_reason = "没有找到与问题角色兼容的 research_gap/problem_setup/motivation 证据；需人工确认。"
    question = _role_item(
        [item for item in (next((item for item in semantic_slots if item.get("semantic_slot") == "objective_or_research_question"), None), *claims) if item is not None],
        ("research question", "question", "objective", "aim", "goal"),
        fallback=problem,
    )
    method_pool = [*semantic_slots, *contributions, *claims]
    if semantic_slots:
        method, method_selection = select_role_evidence("method", method_pool)
    else:
        # Preserve the legacy reviewed-claim path when no confirmed semantic
        # slots exist.  Once a confirmed slot view exists, role selection is
        # governed by that stronger contract and fails closed if incompatible.
        method = _role_item(
            method_pool,
            ("framework", "pipeline", "system", "overview", "approach", "method", "主线"),
            fallback=contributions[0] if contributions else (claims[0] if claims else None),
        )
        method_selection = {}
    role_selections["method"] = method_selection
    if title_asset_item is not None and method is not None:
        title_locator = _text(title_asset_item.get("label"), _text(title_asset_item.get("figure_table_equation"), "")).casefold()
        method_locator = _text(method.get("figure_table_equation"), "").casefold()
        if title_locator and method_locator and title_locator == method_locator:
            title_asset_item = None
    # Process is a method-stage role.  Keep non-method semantic slots out of
    # this lexical fallback: a limitation/result can mention trajectories or
    # execution while remaining semantically incompatible with process.
    process_pool = [*semantic_slots, *contributions, *claims]
    compatible_process_pool = [
        item for item in process_pool
        if select_role_evidence("process", [item])[0] is not None
    ]
    process = _role_item(
        compatible_process_pool,
        ("stage", "step", "process", "mechanism", "pipeline", "workflow", "input", "output", "阶段", "步骤", "机制", "流程"),
        fallback=None,
        preferred_sections=("method", "approach", "pipeline", "workflow"),
        exclude_items=(method,) if method else (),
    )
    _, process_selection = select_role_evidence("process", [process] if process else [])
    if process is None and not semantic_slots and method is not None and classify_evidence(method) in {"method", "proposal"}:
        # Legacy reviewed structures can reuse their sole method record as a
        # source-bound assertion.  The relation-aware materializer will not
        # add arrows unless the record itself supports sequence or causality.
        process = method
        _, process_selection = select_role_evidence("process", [method])
    role_selections["process"] = process_selection
    process_asset_item = _method_stage_asset_context(process, method, visible)
    metric = results[0] if results else (method or (claims[0] if claims else None))
    critique_items = _discussion_items(all_evidence)
    discussion_questions, discussion_grounding = _discussion_questions(critique_items)
    discussion_categories = [_discussion_category(item) for item in critique_items]
    discussion_evidence = critique_items or ([claims[0]] if claims else ([contributions[0]] if contributions else results[:1]))
    semantic_slots_by_name = {
        _text(item.get("semantic_slot"), ""): item
        for item in semantic_slots if _text(item.get("semantic_slot"), "")
    }
    conclusion_components: dict[str, dict[str, Any]] = {}
    conclusion_items: list[Mapping[str, Any]] = []
    conclusion_points: list[str] = []
    for component_name, slot_name, audience_label in (
        ("contribution", "contributions", "贡献"),
        ("main_result", "main_results", "主要结果"),
        ("limitation", "limitations_or_failure_modes", "限制"),
    ):
        item = semantic_slots_by_name.get(slot_name)
        if item is None:
            continue
        text = _sentence_fragment(item.get("summary"), "")
        if not text:
            continue
        conclusion_items.append(item)
        conclusion_points.append(f"{audience_label}：{text}")
        conclusion_components[component_name] = {
            "text": text,
            "origin_semantic_slot": slot_name,
            "origin_reviewed_semantics_hash": _text(item.get("origin_reviewed_semantics_hash"), ""),
            "locator": _text(item.get("figure_table_equation"), _text(item.get("locator"), "")),
            "source_page": item.get("source_page"),
            "section": _text(item.get("section"), ""),
            "ownership": "author_reported",
        }
    if not conclusion_items:
        conclusion_items = _unique_items([
            *contributions[:2],
            *[item for item in claims if any(term in " ".join(_text(item.get(key), "") for key in ("summary", "evidence", "section")).casefold() for term in ("conclusion", "finding", "limitation", "result"))],
            *results[:1],
        ])
        if not conclusion_items:
            conclusion_items = [item for item in (contributions[:1] or results[:1] or claims[:1]) if item]
        conclusion_items = [
            item for item in conclusion_items
            if not (
                len(re.findall(r"\d+(?:\.\d+)?", _text(item.get("summary"), ""))) >= 6
                and any(marker in _text(item.get("summary"), "") for marker in ("=", "/", ";", "|"))
            )
        ]
        if not conclusion_items:
            conclusion_items = [item for item in contributions[:1] if item]
        conclusion_points = [_sentence_fragment(item.get("summary"), "已确认的论文贡献") for item in conclusion_items[:3]]
    requirements = [dict(item) for item in quantitative_requirements if isinstance(item, Mapping)]
    known_kinds = {"key_metric", "quantitative_result", "pairwise_audit_comparison", "scientific_result"}
    for item in requirements:
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("quantitative requirement id must be a non-empty string")
        if item.get("kind") not in known_kinds:
            raise ValueError(f"unknown quantitative requirement kind: {item.get('kind')}")
    key_metric_requirements = [item for item in requirements if item.get("kind") == "key_metric"]
    quantitative_result_requirements = [item for item in requirements if item.get("kind") == "quantitative_result"]
    pairwise_requirements = [item for item in requirements if item.get("kind") == "pairwise_audit_comparison"]
    scientific_result_requirements = [item for item in requirements if item.get("kind") == "scientific_result"]
    requirement_groups = _quantitative_requirement_groups(
        [*scientific_result_requirements, *key_metric_requirements, *quantitative_result_requirements, *pairwise_requirements]
    )
    group_slide_counts = dict(quantitative_group_slide_counts or {})
    for key, count in group_slide_counts.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise ValueError("quantitative_group_slide_counts must map source keys to positive integers")
    required_quantitative_slides = sum(
        group_slide_counts.get(key, 1) for key, _ in requirement_groups
    )
    expanded_results = _expanded_evidence_items(results)
    distinct_result_locators = {
        _text(item.get("figure_table_equation"), "").casefold()
        for item in expanded_results
        if _text(item.get("figure_table_equation"), "")
    }
    has_quantitative = bool(requirements)
    minimum_narrative_slides = 4  # title, context, problem, and method
    if has_quantitative and required_quantitative_slides + minimum_narrative_slides > high:
        raise RuntimeError(
            "quantitative-coverage failure: "
            f"{required_quantitative_slides} packed quantitative slides plus "
            f"{minimum_narrative_slides} core narrative slides cannot fit the {high}-slide budget"
        )
    dense_coverage = high <= 12 and len(distinct_result_locators) >= 5 and not has_quantitative
    result_items = _dense_result_items(results, max(1, high - 6)) if dense_coverage else results
    required_contexts = [
        ("method", method), ("process", process_asset_item or process),
        *(("results", item) for item in result_items),
    ]
    reserved = _reserved_required_asset_ids(required_contexts, visible)

    def evidence(item: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
        if item is None:
            return []
        items = [item] if isinstance(item, Mapping) else [value for value in item if isinstance(value, Mapping)]
        return [{
            "summary": _audience(value.get("summary"), "[MISSING: reviewed evidence]"),
            "evidence": _audience(value.get("evidence"), "[MISSING: reviewed evidence]"),
            "source_page": value.get("source_page"),
            "section": _text(value.get("section"), "[MISSING: section]"),
            "locator": _text(value.get("figure_table_equation"), "[MISSING: locator]"),
        } for value in items]

    def slide(
        role: str,
        takeaway: str,
        item: Mapping[str, Any] | None = None,
        *,
        ownership: str = "author_conclusion",
        process: str = "",
        table_host: bool = False,
        archetypes: Sequence[str] = (),
        evidence_items: Sequence[Mapping[str, Any]] | None = None,
        asset_item: Mapping[str, Any] | None = None,
        conclusion_points: Sequence[str] | None = None,
        conclusion_components: Mapping[str, Mapping[str, Any]] | None = None,
        discussion_categories: Sequence[str] = (),
        role_overlap_reason: str = "",
        role_selection: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = item or (evidence_items[0] if evidence_items else None) or {}
        binding_item = current.get("_binding_item") if isinstance(current.get("_binding_item"), Mapping) else current
        selected, selection = _asset_choice(role, asset_item or current, visible, used, reserved)
        bound_items = list(evidence_items) if evidence_items is not None else ([binding_item] if binding_item else [])
        bound_evidence = evidence(bound_items)
        selection = _finalize_asset_selection(selection, selected, bound_items)
        final_evidence_locators = (
            [selection["evidence_locator"]]
            if selected and selection.get("evidence_locator")
            else list(dict.fromkeys(
                entry["locator"] for entry in bound_evidence if entry["locator"]
            ))
        )
        planned = {
            "role": role,
            "semantic_role": role,
            "takeaway": takeaway,
            "evidence": bound_evidence,
            "evidence_locators": final_evidence_locators,
            "asset_policy": asset_policy_for_role(role),
            "recommended_assets": selected,
            "asset_selection": selection,
            "ownership": ownership,
            "process_explanation": process,
            "archetypes": list(archetypes),
            "audience": effective["audience"],
            "density": effective["density"],
        }
        if table_host:
            planned["table_host"] = True
        if conclusion_points is not None:
            planned["conclusion_points"] = list(conclusion_points)
        if conclusion_components is not None:
            planned["conclusion_components"] = {
                str(key): dict(value) for key, value in conclusion_components.items()
            }
        if discussion_categories:
            planned["discussion_categories"] = list(discussion_categories)
        if role_overlap_reason:
            planned["role_overlap_reason"] = role_overlap_reason
        if isinstance(role_selection, Mapping):
            planned["role_selection"] = dict(role_selection)
            planned["semantic_evidence_type"] = _text(
                role_selection.get("semantic_evidence_type"), "missing"
            )
            planned["evidence_section"] = _text(role_selection.get("evidence_section"), "")
            planned["role_compatibility_score"] = int(role_selection.get("role_compatibility_score") or 0)
            selected_candidate = role_selection.get("selected_candidate")
            if isinstance(selected_candidate, Mapping):
                planned["origin_semantic_slot"] = _text(selected_candidate.get("origin_semantic_slot"), "")
                planned["origin_reviewed_semantics_hash"] = _text(
                    selected_candidate.get("origin_reviewed_semantics_hash"), ""
                )
            else:
                planned["origin_semantic_slot"] = ""
                planned["origin_reviewed_semantics_hash"] = ""
        if planned["asset_policy"]["mode"] == "required" and not selected:
            representation = _native_representation(current, selection["evidence_locator"])
            if representation["type"] == "evidence-flow":
                planned["native_diagram"] = representation
            else:
                planned["native_representation"] = representation
            # A source-bound editable fallback is an intentional resolution when
            # no matcher-approved visual remains.  Do not carry reservation or
            # locator conflicts into the review record as unresolved errors.
            planned["asset_selection"]["conflicts"] = []
        elif not selected and planned["asset_policy"].get("allow_no_asset"):
            # Optional/no-asset roles deliberately decline ambiguous visuals.
            planned["asset_selection"]["conflicts"] = []
        return planned

    def requirement_evidence(requirement: Mapping[str, Any]) -> list[dict[str, Any]]:
        source = requirement.get("source") if isinstance(requirement.get("source"), Mapping) else {}
        locator = _text(source.get("locator"), "")
        if not locator:
            return []
        return [{
            "summary": _audience(requirement.get("display_text"), "[MISSING: quantitative display text]"),
            "evidence": locator,
            "source_page": source.get("page") if isinstance(source.get("page"), int) else None,
            "section": _text(source.get("section"), locator),
            "locator": locator,
        }]

    def quantitative_slide(
        requirement_ids: Sequence[str],
        takeaway: str,
        evidence_items: Sequence[Mapping[str, Any]],
        quantitative_index: int,
    ) -> dict[str, Any]:
        locator = _text(evidence_items[0].get("locator") if evidence_items else None, "")
        return {
            "role": "metrics",
            "semantic_role": "metrics",
            "takeaway": takeaway,
            "evidence": [dict(item) for item in evidence_items],
            "evidence_locators": [locator] if locator else [],
            "asset_policy": asset_policy_for_role("metrics"),
            "recommended_assets": [],
            "asset_selection": {
                "candidate_id": None,
                "score": 0,
                "reasons": [],
                "conflicts": [],
                "evidence_locator": locator,
            },
            "ownership": "author_conclusion",
            "process_explanation": "",
            "coverage_requirement_ids": list(requirement_ids),
            "quantitative_index": quantitative_index,
            "quantitative_focus": f"{locator} 的关键定量结果" if locator else "关键定量结果",
        }

    def quantitative_slides_for(
        grouped_requirements: Sequence[tuple[tuple[str, int], list[Mapping[str, Any]]]],
        first_index: int,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        next_index = first_index
        for key, group in grouped_requirements:
            locator = _text((group[0].get("source") or {}).get("locator") if isinstance(group[0].get("source"), Mapping) else None, "")
            evidence_items: list[dict[str, Any]] = []
            for group_item in group:
                evidence_items.extend(requirement_evidence(group_item))
            panel_count = group_slide_counts.get(key, 1)
            for panel_index in range(panel_count):
                planned = quantitative_slide(
                    [str(item["id"]) for item in group],
                    _quantitative_group_title(locator, group),
                    evidence_items,
                    next_index,
                )
                if panel_count > 1:
                    planned["quantitative_panel_index"] = panel_index
                    planned["quantitative_panel_count"] = panel_count
                    planned["quantitative_source_key"] = [key[0], key[1]]
                result.append(planned)
                next_index += 1
        return result

    if has_quantitative:
        quantitative_index = 1
        quantitative_slide_blocks = quantitative_slides_for(
            requirement_groups,
            quantitative_index,
        )
        base = [
            slide("title", "论文、版本与本次讨论范围。", metadata_item, ownership="paper_metadata", asset_item=title_asset_item),
            slide("background", _audience(background.get("summary") if background else None, "背景证据待人工确认。"), background, role_overlap_reason=role_overlap_reason, role_selection=role_selections["background"]),
            slide("problem", _audience(problem.get("summary") if problem else None, "问题证据待人工确认。"), problem, role_selection=role_selections["problem"]),
            slide("method", _audience(method.get("summary") if method else None, "[MISSING: reviewed method]"), method, process="先定位论文的方法总览，再把后续阶段连接到可评估的输出。", archetypes=("method-overview",), role_selection=role_selections["method"]),
            *([slide("process", _audience(process.get("summary"), "[MISSING: reviewed process]"), process, process="核心阶段从方法表示/变换出发，经过论文所述机制，再连接到可执行或可评估的输出。", archetypes=("core-stage", "mechanism"), asset_item=process_asset_item, role_selection=role_selections["process"])] if process else []),
            *quantitative_slide_blocks,
            slide("conclusion", "结论汇总贡献、主要结果与已报告的限制。", evidence_items=conclusion_items, conclusion_points=conclusion_points, conclusion_components=conclusion_components),
            slide("discussion", discussion_questions[0], discussion_evidence[0] if discussion_evidence else None, ownership="presenter_discussion", evidence_items=discussion_evidence),
            slide("sources", "来源、页码与资产出处可回溯。", metadata_item, ownership="paper_metadata"),
        ]
        base[-2]["discussion_questions"] = discussion_questions
        base[-2]["discussion_grounding"] = discussion_grounding
        base[-2]["discussion_categories"] = discussion_categories
    else:
        base = [
            # The cover reports paper metadata; it is not presenter-owned analysis.
            slide("title", "论文、版本与本次讨论范围。", metadata_item, ownership="paper_metadata", asset_item=title_asset_item),
            slide("background", _audience(background.get("summary") if background else None, "背景证据待人工确认。"), background, role_overlap_reason=role_overlap_reason, role_selection=role_selections["background"]),
            slide("problem", _audience(problem.get("summary") if problem else None, "问题证据待人工确认。"), problem, role_selection=role_selections["problem"]),
            slide("method", _audience(method.get("summary") if method else None, "[MISSING: reviewed method]"), method, process="先定位论文的方法总览，再把后续阶段连接到可评估的输出。", archetypes=("method-overview",), role_selection=role_selections["method"]),
            *([slide("process", _audience(process.get("summary"), "[MISSING: reviewed process]"), process, process="核心阶段从方法表示/变换出发，经过论文所述机制，再连接到可执行或可评估的输出。", archetypes=("core-stage", "mechanism"), asset_item=process_asset_item, role_selection=role_selections["process"])] if process else []),
            slide("metrics", "评价指标必须与论文结果证据对应。", metric),
            slide("results", _audience(metric.get("summary") if metric else None, "[MISSING: reviewed experimental result]"), metric),
            slide("analysis", "分析区分作者证据与汇报者可讨论的解释。", results[1] if len(results) > 1 else metric, ownership="presenter_discussion"),
            slide("conclusion", "结论汇总贡献、主要结果与已报告的限制。", evidence_items=conclusion_items, conclusion_points=conclusion_points, conclusion_components=conclusion_components),
            slide("discussion", discussion_questions[0], discussion_evidence[0] if discussion_evidence else None, ownership="presenter_discussion", evidence_items=discussion_evidence),
            slide("sources", "来源、页码与资产出处可回溯。", metadata_item, ownership="paper_metadata"),
        ]
        base[-2]["discussion_questions"] = discussion_questions
        base[-2]["discussion_grounding"] = discussion_grounding
        base[-2]["discussion_categories"] = discussion_categories
    if dense_coverage:
        used.clear()
        context_takeaway = (
            f"\u80cc\u666f\uff1a{_text(problem.get('summary') if problem else None, '[MISSING: reviewed research problem]')}\uff1b"
            f"\u95ee\u9898\u7531\u6b64\u4ea7\u751f\uff1a{_text(problem.get('evidence') if problem else None, '[MISSING: reviewed context]')}"
        )
        method_takeaway = _text(method.get("summary") if method else None, "[MISSING: reviewed method]")
        if metric and metric is not method:
            method_takeaway += f"\uff1b\u8bc4\u4ef7\u6307\u6807\uff1a{_text(metric.get('summary'), '[MISSING: reviewed metrics]')}"
        conclusion_takeaway = (
            f"\u8bc1\u636e\u603b\u7ed3\uff1a{_text(claims[0].get('summary') if claims else None, '[MISSING: reviewed conclusion]')}\uff1b"
            "\u4ee5\u4e0b\u95ee\u9898\u7528\u4e8e\u8ba8\u8bba\u5176\u9002\u7528\u8303\u56f4\u4e0e\u9650\u5236\u3002"
        )
        base = [
            slide("title", "论文、版本与本次讨论范围。", metadata_item, ownership="paper_metadata", asset_item=title_asset_item),
            slide("background", context_takeaway, background, role_selection=role_selections["background"]),
            slide("question", _text(question.get("summary") if question else None, "[MISSING: reviewed question]"), question),
            slide("method", method_takeaway, method, process="\u4ece\u8f93\u5165\u51fa\u53d1\uff0c\u6309\u8bba\u6587\u63cf\u8ff0\u5904\u7406\uff0c\u518d\u4ea7\u51fa\u53ef\u8bc4\u4f30\u7684\u7ed3\u679c\uff1b\u6bcf\u4e2a\u6b65\u9aa4\u90fd\u4e0e\u8bba\u6587\u7ed3\u679c\u76f8\u8fde\u3002", role_selection=role_selections["method"]),
            *[
                slide(
                    "results",
                    _text(item.get("summary"), "[MISSING: reviewed result]"),
                    item,
                    table_host=bool(re.search(r"\btable\b", _text(item.get("figure_table_equation"), ""), re.IGNORECASE)),
                )
                for item in result_items
            ],
            slide("discussion", conclusion_takeaway, claims[0] if claims else metric, ownership="presenter_discussion"),
            slide("sources", "source trail", ownership="presenter_discussion"),
        ]
    if contract.deck_type.value == "conference":
        # A conference deck remains source-bound, but it is a speaker-led teaser:
        # do not turn its explanatory analysis slot into a critique slide.
        base = [item for item in base if item["role"] != "analysis"]
        for item in base:
            if item["role"] in {"method", "process", "results", "evidence"}:
                item["archetypes"] = [*item["archetypes"], "figure-forward"]
    elif contract.deck_type.value == "thesis-defense":
        contribution = contributions[0] if contributions else method
        base.insert(1, slide(
            "background",
            "贡献路线图：以下各部分分别说明可追溯的研究贡献。",
            contribution,
            archetypes=("section-divider",),
        ))
        backup_item = results[-1] if results else contribution
        base.insert(-1, slide(
            "evidence",
            "备份材料：保留可追溯的补充结果以回应答辩追问。",
            backup_item,
            archetypes=("backup", "appendix"),
        ))
        limitation = _evidence_with_terms(all_evidence, ("limitation", "limitations", "局限"))
        if limitation is not None:
            base.insert(-2, slide(
                "analysis",
                _text(limitation.get("summary"), "论文已述局限需要在答辩中说明。"),
                limitation,
                archetypes=("limitations",),
            ))
        future_work = _evidence_with_terms(all_evidence, ("future work", "future-work", "future", "未来工作", "后续工作"))
        if future_work is not None:
            base.insert(-2, slide(
                "evidence",
                _text(future_work.get("summary"), "论文已述后续工作需要在答辩中说明。"),
                future_work,
                archetypes=("future-work",),
            ))
    # The reviewed structure, rather than a paper name or fixed figure sequence,
    # decides which evidence-bearing roles earn dedicated pages.
    # At the upper journal-club budget, quantitative tables may expand into
    # multiple native panels during rendering.  Reserve one optional question
    # slot so that readability-preserving expansion stays within the contract.
    if (
        not dense_coverage
        and len(claims) > 1
        and len(base) < high
        and not (high >= 15 and quantitative_requirements)
    ):
        distinct_question = next((claim for claim in claims if claim is not problem), claims[1])
        base.insert(3, slide("question", _text(distinct_question.get("summary"), "[MISSING: reviewed question]"), distinct_question))
    for item in (() if dense_coverage else contributions[1:]):
        if len(base) + (1 if high >= 15 and quantitative_requirements else 0) >= high:
            break
        base.insert(-4, slide("contribution", _text(item.get("summary"), "[MISSING: reviewed contribution]"), item))
    # Rich evidence earns additional result pages, within the configured budget.
    if has_quantitative:
        covered_locators = {
            _text(item.get("source", {}).get("locator"), "").casefold()
            for item in requirements
            if item.get("kind") != "key_metric"
        }
        optional_result_items = [
            item
            for item in results
            if _text(item.get("figure_table_equation"), "").casefold() not in covered_locators
            and not re.search(_NUMBER_RE.pattern, _text(item.get("summary"), ""))
        ]
        result_iter = optional_result_items
    else:
        result_iter = (() if dense_coverage else results[1:])
    for item in result_iter:
        if len(base) + (1 if high >= 15 and quantitative_requirements else 0) >= high:
            break
        base.insert(-3, slide("results", _text(item.get("summary"), "[MISSING: reviewed result]"), item))
    def merge_role(target_role: str, source_role: str) -> bool:
        target_index = next((index for index, item in enumerate(base) if item["role"] == target_role), None)
        source_index = next((index for index, item in enumerate(base) if item["role"] == source_role), None)
        if target_index is None or source_index is None:
            return False
        target, source = base[target_index], base[source_index]
        target["merged_roles"] = list(dict.fromkeys([*target.get("merged_roles", []), source_role]))
        target.setdefault("merged_role_takeaways", {})[source_role] = source.get("takeaway", "")
        target.setdefault("merged_role_evidence", {})[source_role] = list(source.get("evidence", []))
        if source.get("role_selection"):
            target.setdefault("merged_role_selections", {})[source_role] = dict(source["role_selection"])
        target["evidence"] = [
            dict(item)
            for item in _unique_items([*target.get("evidence", []), *source.get("evidence", [])])
        ]
        target["evidence_locators"] = list(dict.fromkeys([
            *target.get("evidence_locators", []),
            *source.get("evidence_locators", []),
        ]))
        if source_role == "conclusion":
            target["conclusion_points"] = list(source.get("conclusion_points", []))
            target["conclusion_components"] = dict(source.get("conclusion_components", {}))
        base.pop(source_index)
        return True

    # Compress optional structure before considering any required quantitative
    # group. Context/problem and conclusion/discussion remain visible when they
    # share a page; source references are already carried by every slide.
    while len(base) > high:
        sources_index = next((index for index, item in enumerate(base) if item["role"] == "sources"), None)
        if sources_index is not None:
            base.pop(sources_index)
            continue
        if merge_role("background", "problem"):
            continue
        if merge_role("discussion", "conclusion"):
            continue
        removable = next((
            index
            for index, item in enumerate(base)
            if item["role"] in {"question", "contribution", "evidence"}
            or (
                item["role"] == "analysis"
                and not {"limitations", "future-work"} & set(item.get("archetypes", ()))
            )
            or (item["role"] == "metrics" and not item.get("coverage_requirement_ids"))
            or (item["role"] == "results" and not item.get("coverage_requirement_ids"))
        ), None)
        if removable is not None:
            base.pop(removable)
            continue
        if has_quantitative:
            raise RuntimeError(
                "quantitative-coverage failure: "
                f"{required_quantitative_slides} packed quantitative slides cannot fit "
                f"with the minimum narrative in the {high}-slide budget"
            )
        raise RuntimeError("narrative defaults cannot fit the configured slide budget")
    target_low = low - 1 if high >= 15 and quantitative_requirements else low
    padding_pool = _unique_items([*contributions, method, *claims, *results, background, problem, metric])
    for item in padding_pool:
        if len(base) >= target_low:
            break
        index = len(base) - 3
        base.insert(index, slide("evidence", "补充证据：请核对对应来源页码的论文证据。", item))
    # Guard against accidental adjacent copy when evidence inputs repeat.
    for index in range(1, len(base)):
        if base[index]["takeaway"] == base[index - 1]["takeaway"]:
            base[index]["takeaway"] = "\u53e6\u4e00\u89c6\u89d2\uff1a" + base[index]["takeaway"]
    return {
        "deck_type": contract.deck_type.value,
        "audience": effective["audience"],
        "density": effective["density"],
        "arc": list(contract.narrative_arc),
        "contract": contract.as_dict(),
        "slides": base,
    }
