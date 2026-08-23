"""Typed evidence-segment identity over stable sources and exact revisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from friday.retrieval._contract_utils import (
    RetrievalContractError,
    bounded_count,
    bounded_text,
    canonical_json,
    canonical_utc,
    enum_value,
    exact_object,
    keyed_digest,
    lowercase_sha256,
    parse_canonical_object,
    utc_text,
)
from friday.retrieval.identity_contract import (
    CanonicalObjectKind,
    RepresentationKind,
    ResolvedSource,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRevision,
)

PASSAGE_REF_SCHEMA = "friday.passage-ref.private.v1"
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")


def _count(value: object, *, label: str) -> int:
    result = bounded_count(value, label=label)
    assert result is not None
    return result


class PassageLocatorKind(StrEnum):
    TEXT_SPAN = "text_span"
    MESSAGE_WINDOW = "message_window"


@dataclass(frozen=True, slots=True)
class TextSpanLocator:
    """A body-free Python/codepoint-character span in one exact revision."""

    chunk_index: int
    start_char: int
    end_char: int

    def __post_init__(self) -> None:
        _count(self.chunk_index, label="chunk_index")
        _count(self.start_char, label="start_char")
        _count(self.end_char, label="end_char")
        if self.end_char <= self.start_char:
            raise RetrievalContractError("text span must be non-empty and half-open")

    def to_private_payload(self) -> dict[str, object]:
        return {
            "chunk_index": self.chunk_index,
            "end_char": self.end_char,
            "kind": PassageLocatorKind.TEXT_SPAN.value,
            "start_char": self.start_char,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> TextSpanLocator:
        payload = exact_object(
            value,
            frozenset({"chunk_index", "end_char", "kind", "start_char"}),
            label="text span locator",
        )
        if payload["kind"] != PassageLocatorKind.TEXT_SPAN.value:
            raise RetrievalContractError("text span locator kind is invalid")
        return cls(
            chunk_index=_count(payload["chunk_index"], label="chunk_index"),
            start_char=_count(payload["start_char"], label="start_char"),
            end_char=_count(payload["end_char"], label="end_char"),
        )


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True, repr=False)
class MessageWindowLocator:
    """A body-free range anchored to message boundaries in one conversation."""

    first_message_id: str
    last_message_id: str
    start_at: str
    end_at: str
    context_before: int
    context_after: int
    matched_role: MessageRole | None = None

    def __post_init__(self) -> None:
        if (
            _MESSAGE_ID_RE.fullmatch(self.first_message_id) is None
            or _MESSAGE_ID_RE.fullmatch(self.last_message_id) is None
        ):
            raise RetrievalContractError("message window requires canonical boundary message IDs")
        canonical_utc(self.start_at, label="message window start")
        canonical_utc(self.end_at, label="message window end")
        if self.end_at <= self.start_at:
            raise RetrievalContractError("message window must be a non-empty UTC half-open interval")
        _count(self.context_before, label="context_before")
        _count(self.context_after, label="context_after")
        if self.matched_role is not None and not isinstance(self.matched_role, MessageRole):
            raise RetrievalContractError("matched_role must be a closed enum or null")

    def __repr__(self) -> str:
        return "MessageWindowLocator(private_message_boundaries=True)"

    @classmethod
    def create(
        cls,
        *,
        first_message_id: str,
        last_message_id: str,
        start_at: datetime,
        end_at: datetime,
        context_before: int = 0,
        context_after: int = 0,
        matched_role: MessageRole | None = None,
    ) -> MessageWindowLocator:
        return cls(
            first_message_id=first_message_id,
            last_message_id=last_message_id,
            start_at=utc_text(start_at, label="message window start"),
            end_at=utc_text(end_at, label="message window end"),
            context_before=context_before,
            context_after=context_after,
            matched_role=matched_role,
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "context_after": self.context_after,
            "context_before": self.context_before,
            "end_at": self.end_at,
            "first_message_id": self.first_message_id,
            "kind": PassageLocatorKind.MESSAGE_WINDOW.value,
            "last_message_id": self.last_message_id,
            "matched_role": self.matched_role.value if self.matched_role is not None else None,
            "start_at": self.start_at,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> MessageWindowLocator:
        payload = exact_object(
            value,
            frozenset(
                {
                    "context_after",
                    "context_before",
                    "end_at",
                    "first_message_id",
                    "kind",
                    "last_message_id",
                    "matched_role",
                    "start_at",
                }
            ),
            label="message window locator",
        )
        if payload["kind"] != PassageLocatorKind.MESSAGE_WINDOW.value:
            raise RetrievalContractError("message window locator kind is invalid")
        strings = [
            payload["first_message_id"],
            payload["last_message_id"],
            payload["start_at"],
            payload["end_at"],
        ]
        if any(not isinstance(item, str) for item in strings):
            raise RetrievalContractError("message window boundaries must be private text")
        role = payload["matched_role"]
        return cls(
            first_message_id=strings[0],
            last_message_id=strings[1],
            start_at=strings[2],
            end_at=strings[3],
            context_before=_count(payload["context_before"], label="context_before"),
            context_after=_count(payload["context_after"], label="context_after"),
            matched_role=None if role is None else enum_value(MessageRole, role, label="matched role"),
        )


PassageLocator: TypeAlias = TextSpanLocator | MessageWindowLocator


class EmbeddingCompatibility(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"
    MISSING = "missing"
    BACKFILL_PENDING = "backfill_pending"
    NOT_APPLICABLE = "not_applicable"


_INDEXED_EMBEDDING_STATES = frozenset(
    {
        EmbeddingCompatibility.CURRENT,
        EmbeddingCompatibility.STALE,
        EmbeddingCompatibility.INCOMPATIBLE,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class EmbeddingIdentity:
    compatibility: EmbeddingCompatibility
    model_id: str | None
    dimensions: int | None
    source_version: int | None
    chunk_scheme: str | None
    chunk_content_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.compatibility, EmbeddingCompatibility):
            raise RetrievalContractError("embedding compatibility must be a closed enum")
        values_present = all(
            value is not None
            for value in (
                self.model_id,
                self.dimensions,
                self.source_version,
                self.chunk_scheme,
                self.chunk_content_sha256,
            )
        )
        if self.compatibility in _INDEXED_EMBEDDING_STATES:
            if not values_present:
                raise RetrievalContractError("indexed embedding states require exact model identity")
            bounded_text(self.model_id, label="embedding model", maximum_bytes=200)
            dimensions = _count(self.dimensions, label="embedding dimensions")
            if dimensions == 0:
                raise RetrievalContractError("embedding dimensions must be positive")
            _count(self.source_version, label="embedding source_version")
            bounded_text(self.chunk_scheme, label="embedding chunk_scheme", maximum_bytes=200)
            lowercase_sha256(self.chunk_content_sha256, label="chunk content SHA-256")
        elif any(
            value is not None
            for value in (
                self.model_id,
                self.dimensions,
                self.source_version,
                self.chunk_scheme,
                self.chunk_content_sha256,
            )
        ):
            raise RetrievalContractError("unindexed embedding states cannot carry guessed identity")

    def __repr__(self) -> str:
        return f"EmbeddingIdentity(compatibility={self.compatibility.value!r}, private_model=True)"

    @classmethod
    def indexed(
        cls,
        compatibility: EmbeddingCompatibility,
        *,
        model_id: str,
        dimensions: int,
        source_version: int,
        chunk_scheme: str,
        chunk_content_sha256: str,
    ) -> EmbeddingIdentity:
        return cls(
            compatibility,
            model_id,
            dimensions,
            source_version,
            chunk_scheme,
            chunk_content_sha256,
        )

    @classmethod
    def unindexed(cls, compatibility: EmbeddingCompatibility) -> EmbeddingIdentity:
        return cls(compatibility, None, None, None, None, None)

    def to_private_payload(self) -> dict[str, object]:
        return {
            "compatibility": self.compatibility.value,
            "chunk_content_sha256": self.chunk_content_sha256,
            "chunk_scheme": self.chunk_scheme,
            "dimensions": self.dimensions,
            "model_id": self.model_id,
            "source_version": self.source_version,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> EmbeddingIdentity:
        payload = exact_object(
            value,
            frozenset(
                {
                    "chunk_content_sha256",
                    "chunk_scheme",
                    "compatibility",
                    "dimensions",
                    "model_id",
                    "source_version",
                }
            ),
            label="embedding identity",
        )
        model = payload["model_id"]
        chunk_scheme = payload["chunk_scheme"]
        chunk_hash = payload["chunk_content_sha256"]
        dimensions = payload["dimensions"]
        if model is not None and not isinstance(model, str):
            raise RetrievalContractError("embedding model must be private text or null")
        if chunk_scheme is not None and not isinstance(chunk_scheme, str):
            raise RetrievalContractError("chunk scheme must be private text or null")
        if chunk_hash is not None and not isinstance(chunk_hash, str):
            raise RetrievalContractError("chunk digest must be private text or null")
        parsed_dimensions = bounded_count(dimensions, label="embedding dimensions", optional=True)
        source_version = bounded_count(
            payload["source_version"],
            label="embedding source_version",
            optional=True,
        )
        return cls(
            compatibility=enum_value(
                EmbeddingCompatibility,
                payload["compatibility"],
                label="embedding compatibility",
            ),
            model_id=model,
            dimensions=parsed_dimensions,
            source_version=source_version,
            chunk_scheme=chunk_scheme,
            chunk_content_sha256=chunk_hash,
        )


@dataclass(frozen=True, slots=True, repr=False)
class PassageRef:
    """Stable segment locator; copied excerpts are never part of its identity."""

    source_ref: SourceRef
    source_revision: SourceRevision
    locator: PassageLocator
    passage_index_version: str
    embedding: EmbeddingIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, SourceRef) or not isinstance(self.source_revision, SourceRevision):
            raise RetrievalContractError("passage requires typed source and revision")
        if not isinstance(self.locator, (TextSpanLocator, MessageWindowLocator)):
            raise RetrievalContractError("passage locator must use a closed typed locator")
        bounded_text(self.passage_index_version, label="passage index version", maximum_bytes=120)
        if not isinstance(self.embedding, EmbeddingIdentity):
            raise RetrievalContractError("passage requires an embedding compatibility value")
        representation = self.source_revision.representation
        if isinstance(self.locator, MessageWindowLocator):
            if (
                self.source_ref.source_kind is not SourceKind.CONVERSATION
                or self.source_revision.kind is not RevisionKind.MESSAGE_LEDGER_SHA256
                or representation.kind is not RepresentationKind.CONVERSATION
                or representation.object_id != self.source_ref.canonical_object_id
            ):
                raise RetrievalContractError("message windows must anchor to their conversation revision")
        else:
            if self.source_ref.source_kind is SourceKind.CONVERSATION:
                raise RetrievalContractError("conversation passages require a message-window locator")
            if representation.kind is RepresentationKind.KNOWLEDGE_OBJECT:
                if self.source_ref.canonical_object_kind is not CanonicalObjectKind.RAW_OBJECT:
                    raise RetrievalContractError("KO passages require a Raw-root source")
            elif (
                representation.kind.value != self.source_ref.canonical_object_kind.value
                or representation.object_id != self.source_ref.canonical_object_id
            ):
                raise RetrievalContractError("passage revision must address its stable source")
        if self.embedding.compatibility in _INDEXED_EMBEDDING_STATES:
            if self.source_revision.kind is not RevisionKind.KNOWLEDGE_VERSION:
                raise RetrievalContractError("schema-38 indexed passages require an exact KO revision")
            if self.embedding.source_version != int(self.source_revision.value):
                raise RetrievalContractError("embedding source_version must equal the KO revision")
            if self.embedding.chunk_scheme != self.passage_index_version:
                raise RetrievalContractError("passage index version must equal the stored chunk_scheme")

    def __repr__(self) -> str:
        return f"PassageRef(source_kind={self.source_ref.source_kind.value!r}, private_locator=True)"

    @classmethod
    def from_resolved_source(
        cls,
        resolved_source: ResolvedSource,
        *,
        source_revision: SourceRevision,
        locator: PassageLocator,
        passage_index_version: str,
        embedding: EmbeddingIdentity,
    ) -> PassageRef:
        if source_revision not in resolved_source.revisions:
            raise RetrievalContractError("passage revision is absent from the resolved snapshot")
        return cls(
            source_ref=resolved_source.source_ref,
            source_revision=source_revision,
            locator=locator,
            passage_index_version=passage_index_version,
            embedding=embedding,
        )

    def revision_matches(self, resolved_source: ResolvedSource) -> bool:
        """Check only identity/revision membership, never actor authority or lifecycle."""

        return (
            self.source_ref == resolved_source.source_ref
            and self.source_revision in resolved_source.revisions
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "locator": self.locator.to_private_payload(),
            "passage_index_version": self.passage_index_version,
            "source_ref": self.source_ref.to_private_payload(),
            "source_revision": self.source_revision.to_private_payload(),
        }

    def to_private_payload(self) -> dict[str, object]:
        return {
            "embedding": self.embedding.to_private_payload(),
            **self._identity_payload(),
            "schema": PASSAGE_REF_SCHEMA,
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> PassageRef:
        payload = exact_object(
            value,
            frozenset(
                {
                    "embedding",
                    "locator",
                    "passage_index_version",
                    "schema",
                    "source_ref",
                    "source_revision",
                }
            ),
            label="passage ref",
        )
        if payload["schema"] != PASSAGE_REF_SCHEMA:
            raise RetrievalContractError("passage ref schema is unsupported")
        locator_payload = exact_object(
            payload["locator"],
            frozenset(payload["locator"].keys()) if type(payload["locator"]) is dict else frozenset(),
            label="passage locator",
        )
        kind = enum_value(PassageLocatorKind, locator_payload.get("kind"), label="passage locator kind")
        locator: PassageLocator
        if kind is PassageLocatorKind.TEXT_SPAN:
            locator = TextSpanLocator.from_private_payload(locator_payload)
        else:
            locator = MessageWindowLocator.from_private_payload(locator_payload)
        index_version = payload["passage_index_version"]
        if not isinstance(index_version, str):
            raise RetrievalContractError("passage index version must be private text")
        return cls(
            source_ref=SourceRef.from_private_payload(payload["source_ref"]),
            source_revision=SourceRevision.from_private_payload(payload["source_revision"]),
            locator=locator,
            passage_index_version=index_version,
            embedding=EmbeddingIdentity.from_private_payload(payload["embedding"]),
        )

    @classmethod
    def parse_private(cls, value: str) -> PassageRef:
        result = cls.from_private_payload(parse_canonical_object(value, label="passage ref"))
        if value != result.to_private_json():
            raise RetrievalContractError("passage ref JSON is not semantically canonical")
        return result

    def passage_digest(self, privacy_key: bytes) -> str:
        """Opaque identity excludes mutable embedding compatibility."""

        return keyed_digest(b"friday/passage-ref/v1", self._identity_payload(), privacy_key)


__all__ = [
    "EmbeddingCompatibility",
    "EmbeddingIdentity",
    "MessageRole",
    "MessageWindowLocator",
    "PassageLocatorKind",
    "PassageRef",
    "TextSpanLocator",
]
