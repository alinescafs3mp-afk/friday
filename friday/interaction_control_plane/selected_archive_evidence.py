"""Body-free schema-39 identity retained for selected archive evidence.

This module is storage-independent.  It deliberately carries only stable
retrieval identities, exact revisions, structural coverage digests and the
accepted-turn boundary needed to revalidate a message ledger.  It grants no
authority and contains no excerpt, title, filename, query or model prose.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.retrieval._contract_utils import RetrievalContractError
from friday.retrieval.identity_contract import (
    AuthorityScope,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
)
from friday.retrieval.passage_contract import MessageWindowLocator, PassageRef, TextSpanLocator

SELECTED_ARCHIVE_EVIDENCE_SCHEMA = "friday.work-item-selected-archive-evidence.v1"
WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES = 4_096
WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES = 65_536
WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT = 8

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_DOCUMENT_PASSAGE_INDEX_VERSION = "archive-storage-char-v1"
_MESSAGE_PASSAGE_INDEX_VERSION = "archive-message-window-v1"


class SelectedArchiveEvidenceError(ValueError):
    """A selected-evidence row is outside the closed schema-39 contract."""


class SelectedArchiveCorpus(StrEnum):
    DOCUMENTS = "documents"
    KNOWLEDGE = "knowledge"
    MESSAGES = "messages"


class SelectedArchiveCoverageGrade(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise SelectedArchiveEvidenceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SelectedArchiveEvidenceError(f"{label} is not a canonical identifier")
    return value


def _canonical_passage_refs(values: Iterable[PassageRef]) -> tuple[PassageRef, ...]:
    passages = tuple(values)
    if not 1 <= len(passages) <= WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT or any(
        type(item) is not PassageRef for item in passages
    ):
        raise SelectedArchiveEvidenceError("passage refs must contain one to eight typed values")
    encoded = tuple(item.to_private_json() for item in passages)
    if len(encoded) != len(set(encoded)) or encoded != tuple(sorted(encoded)):
        raise SelectedArchiveEvidenceError("passage refs must be unique and canonically ordered")
    return passages


def canonical_passage_refs_json(values: Iterable[PassageRef]) -> str:
    """Serialize one bounded, canonical array of body-free passage identities."""

    passages = _canonical_passage_refs(values)
    encoded = json.dumps(
        [item.to_private_payload() for item in passages],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("ascii")) > WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES:
        raise SelectedArchiveEvidenceError("passage refs JSON exceeds its closed byte limit")
    return encoded


def parse_canonical_passage_refs(value: object) -> tuple[PassageRef, ...]:
    """Parse only the exact canonical array representation used by the sidecar."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise SelectedArchiveEvidenceError("passage refs must be canonical JSON text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SelectedArchiveEvidenceError("passage refs JSON must be valid UTF-8") from exc
    if len(encoded) > WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES:
        raise SelectedArchiveEvidenceError("passage refs JSON exceeds its closed byte limit")
    try:
        decoded = json.loads(
            value,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SelectedArchiveEvidenceError("passage refs JSON contains a non-finite number")
            ),
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise SelectedArchiveEvidenceError("passage refs must contain one JSON array") from exc
    if type(decoded) is not list or not 1 <= len(decoded) <= WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT:
        raise SelectedArchiveEvidenceError("passage refs must contain one JSON array")
    try:
        passages = _canonical_passage_refs(PassageRef.from_private_payload(item) for item in decoded)
    except RetrievalContractError as exc:
        raise SelectedArchiveEvidenceError("passage refs contain an invalid typed identity") from exc
    if value != canonical_passage_refs_json(passages):
        raise SelectedArchiveEvidenceError("passage refs JSON is not semantically canonical")
    return passages


