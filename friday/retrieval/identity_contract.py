"""Stable source identity and mutable resolution snapshots.

These pure values neither authorize a caller nor read a store. Identifiers and
owner scope are exposed only by explicitly private serialization. The only
publishable identity is a keyed digest, preventing offline guessing of
low-entropy owner IDs and external-source names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from friday.retrieval._contract_utils import (
    RetrievalContractError,
    bounded_text,
    canonical_json,
    enum_value,
    exact_object,
    keyed_digest,
    lowercase_sha256,
    optional_bounded_text,
    parse_canonical_object,
)

SOURCE_REF_SCHEMA = "friday.source-ref.private.v1"
RESOLVED_SOURCE_SCHEMA = "friday.resolved-source.private.v1"

_RAW_ID_RE = re.compile(r"raw_[0-9a-f]{16}\Z")
_KNOWLEDGE_ID_RE = re.compile(r"ko_[A-Za-z0-9_-]{8,120}\Z")
_INBOX_ID_RE = re.compile(r"inbox_[0-9a-f]{16}\Z")
_OBSIDIAN_BINDING_ID_RE = re.compile(r"obsbind_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")


class SourceKind(StrEnum):
    DOCUMENT = "document"
    OBSIDIAN_NOTE = "obsidian_note"
    CONVERSATION = "conversation"
    WEB_CAPTURE = "web_capture"
    EXTERNAL_REGISTERED_SOURCE = "external_registered_source"
    GENERATED_ARTIFACT = "generated_artifact"


class CanonicalObjectKind(StrEnum):
    RAW_OBJECT = "raw_object"
    OBSIDIAN_BINDING = "obsidian_binding"
    CONVERSATION = "conversation"
    EXTERNAL_SOURCE = "external_source"


class AuthorityScope(StrEnum):
    TENANT = "tenant"
    PRINCIPAL = "principal"
    TENANT_PRINCIPAL = "tenant_principal"


_ROOT_KIND = {
    SourceKind.DOCUMENT: CanonicalObjectKind.RAW_OBJECT,
    SourceKind.OBSIDIAN_NOTE: CanonicalObjectKind.OBSIDIAN_BINDING,
    SourceKind.CONVERSATION: CanonicalObjectKind.CONVERSATION,
    SourceKind.WEB_CAPTURE: CanonicalObjectKind.RAW_OBJECT,
    SourceKind.EXTERNAL_REGISTERED_SOURCE: CanonicalObjectKind.EXTERNAL_SOURCE,
    SourceKind.GENERATED_ARTIFACT: CanonicalObjectKind.RAW_OBJECT,
}

_SOURCE_AUTHORITY_SCOPES = {
    SourceKind.DOCUMENT: frozenset({AuthorityScope.TENANT, AuthorityScope.TENANT_PRINCIPAL}),
    SourceKind.OBSIDIAN_NOTE: frozenset({AuthorityScope.PRINCIPAL}),
    SourceKind.CONVERSATION: frozenset({AuthorityScope.PRINCIPAL}),
    SourceKind.WEB_CAPTURE: frozenset({AuthorityScope.TENANT, AuthorityScope.TENANT_PRINCIPAL}),
    SourceKind.EXTERNAL_REGISTERED_SOURCE: frozenset({AuthorityScope.TENANT}),
    SourceKind.GENERATED_ARTIFACT: frozenset({AuthorityScope.TENANT_PRINCIPAL}),
}


def _canonical_object_id(kind: CanonicalObjectKind, value: object) -> str:
    if kind is CanonicalObjectKind.EXTERNAL_SOURCE:
        # Schema 38 keys data_sources by (tenant, name); existing names are not slugs.
        return bounded_text(value, label="external source name", maximum_bytes=200)
    if not isinstance(value, str):
        raise RetrievalContractError("canonical object ID must be an opaque identifier")
    pattern = {
        CanonicalObjectKind.RAW_OBJECT: _RAW_ID_RE,
        CanonicalObjectKind.OBSIDIAN_BINDING: _OBSIDIAN_BINDING_ID_RE,
        CanonicalObjectKind.CONVERSATION: _CONVERSATION_ID_RE,
    }[kind]
    if pattern.fullmatch(value) is None:
        raise RetrievalContractError("canonical object ID must be an existing opaque identifier")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class SourceRef:
    """Stable logical identity, never an authorization grant.

    Promotion, rename, movement, revision, and lifecycle transitions do not
    change this value. A message range is a locator within a conversation and
    is intentionally not accepted as a source root.
    """

    source_kind: SourceKind
    authority_scope: AuthorityScope
    tenant_id: str | None
    principal_id: str | None
    canonical_object_kind: CanonicalObjectKind
    canonical_object_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_kind, SourceKind)
            or not isinstance(self.authority_scope, AuthorityScope)
            or not isinstance(self.canonical_object_kind, CanonicalObjectKind)
        ):
            raise RetrievalContractError("source kinds must be closed enums")
        optional_bounded_text(self.tenant_id, label="tenant_id", maximum_bytes=200)
        optional_bounded_text(self.principal_id, label="principal_id", maximum_bytes=200)
        if self.authority_scope not in _SOURCE_AUTHORITY_SCOPES[self.source_kind]:
            raise RetrievalContractError("source kind and authority lookup axis disagree")
        expected_presence = {
            AuthorityScope.TENANT: (True, False),
            AuthorityScope.PRINCIPAL: (False, True),
            AuthorityScope.TENANT_PRINCIPAL: (True, True),
        }[self.authority_scope]
        if (self.tenant_id is not None, self.principal_id is not None) != expected_presence:
            raise RetrievalContractError("authority lookup IDs do not match the declared axis")
        if _ROOT_KIND[self.source_kind] is not self.canonical_object_kind:
            raise RetrievalContractError("source kind and stable root disagree")
        _canonical_object_id(self.canonical_object_kind, self.canonical_object_id)

    def __repr__(self) -> str:
        return f"SourceRef(source_kind={self.source_kind.value!r}, private_identity=True)"

    def to_private_payload(self) -> dict[str, object]:
        return {
            "authority_scope": self.authority_scope.value,
            "canonical_object_id": self.canonical_object_id,
            "canonical_object_kind": self.canonical_object_kind.value,
            "principal_id": self.principal_id,
            "schema": SOURCE_REF_SCHEMA,
            "source_kind": self.source_kind.value,
            "tenant_id": self.tenant_id,
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> SourceRef:
        payload = exact_object(
            value,
            frozenset(
                {
                    "canonical_object_id",
                    "canonical_object_kind",
                    "authority_scope",
                    "principal_id",
                    "schema",
                    "source_kind",
                    "tenant_id",
                }
            ),
            label="source ref",
        )
        if payload["schema"] != SOURCE_REF_SCHEMA:
            raise RetrievalContractError("source ref schema is unsupported")
        principal = payload["principal_id"]
        tenant = payload["tenant_id"]
        object_id = payload["canonical_object_id"]
        if principal is not None and not isinstance(principal, str):
            raise RetrievalContractError("principal_id must be private text or null")
        if tenant is not None and not isinstance(tenant, str):
            raise RetrievalContractError("tenant_id must be private text or null")
        if not isinstance(object_id, str):
            raise RetrievalContractError("source ref identifiers must be private text")
        return cls(
            source_kind=enum_value(SourceKind, payload["source_kind"], label="source kind"),
            authority_scope=enum_value(
                AuthorityScope,
                payload["authority_scope"],
                label="authority scope",
            ),
            tenant_id=tenant,
            principal_id=principal,
            canonical_object_kind=enum_value(
                CanonicalObjectKind,
                payload["canonical_object_kind"],
                label="canonical object kind",
            ),
            canonical_object_id=object_id,
        )

    @classmethod
    def parse_private(cls, value: str) -> SourceRef:
        result = cls.from_private_payload(parse_canonical_object(value, label="source ref"))
        if value != result.to_private_json():
            raise RetrievalContractError("source ref JSON is not semantically canonical")
        return result

    def logical_digest(self, privacy_key: bytes) -> str:
        """Return a domain-separated opaque handle safe for controlled publication."""

        return keyed_digest(b"friday/source-ref/v1", self.to_private_payload(), privacy_key)


class RepresentationKind(StrEnum):
    RAW_OBJECT = "raw_object"
    INBOX_ITEM = "inbox_item"
    KNOWLEDGE_OBJECT = "knowledge_object"
    OBSIDIAN_BINDING = "obsidian_binding"
    CONVERSATION = "conversation"
    EXTERNAL_SOURCE = "external_source"


_SOURCE_REPRESENTATIONS = {
    SourceKind.DOCUMENT: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
    SourceKind.OBSIDIAN_NOTE: frozenset({RepresentationKind.OBSIDIAN_BINDING}),
    SourceKind.CONVERSATION: frozenset({RepresentationKind.CONVERSATION}),
    SourceKind.WEB_CAPTURE: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
    SourceKind.EXTERNAL_REGISTERED_SOURCE: frozenset({RepresentationKind.EXTERNAL_SOURCE}),
    SourceKind.GENERATED_ARTIFACT: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class SourceRepresentation:
    kind: RepresentationKind
    object_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RepresentationKind):
            raise RetrievalContractError("representation kind must be a closed enum")
        if self.kind is RepresentationKind.KNOWLEDGE_OBJECT:
            if not isinstance(self.object_id, str) or _KNOWLEDGE_ID_RE.fullmatch(self.object_id) is None:
                raise RetrievalContractError("knowledge representation requires a canonical KO ID")
        elif self.kind is RepresentationKind.INBOX_ITEM:
            if not isinstance(self.object_id, str) or _INBOX_ID_RE.fullmatch(self.object_id) is None:
                raise RetrievalContractError("Inbox representation requires a canonical inbox ID")
        elif self.kind is RepresentationKind.EXTERNAL_SOURCE:
            bounded_text(self.object_id, label="external source name", maximum_bytes=200)
        else:
            _canonical_object_id(CanonicalObjectKind(self.kind.value), self.object_id)

    def __repr__(self) -> str:
        return f"SourceRepresentation(kind={self.kind.value!r}, private_identity=True)"

    def to_private_payload(self) -> dict[str, str]:
        return {"kind": self.kind.value, "object_id": self.object_id}

    @classmethod
    def from_private_payload(cls, value: object) -> SourceRepresentation:
        payload = exact_object(value, frozenset({"kind", "object_id"}), label="representation")
        object_id = payload["object_id"]
        if not isinstance(object_id, str):
            raise RetrievalContractError("representation object_id must be private text")
        return cls(
            kind=enum_value(RepresentationKind, payload["kind"], label="representation kind"),
            object_id=object_id,
        )


class LifecycleState(StrEnum):
    PENDING = "pending"
    CLASSIFIED = "classified"
    IGNORED = "ignored"
    ACTIVE = "active"
    ARCHIVED = "archived"
    DEPRECATED = "deprecated"
    DELETED = "deleted"
    TOMBSTONED = "tombstoned"
    UNAVAILABLE = "unavailable"


_LIFECYCLE_STATES = {
    RepresentationKind.RAW_OBJECT: frozenset({LifecycleState.ACTIVE, LifecycleState.DELETED}),
    RepresentationKind.INBOX_ITEM: frozenset(
        {
            LifecycleState.PENDING,
            LifecycleState.CLASSIFIED,
            LifecycleState.ARCHIVED,
            LifecycleState.IGNORED,
        }
    ),
    RepresentationKind.KNOWLEDGE_OBJECT: frozenset(
        {
            LifecycleState.ACTIVE,
            LifecycleState.ARCHIVED,
            LifecycleState.DEPRECATED,
            LifecycleState.DELETED,
        }
    ),
    RepresentationKind.OBSIDIAN_BINDING: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.TOMBSTONED, LifecycleState.DELETED}
    ),
    RepresentationKind.CONVERSATION: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.ARCHIVED, LifecycleState.DELETED}
    ),
    RepresentationKind.EXTERNAL_SOURCE: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.UNAVAILABLE, LifecycleState.DELETED}
    ),
}


@dataclass(frozen=True, slots=True)
class LifecycleRef:
    representation: SourceRepresentation
    state: LifecycleState

    def __post_init__(self) -> None:
        if not isinstance(self.representation, SourceRepresentation) or not isinstance(
            self.state, LifecycleState
        ):
            raise RetrievalContractError("lifecycle fields must use closed contract types")
        if self.state not in _LIFECYCLE_STATES[self.representation.kind]:
            raise RetrievalContractError("lifecycle state is invalid for the representation")

    def to_private_payload(self) -> dict[str, object]:
        return {"representation": self.representation.to_private_payload(), "state": self.state.value}

    @classmethod
    def from_private_payload(cls, value: object) -> LifecycleRef:
        payload = exact_object(value, frozenset({"representation", "state"}), label="lifecycle")
        return cls(
            representation=SourceRepresentation.from_private_payload(payload["representation"]),
            state=enum_value(LifecycleState, payload["state"], label="lifecycle state"),
        )


class RevisionKind(StrEnum):
    RAW_CONTENT_SHA256 = "raw_content_sha256"
    KNOWLEDGE_VERSION = "knowledge_version"
    OBSIDIAN_REVISION_SHA256 = "obsidian_revision_sha256"
    MESSAGE_LEDGER_SHA256 = "message_ledger_sha256"
    EXTERNAL_REVISION = "external_revision"


_REVISION_TARGET = {
    RevisionKind.RAW_CONTENT_SHA256: RepresentationKind.RAW_OBJECT,
    RevisionKind.KNOWLEDGE_VERSION: RepresentationKind.KNOWLEDGE_OBJECT,
    RevisionKind.OBSIDIAN_REVISION_SHA256: RepresentationKind.OBSIDIAN_BINDING,
    RevisionKind.MESSAGE_LEDGER_SHA256: RepresentationKind.CONVERSATION,
    RevisionKind.EXTERNAL_REVISION: RepresentationKind.EXTERNAL_SOURCE,
}


@dataclass(frozen=True, slots=True, repr=False)
class SourceRevision:
    representation: SourceRepresentation
    kind: RevisionKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.representation, SourceRepresentation) or not isinstance(
            self.kind, RevisionKind
        ):
            raise RetrievalContractError("revision fields must use closed contract types")
        if _REVISION_TARGET[self.kind] is not self.representation.kind:
            raise RetrievalContractError("revision kind and representation disagree")
        if not isinstance(self.value, str):
            raise RetrievalContractError("revision value must be private text")
        if self.kind is RevisionKind.KNOWLEDGE_VERSION:
            if not self.value.isdecimal() or self.value.startswith("0") or int(self.value) > 1_000_000_000:
                raise RetrievalContractError("knowledge version must be a canonical positive integer")
        elif self.kind is RevisionKind.EXTERNAL_REVISION:
            bounded_text(self.value, label="external revision", maximum_bytes=200)
        else:
            lowercase_sha256(self.value, label="source revision")

    def __repr__(self) -> str:
        return f"SourceRevision(kind={self.kind.value!r}, private_identity=True)"

    def to_private_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "representation": self.representation.to_private_payload(),
            "value": self.value,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> SourceRevision:
        payload = exact_object(
            value,
            frozenset({"kind", "representation", "value"}),
            label="source revision",
        )
        revision = payload["value"]
        if not isinstance(revision, str):
            raise RetrievalContractError("source revision value must be private text")
        return cls(
            representation=SourceRepresentation.from_private_payload(payload["representation"]),
            kind=enum_value(RevisionKind, payload["kind"], label="revision kind"),
            value=revision,
        )


@dataclass(frozen=True, slots=True, repr=False)
class RevalidationTarget:
    """Where fresh authorization must be checked; this value grants no authority."""

    representation: SourceRepresentation
    lookup_axis: AuthorityScope

    def __post_init__(self) -> None:
        if not isinstance(self.representation, SourceRepresentation) or not isinstance(
            self.lookup_axis, AuthorityScope
        ):
            raise RetrievalContractError("revalidation target must name a representation and lookup axis")

    def __repr__(self) -> str:
        return f"RevalidationTarget(kind={self.representation.kind.value!r}, private_identity=True)"

    def to_private_payload(self) -> dict[str, object]:
        return {
            "lookup_axis": self.lookup_axis.value,
            "representation": self.representation.to_private_payload(),
        }

    @classmethod
    def from_private_payload(cls, value: object) -> RevalidationTarget:
        payload = exact_object(
            value,
            frozenset({"lookup_axis", "representation"}),
            label="revalidation target",
        )
        return cls(
            SourceRepresentation.from_private_payload(payload["representation"]),
            enum_value(AuthorityScope, payload["lookup_axis"], label="authority lookup axis"),
        )


class _PrivatePayload(Protocol):
    def to_private_payload(self) -> Mapping[str, Any]: ...


PrivateT = TypeVar("PrivateT", bound=_PrivatePayload)


def _canonical_tuple(
    values: Iterable[PrivateT], *, label: str, expected_type: type[PrivateT]
) -> tuple[PrivateT, ...]:
    items = tuple(values)
    if any(type(item) is not expected_type for item in items):
        raise RetrievalContractError(f"{label} must use the typed contract")
    keyed = [(canonical_json(item.to_private_payload()), item) for item in items]
    if not keyed:
        raise RetrievalContractError(f"{label} must not be empty")
    keys = [key for key, _item in keyed]
    if len(keys) != len(set(keys)):
        raise RetrievalContractError(f"{label} must be unique")
    return tuple(item for _key, item in sorted(keyed, key=lambda pair: pair[0]))


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedSource:
    """Non-authoritative mutable-state snapshot from authoritative stores."""

    source_ref: SourceRef
    representations: tuple[SourceRepresentation, ...]
    lifecycle: tuple[LifecycleRef, ...]
    revisions: tuple[SourceRevision, ...]
    revalidation_targets: tuple[RevalidationTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, SourceRef):
            raise RetrievalContractError("resolved source requires a SourceRef")
        checks: tuple[tuple[str, tuple[object, ...], type[object]], ...] = (
            ("representations", self.representations, SourceRepresentation),
            ("lifecycle", self.lifecycle, LifecycleRef),
            ("revisions", self.revisions, SourceRevision),
            ("revalidation targets", self.revalidation_targets, RevalidationTarget),
        )
        for name, values, expected in checks:
            if type(values) is not tuple or not values or any(type(item) is not expected for item in values):
                raise RetrievalContractError(f"resolved source {name} must be a non-empty typed tuple")
            keys = [canonical_json(item.to_private_payload()) for item in values]  # type: ignore[attr-defined]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise RetrievalContractError(f"resolved source {name} must be sorted and unique")
        root = SourceRepresentation(
            RepresentationKind(self.source_ref.canonical_object_kind.value),
            self.source_ref.canonical_object_id,
        )
        representation_set = set(self.representations)
        if root not in representation_set:
            raise RetrievalContractError("resolved source must retain its stable root representation")
        representation_kinds = tuple(item.kind for item in self.representations)
        if len(representation_kinds) != len(set(representation_kinds)):
            raise RetrievalContractError(
                "resolved source permits at most one current representation per kind"
            )
        if not set(representation_kinds) <= _SOURCE_REPRESENTATIONS[self.source_ref.source_kind]:
            raise RetrievalContractError("source kind contains an unsupported representation")
        if any(item.representation not in representation_set for item in self.lifecycle):
            raise RetrievalContractError("lifecycle must address a declared representation")
        lifecycle_representations = tuple(item.representation for item in self.lifecycle)
        if (
            len(lifecycle_representations) != len(set(lifecycle_representations))
            or set(lifecycle_representations) != representation_set
        ):
            raise RetrievalContractError("every representation requires exactly one lifecycle state")
        if any(item.representation not in representation_set for item in self.revisions):
            raise RetrievalContractError("revision must address a declared representation")
        revision_representations = tuple(item.representation for item in self.revisions)
        evidence_representations = {
            item for item in self.representations if item.kind is not RepresentationKind.INBOX_ITEM
        }
        if (
            len(revision_representations) != len(set(revision_representations))
            or set(revision_representations) != evidence_representations
        ):
            raise RetrievalContractError(
                "every evidence representation requires exactly one current revision"
            )
        if any(item.representation not in representation_set for item in self.revalidation_targets):
            raise RetrievalContractError("revalidation target must address a declared representation")
        target_representations = tuple(item.representation for item in self.revalidation_targets)
        if (
            len(target_representations) != len(set(target_representations))
            or set(target_representations) != representation_set
        ):
            raise RetrievalContractError("every representation requires exactly one revalidation target")
        if any(item.lookup_axis is not self.source_ref.authority_scope for item in self.revalidation_targets):
            raise RetrievalContractError("revalidation lookup axis must equal the SourceRef authority scope")

    def __repr__(self) -> str:
        return f"ResolvedSource(source_kind={self.source_ref.source_kind.value!r}, private_snapshot=True)"

    @classmethod
    def create(
        cls,
        *,
        source_ref: SourceRef,
        representations: Iterable[SourceRepresentation],
        lifecycle: Iterable[LifecycleRef],
        revisions: Iterable[SourceRevision],
        revalidation_targets: Iterable[RevalidationTarget],
    ) -> ResolvedSource:
        return cls(
            source_ref=source_ref,
            representations=_canonical_tuple(
                representations,
                label="representations",
                expected_type=SourceRepresentation,
            ),
            lifecycle=_canonical_tuple(
                lifecycle,
                label="lifecycle",
                expected_type=LifecycleRef,
            ),
            revisions=_canonical_tuple(
                revisions,
                label="revisions",
                expected_type=SourceRevision,
            ),
            revalidation_targets=_canonical_tuple(
                revalidation_targets,
                label="revalidation targets",
                expected_type=RevalidationTarget,
            ),
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "lifecycle": [item.to_private_payload() for item in self.lifecycle],
            "representations": [item.to_private_payload() for item in self.representations],
            "revalidation_targets": [item.to_private_payload() for item in self.revalidation_targets],
            "revisions": [item.to_private_payload() for item in self.revisions],
            "schema": RESOLVED_SOURCE_SCHEMA,
            "source_ref": self.source_ref.to_private_payload(),
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> ResolvedSource:
        payload = exact_object(
            value,
            frozenset(
                {
                    "lifecycle",
                    "representations",
                    "revalidation_targets",
                    "revisions",
                    "schema",
                    "source_ref",
                }
            ),
            label="resolved source",
        )
        if payload["schema"] != RESOLVED_SOURCE_SCHEMA:
            raise RetrievalContractError("resolved source schema is unsupported")
        representations = payload["representations"]
        lifecycle = payload["lifecycle"]
        revisions = payload["revisions"]
        targets = payload["revalidation_targets"]
        if any(type(item) is not list for item in (representations, lifecycle, revisions, targets)):
            raise RetrievalContractError("resolved source collections must be arrays")
        return cls.create(
            source_ref=SourceRef.from_private_payload(payload["source_ref"]),
            representations=(SourceRepresentation.from_private_payload(item) for item in representations),
            lifecycle=(LifecycleRef.from_private_payload(item) for item in lifecycle),
            revisions=(SourceRevision.from_private_payload(item) for item in revisions),
            revalidation_targets=(RevalidationTarget.from_private_payload(item) for item in targets),
        )

    @classmethod
    def parse_private(cls, value: str) -> ResolvedSource:
        result = cls.from_private_payload(parse_canonical_object(value, label="resolved source"))
        if value != result.to_private_json():
            raise RetrievalContractError("resolved source JSON is not semantically canonical")
        return result

    def logical_digest(self, privacy_key: bytes) -> str:
        return self.source_ref.logical_digest(privacy_key)

    def snapshot_digest(self, privacy_key: bytes) -> str:
        return keyed_digest(b"friday/resolved-source/v1", self.to_private_payload(), privacy_key)


RetrievalIdentityContractError = RetrievalContractError

__all__ = [
    "AuthorityScope",
    "CanonicalObjectKind",
    "LifecycleRef",
    "LifecycleState",
    "RepresentationKind",
    "ResolvedSource",
    "RetrievalContractError",
    "RetrievalIdentityContractError",
    "RevalidationTarget",
    "RevisionKind",
    "SourceKind",
    "SourceRef",
    "SourceRepresentation",
    "SourceRevision",
]
