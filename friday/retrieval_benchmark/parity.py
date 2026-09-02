"""Body-free parity evidence for the shipped archive facade and legacy readers.

This module deliberately owns a corpus separate from the recall benchmark.  The
existing 21-case manifest remains the release recall baseline; this smaller
matrix asks a different question: whether the accepted archive projection and
one applicable internal adapter retain the same source membership and order.

Private queries, bodies and durable identities exist only inside the temporary
process.  The report contains deterministic synthetic pseudonyms and aggregate facts, but
it cannot be used as runtime authority and does not claim support for dimensions
whose dependencies or contracts are absent from this offline corpus.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal, cast

from friday.config import ensure_runtime_dirs, load_settings
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BYTES,
    ArchiveSearchCandidateProjectionEntry,
    abandon_empty_archive_model_batch_ledger,
    attest_archive_search_before_publication,
    consume_archive_model_batch_ledger_fail_closed,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_service import (
    PreparedArchiveSearch,
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    SourceKind,
    SourceRef,
)
from friday.retrieval_benchmark._canonical import canonical_json
from friday.retrieval_benchmark.contracts import opaque_case_identity, opaque_source_identity
from friday.retrieval_benchmark.harness import _isolated_friday_environment
from friday.retrieval_benchmark.release import (
    RecallReleaseIdentityError,
    archive_search_release_sha256,
)
from friday.source_identity import source_search_page_snapshots
from friday.storage import FridayStorage, init_storage
from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject

PARITY_REPORT_SCHEMA: Final = "friday.retrieval-recall-parity-report.body-free.v1"
PARITY_CASE_SCHEMA: Final = "friday.retrieval-recall-parity-case.body-free.v1"
PARITY_DIMENSION_SCHEMA: Final = "friday.retrieval-recall-parity-dimension.body-free.v1"

_TENANT: Final = "recall-parity-tenant"
_PRINCIPAL: Final = "recall-parity-principal"
_BOUNDARY_CONVERSATION_ID: Final = "conv_f100000000000001"
_BOUNDARY_MESSAGE_ID: Final = "msg_f100000000000001"
_RAW_LITERAL_ID: Final = "raw_a100000000000001"
_RAW_LITERAL_DECOY_ID: Final = "raw_a100000000000004"
_RAW_PENDING_ID: Final = "raw_a100000000000002"
_RAW_KNOWLEDGE_ID: Final = "raw_a100000000000003"
_RAW_FOCUSED_ID: Final = "raw_a100000000000005"
_RAW_FOCUSED_DECOY_ID: Final = "raw_a100000000000006"
_KNOWLEDGE_ID: Final = "ko_a100000000000001"
_MESSAGE_LITERAL_CONVERSATION_ID: Final = "conv_a100000000000001"
_MESSAGE_LITERAL_ID: Final = "msg_a100000000000001"
_MESSAGE_LITERAL_SECOND_ID: Final = "msg_a100000000000003"
_MESSAGE_LITERAL_DECOY_CONVERSATION_ID: Final = "conv_a100000000000003"
_MESSAGE_LITERAL_DECOY_ID: Final = "msg_a100000000000004"
_MESSAGE_LAYOUT_CONVERSATION_ID: Final = "conv_a100000000000002"
_MESSAGE_LAYOUT_ID: Final = "msg_a100000000000002"
_MESSAGE_ORDER_RELEVANT_CONVERSATION_ID: Final = "conv_a100000000000004"
_MESSAGE_ORDER_RELEVANT_ID: Final = "msg_a100000000000005"
_MESSAGE_ORDER_RECENT_CONVERSATION_ID: Final = "conv_a100000000000005"
_MESSAGE_ORDER_RECENT_ID: Final = "msg_a100000000000006"

_RAW_LITERAL_QUERY: Final = "saffronneedle"
_RAW_PENDING_QUERY: Final = "quartzpendingneedle"
_KNOWLEDGE_QUERY: Final = "cobaltpromotedneedle"
_RAW_FOCUSED_QUERY: Final = "orionfocusneedle"
_RAW_FOCUSED_FOCUS: Final = "orionfocusneedle role"
_MESSAGE_LITERAL_QUERY: Final = "lanternmessageneedle"
_MESSAGE_LAYOUT_QUERY: Final = "Uhfabr lt;ehcnd"
_MESSAGE_ORDER_QUERY: Final = "фдзрф иуеф"

_RAW_LITERAL_BODY: Final = "saffronneedle evidence alpha."
_RAW_LITERAL_DECOY_BODY: Final = "saffronneedle evidence bravo."
_RAW_PENDING_BODY: Final = "The pending source contains quartzpendingneedle evidence."
_RAW_KNOWLEDGE_BODY: Final = "The raw source precedes its promoted projection."
_RAW_FOCUSED_BODY: Final = "orionfocusneedle role commander."
_RAW_FOCUSED_DECOY_BODY: Final = "orionfocusneedle role"
_KNOWLEDGE_BODY: Final = "The promoted knowledge contains cobaltpromotedneedle evidence."
_MESSAGE_LITERAL_BODY: Final = "The chat contains lanternmessageneedle evidence."
_MESSAGE_LITERAL_SECOND_BODY: Final = "A second lanternmessageneedle passage."
_MESSAGE_LITERAL_DECOY_BODY: Final = "Another chat contains lanternmessageneedle evidence."
_MESSAGE_LAYOUT_BODY: Final = "График дежурств на август утверждён."
_MESSAGE_ORDER_RELEVANT_BODY: Final = "alpha alpha alpha beta beta"
_MESSAGE_ORDER_RECENT_BODY: Final = "alpha beta " + "context " * 40
_BOUNDARY_BODY: Final = "Synthetic accepted parity request."

_Adapter = Literal["memory_search", "message_search", "source_search"]
_Status = Literal["mismatch", "parity", "partial", "unsupported"]
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RUNS = itertools.count(1)

_MEMBERSHIP_LAYOUT_REASON: Final = "archive_message_keyboard_repair_unavailable"
_UNCLASSIFIED_MISMATCH_REASON: Final = "unclassified_candidate_membership_mismatch"

UNSUPPORTED_REASON_CODES: Final = {
    "bitemporal_graph": "bitemporal_graph_snapshot_not_seeded",
    "dense_semantic": "offline_dense_semantic_dependencies_disabled",
    "queryless_message_window": "archive_query_contract_requires_nonempty_query",
}

_PRIVATE_OUTPUT_SENTINELS: Final = (
    _TENANT,
    _PRINCIPAL,
    _BOUNDARY_CONVERSATION_ID,
    _BOUNDARY_MESSAGE_ID,
    _RAW_LITERAL_ID,
    _RAW_LITERAL_DECOY_ID,
    _RAW_PENDING_ID,
    _RAW_KNOWLEDGE_ID,
    _RAW_FOCUSED_ID,
    _RAW_FOCUSED_DECOY_ID,
    _KNOWLEDGE_ID,
    _MESSAGE_LITERAL_CONVERSATION_ID,
    _MESSAGE_LITERAL_ID,
    _MESSAGE_LITERAL_SECOND_ID,
    _MESSAGE_LITERAL_DECOY_CONVERSATION_ID,
    _MESSAGE_LITERAL_DECOY_ID,
    _MESSAGE_LAYOUT_CONVERSATION_ID,
    _MESSAGE_LAYOUT_ID,
    _MESSAGE_ORDER_RELEVANT_CONVERSATION_ID,
    _MESSAGE_ORDER_RELEVANT_ID,
    _MESSAGE_ORDER_RECENT_CONVERSATION_ID,
    _MESSAGE_ORDER_RECENT_ID,
    _RAW_LITERAL_QUERY,
    _RAW_PENDING_QUERY,
    _KNOWLEDGE_QUERY,
    _RAW_FOCUSED_QUERY,
    _RAW_FOCUSED_FOCUS,
    _MESSAGE_LITERAL_QUERY,
    _MESSAGE_LAYOUT_QUERY,
    _MESSAGE_ORDER_QUERY,
    _RAW_LITERAL_BODY,
    _RAW_LITERAL_DECOY_BODY,
    _RAW_PENDING_BODY,
    _RAW_KNOWLEDGE_BODY,
    _RAW_FOCUSED_BODY,
    _RAW_FOCUSED_DECOY_BODY,
    _KNOWLEDGE_BODY,
    _MESSAGE_LITERAL_BODY,
    _MESSAGE_LITERAL_SECOND_BODY,
    _MESSAGE_LITERAL_DECOY_BODY,
    _MESSAGE_LAYOUT_BODY,
    _MESSAGE_ORDER_RELEVANT_BODY,
    _MESSAGE_ORDER_RECENT_BODY,
    _BOUNDARY_BODY,
    "literal-source.txt",
    "literal-source-decoy.txt",
    "pending-source.txt",
    "promoted-source.txt",
    "focused-source.txt",
    "focused-source-decoy.txt",
    "Promoted record",
    "Literal parity conversation",
    "Literal parity decoy conversation",
    "Keyboard parity conversation",
    "Relevance-ranked parity conversation",
    "Recent parity conversation",
    "Accepted parity turn",
    "/home/",
    '"query"',
    '"path"',
    '"body"',
    '"excerpt"',
)


class ParityHarnessError(RuntimeError):
    """The real ephemeral parity path failed closed."""


def _digest(value: Mapping[str, object] | list[object], *, domain: bytes) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_json(value).encode("ascii")).hexdigest()


def _keyed_digest(
    value: Mapping[str, object] | list[object],
    *,
    domain: bytes,
    key: bytes,
) -> str:
    return hmac.new(key, domain + b"\0" + canonical_json(value).encode("ascii"), hashlib.sha256).hexdigest()


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ParityHarnessError(f"{label} is invalid")
    return value


def _require_count(value: object, *, label: str, maximum: int = 1_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ParityHarnessError(f"{label} is invalid")
    return value


def _contains_private_material(serialized: str) -> bool:
    """Cover both literal and JSON-escaped spellings of private sentinels."""

    return any(
        value in serialized or json.dumps(value, ensure_ascii=True)[1:-1] in serialized
        for value in _PRIVATE_OUTPUT_SENTINELS
    )


def _optional_rank(value: object, *, count: int, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= count:
        raise ParityHarnessError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ParityCaseResultV1:
    case_id: str
    case_sha256: str
    expected_corpus: ArchiveSearchCorpus
    adapter: _Adapter
    expected_source_identity: str
    archive_source_identities: tuple[str, ...]
    archive_publication_source_identities: tuple[str, ...]
    adapter_source_identities: tuple[str, ...]
    archive_expected_rank: int | None
    adapter_expected_rank: int | None
    membership_status: Literal["mismatch", "parity"]
    order_status: Literal["mismatch", "not_comparable", "parity"]
    reason_code: str | None

    def __post_init__(self) -> None:
        _require_digest(self.case_id, label="parity case identity")
        _require_digest(self.case_sha256, label="parity case digest")
        _require_digest(self.expected_source_identity, label="expected source identity")
        if type(self.expected_corpus) is not ArchiveSearchCorpus:
            raise ParityHarnessError("parity expected corpus is invalid")
        if self.adapter not in {"memory_search", "message_search", "source_search"}:
            raise ParityHarnessError("parity adapter is invalid")
        for label, values in (
            ("archive source identities", self.archive_source_identities),
            (
                "archive publication source identities",
                self.archive_publication_source_identities,
            ),
            ("adapter source identities", self.adapter_source_identities),
        ):
            if (
                type(values) is not tuple
                or len(values) > 100
                or len(values) != len(set(values))
                or any(type(item) is not str or _DIGEST.fullmatch(item) is None for item in values)
            ):
                raise ParityHarnessError(f"{label} are invalid")
        if not set(self.archive_publication_source_identities) <= set(self.archive_source_identities):
            raise ParityHarnessError("archive publication membership is invalid")
        archive_rank = _optional_rank(
            self.archive_expected_rank,
            count=len(self.archive_source_identities),
            label="archive expected rank",
        )
        adapter_rank = _optional_rank(
            self.adapter_expected_rank,
            count=len(self.adapter_source_identities),
            label="adapter expected rank",
        )
        expected_archive_rank = (
            self.archive_source_identities.index(self.expected_source_identity) + 1
            if self.expected_source_identity in self.archive_source_identities
            else None
        )
        expected_adapter_rank = (
            self.adapter_source_identities.index(self.expected_source_identity) + 1
            if self.expected_source_identity in self.adapter_source_identities
            else None
        )
        if archive_rank != expected_archive_rank or adapter_rank != expected_adapter_rank:
            raise ParityHarnessError("parity expected rank contradicts source order")
        same_membership = set(self.archive_source_identities) == set(self.adapter_source_identities)
        expected_status = "parity" if same_membership else "mismatch"
        if self.membership_status != expected_status:
            raise ParityHarnessError("parity membership status contradicts source membership")
        expected_order_status = (
            "parity"
            if self.archive_source_identities == self.adapter_source_identities
            else "mismatch"
            if same_membership
            else "not_comparable"
        )
        if self.order_status != expected_order_status:
            raise ParityHarnessError("parity order status contradicts source order")
        if (self.membership_status == "parity") != (self.reason_code is None):
            raise ParityHarnessError("parity mismatch reason is invalid")
        if self.reason_code is not None and (not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", self.reason_code)):
            raise ParityHarnessError("parity mismatch reason is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "adapter_candidate_count": len(self.adapter_source_identities),
            "adapter_expected_rank": self.adapter_expected_rank,
            "adapter_source_identities": list(self.adapter_source_identities),
            "archive_candidate_count": len(self.archive_source_identities),
            "archive_expected_rank": self.archive_expected_rank,
            "archive_expected_publication_eligible": (
                self.expected_source_identity in self.archive_publication_source_identities
            ),
            "archive_publication_candidate_count": len(self.archive_publication_source_identities),
            "archive_publication_source_identities": list(self.archive_publication_source_identities),
            "archive_source_identities": list(self.archive_source_identities),
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "expected_corpus": self.expected_corpus.value,
            "expected_source_identity": self.expected_source_identity,
            "membership_status": self.membership_status,
            "order_status": self.order_status,
            "reason_code": self.reason_code,
            "schema": PARITY_CASE_SCHEMA,
        }


@dataclass(frozen=True, slots=True)
class ParityDimensionV1:
    name: str
    status: _Status
    compared: int
    matched: int
    mismatched: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name):
            raise ParityHarnessError("parity dimension name is invalid")
        if self.status not in {"mismatch", "parity", "partial", "unsupported"}:
            raise ParityHarnessError("parity dimension status is invalid")
        compared = _require_count(self.compared, label="parity compared count")
        matched = _require_count(self.matched, label="parity matched count")
        mismatched = _require_count(self.mismatched, label="parity mismatched count")
        if compared != matched + mismatched:
            raise ParityHarnessError("parity dimension counts disagree")
        if (
            type(self.reason_codes) is not tuple
            or self.reason_codes != tuple(sorted(set(self.reason_codes)))
            or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", item) for item in self.reason_codes)
        ):
            raise ParityHarnessError("parity dimension reasons are invalid")
        if self.status == "unsupported" and (compared or not self.reason_codes):
            raise ParityHarnessError("unsupported parity dimension is invalid")
        if self.status == "parity" and (not compared or mismatched or self.reason_codes):
            raise ParityHarnessError("successful parity dimension is invalid")
        if self.status == "mismatch" and (not mismatched or not self.reason_codes):
            raise ParityHarnessError("mismatched parity dimension is invalid")
        if self.status == "partial" and not self.reason_codes:
            raise ParityHarnessError("partial parity dimension is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "compared": self.compared,
            "matched": self.matched,
            "mismatched": self.mismatched,
            "name": self.name,
            "reason_codes": list(self.reason_codes),
            "schema": PARITY_DIMENSION_SCHEMA,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, repr=False)
class ParityReportV1:
    release_sha256: str
    case_manifest_sha256: str
    cases: tuple[ParityCaseResultV1, ...]
    dimensions: tuple[ParityDimensionV1, ...]
    report_sha256: str

    def __post_init__(self) -> None:
        _require_digest(self.release_sha256, label="parity release digest")
        _require_digest(self.case_manifest_sha256, label="parity case manifest")
        _require_digest(self.report_sha256, label="parity report digest")
        if (
            type(self.cases) is not tuple
            or len(self.cases) != 7
            or any(type(item) is not ParityCaseResultV1 for item in self.cases)
            or tuple(item.case_id for item in self.cases)
            != tuple(sorted(item.case_id for item in self.cases))
            or len({item.case_id for item in self.cases}) != len(self.cases)
        ):
            raise ParityHarnessError("parity report cases are invalid")
        if (
            type(self.dimensions) is not tuple
            or not self.dimensions
            or any(type(item) is not ParityDimensionV1 for item in self.dimensions)
            or tuple(item.name for item in self.dimensions)
            != tuple(sorted(item.name for item in self.dimensions))
            or len({item.name for item in self.dimensions}) != len(self.dimensions)
        ):
            raise ParityHarnessError("parity report dimensions are invalid")
        expected_manifest = _digest(
            [{"case_id": item.case_id, "case_sha256": item.case_sha256} for item in self.cases],
            domain=b"friday/retrieval-recall-parity-case-manifest/v1",
        )
        if not hmac.compare_digest(self.case_manifest_sha256, expected_manifest):
            raise ParityHarnessError("parity case manifest is forged")
        expected_report = _digest(
            self._payload_without_digest(),
            domain=b"friday/retrieval-recall-parity-report/v1",
        )
        if not hmac.compare_digest(self.report_sha256, expected_report):
            raise ParityHarnessError("parity report digest is forged")
        serialized = self.to_json()
        if _contains_private_material(serialized):
            raise ParityHarnessError("parity report contains private material")

    def __repr__(self) -> str:
        return f"ParityReportV1(case_count={len(self.cases)}, body_free=True)"

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "case_count": len(self.cases),
            "case_manifest_sha256": self.case_manifest_sha256,
            "cases": [item.to_payload() for item in self.cases],
            "dimensions": [item.to_payload() for item in self.dimensions],
            "evidence_source": "synthetic_ephemeral",
            "identity_kind": "deterministic_synthetic_pseudonym_v1",
            "release_sha256": self.release_sha256,
            "schema": PARITY_REPORT_SCHEMA,
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._payload_without_digest(), "report_sha256": self.report_sha256}

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @classmethod
    def create(
        cls,
        *,
        release_sha256: str,
        cases: Iterable[ParityCaseResultV1],
        dimensions: Iterable[ParityDimensionV1],
    ) -> ParityReportV1:
        case_values = tuple(sorted(cases, key=lambda item: item.case_id))
        dimension_values = tuple(sorted(dimensions, key=lambda item: item.name))
        manifest = _digest(
            [{"case_id": item.case_id, "case_sha256": item.case_sha256} for item in case_values],
            domain=b"friday/retrieval-recall-parity-case-manifest/v1",
        )
        base = {
            "case_count": len(case_values),
            "case_manifest_sha256": manifest,
            "cases": [item.to_payload() for item in case_values],
            "dimensions": [item.to_payload() for item in dimension_values],
            "evidence_source": "synthetic_ephemeral",
            "identity_kind": "deterministic_synthetic_pseudonym_v1",
            "release_sha256": release_sha256,
            "schema": PARITY_REPORT_SCHEMA,
        }
        return cls(
            release_sha256=release_sha256,
            case_manifest_sha256=manifest,
            cases=case_values,
            dimensions=dimension_values,
            report_sha256=_digest(
                base,
                domain=b"friday/retrieval-recall-parity-report/v1",
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class _ParityProbe:
    ordinal: int
    private_case_id: str
    request: ArchiveSearchRequest
    adapter: _Adapter
    expected_source_ref: SourceRef
    mismatch_reason: str | None = None

    @property
    def privacy_key(self) -> bytes:
        return hashlib.sha256(
            f"friday/retrieval-recall-parity-key/v1/{self.ordinal:04d}".encode("ascii")
        ).digest()

    @property
    def opaque_case_id(self) -> str:
        return opaque_case_identity(self.private_case_id, self.privacy_key)

    @property
    def case_sha256(self) -> str:
        return _keyed_digest(
            {
                "adapter": self.adapter,
                "expected_source": self.expected_source_ref.to_private_payload(),
                "request": self.request.to_identity_payload(),
                "schema": "friday.retrieval-recall-parity-private-case.v1",
            },
            domain=b"friday/retrieval-recall-parity-private-case/v1",
            key=self.privacy_key,
        )

    @property
    def expected_source_identity(self) -> str:
        return opaque_source_identity(self.expected_source_ref, self.privacy_key)


def _document_source(raw_id: str) -> SourceRef:
    return SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        _TENANT,
        _PRINCIPAL,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )


def _message_source(conversation_id: str) -> SourceRef:
    return SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        _PRINCIPAL,
        CanonicalObjectKind.CONVERSATION,
        conversation_id,
    )


def _probes() -> tuple[_ParityProbe, ...]:
    return (
        _ParityProbe(
            1,
            "parity.case.0001",
            ArchiveSearchRequest.create(
                query=_RAW_LITERAL_QUERY,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
            "source_search",
            _document_source(_RAW_LITERAL_ID),
        ),
        _ParityProbe(
            2,
            "parity.case.0002",
            ArchiveSearchRequest.create(
                query=_RAW_PENDING_QUERY,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                limit=20,
            ),
            "source_search",
            _document_source(_RAW_PENDING_ID),
        ),
        _ParityProbe(
            3,
            "parity.case.0003",
            ArchiveSearchRequest.create(
                query=_KNOWLEDGE_QUERY,
                corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
                limit=20,
            ),
            "memory_search",
            _document_source(_RAW_KNOWLEDGE_ID),
        ),
        _ParityProbe(
            4,
            "parity.case.0004",
            ArchiveSearchRequest.create(
                query=_MESSAGE_LITERAL_QUERY,
                corpora=(ArchiveSearchCorpus.MESSAGES,),
                limit=20,
            ),
            "message_search",
            _message_source(_MESSAGE_LITERAL_CONVERSATION_ID),
        ),
        _ParityProbe(
            5,
            "parity.case.0005",
            ArchiveSearchRequest.create(
                query=_MESSAGE_LAYOUT_QUERY,
                corpora=(ArchiveSearchCorpus.MESSAGES,),
                limit=20,
            ),
            "message_search",
            _message_source(_MESSAGE_LAYOUT_CONVERSATION_ID),
            _MEMBERSHIP_LAYOUT_REASON,
        ),
        _ParityProbe(
            6,
            "parity.case.0006",
            ArchiveSearchRequest.create(
                query=_MESSAGE_ORDER_QUERY,
                corpora=(ArchiveSearchCorpus.MESSAGES,),
                limit=20,
            ),
            "message_search",
            _message_source(_MESSAGE_ORDER_RELEVANT_CONVERSATION_ID),
        ),
        _ParityProbe(
            7,
            "parity.case.0007",
            ArchiveSearchRequest.create(
                query=_RAW_FOCUSED_QUERY,
                corpora=(ArchiveSearchCorpus.DOCUMENTS,),
                focus=_RAW_FOCUSED_FOCUS,
                limit=20,
            ),
            "source_search",
            _document_source(_RAW_FOCUSED_ID),
        ),
    )


def _raw(
    raw_id: str,
    *,
    body: str,
    filename: str,
    timestamp: str,
) -> RawObject:
    return RawObject(
        id=raw_id,
        user_id=_TENANT,
        source="upload",
        source_ref=f"parity:{raw_id[-4:]}",
        raw_content=body,
        content_type="file",
        metadata_json={
            "filename": filename,
            "media_kind": "document",
            "mime_type": "text/plain",
            "uploaded_by": _PRINCIPAL,
        },
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        received_at=timestamp,
        created_at=timestamp,
    )


def _inbox(
    inbox_id: str,
    raw_id: str,
    *,
    status: InboxStatus,
    timestamp: str,
    knowledge_id: str | None = None,
) -> InboxItem:
    confirmed = status is InboxStatus.CLASSIFIED
    return InboxItem(
        id=inbox_id,
        user_id=_TENANT,
        raw_object_id=raw_id,
        knowledge_object_id=knowledge_id,
        status=status,
        created_at=timestamp,
        reviewed_at=timestamp if confirmed else None,
        reviewed_by=_PRINCIPAL if confirmed else None,
    )


def _seed_parity_storage(storage: FridayStorage) -> None:
    storage.ensure_user(_TENANT)
    storage.ensure_user(_PRINCIPAL)
    raw_rows = (
        (
            _raw(
                _RAW_LITERAL_ID,
                body=_RAW_LITERAL_BODY,
                filename="literal-source.txt",
                timestamp="2026-05-01T10:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000001",
                _RAW_LITERAL_ID,
                status=InboxStatus.CLASSIFIED,
                timestamp="2026-05-01T10:00:00+00:00",
            ),
        ),
        (
            _raw(
                _RAW_PENDING_ID,
                body=_RAW_PENDING_BODY,
                filename="pending-source.txt",
                timestamp="2026-05-02T10:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000002",
                _RAW_PENDING_ID,
                status=InboxStatus.PENDING,
                timestamp="2026-05-02T10:00:00+00:00",
            ),
        ),
        (
            _raw(
                _RAW_LITERAL_DECOY_ID,
                body=_RAW_LITERAL_DECOY_BODY,
                filename="literal-source-decoy.txt",
                timestamp="2026-05-01T09:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000004",
                _RAW_LITERAL_DECOY_ID,
                status=InboxStatus.CLASSIFIED,
                timestamp="2026-05-01T09:00:00+00:00",
            ),
        ),
        (
            _raw(
                _RAW_KNOWLEDGE_ID,
                body=_RAW_KNOWLEDGE_BODY,
                filename="promoted-source.txt",
                timestamp="2026-05-03T10:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000003",
                _RAW_KNOWLEDGE_ID,
                status=InboxStatus.CLASSIFIED,
                timestamp="2026-05-03T10:00:00+00:00",
                knowledge_id=_KNOWLEDGE_ID,
            ),
        ),
        (
            _raw(
                _RAW_FOCUSED_ID,
                body=_RAW_FOCUSED_BODY,
                filename="focused-source.txt",
                timestamp="2026-05-03T12:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000005",
                _RAW_FOCUSED_ID,
                status=InboxStatus.CLASSIFIED,
                timestamp="2026-05-03T12:00:00+00:00",
            ),
        ),
        (
            _raw(
                _RAW_FOCUSED_DECOY_ID,
                body=_RAW_FOCUSED_DECOY_BODY,
                filename="focused-source-decoy.txt",
                timestamp="2026-05-03T11:00:00+00:00",
            ),
            _inbox(
                "inbox_a100000000000006",
                _RAW_FOCUSED_DECOY_ID,
                status=InboxStatus.CLASSIFIED,
                timestamp="2026-05-03T11:00:00+00:00",
            ),
        ),
    )
    for raw, _inbox_item in raw_rows:
        storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=_KNOWLEDGE_ID,
            user_id=_TENANT,
            raw_object_id=_RAW_KNOWLEDGE_ID,
            content=_KNOWLEDGE_BODY,
            content_type="text/plain",
            title="Promoted record",
            created_at="2026-05-03T10:00:00+00:00",
            updated_at="2026-05-03T10:00:00+00:00",
        )
    )
    for _raw_object, inbox in raw_rows:
        storage.store_inbox_item(inbox)
    messages = (
        (
            _MESSAGE_LITERAL_CONVERSATION_ID,
            _MESSAGE_LITERAL_ID,
            "Literal parity conversation",
            _MESSAGE_LITERAL_BODY,
            "2026-05-04T10:00:00+00:00",
        ),
        (
            _MESSAGE_LAYOUT_CONVERSATION_ID,
            _MESSAGE_LAYOUT_ID,
            "Keyboard parity conversation",
            _MESSAGE_LAYOUT_BODY,
            "2026-05-05T10:00:00+00:00",
        ),
        (
            _MESSAGE_LITERAL_DECOY_CONVERSATION_ID,
            _MESSAGE_LITERAL_DECOY_ID,
            "Literal parity decoy conversation",
            _MESSAGE_LITERAL_DECOY_BODY,
            "2026-05-04T08:00:00+00:00",
        ),
        (
            _MESSAGE_ORDER_RELEVANT_CONVERSATION_ID,
            _MESSAGE_ORDER_RELEVANT_ID,
            "Relevance-ranked parity conversation",
            _MESSAGE_ORDER_RELEVANT_BODY,
            "2026-05-05T08:00:00+00:00",
        ),
        (
            _MESSAGE_ORDER_RECENT_CONVERSATION_ID,
            _MESSAGE_ORDER_RECENT_ID,
            "Recent parity conversation",
            _MESSAGE_ORDER_RECENT_BODY,
            "2026-05-05T09:00:00+00:00",
        ),
        (
            _BOUNDARY_CONVERSATION_ID,
            _BOUNDARY_MESSAGE_ID,
            "Accepted parity turn",
            _BOUNDARY_BODY,
            "2026-05-06T10:00:00+00:00",
        ),
    )
    with storage.transaction() as conn:
        for conversation_id, message_id, title, body, timestamp in messages:
            conn.execute(
                """INSERT INTO conversations(
                       id,user_id,title,last_message,unread_count,is_pinned,is_archived,
                       mode,created_at,updated_at
                   ) VALUES(?,?,?,'',0,0,0,'dialogue',?,?)""",
                (conversation_id, _PRINCIPAL, title, timestamp, timestamp),
            )
            conn.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                   ) VALUES(?,?,?,'user',?,'{}',NULL,?)""",
                (message_id, conversation_id, _PRINCIPAL, body, timestamp),
            )
            if conversation_id == _MESSAGE_LITERAL_CONVERSATION_ID:
                conn.execute(
                    """INSERT INTO messages(
                           id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                       ) VALUES(?,?,?,'user',?,'{}',NULL,?)""",
                    (
                        _MESSAGE_LITERAL_SECOND_ID,
                        _MESSAGE_LITERAL_CONVERSATION_ID,
                        _PRINCIPAL,
                        _MESSAGE_LITERAL_SECOND_BODY,
                        "2026-05-04T09:00:00+00:00",
                    ),
                )


