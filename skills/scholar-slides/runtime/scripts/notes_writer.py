"""Fail-closed, paper-agnostic construction of grounded Chinese speaker notes."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from audience_text import (
    PROVENANCE_LEAK_RE,
    REFERENCE_NUMBER_RE,
    mask_non_claim_numeric_spans,
    protect_math_spans,
    restore_math_spans,
    sanitize_audience_text,
)


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?%?(?![\w.])")
_REFERENCE_NUMBER_RE = REFERENCE_NUMBER_RE
_MARKER_RE = re.compile(r"\[(?:MISSING|UNVERIFIED)(?::[^\]]*)?\]", re.I)
_INTERNAL_RE = re.compile(
    r"\b(?:ckpt(?:[- ]?\d+)?|checkpoint|audit(?:ed)?|ledger|marker|sha[- ]?256|hash(?:ed|ing)?|artifact[- ]?bundle|resolved_with_audit|pending_human_confirmation)\b|"
    + PROVENANCE_LEAK_RE.pattern,
    re.I,
)
_PLACEHOLDER_RE = re.compile(r"(?:<\s*(?:presenter|date|name|author|todo|tbd|placeholder)[^>]*>|\b(?:todo|tbd|placeholder|template)\b)", re.I)
_DOUBLE_PUNCTUATION_RE = re.compile(r"(?:[。！？]{2,}|[!?]{2,})")
_FIGURE_SPACING_RE = re.compile(r"\b(?:figure|fig\.?)(?:\d)", re.I)
_MALFORMED_TRANSITION_RE = re.compile(r"(?:本页聚请|聚请(?:说明|展示|介绍|讨论)?)")
_INTERNAL_DELIVERY_RE = re.compile(
    r"(?:\b(?:ckpt(?:[- ]?\d+)?|checkpoint|audit(?:ed)?|ledger|marker|sha[- ]?256|hash(?:ed|ing)?|"
    r"artifact[- ]?bundle|resolved_with_audit|pending_human_confirmation)\b|"
    r"(?:\u5df2\u5ba1\u9605|\u5ba1\u9605(?:\u5b8c\u6210|\u6d41\u7a0b)?|\u8bc1\u636e\u7ed1\u5b9a|\u6765\u6e90\u7ed1\u5b9a|\u5185\u90e8\u6d41\u7a0b|\u7cfb\u7edf\u751f\u6210|\u81ea\u52a8\u751f\u6210|\u8d28\u91cf\u68c0\u67e5|\u5ba1\u6838\u8bb0\u5f55|\u672c\u9875\u4ec5\u5c55\u793a\u5df2\u5165\u5e93\u7684\u8bba\u6587\u8bc1\u636e|\u9875\u9762\u7ed1\u5b9a|\u5ba1\u9605\u8303\u56f4|\u8bc1\u636e\u5e93))",
    re.IGNORECASE,
)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)?")
_DISCOURSE_CONNECTIVE_RE = re.compile(r"(?:因此|所以|因而|但是|然而|同时|此外|随后|然后|并且|而且|以及|如果|虽然|因为)")
_TRUNCATED_ENDING_RE = re.compile(r"(?:仍待|待补充|未完|未尽|待定|待续|[，、：；…]|\.\.\.)$")
_CONTENT_FIELDS = frozenset({"text", "transition", "visual_guidance", "takeaway", "source_refs"})
SUPPORTED_ROLES = frozenset({
    "title", "background", "research-question", "method-overview", "concept-or-metric",
    "experiment", "comparison", "results-table", "analysis", "conclusion",
    "presenter-discussion", "references",
})
# Role ids are useful for machine contracts, but they are not suitable presenter
# language.  Keep the ids in structured fields and use these labels only when
# composing visible Chinese delivery text.
_ROLE_LABELS_ZH = {
    "title": "标题信息",
    "background": "研究背景",
    "research-question": "研究问题",
    "method-overview": "方法概览",
    "concept-or-metric": "关键概念或指标",
    "experiment": "实验设计",
    "comparison": "方案比较",
    "results-table": "结果表",
    "analysis": "结果分析",
    "conclusion": "结论",
    "presenter-discussion": "开放讨论",
    "references": "参考来源",
}
_RAW_ROLE_LABEL_RE = re.compile(
    r"本页\s*(?:" + "|".join(re.escape(role) for role in sorted(SUPPORTED_ROLES)) + r")(?=的|对|要点|[，。！？!?]|$)",
    re.IGNORECASE,
)
_SHORT_ROLES = frozenset({"title", "references"})
_ALLOWED_EVIDENCE_METADATA = frozenset({"audit_metadata", "provenance", "metadata", "source", "reviewed_at", "reviewed_by", "source_sha256", "pdf_sha256"})
_INTERNAL_EVIDENCE_FIELDS = frozenset({
    "audit_metadata", "audit_ref", "direct_source_evidence", "evidence_type", "semantic_evidence_type",
    "semantic_type", "semantic_slot", "source_refs", "role_selection", "provenance", "metadata", "source_sha256", "pdf_sha256",
})
_INTERNAL_SLIDE_FIELDS = frozenset({
    "role_selection", "semantic_evidence_type", "evidence_section", "role_compatibility_score",
})
_SPEAKER_VISIBLE_TABLE_FIELDS = frozenset({
    "caption", "columns", "rows", "footnote", "source_label", "locator",
})


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    detail: str
    slide: int | None = None


@dataclass(frozen=True)
class NotesContext:
    evidence: Mapping[str, Any] = field(default_factory=dict)
    language: str = "zh-CN"
    slides: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class SpeakerNote:
    text: str
    transition: str
    visual_guidance: str
    takeaway: str
    source_refs: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_refs"] = [dict(item) for item in self.source_refs]
        return payload


class LegacySpeakerNotes(list[str]):
    """List-of-strings projection with explicit legacy-v1 validation context."""

    schema = "legacy-v1"

    def __init__(self, values: Sequence[str], language: str) -> None:
        super().__init__(values)
        self.language = language


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _strings(child)


def _unsafe(value: Any) -> bool:
    return any(_MARKER_RE.search(text) or _INTERNAL_RE.search(text) or _PLACEHOLDER_RE.search(text) for text in _strings(value))


def _role(slide: Mapping[str, Any]) -> str:
    role = slide.get("role")
    if not isinstance(role, str) or role not in SUPPORTED_ROLES:
        raise ValueError(f"unknown speaker-note role: {role if isinstance(role, str) else 'missing role'}")
    return role


def _role_label(role: str) -> str:
    """Return a natural-language label for a machine role id."""
    return _ROLE_LABELS_ZH.get(role, "本页内容")


def _reviewed_items(evidence: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    result: list[tuple[str, Mapping[str, Any]]] = []
    for key, kind in (("reviewed_claims", "reviewed-claim"), ("reviewed_contributions", "reviewed-contribution"), ("reviewed_experimental_results", "reviewed-result"), ("reviewed_semantic_slots", "reviewed-semantic-slot"), ("audited_table_evidence", "audited-table"), ("paper_metadata_evidence", "paper-metadata")):
        value = evidence.get(key)
        if isinstance(value, list):
            result.extend((kind, item) for item in value if isinstance(item, Mapping))
    if not any(kind == "paper-metadata" for kind, _ in result):
        metadata = evidence.get("paper_metadata") if isinstance(evidence.get("paper_metadata"), Mapping) else {}
        title = metadata.get("title") if isinstance(metadata.get("title"), str) else "Paper metadata"
        metadata_evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
        title_record = metadata_evidence.get("title") if isinstance(metadata_evidence.get("title"), Mapping) else {}
        locations = title_record.get("locations") if isinstance(title_record.get("locations"), list) else []
        locator = next((value for value in locations if isinstance(value, str) and value.strip()), "p. 1")
        match = re.search(r"(?:p(?:age)?\.?\s*)(\d+)", locator, re.IGNORECASE)
        result.append(("paper-metadata", {"summary": title, "evidence": title, "source_page": int(match.group(1)) if match else 1, "section": "Paper metadata", "figure_table_equation": locator}))
    return result


def _audience_reviewed_items(evidence: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Match bindings against the same sanitized projection that reaches the audience."""
    projected: list[tuple[str, Mapping[str, Any]]] = []
    for kind, item in _reviewed_items(evidence):
        copy = dict(item)
        for field in ("summary", "evidence"):
            if isinstance(copy.get(field), str):
                copy[field] = sanitize_audience_text(copy[field], copy[field])
        projected.append((kind, copy))
    return projected


