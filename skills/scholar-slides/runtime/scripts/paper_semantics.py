"""Generic, source-bound paper semantics for the CKPT-1 review boundary.

This module is intentionally upstream of deck planning.  It extracts bounded
sentences from persisted PDF page text, applies section-aware priors, and keeps
the selected text plus page/section/locator metadata together.  It is not a
paper summarizer and contains no paper-specific identifiers or claims.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import re
from typing import Any


REQUIRED_SLOTS = (
    "context",
    "objective_or_research_question",
    "motivation_or_gap",
    "problem_setup",
    "approach",
    "contributions",
    "experimental_setup",
    "main_results",
    "limitations_or_failure_modes",
)

_MODE_B_REQUIRED = (
    "context",
    "objective_or_research_question",
    "approach",
    "main_results",
    "limitations_or_failure_modes",
)

_SECTION_PRIORS: dict[str, dict[str, int]] = {
    "context": {
        "Introduction": 8,
        "Background": 9,
        "Related Work": 7,
        "Abstract": 3,
        "Problem Setup": 2,
        "Method": -6,
        "System Overview": -6,
        "Experiments": -5,
        "Experimental Setup": -5,
        "Results": -7,
        "Failure Analysis": -7,
        "Conclusion and Limitations": -5,
    },
    "objective_or_research_question": {
        "Problem Setup": 9,
        "Task Formulation": 9,
        "Introduction": 5,
        "Abstract": 4,
        "Method": 1,
        "System Overview": 0,
        "Results": -5,
        "Conclusion and Limitations": -4,
    },
    "motivation_or_gap": {
        "Introduction": 10,
        "Background": 8,
        "Related Work": 8,
        "Problem Setup": 3,
        "Abstract": 1,
        "Method": -5,
        "System Overview": -6,
        "Experiments": -6,
        "Results": -8,
        "Failure Analysis": -7,
        "Conclusion and Limitations": -6,
    },
    "problem_setup": {
        "Problem Setup": 11,
        "Task Formulation": 11,
        "Introduction": 3,
        "Method": 2,
        "System Overview": 1,
        "Abstract": 1,
        "Results": -5,
        "Conclusion and Limitations": -4,
    },
    "approach": {
        "Method": 9,
        "Approach": 9,
        "System Overview": 10,
        "Introduction": 3,
        "Abstract": 3,
        "Problem Setup": 1,
        "Experiments": -4,
        "Results": -5,
        "Conclusion and Limitations": -4,
    },
    "contributions": {
        "Contributions": 12,
        "Introduction": 8,
        "Abstract": 2,
        "Method": 1,
        "Experiments": -4,
        "Results": -5,
        "Conclusion and Limitations": -4,
    },
    "experimental_setup": {
        "Experimental Setup": 12,
        "Experiments": 10,
        "Evaluation": 8,
        "Long-term Task Evaluation": -4,
        "Coding Model Evaluation": -4,
        "Introduction": -3,
        "Method": 0,
        "Results": 3,
        "Conclusion and Limitations": -4,
    },
    "main_results": {
        "Results": 12,
        "Evaluation": 10,
        "Experiments": 8,
        "Long-term Task Evaluation": 8,
        "Coding Model Evaluation": 8,
        "Abstract": 3,
        "Introduction": -3,
        "Method": -4,
        "Failure Analysis": -6,
        "Conclusion and Limitations": -4,
    },
    "limitations_or_failure_modes": {
        "Conclusion and Limitations": 12,
        "Limitations": 12,
        "Failure Analysis": 11,
        "Discussion": 7,
        "Long-term Task Evaluation": 5,
        "Results": 1,
        "Introduction": 0,
        "Method": -4,
        "System Overview": -5,
    },
}

_CUES: dict[str, tuple[tuple[str, int], ...]] = {
    "context": (
        ("existing", 3), ("prior", 3), ("previous", 2), ("conventional", 2),
        ("paradigm", 2), ("generaliz", 2), ("deployment", 2), ("environment", 1),
        ("field", 1), ("widely", 1), ("typically", 1), ("have demonstrated", 2),
    ),
    "objective_or_research_question": (
        ("aim", 4), ("goal", 4), ("objective", 4), ("research question", 5),
        ("investigate", 3), ("we seek", 3), ("expected to", 4), ("is expected", 4),
        ("consider", 3), ("plan", 2), ("goal state", 3), ("task", 2), ("given", 2),
        ("input", 2), ("output", 2), ("execute", 2),
        ("address these challenges", 3),
    ),
    "motivation_or_gap": (
        ("however", 3), ("but", 2), ("cannot", 3), ("limited", 3), ("limitation", 3),
        ("lack", 3), ("difficult", 2), ("challenge", 2), ("gap", 4), ("remain", 2),
        ("fails", 3), ("insufficient", 3), ("predefined", 3), ("costly", 2),
        ("not able", 3),
    ),
    "problem_setup": (
        ("problem", 3), ("task", 3), ("expected to", 4), ("given", 2), ("input", 2),
        ("output", 2), ("constraint", 3), ("execute", 3), ("sequence", 2),
        ("objective", 3), ("formulate", 3), ("instruction", 2),
    ),
    "approach": (
        ("we propose", 5), ("our approach", 4), ("framework", 3), ("method", 2),
        ("system", 2), ("pipeline", 3), ("architecture", 3), ("parameter", 2),
        ("synthes", 3), ("generate", 2), ("consists", 2), ("module", 2),
    ),
    "contributions": (
        ("contribution", 6), ("we introduce", 5), ("we present", 4), ("we propose", 3),
        ("first", 2), ("second", 2), ("third", 2), ("summarize", 3),
    ),
    "experimental_setup": (
        ("experiment", 3), ("evaluate", 3), ("evaluation", 3), ("dataset", 3),
        ("benchmark", 3), ("baseline", 3), ("trial", 3), ("task", 2), ("setting", 2),
        ("implementation", 2), ("compare", 2), ("robot", 1),
    ),
    "main_results": (
        ("result", 4), ("success", 3), ("performance", 3), ("outperform", 4),
        ("improve", 3), ("average", 2), ("rate", 2), ("latency", 2), ("accuracy", 2),
        ("table", 2), ("figure", 2), ("achieve", 3), ("attain", 4), ("comparable", 3),
        ("higher", 3), ("demonstrate", 3), ("generalize", 2), ("consistent", 2), ("report", 2),
    ),
    "limitations_or_failure_modes": (
        ("limitation", 6), ("failure", 5), ("fail", 4), ("error", 4), ("latency", 2),
        ("future", 3), ("remain", 2), ("incomplete", 3), ("misestimate", 3),
        ("challenge", 2), ("cannot", 2), ("logic", 3), ("syntax", 3), ("real-time", 3),
        ("responsiveness", 3), ("sensitive", 2), ("generation", 2),
    ),
}

_NEGATIVE_CUES: dict[str, tuple[tuple[str, int], ...]] = {
    "context": (("we propose", 8), ("our approach", 7), ("we introduce", 7), ("result", 4), ("failure", 5), ("limitation", 5), ("latency", 3)),
    "objective_or_research_question": (("we propose", 3), ("our approach", 3), ("we employ", 4), ("trajectory", 3), ("parameter", 3), ("framework", 3), ("generate", 3), ("result", 5), ("failure analysis", 6), ("limitation", 4)),
    "motivation_or_gap": (("we propose", 8), ("our approach", 7), ("we introduce", 6), ("result", 5), ("achieve", 4)),
    "problem_setup": (("we propose", 5), ("our approach", 4), ("result", 5), ("success rate", 4), ("failure analysis", 4)),
    "approach": (("failure analysis", 5), ("limitation", 4), ("error", 2)),
    "contributions": (("failure analysis", 4), ("result", 3)),
    "experimental_setup": (("conclusion", 4), ("limitation", 3), ("table", 4), ("success rate", 4), ("results", 3), ("%", 3)),
    "main_results": (("failure analysis", 5), ("limitation", 4)),
    "limitations_or_failure_modes": (("we propose", 5), ("our approach", 4), ("we introduce", 4)),
}

_HEADING_ALIASES = (
    ("conclusion and limitations", "Conclusion and Limitations"),
    ("conclusion", "Conclusion and Limitations"),
    ("limitations", "Limitations"),
    ("failure analysis", "Failure Analysis"),
    ("long-term task evaluation", "Long-term Task Evaluation"),
    ("coding model evaluation", "Coding Model Evaluation"),
    ("experimental setup", "Experimental Setup"),
    ("problem setup", "Problem Setup"),
    ("task formulation", "Task Formulation"),
    ("system overview", "System Overview"),
    ("contributions", "Contributions"),
    ("related work", "Related Work"),
    ("related works", "Related Work"),
    ("background", "Background"),
    ("introduction", "Introduction"),
    ("abstract", "Abstract"),
    ("evaluation", "Evaluation"),
    ("experiments", "Experiments"),
    ("results", "Results"),
    ("method", "Method"),
    ("approach", "Approach"),
    ("discussion", "Discussion"),
)

_SENTENCE_RE = re.compile(r"[^.!?。！？]+(?:[.!?。！？]|$)")


def _normal_text(value: object) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split()).strip()


def _section_heading(value: str) -> str | None:
    text = _normal_text(value).strip(" :：")
    if not text or len(text) > 120:
        return None
    stripped = re.sub(r"^(?:(?:[IVXLCDM]+|\d+)\.?\s+|[A-Z]\.\s*)", "", text, flags=re.IGNORECASE).strip()
    lowered = stripped.casefold()
    for alias, canonical in _HEADING_ALIASES:
        if lowered == alias or lowered.startswith(alias + ":"):
            return canonical
    if text.isupper() and len(stripped.split()) <= 10 and not re.search(r"[.!?。！？]$", text):
        return stripped.title()
    return None


def _sentence_chunks(text: str) -> list[str]:
    compact = _normal_text(text)
    if not compact:
        return []
    chunks = [_normal_text(match.group(0)) for match in _SENTENCE_RE.finditer(compact)]
    return [chunk for chunk in chunks if len(chunk) >= 18]


def _iter_sentences(page_texts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    section = "Unknown"
    for page_record in page_texts:
        if not isinstance(page_record, Mapping) or not isinstance(page_record.get("page"), int):
            continue
        page = page_record["page"]
        paragraph: list[str] = []
        paragraph_index = 0

        def flush() -> None:
            nonlocal paragraph_index
            if not paragraph:
                return
            paragraph_index += 1
            joined = _normal_text(" ".join(paragraph))
            for sentence_index, sentence in enumerate(_sentence_chunks(joined), start=1):
                result.append({
                    "page": page,
                    "section": section,
                    "text": sentence,
                    "paragraph": paragraph_index,
                    "sentence": sentence_index,
                    "locator": f"{section} paragraph {paragraph_index}",
                })
            paragraph.clear()

        for raw_line in str(page_record.get("text") or "").splitlines():
            line = _normal_text(raw_line)
            heading = _section_heading(line) if line else None
            if heading:
                flush()
                section = heading
            elif line:
                paragraph.append(line)
            else:
                flush()
        flush()
    return result


def _section_score(slot: str, section: str) -> int:
    priors = _SECTION_PRIORS.get(slot, {})
    if section in priors:
        return priors[section]
    lowered = section.casefold()
    for name, score in priors.items():
        if name.casefold() in lowered or lowered in name.casefold():
            return score
    return 0


def _cue_score(text: str, cues: Sequence[tuple[str, int]]) -> int:
    lowered = text.casefold()
    return sum(weight for cue, weight in cues if cue.casefold() in lowered)


def _score(slot: str, candidate: Mapping[str, Any]) -> int:
    text = str(candidate.get("text") or "")
    score = _section_score(slot, str(candidate.get("section") or "")) + _cue_score(text, _CUES.get(slot, ())) - _cue_score(text, _NEGATIVE_CUES.get(slot, ()))
    if text and not re.match(r"^[A-Za-z0-9\u3400-\u9fff]", text):
        score -= 8
    if text.count("]") > text.count("[") or text.count(")") > text.count("("):
        score -= 4
    if any(marker in text for marker in ("→", "←", "∑", "∫", "≤", "≥", "≈")) or re.search(r"\b[A-Za-z]{1,8}\s*:\s*\(", text):
        score -= 8
    return score


def _semantic_type(slot: str) -> str:
    return {
        "context": "context",
        "objective_or_research_question": "objective",
        "motivation_or_gap": "research_gap",
        "problem_setup": "problem_setup",
        "approach": "proposal",
        "contributions": "contribution",
        "experimental_setup": "experimental_setup",
        "main_results": "result",
        "limitations_or_failure_modes": "limitation",
    }[slot]


def _record(slot: str, candidate: Mapping[str, Any], score: int) -> dict[str, Any]:
    section = _normal_text(candidate.get("section")) or "Unknown"
    page = int(candidate["page"])
    paragraph = int(candidate.get("paragraph") or 1)
    locator = f"{section} paragraph {paragraph}"
    text = _normal_text(candidate.get("text"))
    confidence = "high" if score >= 13 else "medium" if score >= 7 else "low"
    return {
        "text": text,
        "summary": text,
        "semantic_evidence_type": _semantic_type(slot),
        "source_page": page,
        "section": section,
        "locator": locator,
        "confidence": confidence,
        "score": score,
    }


def _select(slot: str, candidates: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    scored = sorted(
        ((int(_score(slot, item)), item) for item in candidates),
        key=lambda pair: (-pair[0], int(pair[1].get("page", 0)), int(pair[1].get("paragraph", 0)), int(pair[1].get("sentence", 0)), str(pair[1].get("text", ""))),
    )
    if not scored or scored[0][0] < 4:
        return None, [
            {"text": item.get("text", ""), "section": item.get("section", ""), "score": score, "rejection_reason": "below semantic evidence threshold"}
            for score, item in scored[:3]
        ]
    selected_score, selected = scored[0]
    rejected = [
        {"text": item.get("text", ""), "section": item.get("section", ""), "score": score, "rejection_reason": "lower section-aware compatibility score"}
        for score, item in scored[1:4]
    ]
    return _record(slot, selected, selected_score), rejected


def _readiness(slots: Mapping[str, Any]) -> dict[str, Any]:
    required = [*_MODE_B_REQUIRED, "motivation_or_gap_or_problem_setup"]
    missing: list[str] = []
    for slot in _MODE_B_REQUIRED:
        if not isinstance(slots.get(slot), Mapping):
            missing.append(slot)
    if not isinstance(slots.get("motivation_or_gap"), Mapping) and not isinstance(slots.get("problem_setup"), Mapping):
        missing.append("motivation_or_gap_or_problem_setup")
    return {
        "status": "ready" if not missing else "incomplete",
        "ready": not missing,
        "required_slots": required,
        "missing_slots": missing,
        "satisfied_slots": [slot for slot in REQUIRED_SLOTS if isinstance(slots.get(slot), Mapping)],
    }


def build_paper_semantics(page_texts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract a deterministic semantic slot view from persisted page text."""
    sentences = _iter_sentences(page_texts)
    slots: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    for slot in REQUIRED_SLOTS:
        selected, rejected = _select(slot, sentences)
        slots[slot] = selected
        audit[slot] = {"candidate_count": len(sentences), "rejected_candidates": rejected}
    return {
        "schema_version": 1,
        "slots": slots,
        "selection_audit": audit,
        "source_evidence": sentences,
        "mode_b_narrative_readiness": _readiness(slots),
    }


def semantic_records(value: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Return non-empty slot records for locator validation and reporting."""
    if not isinstance(value, Mapping):
        return []
    slots = value.get("slots")
    if not isinstance(slots, Mapping):
        return []
    return [record for record in slots.values() if isinstance(record, Mapping)]