def _actor() -> ActorContext:
    return ActorContext(
        user_id=_TENANT,
        preset_key="user",
        source="retrieval-recall-parity",
        shared_tenant=True,
        person_id=_PRINCIPAL,
    )


def _accepted_labels(payload: Mapping[str, object]) -> tuple[tuple[str, ...], int]:
    raw_candidates = payload.get("candidates")
    if type(raw_candidates) is not list:
        raise ParityHarnessError("archive parity candidates are invalid")
    labels: list[str] = []
    for raw_candidate in raw_candidates:
        if type(raw_candidate) is not dict:
            raise ParityHarnessError("archive parity candidate is invalid")
        candidate = cast(dict[str, object], raw_candidate)
        label = candidate.get("label")
        passages = candidate.get("passages")
        navigation_only = candidate.get("navigation_only")
        if type(label) is not str or type(passages) is not list or type(navigation_only) is not bool:
            raise ParityHarnessError("archive parity candidate is invalid")
        if (
            candidate.get("evidence_authority") == ArchiveEvidenceAuthority.CANONICAL.value
            and navigation_only is False
            and bool(passages)
        ):
            labels.append(label)
    if len(labels) != len(set(labels)):
        raise ParityHarnessError("archive parity citation labels collide")
    return tuple(labels), len(raw_candidates)