def _field(item: Mapping[str, Any], name: str, fallback: str = "") -> str:
    value = item.get(name, fallback)
    return value.strip() if isinstance(value, str) else fallback


def _identity(kind: str, item: Mapping[str, Any]) -> str:
    page = item.get("source_page")
    section = _field(item, "section")
    locator = _field(item, "figure_table_equation", _field(item, "locator"))
    canonical = {
        "kind": kind, "source_page": page, "section": section, "locator": locator,
        "summary": item.get("summary"), "evidence": item.get("evidence"), "rows": item.get("rows"),
    }
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{kind}:{page}:{section}:{locator}:{digest}"


def _source_ref(kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
    page = item.get("source_page")
    section = _field(item, "section")
    locator = _field(item, "figure_table_equation", _field(item, "locator"))
    if not isinstance(page, int) or page < 1 or not section or not locator:
        raise ValueError("speaker-note evidence requires a positive reviewed source_page, section, and locator")
    return {"source_page": page, "section": section, "locator": locator, "kind": kind, "evidence_id": _identity(kind, item)}


def _numbers(item: Mapping[str, Any]) -> set[str]:
    """Numbers from reviewed claim/result prose or an audited table's visible cells only."""
    fields: list[Any] = [item.get("summary"), item.get("evidence"), item.get("rows"), item.get("key_numbers"), item.get("speaker_key_values")]
    return {
        token
        for value in fields
        for text in _strings(value)
        for token in _NUMBER_RE.findall(mask_non_claim_numeric_spans(text))
    }


def _slide_allowed_numbers(slide: Mapping[str, Any], item: Mapping[str, Any]) -> set[str]:
    allowed = _numbers(item)
    values = slide.get("speaker_allowed_numbers")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        allowed.update(
            str(value)
            for value in values
            if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        )
    return allowed


def _delivery_numbers(text: str) -> set[str]:
    """Numbers spoken as claims, excluding source locator labels such as ``Table 1``."""
    return set(_NUMBER_RE.findall(mask_non_claim_numeric_spans(text)))


def _bounded_sentence(value: str, limit: int, fallback: str) -> str:
    """Return one complete, speakable sentence rather than a clipped label."""
    cleaned = _INTERNAL_DELIVERY_RE.sub("", re.sub(r"\s+", " ", value)).strip()
    first = re.split(r"[。！？!?]", cleaned, maxsplit=1)[0].strip(" ，、：；")
    if not first or len(_CJK_RE.findall(first)) > limit or len(first) > 160:
        return fallback
    return f"{first}。"


def _visible_slide_numbers(slide: Mapping[str, Any]) -> set[str]:
    values = [slide.get(key) for key in ("title", "action_title", "core_conclusion", "annotation", "points", "points2")]
    return {
        token
        for value in values
        for text in _strings(value)
        for token in _NUMBER_RE.findall(mask_non_claim_numeric_spans(text))
    }


def _bound_item(slide: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    refs = _audience_reviewed_items(evidence)
    if not refs:
        raise ValueError("speaker-note writer requires reviewed evidence")
    role_selection = slide.get("role_selection")
    if isinstance(role_selection, Mapping) and _field(role_selection, "status").casefold() == "missing":
        # A pending role still needs a source-grounded note object so deck
        # generation remains inspectable.  The selected-role contract remains
        # missing and semantic QA blocks readiness; this fallback is not slide
        # coverage and cannot turn the first reviewed item into role evidence.
        return refs[0]
    binding = slide.get("speaker_evidence_binding")
    if isinstance(binding, Mapping):
        if binding.get("kind") == "quantitative-coverage":
            # Quantitative slides are bound to confirmed coverage requirements
            # rather than one reviewed prose item; the writer still needs a
            # synthetic source-bound item for natural speaker guidance.
            return "quantitative-coverage", {
                "summary": _field(binding, "summary"),
                "evidence": _field(binding, "evidence"),
                "source_page": binding.get("source_page"),
                "section": _field(binding, "section"),
                "figure_table_equation": _field(binding, "locator"),
                "key_numbers": binding.get("key_numbers", slide.get("quantitative_key_numbers", [])),
                "speaker_focus": _field(binding, "focus", _field(binding, "speaker_focus", _field(slide, "quantitative_focus", "关键定量结果"))),
                "technical_terms": binding.get("technical_terms", slide.get("speaker_technical_terms", [])),
            }
        wanted = binding.get("evidence_id")
        if isinstance(wanted, str):
            matches = [(kind, item) for kind, item in refs if _identity(kind, item) == wanted]
            if len(matches) == 1:
                return matches[0]
        bound_summary = _field(binding, "summary")
        bound_page = binding.get("source_page")
        bound_section = _field(binding, "section")
        bound_locator = _field(binding, "locator")
        matches = [
            (kind, item) for kind, item in refs
            if item.get("source_page") == bound_page
            and _field(item, "section") == bound_section
            and _field(item, "figure_table_equation", _field(item, "locator")) == bound_locator
            and _field(item, "summary") == bound_summary
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError("speaker-note slide has no matching reviewed evidence binding")
    source = slide.get("source_ref")
    if isinstance(source, str):
        pages = {int(token) for token in re.findall(r"(?<!\d)(\d+)(?!\d)", source)}
        matches = [(kind, item) for kind, item in refs if item.get("source_page") in pages]
        if len(matches) == 1:
            return matches[0]
    if len(refs) == 1:
        return refs[0]
    raise ValueError("speaker-note slide requires one explicit reviewed evidence binding")


def _takeaway(slide: Mapping[str, Any], item: Mapping[str, Any], role: str) -> str:
    for key in ("core_conclusion", "action_title", "title", "annotation"):
        value = slide.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _field(item, "summary", f"本页围绕{_role_label(role)}的论文证据展开")


_TRANSITIONS = {
    "title": "开场先说明本次汇报只依据论文原文材料。", "background": "在明确主题后，先回到问题产生的背景。",
    "research-question": "有了背景，接下来聚焦论文提出的具体问题。", "method-overview": "问题明确后，再看作者如何组织整体方法。",
    "concept-or-metric": "理解方法之前，先统一本页关键概念或度量。", "experiment": "定义清楚后，转向实验设计与执行过程。",
    "comparison": "实验设置明确后，比较不同方案所回答的问题。", "results-table": "比较框架建立后，阅读页面中的结果证据。",
    "analysis": "结果呈现后，讨论这些证据能够支持到什么范围。", "conclusion": "最后收束为与证据边界一致的结论。",
    "presenter-discussion": "证据总结后，进入汇报者的开放讨论。", "references": "结束前回到可追溯的来源，方便会后复核。",
}


def _narrative_neighbor(value: str, fallback: str, allowed_terms: Sequence[str] = ()) -> str:
    """Keep a Chinese narrative handoff without reading an English label aloud."""
    if not _CJK_RE.search(value):
        return fallback
    protected, math_spans = protect_math_spans(value)
    # Source labels such as ``Table 1`` and ``Figure 6`` are locator metadata,
    # not spoken quantities; keep a natural Chinese subject while removing the
    # machine-facing number.
    def replace_locator(match: re.Match[str]) -> str:
        raw = match.group(0).casefold()
        if raw.startswith(("table", "tab")):
            return "表格"
        if raw.startswith(("figure", "fig")):
            return "图中"
        if raw.startswith(("equation", "eq")):
            return "公式"
        return ""

    simplified = _REFERENCE_NUMBER_RE.sub(replace_locator, protected)
    # A mixed Chinese/English narrative neighbor may contain an entire
    # untranslated sentence (for example ``另一视角：The question is
    # testable.``).  Remove sentence-like runs while retaining compact,
    # source-grounded technical terms such as ``coding model`` and
    # ``code-driven``.  The caller may provide additional grounded terms.
    allowed_phrases = tuple(
        phrase.casefold().strip()
        for phrase in allowed_terms
        if isinstance(phrase, str) and phrase.strip()
    )

    def remove_untranslated_run(match: re.Match[str]) -> str:
        raw = match.group(0)
        lowered = raw.casefold()
        if any(phrase in lowered for phrase in allowed_phrases):
            return raw
        if "-" in raw or re.search(r"\b[A-Z]{2,}\b", raw):
            return raw
        return ""

    simplified = re.sub(
        r"(?<![A-Za-z])(?:[A-Za-z]+(?:['-][A-Za-z]+)?(?:\s+[A-Za-z]+(?:['-][A-Za-z]+)?){3,})(?![A-Za-z])",
        remove_untranslated_run,
        simplified,
    )
    # Single-letter labels (for example A/B panel tags) are presentation
    # metadata rather than technical terminology; remove those isolated tags
    # while retaining grounded multi-word terms and model/metric names.
    simplified = re.sub(r"(?<![A-Za-z-])[A-Za-z](?![A-Za-z-])", "", simplified)
    # Neighboring takeaways are only handoffs; never read a reviewed numeric
    # value from the adjacent slide as if it were this slide's claim.  Grounded
    # technical terms remain intact (for example ``coding model`` or a metric
    # name); removing every Latin token creates malformed Chinese fragments.
    simplified = re.sub(r"(?<![\w.])\d+(?:\.\d+)?", "", simplified)
    simplified = re.sub(r"\s+", " ", simplified).strip(" ，、：；")
    simplified = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", simplified)
    return restore_math_spans(simplified or fallback, math_spans)


def _transition(slide: Mapping[str, Any], role: str) -> str:
    kinds = slide.get("quantitative_kinds") if isinstance(slide.get("quantitative_kinds"), list) else []
    if kinds:
        labels = {
            "key_metric": "关键指标",
            "quantitative_result": "定量结果",
            "pairwise_audit_comparison": "审计对比",
        }
        names = "、".join(labels.get(kind, "定量内容") for kind in kinds)
        focus = slide.get("quantitative_focus")
        if isinstance(focus, str) and focus.strip():
            return f"承接上一页的重点，本页聚焦{sanitize_audience_text(focus, names)}。"
        return f"承接上一页的重点，本页聚焦{names}。"
    previous, following = slide.get("narrative_previous"), slide.get("narrative_next")
    technical_terms = slide.get("speaker_technical_terms", ()) if isinstance(slide.get("speaker_technical_terms"), Sequence) else ()
    if isinstance(previous, str) and previous.strip() and isinstance(following, str) and following.strip():
        return (
            f"承接上一页的{_narrative_neighbor(previous, '重点', technical_terms)}，本页聚焦{_role_label(role)}，"
            f"以便进入下一页的{_narrative_neighbor(following, '后续内容', technical_terms)}。"
        )
    return _TRANSITIONS[role]


def _visual(slide: Mapping[str, Any], role: str) -> str:
    kinds = slide.get("quantitative_kinds") if isinstance(slide.get("quantitative_kinds"), list) else []
    if kinds:
        return "请沿本页已展示的数值逐项说明，并将口头解释限定在已确认证据内。"
    table = slide.get("table")
    figure = slide.get("figure")
    claim_source = slide.get("claim_source") if isinstance(slide.get("claim_source"), Mapping) else {}
    visual_source = slide.get("visual_source") if isinstance(slide.get("visual_source"), Mapping) else {}
    if figure and visual_source.get("support_type") == "illustrative_support":
        visual_locator = _field(visual_source, "locator", "图示")
        claim_locator = _field(claim_source, "locator", "论断来源")
        return f"图中{visual_locator}仅作说明性辅助，论断请回到{claim_locator}。"
    if isinstance(table, Mapping):
        return "请先读表头和比较对象，再定位支持结论的单元格。"
    if isinstance(figure, Mapping):
        if role in {"method-overview", "experiment"}:
            return "请沿图中输入、定义和识别步骤说明方法链条，再指出如何连接结果。"
        return "请先指向图中的比较对象和变化趋势，再说明图形支持的范围。"
    return f"请沿着本页{_role_label(role)}的要点顺序说明，并将口头解释限定在已展示证据内。"


def _spoken_takeaway(slide: Mapping[str, Any], item: Mapping[str, Any], role: str) -> str:
    candidate = _takeaway(slide, item, role)
    allowed_terms = slide.get("speaker_technical_terms", ()) if isinstance(slide.get("speaker_technical_terms"), Sequence) else ()
    if _CJK_RE.search(candidate) and not _has_mixed_delivery(candidate, allowed_terms):
        return _bounded_sentence(candidate, 28, f"请概括本页{_role_label(role)}对理解论文的意义。")
    return f"请概括本页{_role_label(role)}对理解论文的意义。"


def _explanation(item: Mapping[str, Any], role: str = "") -> str:
    key_numbers = [str(value) for value in item.get("key_numbers", item.get("speaker_key_values", [])) if isinstance(value, (str, int, float))]
    focus = sanitize_audience_text(item.get("speaker_focus"), "关键定量结果")
    if key_numbers:
        return f"请围绕{focus}说明关键数值{'、'.join(list(dict.fromkeys(key_numbers))[:3])}，并把它们同研究问题联系起来。"
    # Locator numbers (for example the ``1`` in ``Table 1``) identify a
    # source asset; they are not quantitative claims and should not be read
    # aloud as a key value.
    fields: list[Any] = [item.get("summary"), item.get("evidence"), item.get("rows")]
    numbers = sorted(
        {
            token
            for value in fields
            for text in _strings(value)
            for token in _NUMBER_RE.findall(mask_non_claim_numeric_spans(text))
        },
        key=lambda token: (len(token), token),
    )
    if numbers:
        return f"请把页面中的关键数值{'、'.join(numbers[:3])}同研究问题联系起来，说明它们支持的结论范围。"
    if role in {"method-overview", "experiment"}:
        return "这里先交代作者如何把观察转成方法，再界定这套方法能够支持的结论范围。"
    if role in {"results-table", "comparison", "concept-or-metric", "analysis"}:
        return "这里把页面中的比较对象和观察结果联系起来，并说明证据能够支持的范围。"
    if role in {"background", "research-question"}:
        return "这里先交代问题从何而来，再说明论文把研究问题限定在什么范围。"
    if role == "presenter-discussion":
        return "这里把论文已经报告的观察与汇报中的延伸问题分开，并保留证据的边界。"
    if role == "references":
        return "这里回到论文原文的出处，方便把页面上的判断追溯到对应证据。"
    return "这里先说明作者报告的观察，再说明该观察能够支持的结论范围。"


def _merged_role_explanation(slide: Mapping[str, Any]) -> str:
    """Project one source-grounded sentence for each role merged into a slide."""
    roles = slide.get("merged_roles")
    evidence_by_role = slide.get("merged_role_evidence")
    if (
        not isinstance(roles, Sequence)
        or isinstance(roles, (str, bytes))
        or not isinstance(evidence_by_role, Mapping)
    ):
        return ""

    sentences: list[str] = []
    for role in roles:
        if not isinstance(role, str):
            continue
        candidates = evidence_by_role.get(role)
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
            continue
        item = next((
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and isinstance(candidate.get("source_page"), int)
            and candidate["source_page"] > 0
            and _field(candidate, "section")
            and _field(candidate, "figure_table_equation", _field(candidate, "locator"))
            and (_field(candidate, "summary") or _field(candidate, "evidence"))
        ), None)
        if item is None:
            continue
        summary = sanitize_audience_text(
            _field(item, "summary", _field(item, "evidence")),
            "",
        )
        protected, math_spans = protect_math_spans(summary)
        sentence = _bounded_sentence(protected, 48, "")
        if sentence:
            sentences.append(restore_math_spans(sentence, math_spans))
    return "".join(sentences)


def _compose(slide: Mapping[str, Any], kind: str, item: Mapping[str, Any], role: str) -> SpeakerNote:
    transition = _bounded_sentence(_transition(slide, role), 64, "现在转向本页的内容。")
    visual = _bounded_sentence(_visual(slide, role), 28, "请结合页面的视觉要素逐项说明。")
    takeaway = _spoken_takeaway(slide, item, role)
    explanation = _explanation(item, role)
    merged_explanation = _merged_role_explanation(slide)
    attribution = (
        "下面是我的讨论，不是论文作者的直接结论。请结合听众的问题讨论证据的含义和限制。"
        if role == "presenter-discussion"
        else "以下说明只依据论文原文，不把汇报者的推测当作作者结论。"
        if role == "title"
        else "下面将论文的做法与汇报者的解释分开说明。"
        if role in {"method-overview", "experiment"}
        else "下面先报告论文观察，再说明这项观察能够支持的结论边界。"
        if role in {"results-table", "comparison", "concept-or-metric", "analysis"}
        else "请把论文中的观察与自己的解释清楚区分。"
    )
    text = f"{transition}{visual}{explanation}{merged_explanation}{takeaway}{attribution}"
    if len(_CJK_RE.findall(text)) < 80:
        text += "请将这个结论同下一页的问题衔接，并在追问时回到页面中的来源。"
    return SpeakerNote(text, transition, visual, takeaway, (_source_ref(kind, item),))


def _evidence_for_safety(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Allow audit explanations in reviewed prose while still rejecting markers/CKPT text."""
    safe = dict(evidence)
    for key in ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results", "reviewed_semantic_slots", "audited_table_evidence", "paper_metadata_evidence"):
        values = evidence.get(key)
        if not isinstance(values, list):
            continue
        copied: list[Any] = []
        for value in values:
            if not isinstance(value, Mapping):
                copied.append(value)
                continue
            item = dict(value)
            # These fields are useful to semantic selection and QA, but are
            # machine-facing provenance rather than spoken evidence.  They may
            # legitimately contain words such as ``audit`` or ``hash``.
            for field in _INTERNAL_EVIDENCE_FIELDS:
                item.pop(field, None)
            for field in ("summary", "evidence"):
                if isinstance(item.get(field), str):
                    item[field] = sanitize_audience_text(item[field], item[field])
            copied.append(item)
        safe[key] = copied
    return safe


def _slides_for_safety(slides: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project slides to fields that can actually be rendered or spoken."""
    safe: list[dict[str, Any]] = []
    for slide in slides:
        copied = dict(slide) if isinstance(slide, Mapping) else {}
        for field in _INTERNAL_SLIDE_FIELDS:
            copied.pop(field, None)
        table = copied.get("table")
        if isinstance(table, Mapping):
            copied["table"] = {
                field: table[field]
                for field in _SPEAKER_VISIBLE_TABLE_FIELDS
                if field in table
            }
        safe.append(copied)
    return safe


def _assert_safe_input(slides: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any]) -> None:
    unsafe_evidence = {
        key: value for key, value in _evidence_for_safety(evidence).items()
        if key not in _ALLOWED_EVIDENCE_METADATA
    }
    if _unsafe(_slides_for_safety(slides)) or _unsafe(unsafe_evidence):
        raise ValueError("unsafe speaker-note input contains marker, audit, hash, or placeholder text")


def build_speaker_content(slides: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any], language: str = "zh-CN") -> list[SpeakerNote]:
    if language not in {"zh-CN", "en"}:
        raise ValueError("speaker-note writer supports language='zh-CN' or 'en'")
    _assert_safe_input(slides, evidence)
    result: list[SpeakerNote] = []
    previous_transition = ""
    transition_variants = (
        "接着把这一点落到页面证据上。",
        "继续看本页的具体比较。",
        "下面换一个角度展开。",
    )
    for slide in slides:
        role = _role(slide)
        kind, item = _bound_item(slide, evidence)
        unsupported_input = _visible_slide_numbers(slide) - _slide_allowed_numbers(slide, item)
        if unsupported_input:
            raise ValueError(f"unsupported numeric evidence in speaker-note input: {sorted(unsupported_input)!r}")
        note = _compose(slide, kind, item, role)
        role_selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
        if _field(role_selection, "status").casefold() == "missing":
            # A missing role may retain a safe delivery scaffold, but never a
            # source binding that could be mistaken for selected evidence.
            note = SpeakerNote(note.text, note.transition, note.visual_guidance, note.takeaway, ())
        if previous_transition and note.transition == previous_transition:
            replacement = next(
                variant for variant in transition_variants if variant != previous_transition
            )
            text = replacement + note.text[len(note.transition):] if note.text.startswith(note.transition) else note.text
            note = SpeakerNote(text, replacement, note.visual_guidance, note.takeaway, note.source_refs)
        unsupported = _delivery_numbers(note.text) - _slide_allowed_numbers(slide, item)
        if unsupported:
            raise ValueError(f"unsupported numeric evidence in speaker note: {sorted(unsupported)!r}")
        result.append(note)
        previous_transition = note.transition
    return result


def write_speaker_notes(slides: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any], language: str = "zh-CN") -> list[str]:
    return LegacySpeakerNotes([note.text for note in build_speaker_content(slides, evidence, language)], language)


def write_speaker_note(context: NotesContext) -> LegacySpeakerNotes:
    """Write the legacy projection from an explicit, reusable notes context."""
    if not context.slides:
        raise ValueError("NotesContext.slides must contain at least one slide")
    return write_speaker_notes(context.slides, context.evidence, context.language)


def apply_speaker_notes(slides: Sequence[Mapping[str, Any]], evidence: Mapping[str, Any], language: str = "zh-CN") -> list[dict[str, Any]]:
    notes = build_speaker_content(slides, evidence, language)
    applied: list[dict[str, Any]] = []
    for slide, note in zip(slides, notes):
        kind, item = _bound_item(slide, evidence)
        updated = dict(slide)
        role_selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
        missing_role = _field(role_selection, "status").casefold() == "missing"
        if missing_role:
            updated["speaker_evidence_binding"] = None
            content = note.as_dict()
            content["source_refs"] = []
            updated["speaker_content"] = content
        else:
            updated["speaker_evidence_binding"] = {
                "kind": kind, "evidence_id": _identity(kind, item), "source_page": item.get("source_page"),
                "section": _field(item, "section"), "locator": _field(item, "figure_table_equation", _field(item, "locator")),
                "summary": item.get("summary"), "evidence": item.get("evidence"), "rows": item.get("rows"),
            }
            if kind == "quantitative-coverage":
                updated["speaker_evidence_binding"].update({
                    "key_numbers": item.get("key_numbers", []),
                    "focus": item.get("speaker_focus", "关键定量结果"),
                    "technical_terms": item.get("technical_terms", []),
                })
            updated["speaker_content"] = note.as_dict()
        updated["speaker_notes"] = note.text
        applied.append(updated)
    findings = validate_speaker_notes(applied, applied, evidence)
    if findings:
        raise ValueError("generated speaker notes failed validation: " + "; ".join(finding.check for finding in findings))
    return applied


def _normalized(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", text, flags=re.UNICODE).casefold()


def _similarity(left: str, right: str) -> float:
    def grams(text: str) -> set[str]:
        normalized = _normalized(text)
        return {normalized[index:index + 4] for index in range(max(0, len(normalized) - 3))}
    a, b = grams(left), grams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _similarity_without_transition(left: str, right: str) -> float:
    """Compare note bodies so a varied handoff cannot hide a duplicate note."""
    def body(text: str) -> str:
        return text.split("。", 1)[1] if "。" in text else text

    return _similarity(body(left), body(right))


def _note_evidence_id(value: Any) -> str:
    """Return the single structured evidence identity bound to a note, if any."""
    if not isinstance(value, Mapping):
        return ""
    content = value.get("speaker_content")
    refs = content.get("source_refs") if isinstance(content, Mapping) else None
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
        return ""
    evidence_id = refs[0].get("evidence_id")
    return evidence_id.strip() if isinstance(evidence_id, str) else ""


def _evidence_comparable(left: str, right: str) -> bool:
    """Compare duplicate risk only when both notes refer to the same evidence.

    Distinct reviewed evidence can legitimately share a delivery scaffold.  An
    exact repeated projection is still caught separately by ``speaker-duplicate``.
    """
    return not left or not right or left == right


def _validate_content(content: Any, index: int, findings: list[Finding]) -> Mapping[str, Any] | None:
    if not isinstance(content, Mapping) or set(content) != _CONTENT_FIELDS:
        findings.append(Finding("speaker-content-schema", "P1", "speaker_content must contain exactly the five supported fields", index))
        return None
    return content


def _bound_numbers(slide: Mapping[str, Any], content: Any, evidence: Mapping[str, Any] | None) -> set[str] | None:
    binding = slide.get("speaker_evidence_binding") if isinstance(slide, Mapping) else None
    if not isinstance(binding, Mapping) or not isinstance(content, Mapping) or not isinstance(evidence, Mapping):
        return None
    refs = content.get("source_refs")
    if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], Mapping):
        return None
    if not isinstance(binding.get("kind"), str):
        return None
    if binding.get("kind") == "quantitative-coverage":
        ref = refs[0]
        if (
            ref.get("source_page") != binding.get("source_page")
            or _field(ref, "section") != _field(binding, "section")
            or _field(ref, "locator") != _field(binding, "locator")
        ):
            return None
        return _numbers({"summary": binding.get("summary"), "evidence": binding.get("evidence"), "rows": None, "key_numbers": binding.get("key_numbers")})
    if refs[0].get("evidence_id") != binding.get("evidence_id"):
        return None
    canonical = [(kind, item) for kind, item in _reviewed_items(evidence) if _identity(kind, item) == binding.get("evidence_id")]
    if len(canonical) != 1:
        return None
    return _numbers(canonical[0][1])