@dataclass(frozen=True, slots=True, repr=False)
class SelectedArchiveEvidence:
    """The exact private projection of one ``work_item_selected_evidence`` row."""

    work_item_id: str
    corpus: SelectedArchiveCorpus
    source_ref: SourceRef
    passage_refs: tuple[PassageRef, ...]
    source_snapshot_sha256: str
    coverage_sha256: str
    coverage_grade: SelectedArchiveCoverageGrade
    origin_boundary_user_message_id: str

    def __post_init__(self) -> None:
        _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(
            self.origin_boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="origin_boundary_user_message_id",
        )
        if not isinstance(self.corpus, SelectedArchiveCorpus):
            raise SelectedArchiveEvidenceError("corpus must use the closed archive enum")
        if type(self.source_ref) is not SourceRef:
            raise SelectedArchiveEvidenceError("source_ref must use the exact typed contract")
        passages = _canonical_passage_refs(self.passage_refs)
        if passages != self.passage_refs or any(item.source_ref != self.source_ref for item in passages):
            raise SelectedArchiveEvidenceError("every passage must belong to the one selected source")
        expected_source_kinds = {
            SelectedArchiveCorpus.DOCUMENTS: frozenset({SourceKind.DOCUMENT}),
            SelectedArchiveCorpus.KNOWLEDGE: frozenset(
                {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
            ),
            SelectedArchiveCorpus.MESSAGES: frozenset({SourceKind.CONVERSATION}),
        }[self.corpus]
        expected_representation = {
            SelectedArchiveCorpus.DOCUMENTS: RepresentationKind.RAW_OBJECT,
            SelectedArchiveCorpus.KNOWLEDGE: RepresentationKind.KNOWLEDGE_OBJECT,
            SelectedArchiveCorpus.MESSAGES: RepresentationKind.CONVERSATION,
        }[self.corpus]
        expected_revision = {
            SelectedArchiveCorpus.DOCUMENTS: RevisionKind.RAW_CONTENT_SHA256,
            SelectedArchiveCorpus.KNOWLEDGE: RevisionKind.KNOWLEDGE_VERSION,
            SelectedArchiveCorpus.MESSAGES: RevisionKind.MESSAGE_LEDGER_SHA256,
        }[self.corpus]
        expected_authority = (
            AuthorityScope.PRINCIPAL
            if self.corpus is SelectedArchiveCorpus.MESSAGES
            else AuthorityScope.TENANT_PRINCIPAL
        )
        expected_locator = (
            MessageWindowLocator if self.corpus is SelectedArchiveCorpus.MESSAGES else TextSpanLocator
        )
        expected_passage_index = (
            _MESSAGE_PASSAGE_INDEX_VERSION
            if self.corpus is SelectedArchiveCorpus.MESSAGES
            else _DOCUMENT_PASSAGE_INDEX_VERSION
        )
        if (
            self.source_ref.source_kind not in expected_source_kinds
            or self.source_ref.authority_scope is not expected_authority
            or any(
                item.source_revision.representation.kind is not expected_representation
                or item.source_revision.kind is not expected_revision
                or type(item.locator) is not expected_locator
                or item.passage_index_version != expected_passage_index
                for item in passages
            )
        ):
            raise SelectedArchiveEvidenceError("corpus, source and passage replay matrix disagree")
        if len({item.source_revision for item in passages}) != 1:
            raise SelectedArchiveEvidenceError("passage refs must share one exact source revision")
        locator_identities = tuple(
            json.dumps(
                item.locator.to_private_payload(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for item in passages
        )
        if len(locator_identities) != len(set(locator_identities)):
            raise SelectedArchiveEvidenceError("passage locators must be unique")
        source_json = self.source_ref.to_private_json()
        if len(source_json.encode("ascii")) > WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES:
            raise SelectedArchiveEvidenceError("source ref JSON exceeds its closed byte limit")
        canonical_passage_refs_json(passages)
        _digest(self.source_snapshot_sha256, label="source_snapshot_sha256")
        _digest(self.coverage_sha256, label="coverage_sha256")
        if not isinstance(self.coverage_grade, SelectedArchiveCoverageGrade):
            raise SelectedArchiveEvidenceError("coverage_grade must use the closed coverage enum")

    def __repr__(self) -> str:
        return (
            "SelectedArchiveEvidence(private_source=True, "
            f"corpus={self.corpus.value!r}, passage_count={len(self.passage_refs)})"
        )

    @classmethod
    def from_storage_row(cls, value: Mapping[str, object]) -> SelectedArchiveEvidence:
        expected = frozenset(
            {
                "work_item_id",
                "corpus",
                "source_ref_json",
                "passage_refs_json",
                "source_snapshot_sha256",
                "coverage_sha256",
                "coverage_grade",
                "origin_boundary_user_message_id",
            }
        )
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise SelectedArchiveEvidenceError("selected evidence storage row must be an object")
        if frozenset(value) != expected:
            raise SelectedArchiveEvidenceError("selected evidence storage keys do not match")
        raw_corpus = value["corpus"]
        raw_grade = value["coverage_grade"]
        if not isinstance(raw_corpus, str) or not isinstance(raw_grade, str):
            raise SelectedArchiveEvidenceError("selected evidence storage row is invalid")
        try:
            source = SourceRef.parse_private(value["source_ref_json"])  # type: ignore[arg-type]
            passages = parse_canonical_passage_refs(value["passage_refs_json"])
            corpus = SelectedArchiveCorpus(raw_corpus)
            grade = SelectedArchiveCoverageGrade(raw_grade)
        except (RetrievalContractError, TypeError, ValueError) as exc:
            raise SelectedArchiveEvidenceError("selected evidence storage row is invalid") from exc
        return cls(
            work_item_id=_identifier(value["work_item_id"], _WORK_ITEM_ID_RE, label="work_item_id"),
            corpus=corpus,
            source_ref=source,
            passage_refs=passages,
            source_snapshot_sha256=_digest(
                value["source_snapshot_sha256"],
                label="source_snapshot_sha256",
            ),
            coverage_sha256=_digest(value["coverage_sha256"], label="coverage_sha256"),
            coverage_grade=grade,
            origin_boundary_user_message_id=_identifier(
                value["origin_boundary_user_message_id"],
                _MESSAGE_ID_RE,
                label="origin_boundary_user_message_id",
            ),
        )

    def to_storage_payload(self) -> dict[str, str]:
        return {
            "work_item_id": self.work_item_id,
            "corpus": self.corpus.value,
            "source_ref_json": self.source_ref.to_private_json(),
            "passage_refs_json": canonical_passage_refs_json(self.passage_refs),
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "coverage_sha256": self.coverage_sha256,
            "coverage_grade": self.coverage_grade.value,
            "origin_boundary_user_message_id": self.origin_boundary_user_message_id,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": SELECTED_ARCHIVE_EVIDENCE_SCHEMA,
            "work_item_id": self.work_item_id,
            "corpus": self.corpus.value,
            "source_ref": self.source_ref.to_private_payload(),
            "passage_refs": [item.to_private_payload() for item in self.passage_refs],
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "coverage_sha256": self.coverage_sha256,
            "coverage_grade": self.coverage_grade.value,
            "origin_boundary_user_message_id": self.origin_boundary_user_message_id,
        }


__all__ = [
    "SELECTED_ARCHIVE_EVIDENCE_SCHEMA",
    "WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT",
    "WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES",
    "WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES",
    "SelectedArchiveCorpus",
    "SelectedArchiveCoverageGrade",
    "SelectedArchiveEvidence",
    "SelectedArchiveEvidenceError",
    "canonical_passage_refs_json",
    "parse_canonical_passage_refs",
]
