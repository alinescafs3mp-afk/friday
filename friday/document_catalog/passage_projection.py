"""Body-free passage projection for exact extracted document text.

This contract is deliberately storage- and model-independent.  It fixes the
chunking policy and exact source binding needed by a later rebuildable passage
index, but grants no lifecycle, review, tenant, or evidence authority.  The
serialized form contains only opaque identity, revisions, offsets, counts, and
digests; source text stays in the authoritative Raw Object.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from friday.retrieval import chunk_spans
from friday.retrieval._contract_utils import (
    RetrievalContractError,
    bounded_count,
    bounded_text,
    canonical_json,
    enum_value,
    exact_object,
    lowercase_sha256,
    parse_canonical_object,
)

DOCUMENT_PASSAGE_PROJECTION_SCHEMA = "friday.document-passage-projection.private.v1"

# Passage visibility must not disappear when embeddings are disabled or their
# endpoint changes.  These are therefore code-owned projection constants, not
# FridaySettings.  Any algorithm/parameter change requires a new revision and a
# resumable rebuild; tests pin the current full-coverage behaviour.
DOCUMENT_PASSAGE_INDEX_REVISION = "document-char-v1:chunk-spans-v2:1200:200:64"
DOCUMENT_PASSAGE_MAX_CHARS = 1_200
DOCUMENT_PASSAGE_OVERLAP_CHARS = 200
DOCUMENT_PASSAGE_MAX_COUNT = 64

_MAX_RAW_OBJECT_ID_BYTES = 200
_MAX_SOURCE_CHARS = 1_000_000_000


class DocumentPassageProjectionStatus(StrEnum):
    CURRENT = "current"
    INCOMPLETE = "incomplete"


class DocumentPassageIncompleteReason(StrEnum):
    BACKFILL_PENDING = "backfill_pending"
    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_INCOMPLETE = "extraction_incomplete"
    NO_TEXT = "no_text"
    UNSUPPORTED_CONTENT = "unsupported_content"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_CHANGED = "source_changed"


def _positive_version(value: object, *, optional: bool = False) -> int | None:
    parsed = bounded_count(value, label="document passage source version", optional=optional)
    if parsed is not None and parsed < 1:
        raise RetrievalContractError("document passage source version must be positive")
    return parsed


def _exact_text_sha256(value: str) -> str:
    try:
        material = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError("document passage source text must be valid UTF-8") from exc
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class DocumentPassageSpan:
    """One body-free half-open span into an exact extracted-text revision."""

    chunk_index: int
    start_char: int
    end_char: int
    content_sha256: str

    def __post_init__(self) -> None:
        index = bounded_count(self.chunk_index, label="document passage chunk index")
        start = bounded_count(self.start_char, label="document passage start")
        end = bounded_count(self.end_char, label="document passage end")
        assert index is not None and start is not None and end is not None
        if index >= DOCUMENT_PASSAGE_MAX_COUNT:
            raise RetrievalContractError("document passage chunk index exceeds the closed cap")
        if end <= start or end > _MAX_SOURCE_CHARS:
            raise RetrievalContractError("document passage span must be a bounded non-empty range")
        lowercase_sha256(self.content_sha256, label="document passage content SHA-256")

    def __repr__(self) -> str:
        return (
            "DocumentPassageSpan("
            f"chunk_index={self.chunk_index}, start_char={self.start_char}, "
            f"end_char={self.end_char}, private_digest=True)"
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "chunk_index": self.chunk_index,
            "content_sha256": self.content_sha256,
            "end_char": self.end_char,
            "start_char": self.start_char,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> DocumentPassageSpan:
        payload = exact_object(
            value,
            frozenset({"chunk_index", "content_sha256", "end_char", "start_char"}),
            label="document passage span",
        )
        digest = payload["content_sha256"]
        if not isinstance(digest, str):
            raise RetrievalContractError("document passage content digest must be text")
        index = bounded_count(payload["chunk_index"], label="document passage chunk index")
        start = bounded_count(payload["start_char"], label="document passage start")
        end = bounded_count(payload["end_char"], label="document passage end")
        assert index is not None and start is not None and end is not None
        return cls(index, start, end, digest)


@dataclass(frozen=True, slots=True, repr=False)
class DocumentPassageProjection:
    """Exact private projection state; never an authorization decision."""

    raw_object_id: str
    source_version: int | None
    source_content_sha256: str | None
    extracted_text_sha256: str | None
    source_char_count: int | None
    passage_index_revision: str
    status: DocumentPassageProjectionStatus
    incomplete_reason: DocumentPassageIncompleteReason | None
    passages: tuple[DocumentPassageSpan, ...]

    def __post_init__(self) -> None:
        bounded_text(
            self.raw_object_id,
            label="document passage Raw Object ID",
            maximum_bytes=_MAX_RAW_OBJECT_ID_BYTES,
        )
        source_version = _positive_version(self.source_version, optional=True)
        if self.source_content_sha256 is not None:
            lowercase_sha256(
                self.source_content_sha256,
                label="document passage source content SHA-256",
            )
        if self.extracted_text_sha256 is not None:
            lowercase_sha256(
                self.extracted_text_sha256,
                label="document passage extracted text SHA-256",
            )
        if self.passage_index_revision != DOCUMENT_PASSAGE_INDEX_REVISION:
            raise RetrievalContractError("document passage index revision is unsupported")
        if not isinstance(self.status, DocumentPassageProjectionStatus):
            raise RetrievalContractError("document passage status must use the closed enum")
        if self.incomplete_reason is not None and not isinstance(
            self.incomplete_reason,
            DocumentPassageIncompleteReason,
        ):
            raise RetrievalContractError("document passage incomplete reason must use the closed enum")
        if type(self.passages) is not tuple or any(
            type(item) is not DocumentPassageSpan for item in self.passages
        ):
            raise RetrievalContractError("document passages must be an exact typed tuple")

        if self.status is DocumentPassageProjectionStatus.CURRENT:
            source_chars = bounded_count(
                self.source_char_count,
                label="document passage source character count",
            )
            if (
                source_version is None
                or self.source_content_sha256 is None
                or self.extracted_text_sha256 is None
                or source_chars is None
                or source_chars < 1
                or self.incomplete_reason is not None
                or not 1 <= len(self.passages) <= DOCUMENT_PASSAGE_MAX_COUNT
            ):
                raise RetrievalContractError("current document passage projection is incomplete")
            self._validate_current_spans(source_chars)
            return

        if (
            self.incomplete_reason is None
            or self.extracted_text_sha256 is not None
            or self.source_char_count is not None
            or self.passages
        ):
            raise RetrievalContractError("incomplete document passage projection carries evidence")
        if self.incomplete_reason is DocumentPassageIncompleteReason.SOURCE_UNAVAILABLE:
            if source_version is not None and self.source_content_sha256 is not None:
                raise RetrievalContractError("source-unavailable projection has a complete source binding")
        elif source_version is None or self.source_content_sha256 is None:
            raise RetrievalContractError("document passage incomplete state lacks source binding")

    def _validate_current_spans(self, source_chars: int) -> None:
        if self.passages[0].start_char != 0 or self.passages[-1].end_char != source_chars:
            raise RetrievalContractError("document passages do not cover the exact source boundaries")
        previous: DocumentPassageSpan | None = None
        for expected_index, passage in enumerate(self.passages):
            if passage.chunk_index != expected_index or passage.end_char > source_chars:
                raise RetrievalContractError("document passage ordering is not canonical")
            if previous is not None and (
                passage.start_char <= previous.start_char
                or passage.start_char > previous.end_char
                or passage.end_char <= previous.end_char
            ):
                raise RetrievalContractError("document passages contain a gap or do not progress")
            previous = passage

    def __repr__(self) -> str:
        return (
            "DocumentPassageProjection("
            f"status={self.status.value!r}, passage_count={len(self.passages)}, "
            "private_source=True)"
        )

    @classmethod
    def from_complete_text(
        cls,
        *,
        raw_object_id: str,
        source_version: int,
        source_content_sha256: str,
        extracted_text: str,
    ) -> DocumentPassageProjection:
        """Build the sole current v1 projection from exact persisted text."""

        if type(extracted_text) is not str or not extracted_text.strip():
            raise RetrievalContractError("current document passage source text must be non-empty")
        if len(extracted_text) > _MAX_SOURCE_CHARS:
            raise RetrievalContractError("document passage source text exceeds the closed cap")
        bounded_text(
            raw_object_id,
            label="document passage Raw Object ID",
            maximum_bytes=_MAX_RAW_OBJECT_ID_BYTES,
        )
        parsed_version = _positive_version(source_version)
        assert parsed_version is not None
        lowercase_sha256(
            source_content_sha256,
            label="document passage source content SHA-256",
        )
        spans = chunk_spans(
            extracted_text,
            max_chars=DOCUMENT_PASSAGE_MAX_CHARS,
            overlap_chars=DOCUMENT_PASSAGE_OVERLAP_CHARS,
            max_chunks=DOCUMENT_PASSAGE_MAX_COUNT,
        )
        if not spans:
            raise RetrievalContractError("current document passage projection has no spans")
        passages = tuple(
            DocumentPassageSpan(
                chunk_index=index,
                start_char=start,
                end_char=end,
                content_sha256=_exact_text_sha256(extracted_text[start:end]),
            )
            for index, (start, end) in enumerate(spans)
        )
        return cls(
            raw_object_id=raw_object_id,
            source_version=parsed_version,
            source_content_sha256=source_content_sha256,
            extracted_text_sha256=_exact_text_sha256(extracted_text),
            source_char_count=len(extracted_text),
            passage_index_revision=DOCUMENT_PASSAGE_INDEX_REVISION,
            status=DocumentPassageProjectionStatus.CURRENT,
            incomplete_reason=None,
            passages=passages,
        )

    @classmethod
    def incomplete(
        cls,
        *,
        raw_object_id: str,
        reason: DocumentPassageIncompleteReason,
        source_version: int | None,
        source_content_sha256: str | None,
    ) -> DocumentPassageProjection:
        """Record an explicit non-evidence state without guessing passage rows."""

        return cls(
            raw_object_id=raw_object_id,
            source_version=source_version,
            source_content_sha256=source_content_sha256,
            extracted_text_sha256=None,
            source_char_count=None,
            passage_index_revision=DOCUMENT_PASSAGE_INDEX_REVISION,
            status=DocumentPassageProjectionStatus.INCOMPLETE,
            incomplete_reason=reason,
            passages=(),
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "extracted_text_sha256": self.extracted_text_sha256,
            "incomplete_reason": (
                self.incomplete_reason.value if self.incomplete_reason is not None else None
            ),
            "passage_index_revision": self.passage_index_revision,
            "passages": [item.to_private_payload() for item in self.passages],
            "raw_object_id": self.raw_object_id,
            "schema": DOCUMENT_PASSAGE_PROJECTION_SCHEMA,
            "source_char_count": self.source_char_count,
            "source_content_sha256": self.source_content_sha256,
            "source_version": self.source_version,
            "status": self.status.value,
        }

    def to_private_json(self) -> str:
        return canonical_json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> DocumentPassageProjection:
        payload = exact_object(
            value,
            frozenset(
                {
                    "extracted_text_sha256",
                    "incomplete_reason",
                    "passage_index_revision",
                    "passages",
                    "raw_object_id",
                    "schema",
                    "source_char_count",
                    "source_content_sha256",
                    "source_version",
                    "status",
                }
            ),
            label="document passage projection",
        )
        if payload["schema"] != DOCUMENT_PASSAGE_PROJECTION_SCHEMA:
            raise RetrievalContractError("document passage projection schema is unsupported")
        raw_object_id = payload["raw_object_id"]
        source_digest = payload["source_content_sha256"]
        text_digest = payload["extracted_text_sha256"]
        revision = payload["passage_index_revision"]
        raw_passages = payload["passages"]
        if not isinstance(raw_object_id, str) or not isinstance(revision, str):
            raise RetrievalContractError("document passage identity must be private text")
        if source_digest is not None and not isinstance(source_digest, str):
            raise RetrievalContractError("document passage source digest must be text or null")
        if text_digest is not None and not isinstance(text_digest, str):
            raise RetrievalContractError("document passage text digest must be text or null")
        if type(raw_passages) is not list or len(raw_passages) > DOCUMENT_PASSAGE_MAX_COUNT:
            raise RetrievalContractError("document passage projection passages must be an array")
        reason = payload["incomplete_reason"]
        return cls(
            raw_object_id=raw_object_id,
            source_version=_positive_version(payload["source_version"], optional=True),
            source_content_sha256=source_digest,
            extracted_text_sha256=text_digest,
            source_char_count=bounded_count(
                payload["source_char_count"],
                label="document passage source character count",
                optional=True,
            ),
            passage_index_revision=revision,
            status=enum_value(
                DocumentPassageProjectionStatus,
                payload["status"],
                label="document passage status",
            ),
            incomplete_reason=(
                None
                if reason is None
                else enum_value(
                    DocumentPassageIncompleteReason,
                    reason,
                    label="document passage incomplete reason",
                )
            ),
            passages=tuple(DocumentPassageSpan.from_private_payload(item) for item in raw_passages),
        )

    @classmethod
    def parse_private(cls, value: object) -> DocumentPassageProjection:
        parsed = cls.from_private_payload(parse_canonical_object(value, label="document passage projection"))
        if parsed.to_private_json() != value:
            raise RetrievalContractError("document passage projection JSON is not canonical")
        return parsed

    def matches_exact_source_projection(
        self,
        *,
        source_version: int,
        source_content_sha256: str,
        extracted_text: str,
    ) -> bool:
        """Rebuild and compare identity only; this is not an authority check."""

        if self.status is not DocumentPassageProjectionStatus.CURRENT:
            return False
        try:
            rebuilt = type(self).from_complete_text(
                raw_object_id=self.raw_object_id,
                source_version=source_version,
                source_content_sha256=source_content_sha256,
                extracted_text=extracted_text,
            )
        except RetrievalContractError:
            return False
        return rebuilt == self


__all__ = [
    "DOCUMENT_PASSAGE_INDEX_REVISION",
    "DOCUMENT_PASSAGE_MAX_CHARS",
    "DOCUMENT_PASSAGE_MAX_COUNT",
    "DOCUMENT_PASSAGE_OVERLAP_CHARS",
    "DOCUMENT_PASSAGE_PROJECTION_SCHEMA",
    "DocumentPassageIncompleteReason",
    "DocumentPassageProjection",
    "DocumentPassageProjectionStatus",
    "DocumentPassageSpan",
]
