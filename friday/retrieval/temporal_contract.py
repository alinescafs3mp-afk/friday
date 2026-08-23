"""Typed temporal facts with exact-role matching and no silent substitution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from friday.retrieval._contract_utils import (
    RetrievalContractError,
    canonical_json,
    canonical_utc,
    enum_value,
    exact_object,
    parse_canonical_object,
    utc_text,
)
from friday.retrieval.identity_contract import RepresentationKind, RevisionKind, SourceRevision

TEMPORAL_FACT_SCHEMA = "friday.temporal-fact.private.v1"


class TemporalRole(StrEnum):
    DOCUMENT_CREATED_AT = "document_created_at"
    DOCUMENT_MODIFIED_AT = "document_modified_at"
    RECEIVED_AT = "received_at"
    UPLOADED_AT = "uploaded_at"
    INDEXED_AT = "indexed_at"
    EVENT_DATE = "event_date"
    MENTIONED_DATE = "mentioned_date"
    CONVERSATION_TIME = "conversation_time"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
    KNOWLEDGE_PROJECTION_CREATED_AT = "knowledge_projection_created_at"
    KNOWLEDGE_PROJECTION_MODIFIED_AT = "knowledge_projection_modified_at"
    LEGACY_UNCLASSIFIED_DOCUMENT_DATE = "legacy_unclassified_document_date"


class TemporalValueKind(StrEnum):
    DATE_INTERVAL = "date_interval"
    INSTANT = "instant"


class TemporalPrecision(StrEnum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    INSTANT = "instant"


class TemporalOrigin(StrEnum):
    STORAGE_COLUMN = "storage_column"
    SOURCE_METADATA = "source_metadata"
    PARSER_EXTRACTED = "parser_extracted"
    EXTERNAL_AUTHORITY = "external_authority"
    USER_ASSERTED = "user_asserted"
    KNOWLEDGE_PROJECTION = "knowledge_projection"
    LEGACY_COLLAPSED = "legacy_collapsed"


_PROJECTION_ROLES = frozenset(
    {
        TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT,
        TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT,
    }
)
_INSTANT_ONLY_ROLES = frozenset(
    {
        TemporalRole.RECEIVED_AT,
        TemporalRole.UPLOADED_AT,
        TemporalRole.INDEXED_AT,
        TemporalRole.CONVERSATION_TIME,
        *_PROJECTION_ROLES,
    }
)
_EXTRACTED_ROLES = frozenset(
    {TemporalRole.EVENT_DATE, TemporalRole.MENTIONED_DATE, TemporalRole.VALID_FROM, TemporalRole.VALID_TO}
)


def _canonical_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise RetrievalContractError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RetrievalContractError(f"{label} must be an ISO date") from exc
    if value != parsed.isoformat():
        raise RetrievalContractError(f"{label} must be a canonical ISO date")
    return parsed


def _date_interval(start: date, precision: TemporalPrecision) -> tuple[str, str]:
    try:
        if precision is TemporalPrecision.YEAR:
            if start.month != 1 or start.day != 1:
                raise RetrievalContractError("year precision must start on January 1")
            end = date(start.year + 1, 1, 1)
        elif precision is TemporalPrecision.MONTH:
            if start.day != 1:
                raise RetrievalContractError("month precision must start on day 1")
            end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
        elif precision is TemporalPrecision.DAY:
            end = start + timedelta(days=1)
        else:
            raise RetrievalContractError("date intervals require year, month, or day precision")
    except (OverflowError, ValueError) as exc:
        raise RetrievalContractError("date interval cannot be represented canonically") from exc
    return start.isoformat(), end.isoformat()


@dataclass(frozen=True, slots=True, repr=False)
class TemporalFact:
    source_revision: SourceRevision
    role: TemporalRole
    value_kind: TemporalValueKind
    precision: TemporalPrecision
    start: str
    end: str | None
    origin: TemporalOrigin

    def __post_init__(self) -> None:
        if not isinstance(self.source_revision, SourceRevision):
            raise RetrievalContractError("temporal fact requires an exact source revision")
        if not all(
            isinstance(item, expected)
            for item, expected in (
                (self.role, TemporalRole),
                (self.value_kind, TemporalValueKind),
                (self.precision, TemporalPrecision),
                (self.origin, TemporalOrigin),
            )
        ):
            raise RetrievalContractError("temporal fact enums must use closed values")
        is_projection_role = self.role in _PROJECTION_ROLES
        if is_projection_role != (self.origin is TemporalOrigin.KNOWLEDGE_PROJECTION):
            raise RetrievalContractError("KO projection dates require explicit projection roles and origin")
        if is_projection_role and (
            self.source_revision.representation.kind is not RepresentationKind.KNOWLEDGE_OBJECT
            or self.source_revision.kind is not RevisionKind.KNOWLEDGE_VERSION
        ):
            raise RetrievalContractError("KO projection dates must anchor to an exact KO revision")
        is_legacy_role = self.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE
        if is_legacy_role != (self.origin is TemporalOrigin.LEGACY_COLLAPSED):
            raise RetrievalContractError("legacy collapsed dates require the explicit legacy role")
        if is_legacy_role and self.precision is not TemporalPrecision.DAY:
            raise RetrievalContractError("legacy document_date requires exact day precision")
        if self.role is TemporalRole.CONVERSATION_TIME and self.origin is not TemporalOrigin.STORAGE_COLUMN:
            raise RetrievalContractError("conversation_time must retain storage-column provenance")
        if self.role is TemporalRole.CONVERSATION_TIME and (
            self.source_revision.representation.kind is not RepresentationKind.CONVERSATION
            or self.source_revision.kind is not RevisionKind.MESSAGE_LEDGER_SHA256
        ):
            raise RetrievalContractError("conversation_time must anchor to a message-ledger revision")
        if self.origin is TemporalOrigin.PARSER_EXTRACTED and self.role not in _EXTRACTED_ROLES:
            raise RetrievalContractError("extracted dates must retain a content date role")
        if self.role in _INSTANT_ONLY_ROLES and self.value_kind is not TemporalValueKind.INSTANT:
            raise RetrievalContractError("this temporal role requires an exact instant")
        if self.value_kind is TemporalValueKind.INSTANT:
            if self.precision is not TemporalPrecision.INSTANT or self.end is not None:
                raise RetrievalContractError("instants require instant precision and no interval end")
            canonical_utc(self.start, label="temporal instant")
        else:
            if self.precision is TemporalPrecision.INSTANT or self.end is None:
                raise RetrievalContractError("date intervals require bounded date precision and an end")
            parsed_start = _canonical_date(self.start, label="temporal start")
            _canonical_date(self.end, label="temporal end")
            if _date_interval(parsed_start, self.precision) != (self.start, self.end):
                raise RetrievalContractError("temporal interval is not canonical for its precision")

    @classmethod
    def for_date(
        cls,
        *,
        role: TemporalRole,
        value: date,
        precision: TemporalPrecision,
        origin: TemporalOrigin,
        source_revision: SourceRevision,
    ) -> TemporalFact:
        if not isinstance(value, date) or isinstance(value, datetime):
            raise RetrievalContractError("date facts require a date, not an instant")
        start, end = _date_interval(value, precision)
        return cls(
            source_revision,
            role,
            TemporalValueKind.DATE_INTERVAL,
            precision,
            start,
            end,
            origin,
        )

    @classmethod
    def for_instant(
        cls,
        *,
        role: TemporalRole,
        value: datetime,
        origin: TemporalOrigin,
        source_revision: SourceRevision,
    ) -> TemporalFact:
        return cls(
            source_revision,
            role,
            TemporalValueKind.INSTANT,
            TemporalPrecision.INSTANT,
            utc_text(value, label="temporal instant"),
            None,
            origin,
        )

    def matches_role(self, requested_role: TemporalRole) -> bool:
        return isinstance(requested_role, TemporalRole) and self.role is requested_role

    def __repr__(self) -> str:
        return f"TemporalFact(role={self.role.value!r}, private_source_revision=True)"

    def to_private_payload(self) -> dict[str, object]:
        return {
            "end": self.end,
            "origin": self.origin.value,
            "precision": self.precision.value,
            "role": self.role.value,
            "schema": TEMPORAL_FACT_SCHEMA,
            "start": self.start,
            "source_revision": self.source_revision.to_private_payload(),
            "value_kind": self.value_kind.value,
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> TemporalFact:
        payload = exact_object(
            value,
            frozenset(
                {
                    "end",
                    "origin",
                    "precision",
                    "role",
                    "schema",
                    "source_revision",
                    "start",
                    "value_kind",
                }
            ),
            label="temporal fact",
        )
        if payload["schema"] != TEMPORAL_FACT_SCHEMA:
            raise RetrievalContractError("temporal fact schema is unsupported")
        start = payload["start"]
        end = payload["end"]
        if not isinstance(start, str) or (end is not None and not isinstance(end, str)):
            raise RetrievalContractError("temporal bounds must be text or null")
        return cls(
            source_revision=SourceRevision.from_private_payload(payload["source_revision"]),
            role=enum_value(TemporalRole, payload["role"], label="temporal role"),
            value_kind=enum_value(
                TemporalValueKind,
                payload["value_kind"],
                label="temporal value kind",
            ),
            precision=enum_value(
                TemporalPrecision,
                payload["precision"],
                label="temporal precision",
            ),
            start=start,
            end=end,
            origin=enum_value(TemporalOrigin, payload["origin"], label="temporal origin"),
        )

    @classmethod
    def parse_private(cls, value: str) -> TemporalFact:
        result = cls.from_private_payload(parse_canonical_object(value, label="temporal fact"))
        if value != result.to_private_json():
            raise RetrievalContractError("temporal fact JSON is not semantically canonical")
        return result


def temporal_facts_for_role(
    facts: Iterable[TemporalFact], requested_role: TemporalRole
) -> tuple[TemporalFact, ...]:
    """Return exact-role matches only; legacy and projection roles never substitute."""

    if not isinstance(requested_role, TemporalRole):
        raise RetrievalContractError("requested temporal role must be a closed enum")
    values = tuple(facts)
    if any(not isinstance(item, TemporalFact) for item in values):
        raise RetrievalContractError("temporal facts must use the typed contract")
    return tuple(item for item in values if item.matches_role(requested_role))


__all__ = [
    "TemporalFact",
    "TemporalOrigin",
    "TemporalPrecision",
    "TemporalRole",
    "TemporalValueKind",
    "temporal_facts_for_role",
]