def _continuation(payload: Mapping[str, object]) -> str | None:
    value = payload.get("continuation")
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 512:
        raise ParityHarnessError("archive parity continuation is invalid")
    return value


def _run_archive_probe(
    storage: FridayStorage,
    authorization: AuthorizationService,
    actor: ActorContext,
    probe: _ParityProbe,
    *,
    release_sha256: str,
    run_number: int,
) -> tuple[tuple[SourceRef, ...], tuple[ArchiveSearchCandidateProjectionEntry, ...]]:
    ledger = create_archive_model_batch_ledger(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        turn_discriminator=f"recall-parity-{run_number}-{probe.ordinal}",
    )
    prepared_searches: list[PreparedArchiveSearch] = []
    discovered_sources: list[SourceRef] = []
    accepted_labels: list[str] = []
    candidate_count = 0
    admitted_bytes = 0
    request = probe.request
    admitted = False
    attestation_attempted = False
    try:
        for page_index in range(1, 6):
            messages_requested = ArchiveSearchCorpus.MESSAGES in request.corpora
            with storage.transaction() as conn:
                prepared = prepare_archive_search_in_transaction(
                    conn,
                    authorization=authorization,
                    actor=actor,
                    tenant_id=_TENANT,
                    principal_id=_PRINCIPAL,
                    request=request,
                    snapshot_discriminator=release_sha256,
                    run_discriminator=(f"recall-parity-{run_number}-{probe.ordinal}-page-{page_index}"),
                    turn_ledger=ledger,
                    current_conversation_id=(_BOUNDARY_CONVERSATION_ID if messages_requested else None),
                    boundary_user_message_id=_BOUNDARY_MESSAGE_ID if messages_requested else None,
                )
            payload = prepared.authorized_batch.public_tool_result_payload
            for result in prepared.authorized_batch._page.results:  # noqa: SLF001
                source = result.candidate.resolved_source.source_ref
                if source not in discovered_sources:
                    discovered_sources.append(source)
            page_labels, page_candidate_count = _accepted_labels(payload)
            accepted_labels.extend(page_labels)
            candidate_count += page_candidate_count
            model_bytes = prepared.authorized_batch.model_visible_canonical_bytes
            ledger.admit_model_tool_bytes(prepared.run_binding, prepared.authorized_batch, model_bytes)
            admitted = True
            admitted_bytes += len(model_bytes)
            prepared_searches.append(prepared)
            token = _continuation(payload)
            if (
                token is None
                or candidate_count >= 100
                or admitted_bytes > ARCHIVE_AUTHORITY_MAX_MODEL_BYTES - 7_900
            ):
                break
            request = replace(probe.request, continuation=token)
        if not prepared_searches:
            raise ParityHarnessError("archive parity search emitted no page")
        ledger.freeze_for_publication()
        with storage.transaction() as conn:
            authority_context = refresh_archive_search_reauthorization_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=_TENANT,
                principal_id=_PRINCIPAL,
                prepared_searches=tuple(prepared_searches),
            )
        answer = " ".join(f"[{label}]" for label in accepted_labels)
        if not answer:
            answer = "No accepted factual candidate."
        attestation_attempted = True
        attestation = attest_archive_search_before_publication(
            tenant_id=_TENANT,
            principal_id=_PRINCIPAL,
            ledger=ledger,
            answer=answer,
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=authority_context,
        )
        return tuple(discovered_sources), attestation.candidate_projection.candidates
    except Exception as exc:
        if not attestation_attempted:
            try:
                if admitted:
                    consume_archive_model_batch_ledger_fail_closed(ledger)
                else:
                    abandon_empty_archive_model_batch_ledger(ledger)
            except Exception:
                pass
        if isinstance(exc, ParityHarnessError):
            raise
        raise ParityHarnessError("archive parity path failed") from exc


