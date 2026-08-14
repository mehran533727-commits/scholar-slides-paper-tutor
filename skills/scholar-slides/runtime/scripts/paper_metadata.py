"""Extract auditable paper metadata from an ingested PDF bundle.

The extractor deliberately reports uncertainty instead of completing bibliographic
fields from a filename, a search result, or a paper-specific lookup table.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Mapping


_ARXIV = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)
_ARXIV_STAMP = re.compile(
    r"arxiv\s*:\s*(\d{4}\.\d{4,5})(v\d+)?\s*\[[^]]+\](?:\s+([^\n]+))?",
    re.IGNORECASE,
)
_DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b", re.IGNORECASE)
_SECTION = re.compile(r"^(?:abstract|keywords?|index terms?|\d+(?:\.\d+)*\s+introduction)\b", re.IGNORECASE)
_CJK_SECTION_LABEL = re.compile(r"^(?:摘要|引言|关键词|关键字|参考文献|方法|实验|结论|目录)$")
_AFFILIATION = re.compile(r"\b(?:university|institute|department|laboratory|laboratories|school|college|centre|center|corporation|inc\.?|ltd\.?)\b", re.IGNORECASE)
_AFFILIATION_NUMBER = re.compile(r"(?<!\w)\d{1,2}\s+(?=[A-Za-z])")
_EMAIL = re.compile(r"@")
_LATIN_AUTHOR_PAIR = re.compile(r"\b([A-Z][A-Za-z'’-]+)\s+([A-Z][A-Za-z'’-]+)\b")
_SPLIT_ACRONYM = re.compile(r"\b([A-Z])\s+([A-Z]{2,})(?=\s+\d+D\b)")
_FOOTNOTE_MARKERS = re.compile(r"(?:\b\d+\b|[*†‡]+)")


def normalize_arxiv(value: object) -> tuple[str, str] | None:
    """Return ``(base_id, resolved_id)`` while retaining an explicit vN."""

    match = _ARXIV.search(str(value or ""))
    if not match:
        return None
    base = match.group(1)
    return base, f"{base}{match.group(2) or ''}"


def _first_page(ingest: Mapping[str, Any]) -> Mapping[str, Any]:
    pages = ingest.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, Mapping) and page.get("page") == 1:
                return page
    page_text = ingest.get("page_text")
    if isinstance(page_text, list):
        for page in page_text:
            if isinstance(page, Mapping) and page.get("page") == 1:
                return page
    return {"page": 1, "text": str(ingest.get("full_text") or "")}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _is_section_label(value: str) -> bool:
    """Recognize standalone structural labels, including common CJK punctuation."""
    normalized = value.strip().strip(":：;；.。．、 ")
    return bool(_SECTION.match(normalized) or _CJK_SECTION_LABEL.fullmatch(normalized))


def _evidence(status: str, source: str, locations: list[str], detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "source": source, "locations": locations}
    if detail:
        result["detail"] = detail
    return result


def _span_texts(block: Mapping[str, Any] | None, fallback: str) -> list[str]:
    """Keep the extractor's source strings available for a later human review."""

    if not isinstance(block, Mapping):
        return [fallback] if fallback else []
    spans = block.get("spans")
    if isinstance(spans, list):
        retained = [str(span.get("text")) for span in spans if isinstance(span, Mapping) and str(span.get("text") or "")]
        if retained:
            return retained
    raw = str(block.get("text") or "")
    return [raw] if raw else ([fallback] if fallback else [])


def _metadata_evidence(
    *, source: str, locations: list[str], original: str, proposed: object,
    spans: list[str], quality_flags: list[str], source_offsets: list[dict[str, int]] | None = None,
    missing: bool = False,
) -> dict[str, Any]:
    """Describe automatic extraction without claiming a human has confirmed it."""

    status = "MISSING" if missing else ("NEEDS_REVIEW" if quality_flags else "EXTRACTED")
    record = _evidence(status, source, locations)
    record.update({
        "original": original,
        "proposed": proposed,
        "spans": spans,
        "quality_flags": quality_flags,
    })
    if source_offsets:
        record["source_offsets"] = source_offsets
    return record


def _propose_title_correction(value: str) -> tuple[str, list[str]]:
    """Flag a generic split-acronym extraction artifact while keeping its source intact."""

    proposed = _SPLIT_ACRONYM.sub(lambda match: f"{match.group(1)}{match.group(2)}", value)
    return proposed, ["split_acronym"] if proposed != value else []


