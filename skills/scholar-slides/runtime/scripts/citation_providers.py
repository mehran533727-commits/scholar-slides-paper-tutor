"""Normalized, fail-closed citation provider chain.

The provider chain is deliberately a small boundary around external related-work
lookups.  It never edits the source paper metadata that is extracted and approved
at CKPT-1.  Providers may return a partial record, but a record is only marked
``verified`` when it has come from a provider and passed the identifier/title
identity checks.  Missing records remain visible as ``[UNVERIFIED]``.
"""
from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


_ARXIV_ID = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?$", re.IGNORECASE)
_DOI = re.compile(r"^(?:doi:)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
_NETWORK_PROVIDER_NAMES = frozenset({"crossref", "doi", "arxiv"})


def _text(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _authors(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = " ".join(part for part in (_text(item.get("given")), _text(item.get("family"))) if part)
        else:
            name = _text(item)
        if name:
            result.append(name)
    return result


def _year(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and int(value) == value:
        return str(int(value))
    result = _text(value)
    return result


def _doi(value: object) -> str | None:
    result = _text(value)
    if not result:
        return None
    match = _DOI.match(result)
    return match.group(1) if match else None


def _arxiv(value: object) -> str | None:
    result = _text(value)
    if not result:
        return None
    match = _ARXIV_ID.match(result)
    if not match:
        return None
    version = re.search(r"v\d+$", result, re.IGNORECASE)
    return match.group(1) + (version.group(0) if version else "")


def _normal_title(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def title_matches(query: object, result: object, threshold: float = 0.95) -> bool:
    """Guard title lookups against a wrong provider hit.

    The comparison is order-sensitive.  This intentionally rejects a title with
    the same words in a different order, which is a common Crossref ambiguity.
    """

    left, right = _normal_title(query), _normal_title(result)
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    if len(short) >= 15 and long.startswith(short):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= threshold


@dataclass
class Citation:
    """The normalized citation model shared by all providers."""

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    url_or_locator: str | None = None
    provider: str = "unresolved"
    verified: bool = False
    confidence: float = 0.0
    query: str | None = None
    scope: str = "external_related_work"
    error: str | None = None
    attempted_providers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.title = _text(self.title)
        self.authors = _authors(self.authors)
        self.year = _year(self.year)
        self.venue = _text(self.venue)
        self.doi = _doi(self.doi)
        self.arxiv_id = _arxiv(self.arxiv_id)
        self.url_or_locator = _text(self.url_or_locator)
        self.provider = _text(self.provider) or "unresolved"
        self.verified = bool(self.verified and self.provider != "unresolved" and self.title)
        try:
            self.confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            self.confidence = 0.0
        if not self.verified:
            self.confidence = 0.0 if self.provider == "unresolved" else self.confidence
        self.query = _text(self.query)
        self.scope = _text(self.scope) or "external_related_work"
        self.error = _text(self.error)
        self.attempted_providers = [str(item) for item in self.attempted_providers if _text(item)]
        if not self.url_or_locator:
            if self.doi:
                self.url_or_locator = f"https://doi.org/{self.doi}"
            elif self.arxiv_id:
                self.url_or_locator = f"https://arxiv.org/abs/{self.arxiv_id}"
            elif self.query:
                self.url_or_locator = self.query

    @property
    def resolved(self) -> bool:
        return self.verified

    @property
    def marker(self) -> str | None:
        if self.verified:
            return None
        label = self.query or self.title or self.url_or_locator or "citation"
        return f"[UNVERIFIED: {label}]"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "url_or_locator": self.url_or_locator,
            "provider": self.provider,
            "verified": self.verified,
            "confidence": self.confidence,
            "resolved": self.resolved,
            "scope": self.scope,
        }
        if self.query is not None:
            payload["query"] = self.query
        if self.marker:
            payload["marker"] = self.marker
        if self.error:
            payload["error"] = self.error
        if self.attempted_providers:
            payload["attempted_providers"] = list(self.attempted_providers)
        return payload

    # Mapping conveniences keep the model compatible with the historical
    # ``fetch_bib.resolve`` dictionary API while callers migrate to attributes.
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def unresolved_citation(
    query: object,
    *,
    attempted_providers: Sequence[str] = (),
    errors: Sequence[str] = (),
    scope: str = "external_related_work",
) -> Citation:
    """Create the explicit unresolved result; no metadata is guessed."""

    error = "; ".join(str(item) for item in errors if _text(item)) or None
    return Citation(
        provider="unresolved",
        verified=False,
        confidence=0.0,
        query=_text(query),
        url_or_locator=_text(query),
        scope=scope,
        error=error,
        attempted_providers=list(attempted_providers),
    )


def normalize_citation(
    value: Citation | Mapping[str, Any],
    *,
    provider: str,
    query: object = None,
    verified: bool | None = None,
    confidence: float | None = None,
    scope: str = "external_related_work",
) -> Citation:
    """Normalize a provider payload without filling absent bibliographic fields."""

    if isinstance(value, Citation):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("citation provider must return a mapping, Citation, or None")
    provider_name = _text(provider) or "unresolved"
    query_text = _text(query) or _text(payload.get("query"))
    explicitly_verified = payload.get("verified") if isinstance(payload.get("verified"), bool) else verified
    if explicitly_verified is None:
        explicitly_verified = provider_name != "unresolved" and payload.get("resolved", True) is not False
    default_confidence = {
        "zotero": 0.98,
        "crossref": 0.95,
        "doi": 0.95,
        "arxiv": 0.90,
    }.get(provider_name, 0.80 if explicitly_verified else 0.0)
    citation = Citation(
        title=payload.get("title"),
        authors=payload.get("authors", []),
        year=payload.get("year"),
        venue=payload.get("venue"),
        doi=payload.get("doi"),
        arxiv_id=payload.get("arxiv_id", payload.get("arxiv")),
        url_or_locator=payload.get("url_or_locator", payload.get("url", payload.get("locator"))),
        provider=provider_name,
        verified=bool(explicitly_verified),
        confidence=default_confidence if confidence is None else confidence,
        query=query_text,
        scope=payload.get("scope", scope),
        error=payload.get("error"),
        attempted_providers=payload.get("attempted_providers", []),
    )
    return citation


class CitationProvider(Protocol):
    name: str
    network: bool

    def resolve(self, query: str) -> Citation | Mapping[str, Any] | None:
        ...


class FunctionCitationProvider:
    """Adapter for an injected provider function, useful for connectors/tests."""

    def __init__(self, name: str, resolver: Callable[[str], Citation | Mapping[str, Any] | None], *, network: bool | None = None):
        self.name = _text(name) or "provider"
        self.resolver = resolver
        self.network = self.name.casefold() in _NETWORK_PROVIDER_NAMES if network is None else bool(network)

    def resolve(self, query: str) -> Citation | Mapping[str, Any] | None:
        return self.resolver(query)

    def available(self) -> bool:
        return callable(self.resolver)


class ZoteroProvider(FunctionCitationProvider):
    """Optional semantic adapter; it never assumes a connector tool name."""

    def __init__(self, resolver: Callable[[str], Citation | Mapping[str, Any] | None] | None = None):
        super().__init__("zotero", resolver or (lambda query: None), network=False)
        self.optional = True


class CrossrefProvider(FunctionCitationProvider):
    """DOI/title provider backed by injected fetchers."""

    def __init__(
        self,
        by_doi: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
        by_title: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
    ):
        self.by_doi = by_doi
        self.by_title = by_title
        super().__init__("crossref", self._lookup, network=True)

    def _lookup(self, query: str) -> Citation | Mapping[str, Any] | None:
        match = _DOI.match(query.strip())
        if match and callable(self.by_doi):
            return self.by_doi(match.group(1))
        if not match and callable(self.by_title):
            return self.by_title(query)
        return None


class ArxivProvider(FunctionCitationProvider):
    """arXiv identifier provider backed by an injected fetcher."""

    def __init__(self, resolver: Callable[[str], Citation | Mapping[str, Any] | None] | None = None):
        self.lookup = resolver
        super().__init__("arxiv", self._lookup, network=True)

    def _lookup(self, query: str) -> Citation | Mapping[str, Any] | None:
        match = _ARXIV_ID.match(query.strip())
        if match and callable(self.lookup):
            return self.lookup(match.group(1))
        return None


class CitationProviderChain:
    """Run providers in order and fail closed on misses or identity conflicts."""

    def __init__(self, providers: Iterable[CitationProvider]):
        self.providers = tuple(providers)

    @property
    def provider_names(self) -> list[str]:
        return [str(getattr(provider, "name", provider.__class__.__name__)) for provider in self.providers]

    def resolve(self, query: object, *, allow_network: bool = True, scope: str = "external_related_work") -> Citation:
        query_text = _text(query) or ""
        attempted: list[str] = []
        errors: list[str] = []
        for provider in self.providers:
            name = _text(getattr(provider, "name", None)) or provider.__class__.__name__.casefold()
            if not allow_network and bool(getattr(provider, "network", False)):
                continue
            available = getattr(provider, "available", None)
            if callable(available):
                try:
                    if not available():
                        continue
                except Exception as exc:  # an optional provider is never a hard dependency
                    errors.append(f"{name}: {exc}")
                    continue
            attempted.append(name)
            try:
                raw = provider.resolve(query_text)
            except Exception as exc:  # network/provider failures fall through
                errors.append(f"{name}: {exc}")
                continue
            if raw is None:
                continue
            try:
                citation = normalize_citation(raw, provider=name, query=query_text, scope=scope)
            except (TypeError, ValueError) as exc:
                errors.append(f"{name}: invalid citation ({exc})")
                continue
            if not citation.verified:
                continue
            if not _identity_matches(query_text, citation):
                errors.append(f"{name}: identity mismatch")
                continue
            citation.attempted_providers = list(attempted)
            return citation
        return unresolved_citation(query_text, attempted_providers=attempted, errors=errors, scope=scope)

    def resolve_many(self, queries: Iterable[object], *, allow_network: bool = True, scope: str = "external_related_work") -> list[Citation]:
        return [self.resolve(query, allow_network=allow_network, scope=scope) for query in queries]


def _identity_matches(query: str, citation: Citation) -> bool:
    arxiv = _ARXIV_ID.match(query)
    if arxiv:
        return bool(citation.arxiv_id and citation.arxiv_id.split("v", 1)[0] == arxiv.group(1))
    doi = _DOI.match(query)
    if doi:
        return bool(citation.doi and citation.doi.casefold() == doi.group(1).casefold())
    return bool(citation.title and title_matches(query, citation.title))


def resolve_external_citations(
    queries: Iterable[object],
    *,
    providers: Iterable[CitationProvider] | None = None,
    source_metadata: Mapping[str, Any] | None = None,
    allow_network: bool = True,
    mode: str | None = None,
) -> dict[str, Any]:
    """Resolve related-work citations while preserving CKPT-1 source metadata.

    ``mode='A'`` (paper-understanding only) is offline by contract.  The returned
    source metadata is a deep copy and is never merged with provider output.
    """

    normalized_mode = _text(mode).casefold() if mode else None
    network_allowed = bool(allow_network) and normalized_mode not in {"a", "mode a", "paper-understanding", "paper understanding"}
    chain = CitationProviderChain(providers or ())
    citations = chain.resolve_many(queries, allow_network=network_allowed)
    payloads: list[dict[str, Any]] = []
    for citation in citations:
        payload = citation.to_dict()
        payload["scope"] = "external_related_work"
        payloads.append(payload)
    return {
        "source_metadata": copy.deepcopy(dict(source_metadata)) if isinstance(source_metadata, Mapping) else None,
        "citations": payloads,
        "provider_order": chain.provider_names,
        "network_allowed": network_allowed,
        "mode": mode,
    }


def default_provider_chain(
    *,
    zotero_lookup: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
    crossref_by_doi: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
    crossref_by_title: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
    arxiv_lookup: Callable[[str], Citation | Mapping[str, Any] | None] | None = None,
) -> CitationProviderChain:
    """Construct the canonical optional-Zotero → Crossref → arXiv chain."""

    providers: list[CitationProvider] = []
    if callable(zotero_lookup):
        providers.append(ZoteroProvider(zotero_lookup))
    providers.extend((CrossrefProvider(crossref_by_doi, crossref_by_title), ArxivProvider(arxiv_lookup)))
    return CitationProviderChain(providers)


__all__ = [
    "ArxivProvider",
    "Citation",
    "CitationProvider",
    "CitationProviderChain",
    "CrossrefProvider",
    "FunctionCitationProvider",
    "ZoteroProvider",
    "default_provider_chain",
    "normalize_citation",
    "resolve_external_citations",
    "title_matches",
    "unresolved_citation",
]
