"""Pure public-source host-diversity contract for bounded web research.

This module receives already-admitted source URLs and records only their
lexical URL host identities.  It intentionally performs no DNS, public-suffix
list lookup, network request, file I/O, persistence, or live retrieval wiring.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

from friday.orchestration.web_provider_policy import validate_public_web_url
from friday.web_research_contract import MAX_RESEARCH_SOURCES

WEB_SOURCE_DIVERSITY_SCHEMA = "friday.web-source-diversity.v1"
MAX_DIVERSITY_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128


class WebSourceDiversityError(ValueError):
    """A source set is unsafe, inconsistent, or outside the closed contract."""


class WebSourceDiversityNote(StrEnum):
    """Closed outcomes for admitted public-source host diversity."""

    DIVERSE = "diverse"
    CONCENTRATED = "concentrated"
    SINGLE_HOST = "single_host"
    EMPTY = "empty"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebSourceDiversityError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        _fail(field, "id")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(field, "control")
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
        for character in value
    ):
        _fail(field, "id")
    return cast(str, value)


def _bounded_int(value: object, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail(field, "range")
    return cast(int, value)


def _host_identity(value: object, *, field: str) -> str:
    try:
        validated = validate_public_web_url(value, field=field)
        hostname = urlsplit(validated).hostname
    except (TypeError, ValueError) as exc:
        raise WebSourceDiversityError(f"{field}_not_public") from exc
    if not hostname:
        _fail(field, "host")
    return hostname.rstrip(".").casefold()


def _source_urls(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("source_urls", "sequence")
    if len(value) > MAX_RESEARCH_SOURCES:
        _fail("source_urls", "bound")
    urls: list[str] = []
    for index, item in enumerate(value):
        raw_url = item
        if isinstance(item, Mapping):
            keys = set(item)
            if keys - {"canonical_url", "url"}:
                _fail("source_urls_item", "closed")
            raw_url = item.get("canonical_url", item.get("url"))
        try:
            urls.append(validate_public_web_url(raw_url, field=f"source_urls[{index}]"))
        except (TypeError, ValueError) as exc:
            raise WebSourceDiversityError(f"source_urls[{index}]_not_public") from exc
    return tuple(urls)


def _count_note(admitted: int, unique: int) -> WebSourceDiversityNote:
    if admitted == 0:
        return WebSourceDiversityNote.EMPTY
    if unique == 1:
        return WebSourceDiversityNote.SINGLE_HOST
    # The exact concentrated/diverse result depends on the host multiplicity;
    # the builder computes it from the observed host sequence.  A value built
    # directly still has a closed two-state choice for the multi-host case.
    return WebSourceDiversityNote.DIVERSE


def _note_for_hosts(hosts: Sequence[str]) -> WebSourceDiversityNote:
    if not hosts:
        return WebSourceDiversityNote.EMPTY
    counts = Counter(hosts)
    if len(counts) == 1:
        return WebSourceDiversityNote.SINGLE_HOST
    if max(counts.values()) * 2 > len(hosts):
        # A host owns a majority exactly when its multiplicity is greater than
        # half of all admitted sources.  Ties remain honestly diverse.
        return WebSourceDiversityNote.CONCENTRATED
    return WebSourceDiversityNote.DIVERSE


@dataclass(frozen=True, slots=True)
class WebSourceDiversityV1:
    """Immutable host-diversity facts for one admitted source set."""

    diversity_id: str
    authenticated_turn_id: str
    unique_host_count: int
    duplicate_host_count: int
    admitted_source_count: int
    diversity_note: WebSourceDiversityNote
    unique_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.diversity_id, field="diversity_id", maximum=MAX_DIVERSITY_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admitted = _bounded_int(
            self.admitted_source_count,
            field="admitted_source_count",
            maximum=MAX_RESEARCH_SOURCES,
        )
        unique = _bounded_int(
            self.unique_host_count,
            field="unique_host_count",
            maximum=MAX_RESEARCH_SOURCES,
        )
        duplicate = _bounded_int(
            self.duplicate_host_count,
            field="duplicate_host_count",
            maximum=MAX_RESEARCH_SOURCES,
        )
        if unique > admitted or duplicate != admitted - unique:
            _fail("host_counts", "inconsistent")
        if type(self.unique_hosts) is not tuple:
            _fail("unique_hosts", "immutable")
        if len(self.unique_hosts) != unique or len(set(self.unique_hosts)) != len(self.unique_hosts):
            _fail("unique_hosts", "inconsistent")
        for host in self.unique_hosts:
            if type(host) is not str or not host or host != host.casefold() or host.endswith("."):
                _fail("unique_hosts", "identity")
        try:
            note = WebSourceDiversityNote(self.diversity_note)
        except (TypeError, ValueError) as exc:
            raise WebSourceDiversityError("diversity_note_closed") from exc
        if note is not self.diversity_note:
            object.__setattr__(self, "diversity_note", note)
        if note not in {
            WebSourceDiversityNote.DIVERSE,
            WebSourceDiversityNote.CONCENTRATED,
        } and note is not _count_note(admitted, unique):
            _fail("diversity_note", "inconsistent")

    @property
    def note(self) -> WebSourceDiversityNote:
        return self.diversity_note

    @property
    def closed_diversity_note(self) -> WebSourceDiversityNote:
        return self.diversity_note

    @property
    def source_hosts(self) -> tuple[str, ...]:
        return self.unique_hosts

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_SOURCE_DIVERSITY_SCHEMA,
            "diversity_id": self.diversity_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "unique_host_count": self.unique_host_count,
            "duplicate_host_count": self.duplicate_host_count,
            "admitted_source_count": self.admitted_source_count,
            "diversity_note": self.diversity_note.value,
            "unique_hosts": list(self.unique_hosts),
        }


WebSourceDiversity = WebSourceDiversityV1
WebSourceDiversityClass = WebSourceDiversityNote
WebSourceDiversityGrade = WebSourceDiversityNote


def _mapping_source_urls(raw: Mapping[str, Any], explicit: object) -> object:
    if explicit is not None:
        return explicit
    for key in ("source_urls", "admitted_source_urls", "public_source_urls", "urls", "sources"):
        if key in raw:
            return raw[key]
    return ()


def _validate_context_query_plan(raw: Mapping[str, Any]) -> None:
    # A mission's query plan is accepted as context only.  It is deliberately
    # not used to infer source counts, hosts, or diversity and is not retained
    # in the diversity result.
    if "query_plan" in raw:
        value = raw["query_plan"]
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            _fail("query_plan", "sequence")


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "diversity_id",
        "authenticated_turn_id",
        "source_urls",
        "admitted_source_urls",
        "public_source_urls",
        "urls",
        "sources",
        "query_plan",
        "unique_host_count",
        "duplicate_host_count",
        "admitted_source_count",
        "diversity_note",
        "unique_hosts",
    }
    if any(not isinstance(key, str) or key not in known for key in raw):
        _fail("diversity", "unknown_fields")


def _build_from_urls(
    *,
    diversity_id: object,
    authenticated_turn_id: object,
    source_urls: object,
) -> WebSourceDiversityV1:
    urls = _source_urls(source_urls)
    hosts = tuple(_host_identity(url, field=f"source_urls[{index}]") for index, url in enumerate(urls))
    unique_hosts = tuple(dict.fromkeys(hosts))
    unique_count = len(unique_hosts)
    admitted_count = len(urls)
    duplicate_count = admitted_count - unique_count
    return WebSourceDiversityV1(
        diversity_id=_identifier(diversity_id, field="diversity_id", maximum=MAX_DIVERSITY_ID_CHARS),
        authenticated_turn_id=_identifier(
            authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        ),
        unique_host_count=unique_count,
        duplicate_host_count=duplicate_count,
        admitted_source_count=admitted_count,
        diversity_note=_note_for_hosts(hosts),
        unique_hosts=unique_hosts,
    )


def build_web_source_diversity(
    raw: Mapping[str, Any] | WebSourceDiversityV1 | Sequence[object] | None = None,
    *,
    diversity_id: object = None,
    authenticated_turn_id: object = None,
    source_urls: object = None,
    query_plan: object = None,
) -> WebSourceDiversityV1:
    """Build host diversity from admitted URLs without DNS or network access."""

    if isinstance(raw, WebSourceDiversityV1):
        if source_urls is not None or diversity_id is not None or authenticated_turn_id is not None:
            _fail("diversity", "duplicate_arguments")
        return raw
    if isinstance(raw, Mapping):
        _known_mapping_keys(raw)
        if raw.get("schema", WEB_SOURCE_DIVERSITY_SCHEMA) != WEB_SOURCE_DIVERSITY_SCHEMA:
            _fail("schema")
        if query_plan is not None or "query_plan" in raw:
            _validate_context_query_plan({"query_plan": raw.get("query_plan", query_plan)})
        source_keys = {
            key
            for key in ("source_urls", "admitted_source_urls", "public_source_urls", "urls", "sources")
            if key in raw
        }
        if "unique_hosts" in raw and source_keys:
            _fail("diversity", "duplicate_representations")
        if "unique_hosts" in raw:
            hosts_value = raw["unique_hosts"]
            if isinstance(hosts_value, (str, bytes, bytearray)) or not isinstance(hosts_value, Sequence):
                _fail("unique_hosts", "sequence")
            return WebSourceDiversityV1(
                diversity_id=_identifier(
                    raw.get("diversity_id", diversity_id),
                    field="diversity_id",
                    maximum=MAX_DIVERSITY_ID_CHARS,
                ),
                authenticated_turn_id=_identifier(
                    raw.get("authenticated_turn_id", authenticated_turn_id),
                    field="authenticated_turn_id",
                    maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
                ),
                unique_host_count=cast(int, raw.get("unique_host_count")),
                duplicate_host_count=cast(int, raw.get("duplicate_host_count")),
                admitted_source_count=cast(int, raw.get("admitted_source_count")),
                diversity_note=cast(WebSourceDiversityNote, raw.get("diversity_note")),
                unique_hosts=tuple(hosts_value),
            )
        return _build_from_urls(
            diversity_id=raw.get("diversity_id", diversity_id),
            authenticated_turn_id=raw.get("authenticated_turn_id", authenticated_turn_id),
            source_urls=_mapping_source_urls(raw, source_urls),
        )
    if raw is not None:
        if source_urls is not None:
            _fail("source_urls", "duplicate_arguments")
        source_urls = raw
    if query_plan is not None:
        _validate_context_query_plan({"query_plan": query_plan})
    return _build_from_urls(
        diversity_id=diversity_id,
        authenticated_turn_id=authenticated_turn_id,
        source_urls=source_urls if source_urls is not None else (),
    )


def validate_web_source_diversity(value: object) -> bool:
    """Return whether a value satisfies the complete frozen diversity contract."""

    try:
        build_web_source_diversity(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


build_source_diversity = build_web_source_diversity
plan_web_source_diversity = build_web_source_diversity
validate_source_diversity = validate_web_source_diversity


__all__ = [
    "MAX_RESEARCH_SOURCES",
    "WEB_SOURCE_DIVERSITY_SCHEMA",
    "WebSourceDiversity",
    "WebSourceDiversityClass",
    "WebSourceDiversityError",
    "WebSourceDiversityGrade",
    "WebSourceDiversityNote",
    "WebSourceDiversityV1",
    "build_source_diversity",
    "build_web_source_diversity",
    "plan_web_source_diversity",
    "validate_source_diversity",
    "validate_web_source_diversity",
]