def _author_quality_flags(value: str, authors: list[str]) -> list[str]:
    flags: list[str] = []
    has_delimiter = bool(re.search(r"(?:,|、|\band\b|&|;)", value, flags=re.IGNORECASE))
    if len(authors) >= 2 and not has_delimiter:
        flags.append("merged_author_list")
    if _FOOTNOTE_MARKERS.search(value):
        flags.append("footnote_markers")
    return flags


def _blocks(page: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = page.get("blocks")
    return [item for item in raw if isinstance(item, Mapping) and _clean(item.get("text"))] if isinstance(raw, list) else []


def _combine_blocks(blocks: list[Mapping[str, Any]], indices: list[int]) -> Mapping[str, Any] | None:
    """Return one evidence block for a single block or a typography-grouped title."""

    if not indices:
        return None
    if len(indices) == 1:
        return blocks[indices[0]]
    selected = [blocks[index] for index in indices]
    texts = [_clean(block.get("text")) for block in selected if _clean(block.get("text"))]
    boxes = [block.get("bbox") for block in selected if isinstance(block.get("bbox"), (list, tuple)) and len(block.get("bbox")) >= 4]
    bbox = (
        min(float(box[0]) for box in boxes), min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes), max(float(box[3]) for box in boxes),
    ) if boxes else None
    return {
        "text": " ".join(texts),
        "bbox": bbox,
        # Keep one source span per visual line so the review record exposes the
        # grouping decision without leaking the extractor's word-level spacing.
        "spans": [{"text": text} for text in texts],
        "merged_title_blocks": list(indices),
    }


def _typography_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two nearby blocks plausibly belong to one identity block."""
    left_sizes = [
        float(span.get("size", 0))
        for span in left.get("spans", [])
        if isinstance(span, Mapping) and isinstance(span.get("size"), (int, float))
    ]
    right_sizes = [
        float(span.get("size", 0))
        for span in right.get("spans", [])
        if isinstance(span, Mapping) and isinstance(span.get("size"), (int, float))
    ]
    if not left_sizes or not right_sizes:
        return False
    reference_size = max(left_sizes)
    if abs(max(right_sizes) - reference_size) > max(0.5, reference_size * 0.08):
        return False
    left_box, right_box = left.get("bbox"), right.get("bbox")
    if not isinstance(left_box, (list, tuple)) or not isinstance(right_box, (list, tuple)):
        return False
    if len(left_box) < 4 or len(right_box) < 4:
        return False
    vertical_gap = float(right_box[1]) - float(left_box[3])
    if vertical_gap < -2.0 or vertical_gap > max(10.0, reference_size * 1.2):
        return False
    left_center = (float(left_box[0]) + float(left_box[2])) / 2
    right_center = (float(right_box[0]) + float(right_box[2])) / 2
    return (
        abs(float(right_box[0]) - float(left_box[0])) <= max(24.0, reference_size * 2.0)
        or abs(right_center - left_center) <= max(48.0, reference_size * 4.0)
    )


def _title_from_spans(blocks: list[Mapping[str, Any]]) -> tuple[str | None, int | None, list[int]]:
    scored: list[tuple[float, float, int, str]] = []
    # A title belongs in the opening layout region, not in a later large section
    # heading.  Bound this to the first three text blocks and a compact vertical
    # window anchored at the first text block.
    first_top = None
    for block in blocks:
        bbox = block.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
            first_top = float(bbox[1])
            break
    for index, block in enumerate(blocks[:12]):
        spans = block.get("spans")
        sizes = [float(span.get("size", 0)) for span in spans if isinstance(span, Mapping) and isinstance(span.get("size"), (int, float))] if isinstance(spans, list) else []
        bbox = block.get("bbox")
        top = float(bbox[1]) if isinstance(bbox, (list, tuple)) and len(bbox) >= 2 else 0.0
        text = _clean(block.get("text"))
        if first_top is not None and top > first_top + 180:
            continue
        if sizes and not _is_section_label(text) and not _ARXIV.search(text) and not _AFFILIATION.search(text) and len(text) <= 300:
            scored.append((max(sizes), -top, index, text))
    if not scored:
        return None, None, []
    best_size, _, index, text = max(scored)
    selected = [index]
    previous = blocks[index]
    previous_box = previous.get("bbox")
    for candidate_index in range(index + 1, min(len(blocks), index + 6)):
        candidate = blocks[candidate_index]
        candidate_text = _clean(candidate.get("text"))
        candidate_box = candidate.get("bbox")
        if not candidate_text or not isinstance(candidate_box, (list, tuple)) or len(candidate_box) < 4:
            break
        if _is_section_label(candidate_text) or _ARXIV.search(candidate_text) or _AFFILIATION.search(candidate_text) or _EMAIL.search(candidate_text):
            break
        if not isinstance(previous_box, (list, tuple)) or len(previous_box) < 4:
            break
        vertical_gap = float(candidate_box[1]) - float(previous_box[3])
        if vertical_gap < -1 or vertical_gap > max(12.0, best_size * 0.75):
            break
        sizes = [float(span.get("size", 0)) for span in candidate.get("spans", []) if isinstance(span, Mapping) and isinstance(span.get("size"), (int, float))]
        if not sizes or abs(max(sizes) - best_size) > max(0.5, best_size * 0.08):
            break
        previous_center = (float(previous_box[0]) + float(previous_box[2])) / 2
        candidate_center = (float(candidate_box[0]) + float(candidate_box[2])) / 2
        left_aligned = abs(float(candidate_box[0]) - float(previous_box[0])) <= max(18.0, best_size * 1.5)
        center_aligned = abs(candidate_center - previous_center) <= max(48.0, best_size * 3.0)
        if not (left_aligned or center_aligned):
            break
        selected.append(candidate_index)
        previous = candidate
        previous_box = candidate_box
    merged = _combine_blocks(blocks, selected)
    return _clean(merged.get("text")) if isinstance(merged, Mapping) else text, selected[-1], selected


def _text_line_records(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return normalized lines with their untouched page-text source ranges."""

    records: list[dict[str, Any]] = []
    start = 0
    for number, segment in enumerate(str(page.get("text") or "").splitlines(keepends=True), start=1):
        raw = segment.rstrip("\r\n")
        normalized = _clean(raw)
        if normalized:
            records.append({"text": normalized, "raw": raw, "start": start, "end": start + len(raw), "line": number})
        start += len(segment)
    return records


