"""Pure catalog of already-observed coding-archive member metadata.

The catalog is a frozen hand-off between archive inspection and later planning.
It never opens an archive, reads a member path, or touches the filesystem.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.coding_archive_extract_admission import (
    MAX_ARCHIVE_MEMBER_COUNT,
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
)

CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA = "friday.coding-archive-member-catalog.v1"
MAX_CATALOG_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_CATALOG_MEMBER_COUNT = MAX_ARCHIVE_MEMBER_COUNT

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class CodingArchiveMemberCatalogError(ValueError):
    """A catalog identity, member sequence, or result is malformed."""


class CodingArchiveMemberCatalogState(StrEnum):
    """Closed outcomes for one observed archive-member catalog."""

    EMPTY = "empty"
    CATALOGUED = "catalogued"
    BLOCKED = "blocked"


class CodingArchiveMemberCatalogReason(StrEnum):
    """Closed reason for one archive-member catalog."""

    NO_MEMBERS = "no_members"
    ALL_MEMBERS_CATALOGUED = "all_members_catalogued"
    MEMBER_COUNT_LIMIT = "member_count_limit"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingArchiveMemberCatalogError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingArchiveMemberCatalogState:
    try:
        return CodingArchiveMemberCatalogState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveMemberCatalogError("catalog_closed") from exc


def _reason(value: object) -> CodingArchiveMemberCatalogReason:
    try:
        return CodingArchiveMemberCatalogReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingArchiveMemberCatalogError("reason_closed") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CATALOG_MEMBER_COUNT:
        _fail(field, "range")
    return cast(int, value)


def _member(value: object) -> CodingArchiveMemberV1:
    if isinstance(value, CodingArchiveMemberV1):
        return value
    if not isinstance(value, Mapping):
        _fail("member", "type")
    allowed = {
        "path",
        "name",
        "compressed_size",
        "compressed_bytes",
        "compressed",
        "uncompressed_size",
        "uncompressed_bytes",
        "uncompressed",
        "link_kind",
        "link_type",
        "link",
        "file_kind",
        "kind",
        "type",
    }
    if set(value) - allowed:
        _fail("member", "unknown_fields")
    try:
        return CodingArchiveMemberV1(
            path=cast(str, value.get("path", value.get("name"))),
            compressed_size=cast(
                int,
                value.get("compressed_size", value.get("compressed_bytes", value.get("compressed"))),
            ),
            uncompressed_size=cast(
                int,
                value.get(
                    "uncompressed_size",
                    value.get("uncompressed_bytes", value.get("uncompressed")),
                ),
            ),
            link_kind=cast(
                CodingArchiveLinkKind,
                value.get("link_kind", value.get("link_type", value.get("link"))),
            ),
            file_kind=cast(
                CodingArchiveFileKind,
                value.get("file_kind", value.get("kind", value.get("type"))),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise CodingArchiveMemberCatalogError("member_invalid") from exc


def _members(value: object) -> tuple[CodingArchiveMemberV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("members", "sequence")
    if len(value) > MAX_CATALOG_MEMBER_COUNT:
        _fail("members", "count")
    return tuple(_member(item) for item in value)


@dataclass(frozen=True, slots=True)
class CodingArchiveMemberCatalogV1:
    """Immutable catalog of the observed archive members."""

    catalog_id: str
    authenticated_turn_id: str
    catalog: CodingArchiveMemberCatalogState
    catalogued_member_count: int
    member_count: int
    reason: CodingArchiveMemberCatalogReason
    members: tuple[CodingArchiveMemberV1, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.catalog_id, field="catalog_id", maximum=MAX_CATALOG_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        catalog = _state(self.catalog)
        reason = _reason(self.reason)
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(self, "reason", reason)
        catalogued = _count(self.catalogued_member_count, field="catalogued_member_count")
        members = _count(self.member_count, field="member_count")
        if type(self.members) is not tuple or len(self.members) > MAX_CATALOG_MEMBER_COUNT:
            _fail("members", "immutable")
        if any(not isinstance(member, CodingArchiveMemberV1) for member in self.members):
            _fail("members", "item")
        if catalogued > members or len(self.members) != catalogued:
            _fail("member_counts", "inconsistent")
        if catalog is CodingArchiveMemberCatalogState.BLOCKED and (catalogued or members or self.members):
            _fail("blocked_members", "nonempty")
        if catalog is CodingArchiveMemberCatalogState.EMPTY and (catalogued or members or self.members):
            _fail("empty_members", "nonempty")
        if catalog is CodingArchiveMemberCatalogState.CATALOGUED and (members == 0 or catalogued != members):
            _fail("catalogued_members", "inconsistent")

    @property
    def state(self) -> CodingArchiveMemberCatalogState:
        return self.catalog

    @property
    def closed_catalog(self) -> CodingArchiveMemberCatalogState:
        return self.catalog

    @property
    def catalogued_count(self) -> int:
        return self.catalogued_member_count

    @property
    def decision(self) -> CodingArchiveMemberCatalogState:
        return self.catalog

    @property
    def closed_reason(self) -> CodingArchiveMemberCatalogReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA,
            "catalog_id": self.catalog_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "catalog": self.catalog.value,
            "catalogued_member_count": self.catalogued_member_count,
            "member_count": self.member_count,
            "reason": self.reason.value,
            "members": [member.to_mapping() for member in self.members],
        }


MemberCatalogState = CodingArchiveMemberCatalogState
MemberCatalogReason = CodingArchiveMemberCatalogReason
CodingArchiveMemberFactsV1 = CodingArchiveMemberV1
CodingArchiveMemberFactV1 = CodingArchiveMemberV1
CodingArchiveMemberCatalog = CodingArchiveMemberCatalogV1
CodingArchiveCatalogState = CodingArchiveMemberCatalogState
CodingArchiveCatalogReason = CodingArchiveMemberCatalogReason


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "catalog_id",
        "authenticated_turn_id",
        "members",
        "member_facts",
        "archive_members",
        "catalog",
        "state",
        "catalogued_member_count",
        "member_count",
        "reason",
    }
    if set(raw) - known:
        _fail("catalog", "unknown_fields")


def build_coding_archive_member_catalog(
    catalog_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    members: object = (),
) -> CodingArchiveMemberCatalogV1:
    """Catalog supplied member metadata without accessing archive contents."""

    if isinstance(catalog_id, Mapping):
        raw = catalog_id
        _known_mapping_keys(raw)
        if raw.get("schema", CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA) != CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA:
            _fail("schema")
        output_keys = {
            "catalog",
            "state",
            "catalogued_member_count",
            "member_count",
            "reason",
        }
        fact_aliases = {"member_facts", "archive_members"}
        if output_keys.intersection(raw) and fact_aliases.intersection(raw):
            _fail("catalog", "duplicate_representations")
        if output_keys.intersection(raw):
            return CodingArchiveMemberCatalogV1(
                catalog_id=cast(str, raw.get("catalog_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                catalog=cast(CodingArchiveMemberCatalogState, raw.get("catalog", raw.get("state"))),
                catalogued_member_count=cast(int, raw.get("catalogued_member_count")),
                member_count=cast(int, raw.get("member_count")),
                reason=cast(CodingArchiveMemberCatalogReason, raw.get("reason")),
                members=_members(raw.get("members", ())),
            )
        catalog_id = cast(str, raw.get("catalog_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        members = raw.get("members", raw.get("member_facts", raw.get("archive_members", ())))

    catalog_key = _identifier(catalog_id, field="catalog_id", maximum=MAX_CATALOG_ID_CHARS)
    turn_key = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        member_values = _members(members)
    except CodingArchiveMemberCatalogError as exc:
        reason = (
            CodingArchiveMemberCatalogReason.MEMBER_COUNT_LIMIT
            if "members_count" in str(exc)
            else CodingArchiveMemberCatalogReason.INVALID_FACTS
        )
        return CodingArchiveMemberCatalogV1(
            catalog_id=catalog_key,
            authenticated_turn_id=turn_key,
            catalog=CodingArchiveMemberCatalogState.BLOCKED,
            catalogued_member_count=0,
            member_count=0,
            reason=reason,
            members=(),
        )
    if not member_values:
        return CodingArchiveMemberCatalogV1(
            catalog_id=catalog_key,
            authenticated_turn_id=turn_key,
            catalog=CodingArchiveMemberCatalogState.EMPTY,
            catalogued_member_count=0,
            member_count=0,
            reason=CodingArchiveMemberCatalogReason.NO_MEMBERS,
            members=(),
        )
    count = len(member_values)
    return CodingArchiveMemberCatalogV1(
        catalog_id=catalog_key,
        authenticated_turn_id=turn_key,
        catalog=CodingArchiveMemberCatalogState.CATALOGUED,
        catalogued_member_count=count,
        member_count=count,
        reason=CodingArchiveMemberCatalogReason.ALL_MEMBERS_CATALOGUED,
        members=member_values,
    )


def validate_coding_archive_member_catalog(value: object) -> bool:
    """Return whether a catalog object or serialized catalog is valid."""

    try:
        if isinstance(value, CodingArchiveMemberCatalogV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        _known_mapping_keys(value)
        if value.get("schema") != CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA:
            return False
        required = {
            "schema",
            "catalog_id",
            "authenticated_turn_id",
            "catalog",
            "catalogued_member_count",
            "member_count",
            "reason",
            "members",
        }
        if set(value) != required:
            return False
        return (
            CodingArchiveMemberCatalogV1(
                catalog_id=cast(str, value.get("catalog_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                catalog=cast(CodingArchiveMemberCatalogState, value.get("catalog")),
                catalogued_member_count=cast(int, value.get("catalogued_member_count")),
                member_count=cast(int, value.get("member_count")),
                reason=cast(CodingArchiveMemberCatalogReason, value.get("reason")),
                members=_members(value.get("members")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


build_archive_member_catalog = build_coding_archive_member_catalog
catalog_coding_archive_members = build_coding_archive_member_catalog
validate_archive_member_catalog = validate_coding_archive_member_catalog


__all__ = [
    "CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA",
    "MAX_ARCHIVE_MEMBER_COUNT",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_CATALOG_ID_CHARS",
    "MAX_CATALOG_MEMBER_COUNT",
    "CodingArchiveCatalogReason",
    "CodingArchiveCatalogState",
    "CodingArchiveMemberCatalog",
    "CodingArchiveMemberCatalogError",
    "CodingArchiveMemberCatalogReason",
    "CodingArchiveMemberCatalogState",
    "CodingArchiveMemberCatalogV1",
    "CodingArchiveMemberFactV1",
    "CodingArchiveMemberFactsV1",
    "MemberCatalogReason",
    "MemberCatalogState",
    "build_archive_member_catalog",
    "build_coding_archive_member_catalog",
    "catalog_coding_archive_members",
    "validate_archive_member_catalog",
    "validate_coding_archive_member_catalog",
]
