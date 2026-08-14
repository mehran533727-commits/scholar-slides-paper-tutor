"""Machine-readable contracts for the supported academic deck types."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping


class DeckTypeError(ValueError):
    """A deck-type option cannot be resolved safely."""


class DeckType(str, Enum):
    JOURNAL_CLUB = "journal-club"
    CONFERENCE = "conference"
    THESIS_DEFENSE = "thesis-defense"


DEFAULT_DECK_TYPE = DeckType.JOURNAL_CLUB
VALID_DENSITIES = frozenset({"high", "sparse", "mixed"})


@dataclass(frozen=True)
class DeckTypeContract:
    """Type-specific authoring policy, independent of any source paper."""

    deck_type: DeckType
    audience_intent: str
    time_to_slide_budget: Mapping[str, Mapping[str, int]]
    density: str
    narrative_arc: tuple[str, ...]
    required_archetypes: tuple[str, ...]
    optional_archetypes: tuple[str, ...]
    backup_policy: Mapping[str, Any]
    critique_policy: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["deck_type"] = self.deck_type.value
        return payload


DECK_TYPE_CONTRACTS: dict[DeckType, DeckTypeContract] = {
    DeckType.JOURNAL_CLUB: DeckTypeContract(
        deck_type=DeckType.JOURNAL_CLUB,
        audience_intent="reading-first",
        time_to_slide_budget={"talk_minutes": {"min": 30, "max": 60, "default": 45}, "slide_count": {"min": 10, "max": 15, "default": 12}},
        density="high",
        narrative_arc=("context", "claims", "method", "results", "critique", "takeaway", "discussion"),
        required_archetypes=("critique-concerns", "discussion-questions"),
        optional_archetypes=("results-table", "assertion-evidence"),
        backup_policy={"mode": "optional", "appendix": False},
        critique_policy={"mode": "required", "discussion": True},
    ),
    DeckType.CONFERENCE: DeckTypeContract(
        deck_type=DeckType.CONFERENCE,
        audience_intent="speaker-led",
        time_to_slide_budget={"talk_minutes": {"min": 12, "max": 20, "default": 12}, "slide_count": {"min": 10, "max": 15, "default": 12}},
        density="sparse",
        narrative_arc=("teaser", "problem", "idea", "strongest-result", "takeaway"),
        required_archetypes=("assertion-evidence",),
        optional_archetypes=("backup", "discussion-questions"),
        backup_policy={"mode": "optional", "appendix": True},
        critique_policy={"mode": "disabled", "discussion": False},
    ),
    DeckType.THESIS_DEFENSE: DeckTypeContract(
        deck_type=DeckType.THESIS_DEFENSE,
        audience_intent="examiner-facing",
        time_to_slide_budget={"talk_minutes": {"min": 40, "max": 60, "default": 45}, "slide_count": {"min": 30, "max": 60, "default": 40}},
        density="mixed",
        narrative_arc=("roadmap", "background", "contributions", "synthesis", "limitations", "future-work", "recap"),
        required_archetypes=("section", "backup"),
        optional_archetypes=("limitations", "future-work", "results-table", "assertion-evidence"),
        backup_policy={"mode": "required", "appendix": True, "rich": True},
        critique_policy={"mode": "evidence-bound", "discussion": False},
    ),
}


def get_deck_contract(value: DeckType | str | None = None) -> DeckTypeContract:
    """Return a supported contract or fail closed for an unknown public type."""
    if value is None:
        deck_type = DEFAULT_DECK_TYPE
    else:
        try:
            deck_type = value if isinstance(value, DeckType) else DeckType(value)
        except (TypeError, ValueError) as exc:
            supported = ", ".join(member.value for member in DeckType)
            raise DeckTypeError(f"Unknown deck type {value!r}; supported types are: {supported}.") from exc
    return DECK_TYPE_CONTRACTS[deck_type]


def resolve_deck_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve type defaults while keeping explicit user presentation constraints."""
    resolved = dict(options)
    contract = get_deck_contract(resolved.get("deck_type"))
    density = resolved.get("density") or contract.density
    if not isinstance(density, str) or density not in VALID_DENSITIES:
        raise DeckTypeError(f"density must be one of: {', '.join(sorted(VALID_DENSITIES))}.")
    talk_time = resolved.get("talk_time_minutes")
    if talk_time is None:
        talk_time = contract.time_to_slide_budget["talk_minutes"]["default"]
    if not isinstance(talk_time, int) or isinstance(talk_time, bool) or not 1 <= talk_time <= 240:
        raise DeckTypeError("talk_time_minutes must be an integer from 1 through 240.")
    slide_count = resolved.get("slide_count")
    if slide_count is None:
        slide_count = contract.time_to_slide_budget["slide_count"]["default"]
    slide_budget = contract.time_to_slide_budget["slide_count"]
    if (
        not isinstance(slide_count, int)
        or isinstance(slide_count, bool)
        or not slide_budget["min"] <= slide_count <= slide_budget["max"]
    ):
        raise DeckTypeError(
            f"slide_count must be an integer from {slide_budget['min']} through {slide_budget['max']} for {contract.deck_type.value}."
        )
    audience = resolved.get("audience") or contract.audience_intent
    if not isinstance(audience, str) or not audience.strip():
        raise DeckTypeError("audience must be a non-empty string.")
    resolved.update({
        "deck_type": contract.deck_type.value,
        "audience": audience,
        "density": density,
        "talk_time_minutes": talk_time,
        "slide_count": slide_count,
        "deck_type_contract": contract.as_dict(),
    })
    return resolved