def _text_lines(page: Mapping[str, Any]) -> list[str]:
    return [record["text"] for record in _text_line_records(page)]


def _line_offset(record: Mapping[str, Any] | None) -> list[dict[str, int]]:
    if not isinstance(record, Mapping):
        return []
    return [{key: int(record[key]) for key in ("start", "end", "line")}]


def _looks_like_authors(value: str) -> bool:
    if not value or _is_section_label(value) or _ARXIV.search(value) or _DOI.search(value) or _EMAIL.search(value) or _AFFILIATION.search(value):
        return False
    # Author lines conventionally contain names with at least one capitalized word;
    # reject prose sentences to avoid turning arbitrary title-page text into people.
    if len(value) > 300 or value.endswith("."):
        return False
    token = r"(?:[A-Z][A-Za-z'’-]+|[A-Z]\.)"
    latin_name = rf"{token}(?:\s+{token}){{1,3}}"
    cjk_name = r"[\u3400-\u9fff]{2,4}"
    # Conference PDFs often place affiliation superscripts between adjacent names
    # and lose commas during text extraction (e.g. ``Ada Lovelace 1 2 Alan Turing 1 2``).
    # Recover only a line containing at least two complete capitalized name pairs;
    # ordinary prose still fails this bounded author-shape check.
    stripped_markers = re.sub(r"[0-9*†‡]+", " ", value)
    if len(_LATIN_AUTHOR_PAIR.findall(stripped_markers)) >= 2:
        return True
    pieces = [item.strip(" *†‡0123456789") for item in re.split(r"\s*(?:,|、|\band\b|&|;)\s*", value, flags=re.IGNORECASE)]
    return bool(pieces) and all(re.fullmatch(rf"(?:{latin_name}|{cjk_name})", item) for item in pieces if item) and any(pieces)


def _split_authors(value: str) -> list[str]:
    stripped_markers = re.sub(r"[0-9*†‡]+", " ", value)
    pairs = [f"{first} {last}" for first, last in _LATIN_AUTHOR_PAIR.findall(stripped_markers)]
    if len(pairs) >= 2:
        return pairs
    pieces = re.split(r"\s*(?:,|、|\band\b|&|;|\n)\s*", value, flags=re.IGNORECASE)
    return [piece.strip(" *†‡0123456789") for piece in pieces if _looks_like_authors(piece.strip(" *†‡0123456789"))]