def _opaque_sources(
    sources: Iterable[SourceRef],
    *,
    key: bytes,
) -> tuple[str, ...]:
    identities: list[str] = []
    for source in sources:
        identity = opaque_source_identity(source, key)
        if identity not in identities:
            identities.append(identity)
    return tuple(identities)


def _publication_identities(
    entries: tuple[ArchiveSearchCandidateProjectionEntry, ...],
    *,
    probe: _ParityProbe,
) -> tuple[str, ...]:
    if any(type(item) is not ArchiveSearchCandidateProjectionEntry for item in entries):
        raise ParityHarnessError("archive parity projection is invalid")
    return _opaque_sources((item.source_ref for item in entries), key=probe.privacy_key)


def _tool_data(result: ToolResult, *, adapter: str) -> Mapping[str, object]:
    if type(result) is not ToolResult or result.success is not True or not isinstance(result.data, Mapping):
        raise ParityHarnessError(f"{adapter} parity adapter failed")
    return cast(Mapping[str, object], result.data)


async def _adapter_identities(
    kernel: ExecutionKernel,
    actor: ActorContext,
    probe: _ParityProbe,
) -> tuple[str, ...]:
    arguments: dict[str, Any] = {"query": probe.request.query, "limit": 20}
    if probe.adapter == "message_search":
        arguments["before_message_id"] = _BOUNDARY_MESSAGE_ID
    if probe.request.focus:
        if probe.adapter != "source_search" or probe.request.corpora != (ArchiveSearchCorpus.DOCUMENTS,):
            raise ParityHarnessError("focused source parity probe is invalid")
        arguments["focus"] = probe.request.focus
    # The legacy adapters remain dialogue-visible until measured cutover, but
    # parity is a code-owned caller and must exercise their durable internal
    # surface explicitly.  Relying on the dialogue default would let a later
    # catalog-only retirement silently break the benchmark.
    result = await kernel.execute(
        probe.adapter,
        arguments,
        actor=actor,
        execution_scope="internal",
    )
    payload = _tool_data(result, adapter=probe.adapter)
    if probe.adapter == "source_search":
        snapshots = source_search_page_snapshots(result.data)
        if snapshots is None:
            raise ParityHarnessError("source parity adapter lost its private identities")
        return _opaque_sources(
            (_document_source(item.raw_id) for item in snapshots),
            key=probe.privacy_key,
        )
    raw_results = payload.get("results")
    if type(raw_results) is not list:
        raise ParityHarnessError(f"{probe.adapter} parity results are invalid")
    if probe.adapter == "memory_search":
        knowledge_to_raw = {_KNOWLEDGE_ID: _RAW_KNOWLEDGE_ID}
        sources: list[SourceRef] = []
        for raw_result in raw_results:
            if type(raw_result) is not dict:
                raise ParityHarnessError("memory parity result is invalid")
            knowledge_id = raw_result.get("id")
            raw_id = knowledge_to_raw.get(cast(str, knowledge_id))
            if raw_id is None:
                raise ParityHarnessError("memory parity result has no code-owned source mapping")
            sources.append(_document_source(raw_id))
        return _opaque_sources(sources, key=probe.privacy_key)
    sources = []
    for raw_result in raw_results:
        if type(raw_result) is not dict:
            raise ParityHarnessError("message parity result is invalid")
        conversation_id = raw_result.get("conversation_id")
        if type(conversation_id) is not str:
            raise ParityHarnessError("message parity result has no source identity")
        sources.append(_message_source(conversation_id))
    return _opaque_sources(sources, key=probe.privacy_key)