def _caption_is_copied(slide: Mapping[str, Any], text: str) -> bool:
    figure = slide.get("figure") if isinstance(slide, Mapping) else None
    if not isinstance(figure, Mapping):
        return False
    caption = _field(figure, "caption", _field(figure, "cite"))
    normalized_caption = _normalized(caption)
    return len(normalized_caption) >= 24 and normalized_caption in _normalized(text)


def _has_overlong_sentence(text: str) -> bool:
    sentences = [part.strip() for part in re.split(r"[。！？!?]", text) if part.strip()]
    return any(len(_CJK_RE.findall(sentence)) > 72 or len(sentence) > 220 for sentence in sentences)


def _has_mixed_delivery(text: str, allowed_terms: Sequence[str] = ()) -> bool:
    """Flag an untranslated English run while allowing grounded technical terms."""
    if not _CJK_RE.search(text):
        return False
    allowed = {
        token.casefold()
        for term in allowed_terms
        for token in _LATIN_WORD_RE.findall(str(term))
    }
    for sentence in re.split(r"[。！？!?]", text):
        run: list[str] = []
        previous_end: int | None = None
        for match in _LATIN_WORD_RE.finditer(sentence):
            gap = sentence[previous_end:match.start()] if previous_end is not None else ""
            if previous_end is not None and (re.search(r"[\u3400-\u9fff]", gap) or re.search(r"[^\s]", gap)):
                if sum(word not in allowed for word in run) >= 4:
                    return True
                run = []
            run.append(match.group(0).casefold())
            previous_end = match.end()
        if sum(word not in allowed for word in run) >= 4:
            return True
    return False