def _affiliation_parts(value: str) -> list[str]:
    """Split packed or line-preserved affiliation text into source-grounded entries."""

    parts: list[str] = []
    raw_value = str(value or "")
    numbered_affiliation_block = bool(_AFFILIATION.search(raw_value) and _AFFILIATION_NUMBER.search(raw_value))
    for raw_line in raw_value.replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        markers = list(_AFFILIATION_NUMBER.finditer(line))
        starts = [0] + [match.start() for match in markers[1:]] if markers and markers[0].start() > 0 else [match.start() for match in markers]
        chunks = [line[start:end] for start, end in zip(starts, starts[1:] + [len(line)])] if starts else [line]
        for chunk in chunks:
            email = _EMAIL.search(chunk)
            if email:
                chunk = chunk[: email.start()]
            cleaned = _clean(chunk)
            if cleaned and (_AFFILIATION.search(cleaned) or (numbered_affiliation_block and _AFFILIATION_NUMBER.match(cleaned))):
                parts.append(cleaned)
    return parts


def _local_hash(source: Mapping[str, Any], ingest: Mapping[str, Any]) -> tuple[str | None, str | None]:
    if _clean(source.get("source_kind")) != "local_pdf":
        return None, None
    candidate = source.get("pdf") or source.get("source_input") or ingest.get("path")
    if candidate and os.path.isfile(str(candidate)):
        digest = hashlib.sha256(Path(str(candidate)).read_bytes()).hexdigest()
        return digest, "local PDF bytes"
    return None, None