def _rank(identity: str, values: tuple[str, ...]) -> int | None:
    return values.index(identity) + 1 if identity in values else None


def _case_result(
    probe: _ParityProbe,
    archive_sources: tuple[str, ...],
    archive_publication_sources: tuple[str, ...],
    adapter_sources: tuple[str, ...],
) -> ParityCaseResultV1:
    same_membership = set(archive_sources) == set(adapter_sources)
    status: Literal["mismatch", "parity"] = "parity" if same_membership else "mismatch"
    order_status: Literal["mismatch", "not_comparable", "parity"] = (
        "parity"
        if archive_sources == adapter_sources
        else "mismatch"
        if same_membership
        else "not_comparable"
    )
    expected = probe.expected_source_identity
    reason = None
    if status == "mismatch":
        missing_from_archive = set(adapter_sources) - set(archive_sources)
        extra_in_archive = set(archive_sources) - set(adapter_sources)
        reason = (
            probe.mismatch_reason
            if probe.mismatch_reason is not None
            and missing_from_archive == {expected}
            and not extra_in_archive
            else _UNCLASSIFIED_MISMATCH_REASON
        )
    return ParityCaseResultV1(
        case_id=probe.opaque_case_id,
        case_sha256=probe.case_sha256,
        expected_corpus=probe.request.corpora[0],
        adapter=probe.adapter,
        expected_source_identity=expected,
        archive_source_identities=archive_sources,
        archive_publication_source_identities=archive_publication_sources,
        adapter_source_identities=adapter_sources,
        archive_expected_rank=_rank(expected, archive_sources),
        adapter_expected_rank=_rank(expected, adapter_sources),
        membership_status=status,
        order_status=order_status,
        reason_code=reason,
    )


