"""Code-owned synthetic corpus and qrels for the ephemeral benchmark."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from friday.retrieval.archive_search_contract import (
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    MessageRole,
    MessageWindowLocator,
    PassageLocatorKind,
    PassageRef,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
    TextSpanLocator,
)
from friday.retrieval_benchmark.contracts import (
    RecallAlternativeV1,
    RecallCaseV1,
    RecallEvidenceSourceV1,
    RecallTaxonomyV1,
    opaque_passage_window_identity,
    opaque_source_identity,
)
from friday.storage.models import InboxItem, InboxStatus, RawObject

SYNTHETIC_TENANT: Final = "recall-benchmark-tenant"
SYNTHETIC_PRINCIPAL: Final = "recall-benchmark-principal"
BOUNDARY_CONVERSATION_ID: Final = "conv_f000000000000001"
BOUNDARY_MESSAGE_ID: Final = "msg_f000000000000001"


class SyntheticStorage(Protocol):
    def ensure_user(self, user_id: str) -> object: ...

    def store_raw_object(self, obj: RawObject) -> RawObject: ...

    def store_inbox_item(self, item: InboxItem) -> InboxItem: ...

    def transaction(self): ...  # type: ignore[no-untyped-def]


@dataclass(frozen=True, slots=True)
class SyntheticDocumentSpec:
    ordinal: int
    content: str
    filename: str
    received_at: str
    status: InboxStatus
    uploaded_at: str | None = None

    @property
    def raw_id(self) -> str:
        return f"raw_{self.ordinal:016x}"

    @property
    def inbox_id(self) -> str:
        return f"inbox_{self.ordinal:016x}"

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(
            SourceKind.DOCUMENT,
            AuthorityScope.TENANT_PRINCIPAL,
            SYNTHETIC_TENANT,
            SYNTHETIC_PRINCIPAL,
            CanonicalObjectKind.RAW_OBJECT,
            self.raw_id,
        )


@dataclass(frozen=True, slots=True)
class SyntheticMessageSpec:
    ordinal: int
    content: str
    created_at: str

    @property
    def conversation_id(self) -> str:
        return f"conv_{self.ordinal:016x}"

    @property
    def message_id(self) -> str:
        return f"msg_{self.ordinal:016x}"

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(
            SourceKind.CONVERSATION,
            AuthorityScope.PRINCIPAL,
            None,
            SYNTHETIC_PRINCIPAL,
            CanonicalObjectKind.CONVERSATION,
            self.conversation_id,
        )


_DOCUMENTS: Final = (
    SyntheticDocumentSpec(
        1,
        "Orchid nebula budget reconciliation uses a violet reserve ledger.",
        "nebula-budget.md",
        "2026-01-12T09:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        2,
        "Amber glacier capacity forecast tracks the winter reserve margin.",
        "glacier-capacity.txt",
        "2026-01-13T09:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        3,
        "Cobalt summit calendar records the exact migration rehearsal.",
        "summit-calendar.md",
        "2024-05-14T11:30:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        4,
        "Saffron harbor schedule records the exact recovery drill.",
        "harbor-schedule.md",
        "2024-06-18T15:45:00+00:00",
        InboxStatus.CLASSIFIED,
        "2024-06-17T15:45:00+00:00",
    ),
    SyntheticDocumentSpec(
        5,
        "Indigo archive compass preserves the retired launch checklist.",
        "archive-compass-2017.md",
        "2017-02-03T08:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        6,
        "Copper legacy atlas preserves the original incident playbook.",
        "legacy-atlas-2018.md",
        "2018-09-21T08:00:00+00:00",
        InboxStatus.CLASSIFIED,
        "2018-09-20T08:00:00+00:00",
    ),
    SyntheticDocumentSpec(
        7,
        "Pending quartz memorandum describes the unreviewed vendor decision.",
        "pending-quartz.md",
        "2026-02-01T10:00:00+00:00",
        InboxStatus.PENDING,
    ),
    SyntheticDocumentSpec(
        8,
        "Pending topaz memorandum describes the unreviewed security decision.",
        "pending-topaz.md",
        "2026-02-02T10:00:00+00:00",
        InboxStatus.PENDING,
    ),
    SyntheticDocumentSpec(
        9,
        "Linden procurement matrix contains the approved adapter quantities.",
        "scan_0009.bin",
        "2026-02-10T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        10,
        "Juniper compliance matrix contains the approved retention controls.",
        "x10.dat",
        "2026-02-11T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        11,
        "Qwêrtz cobalt layout\nkeeps the keyboard migration marker isolated.",
        "layout-cobalt.txt",
        "2026-03-01T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        12,
        "Azêrty bronze layout\nkeeps the keyboard recovery marker isolated.",
        "layout-bronze.txt",
        "2026-03-02T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        13,
        "Mira studies the helios routing topic for the eastern deployment.",
        "mira-helios.md",
        "2026-03-10T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        14,
        "Nolan studies the selene indexing topic for the western deployment.",
        "nolan-selene.md",
        "2026-03-11T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        15,
        "Aurora billing topic closes the monthly reconciliation packet.",
        "aurora-billing-march.md",
        "2025-03-17T12:00:00+00:00",
        InboxStatus.CLASSIFIED,
    ),
    SyntheticDocumentSpec(
        16,
        "Borealis staffing topic closes the monthly allocation packet.",
        "borealis-staffing-april.md",
        "2025-04-19T12:00:00+00:00",
        InboxStatus.CLASSIFIED,
        "2025-04-18T12:00:00+00:00",
    ),
)

_MESSAGES: Final = (
    SyntheticMessageSpec(
        101,
        "The lantern deployment was postponed until the checksum review finishes.",
        "2026-04-01T10:00:00+00:00",
    ),
    SyntheticMessageSpec(
        102,
        "The meadow migration can start after the access audit is complete.",
        "2026-04-02T10:00:00+00:00",
    ),
)

_CONTINUATION_DECOYS: Final = tuple(
    SyntheticDocumentSpec(
        1_000 + index,
        f"Nebula budget decoy record {index:02d} contains no relevant reconciliation evidence.",
        f"decoy-{index:02d}.txt",
        f"2026-06-{index:02d}T10:00:00+00:00",
        InboxStatus.CLASSIFIED,
    )
    for index in range(1, 26)
)


def _case_privacy_key(index: int) -> str:
    return hashlib.sha256(
        f"friday/retrieval-recall-synthetic-case-key/v1/{index:04d}".encode("ascii")
    ).hexdigest()


def _document_passage_ref(spec: SyntheticDocumentSpec) -> PassageRef:
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, spec.raw_id)
    revision = SourceRevision(
        representation,
        RevisionKind.RAW_CONTENT_SHA256,
        hashlib.sha256(spec.content.encode("utf-8")).hexdigest(),
    )
    end = spec.content.find("\n")
    if end < 0:
        end = len(spec.content)
    return PassageRef(
        source_ref=spec.source_ref,
        source_revision=revision,
        locator=TextSpanLocator(chunk_index=0, start_char=0, end_char=end),
        passage_index_version="archive-storage-char-v1",
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )


def _message_passage_ref(spec: SyntheticMessageSpec) -> PassageRef:
    row_identity = hashlib.sha256(
        json.dumps(
            {
                "content": spec.content,
                "conversation_id": spec.conversation_id,
                "created_at": spec.created_at,
                "id": spec.message_id,
                "person_id": SYNTHETIC_PRINCIPAL,
                "role": MessageRole.USER.value,
                "schema": "friday.private-message-window-row.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    ledger_sha256 = hashlib.sha256(
        (
            '{"row_identity_sha256s":["'
            + row_identity
            + '"],"schema":"friday.private-message-window-row-ledger.v1"}'
        ).encode("ascii")
    ).hexdigest()
    representation = SourceRepresentation(RepresentationKind.CONVERSATION, spec.conversation_id)
    revision = SourceRevision(
        representation,
        RevisionKind.MESSAGE_LEDGER_SHA256,
        ledger_sha256,
    )
    start = datetime.fromisoformat(spec.created_at)
    return PassageRef(
        source_ref=spec.source_ref,
        source_revision=revision,
        locator=MessageWindowLocator.create(
            first_message_id=spec.message_id,
            last_message_id=spec.message_id,
            start_at=start,
            end_at=start + timedelta(microseconds=1),
            context_before=0,
            context_after=0,
            matched_role=MessageRole.USER,
        ),
        passage_index_version="archive-message-window-v1",
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )


def _instant_constraint(
    role: TemporalRole,
    start: str,
    end: str,
    *,
    corpus: ArchiveSearchCorpus = ArchiveSearchCorpus.DOCUMENTS,
) -> ArchiveTemporalConstraint:
    return ArchiveTemporalConstraint(
        corpus=corpus,
        role=role,
        value_kind=TemporalValueKind.INSTANT,
        precision=TemporalPrecision.INSTANT,
        start=start,
        end=end,
    )


def _document_case(
    index: int,
    taxonomy: RecallTaxonomyV1,
    query: str,
    *,
    temporal: ArchiveTemporalConstraint | None = None,
) -> RecallCaseV1:
    spec = _DOCUMENTS[index - 1]
    privacy_key_hex = _case_privacy_key(index)
    privacy_key = bytes.fromhex(privacy_key_hex)
    source_identity = opaque_source_identity(spec.source_ref, privacy_key)
    passage_identity = opaque_passage_window_identity(
        _document_passage_ref(spec),
        privacy_key,
    )
    request = ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(() if temporal is None else (temporal,)),
        limit=20,
    )
    return RecallCaseV1(
        case_id=f"case.{index:04d}",
        privacy_key_hex=privacy_key_hex,
        taxonomy=taxonomy,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
        request=request,
        expected_corpus=ArchiveSearchCorpus.DOCUMENTS,
        alternatives=(
            RecallAlternativeV1(
                source_identity=source_identity,
                passage_window_identities=(passage_identity,),
                locator_kind=PassageLocatorKind.TEXT_SPAN,
                relevance_grade=3,
                temporal_role=temporal.role if temporal is not None else None,
            ),
        ),
        expected_no_hit=False,
    )


def _message_case(index: int, query: str) -> RecallCaseV1:
    spec = _MESSAGES[index - 17]
    privacy_key_hex = _case_privacy_key(index)
    privacy_key = bytes.fromhex(privacy_key_hex)
    source_identity = opaque_source_identity(spec.source_ref, privacy_key)
    passage_identity = opaque_passage_window_identity(
        _message_passage_ref(spec),
        privacy_key,
    )
    return RecallCaseV1(
        case_id=f"case.{index:04d}",
        privacy_key_hex=privacy_key_hex,
        taxonomy=RecallTaxonomyV1.MESSAGE_PARAPHRASE,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
        request=ArchiveSearchRequest.create(
            query=query,
            corpora=(ArchiveSearchCorpus.MESSAGES,),
            limit=20,
        ),
        expected_corpus=ArchiveSearchCorpus.MESSAGES,
        alternatives=(
            RecallAlternativeV1(
                source_identity=source_identity,
                passage_window_identities=(passage_identity,),
                locator_kind=PassageLocatorKind.MESSAGE_WINDOW,
                relevance_grade=3,
                temporal_role=TemporalRole.CONVERSATION_TIME,
            ),
        ),
        expected_no_hit=False,
    )


def _unknown_case(
    index: int,
    query: str,
    target: SyntheticDocumentSpec | SyntheticMessageSpec,
) -> RecallCaseV1:
    privacy_key_hex = _case_privacy_key(index)
    privacy_key = bytes.fromhex(privacy_key_hex)
    if isinstance(target, SyntheticDocumentSpec):
        expected_corpus = ArchiveSearchCorpus.DOCUMENTS
        passage_ref = _document_passage_ref(target)
        locator_kind = PassageLocatorKind.TEXT_SPAN
        temporal_role = None
    else:
        expected_corpus = ArchiveSearchCorpus.MESSAGES
        passage_ref = _message_passage_ref(target)
        locator_kind = PassageLocatorKind.MESSAGE_WINDOW
        temporal_role = TemporalRole.CONVERSATION_TIME
    return RecallCaseV1(
        case_id=f"case.{index:04d}",
        privacy_key_hex=privacy_key_hex,
        taxonomy=RecallTaxonomyV1.UNKNOWN_CORPUS,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
        request=ArchiveSearchRequest.create(
            query=query,
            corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
            limit=20,
        ),
        expected_corpus=expected_corpus,
        alternatives=(
            RecallAlternativeV1(
                source_identity=opaque_source_identity(target.source_ref, privacy_key),
                passage_window_identities=(opaque_passage_window_identity(passage_ref, privacy_key),),
                locator_kind=locator_kind,
                relevance_grade=3,
                temporal_role=temporal_role,
            ),
        ),
        expected_no_hit=False,
    )


def _unknown_no_hit_case(index: int, query: str) -> RecallCaseV1:
    return RecallCaseV1(
        case_id=f"case.{index:04d}",
        privacy_key_hex=_case_privacy_key(index),
        taxonomy=RecallTaxonomyV1.UNKNOWN_CORPUS,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
        request=ArchiveSearchRequest.create(
            query=query,
            corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
            limit=20,
        ),
        expected_corpus=ArchiveSearchCorpus.DOCUMENTS,
        alternatives=(),
        expected_no_hit=True,
    )


def synthetic_cases() -> tuple[RecallCaseV1, ...]:
    """Return the immutable code-owned manifest covering all ten classes."""

    cases = (
        _document_case(1, RecallTaxonomyV1.APPROXIMATE_CONTENT, "nebula budget"),
        _document_case(2, RecallTaxonomyV1.APPROXIMATE_CONTENT, "winter reserve capacity"),
        _document_case(
            3,
            RecallTaxonomyV1.APPROXIMATE_DATE,
            "summit calendar",
            temporal=_instant_constraint(
                TemporalRole.RECEIVED_AT,
                "2024-05-01T00:00:00+00:00",
                "2024-06-01T00:00:00+00:00",
            ),
        ),
        _document_case(
            4,
            RecallTaxonomyV1.APPROXIMATE_DATE,
            "harbor schedule",
            temporal=_instant_constraint(
                TemporalRole.UPLOADED_AT,
                "2024-06-17T00:00:00+00:00",
                "2024-06-18T00:00:00+00:00",
            ),
        ),
        _document_case(
            5,
            RecallTaxonomyV1.OLD_FILE,
            "archive compass",
            temporal=_instant_constraint(
                TemporalRole.RECEIVED_AT,
                "2017-01-01T00:00:00+00:00",
                "2018-01-01T00:00:00+00:00",
            ),
        ),
        _document_case(
            6,
            RecallTaxonomyV1.OLD_FILE,
            "legacy atlas",
            temporal=_instant_constraint(
                TemporalRole.UPLOADED_AT,
                "2018-09-20T00:00:00+00:00",
                "2018-09-21T00:00:00+00:00",
            ),
        ),
        _document_case(7, RecallTaxonomyV1.PENDING_FILE, "pending quartz"),
        _document_case(8, RecallTaxonomyV1.PENDING_FILE, "pending topaz"),
        _document_case(9, RecallTaxonomyV1.UNHELPFUL_FILENAME, "procurement matrix"),
        _document_case(10, RecallTaxonomyV1.UNHELPFUL_FILENAME, "compliance matrix"),
        _document_case(11, RecallTaxonomyV1.TYPO_LAYOUT, "qwertz cobalt"),
        _document_case(12, RecallTaxonomyV1.TYPO_LAYOUT, "azerty bronze"),
        _document_case(13, RecallTaxonomyV1.PERSON_TOPIC, "Mira helios"),
        _document_case(14, RecallTaxonomyV1.PERSON_TOPIC, "Nolan selene"),
        _document_case(
            15,
            RecallTaxonomyV1.TOPIC_MONTH,
            "aurora billing",
            temporal=_instant_constraint(
                TemporalRole.RECEIVED_AT,
                "2025-03-01T00:00:00+00:00",
                "2025-04-01T00:00:00+00:00",
            ),
        ),
        _document_case(
            16,
            RecallTaxonomyV1.TOPIC_MONTH,
            "borealis staffing",
            temporal=_instant_constraint(
                TemporalRole.UPLOADED_AT,
                "2025-04-18T00:00:00+00:00",
                "2025-04-19T00:00:00+00:00",
            ),
        ),
        _message_case(17, "why was lantern rollout delayed"),
        _message_case(18, "when can meadow transfer begin"),
        _unknown_case(19, "violet reserve ledger", _DOCUMENTS[0]),
        _unknown_case(20, "lantern deployment checksum review", _MESSAGES[0]),
        _unknown_no_hit_case(21, "iridescent zephyr warranty"),
    )
    return tuple(sorted(cases, key=lambda item: item.case_id))


def seed_synthetic_storage(storage: SyntheticStorage) -> None:
    """Seed only a caller-owned ephemeral Friday storage instance."""

    storage.ensure_user(SYNTHETIC_TENANT)
    storage.ensure_user(SYNTHETIC_PRINCIPAL)
    for document in (*_DOCUMENTS, *_CONTINUATION_DECOYS):
        content_hash = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        storage.store_raw_object(
            RawObject(
                id=document.raw_id,
                user_id=SYNTHETIC_TENANT,
                source="upload",
                source_ref=f"synthetic:{document.ordinal:04d}",
                raw_content=document.content,
                content_type="file",
                metadata_json={
                    "filename": document.filename,
                    "media_kind": "document",
                    "mime_type": "text/plain",
                    "uploaded_by": SYNTHETIC_PRINCIPAL,
                    **({"uploaded_at": document.uploaded_at} if document.uploaded_at is not None else {}),
                },
                content_hash=content_hash,
                received_at=document.received_at,
                created_at=document.received_at,
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=document.inbox_id,
                user_id=SYNTHETIC_TENANT,
                raw_object_id=document.raw_id,
                status=document.status,
                created_at=document.received_at,
                reviewed_at=(document.received_at if document.status is InboxStatus.CLASSIFIED else None),
                reviewed_by=(SYNTHETIC_PRINCIPAL if document.status is InboxStatus.CLASSIFIED else None),
            )
        )

    with storage.transaction() as conn:
        for message in _MESSAGES:
            conn.execute(
                """INSERT INTO conversations(
                       id,user_id,title,last_message,unread_count,is_pinned,is_archived,
                       mode,created_at,updated_at
                   ) VALUES(?,?,?,'',0,0,0,'dialogue',?,?)""",
                (
                    message.conversation_id,
                    SYNTHETIC_PRINCIPAL,
                    f"Synthetic conversation {message.ordinal}",
                    message.created_at,
                    message.created_at,
                ),
            )
            conn.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                   ) VALUES(?,?,?,'user',?,'{}',NULL,?)""",
                (
                    message.message_id,
                    message.conversation_id,
                    SYNTHETIC_PRINCIPAL,
                    message.content,
                    message.created_at,
                ),
            )
        boundary_at = "2026-04-03T10:00:00+00:00"
        conn.execute(
            """INSERT INTO conversations(
                   id,user_id,title,last_message,unread_count,is_pinned,is_archived,
                   mode,created_at,updated_at
               ) VALUES(?,?,?,'',0,0,0,'dialogue',?,?)""",
            (
                BOUNDARY_CONVERSATION_ID,
                SYNTHETIC_PRINCIPAL,
                "Synthetic accepted turn",
                boundary_at,
                boundary_at,
            ),
        )
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(?,?,?,'user','synthetic current archive request','{}',NULL,?)""",
            (
                BOUNDARY_MESSAGE_ID,
                BOUNDARY_CONVERSATION_ID,
                SYNTHETIC_PRINCIPAL,
                boundary_at,
            ),
        )


__all__ = [
    "BOUNDARY_CONVERSATION_ID",
    "BOUNDARY_MESSAGE_ID",
    "SYNTHETIC_PRINCIPAL",
    "SYNTHETIC_TENANT",
    "seed_synthetic_storage",
    "synthetic_cases",
]
