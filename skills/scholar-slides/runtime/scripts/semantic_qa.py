"""Deterministic, source-bound semantic checks for a pending CKPT-2 deck.

This module intentionally operates on the structured deck, digest, asset graph and
speaker-note records.  It does not inspect pixels, call an LLM, or contain paper-
specific identifiers.  A semantic report is therefore reproducible and can be
revalidated from the three canonical input hashes before a review is reused.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import json
import re
from typing import Any, Mapping, Sequence

from asset_semantics import asset_policy_for_role
from audience_text import AUDIENCE_INTERNAL_PROCESS_RE, PDF_HYPHENATION_RE, PROVENANCE_LEAK_RE
from deck_types import DeckTypeError, get_deck_contract
from notes_writer import validate_speaker_notes
from quantitative_coverage import missing_coverage_tokens, visible_text
from semantic_evidence import (
    ROLE_COMPATIBLE_TYPES,
    classify_evidence,
    evidence_section,
    is_section_mismatch,
    role_compatibility,
    reviewed_semantic_slot_records,
)


SCHEMA_VERSION = 1
KIND = "scholar-slides-semantic-qa"

_INTERNAL_RE = re.compile(
    r"(?:\b(?:ckpt(?:[- ]?\d+)?|checkpoint|audit(?:ed|ing)?|ledger|sha[- ]?256|hash(?:ed|ing)?|"
    r"artifact[- ]?bundle|pending[_ -]?human[_ -]?confirmation|resolved[_ -]?with[_ -]?audit|"
    r"evidence[_ -]?binding|internal[_ -]?process|system[_ -]?generated)\b|"
    r"(?:\u5df2\u5ba1\u9605|\u5ba1\u9605(?:\u5b8c\u6210|\u6d41\u7a0b)?|\u8bc1\u636e\u7ed1\u5b9a|\u6765\u6e90\u7ed1\u5b9a|\u5185\u90e8\u6d41\u7a0b|\u7cfb\u7edf\u751f\u6210|\u81ea\u52a8\u751f\u6210|\u8d28\u91cf\u68c0\u67e5|\u5ba1\u6838\u8bb0\u5f55|\u672c\u9875\u4ec5\u5c55\u793a\u5df2\u5165\u5e93\u7684\u8bba\u6587\u8bc1\u636e|\u9875\u9762\u7ed1\u5b9a|\u5ba1\u9605\u8303\u56f4|\u8bc1\u636e\u5e93))|"
    + PROVENANCE_LEAK_RE.pattern,
    re.IGNORECASE,
)
_MISSING_RE = re.compile(r"\[(?:MISSING|UNVERIFIED)(?::[^\]]*)?\]", re.IGNORECASE)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)?")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[/\\]{1,2}|file://)")
_PAGE_LOCATOR_RE = re.compile(r"\b(?:p(?:age)?\.?\s*)(\d+)(?:\s*(?:[-–—]|to)\s*(\d+))?\b", re.IGNORECASE)
_ASSET_LOCATOR_RE = re.compile(r"\b(figures?|fig\.?|tables?|tab\.?|equations?|eq\.?)\s*(\d+)(?:\s*(?:[-–—]|to)\s*(\d+))?\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"\b(?:sha(?:[- ]?256)?|hash)\s*[:=\-]?\s*([0-9a-f]{64})\b", re.IGNORECASE)
_TERMINAL_RE = re.compile(r"(?:[.!?。！？；;]|\.{3}|…)$")
_CONNECTIVE_RE = re.compile(r"(?:因此|所以|但是|然而|同时|此外|随后|然后|并且|而且|because|therefore|however)", re.IGNORECASE)

_SPEAKER_TO_SEMANTIC = {
    "title": "title",
    "background": "background",
    "research-question": "problem",
    "method-overview": "method",
    "experiment": "process",
    "concept-or-metric": "metrics",
    "comparison": "contribution",
    "results-table": "results",
    "analysis": "analysis",
    "conclusion": "conclusion",
    "presenter-discussion": "discussion",
    "references": "sources",
}

_LAYOUTS_BY_ROLE = {
    "title": {"paper-title"},
    "background": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "problem": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "question": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "method": {"two-column", "evidence-flow", "assertion-evidence", "bullets"},
    "process": {"two-column", "evidence-flow", "assertion-evidence", "bullets"},
    "metrics": {"bullets", "assertion-evidence", "two-column", "equation", "results-table"},
    "contribution": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "evidence": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "results": {"bullets", "assertion-evidence", "two-column", "evidence-flow", "results-table"},
    "analysis": {"bullets", "assertion-evidence", "two-column", "critique-concerns", "evidence-flow", "results-table"},
    "conclusion": {"bullets", "assertion-evidence", "two-column", "evidence-flow"},
    "discussion": {"discussion-questions", "bullets", "two-column"},
    "sources": {"references"},
}


def _text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _strings(child)


def _norm(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _is_semantically_empty(value: Any) -> bool:
    """Return whether a metadata value carries no semantic content."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Mapping):
        return all(_is_semantically_empty(child) for child in value.values())
    if isinstance(value, (list, tuple, set)):
        return all(_is_semantically_empty(child) for child in value)
    return False


def _has_semantic_value(value: Any) -> bool:
    """Return whether a metadata field is structurally present with semantic content."""
    return not _is_semantically_empty(value)


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[\w\u3400-\u9fff]+", _norm(value), re.UNICODE))


def _locator_signature(value: Any) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[str, int, int], ...], str] | None:
    """Parse page and Figure/Table/Equation locators without token-subset matches."""
    text = _text(value)
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text.casefold().replace("–", "-").replace("—", "-")).strip()
    pages: list[tuple[int, int]] = []
    for match in _PAGE_LOCATOR_RE.finditer(normalized):
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        pages.append((start, end))
    assets: list[tuple[str, int, int]] = []
    for match in _ASSET_LOCATOR_RE.finditer(normalized):
        raw_kind, start, end = match.group(1), int(match.group(2)), int(match.group(3) or match.group(2))
        if raw_kind.startswith(("fig",)):
            kind = "figure"
        elif raw_kind.startswith(("tab",)):
            kind = "table"
        else:
            kind = "equation"
        assets.append((kind, start, end))
    return tuple(sorted(set(pages))), tuple(sorted(set(assets))), normalized


def _locator_relation(left: Any, right: Any, *, allow_range_member: bool = False) -> bool:
    left_sig, right_sig = _locator_signature(left), _locator_signature(right)
    if left_sig is None or right_sig is None:
        return False
    if left_sig[0] != right_sig[0]:
        return False
    if left_sig[1] == right_sig[1] and (left_sig[1] or left_sig[0]):
        return True
    if allow_range_member and left_sig[1] and right_sig[1]:
        def contains(container: tuple[str, int, int], member: tuple[str, int, int]) -> bool:
            return container[0] == member[0] and container[1] <= member[1] and member[2] <= container[2]

        # ``right`` is the reviewed locator in all asset-alignment call sites;
        # only a selected member contained by that reviewed range is licensed.
        return all(any(contains(right_item, left_item) for right_item in right_sig[1]) for left_item in left_sig[1])
    if left_sig[0] or right_sig[0] or left_sig[1] or right_sig[1]:
        return False
    return left_sig[2] == right_sig[2]


def _same_locator(left: Any, right: Any) -> bool:
    return _locator_relation(left, right)


def _source_matches(display: Any, expected: Any) -> bool:
    """Require exact parsed locator semantics (page and asset number/range)."""
    return _locator_relation(display, expected)


def _source_contains(display: Any, expected: Any) -> bool:
    """Allow a public source_ref to list multiple claim evidence records."""
    if _source_matches(display, expected):
        return True
    display_signature = _locator_signature(display)
    expected_signature = _locator_signature(expected)
    if display_signature is None or expected_signature is None:
        return False
    display_pages, display_assets, _ = display_signature
    expected_pages, expected_assets, _ = expected_signature
    return (
        all(page in display_pages for page in expected_pages)
        and all(asset in display_assets for asset in expected_assets)
        and bool(expected_pages or expected_assets)
    )


def _asset_locator_matches(display: Any, expected: Any) -> bool:
    """Allow a selected member when either reviewed side contains it as a range."""
    return _locator_relation(display, expected, allow_range_member=True) or _locator_relation(expected, display, allow_range_member=True)


def _same_provenance(left: Any, right: Any) -> bool:
    """Compare metadata while tolerating list-versus-display-string formatting."""
    if isinstance(left, list):
        left = ", ".join(_text(item) for item in left)
    if isinstance(right, list):
        right = ", ".join(_text(item) for item in right)
    left_norm = re.sub(r"[^\w\u3400-\u9fff]+", "", _norm(left))
    right_norm = re.sub(r"[^\w\u3400-\u9fff]+", "", _norm(right))
    return bool(left_norm) and left_norm == right_norm