def _dimensions(
    cases: tuple[ParityCaseResultV1, ...],
    *,
    focused_source_case_ids: frozenset[str],
) -> tuple[ParityDimensionV1, ...]:
    membership_mismatches = tuple(item for item in cases if item.membership_status == "mismatch")
    membership_reasons = tuple(sorted({cast(str, item.reason_code) for item in membership_mismatches}))
    order_cases = tuple(item for item in cases if item.order_status != "not_comparable")
    order_mismatches = sum(item.order_status == "mismatch" for item in order_cases)
    order_status: _Status
    order_reasons: tuple[str, ...]
    if not order_cases:
        order_status = "unsupported"
        order_reasons = ("no_shared_candidate_order",)
    elif order_mismatches:
        order_status = "mismatch"
        order_reasons = ("candidate_order_mismatch",)
    else:
        order_status = "parity"
        order_reasons = ()
    focused_source_cases = tuple(item for item in cases if item.case_id in focused_source_case_ids)
    if len(focused_source_cases) != 1 or len(focused_source_case_ids) != 1:
        raise ParityHarnessError("focused source parity matrix is invalid")
    focused_source_mismatches = tuple(
        item
        for item in focused_source_cases
        if item.membership_status == "mismatch" or item.order_status == "mismatch"
    )
    focused_source_reasons = tuple(
        sorted(
            {
                reason
                for item in focused_source_mismatches
                for reason in (
                    cast(str, item.reason_code)
                    if item.membership_status == "mismatch"
                    else "candidate_order_mismatch",
                )
            }
        )
    )
    dimensions = [
        ParityDimensionV1(
            "candidate_membership",
            "mismatch" if membership_mismatches else "parity",
            len(cases),
            len(cases) - len(membership_mismatches),
            len(membership_mismatches),
            membership_reasons,
        ),
        ParityDimensionV1(
            "candidate_order",
            order_status,
            len(order_cases),
            len(order_cases) - order_mismatches,
            order_mismatches,
            order_reasons,
        ),
        ParityDimensionV1(
            "authorization_and_publication",
            "partial",
            0,
            0,
            0,
            ("legacy_adapters_have_no_final_publication_reauthorization",),
        ),
        ParityDimensionV1(
            "coverage_and_absence",
            "partial",
            0,
            0,
            0,
            ("legacy_adapters_do_not_share_typed_archive_coverage",),
        ),
        ParityDimensionV1(
            "focused_source",
            # This closes only focused candidate membership/order for the one
            # synthetic documents probe.  The independent partial dimensions
            # below deliberately keep legacy-adapter retirement unsupported.
            "mismatch" if focused_source_mismatches else "parity",
            len(focused_source_cases),
            len(focused_source_cases) - len(focused_source_mismatches),
            len(focused_source_mismatches),
            focused_source_reasons,
        ),
        ParityDimensionV1(
            "passage_locator",
            "unsupported",
            0,
            0,
            0,
            ("legacy_adapters_do_not_emit_typed_passage_identity",),
        ),
    ]
    dimensions.extend(
        ParityDimensionV1(name, "unsupported", 0, 0, 0, (reason,))
        for name, reason in UNSUPPORTED_REASON_CODES.items()
    )
    return tuple(sorted(dimensions, key=lambda item: item.name))


