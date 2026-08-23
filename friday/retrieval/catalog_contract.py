"""Body-free rebuildable catalog projection; never lifecycle or auth authority."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from friday.retrieval._contract_utils import (
    RetrievalContractError,
    bounded_text,
    canonical_json,
    enum_value,
    exact_object,
    optional_bounded_text,
    parse_canonical_object,
)
from friday.retrieval.identity_contract import (
    LifecycleState,
    RepresentationKind,
    ResolvedSource,
    SourceRef,
)
from friday.retrieval.temporal_contract import TemporalFact

CATALOG_ITEM_SCHEMA = "friday.catalog-item.private.v1"


class CatalogReviewState(StrEnum):
    PENDING = "pending"
    CLASSIFIED = "classified"
    ARCHIVED = "archived"
    IGNORED = "ignored"
    NOT_APPLICABLE = "not_applicable"


class CatalogIngestState(StrEnum):
    REGISTERED = "registered"
    EXTRACTED = "extracted"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    DELETED = "deleted"


class CatalogIndexLane(StrEnum):
    CATALOG = "catalog"
    PASSAGES = "passages"
    LEXICAL = "lexical"
    APPROXIMATE_IDENTITY = "approximate_identity"
    DENSE = "dense"


class CatalogIndexStatus(StrEnum):
    CURRENT = "current"
    PENDING = "pending"
    PARTIAL = "partial"
    STALE = "stale"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"
    NOT_APPLICABLE = "not_applicable"


class IndexIncompleteReason(StrEnum):
    BACKFILL_PENDING = "backfill_pending"
    EXTRACTION_FAILED = "extraction_failed"
    NO_TEXT = "no_text"
    UNSUPPORTED_CONTENT = "unsupported_content"
    EMBEDDING_INCOMPATIBLE = "embedding_incompatible"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_CHANGED = "source_changed"


_INDEX_REASON_MATRIX = {
    CatalogIndexStatus.CURRENT: frozenset({None}),
    CatalogIndexStatus.PENDING: frozenset({IndexIncompleteReason.BACKFILL_PENDING}),
    CatalogIndexStatus.PARTIAL: frozenset(
        {
            IndexIncompleteReason.BACKFILL_PENDING,
            IndexIncompleteReason.EXTRACTION_FAILED,
            IndexIncompleteReason.SOURCE_UNAVAILABLE,
        }
    ),
    CatalogIndexStatus.STALE: frozenset({IndexIncompleteReason.SOURCE_CHANGED}),
    CatalogIndexStatus.FAILED: frozenset(
        {IndexIncompleteReason.EXTRACTION_FAILED, IndexIncompleteReason.SOURCE_UNAVAILABLE}
    ),
    CatalogIndexStatus.INCOMPATIBLE: frozenset({IndexIncompleteReason.EMBEDDING_INCOMPATIBLE}),
    CatalogIndexStatus.NOT_APPLICABLE: frozenset(
        {IndexIncompleteReason.NO_TEXT, IndexIncompleteReason.UNSUPPORTED_CONTENT}
    ),
}


@dataclass(frozen=True, slots=True)
class CatalogIndexState:
    lane: CatalogIndexLane
    status: CatalogIndexStatus
    incomplete_reason: IndexIncompleteReason | None

    def __post_init__(self) -> None:
        if not isinstance(self.lane, CatalogIndexLane) or not isinstance(self.status, CatalogIndexStatus):
            raise RetrievalContractError("catalog index fields must use closed enums")
        if self.incomplete_reason is not None and not isinstance(
            self.incomplete_reason, IndexIncompleteReason
        ):
            raise RetrievalContractError("index incomplete reason must use a closed enum")
        if self.incomplete_reason not in _INDEX_REASON_MATRIX[self.status]:
            raise RetrievalContractError("index status and incomplete reason disagree")
        if self.status is CatalogIndexStatus.INCOMPATIBLE and self.lane is not CatalogIndexLane.DENSE:
            raise RetrievalContractError("embedding incompatibility belongs only to the dense index")
        if (
            self.incomplete_reason is IndexIncompleteReason.EMBEDDING_INCOMPATIBLE
            and self.status is not CatalogIndexStatus.INCOMPATIBLE
        ):
            raise RetrievalContractError("embedding incompatibility reason requires incompatible status")

    def to_private_payload(self) -> dict[str, object]:
        return {
            "incomplete_reason": (
                self.incomplete_reason.value if self.incomplete_reason is not None else None
            ),
            "lane": self.lane.value,
            "status": self.status.value,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> CatalogIndexState:
        payload = exact_object(
            value,
            frozenset({"incomplete_reason", "lane", "status"}),
            label="catalog index state",
        )
        reason = payload["incomplete_reason"]
        return cls(
            lane=enum_value(CatalogIndexLane, payload["lane"], label="catalog index lane"),
            status=enum_value(CatalogIndexStatus, payload["status"], label="catalog index status"),
            incomplete_reason=(
                None
                if reason is None
                else enum_value(IndexIncompleteReason, reason, label="index incomplete reason")
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class CatalogItem:
    """Private navigation projection, rebuildable and non-authoritative by design."""

    source_ref: SourceRef
    resolved_source: ResolvedSource
    canonical_title: str | None
    visible_title: str | None
    filename: str | None
    aliases: tuple[str, ...]
    review_state: CatalogReviewState
    ingest_state: CatalogIngestState
    index_states: tuple[CatalogIndexState, ...]
    temporal_facts: tuple[TemporalFact, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, SourceRef) or not isinstance(self.resolved_source, ResolvedSource):
            raise RetrievalContractError("catalog item requires typed source contracts")
        if self.source_ref != self.resolved_source.source_ref:
            raise RetrievalContractError("catalog source_ref and resolved snapshot disagree")
        optional_bounded_text(self.canonical_title, label="canonical title", maximum_bytes=1_024)
        optional_bounded_text(self.visible_title, label="visible title", maximum_bytes=1_024)
        optional_bounded_text(self.filename, label="filename", maximum_bytes=1_024)
        if self.filename is not None and ("/" in self.filename or "\\" in self.filename):
            raise RetrievalContractError("catalog filename must not be a display path")
        if (
            type(self.aliases) is not tuple
            or any(not isinstance(item, str) for item in self.aliases)
            or self.aliases != tuple(sorted(self.aliases))
            or len(self.aliases) != len(set(self.aliases))
        ):
            raise RetrievalContractError("catalog aliases must be a sorted unique tuple")
        for alias in self.aliases:
            bounded_text(alias, label="catalog alias", maximum_bytes=1_024)
        if not isinstance(self.review_state, CatalogReviewState) or not isinstance(
            self.ingest_state, CatalogIngestState
        ):
            raise RetrievalContractError("catalog states must use closed enums")
        inbox_lifecycle = tuple(
            item.state
            for item in self.resolved_source.lifecycle
            if item.representation.kind is RepresentationKind.INBOX_ITEM
        )
        if self.review_state is CatalogReviewState.NOT_APPLICABLE:
            if inbox_lifecycle:
                raise RetrievalContractError("Inbox-backed catalog items require their exact review state")
        elif inbox_lifecycle != (LifecycleState(self.review_state.value),):
            raise RetrievalContractError("catalog review state must equal the authoritative Inbox snapshot")
        if (
            type(self.index_states) is not tuple
            or not self.index_states
            or any(not isinstance(item, CatalogIndexState) for item in self.index_states)
            or tuple(item.lane.value for item in self.index_states)
            != tuple(sorted(item.lane.value for item in self.index_states))
            or len({item.lane for item in self.index_states}) != len(self.index_states)
            or {item.lane for item in self.index_states} != set(CatalogIndexLane)
        ):
            raise RetrievalContractError("catalog must declare every index lane exactly once")
        if type(self.temporal_facts) is not tuple or any(
            not isinstance(item, TemporalFact) for item in self.temporal_facts
        ):
            raise RetrievalContractError("catalog temporal facts must be a typed tuple")
        fact_keys = [canonical_json(item.to_private_payload()) for item in self.temporal_facts]
        if fact_keys != sorted(fact_keys) or len(fact_keys) != len(set(fact_keys)):
            raise RetrievalContractError("catalog temporal facts must be sorted and unique")
        if any(item.source_revision not in self.resolved_source.revisions for item in self.temporal_facts):
            raise RetrievalContractError("catalog temporal facts must belong to the resolved snapshot")

    def __repr__(self) -> str:
        return f"CatalogItem(source_kind={self.source_ref.source_kind.value!r}, private_projection=True)"

    @classmethod
    def create(
        cls,
        *,
        source_ref: SourceRef,
        resolved_source: ResolvedSource,
        canonical_title: str | None,
        visible_title: str | None,
        filename: str | None,
        aliases: Iterable[str],
        review_state: CatalogReviewState,
        ingest_state: CatalogIngestState,
        index_states: Iterable[CatalogIndexState],
        temporal_facts: Iterable[TemporalFact],
    ) -> CatalogItem:
        alias_values = tuple(aliases)
        index_values = tuple(index_states)
        facts = tuple(temporal_facts)
        if any(not isinstance(item, str) for item in alias_values):
            raise RetrievalContractError("catalog aliases must contain only private text")
        if any(not isinstance(item, CatalogIndexState) for item in index_values):
            raise RetrievalContractError("catalog index states must use the typed contract")
        if any(not isinstance(item, TemporalFact) for item in facts):
            raise RetrievalContractError("catalog temporal facts must use the typed contract")
        return cls(
            source_ref=source_ref,
            resolved_source=resolved_source,
            canonical_title=canonical_title,
            visible_title=visible_title,
            filename=filename,
            aliases=tuple(sorted(alias_values)),
            review_state=review_state,
            ingest_state=ingest_state,
            index_states=tuple(sorted(index_values, key=lambda item: item.lane.value)),
            temporal_facts=tuple(
                item
                for _key, item in sorted(
                    ((canonical_json(item.to_private_payload()), item) for item in facts),
                    key=lambda pair: pair[0],
                )
            ),
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "aliases": list(self.aliases),
            "canonical_title": self.canonical_title,
            "filename": self.filename,
            "index_states": [item.to_private_payload() for item in self.index_states],
            "ingest_state": self.ingest_state.value,
            "resolved_source": self.resolved_source.to_private_payload(),
            "review_state": self.review_state.value,
            "schema": CATALOG_ITEM_SCHEMA,
            "source_ref": self.source_ref.to_private_payload(),
            "temporal_facts": [item.to_private_payload() for item in self.temporal_facts],
            "visible_title": self.visible_title,
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> CatalogItem:
        payload = exact_object(
            value,
            frozenset(
                {
                    "aliases",
                    "canonical_title",
                    "filename",
                    "index_states",
                    "ingest_state",
                    "resolved_source",
                    "review_state",
                    "schema",
                    "source_ref",
                    "temporal_facts",
                    "visible_title",
                }
            ),
            label="catalog item",
        )
        if payload["schema"] != CATALOG_ITEM_SCHEMA:
            raise RetrievalContractError("catalog item schema is unsupported")
        aliases = payload["aliases"]
        indexes = payload["index_states"]
        facts = payload["temporal_facts"]
        if type(aliases) is not list or any(not isinstance(item, str) for item in aliases):
            raise RetrievalContractError("catalog aliases must be an array of private text")
        if type(indexes) is not list or type(facts) is not list:
            raise RetrievalContractError("catalog indexes and temporal facts must be arrays")
        titles = (payload["canonical_title"], payload["visible_title"], payload["filename"])
        if any(item is not None and not isinstance(item, str) for item in titles):
            raise RetrievalContractError("catalog titles and filename must be private text or null")
        return cls.create(
            source_ref=SourceRef.from_private_payload(payload["source_ref"]),
            resolved_source=ResolvedSource.from_private_payload(payload["resolved_source"]),
            canonical_title=titles[0],
            visible_title=titles[1],
            filename=titles[2],
            aliases=aliases,
            review_state=enum_value(
                CatalogReviewState,
                payload["review_state"],
                label="catalog review state",
            ),
            ingest_state=enum_value(
                CatalogIngestState,
                payload["ingest_state"],
                label="catalog ingest state",
            ),
            index_states=(CatalogIndexState.from_private_payload(item) for item in indexes),
            temporal_facts=(TemporalFact.from_private_payload(item) for item in facts),
        )

    @classmethod
    def parse_private(cls, value: str) -> CatalogItem:
        result = cls.from_private_payload(parse_canonical_object(value, label="catalog item"))
        if value != result.to_private_json():
            raise RetrievalContractError("catalog item JSON is not semantically canonical")
        return result


__all__ = [
    "CatalogIndexLane",
    "CatalogIndexState",
    "CatalogIndexStatus",
    "CatalogIngestState",
    "CatalogItem",
    "CatalogReviewState",
    "IndexIncompleteReason",
]