def _contains_provenance(display: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return all(_contains_provenance(display, item) for item in expected if _text(item))
    display_norm = re.sub(r"[^\w\u3400-\u9fff]+", "", _norm(display))
    expected_norm = re.sub(r"[^\w\u3400-\u9fff]+", "", _norm(expected))
    return bool(expected_norm) and expected_norm in display_norm


def _sanitize(value: Any) -> Any:
    """Keep diagnostics portable and prevent host paths from entering the report."""
    if isinstance(value, Mapping):
        return {str(key): _sanitize(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if _ABSOLUTE_PATH_RE.match(value) or _ABSOLUTE_PATH_RE.match(normalized):
            return "<absolute-path>"
        return normalized
    return value


def _issue(
    *, code: str, severity: str, slide_index: int, pointer: str, message: str,
    evidence: Mapping[str, Any] | None = None, action: str = "Repair the semantic deck contract and rerun review.",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "slide_index": slide_index,
        "json_pointer": pointer,
        "message": message,
        "evidence": _sanitize({str(key): value for key, value in sorted((evidence or {}).items(), key=lambda item: str(item[0]))}),
        "suggested_action": action,
    }


def _role(slide: Mapping[str, Any]) -> str:
    semantic = slide.get("semantic_role")
    if isinstance(semantic, str) and semantic.strip():
        return semantic.strip().casefold()
    speaker = slide.get("role")
    if isinstance(speaker, str):
        return _SPEAKER_TO_SEMANTIC.get(speaker.casefold(), speaker.casefold())
    layout = _text(slide.get("layout"))
    return "title" if layout == "paper-title" else "standard"


def _contract_present(deck: Mapping[str, Any], digest: Mapping[str, Any]) -> bool:
    meta = deck.get("meta") if isinstance(deck.get("meta"), Mapping) else {}
    slides = deck.get("slides") if isinstance(deck.get("slides"), list) else []
    return (
        meta.get("speaker_notes_schema") == "speaker-content-v1"
        or bool(digest.get("paper_metadata"))
        or any(isinstance(slide, Mapping) and any(key in slide for key in ("semantic_role", "asset_policy", "asset_selection")) for slide in slides)
    )


def validate_deck_type_metadata(meta: Mapping[str, Any]) -> list[str]:
    """Return deterministic public errors for deck-type metadata contracts."""
    try:
        contract = get_deck_contract(meta.get("deck_type"))
    except DeckTypeError:
        return ["meta.deck_type is not supported"]
    declared = meta.get("deck_type_contract")
    expected_contract = json.loads(json.dumps(contract.as_dict()))
    if declared is not None and (not isinstance(declared, Mapping) or dict(declared) != expected_contract):
        return ["meta.deck_type_contract does not match meta.deck_type"]
    options = meta.get("options")
    if options is not None and (
        not isinstance(options, Mapping) or options.get("deck_type") != contract.deck_type.value
    ):
        return ["meta.options.deck_type does not match meta.deck_type"]
    return []


def _deck_type_checks(meta: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool) -> None:
    if not strict:
        return
    for message in validate_deck_type_metadata(meta):
        issues.append(_issue(
            code="semantic-deck-type-contract", severity="error", slide_index=0,
            pointer="/meta/deck_type", message=message,
            action="Use journal-club, conference, or thesis-defense and bind matching resolved options/contract metadata.",
        ))


def _asset_records(digest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    for key in ("figures", "tables", "assets", "reviewed_assets", "audited_table_evidence"):
        values = digest.get(key)
        if isinstance(values, Mapping):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            identifier = value.get("id", value.get("asset_id"))
            if isinstance(identifier, str) and identifier.strip():
                records[identifier] = value
    return records


_REVIEWED_EVIDENCE_KEYS = ("reviewed_claims", "reviewed_contributions", "reviewed_experimental_results")


def _reviewed_evidence_records(digest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    records.extend(reviewed_semantic_slot_records(digest))
    for key in _REVIEWED_EVIDENCE_KEYS:
        values = digest.get(key)
        if isinstance(values, Mapping):
            values = [values]
        if isinstance(values, list):
            records.extend(value for value in values if isinstance(value, Mapping))
    return records


def _reviewed_locator(record: Mapping[str, Any]) -> str:
    return _text(record.get("figure_table_equation")) or _text(record.get("locator"))


def _locator_values(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [_text(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_text(item) for item in value if _text(item)]
    return []


def _slide_evidence_locators(slide: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(_locator_values(slide.get("evidence_locators")))
    binding = slide.get("speaker_evidence_binding") if isinstance(slide.get("speaker_evidence_binding"), Mapping) else {}
    values.extend(_locator_values(binding.get("locator")))
    selection = slide.get("asset_selection") if isinstance(slide.get("asset_selection"), Mapping) else {}
    values.extend(_locator_values(selection.get("evidence_locator")))
    return list(dict.fromkeys(values))


def _matches_any_locator(value: Any, expected: Sequence[str], *, allow_range_member: bool = False) -> bool:
    matcher = _asset_locator_matches if allow_range_member else _source_matches
    return bool(_text(value)) and any(matcher(value, candidate) for candidate in expected if _text(candidate))


def _graph_asset_paths(asset_graph: Mapping[str, Any]) -> list[tuple[str, str]]:
    nodes = asset_graph.get("nodes") if isinstance(asset_graph, Mapping) else []
    result: list[tuple[str, str]] = []
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        path, kind = _text(node.get("path")), _text(node.get("kind"))
        if path:
            result.append((path.replace("\\", "/"), kind))
    return sorted(result)


def _graph_visible_asset_nodes(asset_graph: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return graph-declared visible assets with their identity metadata intact.

    ``_graph_asset_paths`` predates source-pointer validation and intentionally
    exposes only path/kind pairs.  Semantic identity checks need the full node so
    a visible renderer path can be compared with the selected digest candidate
    and with the graph pointer for the exact slide field.
    """
    nodes = asset_graph.get("nodes") if isinstance(asset_graph, Mapping) else []
    if not isinstance(nodes, list):
        return []
    return [
        node
        for node in nodes
        if isinstance(node, Mapping)
        and _text(node.get("kind")) == "visible_asset"
        and _text(node.get("path"))
    ]


def _portable_path(value: Any) -> str:
    """Normalize a renderer/graph path for exact identity comparisons."""
    return _text(value).replace("\\", "/")


def _path_stem(value: Any) -> str:
    """Return a portable filename stem without treating directories as identity."""
    basename = _portable_path(value).rstrip("/").rsplit("/", 1)[-1]
    return basename.rsplit(".", 1)[0] if "." in basename else basename


def _declared_asset_paths(record: Mapping[str, Any] | None) -> set[str]:
    """Collect optional digest-native path declarations without inventing paths."""
    if not isinstance(record, Mapping):
        return set()
    paths: set[str] = set()
    for key in ("src", "path", "asset_path", "asset_src", "rendered_path"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            paths.add(_portable_path(value))
    return paths


_TITLE_HERO_ROLE_TOKENS = frozenset({
    "title", "title-page", "title_page", "overview", "framework", "teaser", "cover",
})


def _title_hero_compatible(record: Mapping[str, Any] | None, graph_node: Mapping[str, Any] | None = None) -> bool:
    """Require explicit title-compatible metadata before promoting an asset to a hero."""
    for value in (record, graph_node):
        if not isinstance(value, Mapping):
            continue
        for key in ("title_compatible", "hero_compatible", "title_page_compatible"):
            if value.get(key) is True:
                return True
        role_values: list[Any] = []
        for key in ("roles", "role", "semantic_role", "asset_role", "purpose", "asset_purpose", "usage"):
            candidate = value.get(key)
            if isinstance(candidate, (list, tuple, set)):
                role_values.extend(candidate)
            else:
                role_values.append(candidate)
        for role in role_values:
            normalized = re.sub(r"[_ ]+", "-", _text(role).casefold())
            if normalized in _TITLE_HERO_ROLE_TOKENS:
                return True
    return False


def _visible_slide_blocks(slide: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Return renderer-visible text with stable pointers and presentation roles."""
    blocks: list[tuple[str, str, str]] = []

    def add(pointer: str, value: Any, kind: str) -> None:
        for child in _strings(value):
            text = _text(child)
            if text:
                blocks.append((pointer, text, kind))

    layout = _text(slide.get("layout"))
    if layout == "paper-title":
        for field in ("title", "authors", "affiliation", "venue", "presenter"):
            add(f"/{field}", slide.get(field), "title" if field == "title" else "metadata")
    elif layout == "section":
        add("/num", slide.get("num"), "metadata")
        add("/title", slide.get("title"), "title")
    else:
        add("/eyebrow", slide.get("eyebrow"), "metadata")
        if _text(slide.get("action_title")):
            add("/action_title", slide.get("action_title"), "title")
        else:
            add("/title", slide.get("title"), "title")

    body_fields_by_layout = {
        "outline-agenda": ("items",),
        "assertion-evidence": ("annotation",),
        "equation": ("equations", "note"),
        "results-table": ("points",),
        "two-column": ("points", "text", "points2"),
        "critique-concerns": ("points",),
        "discussion-questions": ("questions",),
        "bullets": ("points",),
        "references": ("entries",),
    }
    for field in body_fields_by_layout.get(layout, ()):
        # A two-column renderer chooses a figure instead of points2.
        if layout == "two-column" and field == "points2" and isinstance(slide.get("figure"), Mapping):
            continue
        # It likewise chooses points instead of free text.
        if layout == "two-column" and field == "text" and _meaningful_content(slide.get("points")):
            continue
        add(f"/{field}", slide.get(field), "body")

    if layout == "results-table":
        table = slide.get("table") if isinstance(slide.get("table"), Mapping) else {}
        add("/table/caption", table.get("caption"), "caption")
        add("/table/footnote", table.get("footnote"), "body")
        add("/table/columns", table.get("columns"), "table")
        add("/table/rows", table.get("rows"), "table")
    if layout == "evidence-flow":
        diagram = slide.get("native_diagram") if isinstance(slide.get("native_diagram"), Mapping) else {}
        nodes = diagram.get("nodes") if isinstance(diagram.get("nodes"), list) else []
        for node_index, node in enumerate(nodes):
            if isinstance(node, Mapping):
                add(f"/native_diagram/nodes/{node_index}/label", node.get("label"), "body")

    figure_visible = layout in {"paper-title", "assertion-evidence", "two-column"}
    figure = slide.get("figure") if figure_visible and isinstance(slide.get("figure"), Mapping) else None
    presentation = slide.get("provenance_display") if isinstance(slide.get("provenance_display"), Mapping) else {}
    entries = presentation.get("entries") if isinstance(presentation.get("entries"), list) else []
    if figure is not None:
        add("/figure/caption", figure.get("caption"), "caption")
        if not entries:
            add("/figure/cite", figure.get("cite"), "provenance")

    source_visible = layout in {
        "assertion-evidence", "equation", "results-table", "two-column",
        "evidence-flow", "critique-concerns", "bullets",
    }
    if source_visible and entries:
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            add(f"/provenance_display/entries/{entry_index}/label", entry.get("label"), "provenance")
            add(f"/provenance_display/entries/{entry_index}/source_ref", entry.get("source_ref"), "provenance")
    elif source_visible:
        add("/source_ref", slide.get("source_ref"), "provenance")
    return blocks


def _visible_slide_strings(slide: Mapping[str, Any]) -> list[str]:
    # Speaker notes and semantic metadata are intentionally excluded: only text
    # projected by a registered layout participates in audience-facing QA.
    return [text for _, text, _ in _visible_slide_blocks(slide)]


def _audience_norm(value: Any) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", _norm(value), flags=re.UNICODE)


def _raw_source_paragraph(value: str) -> bool:
    latin_words = _LATIN_WORD_RE.findall(value)
    sentence_count = len([part for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()])
    letters = len(re.findall(r"[A-Za-z\u3400-\u9fff]", value))
    latin_letters = len(re.findall(r"[A-Za-z]", value))
    return (
        len(value) >= 240
        and len(latin_words) >= 35
        and sentence_count >= 2
        and letters > 0
        and latin_letters / letters >= 0.7
    )


def _audience_projection_checks(
    slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool,
) -> None:
    """Block viewer-facing duplication, raw prose, extraction noise, and process jargon."""
    del strict
    for index, slide in enumerate(slides, 1):
        pointer = f"/slides/{index - 1}"
        blocks = _visible_slide_blocks(slide)
        semantic_blocks = [
            (path, text, kind)
            for path, text, kind in blocks
            if kind in {"title", "body"}
        ]
        by_norm: defaultdict[str, list[str]] = defaultdict(list)
        for path, text, kind in semantic_blocks:
            if kind != "body":
                continue
            normalized = _audience_norm(text)
            if len(normalized) >= 8:
                by_norm[normalized].append(path)
        for normalized, paths in sorted(by_norm.items()):
            if len(paths) > 1:
                issues.append(_issue(
                    code="audience-visible-duplicate", severity="error", slide_index=index,
                    pointer=pointer, message="the same audience claim appears in multiple visible blocks",
                    evidence={"normalized_text": normalized[:120], "pointers": paths},
                    action="Keep one visible occurrence and preserve the remaining role bindings as metadata or notes.",
                ))
        titles = [(path, text) for path, text, kind in semantic_blocks if kind == "title"]
        bodies = [(path, text) for path, text, kind in semantic_blocks if kind == "body"]
        for title_path, title_text in titles:
            title_norm = _audience_norm(title_text)
            if len(title_norm) < 12:
                continue
            duplicate = next((
                (body_path, body_text)
                for body_path, body_text in bodies
                if len(_audience_norm(body_text)) >= 12
                and SequenceMatcher(None, title_norm, _audience_norm(body_text)).ratio() >= 0.88
            ), None)
            if duplicate is not None:
                issues.append(_issue(
                    code="audience-title-body-duplication", severity="error", slide_index=index,
                    pointer=f"{pointer}{title_path}", message="the action title is repeated by a visible body block",
                    evidence={"title": title_text, "body": duplicate[1], "body_pointer": duplicate[0]},
                    action="Use a concise audience title and keep the detailed claim in the body only.",
                ))
                break
        archetypes = {str(value).casefold() for value in (slide.get("archetypes") or [])}
        quote_exempt = bool(archetypes & {"quote", "excerpt", "textual-evidence"}) or _role(slide) in {"quote", "excerpt"}
        for path, value, kind in blocks:
            if kind not in {"title", "body"}:
                continue
            if PDF_HYPHENATION_RE.search(value):
                issues.append(_issue(
                    code="audience-pdf-hyphenation", severity="error", slide_index=index,
                    pointer=f"{pointer}{path}", message="visible text contains a PDF extraction word break",
                    evidence={"text": value[:180]}, action="Repair extraction-only word breaks before audience projection.",
                ))
            if not quote_exempt and _raw_source_paragraph(value):
                issues.append(_issue(
                    code="audience-raw-evidence-projection", severity="error", slide_index=index,
                    pointer=f"{pointer}{path}", message="a long raw source paragraph is projected as audience text",
                    evidence={"characters": len(value), "latin_words": len(_LATIN_WORD_RE.findall(value))},
                    action="Project a concise reviewed claim or process stage and keep raw evidence in review metadata or notes.",
                ))
        for path, value, _ in blocks:
            match = AUDIENCE_INTERNAL_PROCESS_RE.search(value)
            if match:
                issues.append(_issue(
                    code="audience-internal-process-leak", severity="error", slide_index=index,
                    pointer=f"{pointer}{path}", message="visible slide text exposes internal evidence-governance language",
                    evidence={"matched": match.group(0)},
                    action="Replace internal audit/process wording with a source-grounded result, comparison, or neutral audience label.",
                ))


def _title_checks(
    deck: Mapping[str, Any],
    digest: Mapping[str, Any],
    issues: list[dict[str, Any]],
    strict: bool,
    confirmed_metadata: Mapping[str, Any] | None = None,
) -> None:
    slides = deck.get("slides") if isinstance(deck.get("slides"), list) else []
    title_positions = [index for index, slide in enumerate(slides) if isinstance(slide, Mapping) and slide.get("layout") == "paper-title"]
    if not title_positions:
        issues.append(_issue(code="semantic-title-layout", severity="error", slide_index=1, pointer="/slides", message="deck must contain one paper-title slide", action="Add exactly one paper-title title slide as the first slide."))
        return
    if title_positions[0] != 0 or len(title_positions) != 1:
        issues.append(_issue(code="semantic-title-layout", severity="error", slide_index=title_positions[0] + 1, pointer=f"/slides/{title_positions[0]}/layout", message="paper-title must be the unique first slide", evidence={"title_positions": [value + 1 for value in title_positions]}, action="Use a unique paper-title slide at position one."))
    title = slides[title_positions[0]]
    assert isinstance(title, Mapping)
    meta = deck.get("meta") if isinstance(deck.get("meta"), Mapping) else {}
    required = ("title", "authors", "venue", "presenter")
    missing = [field for field in required if not _text(title.get(field)) or _MISSING_RE.search(_text(title.get(field)))]
    if not _text(meta.get("deck_type")):
        missing.append("meta.deck_type")
    if missing:
        issues.append(_issue(code="semantic-title-metadata", severity="error" if strict else "warning", slide_index=title_positions[0] + 1, pointer=f"/slides/{title_positions[0]}", message="title slide is missing verified presentation metadata", evidence={"missing": sorted(missing)}, action="Populate verified title, authors, venue/version, presenter, and deck type metadata."))
    if _text(meta.get("title")) and _text(meta.get("title")) != _text(title.get("title")):
        issues.append(_issue(code="semantic-title-mismatch", severity="error", slide_index=title_positions[0] + 1, pointer="/meta/title", message="deck metadata title differs from the visible title", evidence={"meta_title": _text(meta.get("title")), "visible_title": _text(title.get("title"))}))
    presenter = _text(title.get("presenter"))
    deck_type = _text(meta.get("deck_type"))
    presenter_identity = re.split(r"\s*[·|]\s*", presenter, maxsplit=1)[0]
    presenter_identity = re.sub(r"^汇报人\s*[:：]\s*", "", presenter_identity).strip()
    if strict and presenter_identity and deck_type and _same_provenance(presenter_identity, deck_type):
        issues.append(_issue(
            code="semantic-title-metadata",
            severity="error",
            slide_index=title_positions[0] + 1,
            pointer=f"/slides/{title_positions[0]}/presenter",
            message="title presenter must identify a person or presenting group, not only the deck type",
            evidence={"presenter": presenter, "deck_type": deck_type},
            action="Use the configured presenter or the confirmed CKPT-1 confirmer, while keeping deck type in its own metadata field.",
        ))
    narrative = meta.get("narrative") if isinstance(meta.get("narrative"), Mapping) else {}
    narrative_slides = narrative.get("slides") if isinstance(narrative.get("slides"), list) else []
    if strict and narrative_slides and isinstance(narrative_slides[0], Mapping):
        ownership = _text(narrative_slides[0].get("ownership")).casefold()
        if ownership == "presenter_discussion":
            issues.append(_issue(
                code="semantic-title-ownership",
                severity="error",
                slide_index=title_positions[0] + 1,
                pointer="/meta/narrative/slides/0/ownership",
                message="title narrative ownership must remain neutral paper metadata, not presenter discussion",
                evidence={"ownership": ownership},
                action="Mark the title narrative as paper metadata or author metadata; reserve presenter discussion for interpretation slides.",
            ))
    metadata = digest.get("paper_metadata") if isinstance(digest.get("paper_metadata"), Mapping) else {}
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
    title_record = evidence.get("title") if isinstance(evidence.get("title"), Mapping) else {}
    title_locations = title_record.get("locations") if isinstance(title_record.get("locations"), list) else []
    expected_title_locator = next((value for value in title_locations if isinstance(value, str) and value.strip()), "")
    if strict and expected_title_locator and narrative_slides:
        narrative_title = narrative_slides[0]
        if isinstance(narrative_title, Mapping):
            title_locators = _locator_values(narrative_title.get("evidence_locators"))
            title_selection = narrative_title.get("asset_selection") if isinstance(narrative_title.get("asset_selection"), Mapping) else {}
            title_locators.extend(_locator_values(title_selection.get("evidence_locator")))
            title_binding = narrative_title.get("speaker_evidence_binding") if isinstance(narrative_title.get("speaker_evidence_binding"), Mapping) else {}
            title_locators.extend(_locator_values(title_binding.get("locator")))
            if any(not _source_matches(locator, expected_title_locator) for locator in title_locators):
                issues.append(_issue(
                    code="semantic-title-source-mismatch",
                    severity="error",
                    slide_index=title_positions[0] + 1,
                    pointer="/meta/narrative/slides/0",
                    message="title narrative fields do not share the paper-metadata locator",
                    evidence={"expected_locator": expected_title_locator, "observed_locators": title_locators},
                    action="Bind title evidence, source_ref, speaker binding, and asset selection to paper metadata rather than a claim or result asset.",
                ))
    resolved_title: str | None = None
    resolved_authors: list[str] | None = None
    if (
        isinstance(confirmed_metadata, Mapping)
        and isinstance(confirmed_metadata.get("title"), str)
        and confirmed_metadata["title"].strip()
        and isinstance(confirmed_metadata.get("authors"), list)
        and confirmed_metadata["authors"]
        and all(isinstance(author, str) and author.strip() for author in confirmed_metadata["authors"])
    ):
        resolved_title = confirmed_metadata["title"].strip()
        resolved_authors = [author.strip() for author in confirmed_metadata["authors"]]
    if resolved_title is None or resolved_authors is None:
        for field in ("title", "authors"):
            status = evidence.get(field, {}).get("status") if isinstance(evidence.get(field), Mapping) else None
            if strict and status != "VERIFIED":
                issues.append(_issue(code="semantic-title-verification", severity="error", slide_index=title_positions[0] + 1, pointer=f"/paper_metadata/evidence/{field}", message=f"{field} metadata is not marked VERIFIED in the digest", evidence={"status": status}, action="Resolve the source metadata before generating a reviewed deck."))
    for field in ("version", "venue"):
        record = evidence.get(field)
        if strict and _has_semantic_value(metadata.get(field)) and (not isinstance(record, Mapping) or record.get("status") != "VERIFIED"):
            issues.append(_issue(code="semantic-title-verification", severity="error", slide_index=title_positions[0] + 1, pointer=f"/paper_metadata/evidence/{field}", message=f"{field} metadata is not verified", evidence={"status": record.get("status") if isinstance(record, Mapping) else None}, action="Resolve the source metadata before generating a reviewed deck."))
    if strict:
        expected_title = resolved_title if resolved_title is not None else metadata.get("title")
        if resolved_authors is not None:
            expected_authors = ", ".join(resolved_authors)
        elif isinstance(metadata.get("authors"), list):
            expected_authors = ", ".join(_text(item) for item in metadata["authors"] if isinstance(item, str) and item.strip())
        else:
            expected_authors = None
        provenance_fields = (("title", title.get("title"), expected_title, True), ("authors", title.get("authors"), expected_authors, True), ("venue", title.get("venue"), metadata.get("venue"), False))
        version = metadata.get("version")
        if isinstance(version, Mapping):
            version = version.get("resolved", version.get("version", version.get("base")))
        provenance_fields += (("version", title.get("venue"), version, False),)
        for field, visible, expected, exact in provenance_fields:
            if not _text(expected):
                continue
            matches = _same_provenance(visible, expected) if exact else _contains_provenance(visible, expected)
            if not matches or _MISSING_RE.search(_text(visible)):
                issues.append(_issue(code="semantic-title-provenance", severity="error", slide_index=title_positions[0] + 1, pointer=f"/slides/{title_positions[0]}/{field}", message="visible title metadata does not match the reviewed digest provenance", evidence={"field": field, "expected": expected, "visible": _text(visible)}, action="Use the exact reviewed title, author, venue, and version values on the cover."))


def _role_layout_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    for index, slide in enumerate(slides, 1):
        role, layout = _role(slide), _text(slide.get("layout"))
        allowed = _LAYOUTS_BY_ROLE.get(role)
        archetypes = slide.get("archetypes")
        if isinstance(archetypes, Sequence) and not isinstance(archetypes, (str, bytes, bytearray)) and "section-divider" in archetypes:
            allowed = {"section"}
        if strict and allowed and layout not in allowed:
            issues.append(_issue(code="semantic-role-layout-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/layout", message="slide layout does not match its semantic role", evidence={"role": role, "layout": layout, "allowed_layouts": sorted(allowed)}, action="Choose a layout whose editable structure matches the declared semantic role."))


def _meaningful_content(value: Any) -> bool:
    """Return whether a visible/content field has a non-placeholder value."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    for child in _strings(value):
        text = _text(child)
        if text and not _MISSING_RE.search(text):
            return True
    return False


def _has_untranslated_latin(text: str, slide: Mapping[str, Any]) -> bool:
    allowed = {
        token.casefold()
        for term in slide.get("speaker_technical_terms", []) or []
        for token in _LATIN_WORD_RE.findall(str(term))
    } if isinstance(slide.get("speaker_technical_terms"), Sequence) else set()
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


def _role_content_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    """Fail closed on empty required role structures without constraining valid layouts."""
    if not strict:
        return
    evidence_fields = ("points", "points2", "items", "evidence", "figure", "table", "native_diagram")
    for index, slide in enumerate(slides, 1):
        role, layout = _role(slide), _text(slide.get("layout"))
        if role in {"method", "process", "metrics", "concept", "results", "conclusion", "background", "problem", "question"}:
            primary = any(_meaningful_content(slide.get(field)) for field in ("action_title", "core_conclusion"))
            equivalent = any(_meaningful_content(slide.get(field)) for field in evidence_fields)
            if role in {"background", "problem", "question"}:
                primary = primary or _meaningful_content(slide.get("title"))
                equivalent = equivalent or _meaningful_content(slide.get("questions"))
            if not primary and not equivalent:
                issues.append(_issue(
                    code="semantic-role-content-missing",
                    severity="error",
                    slide_index=index,
                    pointer=f"/slides/{index - 1}",
                    message="evidence-bearing role has no non-empty assertion or equivalent evidence content",
                    evidence={"role": role, "layout": layout, "required": ["action_title/core_conclusion", "equivalent evidence"]},
                    action="Restore the role's assertion and conclusion text or bind an editable figure, table, native diagram, or evidence list.",
                ))
        elif role == "discussion":
            if layout == "discussion-questions":
                required = ("title", "questions")
            else:
                required = ("title/action_title", "questions/points")
            title_ok = _meaningful_content(slide.get("title")) or (layout != "discussion-questions" and _meaningful_content(slide.get("action_title")))
            questions_ok = _meaningful_content(slide.get("questions")) or (layout != "discussion-questions" and _meaningful_content(slide.get("points")))
            missing = [required[0]] if not title_ok else []
            if not questions_ok:
                missing.append(required[1])
            if missing:
                issues.append(_issue(
                    code="semantic-role-content-missing",
                    severity="error",
                    slide_index=index,
                    pointer=f"/slides/{index - 1}",
                    message="discussion slide is missing its title or questions structure",
                    evidence={"role": role, "layout": layout, "missing": missing},
                    action="Provide a discussion title and question list appropriate to the selected layout.",
                ))
        elif role == "sources":
            missing = []
            if not _meaningful_content(slide.get("title")):
                missing.append("title")
            if not _meaningful_content(slide.get("entries")):
                missing.append("entries")
            if missing:
                issues.append(_issue(
                    code="semantic-role-content-missing",
                    severity="error",
                    slide_index=index,
                    pointer=f"/slides/{index - 1}",
                    message="references slide is missing its title or source entries",
                    evidence={"role": role, "layout": layout, "missing": missing},
                    action="Provide a references title and non-empty source entries.",
                ))


def _method_completeness_checks(slides: list[Mapping[str, Any]], digest: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool) -> None:
    """Require an evidence-driven journal-club method arc when the source supports it."""
    if not strict:
        return
    evidence = _reviewed_evidence_records(digest)
    method_items = [item for item in evidence if any(term in " ".join(_text(item.get(key)) for key in ("summary", "evidence", "section")).casefold() for term in ("method", "framework", "pipeline", "stage", "step", "mechanism", "parameter", "trajectory", "process", "机制", "阶段"))]
    if len(method_items) < 2:
        return
    method_slides = [slide for slide in slides if _role(slide) == "method"]
    process_slides = [slide for slide in slides if _role(slide) == "process"]
    experiment_slides = [slide for slide in slides if _role(slide) in {"metrics", "results", "analysis", "experiment"}]
    archetypes = {str(value) for slide in [*method_slides, *process_slides] for value in (slide.get("archetypes") or [])}
    method_text = " ".join(
        _text(slide.get(field))
        + " "
        + _text((slide.get("figure") or {}).get("caption") if isinstance(slide.get("figure"), Mapping) else "")
        for slide in method_slides
        for field in ("action_title", "core_conclusion")
    ).casefold()
    def native_process_structure(slide: Mapping[str, Any]) -> bool:
        diagram = slide.get("native_diagram") if isinstance(slide.get("native_diagram"), Mapping) else {}
        relation = _text(diagram.get("relation_type")).casefold()
        nodes = diagram.get("nodes") if isinstance(diagram.get("nodes"), list) else []
        edges = diagram.get("edges") if isinstance(diagram.get("edges"), list) else []
        directional = {"process", "pipeline", "workflow", "procedure", "stage_sequence", "state_transition", "causal"}
        # Backward-compatible process slides already carry an unambiguous
        # semantic role even when older deck data omitted relation_type.  Do not
        # extend this inference to result/comparison slides.
        relation_is_process = relation in directional or (not relation and _role(slide) == "process")
        return relation_is_process and len(nodes) >= 2 and bool(edges)

    # A constrained journal-club plan may legally co-host overview, stages, and
    # mechanism in one source-bound native flow.  Count the rendered structure,
    # not whether the planner spent a second slide on a separate process role.
    process_has_native_structure = any(
        native_process_structure(slide)
        or _meaningful_content(slide.get("points2"))
        or _meaningful_content(slide.get("process_explanation"))
        for slide in [*method_slides, *process_slides]
    )
    missing: list[str] = []
    if not method_slides or not ("method-overview" in archetypes or "overview" in method_text or any("overview" in _text(slide.get("action_title")).casefold() for slide in method_slides)):
        missing.append("method overview")
    if not ("core-stage" in archetypes or "method-stage" in archetypes or process_has_native_structure or any(any(term in _text(slide.get("action_title")).casefold() for term in ("stage", "step", "阶段")) for slide in [*method_slides, *process_slides])):
        missing.append("core method stage")
    if not ("mechanism" in archetypes or process_has_native_structure):
        missing.append("mechanism/process")
    if not experiment_slides:
        missing.append("experiment evidence")
    if missing:
        issues.append(_issue(
            code="semantic-method-completeness",
            severity="error",
            slide_index=0,
            pointer="/slides",
            message="journal-club method arc is missing evidence-driven sections",
            evidence={"missing": missing, "method_evidence_count": len(method_items)},
            action="Add source-grounded method overview, core-stage, mechanism/process, and experiment evidence before review.",
        ))


def _representation_semantics_checks(
    slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool,
) -> None:
    """Reject arrows whose source relation is comparison rather than sequence/causality."""
    if not strict:
        return
    directional = {"process", "pipeline", "workflow", "procedure", "stage_sequence", "state_transition", "causal"}
    prohibited = {"comparison", "ablation", "trade_off", "correlation", "result_pair", "negative_result", "independent_observations"}
    for index, slide in enumerate(slides, 1):
        if _text(slide.get("layout")).casefold() != "evidence-flow":
            continue
        diagram = slide.get("native_diagram") if isinstance(slide.get("native_diagram"), Mapping) else {}
        relation = _text(diagram.get("relation_type")).casefold()
        evidence_type = _text(diagram.get("semantic_evidence_type")).casefold()
        if relation in prohibited or (relation and relation not in directional) or (not relation and evidence_type in {"result", "failure_analysis", "limitation"}):
            issues.append(_issue(
                code="semantic-representation-mismatch", severity="error",
                slide_index=index, pointer=f"/slides/{index - 1}/native_diagram",
                message="directional process flow represents comparison, independent results, or another non-sequential relation",
                evidence={"relation_type": relation, "semantic_evidence_type": evidence_type, "role": _role(slide)},
                action="Use a comparison table, paired assertions, two-column comparison, or result summary unless the source explicitly supports a stage or causal sequence.",
            ))


_INTERNAL_TABLE_COLUMN_RE = re.compile(r"\b(?:unused|padding|spacer|placeholder)\b", re.IGNORECASE)


def _binding_text(slide: Mapping[str, Any]) -> str:
    binding = slide.get("speaker_evidence_binding") if isinstance(slide.get("speaker_evidence_binding"), Mapping) else {}
    return " ".join(
        _text(binding.get(key))
        for key in ("locator", "section", "summary", "evidence")
    ).casefold()


_ROLE_SOLUTION_TERMS = (
    "training-free", "zero-shot", "framework", "we propose", "proposes", "executable trajectories",
    "免训练", "零样本", "框架", "提出", "可执行轨迹",
)
_ROLE_PROBLEM_TERMS = (
    "problem", "challenge", "gap", "failure", "failed", "error", "incomplete", "limited",
    "limitation", "latency", "问题", "挑战", "缺口", "失败", "错误", "不完整", "受限", "局限", "延迟",
)
_DISCUSSION_PROVENANCE_TERMS = (
    "only used to support", "not the sole evidence", "evidence locator", "provenance",
    "仅用于支撑", "唯一证据", "证据定位", "审计定位",
)
_SPLIT_ROW_EMPTY_TOKENS = frozenset({"", "\u2013", "\u2014", "n/a", "na", "none", "null"})


def _visible_role_text(slide: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("action_title", "core_conclusion", "title", "points", "points2", "questions"):
        values.extend(_strings(slide.get(key)))
    return _norm(" ".join(values))


def _role_contract_present(slides: Sequence[Mapping[str, Any]], digest: Mapping[str, Any]) -> bool:
    role_slides = [
        slide for slide in slides
        if _role(slide) in {"background", "problem"}
    ]
    return any(
        any(key in slide for key in ("role_selection", "semantic_evidence_type", "role_compatibility_score"))
        for slide in role_slides
    ) or any(
        isinstance(record, Mapping)
        and any(record.get(key) for key in ("semantic_evidence_type", "evidence_type", "semantic_type"))
        for record in _reviewed_evidence_records(digest)
    )


def _role_bound_record(slide: Mapping[str, Any], digest: Mapping[str, Any]) -> Mapping[str, Any] | None:
    binding = slide.get("speaker_evidence_binding") if isinstance(slide.get("speaker_evidence_binding"), Mapping) else {}
    locators = _slide_evidence_locators(slide)
    summary = _text(binding.get("summary")) or _visible_role_text(slide)
    explicit_type = _text(slide.get("semantic_evidence_type")) or _text(binding.get("semantic_evidence_type"))
    section = _text(slide.get("evidence_section")) or _text(binding.get("section"))
    records = _reviewed_evidence_records(digest)
    matches = [
        record for record in records
        if locators and _reviewed_locator(record)
        and _matches_any_locator(_reviewed_locator(record), locators, allow_range_member=True)
    ]
    selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
    selected = selection.get("selected_candidate") if isinstance(selection.get("selected_candidate"), Mapping) else {}
    selected_type = _text(selected.get("semantic_evidence_type")) or explicit_type
    selected_slot = _text(selected.get("origin_semantic_slot"))
    selected_summary = _norm(_text(selected.get("summary")) or summary)

    def match_score(record: Mapping[str, Any]) -> tuple[int, str]:
        score = 0
        if selected_type and classify_evidence(record) == selected_type:
            score += 8
        if selected_slot and _text(record.get("origin_semantic_slot") or record.get("semantic_slot")) == selected_slot:
            score += 12
        if selected_summary and _norm(_text(record.get("summary") or record.get("text"))) == selected_summary:
            score += 16
        if section and evidence_section(record).casefold() == section.casefold():
            score += 2
        if binding.get("source_page") == record.get("source_page"):
            score += 1
        return score, _norm(_text(record.get("summary") or record.get("text")))

    if matches:
        record = max(matches, key=match_score)
        bound = dict(record)
        if explicit_type:
            bound["semantic_evidence_type"] = explicit_type
        if section:
            bound["section"] = section
        if summary and not _text(bound.get("summary")):
            bound["summary"] = summary
        return bound
    if not summary and not section and not explicit_type:
        return None
    return {
        "summary": summary,
        "evidence": _text(binding.get("evidence")),
        "source_page": binding.get("source_page"),
        "section": section,
        "figure_table_equation": locators[0] if locators else _text(binding.get("locator")),
        "semantic_evidence_type": explicit_type,
    }


def _append_role_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    slide_index: int,
    role: str,
    record: Mapping[str, Any] | None,
    message: str,
    action: str,
) -> None:
    issues.append(_issue(
        code=code,
        severity="error",
        slide_index=slide_index,
        pointer=f"/slides/{slide_index - 1}",
        message=message,
        evidence={
            "role": role,
            "semantic_evidence_type": classify_evidence(record or {}),
            "section": evidence_section(record or {}),
            "locator": _reviewed_locator(record or {}),
        },
        action=action,
    ))


def _role_semantic_checks(
    slides: list[Mapping[str, Any]], digest: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool,
) -> None:
    if not strict:
        return
    role_contract = _role_contract_present(slides, digest)
    background_index = next((index for index, slide in enumerate(slides, 1) if _role(slide) == "background"), None)
    problem_index = next((index for index, slide in enumerate(slides, 1) if _role(slide) == "problem"), None)
    if role_contract:
        for index, slide in enumerate(slides, 1):
            role = _role(slide)
            if role not in {"background", "problem"}:
                continue
            selection = slide.get("role_selection") if isinstance(slide.get("role_selection"), Mapping) else {}
            record = _role_bound_record(slide, digest)
            if _text(selection.get("status")).casefold() == "missing":
                binding = slide.get("speaker_evidence_binding")
                content = slide.get("speaker_content") if isinstance(slide.get("speaker_content"), Mapping) else {}
                refs = content.get("source_refs") if isinstance(content, Mapping) else None
                if binding is not None or (isinstance(refs, list) and refs):
                    issues.append(_issue(
                        code="missing-role-has-speaker-binding",
                        severity="error",
                        slide_index=index,
                        pointer=f"/slides/{index - 1}",
                        message="a missing role must not retain speaker evidence binding or source references",
                        evidence={"role": role, "binding_present": binding is not None, "source_ref_count": len(refs) if isinstance(refs, list) else None},
                        action="Clear speaker_evidence_binding and set speaker_content.source_refs to an empty list while the role remains missing.",
                    ))
            selected_type = _text(selection.get("semantic_evidence_type"))
            semantic_type = (
                selected_type
                if selected_type and selected_type.casefold() != "missing"
                else _text(slide.get("semantic_evidence_type"))
            )
            if not semantic_type and record is not None:
                semantic_type = classify_evidence(record)
            if _text(selection.get("status")).casefold() == "missing" or semantic_type in {"", "missing"}:
                _append_role_issue(
                    issues,
                    code="role-compatible-evidence-missing",
                    slide_index=index,
                    role=role,
                    record=record,
                    message=f"{role} has no role-compatible reviewed evidence candidate",
                    action="Select reviewed context/existing-paradigm/motivation evidence for background or research-gap/problem-setup/motivation evidence for problem; otherwise leave the slide pending human review.",
                )
            if record is None:
                _append_role_issue(
                    issues,
                    code="role-compatible-evidence-missing",
                    slide_index=index,
                    role=role,
                    record=None,
                    message=f"{role} selection is not bound to a reviewed evidence record",
                    action="Bind the selected role to a reviewed source locator and rerun semantic QA.",
                )
                continue
            compatibility = role_compatibility(role, record)
            if not compatibility["compatible"] or int(selection.get("role_compatibility_score") or compatibility["role_compatibility_score"]) <= 0:
                _append_role_issue(
                    issues,
                    code="role-compatible-evidence-missing",
                    slide_index=index,
                    role=role,
                    record=record,
                    message=f"{role} selection is not compatible with its semantic evidence type",
                    action="Replace the incompatible candidate with a role-compatible reviewed evidence record; do not use lexical similarity as a fallback.",
                )
            if is_section_mismatch(role, record):
                _append_role_issue(
                    issues,
                    code="role-section-mismatch",
                    slide_index=index,
                    role=role,
                    record=record,
                    message=f"{role} evidence type and source section do not agree with the section-aware role prior",
                    action="Use context/gap evidence from Introduction/Background/Problem Setup for the narrative roles, or correct the bound section metadata.",
                )
            if role == "background" and semantic_type == "proposal":
                _append_role_issue(
                    issues,
                    code="role-solution-as-background",
                    slide_index=index,
                    role=role,
                    record=record,
                    message="background presents a solution/proposal claim",
                    action="Reserve proposal/framework claims for method or contribution roles and select context, existing-paradigm, or motivation evidence for background.",
                )
            elif role == "background" and semantic_type == "result":
                _append_role_issue(
                    issues,
                    code="role-result-as-background",
                    slide_index=index,
                    role=role,
                    record=record,
                    message="background presents a result/evaluation claim",
                    action="Move result evidence to the result role and select contextual evidence for background.",
                )
            elif role == "problem" and semantic_type == "proposal":
                _append_role_issue(
                    issues,
                    code="role-solution-as-problem",
                    slide_index=index,
                    role=role,
                    record=record,
                    message="problem presents a solution/proposal claim",
                    action="Select research-gap, problem-setup, or motivation evidence for problem and reserve solution positioning for method/contribution.",
                )
            elif role == "problem" and semantic_type == "failure_analysis":
                _append_role_issue(
                    issues,
                    code="role-failure-as-problem",
                    slide_index=index,
                    role=role,
                    record=record,
                    message="problem presents downstream failure-analysis evidence",
                    action="Use an Introduction or Problem Setup research gap; keep failure analysis in discussion/limitation roles unless the failure mechanism is explicitly the research object.",
                )
            elif role == "problem" and semantic_type == "result":
                _append_role_issue(
                    issues,
                    code="role-result-as-problem",
                    slide_index=index,
                    role=role,
                    record=record,
                    message="problem presents a result/evaluation claim",
                    action="Move result evidence to the result role and select a reviewed research gap or problem setup for problem.",
                )
    if background_index is not None and problem_index is not None:
        background = slides[background_index - 1]
        problem = slides[problem_index - 1]
        background_text = _visible_role_text(background)
        problem_text = _visible_role_text(problem)
        background_locators = set(_slide_evidence_locators(background))
        problem_locators = set(_slide_evidence_locators(problem))
        background_tokens = _tokens(background_text)
        problem_tokens = _tokens(problem_text)
        similarity = (
            len(background_tokens & problem_tokens) / max(1, len(background_tokens | problem_tokens))
            if background_tokens and problem_tokens else 0.0
        )
        if background_locators & problem_locators and similarity >= 0.85:
            overlap_reason = _text(background.get("role_overlap_reason")) or _text(problem.get("role_overlap_reason"))
            issues.append(_issue(
                code="role-semantic-duplication", severity="warning" if overlap_reason else "error", slide_index=problem_index,
                pointer=f"/slides/{problem_index - 1}",
                message="background and problem reuse the same normalized claim and evidence binding",
                evidence={"background_slide": background_index, "problem_slide": problem_index, "locators": sorted(background_locators & problem_locators), "normalized_similarity": round(similarity, 4), "overlap_reason": overlap_reason},
                action="Bind background to contextual motivation and problem to an independent challenge, gap, failure, or limitation record; if none exists, record an explicit human-review reason.",
            ))
        if not role_contract:
            solution_positioning = any(term in problem_text for term in _ROLE_SOLUTION_TERMS)
            independent_problem = any(
                (
                    any(term in " ".join(_text(record.get(key)) for key in ("summary", "evidence", "section")).casefold() for term in _ROLE_PROBLEM_TERMS)
                    and not any(term in " ".join(_text(record.get(key)) for key in ("summary", "evidence", "section")).casefold() for term in _ROLE_SOLUTION_TERMS)
                )
                for record in _reviewed_evidence_records(digest)
            )
            if solution_positioning and independent_problem:
                issues.append(_issue(
                    code="role-solution-as-problem", severity="error", slide_index=problem_index,
                    pointer=f"/slides/{problem_index - 1}",
                    message="problem role presents solution positioning even though an independent problem evidence candidate exists",
                    evidence={"problem_text": problem_text, "candidate_terms": list(_ROLE_PROBLEM_TERMS)},
                    action="Select the reviewed challenge/gap/failure evidence for the problem role and reserve training-free/zero-shot/framework claims for method or contribution roles.",
                ))


def _discussion_semantic_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    if not strict:
        return
    for index, slide in enumerate(slides, 1):
        if _role(slide) != "discussion":
            continue
        questions = slide.get("questions") if isinstance(slide.get("questions"), list) else []
        text = " ".join(_strings(questions)).casefold()
        provenance_hits = [term for term in _DISCUSSION_PROVENANCE_TERMS if term in text]
        if provenance_hits:
            issues.append(_issue(
                code="discussion-provenance-as-scientific-claim", severity="error", slide_index=index,
                pointer=f"/slides/{index - 1}/questions",
                message="discussion promotes evidence-bookkeeping language to a scientific claim",
                evidence={"matches": provenance_hits, "questions": questions},
                action="Use author-reported limitations, failure analysis, experimental design, comparability, dependencies, latency, generalization, or external-validity evidence as the primary discussion grounding.",
            ))
        if re.search(r"(?:[.!?。！？；;]){2,}|\.\.", text):
            issues.append(_issue(
                code="discussion-duplicate-punctuation", severity="error", slide_index=index,
                pointer=f"/slides/{index - 1}/questions",
                message="discussion question contains repeated terminal punctuation",
                evidence={"questions": questions},
                action="Normalize the evidence summary to one terminal punctuation mark before composing the discussion question.",
            ))


def _conclusion_semantic_checks(
    slides: list[Mapping[str, Any]],
    digest: Mapping[str, Any],
    deck: Mapping[str, Any],
    issues: list[dict[str, Any]],
    strict: bool,
) -> None:
    if not strict:
        return
    for index, slide in enumerate(slides, 1):
        if _role(slide) != "conclusion":
            continue
        text = _visible_role_text(slide)
        number_count = len(re.findall(r"\b\d+(?:\.\d+)?\b", text))
        if number_count >= 6 and any(marker in text for marker in ("=", "/", ";", "|")):
            issues.append(_issue(
                code="conclusion-full-matrix-repetition", severity="error", slide_index=index,
                pointer=f"/slides/{index - 1}",
                message="conclusion repeats a dense quantitative model matrix instead of synthesizing takeaways",
                evidence={"number_count": number_count, "text": text},
                action="Keep contribution synthesis, one to three takeaways, and the key limitation/trade-off; leave the full matrix on the results slide.",
            ))
    semantics = digest.get("reviewed_paper_semantics")
    slots = semantics.get("slots") if isinstance(semantics, Mapping) else None
    if not isinstance(slots, Mapping):
        return
    component_slots = {
        "contribution": "contributions",
        "main_result": "main_results",
        "limitation": "limitations_or_failure_modes",
    }
    required = {
        component: slot_name
        for component, slot_name in component_slots.items()
        if isinstance(slots.get(slot_name), Mapping)
        and _text(slots[slot_name].get("summary") or slots[slot_name].get("text"))
    }
    if not required:
        return
    conclusion_slides = [
        (index, slide)
        for index, slide in enumerate(slides, 1)
        if _role(slide) == "conclusion"
        or "conclusion" in {str(value) for value in slide.get("merged_roles", []) or []}
    ]
    meta = deck.get("meta") if isinstance(deck.get("meta"), Mapping) else {}
    contract = meta.get("deck_type_contract") if isinstance(meta.get("deck_type_contract"), Mapping) else {}
    semantic_components = contract.get("semantic_components") if isinstance(contract.get("semantic_components"), Mapping) else {}
    conclusion_contract = semantic_components.get("conclusion") if isinstance(semantic_components.get("conclusion"), Mapping) else {}
    allow_split = conclusion_contract.get("allow_split_fulfillment") is True

    def valid_components(slide: Mapping[str, Any]) -> set[str]:
        components = slide.get("conclusion_components")
        if not isinstance(components, Mapping):
            return set()
        visible = _norm(_visible_role_text(slide))
        valid: set[str] = set()
        for component, slot_name in required.items():
            value = components.get(component)
            if not isinstance(value, Mapping):
                continue
            text = _text(value.get("text"))
            ownership = _text(value.get("ownership")).casefold()
            if (
                _text(value.get("origin_semantic_slot")) == slot_name
                and ownership == "author_reported"
                and text
                and _norm(text) in visible
            ):
                valid.add(component)
        return valid

    unexpected: list[str] = []
    for _, slide in conclusion_slides:
        components = slide.get("conclusion_components")
        if isinstance(components, Mapping):
            unexpected.extend(
                component for component in components
                if component in component_slots and component not in required
            )
    if allow_split:
        fulfilled = set().union(*(valid_components(slide) for _, slide in conclusion_slides)) if conclusion_slides else set()
        complete = set(required) <= fulfilled
    else:
        complete = any(set(required) <= valid_components(slide) for _, slide in conclusion_slides)
        fulfilled = set().union(*(valid_components(slide) for _, slide in conclusion_slides)) if conclusion_slides else set()
    if not complete or unexpected:
        issues.append(_issue(
            code="conclusion-semantic-completeness",
            severity="error",
            slide_index=conclusion_slides[0][0] if conclusion_slides else 0,
            pointer="/slides",
            message="conclusion does not visibly synthesize the confirmed contribution/result/limitation slots",
            evidence={
                "required_components": sorted(required),
                "fulfilled_components": sorted(fulfilled),
                "unexpected_components": sorted(set(unexpected)),
                "allow_split_fulfillment": allow_split,
            },
            action="Render one concise author-reported point for each confirmed conclusion component on one slide unless the deck contract explicitly allows split fulfillment.",
        ))


def _split_table_row_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    if not strict:
        return
    for index, slide in enumerate(slides, 1):
        table = slide.get("table") if isinstance(slide.get("table"), Mapping) else None
        if table is None or table.get("preserve_empty_rows") is True or table.get("row_group_continuity") is True:
            continue
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        row_header_width = 1 if table.get("row_header") is True else 0
        for row_index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            visible_cells = row[row_header_width:]
            if visible_cells and all(
                value is None or (isinstance(value, str) and value.strip().casefold() in _SPLIT_ROW_EMPTY_TOKENS)
                for value in visible_cells
            ):
                issues.append(_issue(
                    code="semantic-split-table-empty-row", severity="error", slide_index=index,
                    pointer=f"/slides/{index - 1}/table/rows/{row_index}",
                    message="native table retains a row that is empty in the visible panel",
                    evidence={"row": row, "row_header_width": row_header_width},
                    action="Prune panel-local all-empty rows while preserving literal hyphen, zero, and cross markers as semantic values.",
                ))


def _role_source_consistency_checks(
    slides: list[Mapping[str, Any]], digest: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool,
) -> None:
    """Reject context/problem roles that bind to result or limitation-only evidence."""
    if not strict:
        return
    records = _reviewed_evidence_records(digest)
    context_records = [
        record for record in records
        if any(term in _text(record.get("section")).casefold() for term in ("introduction", "background", "motivation", "abstract", "problem"))
        and not any(term in " ".join(_text(record.get(key)) for key in ("summary", "evidence", "section")).casefold() for term in ("limitation", "failure", "error", "latency", "局限", "失败", "错误", "延迟"))
    ]
    if not context_records:
        return
    for index, slide in enumerate(slides, 1):
        role = _role(slide)
        if role not in {"background", "problem"}:
            continue
        bound = _binding_text(slide)
        if not bound:
            continue
        result_only = role == "background" and bool(re.search(r"\b(?:table|figure|results?|evaluation|comparison|benchmark)\b", bound))
        problem_text = _visible_role_text(slide)
        limitation_only = (
            role == "problem"
            and any(term in bound for term in ("limitation", "failure", "error", "latency", "局限", "失败", "错误", "延迟"))
            and not any(term in problem_text for term in _ROLE_PROBLEM_TERMS)
        )
        if result_only or limitation_only:
            issues.append(_issue(
                code="semantic-role-source-mismatch",
                severity="error",
                slide_index=index,
                pointer=f"/slides/{index - 1}/speaker_evidence_binding",
                message="background/problem role is bound to result or limitation-only evidence despite available context evidence",
                evidence={"role": role, "binding": bound, "context_candidates": len(context_records)},
                action="Bind context and research-problem roles to introduction/background/abstract evidence and reserve result/limitation records for results or critique.",
            ))


def _method_evidence_identity_checks(slides: list[Mapping[str, Any]], digest: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool) -> None:
    """Require method overview and process slides to use distinct source locators."""
    if not strict:
        return
    evidence = _reviewed_evidence_records(digest)
    method_items = [
        item for item in evidence
        if any(term in " ".join(_text(item.get(key)) for key in ("summary", "evidence", "section")).casefold()
               for term in ("method", "framework", "pipeline", "stage", "step", "mechanism", "parameter", "trajectory", "process", "机制", "阶段"))
    ]
    if len(method_items) < 2:
        return
    method_slides = [slide for slide in slides if _role(slide) in {"method", "process"}]
    overview = next((slide for slide in method_slides if _role(slide) == "method"), None)
    process = next((slide for slide in method_slides if _role(slide) == "process"), None)
    if overview is None or process is None:
        return
    overview_binding = overview.get("speaker_evidence_binding") if isinstance(overview.get("speaker_evidence_binding"), Mapping) else {}
    process_binding = process.get("speaker_evidence_binding") if isinstance(process.get("speaker_evidence_binding"), Mapping) else {}
    overview_locator = _text(overview_binding.get("locator")) or (_slide_evidence_locators(overview)[:1] or [""])[0]
    process_locator = _text(process_binding.get("locator")) or (_slide_evidence_locators(process)[:1] or [""])[0]
    if overview_locator and process_locator and _same_locator(overview_locator, process_locator):
        issues.append(_issue(
            code="semantic-method-evidence-duplicate",
            severity="error",
            slide_index=next((index for index, slide in enumerate(slides, 1) if slide is process), 0),
            pointer="/slides",
            message="method overview and process slides reuse the same evidence locator",
            evidence={"method_locator": overview_locator, "process_locator": process_locator},
            action="Select a distinct reviewed method-stage or mechanism evidence item for the process slide.",
        ))


def _native_table_schema_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    if not strict:
        return
    for index, slide in enumerate(slides, 1):
        table = slide.get("table") if isinstance(slide.get("table"), Mapping) else None
        if table is None:
            continue
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        leaked = [
            _text(column.get("label")) if isinstance(column, Mapping) else _text(column)
            for column in columns
            if _INTERNAL_TABLE_COLUMN_RE.search(_text(column.get("label")) if isinstance(column, Mapping) else _text(column))
        ]
        if leaked:
            issues.append(_issue(
                code="semantic-table-schema",
                severity="error",
                slide_index=index,
                pointer=f"/slides/{index - 1}/table/columns",
                message="native table exposes an internal schema column",
                evidence={"columns": leaked},
                action="Normalize all-missing/internal audit columns before rendering; keep only audience-visible semantic fields.",
            ))


def _discussion_specificity_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    if not strict:
        return
    critique_terms = ("limitation", "failure", "baseline", "protocol", "latency", "dependency", "generalization", "可比", "协议", "失败", "局限", "延迟", "泛化")
    for index, slide in enumerate(slides, 1):
        if _role(slide) != "discussion":
            continue
        questions = slide.get("questions") if isinstance(slide.get("questions"), list) else []
        text = " ".join(_text(value) for value in questions).casefold()
        grounding = _text(slide.get("discussion_grounding")).casefold()
        if not grounding:
            continue
        if grounding != "scientific_critique" or "作者报告的边界是：" in text or not any(term in text for term in critique_terms) or not ("作者" in text and "汇报者" in text):
            issues.append(_issue(
                code="semantic-discussion-specificity",
                severity="error",
                slide_index=index,
                pointer=f"/slides/{index - 1}/questions",
                message="discussion questions lack a scientific critique dimension or author/presenter boundary",
                evidence={"grounding": grounding, "questions": questions},
                action="Synthesize questions around reviewed limitations, failures, protocols, baselines, dependencies, latency, or generalization and distinguish author reports from presenter inference.",
            ))


def _role_duplicate_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool) -> None:
    """Reject repeated role/takeaway pairs while allowing distinct result views."""
    if not strict:
        return
    seen: dict[tuple[str, str], int] = {}
    for index, slide in enumerate(slides, 1):
        role = _role(slide)
        takeaway = next(
            (_norm(slide.get(field)) for field in ("action_title", "core_conclusion", "title") if _text(slide.get(field))),
            "",
        )
        if not role or not takeaway:
            continue
        key = (role, takeaway)
        previous = seen.get(key)
        if previous is not None:
            issues.append(_issue(
                code="semantic-role-duplicate",
                severity="error",
                slide_index=index,
                pointer=f"/slides/{index - 1}",
                message="the same semantic role and takeaway are repeated on multiple slides",
                evidence={"role": role, "takeaway": takeaway, "previous_slide": previous},
                action="Give the repeated role a distinct claim, evidence view, or narrative purpose.",
            ))
        else:
            seen[key] = index


def _native_diagram_valid(slide: Mapping[str, Any], value: Any, digest: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or value.get("editable") is not True:
        return False
    if _text(value.get("type")).casefold() not in {"evidence-flow", "native-diagram"}:
        return False
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not nodes or not isinstance(edges, list) or not edges:
        return False
    slide_locators = _slide_evidence_locators(slide)
    reviewed_locators = [_reviewed_locator(record) for record in _reviewed_evidence_records(digest) if _reviewed_locator(record)]
    if not slide_locators or not reviewed_locators:
        return False

    def valid_locator(locator: Any) -> bool:
        values = _locator_values(locator)
        return bool(values) and all(
            not _MISSING_RE.search(item)
            and _matches_any_locator(item, slide_locators, allow_range_member=True)
            and _matches_any_locator(item, reviewed_locators, allow_range_member=True)
            for item in values
        )

    node_ids = {_text(node.get("id")) for node in nodes if isinstance(node, Mapping)}
    if len(node_ids) != len(nodes) or not all(isinstance(node, Mapping) and _text(node.get("id")) and valid_locator(node.get("source_locator")) for node in nodes):
        return False
    if not valid_locator(value.get("source_locator")) and _locator_values(value.get("source_locator")):
        return False
    node_by_id = {_text(node.get("id")): node for node in nodes if isinstance(node, Mapping)}
    for edge in edges:
        if not isinstance(edge, Mapping) or _text(edge.get("from")) not in node_ids or _text(edge.get("to")) not in node_ids:
            return False
        if _locator_values(edge.get("source_locator")):
            if not valid_locator(edge.get("source_locator")):
                return False
            continue
        # The generic planner emits an unlabeled edge.  It remains bound only
        # when both endpoint nodes carry the same reviewed locator.
        left = _locator_values(node_by_id[_text(edge["from"])].get("source_locator"))
        right = _locator_values(node_by_id[_text(edge["to"])].get("source_locator"))
        if not left or not right or any(not _same_locator(a, b) for a in left for b in right):
            return False
    return True


def _native_representation_valid(slide: Mapping[str, Any], value: Any, digest: Mapping[str, Any]) -> bool:
    if not isinstance(value, Mapping) or value.get("editable") is not True:
        return False
    if _text(value.get("type")).casefold() not in {"comparison", "assertion-list"}:
        return False
    if value.get("edges") not in ([], None):
        return False
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    slide_locators = _slide_evidence_locators(slide)
    reviewed_locators = [_reviewed_locator(record) for record in _reviewed_evidence_records(digest) if _reviewed_locator(record)]
    if not slide_locators or not reviewed_locators:
        return False
    return all(
        isinstance(node, Mapping)
        and _text(node.get("id"))
        and _text(node.get("label"))
        and _matches_any_locator(_text(node.get("source_locator")), slide_locators, allow_range_member=True)
        and _matches_any_locator(_text(node.get("source_locator")), reviewed_locators, allow_range_member=True)
        for node in nodes
    )


def _table_records(digest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for key in ("audited_table_evidence", "tables"):
        values = digest.get(key)
        if isinstance(values, Mapping):
            values = [values]
        if isinstance(values, list):
            records.extend(value for value in values if isinstance(value, Mapping))
    # A source figure may be intentionally redrawn as an editable native
    # table when the figure's quantitative content is the required evidence.
    # Keep the locator/page/hash contract identical to native table evidence.
    records.extend(
        record
        for record in _reviewed_evidence_records(digest)
        if _text(_reviewed_locator(record)).casefold().startswith(("table", "tab.", "figure", "fig."))
    )
    return records


def _hash_value(value: Mapping[str, Any]) -> str:
    for key in ("sha256", "source_sha256", "pdf_sha256", "hash"):
        candidate = _text(value.get(key))
        if candidate:
            return candidate.casefold()
    return ""


def _audited_crop_hashes(asset_graph: Mapping[str, Any], artifact_path: str) -> set[str]:
    """Return crop hashes linked from the exact manual audit artifact."""
    if not artifact_path:
        return set()
    nodes = asset_graph.get("nodes") if isinstance(asset_graph, Mapping) else []
    edges = asset_graph.get("edges") if isinstance(asset_graph, Mapping) else []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return set()
    normalized_path = artifact_path.replace("\\", "/")
    artifact_node = next(
        (
            node for node in nodes
            if isinstance(node, Mapping)
            and _text(node.get("path")).replace("\\", "/") == normalized_path
            and _text(node.get("kind")) == "audit_record"
        ),
        None,
    )
    if not isinstance(artifact_node, Mapping):
        return set()
    artifact_id = _text(artifact_node.get("id"))
    crop_ids = {
        _text(edge.get("to"))
        for edge in edges
        if isinstance(edge, Mapping)
        and _text(edge.get("from")) == artifact_id
        and _text(edge.get("relation")) == "evidences"
    }
    return {
        _hash_value(node)
        for node in nodes
        if isinstance(node, Mapping)
        and _text(node.get("id")) in crop_ids
        and _text(node.get("kind")) == "audit_crop"
        and _hash_value(node)
    }


def _table_fallback_valid(
    role: str,
    slide: Mapping[str, Any],
    value: Any,
    digest: Mapping[str, Any],
    asset_graph: Mapping[str, Any] | None = None,
    *,
    require_hash: bool = False,
) -> bool:
    if role not in {"results", "metrics", "analysis"} or not isinstance(value, Mapping) or not value:
        return False
    rows, columns = value.get("rows"), value.get("columns")
    if not (isinstance(rows, list) and bool(rows) and isinstance(columns, list) and bool(columns)):
        return False
    slide_locators = _slide_evidence_locators(slide)
    binding = slide.get("speaker_evidence_binding") if isinstance(slide.get("speaker_evidence_binding"), Mapping) else {}
    selection = slide.get("asset_selection") if isinstance(slide.get("asset_selection"), Mapping) else {}
    table_locator = _text(value.get("locator")) or _text(value.get("figure_table_equation")) or _text(value.get("source_locator")) or _text(binding.get("locator")) or _text(selection.get("evidence_locator"))
    table_page = value.get("source_page")
    if not isinstance(table_page, int):
        table_page = value.get("page")
    if not isinstance(table_page, int):
        table_page = binding.get("source_page")
    table_hash = _hash_value(value)
    if not table_hash:
        footnote = _text(value.get("footnote"))
        hash_match = _SHA256_RE.search(footnote)
        table_hash = hash_match.group(1).casefold() if hash_match else ""
    if not table_locator or not isinstance(table_page, int) or table_page < 1 or (require_hash and not table_hash):
        return False
    if not _matches_any_locator(table_locator, slide_locators, allow_range_member=True):
        return False
    provenance = value.get("provenance") if isinstance(value.get("provenance"), Mapping) else {}
    audited_hashes = _audited_crop_hashes(asset_graph or {}, _text(provenance.get("artifact")))
    for record in _table_records(digest):
        record_locator = _reviewed_locator(record)
        record_page = record.get("source_page", record.get("page"))
        record_hash = _hash_value(record)
        page_matches = table_page == record_page
        if not page_matches and _text(provenance.get("artifact")):
            # An explicit audit may use the crop-rendering page as its source
            # page while the reviewed semantic record points to the PDF page.
            # Accept that distinction only through the explicit audit binding;
            # strict roles still require the table hash to resolve to the
            # linked audit crop (or to the reviewed record hash).
            page_matches = not require_hash or bool(
                (table_hash and table_hash in audited_hashes)
                or (record_hash and table_hash == record_hash)
            )
        if (
            record_locator
            and _asset_locator_matches(table_locator, record_locator)
            and page_matches
            and (
                not require_hash
                or (
                    table_hash
                    and (
                        (record_hash and table_hash == record_hash)
                        or (not record_hash and table_hash in audited_hashes)
                    )
                )
            )
            and _matches_any_locator(record_locator, slide_locators, allow_range_member=True)
        ):
            return True
    return False


def _provenance_source_ref(record: Mapping[str, Any]) -> str:
    explicit = _text(record.get("source_ref"))
    if explicit:
        return explicit
    page = record.get("source_page", record.get("page"))
    section = _text(record.get("section")) or "Source"
    locator = _provenance_locator(record)
    if _text(page) and locator:
        return f"p. {_text(page)} - {section} - {locator}"
    return ""


def _provenance_locator(record: Mapping[str, Any]) -> str:
    return (
        _text(record.get("locator"))
        or _text(record.get("figure_table_equation"))
        or _text(record.get("label"))
    )


def _provenance_same_source(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_page = _text(left.get("source_page", left.get("page")))
    right_page = _text(right.get("source_page", right.get("page")))
    left_locator = _provenance_locator(left)
    right_locator = _provenance_locator(right)
    return bool(left_page and right_page and left_page == right_page and _same_locator(left_locator, right_locator))


def _provenance_checks(
    slides: list[Mapping[str, Any]], issues: list[dict[str, Any]], strict: bool,
) -> None:
    """Validate the distinction between factual claims and selected visuals."""
    del strict
    for index, slide in enumerate(slides, 1):
        claim = slide.get("claim_source") if isinstance(slide.get("claim_source"), Mapping) else None
        visual = slide.get("visual_source") if isinstance(slide.get("visual_source"), Mapping) else None
        if claim is None and visual is None:
            continue
        pointer = f"/slides/{index - 1}"
        if claim is not None:
            claim_locator = _provenance_locator(claim)
            claim_page = _text(claim.get("source_page", claim.get("page")))
            if not claim_page or not claim_locator:
                issues.append(_issue(
                    code="semantic-provenance-claim-incomplete", severity="error",
                    slide_index=index, pointer=f"{pointer}/claim_source",
                    message="claim_source must include a source page and locator",
                    evidence={"claim_source": claim},
                    action="Bind the factual claim to a reviewed page and locator.",
                ))
            claim_ref = _provenance_source_ref(claim)
            slide_ref = _text(slide.get("source_ref"))
            if claim_ref and slide_ref and not _source_contains(slide_ref, claim_ref):
                issues.append(_issue(
                    code="semantic-provenance-claim-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/source_ref",
                    message="slide source_ref does not identify the claim source",
                    evidence={"slide_source_ref": slide_ref, "claim_source_ref": claim_ref},
                    action="Keep source_ref bound to the factual claim; record an illustrative figure separately.",
                ))
            locators = _locator_values(slide.get("evidence_locators"))
            if claim_locator and (not locators or not _matches_any_locator(claim_locator, locators)):
                issues.append(_issue(
                    code="semantic-provenance-claim-locator-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/evidence_locators",
                    message="evidence_locators must contain the factual claim locator",
                    evidence={"claim_locator": claim_locator, "evidence_locators": locators},
                    action="Expose claim locators only; do not replace them with the optional visual locator.",
                ))
        if visual is not None:
            visual_locator = _provenance_locator(visual)
            visual_page = _text(visual.get("source_page", visual.get("page")))
            if not visual_page or not visual_locator:
                issues.append(_issue(
                    code="semantic-provenance-visual-incomplete", severity="error",
                    slide_index=index, pointer=f"{pointer}/visual_source",
                    message="visual_source must include a source page and locator",
                    evidence={"visual_source": visual},
                    action="Bind the selected visual to its reviewed page and locator.",
                ))
            figure = slide.get("figure") if isinstance(slide.get("figure"), Mapping) else None
            visible_cite = _text((figure or {}).get("cite")) or _text((figure or {}).get("source_ref"))
            visual_ref = _provenance_source_ref(visual)
            if not figure or not visible_cite:
                issues.append(_issue(
                    code="semantic-provenance-visual-citation-missing", severity="error",
                    slide_index=index, pointer=f"{pointer}/figure",
                    message="visual_source requires a visible figure citation",
                    evidence={"visual_source_ref": visual_ref, "has_figure": bool(figure), "has_visible_citation": bool(visible_cite)},
                    action="Keep the selected visual visible and cite its own reviewed page and locator.",
                ))
            elif visual_ref and not _source_matches(visible_cite, visual_ref):
                issues.append(_issue(
                    code="semantic-provenance-visual-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/figure/cite",
                    message="visible figure citation does not identify visual_source",
                    evidence={"visible_citation": visible_cite, "visual_source_ref": visual_ref},
                    action="Align the figure citation with visual_source.",
                ))
            support_type = _text(visual.get("support_type")).casefold()
            if support_type not in {"claim_support", "illustrative_support"}:
                issues.append(_issue(
                    code="semantic-provenance-support-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/visual_source/support_type",
                    message="visual_source support_type must be claim_support or illustrative_support",
                    evidence={"support_type": support_type},
                    action="Declare whether the visual directly supports the claim or is illustrative only.",
                ))
            expected_support = "claim_support"
            if claim is not None and not _provenance_same_source(claim, visual):
                expected_support = "illustrative_support"
            if support_type and support_type != expected_support:
                issues.append(_issue(
                    code="semantic-provenance-support-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/visual_source/support_type",
                    message="visual support type contradicts claim_source and visual_source locators",
                    evidence={"expected_support_type": expected_support, "actual_support_type": support_type},
                    action="Use claim_support only for the same reviewed source; otherwise mark the visual illustrative_support.",
                ))
            provenance_role = _text((figure or {}).get("provenance_role"))
            if provenance_role and provenance_role.casefold() != support_type:
                issues.append(_issue(
                    code="semantic-provenance-support-mismatch", severity="error",
                    slide_index=index, pointer=f"{pointer}/figure/provenance_role",
                    message="figure provenance_role must agree with visual_source support_type",
                    evidence={"figure_provenance_role": provenance_role, "support_type": support_type},
                    action="Keep the visible figure role synchronized with visual_source.",
                ))
            presentation = slide.get("provenance_display") if isinstance(slide.get("provenance_display"), Mapping) else {}
            entries = presentation.get("entries") if isinstance(presentation.get("entries"), list) else []
            entries = [entry for entry in entries if isinstance(entry, Mapping)]
            same_source = claim is not None and _provenance_same_source(claim, visual)
            if claim is not None and not same_source:
                claim_entries = [entry for entry in entries if _text(entry.get("role")).casefold() == "claim"]
                illustration_entries = [entry for entry in entries if _text(entry.get("role")).casefold() == "illustration"]
                roles_clear = (
                    len(claim_entries) == 1
                    and len(illustration_entries) == 1
                    and _text(claim_entries[0].get("label"))
                    and _text(illustration_entries[0].get("label"))
                    and _source_matches(claim_entries[0].get("source_ref"), _provenance_source_ref(claim))
                    and _source_matches(illustration_entries[0].get("source_ref"), visual_ref)
                )
                if not roles_clear:
                    issues.append(_issue(
                        code="audience-provenance-role-unclear", severity="error",
                        slide_index=index, pointer=f"{pointer}/provenance_display",
                        message="distinct claim and illustration sources lack clear visible role labels",
                        evidence={"claim_source": _provenance_source_ref(claim), "visual_source": visual_ref, "entries": entries},
                        action="Render one claim-evidence label and one illustration label in the deck language.",
                    ))
            elif same_source:
                duplicated_visible_refs = bool(
                    not entries
                    and _text(slide.get("source_ref"))
                    and visible_cite
                    and _source_matches(slide.get("source_ref"), visible_cite)
                )
                one_combined_entry = (
                    len(entries) == 1
                    and _text(entries[0].get("role")).casefold() == "claim_visual"
                    and _text(entries[0].get("label"))
                    and _source_matches(entries[0].get("source_ref"), visual_ref)
                )
                if duplicated_visible_refs or (entries and not one_combined_entry):
                    issues.append(_issue(
                        code="audience-provenance-duplicate-label", severity="error",
                        slide_index=index, pointer=f"{pointer}/provenance_display",
                        message="the same claim/visual source is shown as duplicate provenance labels",
                        evidence={"source": visual_ref, "entries": entries},
                        action="Render one combined evidence-and-visual provenance entry for a same-source figure.",
                    ))


def _asset_checks(
    slides: list[Mapping[str, Any]], digest: Mapping[str, Any], asset_graph: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool,
) -> None:
    records = _asset_records(digest)
    graph_paths = _graph_asset_paths(asset_graph)
    graph_visible_nodes = _graph_visible_asset_nodes(asset_graph)
    selected_counts: Counter[str] = Counter()
    selected_slides: defaultdict[str, list[int]] = defaultdict(list)
    family_slides: defaultdict[str, list[int]] = defaultdict(list)
    for index, slide in enumerate(slides, 1):
        role = _role(slide)
        policy = slide.get("asset_policy") if isinstance(slide.get("asset_policy"), Mapping) else asset_policy_for_role(role)
        mode = _text(policy.get("mode")) or "optional"
        allow_no_asset = policy.get("allow_no_asset") is not False
        selection = slide.get("asset_selection") if isinstance(slide.get("asset_selection"), Mapping) else {}
        candidate_id = _text(selection.get("candidate_id")) or None
        conflicts = selection.get("conflicts") if isinstance(selection.get("conflicts"), list) else []
        explicit_locators = slide.get("evidence_locators") if isinstance(slide.get("evidence_locators"), list) else []
        explicit_locators = [_text(value) for value in explicit_locators if _text(value)]
        visual_source = slide.get("visual_source") if isinstance(slide.get("visual_source"), Mapping) else None
        visual_locator = _provenance_locator(visual_source) if visual_source is not None else ""
        asset_locators = [visual_locator] if candidate_id and visual_locator else explicit_locators
        selection_locator = _text(selection.get("evidence_locator"))
        if selection_locator and asset_locators and not any(_asset_locator_matches(selection_locator, value) for value in asset_locators):
            issues.append(_issue(code="semantic-asset-locator-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/evidence_locator", message="selected asset locator differs from the slide visual locator", evidence={"selection_locator": selection_locator, "asset_locators": asset_locators}))
        visible_asset_fields = [field for field in ("figure", "media", "table", "images", "native_diagram", "native_representation") if slide.get(field)]
        if mode == "none" and visible_asset_fields:
            issues.append(_issue(code="semantic-no-asset-policy", severity="error", slide_index=index, pointer=f"/slides/{index - 1}", message="this role forbids visible assets but the slide contains one", evidence={"role": role, "fields": visible_asset_fields}, action="Remove the asset or change the role only when the evidence policy allows it."))
        native_valid = _native_diagram_valid(slide, slide.get("native_diagram"), digest) if "native_diagram" in slide else False
        representation_valid = _native_representation_valid(slide, slide.get("native_representation"), digest) if "native_representation" in slide else False
        table_valid = _table_fallback_valid(role, slide, slide.get("table"), digest, asset_graph, require_hash=mode == "required") if "table" in slide else False
        if strict and "table" in slide and bool(slide.get("table")) and not table_valid:
            issues.append(_issue(code="semantic-table-binding-invalid", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/table", message="visible table is not bound to a reviewed table locator and source page", evidence={"role": role}, action="Bind the visible table to a reviewed Table locator, source page, and audited hash where required."))
        if mode == "required" and (("native_diagram" in slide and bool(slide.get("native_diagram")) and not native_valid) or ("native_representation" in slide and bool(slide.get("native_representation")) and not representation_valid) or ("table" in slide and bool(slide.get("table")) and not table_valid)):
            issues.append(_issue(code="semantic-native-fallback-invalid", severity="error", slide_index=index, pointer=f"/slides/{index - 1}", message="required native evidence fallback is not an editable, source-bound diagram or audited table", evidence={"role": role}, action="Use a non-empty editable evidence-flow with source locators, or a table-compatible audited evidence mapping."))
        if mode == "required" and not candidate_id and not allow_no_asset and not (native_valid or representation_valid or table_valid):
            issues.append(_issue(code="semantic-required-asset-missing", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection", message="a required evidence role has no selected asset or editable native fallback", evidence={"role": role}, action="Bind a reviewed asset with an explicit locator or provide an editable evidence-flow fallback."))
        # A matcher conflict is actionable when it leaves a selected asset unresolved.
        # Required roles may intentionally fall back to a source-bound editable
        # evidence-flow or audited table, in which case the matcher has no selected
        # candidate to repair and the valid fallback is the semantic authority.
        fallback_valid = bool(native_valid or representation_valid or table_valid)
        if conflicts and (mode == "required" or candidate_id) and not (not candidate_id and fallback_valid):
            issues.append(_issue(code="semantic-asset-selection-conflict", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/conflicts", message="asset selection contains an unresolved matcher conflict", evidence={"conflicts": sorted(str(value) for value in conflicts)}, action="Resolve the locator, role, or confidence conflict before review."))
        figure = slide.get("figure") if isinstance(slide.get("figure"), Mapping) else None
        media = slide.get("media") if isinstance(slide.get("media"), Mapping) else None
        rendered_asset_field = "figure" if figure and _text(figure.get("src")) else ("media" if media and _text(media.get("src")) else None)
        rendered_asset = figure if rendered_asset_field == "figure" else media if rendered_asset_field == "media" else None
        src = _text(rendered_asset.get("src")) if rendered_asset else ""
        src_pointer = f"/slides/{index - 1}/{rendered_asset_field}/src" if rendered_asset_field else ""
        if candidate_id:
            selected_counts[candidate_id] += 1
            selected_slides[candidate_id].append(index)
            family_slides[candidate_id.split("-", 1)[0].casefold()].append(index)
            if strict and not any(path.rsplit("/", 1)[-1].startswith(candidate_id + ".") or path.rsplit("/", 1)[-1].startswith(candidate_id + "-") for path, _ in graph_paths):
                issues.append(_issue(code="semantic-asset-graph-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/candidate_id", message="selected asset is not present in the exact asset graph", evidence={"candidate_id": candidate_id}, action="Regenerate the asset graph from the deck and reviewed source bundle."))
            record = records.get(candidate_id)
            if strict and record is None:
                issues.append(_issue(code="semantic-asset-source-missing", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/candidate_id", message="selected asset has no reviewed digest record", evidence={"candidate_id": candidate_id}, action="Bind the selected asset to a reviewed digest locator."))
            if record is not None and asset_locators:
                record_locator = _reviewed_locator(record) or _text(record.get("label"))
                if record_locator and not any(_asset_locator_matches(value, record_locator) for value in asset_locators):
                    issues.append(_issue(code="semantic-asset-locator-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/candidate_id", message="selected asset record does not match the slide visual locator", evidence={"candidate_id": candidate_id, "asset_locator": _text(record_locator), "asset_locators": asset_locators}))
            if record is not None:
                record_locator = _reviewed_locator(record) or _text(record.get("label"))
                record_page = record.get("page", record.get("source_page"))
                record_source = _text(record.get("source_ref")) or _text(record.get("source"))
                record_expected_sources = [value for value in (record_source, record_locator) if value]
                if isinstance(record_page, int) and record_page > 0:
                    record_expected_sources.append(f"p. {record_page}")
                    if record_locator:
                        record_expected_sources.append(f"p. {record_page} — {record_locator}")
                if selection_locator and record_expected_sources and not any(_asset_locator_matches(selection_locator, value) for value in record_expected_sources):
                    issues.append(_issue(code="semantic-asset-locator-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/asset_selection/evidence_locator", message="selected asset locator does not match the reviewed digest locator", evidence={"candidate_id": candidate_id, "selected_locator": selection_locator, "expected_locators": record_expected_sources}))
                expected_sources = record_expected_sources or ([selection_locator] if selection_locator else [])
                figure_caption = _text(figure.get("caption", figure.get("label", ""))) if figure else ""
                visible_asset = figure or media or {}
                visible_citation = _text(visible_asset.get("cite")) or _text(visible_asset.get("source_ref")) or _text(visible_asset.get("source"))
                if (record_source or record_locator or isinstance(record_page, int)) and (not _text(slide.get("source_ref")) or not visible_citation):
                    issues.append(_issue(code="semantic-asset-citation-missing", severity="error", slide_index=index, pointer=f"/slides/{index - 1}", message="selected reviewed asset requires both a slide source_ref and a visible citation", evidence={"candidate_id": candidate_id, "has_slide_source_ref": bool(_text(slide.get("source_ref"))), "has_visible_citation": bool(visible_citation)}, action="Add the reviewed page/locator to source_ref and the visible figure or media citation."))
                binding = slide.get("speaker_evidence_binding") if isinstance(slide.get("speaker_evidence_binding"), Mapping) else {}
                slide_source = _text(slide.get("source_ref"))
                binding_locator = _text(binding.get("locator"))
                binding_page = binding.get("source_page")
                has_claim_binding = isinstance(binding_page, int) and binding_page > 0 and bool(binding_locator)
                # ``source_ref`` records the reviewed claim/evidence provenance,
                # while the visible figure citation records the selected asset's
                # visual page.  They may legitimately differ (for example when
                # an abstract summarizes a comparison shown later in the paper).
                asset_sources = [visible_citation] if has_claim_binding else [slide_source, visible_citation]
                asset_sources = [value for value in asset_sources if value]
                if expected_sources and any(not any(_asset_locator_matches(value, expected) for expected in expected_sources) for value in asset_sources):
                    issues.append(_issue(code="semantic-asset-source-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}", message="visible asset citation does not match the selected digest source reference or locator", evidence={"candidate_id": candidate_id, "asset_source_ref": record_source, "asset_locator": record_locator, "slide_sources": asset_sources}))
                if has_claim_binding and slide_source:
                    claim_expected = f"p. {binding_page} \u2014 {binding_locator}"
                    if not _asset_locator_matches(slide_source, claim_expected):
                        issues.append(_issue(code="semantic-asset-source-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/source_ref", message="claim source_ref does not match the slide's reviewed evidence binding", evidence={"candidate_id": candidate_id, "claim_source_ref": slide_source, "binding_page": binding_page, "binding_locator": binding_locator}))
                if isinstance(record_page, int) and isinstance(binding.get("source_page"), int) and record_page != binding.get("source_page") and not has_claim_binding:
                    issues.append(_issue(code="semantic-asset-source-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_evidence_binding/source_page", message="visible asset page does not match the selected digest page", evidence={"candidate_id": candidate_id, "asset_page": record_page, "slide_page": binding.get("source_page")}))
                record_caption = _text(record.get("caption", record.get("label", "")))
                if figure_caption and record_caption and len(_tokens(figure_caption) & _tokens(record_caption)) < 2:
                    issues.append(_issue(code="semantic-asset-caption-mismatch", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/figure/caption", message="visible asset caption does not describe the selected digest asset", evidence={"candidate_id": candidate_id, "asset_caption": record_caption, "visible_caption": figure_caption}))
            graph_node = next((node for node in graph_visible_nodes if _portable_path(node.get("path")) == _portable_path(src)), None) if src else None
            if src and graph_node is None:
                issues.append(_issue(code="semantic-asset-graph-mismatch", severity="error", slide_index=index, pointer=src_pointer or f"/slides/{index - 1}/figure/src", message="visible asset source is not a graph-declared visible asset", evidence={"src": src, "candidate_id": candidate_id}, action="Use the graph-declared source path for the visible asset."))
            elif strict and src and graph_node is not None:
                if role == "title" and figure and figure.get("hero") is True and not _title_hero_compatible(record, graph_node):
                    issues.append(_issue(
                        code="semantic-title-hero-mismatch",
                        severity="error",
                        slide_index=index,
                        pointer=f"/slides/{index - 1}/figure/hero",
                        message="title hero asset lacks explicit title-compatible digest or graph metadata",
                        evidence={"candidate_id": candidate_id, "src": src},
                        action="Use no hero or bind a digest asset explicitly marked title, overview, framework, teaser, or title-page compatible.",
                    ))
                # The graph may contain several visible assets, so checking only
                # that ``src`` exists is insufficient: a slide can silently point
                # at another graph asset while retaining its original candidate.
                # Bind the rendered path to the selected digest identity (or the
                # exact path declared by that digest record) and, when present,
                # to the graph's canonical source pointer for this slide field.
                declared_paths = _declared_asset_paths(record)
                if declared_paths:
                    canonical_nodes = [
                        node for node in graph_visible_nodes
                        if _portable_path(node.get("path")) in declared_paths
                    ]
                else:
                    canonical_nodes = [
                        node for node in graph_visible_nodes
                        if _path_stem(node.get("path")) == candidate_id
                    ]
                source_pointers = graph_node.get("source_pointers")
                pointer_mismatch = (
                    isinstance(source_pointers, list)
                    and bool(source_pointers)
                    and src_pointer not in source_pointers
                )
                if len(canonical_nodes) != 1 or graph_node is not canonical_nodes[0] or pointer_mismatch:
                    issues.append(_issue(
                        code="semantic-asset-identity-mismatch",
                        severity="error",
                        slide_index=index,
                        pointer=src_pointer or f"/slides/{index - 1}/figure/src",
                        message="visible asset source does not match the selected digest candidate and graph binding",
                        evidence={
                            "candidate_id": candidate_id,
                            "src": src,
                            "expected_paths": sorted(_portable_path(node.get("path")) for node in canonical_nodes),
                            "graph_source_pointers": source_pointers if isinstance(source_pointers, list) else [],
                        },
                        action="Use the selected digest asset's canonical rendered path and regenerate the bound asset graph.",
                    ))
        elif src and strict:
            issues.append(_issue(code="semantic-asset-selection-missing", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/{rendered_asset_field or 'figure'}", message="visible asset has no matcher selection record", evidence={"src": src}, action="Record the matcher-approved candidate and evidence locator."))
    for candidate_id, count in sorted(selected_counts.items()):
        if count > 1:
            severity = "error" if count >= 3 else "warning"
            issues.append(_issue(code="semantic-asset-reuse", severity=severity, slide_index=min(selected_slides[candidate_id]), pointer="/slides", message="the same evidence asset is reused across slides", evidence={"candidate_id": candidate_id, "count": count, "slides": selected_slides[candidate_id]}, action="Use distinct evidence or document a deliberate reuse for human review."))
    for family, indexes in sorted(family_slides.items()):
        distinct = len({candidate for candidate in selected_counts if candidate.split("-", 1)[0].casefold() == family})
        if distinct >= 4:
            issues.append(_issue(code="semantic-asset-series-overuse", severity="warning", slide_index=min(indexes), pointer="/slides", message="too many slides draw from one asset series", evidence={"series": family, "distinct_assets": distinct, "slides": sorted(indexes)}, action="Vary the evidence series or explain the repeated visual grammar during human review."))


def _notes_checks(
    slides: list[Mapping[str, Any]], notes: Any, digest: Mapping[str, Any], issues: list[dict[str, Any]], strict: bool,
    speaker_schema: str,
) -> None:
    if isinstance(notes, Mapping):
        values = notes.get("slides", notes.get("notes", notes.get("speaker_notes", [])))
    else:
        values = notes
    if not isinstance(values, list):
        values = []
    if strict and len(values) != len(slides):
        issues.append(_issue(code="semantic-notes-missing", severity="error", slide_index=0, pointer="/slides", message="speaker notes must have one entry per slide", evidence={"slide_count": len(slides), "note_count": len(values)}, action="Regenerate structured speaker notes for every slide."))
    structured = bool(values) and all(isinstance(value, Mapping) and isinstance(value.get("speaker_content"), Mapping) for value in values)
    modern = speaker_schema == "speaker-content-v1"
    legacy = speaker_schema == "legacy-v1"
    if strict and not modern and not legacy and not structured:
        issues.append(_issue(code="speaker-content-required", severity="error", slide_index=0, pointer="/meta/speaker_notes_schema", message="reviewed decks require structured speaker content or an explicit legacy-v1 schema", action="Generate speaker_content fields or explicitly declare legacy-v1 before review."))
    if strict and modern and not structured:
        for index in range(1, len(slides) + 1):
            value = values[index - 1] if index <= len(values) else None
            if not isinstance(value, Mapping) or not isinstance(value.get("speaker_content"), Mapping):
                issues.append(_issue(code="speaker-content-required", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_content", message="speaker-content-v1 requires structured speaker_content on every slide", action="Generate structured speaker content and keep speaker_notes as its exact projection."))
    if structured and values:
        findings = validate_speaker_notes(slides, values, digest)
        for finding in findings:
            severity = "error" if finding.severity in {"P0", "P1"} else "warning"
            slide_index = int(finding.slide or 0)
            issues.append(_issue(code=finding.check, severity=severity, slide_index=slide_index, pointer=f"/slides/{max(slide_index - 1, 0)}/speaker_notes", message=finding.detail, evidence={"check": finding.check}, action="Rewrite the spoken note as a complete, natural, source-grounded delivery sentence."))
    for index, value in enumerate(values, 1):
        if isinstance(value, Mapping):
            text = _text(value.get("speaker_notes"))
            if not text and isinstance(value.get("speaker_content"), Mapping):
                text = _text(value["speaker_content"].get("text"))
        else:
            text = _text(value)
        if not text:
            continue
        slide = slides[index - 1] if index <= len(slides) and isinstance(slides[index - 1], Mapping) else {}
        if _INTERNAL_RE.search(text):
            issues.append(_issue(code="semantic-internal-delivery-language", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_notes", message="speaker delivery exposes internal review or processing language", evidence={"matched": _INTERNAL_RE.search(text).group(0)} if _INTERNAL_RE.search(text) else {}, action="Remove checkpoint, audit, hash, binding, and system-process wording from presenter notes."))
        # Count across the complete note so an English-first sentence followed by
        # Chinese (or the reverse) is treated as one mixed delivery run.
        if _CJK_RE.search(text) and _has_untranslated_latin(text, slide):
            issues.append(_issue(code="speaker-fluency-language-mix", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_notes", message="speaker delivery mixes a substantial untranslated English run with Chinese", evidence={"latin_word_count": len(_LATIN_WORD_RE.findall(text))}, action="Use one delivery language or translate the full spoken sentence."))
        sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])", text) if part.strip()]
        for sentence in sentences:
            body = sentence.rstrip("。！？.!? ")
            if _CJK_RE.search(body) and _has_untranslated_latin(body, slide):
                issues.append(_issue(code="speaker-fluency-language-mix", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_notes", message="speaker delivery mixes a substantial untranslated English run with Chinese", evidence={"latin_word_count": len(_LATIN_WORD_RE.findall(body))}, action="Use one delivery language or translate the full spoken sentence."))
            if _TERMINAL_RE.search(sentence) is None and len(body) <= 24:
                issues.append(_issue(code="speaker-fluency-fragment", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_notes", message="speaker delivery ends with an incomplete sentence", evidence={"ending": body[-24:]}, action="Finish the spoken thought as a complete sentence."))
            elif _CONNECTIVE_RE.fullmatch(body):
                issues.append(_issue(code="speaker-fluency-fragment", severity="error", slide_index=index, pointer=f"/slides/{index - 1}/speaker_notes", message="a standalone connective is not a complete spoken sentence", evidence={"ending": body}, action="Replace the connective with a complete statement."))


def _internal_checks(slides: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> None:
    for index, slide in enumerate(slides, 1):
        for value in _visible_slide_strings(slide):
            match = _INTERNAL_RE.search(value)
            if match:
                issues.append(_issue(code="semantic-internal-delivery-language", severity="error", slide_index=index, pointer=f"/slides/{index - 1}", message="visible slide content exposes internal review or processing language", evidence={"matched": match.group(0)}, action="Keep internal workflow labels out of viewer-facing slide text."))


def _quantitative_coverage_checks(
    slides: list[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    """Block readiness when any required quantitative fact is missing from visible text."""
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        requirement_id = _text(requirement.get("id"))
        if not requirement_id:
            continue
        assigned = [
            slide
            for slide in slides
            if requirement_id in {str(value) for value in slide.get("coverage_requirement_ids", []) or []}
        ]
        evidence = {
            "requirement_id": requirement_id,
            "label": _text(requirement.get("label")),
            "expected_tokens": list(requirement.get("coverage_tokens", []) or []),
        }
        if not assigned:
            issues.append(
                _issue(
                    code="semantic-quantitative-coverage-missing",
                    severity="error",
                    slide_index=0,
                    pointer=f"/quantitative_requirements/{requirement_id}",
                    message=f"required quantitative fact is not assigned to any visible slide: {requirement_id}",
                    evidence=evidence,
                )
            )
            continue
        missing = missing_coverage_tokens(requirement, visible_text(assigned))
        if missing:
            issues.append(
                _issue(
                    code="semantic-quantitative-coverage-missing",
                    severity="error",
                    slide_index=next(
                        (index for index, slide in enumerate(slides, 1) if slide in assigned),
                        0,
                    ),
                    pointer=f"/quantitative_requirements/{requirement_id}",
                    message=f"required quantitative fact is not visible with its context label: {requirement_id}",
                    evidence={**evidence, "missing_tokens": missing},
                )
            )


def _scientific_priority_checks(
    deck: Mapping[str, Any],
    slides: list[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    issues: list[dict[str, Any]],
) -> None:
    """Block research-question gaps and audit-only appendix promotion."""
    meta = deck.get("meta") if isinstance(deck.get("meta"), Mapping) else {}
    priority = meta.get("scientific_priority") if isinstance(meta.get("scientific_priority"), Mapping) else {}
    if priority.get("mode") == "research-question-aware":
        requirement_ids = {
            _text(requirement.get("id"))
            for requirement in requirements if isinstance(requirement, Mapping)
        }
        assigned_ids = {
            str(requirement_id)
            for slide in slides
            for requirement_id in slide.get("coverage_requirement_ids", []) or []
        }
        method_present = any(
            (_text(slide.get("semantic_role")) or _text(slide.get("role"))).casefold() == "method"
            and (
                _text(slide.get("origin_semantic_slot")).casefold() == "approach"
                or bool(slide.get("evidence_locators"))
            )
            for slide in slides
        )
        for coverage in priority.get("dimension_coverage", []) or []:
            if not isinstance(coverage, Mapping):
                continue
            dimension_id = _text(coverage.get("dimension_id")) or "unknown"
            status = _text(coverage.get("status")).casefold()
            evidence_ids = {
                str(value) for value in coverage.get("evidence_ids", []) or [] if str(value)
            }
            covered = (
                status == "evidence"
                and bool(evidence_ids)
                and evidence_ids <= requirement_ids
                and evidence_ids <= assigned_ids
            ) or (status == "method" and method_present)
            if not covered:
                issues.append(_issue(
                    code="research-question-evidence-coverage",
                    severity="error",
                    slide_index=0,
                    pointer=f"/meta/scientific_priority/dimension_coverage/{dimension_id}",
                    message=f"reviewed research dimension lacks visible method/result evidence: {dimension_id}",
                    evidence={
                        "dimension_id": dimension_id,
                        "status": status,
                        "evidence_ids": sorted(evidence_ids),
                    },
                    action="Assign source-bound method or result evidence to every reviewed research dimension.",
                ))
    promotion_terms = re.compile(
        r"interpretation-changing|no main-text equivalent|deep robustness|robustness-critical",
        re.IGNORECASE,
    )
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or requirement.get("source_scope") != "appendix":
            continue
        reason = _text(requirement.get("scientific_priority_reason"))
        tier = _text(requirement.get("priority_tier"))
        if not reason or not promotion_terms.search(reason) or tier == "tier-3-appendix-support":
            issues.append(_issue(
                code="appendix-priority",
                severity="error",
                slide_index=0,
                pointer=f"/quantitative_requirements/{_text(requirement.get('id')) or 'appendix'}",
                message="appendix evidence was promoted without a scientific priority condition",
                evidence={"reason": reason, "priority_tier": tier},
                action="Keep appendix evidence supporting/optional unless it changes interpretation, is robustness-critical, or has no main-text equivalent.",
            ))


def semantic_qa_is_current(
    report: Mapping[str, Any] | None, *, deck_sha256: str, digest_sha256: str, asset_graph_sha256: str,
    coverage_requirements_sha256: str | None = None,
) -> bool:
    """Return whether a report is bound to the exact canonical semantic inputs."""
    if not isinstance(report, Mapping) or report.get("schema_version") != SCHEMA_VERSION or report.get("kind") != KIND:
        return False
    inputs = report.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    reported_coverage = inputs.get("coverage_requirements_sha256")
    expected_coverage = coverage_requirements_sha256 or ""
    coverage_matches = (
        reported_coverage == expected_coverage
        if expected_coverage
        else reported_coverage in (None, "")
    )
    return (
        inputs.get("deck_sha256") == deck_sha256
        and inputs.get("digest_sha256") == digest_sha256
        and inputs.get("asset_graph_sha256") == asset_graph_sha256
        and coverage_matches
    )


def evaluate_semantic_qa(
    deck: Mapping[str, Any], digest: Mapping[str, Any], asset_graph: Mapping[str, Any], notes: Any, *,
    deck_sha256: str, digest_sha256: str, asset_graph_sha256: str,
    confirmed_metadata: Mapping[str, Any] | None = None,
    coverage_requirements: Sequence[Mapping[str, Any]] | None = None,
    coverage_requirements_sha256: str | None = None,
) -> dict[str, Any]:
    """Evaluate deterministic semantic contracts and return a stable QA payload."""
    deck = deck if isinstance(deck, Mapping) else {}
    digest = digest if isinstance(digest, Mapping) else {}
    asset_graph = asset_graph if isinstance(asset_graph, Mapping) else {}
    slides_raw = deck.get("slides")
    slides = [slide for slide in slides_raw if isinstance(slide, Mapping)] if isinstance(slides_raw, list) else []
    strict = _contract_present(deck, digest)
    meta = deck.get("meta") if isinstance(deck.get("meta"), Mapping) else {}
    speaker_schema = _text(meta.get("speaker_notes_schema"))
    issues: list[dict[str, Any]] = []
    _deck_type_checks(meta, issues, strict)
    _title_checks(deck, digest, issues, strict, confirmed_metadata)
    _role_layout_checks(slides, issues, strict)
    _role_content_checks(slides, issues, strict)
    _method_completeness_checks(slides, digest, issues, strict)
    _representation_semantics_checks(slides, issues, strict)
    _role_source_consistency_checks(slides, digest, issues, strict)
    _method_evidence_identity_checks(slides, digest, issues, strict)
    _native_table_schema_checks(slides, issues, strict)
    _discussion_specificity_checks(slides, issues, strict)
    _role_semantic_checks(slides, digest, issues, strict)
    _discussion_semantic_checks(slides, issues, strict)
    _conclusion_semantic_checks(slides, digest, deck, issues, strict)
    _split_table_row_checks(slides, issues, strict)
    _role_duplicate_checks(slides, issues, strict)
    _audience_projection_checks(slides, issues, strict)
    _provenance_checks(slides, issues, strict)
    _asset_checks(slides, digest, asset_graph, issues, strict)
    _notes_checks(slides, notes, digest, issues, strict, speaker_schema)
    _internal_checks(slides, issues)
    _quantitative_coverage_checks(slides, coverage_requirements or [], issues)
    _scientific_priority_checks(deck, slides, coverage_requirements or [], issues)
    issues.sort(key=lambda item: (int(item.get("slide_index", 0)), str(item.get("code", "")), str(item.get("json_pointer", "")), str(item.get("message", ""))))
    summary = {
        "errors": sum(item["severity"] == "error" for item in issues),
        "warnings": sum(item["severity"] == "warning" for item in issues),
        "info": sum(item["severity"] == "info" for item in issues),
    }
    reuse_codes = {"semantic-asset-reuse", "semantic-asset-series-overuse"}
    source_asset_codes = {item["code"] for item in issues if item["code"].startswith("semantic-asset-") and item["code"] not in reuse_codes}
    source_asset_codes.update({"semantic-table-binding-invalid"})
    provenance_codes = {
        "semantic-provenance-claim-incomplete",
        "semantic-provenance-claim-mismatch",
        "semantic-provenance-claim-locator-mismatch",
        "semantic-provenance-visual-incomplete",
        "semantic-provenance-visual-citation-missing",
        "semantic-provenance-visual-mismatch",
        "semantic-provenance-support-mismatch",
        "audience-provenance-role-unclear",
        "audience-provenance-duplicate-label",
    }
    audience_codes = {
        "audience-visible-duplicate",
        "audience-title-body-duplication",
        "audience-raw-evidence-projection",
        "audience-pdf-hyphenation",
        "audience-internal-process-leak",
        "audience-provenance-role-unclear",
        "audience-provenance-duplicate-label",
    }

    def check_for(codes: set[str]) -> str:
        matching = [item for item in issues if item["code"] in codes]
        if any(item["severity"] == "error" for item in matching):
            return "error"
        if any(item["severity"] == "warning" for item in matching):
            return "warning"
        return "pass"
    check_status = {
        "title_layout_metadata": check_for({item["code"] for item in issues if item["code"].startswith("semantic-title")}),
        "source_asset_alignment": check_for(source_asset_codes),
        "provenance_alignment": check_for(provenance_codes),
        "role_layout_policy": "error" if any(item["code"] in {"semantic-role-layout-mismatch", "semantic-role-content-missing", "semantic-role-duplicate", "semantic-role-source-mismatch", "semantic-method-evidence-duplicate", "semantic-table-schema", "semantic-discussion-specificity", "semantic-no-asset-policy", "semantic-required-asset-missing", "semantic-native-fallback-invalid", "semantic-representation-mismatch", "role-semantic-duplication", "role-solution-as-background", "role-solution-as-problem", "role-failure-as-problem", "role-result-as-background", "role-result-as-problem", "role-section-mismatch", "role-compatible-evidence-missing", "missing-role-has-speaker-binding", "discussion-provenance-as-scientific-claim", "discussion-duplicate-punctuation", "conclusion-full-matrix-repetition", "conclusion-semantic-completeness", "semantic-split-table-empty-row"} and item["severity"] == "error" for item in issues) else "pass",
        "role_content_completeness": check_for({"semantic-role-content-missing"}),
        "asset_reuse": check_for(reuse_codes),
        "delivery_language": check_for({"semantic-internal-delivery-language"}),
        "audience_projection": check_for(audience_codes),
        "quantitative_coverage": "error" if any(item["code"] == "semantic-quantitative-coverage-missing" and item["severity"] == "error" for item in issues) else "pass",
        "scientific_priority": check_for({"research-question-evidence-coverage", "appendix-priority"}),
        "conclusion_completeness": check_for({"conclusion-semantic-completeness"}),
        "notes_fluency": "error" if any((item["code"].startswith("speaker-") or item["code"] == "presenter-discussion-disclaimer") and item["severity"] == "error" for item in issues) else "pass",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "status": "error" if summary["errors"] else "pass",
        "inputs": {
            "deck_sha256": deck_sha256,
            "digest_sha256": digest_sha256,
            "asset_graph_sha256": asset_graph_sha256,
            "coverage_requirements_sha256": coverage_requirements_sha256 or "",
        },
        "summary": summary,
        "checks": check_status,
        "issues": issues,
        "human_review_required": True,
        "human_review_checklist": [
            "Confirm that each factual claim and visual points to the intended source locator.",
            "Confirm that the title metadata and narrative roles match the paper being discussed.",
            "Confirm that any intentional asset reuse or whitespace is acceptable for live delivery.",
        ],
    }