async def _run_parity_ephemeral() -> ParityReportV1:
    run_number = next(_RUNS)
    probes = _probes()
    try:
        release_sha256 = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise ParityHarnessError("parity release source set is unavailable") from exc
    with tempfile.TemporaryDirectory(prefix="friday-recall-parity-") as directory:
        home = Path(directory) / "home"
        with _isolated_friday_environment(home):
            settings = load_settings()
            ensure_runtime_dirs(settings)
            storage = init_storage(settings)
            try:
                _seed_parity_storage(storage)
                authorization = AuthorizationService(storage, shared_tenant=_TENANT)
                actor = _actor()
                kernel = ExecutionKernel(authorization, settings)
                kernel.bind_services(
                    storage,
                    KnowledgeGraph(storage),
                    cast(Any, None),
                    cast(Any, None),
                )
                results: list[ParityCaseResultV1] = []
                for probe in probes:
                    archive_discovery, archive_entries = _run_archive_probe(
                        storage,
                        authorization,
                        actor,
                        probe,
                        release_sha256=release_sha256,
                        run_number=run_number,
                    )
                    archive_sources = _opaque_sources(
                        archive_discovery,
                        key=probe.privacy_key,
                    )
                    archive_publication_sources = _publication_identities(
                        archive_entries,
                        probe=probe,
                    )
                    adapter_sources = await _adapter_identities(kernel, actor, probe)
                    results.append(
                        _case_result(
                            probe,
                            archive_sources,
                            archive_publication_sources,
                            adapter_sources,
                        )
                    )
                try:
                    current_release_sha256 = archive_search_release_sha256()
                except RecallReleaseIdentityError as exc:
                    raise ParityHarnessError("parity release source set is unavailable") from exc
                if not hmac.compare_digest(current_release_sha256, release_sha256):
                    raise ParityHarnessError("parity release source set changed during the run")
            finally:
                storage.close(final=True)
    cases = tuple(results)
    return ParityReportV1.create(
        release_sha256=release_sha256,
        cases=cases,
        dimensions=_dimensions(
            cases,
            focused_source_case_ids=frozenset(
                probe.opaque_case_id
                for probe in probes
                if probe.adapter == "source_search" and bool(probe.request.focus)
            ),
        ),
    )


def run_parity_ephemeral() -> ParityReportV1:
    """Run the isolated matrix without promoting focused parity into retirement."""

    try:
        return asyncio.run(_run_parity_ephemeral())
    except ParityHarnessError:
        raise
    except Exception as exc:
        raise ParityHarnessError("ephemeral parity path failed") from exc


__all__ = [
    "PARITY_CASE_SCHEMA",
    "PARITY_DIMENSION_SCHEMA",
    "PARITY_REPORT_SCHEMA",
    "UNSUPPORTED_REASON_CODES",
    "ParityCaseResultV1",
    "ParityDimensionV1",
    "ParityHarnessError",
    "ParityReportV1",
    "run_parity_ephemeral",
]