def _has_punctuated_fragment(text: str) -> bool:
    """Catch a standalone connective that is punctuated but cannot stand as a sentence."""
    for sentence in re.split(r"(?<=[。！？!?])", text):
        body = sentence.strip().rstrip("。！？!?").strip()
        if len(_CJK_RE.findall(body)) <= 4 and _DISCOURSE_CONNECTIVE_RE.fullmatch(body):
            return True
    return False


def _validate_fluency(
    slide: Mapping[str, Any], text: str, content: Mapping[str, Any] | None, role: str, index: int,
    previous_transition: str, findings: list[Finding],
) -> str:
    """Report deterministic delivery defects without changing schema/provenance rules."""
    transition = content.get("transition", "") if isinstance(content, Mapping) else ""
    if _RAW_ROLE_LABEL_RE.search(text):
        findings.append(Finding("speaker-fluency-role-label", "P1", "replace the internal role id with a natural Chinese delivery label", index))
    if role in _SHORT_ROLES or role == "presenter-discussion":
        return transition if isinstance(transition, str) else ""
    if _DOUBLE_PUNCTUATION_RE.search(text):
        findings.append(Finding("speaker-fluency-punctuation", "P1", "remove repeated terminal punctuation from the spoken note", index))
    if _FIGURE_SPACING_RE.search(text):
        findings.append(Finding("speaker-fluency-figure-spacing", "P1", "write figure references with a space, for example 'Figure 2'", index))
    if _MALFORMED_TRANSITION_RE.search(transition):
        findings.append(Finding("speaker-fluency-transition", "P1", "rewrite the transition as a complete, natural sentence", index))
    if _INTERNAL_DELIVERY_RE.search(text):
        findings.append(Finding("speaker-fluency-internal-process", "P1", "remove review or evidence-processing language from presenter delivery", index))
    if _caption_is_copied(slide, text):
        findings.append(Finding("speaker-fluency-caption-copy", "P1", "summarize the figure caption instead of reading it verbatim", index))
    allowed_terms = slide.get("speaker_technical_terms", ()) if isinstance(slide.get("speaker_technical_terms"), Sequence) else ()
    if _has_mixed_delivery(text, allowed_terms):
        findings.append(Finding("speaker-fluency-language-mix", "P1", "use one delivery language instead of an untranslated English sentence", index))
    if _has_overlong_sentence(text):
        findings.append(Finding("speaker-fluency-sentence-length", "P1", "split the overlong sentence into shorter spoken sentences", index))
    stripped = text.strip()
    if stripped and stripped[-1] not in "。！？!?":
        findings.append(Finding("speaker-fluency-fragment", "P1", "finish the spoken note with a complete sentence", index))
    if _has_punctuated_fragment(text):
        findings.append(Finding("speaker-fluency-fragment", "P1", "replace the standalone connective with a complete sentence", index))
    if _TRUNCATED_ENDING_RE.search(stripped):
        findings.append(Finding("speaker-fluency-truncated", "P1", "replace the clipped ending with a complete sentence", index))
    if isinstance(transition, str) and previous_transition and _normalized(transition) == _normalized(previous_transition):
        findings.append(Finding("speaker-fluency-template-overuse", "P1", "vary adjacent transitions so each slide has a distinct handoff", index))
    return transition if isinstance(transition, str) else ""