def extract_paper_metadata(ingest: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Return one reusable, evidence-bearing metadata object for a paper bundle."""

    page = _first_page(ingest)
    blocks = _blocks(page)
    line_records = _text_line_records(page)
    lines = [record["text"] for record in line_records]
    flags: list[str] = []
    title, title_index, title_block_indices = _title_from_spans(blocks)
    title_line_record: Mapping[str, Any] | None = None
    title_status = "EXTRACTED" if title else "MISSING"
    title_source = "first-page spans" if title else "none"
    if not title:
        document_title = _clean((ingest.get("meta") or {}).get("title") if isinstance(ingest.get("meta"), Mapping) else "")
        if document_title:
            title, title_status, title_source = document_title, "NEEDS_REVIEW", "PDF document metadata"
        elif lines:
            title, title_status, title_source = lines[0], "NEEDS_REVIEW", "first-page text"
            title_line_record = line_records[0]
        else:
            title = "[MISSING: paper title]"
    title_proposed, title_quality_flags = _propose_title_correction(title)
    if title_status == "NEEDS_REVIEW" and not title_quality_flags:
        title_quality_flags = ["weak_source"]
    if title_quality_flags:
        title_status = "NEEDS_REVIEW"
    if title_status != "EXTRACTED":
        flags.append(f"[{'MISSING' if title_status == 'MISSING' else 'UNVERIFIED'}: paper title is not reliably identified]")

    author_line: str | None = None
    author_block: Mapping[str, Any] | None = None
    author_block_indices: list[int] = []
    author_line_record: Mapping[str, Any] | None = None
    ambiguous_author_candidate = False
    if title_index is not None:
        title_bbox = blocks[title_index].get("bbox")
        title_bottom = float(title_bbox[3]) if isinstance(title_bbox, (list, tuple)) and len(title_bbox) >= 4 else None
        previous_author_block: Mapping[str, Any] | None = None
        for block_index in range(title_index + 1, min(len(blocks), title_index + 8)):
            block = blocks[block_index]
            candidate = _clean(block.get("text"))
            bbox = block.get("bbox")
            top = float(bbox[1]) if isinstance(bbox, (list, tuple)) and len(bbox) >= 2 else None
            if title_bottom is not None and top is not None and top - title_bottom > 100:
                continue
            if _is_section_label(candidate) or _ARXIV.search(candidate) or _EMAIL.search(candidate):
                if author_block_indices:
                    break
                ambiguous_author_candidate = True
                continue
            if _AFFILIATION.search(candidate):
                if author_block_indices:
                    break
                continue
            if _looks_like_authors(candidate) and (
                previous_author_block is None or _typography_compatible(previous_author_block, block)
            ):
                author_block_indices.append(block_index)
                previous_author_block = block
                author_line = _clean(" ".join(_clean(blocks[index].get("text")) for index in author_block_indices))
                author_block = _combine_blocks(blocks, author_block_indices)
                continue
            if not author_block_indices and candidate:
                ambiguous_author_candidate = True
                continue
            if author_block_indices:
                break
    title_parts = {
        _clean(blocks[index].get("text"))
        for index in title_block_indices
        if 0 <= index < len(blocks)
    }
    title_position = max((index for index, line in enumerate(lines) if line in title_parts), default=-1)
    if title_position < 0 and title_line_record is not None:
        title_position = next((index for index, record in enumerate(line_records) if record is title_line_record), -1)
    if author_line is None:
        for index, candidate in enumerate(lines[title_position + 1: title_position + 5], start=title_position + 1):
            if _looks_like_authors(candidate):
                author_line = candidate
                author_line_record = line_records[index]
                break
    if author_line is None and len(title_block_indices) > 1:
        # A grouped opening title proves that layout continuity was present, but
        # it does not prove that the next identity block is an author line.
        # Keep the CKPT-1 decision human-reviewable instead of silently calling it
        # a clean missing-author case.
        ambiguous_author_candidate = True
    authors = _split_authors(author_line or "")
    author_adjacent_to_text_title = bool(authors and title_position >= 0 and author_line in lines and 0 < lines.index(author_line) - title_position <= 3)
    author_status = "EXTRACTED" if authors and (title_index is not None or author_adjacent_to_text_title) else ("NEEDS_REVIEW" if authors or ambiguous_author_candidate else "MISSING")
    author_quality_flags = _author_quality_flags(author_line or "", authors)
    if author_status == "NEEDS_REVIEW" and not author_quality_flags:
        author_quality_flags = ["weak_source"] if authors else ["ambiguous_candidate"]
    if author_quality_flags:
        author_status = "NEEDS_REVIEW"
    if author_status != "EXTRACTED":
        flags.append(f"[{'MISSING' if author_status == 'MISSING' else 'UNVERIFIED'}: authors are not reliably identified]")

    affiliations: list[str] = []
    if author_block_indices:
        for block in blocks[author_block_indices[-1] + 1: author_block_indices[-1] + 4]:
            block_text = str(block.get("text") or "")
            if _SECTION.search(block_text) or _ARXIV.search(block_text):
                break
            affiliations.extend(_affiliation_parts(block_text))
            if affiliations and _EMAIL.search(block_text):
                break
    if not affiliations and author_line:
        candidates = [line for line in lines[lines.index(author_line) + 1:] if not _SECTION.match(line)] if author_line in lines else []
        for line in candidates[:3]:
            affiliations.extend(_affiliation_parts(line))
    affiliations = list(dict.fromkeys(affiliations))

    stamp = _ARXIV_STAMP.search(str(page.get("text") or ""))
    source_arxiv = normalize_arxiv(source.get("source_input")) or normalize_arxiv((ingest.get("meta") or {}).get("arxiv_id") if isinstance(ingest.get("meta"), Mapping) else None)
    if stamp:
        base, suffix = stamp.group(1), stamp.group(2) or ""
        arxiv = (base, f"{base}{suffix}")
        version_status, version_source = "VERIFIED", "PDF first-page arXiv stamp"
        date_value = _clean(stamp.group(3)) or None
    else:
        arxiv = source_arxiv
        version_status = "UNVERIFIED" if arxiv and arxiv[1] != arxiv[0] else "MISSING"
        version_source, date_value = ("source input", None) if arxiv else ("none", None)
        if arxiv and arxiv[1] != arxiv[0]:
            flags.append("[UNVERIFIED: arXiv version comes from source input, not PDF evidence]")
    identifiers: dict[str, Any] = {"arxiv": None, "doi": None}
    version = {"base": None, "resolved": None}
    if arxiv:
        base, resolved = arxiv
        identifiers["arxiv"] = {"base_id": base, "resolved_id": resolved, "url": f"https://arxiv.org/abs/{resolved}"}
        version = {"base": base, "resolved": resolved}
    doi_match = _DOI.search(str(page.get("text") or ""))
    if doi_match:
        identifiers["doi"] = doi_match.group(1).rstrip(".,;)")

    local_hash, local_hash_source = _local_hash(source, ingest)
    supplied_hash = _clean(source.get("source_sha256"))
    pdf_hash = local_hash or supplied_hash or "[MISSING: PDF SHA-256]"
    hash_status = "VERIFIED" if local_hash or re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash) else "MISSING"
    if hash_status != "VERIFIED":
        flags.append("[MISSING: PDF SHA-256]")
    title_block = _combine_blocks(blocks, title_block_indices)
    evidence = {
        "title": _metadata_evidence(
            source=title_source,
            locations=["p. 1"] if title_status != "MISSING" else [],
            original=str(title_block.get("text")) if isinstance(title_block, Mapping) else (str(title_line_record["raw"]) if title_line_record else title),
            proposed=title_proposed,
            spans=_span_texts(title_block, str(title_line_record["raw"]) if title_line_record else title),
            quality_flags=title_quality_flags,
            source_offsets=_line_offset(title_line_record),
            missing=title_status == "MISSING",
        ),
        "authors": _metadata_evidence(
            source="first-page spans" if author_block is not None else ("first-page text" if authors else "none"),
            locations=["p. 1"] if authors else [],
            original=str(author_block.get("text")) if isinstance(author_block, Mapping) else (str(author_line_record["raw"]) if author_line_record else (author_line or "")),
            proposed=authors,
            spans=_span_texts(author_block, str(author_line_record["raw"]) if author_line_record else (author_line or "")),
            quality_flags=author_quality_flags,
            source_offsets=_line_offset(author_line_record),
            missing=author_status == "MISSING",
        ),
        "affiliations": _evidence("VERIFIED" if affiliations else "MISSING", "first-page text" if affiliations else "none", ["p. 1"] if affiliations else []),
        "identifiers": _evidence("VERIFIED" if stamp or doi_match else "MISSING", "PDF first page" if stamp or doi_match else "none", ["p. 1"] if stamp or doi_match else []),
        "version": _evidence(version_status, version_source, ["p. 1"] if stamp else []),
        "dates": _evidence("VERIFIED" if date_value else "MISSING", "PDF first-page arXiv stamp" if date_value else "none", ["p. 1"] if date_value else []),
        "pdf_sha256": _evidence(hash_status, local_hash_source or ("source manifest" if supplied_hash else "none"), [], None),
    }
    return {
        "title": title,
        "authors": authors,
        "affiliations": affiliations,
        "identifiers": identifiers,
        "version": version,
        "dates": {"arxiv_stamp": date_value} if date_value else {},
        "pdf_sha256": pdf_hash,
        "evidence": evidence,
        "flags": list(dict.fromkeys(flags)),
    }


def validate_metadata_for_ckpt1(metadata: Mapping[str, Any]) -> list[str]:
    """Return CKPT-1 blockers for identity fields that cannot be trusted."""

    blockers = []
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
    for field in ("title", "authors"):
        record = evidence.get(field) if isinstance(evidence, Mapping) else None
        status = record.get("status") if isinstance(record, Mapping) else None
        value = metadata.get(field)
        if status not in {"EXTRACTED", "HUMAN_CONFIRMED", "VERIFIED"} or not value or (isinstance(value, str) and value.startswith("[MISSING:")):
            blockers.append(f"CKPT-1 metadata blocker: {field} is {status or 'UNVERIFIED'}")
    return blockers


_PREPARATION_REVIEWABLE_STATUSES = {"EXTRACTED", "VERIFIED", "HUMAN_CONFIRMED"}


def _preparation_value(metadata: Mapping[str, Any], field: str) -> Any:
    """Return the automatic value only when it is structurally usable."""

    value = metadata.get(field)
    if field == "authors":
        if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
            return value
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None


def _correction_proposal_is_valid(correction: Mapping[str, Any], field: str) -> bool:
    proposed = correction.get("proposed")
    if field == "authors":
        return isinstance(proposed, list) and len(proposed) > 0 and all(isinstance(item, str) and item.strip() for item in proposed)
    return isinstance(proposed, str) and bool(proposed.strip())


def validate_metadata_for_ckpt1_preparation(
    metadata: Mapping[str, Any],
    review_candidate: Mapping[str, Any] | None = None,
    bound_audits: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> list[str]:
    """Return CKPT-1 preparation blockers for paper identity fields.

    EXTRACTED/VERIFIED/HUMAN_CONFIRMED automatic metadata proceeds without a
    correction.  NEEDS_REVIEW (and MISSING with a schema-supported replacement)
    proceeds only when the review candidate carries a current, source-bound
    correction overlay for the exact field: the correction must preserve the
    automatic original, propose a nonblank value, carry source evidence and a
    reason, identify an agent preparer, and be backed by a resolved evidence
    audit bound to the digest marker.  The extractive digest is never mutated;
    corrected values are exposed only by the resolved view after explicit human
    approval.
    """

    if not isinstance(metadata, Mapping):
        return ["CKPT-1 metadata blocker: paper_metadata is MISSING"]
    evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), Mapping) else {}
    flags = metadata.get("flags") if isinstance(metadata.get("flags"), list) else []
    candidate = review_candidate if isinstance(review_candidate, Mapping) else {}
    corrections = candidate.get("metadata_corrections") if isinstance(candidate.get("metadata_corrections"), Mapping) else {}
    resolutions = {
        item.get("marker"): item
        for item in candidate.get("marker_resolutions", [])
        if isinstance(item, Mapping) and isinstance(item.get("marker"), str)
    }
    prepared_by = candidate.get("prepared_by")
    blockers: list[str] = []
    for field in ("title", "authors"):
        record = evidence.get(field) if isinstance(evidence, Mapping) else None
        status = record.get("status") if isinstance(record, Mapping) else None
        if status in _PREPARATION_REVIEWABLE_STATUSES:
            if _preparation_value(metadata, field) is not None:
                continue
            blockers.append(f"CKPT-1 metadata blocker: {field} is {status or 'UNVERIFIED'}")
            continue
        if status not in {"NEEDS_REVIEW", "MISSING"}:
            blockers.append(f"CKPT-1 metadata blocker: {field} is {status or 'UNVERIFIED'}")
            continue
        correction = corrections.get(field)
        if not isinstance(correction, Mapping):
            blockers.append(f"CKPT-1 metadata blocker: {field} is {status or 'UNVERIFIED'}")
            continue
        if correction.get("original") != metadata.get(field):
            blockers.append(f"CKPT-1 metadata blocker: {field} correction original does not match the automatic value")
            continue
        if not _correction_proposal_is_valid(correction, field):
            blockers.append(f"CKPT-1 metadata blocker: {field} correction proposed value is invalid")
            continue
        evidence_ref = correction.get("evidence")
        if (
            not isinstance(evidence_ref, Mapping)
            or not isinstance(evidence_ref.get("page"), int)
            or evidence_ref.get("page", 0) < 1
            or not isinstance(evidence_ref.get("locator"), str)
            or not evidence_ref["locator"].strip()
        ):
            blockers.append(f"CKPT-1 metadata blocker: {field} correction requires source evidence")
            continue
        if not isinstance(correction.get("reason"), str) or not correction["reason"].strip():
            blockers.append(f"CKPT-1 metadata blocker: {field} correction requires a reason")
            continue
        if (
            not isinstance(prepared_by, Mapping)
            or prepared_by.get("kind") != "agent"
            or not isinstance(prepared_by.get("name"), str)
            or not prepared_by["name"].strip()
        ):
            blockers.append(f"CKPT-1 metadata blocker: {field} correction requires an agent preparer identity")
            continue
        marker = next(
            (
                flag
                for flag in flags
                if f"{field} is not reliably identified" in flag
                or f"{field} are not reliably identified" in flag
            ),
            None,
        )
        if marker is None:
            blockers.append(f"CKPT-1 metadata blocker: {field} is {status} without a correction-bound digest marker")
            continue
        decision = resolutions.get(marker)
        audit_ref = decision.get("audit_ref") if isinstance(decision, Mapping) else None
        if (
            not isinstance(decision, Mapping)
            or decision.get("resolution") != "resolved_with_audit"
            or not isinstance(audit_ref, Mapping)
            or not isinstance(audit_ref.get("path"), str)
            or not isinstance(audit_ref.get("sha256"), str)
        ):
            blockers.append(f"CKPT-1 metadata blocker: {field} correction is not bound by a resolved evidence audit")
            continue
        if bound_audits is not None and (audit_ref["path"], audit_ref["sha256"]) not in bound_audits:
            blockers.append(f"CKPT-1 metadata blocker: {field} correction audit is not current")
            continue
    return blockers
