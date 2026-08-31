"""Private deterministic corpus for the five document-recall contours.

The public benchmark artifacts keep only opaque case/source/passage identities.
Bodies, queries, filenames, aliases and metadata values remain in this
package-private seed plan and never cross the measurement boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from friday.retrieval.archive_search_contract import (
    ArchiveMatchChannel,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
)
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
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
from friday.retrieval_benchmark.synthetic import (
    BOUNDARY_CONVERSATION_ID,
    BOUNDARY_MESSAGE_ID,
    SYNTHETIC_PRINCIPAL,
    SYNTHETIC_TENANT,
)
from friday.storage.models import InboxItem, InboxStatus, RawObject

_FOREIGN_PRINCIPAL: Final = "document-recall-foreign-principal"
_FOREIGN_TENANT: Final = "document-recall-foreign-tenant"
_VISIBLE_TRUNCATION_MARKER: Final = "Visible cobalt truncation boundary 7441."
_HIDDEN_TRUNCATION_MARKER: Final = "ultraviolettail9853"


class DocumentRecallClassV1(StrEnum):
    FILENAME = "filename"
    ALIAS = "alias"
    FORMAT = "format"
    DATE = "date"
    TRUNCATION = "truncation"


class _DocumentSyntheticStorage(Protocol):
    def ensure_user(self, user_id: str) -> object: ...

    def store_raw_object(self, obj: RawObject) -> RawObject: ...

    def store_inbox_item(self, item: InboxItem) -> InboxItem: ...

    def transaction(self): ...  # type: ignore[no-untyped-def]


@dataclass(frozen=True, slots=True, repr=False)
class _DocumentSpec:
    ordinal: int
    body: str
    filename: str
    mime_type: str
    received_at: str
    tenant_id: str = SYNTHETIC_TENANT
    principal_id: str = SYNTHETIC_PRINCIPAL
    alias: str | None = None
    document_date: str | None = None
    text_truncated: bool = False

    @property
    def raw_id(self) -> str:
        return f"raw_{0xE000000000000000 + self.ordinal:016x}"

    @property
    def inbox_id(self) -> str:
        return f"inbox_{0xE100000000000000 + self.ordinal:016x}"

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(
            SourceKind.DOCUMENT,
            AuthorityScope.TENANT_PRINCIPAL,
            self.tenant_id,
            self.principal_id,
            CanonicalObjectKind.RAW_OBJECT,
            self.raw_id,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _DocumentCaseDiagnostic:
    case: RecallCaseV1
    recall_class: DocumentRecallClassV1
    target: _DocumentSpec
    expected_passage_ref: PassageRef
    expected_channels: tuple[ArchiveMatchChannel, ...]
    discovery_request: ArchiveSearchRequest
    discovery_filename: str
    discovery_navigation_only: bool
    negative_control: _DocumentSpec
    expected_negative_channels: tuple[ArchiveMatchChannel, ...]
    safety_request: ArchiveSearchRequest | None = None

    @property
    def case_id(self) -> str:
        return self.case.case_id


@dataclass(frozen=True, slots=True, repr=False)
class _DocumentSyntheticPlan:
    cases: tuple[RecallCaseV1, ...]
    diagnostics: tuple[_DocumentCaseDiagnostic, ...]
    documents: tuple[_DocumentSpec, ...]
    foreign_principal_id: str
    foreign_tenant_id: str

    def diagnostic(self, case_id: str) -> _DocumentCaseDiagnostic:
        match = next((item for item in self.diagnostics if item.case_id == case_id), None)
        if match is None:
            raise KeyError(case_id)
        return match


_FILENAME_BODY: Final = "Frosted archive evidence marker 1103 selects one canonical passage."
_ALIAS_BODY: Final = "Amber alias evidence marker 2207 selects one canonical passage."
_FORMAT_BODY: Final = "Indigo format evidence marker 3301 selects one canonical passage."
_DATE_BODY: Final = "Saffron own-date evidence marker 4409 selects one canonical passage."
_TRUNCATION_PREFIX: Final = "bounded extraction prefix row\n" * 430 + _VISIBLE_TRUNCATION_MARKER

_FILENAME_TARGET = _DocumentSpec(
    1,
    _FILENAME_BODY,
    "s4r7-filename-saffron.txt",
    "text/plain",
    "2026-08-01T09:00:00+00:00",
)
_FILENAME_DECOY = _DocumentSpec(
    2,
    _FILENAME_BODY,
    "opaque-filename-decoy.bin",
    "application/octet-stream",
    "2026-08-02T09:00:00+00:00",
)
_ALIAS_TARGET = _DocumentSpec(
    3,
    _ALIAS_BODY,
    "opaque-alias-carrier.bin",
    "application/octet-stream",
    "2026-08-03T09:00:00+00:00",
    alias="s4r7-historical-alias.odt",
)
_ALIAS_DECOY = _DocumentSpec(
    4,
    _ALIAS_BODY,
    "opaque-alias-decoy.bin",
    "application/octet-stream",
    "2026-08-04T09:00:00+00:00",
)
_FORMAT_TARGET = _DocumentSpec(
    5,
    _FORMAT_BODY,
    "s4r7-format-carrier",
    "text/plain",
    "2026-08-05T09:00:00+00:00",
)
_FORMAT_DECOY = _DocumentSpec(
    6,
    _FORMAT_BODY,
    "s4r7-format-decoy",
    "application/octet-stream",
    "2026-08-06T09:00:00+00:00",
)
_DATE_TARGET = _DocumentSpec(
    7,
    _DATE_BODY,
    "s4r7-own-date-target.txt",
    "text/plain",
    "2026-08-07T09:00:00+00:00",
    document_date="2024-05-10",
)
_DATE_DECOY = _DocumentSpec(
    8,
    _DATE_BODY,
    "s4r7-own-date-decoy.txt",
    "text/plain",
    "2026-08-08T09:00:00+00:00",
    document_date="2025-05-10",
)
_TRUNCATION_TARGET = _DocumentSpec(
    9,
    _TRUNCATION_PREFIX,
    "s4r7-truncated-carrier.txt",
    "text/plain",
    "2026-08-09T09:00:00+00:00",
    text_truncated=True,
)
_FOREIGN_DECOY = _DocumentSpec(
    10,
    " ".join((_FILENAME_BODY, _ALIAS_BODY, _FORMAT_BODY, _DATE_BODY, _VISIBLE_TRUNCATION_MARKER)),
    "s4r7-private-foreign.txt",
    "text/plain",
    "2026-08-10T09:00:00+00:00",
    principal_id=_FOREIGN_PRINCIPAL,
    document_date="2024-05-10",
)
_FOREIGN_TENANT_DECOY = _DocumentSpec(
    11,
    " ".join((_FILENAME_BODY, _ALIAS_BODY, _FORMAT_BODY, _DATE_BODY, _VISIBLE_TRUNCATION_MARKER)),
    "s4r7-private-foreign-tenant.txt",
    "text/plain",
    "2026-08-13T08:00:00+00:00",
    tenant_id=_FOREIGN_TENANT,
    document_date="2024-05-10",
)

_DOCUMENTS: Final = (
    _FILENAME_TARGET,
    _FILENAME_DECOY,
    _ALIAS_TARGET,
    _ALIAS_DECOY,
    _FORMAT_TARGET,
    _FORMAT_DECOY,
    _DATE_TARGET,
    _DATE_DECOY,
    _TRUNCATION_TARGET,
    _FOREIGN_DECOY,
    _FOREIGN_TENANT_DECOY,
)


def _case_privacy_key(ordinal: int) -> str:
    return hashlib.sha256(f"friday/document-recall-case-key/v1/{ordinal:04d}".encode("ascii")).hexdigest()


def _passage_ref(spec: _DocumentSpec, *, query: str) -> PassageRef:
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, spec.raw_id)
    revision = SourceRevision(
        representation,
        RevisionKind.RAW_CONTENT_SHA256,
        hashlib.sha256(spec.body.encode("utf-8")).hexdigest(),
    )
    if spec.text_truncated:
        start = spec.body.rfind("\n") + 1
        if spec.body[start:] != query:
            raise ValueError("truncation qrel must be the exact final visible line")
        locator = TextSpanLocator(0, start, len(spec.body))
        index_version = LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    else:
        if "\n" in spec.body or len(spec.body) > 720 or query not in spec.body:
            raise ValueError("short document qrel is not independently closed")
        locator = TextSpanLocator(0, 0, len(spec.body))
        index_version = DOCUMENT_STORED_PASSAGE_INDEX_VERSION
    return PassageRef(
        source_ref=spec.source_ref,
        source_revision=revision,
        locator=locator,
        passage_index_version=index_version,
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )


def _date_constraint() -> ArchiveTemporalConstraint:
    return ArchiveTemporalConstraint(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        role=TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
        value_kind=TemporalValueKind.DATE_INTERVAL,
        precision=TemporalPrecision.DAY,
        start="2024-05-10",
        end="2024-05-11",
    )


def _recall_case(
    ordinal: int,
    *,
    target: _DocumentSpec,
    query: str,
    taxonomy: RecallTaxonomyV1,
    filename_hints: tuple[str, ...] = (),
    temporal_constraints: tuple[ArchiveTemporalConstraint, ...] = (),
) -> tuple[RecallCaseV1, PassageRef]:
    privacy_key_hex = _case_privacy_key(ordinal)
    privacy_key = bytes.fromhex(privacy_key_hex)
    passage_ref = _passage_ref(target, query=query)
    temporal_role = temporal_constraints[0].role if temporal_constraints else None
    case = RecallCaseV1(
        case_id=f"document.case.{ordinal:04d}",
        privacy_key_hex=privacy_key_hex,
        taxonomy=taxonomy,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
        request=ArchiveSearchRequest.create(
            query=query,
            corpora=(ArchiveSearchCorpus.DOCUMENTS,),
            filename_hints=filename_hints,
            temporal_constraints=temporal_constraints,
            limit=20,
        ),
        expected_corpus=ArchiveSearchCorpus.DOCUMENTS,
        alternatives=(
            RecallAlternativeV1(
                source_identity=opaque_source_identity(target.source_ref, privacy_key),
                passage_window_identities=(opaque_passage_window_identity(passage_ref, privacy_key),),
                locator_kind=PassageLocatorKind.TEXT_SPAN,
                relevance_grade=3,
                temporal_role=temporal_role,
            ),
        ),
        expected_no_hit=False,
    )
    return case, passage_ref


def document_synthetic_plan() -> _DocumentSyntheticPlan:
    """Return the immutable five-class plan without exposing a body map."""

    filename_case, filename_passage = _recall_case(
        1,
        target=_FILENAME_TARGET,
        query="Frosted archive evidence marker 1103",
        taxonomy=RecallTaxonomyV1.UNHELPFUL_FILENAME,
        filename_hints=(_FILENAME_TARGET.filename,),
    )
    alias_case, alias_passage = _recall_case(
        2,
        target=_ALIAS_TARGET,
        query="Amber alias evidence marker 2207",
        taxonomy=RecallTaxonomyV1.UNHELPFUL_FILENAME,
        filename_hints=(_ALIAS_TARGET.alias or "",),
    )
    format_case, format_passage = _recall_case(
        3,
        target=_FORMAT_TARGET,
        query="Indigo format evidence marker 3301",
        taxonomy=RecallTaxonomyV1.UNHELPFUL_FILENAME,
        filename_hints=(_FORMAT_TARGET.mime_type,),
    )
    date_constraint = _date_constraint()
    date_case, date_passage = _recall_case(
        4,
        target=_DATE_TARGET,
        query="Saffron own-date evidence marker 4409",
        taxonomy=RecallTaxonomyV1.APPROXIMATE_DATE,
        temporal_constraints=(date_constraint,),
    )
    truncation_case, truncation_passage = _recall_case(
        5,
        target=_TRUNCATION_TARGET,
        query=_VISIBLE_TRUNCATION_MARKER,
        taxonomy=RecallTaxonomyV1.APPROXIMATE_CONTENT,
    )
    diagnostics = (
        _DocumentCaseDiagnostic(
            filename_case,
            DocumentRecallClassV1.FILENAME,
            _FILENAME_TARGET,
            filename_passage,
            (ArchiveMatchChannel.CATALOG, ArchiveMatchChannel.LEXICAL),
            ArchiveSearchRequest.create(
                query=_FILENAME_TARGET.filename,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
            _FILENAME_TARGET.filename,
            True,
            _FILENAME_DECOY,
            (ArchiveMatchChannel.LEXICAL,),
        ),
        _DocumentCaseDiagnostic(
            alias_case,
            DocumentRecallClassV1.ALIAS,
            _ALIAS_TARGET,
            alias_passage,
            (ArchiveMatchChannel.CATALOG, ArchiveMatchChannel.LEXICAL),
            ArchiveSearchRequest.create(
                query=_ALIAS_TARGET.alias or "",
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
            _ALIAS_TARGET.alias or "",
            True,
            _ALIAS_DECOY,
            (ArchiveMatchChannel.LEXICAL,),
        ),
        _DocumentCaseDiagnostic(
            format_case,
            DocumentRecallClassV1.FORMAT,
            _FORMAT_TARGET,
            format_passage,
            (ArchiveMatchChannel.CATALOG, ArchiveMatchChannel.LEXICAL),
            ArchiveSearchRequest.create(
                query=_FORMAT_TARGET.mime_type,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
            _FORMAT_TARGET.filename,
            True,
            _FORMAT_DECOY,
            (ArchiveMatchChannel.LEXICAL,),
            safety_request=ArchiveSearchRequest.create(
                query="application/pdf",
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
        ),
        _DocumentCaseDiagnostic(
            date_case,
            DocumentRecallClassV1.DATE,
            _DATE_TARGET,
            date_passage,
            (ArchiveMatchChannel.LEXICAL,),
            date_case.request,
            _DATE_TARGET.filename,
            False,
            _DATE_DECOY,
            (),
            safety_request=ArchiveSearchRequest.create(
                query="Frosted archive evidence marker 1103",
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                temporal_constraints=(
                    ArchiveTemporalConstraint(
                        corpus=ArchiveSearchCorpus.DOCUMENTS,
                        role=TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
                        value_kind=TemporalValueKind.DATE_INTERVAL,
                        precision=TemporalPrecision.DAY,
                        start="2024-02-28",
                        end="2024-02-29",
                    ),
                ),
                limit=20,
            ),
        ),
        _DocumentCaseDiagnostic(
            truncation_case,
            DocumentRecallClassV1.TRUNCATION,
            _TRUNCATION_TARGET,
            truncation_passage,
            (ArchiveMatchChannel.LEXICAL,),
            truncation_case.request,
            _TRUNCATION_TARGET.filename,
            False,
            _FOREIGN_DECOY,
            (),
            safety_request=ArchiveSearchRequest.create(
                query=_HIDDEN_TRUNCATION_MARKER,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
        ),
    )
    return _DocumentSyntheticPlan(
        tuple(item.case for item in diagnostics),
        diagnostics,
        _DOCUMENTS,
        _FOREIGN_PRINCIPAL,
        _FOREIGN_TENANT,
    )


def seed_document_synthetic(storage: _DocumentSyntheticStorage) -> None:
    """Seed only a caller-owned ephemeral storage instance."""

    storage.ensure_user(SYNTHETIC_TENANT)
    storage.ensure_user(SYNTHETIC_PRINCIPAL)
    storage.ensure_user(_FOREIGN_PRINCIPAL)
    storage.ensure_user(_FOREIGN_TENANT)
    for document in _DOCUMENTS:
        metadata: dict[str, object] = {
            "extraction_success": True,
            "filename": document.filename,
            "media_kind": "document",
            "mime_type": document.mime_type,
            "text_extraction_success": True,
            "uploaded_by": document.principal_id,
        }
        if document.document_date is not None:
            metadata["document_date"] = document.document_date
        if document.text_truncated:
            metadata["text_truncated"] = True
        content_hash = hashlib.sha256(document.body.encode("utf-8")).hexdigest()
        storage.store_raw_object(
            RawObject(
                id=document.raw_id,
                user_id=document.tenant_id,
                source="upload",
                source_ref=f"document-recall:{document.ordinal:04d}",
                raw_content=document.body,
                content_type="file",
                metadata_json=metadata,
                content_hash=content_hash,
                received_at=document.received_at,
                created_at=document.received_at,
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=document.inbox_id,
                user_id=document.tenant_id,
                raw_object_id=document.raw_id,
                status=InboxStatus.CLASSIFIED,
                created_at=document.received_at,
                reviewed_at=document.received_at,
                reviewed_by=document.principal_id,
            )
        )

    with storage.transaction() as conn:
        for document in _DOCUMENTS:
            if document.alias is None:
                continue
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    document.tenant_id,
                    document.principal_id,
                    f"telegram-file:S4R7-{document.ordinal:04d}",
                    document.raw_id,
                    document.alias,
                    document.received_at,
                ),
            )
        boundary_at = "2026-08-11T09:00:00+00:00"
        conn.execute(
            """INSERT INTO conversations(
                   id,user_id,title,last_message,unread_count,is_pinned,is_archived,
                   mode,created_at,updated_at
               ) VALUES(?,?,?,'',0,0,0,'dialogue',?,?)""",
            (
                BOUNDARY_CONVERSATION_ID,
                SYNTHETIC_PRINCIPAL,
                "Synthetic document recall boundary",
                boundary_at,
                boundary_at,
            ),
        )
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(?,?,?,'user','synthetic document recall request','{}',NULL,?)""",
            (
                BOUNDARY_MESSAGE_ID,
                BOUNDARY_CONVERSATION_ID,
                SYNTHETIC_PRINCIPAL,
                boundary_at,
            ),
        )


__all__ = [
    "DocumentRecallClassV1",
    "document_synthetic_plan",
    "seed_document_synthetic",
]