def validate_speaker_notes(slides: Sequence[Mapping[str, Any]], notes: Sequence[Any], evidence: Mapping[str, Any] | None = None) -> list[Finding]:
    """Validate notes; numeric claims require the canonical reviewed evidence mapping."""
    findings: list[Finding] = []
    if len(notes) != len(slides):
        findings.append(Finding("speaker-count", "P1", "speaker-note count must equal slide count"))
    structured = any(isinstance(value, Mapping) and "speaker_content" in value for value in notes)
    legacy_context = getattr(notes, "schema", None) == "legacy-v1"
    legacy_language = getattr(notes, "language", "zh-CN")
    previous = ""
    all_notes: list[str] = []
    all_note_roles: list[str] = []
    all_note_panel_groups: list[str] = []
    all_note_evidence_ids: list[str] = []
    previous_legacy = ""
    previous_transition = ""
    previous_role = ""
    previous_panel_group = ""
    previous_evidence_id = ""
    for index, (slide, value) in enumerate(zip(slides, notes), 1):
        try:
            role = _role(slide)
        except ValueError as exc:
            findings.append(Finding("speaker-role", "P1", str(exc), index))
            role = ""
        safe_slide = _slides_for_safety([slide])[0]
        safe_value = _slides_for_safety([value])[0] if isinstance(value, Mapping) else value
        if _unsafe(safe_slide) or _unsafe(safe_value):
            findings.append(Finding("speaker-unsafe-structure", "P1", "speaker slide or content exposes marker, audit, hash, or placeholder text", index))
        raw_content = value.get("speaker_content") if isinstance(value, Mapping) else None
        content = _validate_content(raw_content, index, findings) if structured else None
        if isinstance(raw_content, Mapping):
            refs = raw_content.get("source_refs")
            role_selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
            missing_role = _field(role_selection, "status").casefold() == "missing"
            invalid_refs = (
                not isinstance(refs, list)
                or any(not isinstance(ref, Mapping) or not isinstance(ref.get("source_page"), int) or ref["source_page"] < 1 or not _field(ref, "section") or not _field(ref, "locator") or not _field(ref, "evidence_id") for ref in refs)
            )
            if (missing_role and refs != []) or (not missing_role and (not refs or invalid_refs)):
                findings.append(Finding("speaker-source-refs", "P1", "speaker content requires meaningful structured source_refs", index))
        if structured and content is None:
            continue
        if content is None:
            if not legacy_context and (not isinstance(value, Mapping) or value.get("speaker_notes_schema") != "legacy-v1"):
                findings.append(Finding("speaker-content-required", "P1", "legacy notes require explicit legacy-v1 versioning", index))
            text = value.get("speaker_notes", "") if isinstance(value, Mapping) else value
        else:
            text = content.get("text")
            if value.get("speaker_notes") != text:
                findings.append(Finding("speaker-projection", "P1", "speaker_notes must equal speaker_content.text", index))
        if not isinstance(text, str) or not text.strip():
            findings.append(Finding("speaker-empty", "P1", "speaker note must not be empty", index))
            continue
        cjk = len(_CJK_RE.findall(text))
        if legacy_language != "en" and role not in _SHORT_ROLES and not slide.get("quantitative_kinds") and not 80 <= cjk <= 160:
            findings.append(Finding("speaker-length", "P1", f"ordinary Chinese note has {cjk} characters; expected 80-160", index))
        if content is not None:
            if any(not isinstance(content.get(key), str) or not content[key] or content[key] not in text for key in ("transition", "visual_guidance", "takeaway")):
                findings.append(Finding("speaker-coverage", "P1", "speaker content guidance must appear in the spoken text", index))
            allowed_numbers = _bound_numbers(slide, content, evidence)
            if _delivery_numbers(text) - (allowed_numbers or set()):
                findings.append(Finding("speaker-numeric-provenance", "P1", "speaker-note numbers must come from its exact bound reviewed evidence", index))
        if role == "presenter-discussion" and "下面是我的讨论，不是论文作者的直接结论。" not in text:
            findings.append(Finding("presenter-discussion-disclaimer", "P1", "presenter discussion must disclose attribution", index))
        previous_transition = _validate_fluency(slide, text, content, role, index, previous_transition, findings)
        if _normalized(previous) == _normalized(text):
            findings.append(Finding("speaker-duplicate", "P1", "speaker notes must not repeat", index))
        legacy = value.get("speaker_notes") if isinstance(value, Mapping) else text
        if isinstance(legacy, str) and _normalized(legacy) == _normalized(previous_legacy):
            findings.append(Finding("speaker-duplicate", "P1", "legacy speaker-note projection must not repeat", index))
        panel_group = str(slide.get("quantitative_panel_group", ""))
        same_panel_as_previous = bool(panel_group and panel_group == previous_panel_group)
        evidence_id = _note_evidence_id(value)
        if (
            (not same_panel_as_previous and _evidence_comparable(previous_evidence_id, evidence_id) and _similarity(previous, text) >= 0.90)
            or (
                not same_panel_as_previous
                and role == previous_role
                and _evidence_comparable(previous_evidence_id, evidence_id)
                and _similarity_without_transition(previous, text) >= 0.86
            )
            or any(
                not (
                    panel_group
                    and panel_group == existing_panel_group
                )
                and _evidence_comparable(existing_evidence_id, evidence_id)
                and (
                    _similarity(existing, text) >= 0.94
                    or (
                        existing_role == role
                        and _similarity_without_transition(existing, text) >= 0.86
                    )
                )
                for existing, existing_role, existing_panel_group, existing_evidence_id in zip(
                    all_notes, all_note_roles, all_note_panel_groups, all_note_evidence_ids
                )
            )
        ):
            findings.append(Finding("speaker-near-duplicate", "P1", "speaker notes are mechanically similar", index))
        previous, previous_legacy, all_notes = text, legacy if isinstance(legacy, str) else "", [*all_notes, text]
        previous_role = role
        all_note_roles.append(role)
        previous_panel_group = panel_group
        all_note_panel_groups.append(panel_group)
        previous_evidence_id = evidence_id
        all_note_evidence_ids.append(evidence_id)
    return findings
