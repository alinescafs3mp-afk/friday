"""Canonical, body-free contracts for retrieval recall evaluation v1."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import islice
from typing import Final, TypeVar, cast

from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
    ArchiveSearchAcceptedCandidateProjection,
    ArchiveSearchCandidateProjectionEntry,
    ArchiveSearchPublicationAttestation,
    canonical_archive_search_targets,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveSearchWarning,
)
from friday.retrieval.archive_search_service import PreparedArchiveSearch
from friday.retrieval.contracts import (
    AbsenceDecision,
    CoverageState,
    MessageWindowLocator,
    PassageLocatorKind,
    PassageRef,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceRef,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
    TextSpanLocator,
)
from friday.retrieval_benchmark._canonical import (
    MAX_CONTRACT_BYTES,
    RecallContractError,
    bounded_int,
    bounded_optional_int,
    bounded_text,
    canonical_json,
    canonical_manifest_sha256,
    digest_payload,
    exact_object,
    parse_canonical_json,
    sha256_text,
)
from friday.retrieval_benchmark.release import RecallReleaseIdentityError, archive_search_release_sha256

RECALL_CASE_SCHEMA: Final = "friday.retrieval-recall-case.private.v1"
RECALL_OBSERVATION_SCHEMA: Final = "friday.retrieval-recall-observation.body-free.v1"
RECALL_REPORT_SCHEMA: Final = "friday.retrieval-recall-report.body-free.v1"
RECALL_METRIC_SCHEMA: Final = "friday.retrieval-recall-metric.body-free.v1"
MAX_CASES: Final = 10_000
MAX_ALTERNATIVES: Final = 32
MAX_CANDIDATES: Final = 100
MAX_COVERAGES: Final = 64
MAX_BREAKDOWNS: Final = 32
_COUNT_MAX: Final = 1_000_000_000
_METRIC_COUNT_MAX: Final = 1_000_000_000_000_000
_PPM: Final = 1_000_000
_RANK_DISCOUNT_PPM: Final = (
    1_000_000,
    630_930,
    500_000,
    430_677,
    386_853,
    356_207,
    333_333,
    315_465,
    301_030,
    289_065,
)
_RECIPROCAL_RANK_PPM: Final = tuple(_PPM // rank for rank in range(1, 11))
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z")
_CITATION = re.compile(r"A([1-9][0-9]{0,2})\Z")
EnumT = TypeVar("EnumT", bound=StrEnum)
_SHIPPED_OBSERVATION_FACTORY = object()


class RecallTaxonomyV1(StrEnum):
    APPROXIMATE_CONTENT = "approximate_content"
    APPROXIMATE_DATE = "approximate_date"
    OLD_FILE = "old_file"
    PENDING_FILE = "pending_file"
    UNHELPFUL_FILENAME = "unhelpful_filename"
    TYPO_LAYOUT = "typo_layout"
    PERSON_TOPIC = "person_topic"
    TOPIC_MONTH = "topic_month"
    MESSAGE_PARAPHRASE = "message_paraphrase"
    UNKNOWN_CORPUS = "unknown_corpus"


class RecallEvidenceSourceV1(StrEnum):
    SYNTHETIC_EPHEMERAL = "synthetic_ephemeral"
    OWNER_PRIVATE_JSONL = "owner_private_jsonl"


class MetricStatusV1(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_MEASURED = "not_measured"


class RecallOutcomeV1(StrEnum):
    HIT = "hit"
    MISS = "miss"
    EXPECTED_NO_HIT = "expected_no_hit"
    UNCERTAIN_NO_HIT = "uncertain_no_hit"
    FALSE_POSITIVE = "false_positive"


def _enum(kind: type[EnumT], value: object, *, label: str) -> EnumT:
    if not isinstance(value, str):
        raise RecallContractError(f"{label} must be a closed enum value")
    try:
        return kind(value)
    except ValueError as exc:
        raise RecallContractError(f"{label} must be a closed enum value") from exc


def _case_id(value: object) -> str:
    text = bounded_text(value, label="case_id", maximum_bytes=80)
    if _ID.fullmatch(text) is None:
        raise RecallContractError("case_id must be a bounded opaque label")
    return text


def _opaque_case_id(value: object) -> str:
    return sha256_text(value, label="opaque case identity")


def _bounded_tuple(values: Iterable[object], *, maximum: int, label: str) -> tuple[object, ...]:
    try:
        iterator = iter(values)
        result = tuple(islice(iterator, maximum + 1))
    except Exception as exc:
        raise RecallContractError(f"{label} must be a bounded iterable") from exc
    if len(result) > maximum:
        raise RecallContractError(f"{label} exceeds its closed item bound")
    return result


def _privacy_key(value: object) -> bytes:
    return bytes.fromhex(sha256_text(value, label="case privacy key"))


def _keyed_digest(
    domain: bytes,
    payload: Mapping[str, object] | list[object],
    privacy_key: bytes,
) -> str:
    if type(privacy_key) is not bytes or len(privacy_key) != hashlib.sha256().digest_size:
        raise RecallContractError("privacy key must be exactly 32 private bytes")
    return hmac.new(
        privacy_key,
        domain + b"\0" + canonical_json(payload).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def opaque_case_identity(case_id: str, privacy_key: bytes) -> str:
    """Pseudonymize a private owner label before any body-free projection."""

    return _keyed_digest(
        b"friday/retrieval-recall-case-label/v1",
        {"case_id": _case_id(case_id)},
        privacy_key,
    )


def opaque_source_identity(source_ref: SourceRef, privacy_key: bytes) -> str:
    """Project typed source identity to a stable benchmark-only opaque label."""

    if type(source_ref) is not SourceRef:
        raise RecallContractError("source identity requires SourceRef")
    canonical = SourceRef.parse_private(source_ref.to_private_json())
    if type(privacy_key) is not bytes or len(privacy_key) != hashlib.sha256().digest_size:
        raise RecallContractError("privacy key must be exactly 32 private bytes")
    return canonical.logical_digest(privacy_key)


def opaque_passage_window_identity(passage_ref: PassageRef, privacy_key: bytes) -> str:
    """Project an exact typed passage/window to a stable logical qrel identity.

    The shipped keyed digest retains revision and exact span/window identity
    while reports never retain private message IDs, offsets, or revisions.
    """

    if type(passage_ref) is not PassageRef:
        raise RecallContractError("passage identity requires PassageRef")
    canonical = PassageRef.parse_private(passage_ref.to_private_json())
    if type(privacy_key) is not bytes or len(privacy_key) != hashlib.sha256().digest_size:
        raise RecallContractError("privacy key must be exactly 32 private bytes")
    return canonical.passage_digest(privacy_key)


def _locator_kind(passage_ref: PassageRef) -> PassageLocatorKind:
    if type(passage_ref.locator) is TextSpanLocator:
        return PassageLocatorKind.TEXT_SPAN
    if type(passage_ref.locator) is MessageWindowLocator:
        return PassageLocatorKind.MESSAGE_WINDOW
    raise RecallContractError("passage locator does not use the shipped closed contract")


@dataclass(frozen=True, slots=True)
class RecallAlternativeV1:
    source_identity: str
    passage_window_identities: tuple[str, ...]
    locator_kind: PassageLocatorKind
    relevance_grade: int
    temporal_role: TemporalRole | None = None

    def __post_init__(self) -> None:
        sha256_text(self.source_identity, label="alternative source identity")
        if (
            type(self.passage_window_identities) is not tuple
            or not 1 <= len(self.passage_window_identities) <= 8
        ):
            raise RecallContractError("alternative passages exceed the closed item bound")
        for identity in self.passage_window_identities:
            sha256_text(identity, label="alternative passage/window identity")
        if self.passage_window_identities != tuple(sorted(self.passage_window_identities)) or len(
            self.passage_window_identities
        ) != len(set(self.passage_window_identities)):
            raise RecallContractError("alternative passage/window identities must be sorted and unique")
        if type(self.locator_kind) is not PassageLocatorKind:
            raise RecallContractError("alternative locator kind must be typed")
        bounded_int(self.relevance_grade, label="relevance grade", minimum=1, maximum=3)
        if self.temporal_role is not None and type(self.temporal_role) is not TemporalRole:
            raise RecallContractError("alternative temporal role must be typed")

    def to_payload(self) -> dict[str, object]:
        return {
            "locator_kind": self.locator_kind.value,
            "passage_window_identities": list(self.passage_window_identities),
            "relevance_grade": self.relevance_grade,
            "source_identity": self.source_identity,
            "temporal_role": self.temporal_role.value if self.temporal_role is not None else None,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallAlternativeV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "locator_kind",
                    "passage_window_identities",
                    "relevance_grade",
                    "source_identity",
                    "temporal_role",
                }
            ),
            label="recall alternative",
        )
        raw_passages = payload["passage_window_identities"]
        if type(raw_passages) is not list:
            raise RecallContractError("alternative passages must be an array")
        temporal = payload["temporal_role"]
        return cls(
            source_identity=sha256_text(payload["source_identity"], label="alternative source identity"),
            passage_window_identities=tuple(
                sha256_text(item, label="alternative passage/window identity") for item in raw_passages
            ),
            locator_kind=_enum(
                PassageLocatorKind,
                payload["locator_kind"],
                label="passage locator kind",
            ),
            relevance_grade=bounded_int(
                payload["relevance_grade"], label="relevance grade", minimum=1, maximum=3
            ),
            temporal_role=(
                None if temporal is None else _enum(TemporalRole, temporal, label="alternative temporal role")
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class RecallCaseV1:
    case_id: str
    privacy_key_hex: str
    taxonomy: RecallTaxonomyV1
    evidence_source: RecallEvidenceSourceV1
    request: ArchiveSearchRequest
    expected_corpus: ArchiveSearchCorpus
    alternatives: tuple[RecallAlternativeV1, ...]
    expected_no_hit: bool

    def __post_init__(self) -> None:
        _case_id(self.case_id)
        _privacy_key(self.privacy_key_hex)
        if type(self.taxonomy) is not RecallTaxonomyV1:
            raise RecallContractError("case taxonomy must use the exact v1 taxonomy")
        if type(self.evidence_source) is not RecallEvidenceSourceV1:
            raise RecallContractError("case evidence source must be explicit")
        if type(self.request) is not ArchiveSearchRequest:
            raise RecallContractError("case request must reuse ArchiveSearchRequest")
        ArchiveSearchRequest.parse_private(self.request.to_private_json())
        if self.request.continuation is not None:
            raise RecallContractError("recall cases must describe initial archive intent")
        if type(self.expected_corpus) is not ArchiveSearchCorpus:
            raise RecallContractError("expected corpus must use ArchiveSearchCorpus")
        if self.expected_corpus not in self.request.corpora:
            raise RecallContractError("expected corpus was not requested")
        if type(self.alternatives) is not tuple or len(self.alternatives) > MAX_ALTERNATIVES:
            raise RecallContractError("case alternatives exceed the closed item bound")
        if any(type(item) is not RecallAlternativeV1 for item in self.alternatives):
            raise RecallContractError("case alternatives must be typed")
        identities = tuple(item.source_identity for item in self.alternatives)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise RecallContractError("alternative sources must be sorted and unique")
        passage_identities = tuple(
            identity for item in self.alternatives for identity in item.passage_window_identities
        )
        if len(passage_identities) != len(set(passage_identities)):
            raise RecallContractError("alternative passage/window identities collide")
        if type(self.expected_no_hit) is not bool:
            raise RecallContractError("expected_no_hit must be boolean")
        if self.expected_no_hit == bool(self.alternatives):
            raise RecallContractError("expected hit cases need alternatives; no-hit cases forbid them")
        allowed_roles = {
            item.role for item in self.request.temporal_constraints if item.corpus is self.expected_corpus
        }
        if self.expected_corpus is ArchiveSearchCorpus.MESSAGES:
            allowed_roles.add(TemporalRole.CONVERSATION_TIME)
        if any(
            item.temporal_role is not None and item.temporal_role not in allowed_roles
            for item in self.alternatives
        ):
            raise RecallContractError("expected temporal role is not supported by the archive request")
        if len(self.to_json().encode("ascii")) > MAX_CONTRACT_BYTES:
            raise RecallContractError("recall case exceeds its byte bound")

    def __repr__(self) -> str:
        return "RecallCaseV1(private_case_id=True, private_request=True)"

    def to_payload(self) -> dict[str, object]:
        return {
            "alternatives": [item.to_payload() for item in self.alternatives],
            "case_id": self.case_id,
            "evidence_source": self.evidence_source.value,
            "expected_corpus": self.expected_corpus.value,
            "expected_no_hit": self.expected_no_hit,
            "privacy_key_hex": self.privacy_key_hex,
            "request": self.request.to_private_payload(),
            "schema": RECALL_CASE_SCHEMA,
            "taxonomy": self.taxonomy.value,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @property
    def canonical_sha256(self) -> str:
        return _keyed_digest(
            b"friday/retrieval-recall-case/v1",
            self.to_payload(),
            _privacy_key(self.privacy_key_hex),
        )

    @property
    def opaque_case_id(self) -> str:
        return opaque_case_identity(self.case_id, _privacy_key(self.privacy_key_hex))

    @classmethod
    def from_payload(cls, value: object) -> RecallCaseV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "alternatives",
                    "case_id",
                    "evidence_source",
                    "expected_corpus",
                    "expected_no_hit",
                    "privacy_key_hex",
                    "request",
                    "schema",
                    "taxonomy",
                }
            ),
            label="recall case",
        )
        if payload["schema"] != RECALL_CASE_SCHEMA:
            raise RecallContractError("recall case schema is unsupported")
        alternatives = payload["alternatives"]
        if type(alternatives) is not list:
            raise RecallContractError("recall alternatives must be an array")
        expected_no_hit = payload["expected_no_hit"]
        if type(expected_no_hit) is not bool:
            raise RecallContractError("expected_no_hit must be boolean")
        try:
            request = ArchiveSearchRequest.from_private_payload(payload["request"])
        except Exception as exc:
            raise RecallContractError("recall case request is invalid") from exc
        return cls(
            case_id=_case_id(payload["case_id"]),
            privacy_key_hex=sha256_text(payload["privacy_key_hex"], label="case privacy key"),
            taxonomy=_enum(RecallTaxonomyV1, payload["taxonomy"], label="case taxonomy"),
            evidence_source=_enum(
                RecallEvidenceSourceV1,
                payload["evidence_source"],
                label="case evidence source",
            ),
            request=request,
            expected_corpus=_enum(
                ArchiveSearchCorpus,
                payload["expected_corpus"],
                label="expected corpus",
            ),
            alternatives=tuple(RecallAlternativeV1.from_payload(item) for item in alternatives),
            expected_no_hit=expected_no_hit,
        )

    @classmethod
    def parse(cls, value: str | bytes) -> RecallCaseV1:
        result = cls.from_payload(parse_canonical_json(value, label="recall case"))
        text = value.decode("ascii") if type(value) is bytes else value
        if text != result.to_json():
            raise RecallContractError("recall case is not semantically canonical")
        return result


@dataclass(frozen=True, slots=True)
class RecallCandidateV1:
    rank: int
    corpus: ArchiveSearchCorpus
    source_identity: str
    passage_window_identities: tuple[str, ...]
    locator_kinds: tuple[PassageLocatorKind, ...]
    temporal_roles: tuple[TemporalRole, ...]

    def __post_init__(self) -> None:
        bounded_int(self.rank, label="candidate rank", minimum=1, maximum=MAX_CANDIDATES)
        if type(self.corpus) is not ArchiveSearchCorpus:
            raise RecallContractError("candidate corpus must use ArchiveSearchCorpus")
        sha256_text(self.source_identity, label="candidate source identity")
        if (
            type(self.passage_window_identities) is not tuple
            or not 1 <= len(self.passage_window_identities) <= 8
        ):
            raise RecallContractError("candidate passages exceed the closed item bound")
        for identity in self.passage_window_identities:
            sha256_text(identity, label="candidate passage/window identity")
        if self.passage_window_identities != tuple(sorted(self.passage_window_identities)) or len(
            self.passage_window_identities
        ) != len(set(self.passage_window_identities)):
            raise RecallContractError("candidate passage identities must be sorted and unique")
        if (
            type(self.locator_kinds) is not tuple
            or not self.locator_kinds
            or any(type(item) is not PassageLocatorKind for item in self.locator_kinds)
            or self.locator_kinds != tuple(sorted(self.locator_kinds, key=lambda item: item.value))
            or len(self.locator_kinds) != len(set(self.locator_kinds))
        ):
            raise RecallContractError("candidate locator kinds must be sorted unique typed values")
        if (
            type(self.temporal_roles) is not tuple
            or any(type(item) is not TemporalRole for item in self.temporal_roles)
            or self.temporal_roles != tuple(sorted(self.temporal_roles, key=lambda item: item.value))
            or len(self.temporal_roles) != len(set(self.temporal_roles))
        ):
            raise RecallContractError("candidate temporal roles must be sorted unique typed values")

    def to_payload(self) -> dict[str, object]:
        return {
            "corpus": self.corpus.value,
            "locator_kinds": [item.value for item in self.locator_kinds],
            "passage_window_identities": list(self.passage_window_identities),
            "rank": self.rank,
            "source_identity": self.source_identity,
            "temporal_roles": [item.value for item in self.temporal_roles],
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCandidateV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "corpus",
                    "locator_kinds",
                    "passage_window_identities",
                    "rank",
                    "source_identity",
                    "temporal_roles",
                }
            ),
            label="recall candidate",
        )
        passages = payload["passage_window_identities"]
        locators = payload["locator_kinds"]
        temporal = payload["temporal_roles"]
        if any(type(value) is not list for value in (passages, locators, temporal)):
            raise RecallContractError("candidate collections must be arrays")
        passage_values = cast(list[object], passages)
        locator_values = cast(list[object], locators)
        temporal_values = cast(list[object], temporal)
        return cls(
            rank=bounded_int(payload["rank"], label="candidate rank", minimum=1, maximum=100),
            corpus=_enum(ArchiveSearchCorpus, payload["corpus"], label="candidate corpus"),
            source_identity=sha256_text(payload["source_identity"], label="candidate source identity"),
            passage_window_identities=tuple(
                sha256_text(item, label="candidate passage/window identity") for item in passage_values
            ),
            locator_kinds=tuple(
                _enum(PassageLocatorKind, item, label="candidate locator kind") for item in locator_values
            ),
            temporal_roles=tuple(
                _enum(TemporalRole, item, label="candidate temporal role") for item in temporal_values
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallCoverageV1:
    corpus: SearchCorpus
    lane: SearchLane
    states: tuple[CoverageState, ...]
    eligible_authorized: int | None
    examined: int
    matched_at_least: int
    returned: int
    limit: int | None
    next_cursor_available: bool
    authority_rechecked: bool
    snapshot_current: bool

    def __post_init__(self) -> None:
        if type(self.corpus) is not SearchCorpus or type(self.lane) is not SearchLane:
            raise RecallContractError("coverage target must reuse retrieval enums")
        if (
            type(self.states) is not tuple
            or not self.states
            or any(type(item) is not CoverageState for item in self.states)
            or self.states != tuple(sorted(self.states, key=lambda item: item.value))
            or len(self.states) != len(set(self.states))
        ):
            raise RecallContractError("coverage states must be canonical typed values")
        eligible = bounded_optional_int(
            self.eligible_authorized,
            label="coverage eligible_authorized",
            minimum=0,
            maximum=_COUNT_MAX,
        )
        examined = bounded_int(self.examined, label="coverage examined", minimum=0, maximum=_COUNT_MAX)
        matched = bounded_int(
            self.matched_at_least,
            label="coverage matched_at_least",
            minimum=0,
            maximum=_COUNT_MAX,
        )
        returned = bounded_int(self.returned, label="coverage returned", minimum=0, maximum=_COUNT_MAX)
        limit = bounded_optional_int(self.limit, label="coverage limit", minimum=1, maximum=_COUNT_MAX)
        if returned > matched or matched > examined or (eligible is not None and examined > eligible):
            raise RecallContractError("coverage counts are inconsistent")
        if limit is not None and returned > limit:
            raise RecallContractError("coverage returned exceeds limit")
        if any(
            type(item) is not bool
            for item in (
                self.next_cursor_available,
                self.authority_rechecked,
                self.snapshot_current,
            )
        ):
            raise RecallContractError("coverage flags must be booleans")
        if CoverageState.COMPLETE in self.states and (
            self.states != (CoverageState.COMPLETE,)
            or eligible is None
            or examined != eligible
            or self.next_cursor_available
        ):
            raise RecallContractError("complete benchmark coverage is inconsistent")
        if CoverageState.COMPLETE not in self.states and not (
            {CoverageState.PARTIAL, CoverageState.UNAVAILABLE} & set(self.states)
        ):
            raise RecallContractError("incomplete coverage needs a partial/unavailable marker")
        if CoverageState.PARTIAL in self.states and len(self.states) == 1:
            raise RecallContractError("partial coverage needs an explicit reason")
        if CoverageState.EMBEDDING_INCOMPATIBLE in self.states and self.lane is not SearchLane.DENSE:
            raise RecallContractError("embedding incompatibility belongs only to dense coverage")
        if (
            CoverageState.UNAVAILABLE in self.states
            and CoverageState.PARTIAL not in self.states
            and (
                eligible is not None
                or examined != 0
                or matched != 0
                or returned != 0
                or self.next_cursor_available
            )
        ):
            raise RecallContractError("wholly unavailable coverage cannot claim examined material")
        if CoverageState.CAPPED in self.states and (
            CoverageState.PARTIAL not in self.states or limit is None
        ):
            raise RecallContractError("capped coverage must be partial with an explicit limit")
        if self.next_cursor_available and CoverageState.CAPPED not in self.states:
            raise RecallContractError("coverage continuation requires the capped state")

    @classmethod
    def from_search_coverage(cls, coverage: SearchCoverage) -> RecallCoverageV1:
        if type(coverage) is not SearchCoverage:
            raise RecallContractError("coverage projection requires SearchCoverage")
        # Round-trip the shipped contract before discarding its authority-bearing run handle.
        validated = SearchCoverage.from_payload(coverage.to_payload())
        return cls(
            corpus=validated.corpus,
            lane=validated.lane,
            states=validated.states,
            eligible_authorized=validated.eligible_authorized,
            examined=validated.examined,
            matched_at_least=validated.matched_at_least,
            returned=validated.returned,
            limit=validated.limit,
            next_cursor_available=validated.next_cursor_available,
            authority_rechecked=validated.authority_rechecked,
            snapshot_current=validated.snapshot_current,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "authority_rechecked": self.authority_rechecked,
            "corpus": self.corpus.value,
            "eligible_authorized": self.eligible_authorized,
            "examined": self.examined,
            "lane": self.lane.value,
            "limit": self.limit,
            "matched_at_least": self.matched_at_least,
            "next_cursor_available": self.next_cursor_available,
            "returned": self.returned,
            "snapshot_current": self.snapshot_current,
            "states": [item.value for item in self.states],
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCoverageV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "authority_rechecked",
                    "corpus",
                    "eligible_authorized",
                    "examined",
                    "lane",
                    "limit",
                    "matched_at_least",
                    "next_cursor_available",
                    "returned",
                    "snapshot_current",
                    "states",
                }
            ),
            label="recall coverage",
        )
        states = payload["states"]
        if type(states) is not list:
            raise RecallContractError("coverage states must be an array")
        flags = (
            payload["next_cursor_available"],
            payload["authority_rechecked"],
            payload["snapshot_current"],
        )
        if any(type(item) is not bool for item in flags):
            raise RecallContractError("coverage flags must be booleans")
        return cls(
            corpus=_enum(SearchCorpus, payload["corpus"], label="coverage corpus"),
            lane=_enum(SearchLane, payload["lane"], label="coverage lane"),
            states=tuple(_enum(CoverageState, item, label="coverage state") for item in states),
            eligible_authorized=bounded_optional_int(
                payload["eligible_authorized"],
                label="coverage eligible_authorized",
                minimum=0,
                maximum=_COUNT_MAX,
            ),
            examined=bounded_int(
                payload["examined"], label="coverage examined", minimum=0, maximum=_COUNT_MAX
            ),
            matched_at_least=bounded_int(
                payload["matched_at_least"],
                label="coverage matched_at_least",
                minimum=0,
                maximum=_COUNT_MAX,
            ),
            returned=bounded_int(
                payload["returned"], label="coverage returned", minimum=0, maximum=_COUNT_MAX
            ),
            limit=bounded_optional_int(
                payload["limit"], label="coverage limit", minimum=1, maximum=_COUNT_MAX
            ),
            next_cursor_available=cast(bool, flags[0]),
            authority_rechecked=cast(bool, flags[1]),
            snapshot_current=cast(bool, flags[2]),
        )


def _coverage_target_score(coverage: RecallCoverageV1) -> int | None:
    """Project one body-free lane to the exact V1 coverage score fact."""

    if type(coverage) is not RecallCoverageV1:
        raise RecallContractError("coverage score requires a typed target")
    if coverage.eligible_authorized is None:
        return None
    if coverage.eligible_authorized == 0:
        return _PPM
    return coverage.examined * _PPM // coverage.eligible_authorized


_UNCERTAIN_STATES = frozenset(
    {
        CoverageState.PARTIAL,
        CoverageState.UNAVAILABLE,
        CoverageState.STALE,
        CoverageState.PERMISSION_FILTERED,
        CoverageState.BACKFILL_PENDING,
        CoverageState.EMBEDDING_INCOMPATIBLE,
        CoverageState.CAPPED,
    }
)


def coverage_absence_oracle(
    coverages: Iterable[SearchCoverage | RecallCoverageV1],
    *,
    candidate_count: int,
) -> AbsenceDecision:
    """Decide absence without allowing any incomplete lane to claim not-found."""

    values = _bounded_tuple(coverages, maximum=MAX_COVERAGES, label="coverage oracle")
    if any(type(item) not in {SearchCoverage, RecallCoverageV1} for item in values):
        raise RecallContractError("coverage oracle requires typed coverage")
    bounded_int(candidate_count, label="candidate count", minimum=0, maximum=MAX_CANDIDATES)
    if candidate_count:
        return AbsenceDecision.EVIDENCE_FOUND
    if not values:
        return AbsenceDecision.NOT_ESTABLISHED
    projected = tuple(
        RecallCoverageV1.from_search_coverage(item)
        if type(item) is SearchCoverage
        else cast(RecallCoverageV1, item)
        for item in values
    )
    keys = tuple((item.corpus.value, item.lane.value) for item in projected)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise RecallContractError("coverage targets must be sorted and unique")
    if any(
        set(item.states) & _UNCERTAIN_STATES
        or item.states != (CoverageState.COMPLETE,)
        or item.eligible_authorized != item.examined
        or item.matched_at_least != 0
        or item.returned != 0
        or item.next_cursor_available
        or not item.authority_rechecked
        or not item.snapshot_current
        for item in projected
    ):
        return AbsenceDecision.NOT_ESTABLISHED
    return AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED


def _public_count(value: object, *, label: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    return bounded_int(value, label=label, minimum=0, maximum=_COUNT_MAX)


def _public_boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise RecallContractError(f"{label} must be boolean")
    return value


def _page_coverages(
    binding: SearchExecutionBinding,
    payload: Mapping[str, object],
) -> tuple[SearchCoverage, ...]:
    raw_coverage = payload.get("coverage")
    if type(raw_coverage) is not list or payload.get("execution_binding") != binding.to_payload():
        raise RecallContractError("archive page coverage is not bound to its live execution")
    values: list[SearchCoverage] = []
    expected_keys = frozenset(
        {
            "authority_rechecked",
            "corpus",
            "eligible_authorized",
            "examined",
            "lane",
            "limit",
            "matched_at_least",
            "next_cursor_available",
            "returned",
            "schema",
            "snapshot_current",
            "states",
        }
    )
    try:
        for raw_item in raw_coverage:
            if type(raw_item) is not dict or frozenset(raw_item) != expected_keys:
                raise RecallContractError("archive page coverage shape is invalid")
            item = cast(dict[str, object], raw_item)
            raw_states = item["states"]
            if type(raw_states) is not list:
                raise RecallContractError("archive page coverage states are invalid")
            values.append(
                SearchCoverage.create(
                    corpus=SearchCorpus(cast(str, item["corpus"])),
                    lane=SearchLane(cast(str, item["lane"])),
                    execution_binding=binding,
                    states=(CoverageState(cast(str, state)) for state in raw_states),
                    eligible_authorized=_public_count(
                        item["eligible_authorized"],
                        label="eligible_authorized",
                        optional=True,
                    ),
                    examined=cast(int, _public_count(item["examined"], label="examined")),
                    matched_at_least=cast(
                        int,
                        _public_count(item["matched_at_least"], label="matched_at_least"),
                    ),
                    returned=cast(int, _public_count(item["returned"], label="returned")),
                    authority_rechecked=_public_boolean(
                        item["authority_rechecked"],
                        label="authority_rechecked",
                    ),
                    snapshot_current=_public_boolean(
                        item["snapshot_current"],
                        label="snapshot_current",
                    ),
                    limit=_public_count(item["limit"], label="limit", optional=True),
                    next_cursor_available=_public_boolean(
                        item["next_cursor_available"],
                        label="next_cursor_available",
                    ),
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, RecallContractError):
            raise
        raise RecallContractError("archive page coverage contract is invalid") from exc
    result = tuple(values)
    keys = tuple((item.corpus.value, item.lane.value) for item in result)
    if not result or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise RecallContractError("archive page coverage targets are not canonical")
    return result


def _canonical_temporal_bound(value: object, *, instant: bool, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        raise RecallContractError("archive temporal bound is invalid")
    try:
        if instant:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            canonical = parsed.astimezone(UTC).isoformat()
        else:
            canonical = date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise RecallContractError("archive temporal bound is invalid") from exc
    if canonical != value:
        raise RecallContractError("archive temporal bound is not canonical")
    return value


def _temporal_fact_role(
    case: RecallCaseV1,
    corpus: ArchiveSearchCorpus,
    value: object,
) -> TemporalRole | None:
    payload = exact_object(
        value,
        frozenset({"end", "origin", "precision", "role", "start", "value_kind"}),
        label="archive public temporal fact",
    )
    role = _enum(TemporalRole, payload["role"], label="archive temporal role")
    origin = _enum(TemporalOrigin, payload["origin"], label="archive temporal origin")
    precision = _enum(TemporalPrecision, payload["precision"], label="archive temporal precision")
    value_kind = _enum(TemporalValueKind, payload["value_kind"], label="archive temporal value kind")
    instant = value_kind is TemporalValueKind.INSTANT
    start = _canonical_temporal_bound(payload["start"], instant=instant)
    end = _canonical_temporal_bound(payload["end"], instant=instant, optional=instant)
    if instant:
        if precision is not TemporalPrecision.INSTANT or end is not None:
            raise RecallContractError("archive temporal instant shape is invalid")
    elif precision is TemporalPrecision.INSTANT or end is None or cast(str, start) >= end:
        raise RecallContractError("archive temporal interval shape is invalid")
    expected_origin = {
        TemporalRole.RECEIVED_AT: TemporalOrigin.STORAGE_COLUMN,
        TemporalRole.UPLOADED_AT: TemporalOrigin.STORAGE_COLUMN,
        TemporalRole.CONVERSATION_TIME: TemporalOrigin.STORAGE_COLUMN,
        TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT: TemporalOrigin.KNOWLEDGE_PROJECTION,
        TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT: TemporalOrigin.KNOWLEDGE_PROJECTION,
    }.get(role)
    if expected_origin is not None and origin is not expected_origin:
        return None
    if role is TemporalRole.CONVERSATION_TIME and corpus is ArchiveSearchCorpus.MESSAGES:
        return role if instant else None
    constraints = tuple(
        item for item in case.request.temporal_constraints if item.corpus is corpus and item.role is role
    )
    for constraint in constraints:
        if constraint.value_kind is not value_kind:
            continue
        if instant:
            if constraint.start <= cast(str, start) < constraint.end:
                return role
        elif cast(str, start) < constraint.end and cast(str, end) > constraint.start:
            return role
    return None


def _page_temporal_roles(
    case: RecallCaseV1,
    payload: Mapping[str, object],
) -> tuple[dict[str, tuple[TemporalRole, ...]], tuple[str, ...], int]:
    raw_candidates = payload.get("candidates")
    if type(raw_candidates) is not list:
        raise RecallContractError("archive public candidates are invalid")
    roles: dict[str, tuple[TemporalRole, ...]] = {}
    accepted_labels: list[str] = []
    for raw_candidate in raw_candidates:
        if type(raw_candidate) is not dict:
            raise RecallContractError("archive public candidate is invalid")
        candidate = cast(dict[str, object], raw_candidate)
        label = candidate.get("label")
        raw_facts = candidate.get("temporal_facts")
        raw_passages = candidate.get("passages")
        navigation_only = candidate.get("navigation_only")
        try:
            corpus = ArchiveSearchCorpus(cast(str, candidate.get("corpus")))
        except (TypeError, ValueError) as exc:
            raise RecallContractError("archive public candidate corpus is invalid") from exc
        if (
            type(label) is not str
            or _CITATION.fullmatch(label) is None
            or type(raw_facts) is not list
            or type(raw_passages) is not list
            or type(navigation_only) is not bool
        ):
            raise RecallContractError("archive public candidate projection is invalid")
        matched = {
            role for fact in raw_facts if (role := _temporal_fact_role(case, corpus, fact)) is not None
        }
        if label in roles:
            raise RecallContractError("archive public candidate labels collide")
        roles[label] = tuple(sorted(matched, key=lambda item: item.value))
        if (
            candidate.get("evidence_authority") == ArchiveEvidenceAuthority.CANONICAL.value
            and navigation_only is False
            and bool(raw_passages)
        ):
            accepted_labels.append(label)
    return roles, tuple(accepted_labels), len(raw_candidates)


def _attested_archive_inputs(
    case: RecallCaseV1,
    release_sha256: str,
    attestation: ArchiveSearchPublicationAttestation,
    prepared_searches: Iterable[PreparedArchiveSearch],
) -> tuple[
    ArchiveSearchAcceptedCandidateProjection,
    tuple[SearchCoverage, ...],
    dict[str, tuple[TemporalRole, ...]],
]:
    if type(attestation) is not ArchiveSearchPublicationAttestation:
        raise RecallContractError("shipped observation requires a sealed phase-2 attestation")
    raw_prepared = _bounded_tuple(
        prepared_searches,
        maximum=ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
        label="prepared archive pages",
    )
    if not raw_prepared or any(type(item) is not PreparedArchiveSearch for item in raw_prepared):
        raise RecallContractError("shipped observation requires sealed prepared archive pages")
    pages = cast(tuple[PreparedArchiveSearch, ...], raw_prepared)
    expected_release = sha256_text(release_sha256, label="observation release digest")
    try:
        current_release = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise RecallContractError("current archive release identity is unavailable") from exc
    if not hmac.compare_digest(expected_release, current_release):
        raise RecallContractError("observation release does not match the current shipped source")
    coverage_records: list[object] = []
    roles: dict[str, tuple[TemporalRole, ...]] = {}
    accepted_labels: list[str] = []
    candidate_count = 0
    page_coverages: list[tuple[SearchCoverage, ...]] = []
    try:
        for page_index, prepared in enumerate(pages, 1):
            binding = prepared.run_binding.execution_binding
            if not prepared.attests_origin(case.request, expected_release):
                raise RecallContractError("prepared archive page is bound to another case or release")
            payload = prepared.authorized_batch.public_tool_result_payload
            coverage = _page_coverages(binding, payload)
            page_coverages.append(coverage)
            page_roles, page_accepted_labels, page_candidate_count = _page_temporal_roles(case, payload)
            if set(roles) & set(page_roles):
                raise RecallContractError("archive public candidate labels collide across pages")
            roles.update(page_roles)
            accepted_labels.extend(page_accepted_labels)
            candidate_count += page_candidate_count
            exhaustive = _public_boolean(payload.get("exhaustive"), label="archive exhaustive flag")
            continuation = payload.get("continuation")
            if continuation is not None and type(continuation) is not str:
                raise RecallContractError("archive continuation is invalid")
            raw_warnings = payload.get("warnings")
            if type(raw_warnings) is not list:
                raise RecallContractError("archive warnings are invalid")
            warnings = [ArchiveSearchWarning(cast(str, item)).value for item in raw_warnings]
            if warnings != raw_warnings:
                raise RecallContractError("archive warnings are not canonical")
            coverage_records.append(
                {
                    "coverage_sha256": [
                        hashlib.sha256(item.to_json().encode("ascii")).hexdigest() for item in coverage
                    ],
                    "exhaustive": exhaustive,
                    "has_continuation": continuation is not None,
                    "model_page_index": page_index,
                    "warnings": warnings,
                }
            )
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError("sealed archive evidence could not be projected") from exc
    request_identity = hashlib.sha256(case.request.identity_digest_material()).hexdigest()
    expected_plan_sha256 = hashlib.sha256(canonical_json([request_identity]).encode("ascii")).hexdigest()
    expected_coverage_sha256 = hashlib.sha256(canonical_json(coverage_records).encode("ascii")).hexdigest()
    if not hmac.compare_digest(attestation.plan_sha256, expected_plan_sha256) or not hmac.compare_digest(
        attestation.coverage_sha256,
        expected_coverage_sha256,
    ):
        raise RecallContractError("phase-2 attestation does not bind the exact request pages")
    if attestation.candidate_count != candidate_count:
        raise RecallContractError("phase-2 attestation candidate count contradicts the exact pages")
    projection = attestation.candidate_projection
    projection_labels = tuple(item.public_citation_label for item in projection.candidates)
    if (
        len(accepted_labels) != len(set(accepted_labels))
        or attestation.used_citation_labels != tuple(accepted_labels)
        or projection_labels != tuple(accepted_labels)
    ):
        raise RecallContractError("phase-2 projection omitted or reordered a scoreable candidate")
    return projection, page_coverages[-1], roles


@dataclass(frozen=True, slots=True, repr=False)
class RecallObservationV1:
    case_id: str
    case_sha256: str
    evidence_source: RecallEvidenceSourceV1
    release_sha256: str
    candidates: tuple[RecallCandidateV1, ...]
    coverage: tuple[RecallCoverageV1, ...]
    absence_decision: AbsenceDecision
    observation_sha256: str
    _provenance: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _opaque_case_id(self.case_id)
        sha256_text(self.case_sha256, label="observation case digest")
        if type(self.evidence_source) is not RecallEvidenceSourceV1:
            raise RecallContractError("observation evidence source must be explicit")
        sha256_text(self.release_sha256, label="observation release digest")
        if type(self.candidates) is not tuple or len(self.candidates) > MAX_CANDIDATES:
            raise RecallContractError("observation candidates exceed the closed item bound")
        if any(type(item) is not RecallCandidateV1 for item in self.candidates):
            raise RecallContractError("observation candidates must be typed")
        ranks = tuple(item.rank for item in self.candidates)
        if ranks != tuple(sorted(ranks)) or len(ranks) != len(set(ranks)):
            raise RecallContractError("candidate ranks must be strictly increasing")
        identities = tuple(
            (item.rank, item.source_identity, item.passage_window_identities) for item in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise RecallContractError("candidate identities collide")
        passage_sources: dict[str, str] = {}
        for candidate in self.candidates:
            for passage_identity in candidate.passage_window_identities:
                previous = passage_sources.setdefault(passage_identity, candidate.source_identity)
                if previous != candidate.source_identity:
                    raise RecallContractError("candidate passage identity collides across sources")
        if type(self.coverage) is not tuple or not 1 <= len(self.coverage) <= MAX_COVERAGES:
            raise RecallContractError("observation coverage exceeds the closed item bound")
        if any(type(item) is not RecallCoverageV1 for item in self.coverage):
            raise RecallContractError("observation coverage must be typed")
        coverage_keys = tuple((item.corpus.value, item.lane.value) for item in self.coverage)
        if coverage_keys != tuple(sorted(coverage_keys)) or len(coverage_keys) != len(set(coverage_keys)):
            raise RecallContractError("observation coverage must be sorted with unique targets")
        if type(self.absence_decision) is not AbsenceDecision:
            raise RecallContractError("observation absence must reuse AbsenceDecision")
        expected_absence = coverage_absence_oracle(self.coverage, candidate_count=len(self.candidates))
        if self.absence_decision is not expected_absence:
            raise RecallContractError("observation absence contradicts the closed coverage oracle")
        sha256_text(self.observation_sha256, label="observation digest")
        if not hmac.compare_digest(self.observation_sha256, self._computed_sha256()):
            raise RecallContractError("observation digest is forged")
        if len(self.to_json().encode("ascii")) > MAX_CONTRACT_BYTES:
            raise RecallContractError("recall observation exceeds its byte bound")

    def __repr__(self) -> str:
        return f"RecallObservationV1(case_id={self.case_id!r}, body_free=True)"

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "absence_decision": self.absence_decision.value,
            "candidates": [item.to_payload() for item in self.candidates],
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "coverage": [item.to_payload() for item in self.coverage],
            "evidence_source": self.evidence_source.value,
            "release_sha256": self.release_sha256,
            "schema": RECALL_OBSERVATION_SCHEMA,
        }

    def _computed_sha256(self) -> str:
        return digest_payload(
            b"friday/retrieval-recall-observation/v1",
            self._payload_without_digest(),
        )

    @classmethod
    def create(
        cls,
        *,
        case: RecallCaseV1,
        release_sha256: str,
        candidates: Iterable[RecallCandidateV1],
        coverage: Iterable[RecallCoverageV1],
        _factory: object = None,
    ) -> RecallObservationV1:
        shipped = _factory is _SHIPPED_OBSERVATION_FACTORY
        if shipped != (case.evidence_source is RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL):
            raise RecallContractError(
                "synthetic shipped evidence requires the real archive projection factory"
            )
        raw_candidates = _bounded_tuple(
            candidates,
            maximum=MAX_CANDIDATES,
            label="observation candidates",
        )
        raw_coverage = _bounded_tuple(
            coverage,
            maximum=MAX_COVERAGES,
            label="observation coverage",
        )
        if any(type(item) is not RecallCandidateV1 for item in raw_candidates) or any(
            type(item) is not RecallCoverageV1 for item in raw_coverage
        ):
            raise RecallContractError("observation factory requires typed candidates and coverage")
        candidate_values = tuple(
            sorted(cast(tuple[RecallCandidateV1, ...], raw_candidates), key=lambda item: item.rank)
        )
        coverage_values = tuple(
            sorted(
                cast(tuple[RecallCoverageV1, ...], raw_coverage),
                key=lambda item: (item.corpus.value, item.lane.value),
            )
        )
        expected_targets = tuple(
            (corpus.value, lane.value) for corpus, lane in canonical_archive_search_targets(case.request)
        )
        observed_targets = tuple((item.corpus.value, item.lane.value) for item in coverage_values)
        if observed_targets != expected_targets:
            raise RecallContractError("observation coverage does not match the canonical archive plan")
        base = {
            "absence_decision": coverage_absence_oracle(
                coverage_values,
                candidate_count=len(candidate_values),
            ).value,
            "candidates": [item.to_payload() for item in candidate_values],
            "case_id": case.opaque_case_id,
            "case_sha256": case.canonical_sha256,
            "coverage": [item.to_payload() for item in coverage_values],
            "evidence_source": case.evidence_source.value,
            "release_sha256": sha256_text(release_sha256, label="observation release digest"),
            "schema": RECALL_OBSERVATION_SCHEMA,
        }
        result = cls(
            case_id=case.opaque_case_id,
            case_sha256=case.canonical_sha256,
            evidence_source=case.evidence_source,
            release_sha256=cast(str, base["release_sha256"]),
            candidates=candidate_values,
            coverage=coverage_values,
            absence_decision=AbsenceDecision(cast(str, base["absence_decision"])),
            observation_sha256=digest_payload(
                b"friday/retrieval-recall-observation/v1",
                base,
            ),
        )
        result.validate_case_binding(case)
        if shipped:
            object.__setattr__(result, "_provenance", _SHIPPED_OBSERVATION_FACTORY)
        return result

    @classmethod
    def from_archive_attestation(
        cls,
        *,
        case: RecallCaseV1,
        release_sha256: str,
        attestation: ArchiveSearchPublicationAttestation,
        prepared_searches: Iterable[PreparedArchiveSearch],
    ) -> RecallObservationV1:
        """Build a body-free observation from request-bound shipped phase-2 evidence."""

        projection, coverages, role_map = _attested_archive_inputs(
            case,
            release_sha256,
            attestation,
            prepared_searches,
        )
        candidates: list[RecallCandidateV1] = []
        entries = _bounded_tuple(
            projection.candidates,
            maximum=MAX_CANDIDATES,
            label="archive candidate projection",
        )
        if any(type(entry) is not ArchiveSearchCandidateProjectionEntry for entry in entries):
            raise RecallContractError("archive projection entry is not typed")
        for entry in cast(tuple[ArchiveSearchCandidateProjectionEntry, ...], entries):
            match = _CITATION.fullmatch(entry.public_citation_label)
            if match is None:
                raise RecallContractError("archive projection citation label is invalid")
            rank = bounded_int(int(match.group(1)), label="candidate rank", minimum=1, maximum=100)
            source_ref = SourceRef.parse_private(entry.source_ref.to_private_json())
            passage_refs = tuple(
                PassageRef.parse_private(item.to_private_json()) for item in entry.passage_refs
            )
            roles = tuple(sorted(role_map.get(entry.public_citation_label, ()), key=lambda item: item.value))
            candidates.append(
                RecallCandidateV1(
                    rank=rank,
                    corpus=entry.corpus,
                    source_identity=opaque_source_identity(
                        source_ref,
                        _privacy_key(case.privacy_key_hex),
                    ),
                    passage_window_identities=tuple(
                        sorted(
                            {
                                opaque_passage_window_identity(
                                    item,
                                    _privacy_key(case.privacy_key_hex),
                                )
                                for item in passage_refs
                            }
                        )
                    ),
                    locator_kinds=tuple(
                        sorted({_locator_kind(item) for item in passage_refs}, key=lambda item: item.value)
                    ),
                    temporal_roles=roles,
                )
            )
        return cls.create(
            case=case,
            release_sha256=release_sha256,
            candidates=candidates,
            coverage=(RecallCoverageV1.from_search_coverage(item) for item in coverages),
            _factory=_SHIPPED_OBSERVATION_FACTORY,
        )

    def attests_shipped_projection(self) -> bool:
        """Return true only for an in-process real phase-2 projection."""

        return self._provenance is _SHIPPED_OBSERVATION_FACTORY

    def validate_case_binding(self, case: RecallCaseV1) -> None:
        """Recheck every serialized observation fact against its private case."""

        if type(case) is not RecallCaseV1:
            raise RecallContractError("observation binding requires a typed recall case")
        if (
            self.case_id != case.opaque_case_id
            or self.case_sha256 != case.canonical_sha256
            or self.evidence_source is not case.evidence_source
        ):
            raise RecallContractError("observation is bound to a different private case")
        expected_targets = tuple(
            (corpus.value, lane.value) for corpus, lane in canonical_archive_search_targets(case.request)
        )
        observed_targets = tuple((item.corpus.value, item.lane.value) for item in self.coverage)
        if observed_targets != expected_targets:
            raise RecallContractError("observation coverage does not match the canonical archive plan")
        roles_by_corpus: dict[ArchiveSearchCorpus, set[TemporalRole]] = {
            corpus: set() for corpus in case.request.corpora
        }
        for constraint in case.request.temporal_constraints:
            roles_by_corpus[constraint.corpus].add(constraint.role)
        if ArchiveSearchCorpus.MESSAGES in roles_by_corpus:
            roles_by_corpus[ArchiveSearchCorpus.MESSAGES].add(TemporalRole.CONVERSATION_TIME)
        for candidate in self.candidates:
            allowed = roles_by_corpus.get(candidate.corpus)
            if allowed is None or not set(candidate.temporal_roles) <= allowed:
                raise RecallContractError("candidate temporal roles escape the private archive request")

    def to_payload(self) -> dict[str, object]:
        return {**self._payload_without_digest(), "observation_sha256": self.observation_sha256}

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, value: object) -> RecallObservationV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "absence_decision",
                    "candidates",
                    "case_id",
                    "case_sha256",
                    "coverage",
                    "evidence_source",
                    "observation_sha256",
                    "release_sha256",
                    "schema",
                }
            ),
            label="recall observation",
        )
        if payload["schema"] != RECALL_OBSERVATION_SCHEMA:
            raise RecallContractError("recall observation schema is unsupported")
        raw_candidates = payload["candidates"]
        raw_coverage = payload["coverage"]
        if type(raw_candidates) is not list or type(raw_coverage) is not list:
            raise RecallContractError("observation collections must be arrays")
        return cls(
            case_id=_opaque_case_id(payload["case_id"]),
            case_sha256=sha256_text(payload["case_sha256"], label="observation case digest"),
            evidence_source=_enum(
                RecallEvidenceSourceV1,
                payload["evidence_source"],
                label="observation evidence source",
            ),
            release_sha256=sha256_text(payload["release_sha256"], label="observation release digest"),
            candidates=tuple(RecallCandidateV1.from_payload(item) for item in raw_candidates),
            coverage=tuple(RecallCoverageV1.from_payload(item) for item in raw_coverage),
            absence_decision=_enum(
                AbsenceDecision,
                payload["absence_decision"],
                label="observation absence",
            ),
            observation_sha256=sha256_text(payload["observation_sha256"], label="observation digest"),
        )

    @classmethod
    def parse(cls, value: str | bytes) -> RecallObservationV1:
        result = cls.from_payload(parse_canonical_json(value, label="recall observation"))
        text = value.decode("ascii") if type(value) is bytes else value
        if text != result.to_json():
            raise RecallContractError("recall observation is not semantically canonical")
        return result


@dataclass(frozen=True, slots=True)
class MetricValueV1:
    status: MetricStatusV1
    numerator: int | None
    denominator: int | None
    value_ppm: int | None

    def __post_init__(self) -> None:
        if type(self.status) is not MetricStatusV1:
            raise RecallContractError("metric status must be typed")
        values = (self.numerator, self.denominator, self.value_ppm)
        if self.status is MetricStatusV1.AVAILABLE:
            if any(type(item) is not int for item in values):
                raise RecallContractError("available metric requires integer fields")
            assert self.numerator is not None and self.denominator is not None
            assert self.value_ppm is not None
            bounded_int(
                self.numerator,
                label="metric numerator",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            )
            bounded_int(
                self.denominator,
                label="metric denominator",
                minimum=1,
                maximum=_METRIC_COUNT_MAX,
            )
            bounded_int(self.value_ppm, label="metric ppm", minimum=0, maximum=1_000_000)
            if self.numerator > self.denominator:
                raise RecallContractError("metric numerator exceeds denominator")
            if self.value_ppm != self.numerator * 1_000_000 // self.denominator:
                raise RecallContractError("metric ppm is forged")
        elif values != (None, None, None):
            raise RecallContractError("unsupported/unmeasured metrics carry no invented value")

    @classmethod
    def ratio(cls, numerator: int, denominator: int) -> MetricValueV1:
        bounded_int(
            numerator,
            label="metric numerator",
            minimum=0,
            maximum=_METRIC_COUNT_MAX,
        )
        bounded_int(
            denominator,
            label="metric denominator",
            minimum=1,
            maximum=_METRIC_COUNT_MAX,
        )
        if numerator > denominator:
            raise RecallContractError("metric numerator exceeds denominator")
        return cls(MetricStatusV1.AVAILABLE, numerator, denominator, numerator * 1_000_000 // denominator)

    @classmethod
    def unavailable(cls) -> MetricValueV1:
        return cls(MetricStatusV1.UNAVAILABLE, None, None, None)

    @classmethod
    def not_measured(cls) -> MetricValueV1:
        return cls(MetricStatusV1.NOT_MEASURED, None, None, None)

    def to_payload(self) -> dict[str, object]:
        return {
            "denominator": self.denominator,
            "numerator": self.numerator,
            "schema": RECALL_METRIC_SCHEMA,
            "status": self.status.value,
            "value_ppm": self.value_ppm,
        }

    @classmethod
    def from_payload(cls, value: object) -> MetricValueV1:
        payload = exact_object(
            value,
            frozenset({"denominator", "numerator", "schema", "status", "value_ppm"}),
            label="recall metric",
        )
        if payload["schema"] != RECALL_METRIC_SCHEMA:
            raise RecallContractError("recall metric schema is unsupported")
        status = _enum(MetricStatusV1, payload["status"], label="metric status")
        if status is MetricStatusV1.AVAILABLE:
            numerator = bounded_int(
                payload["numerator"],
                label="metric numerator",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            )
            denominator = bounded_int(
                payload["denominator"],
                label="metric denominator",
                minimum=1,
                maximum=_METRIC_COUNT_MAX,
            )
            value_ppm = bounded_int(payload["value_ppm"], label="metric ppm", minimum=0, maximum=1_000_000)
            result = cls(status, numerator, denominator, value_ppm)
            if result.value_ppm != numerator * 1_000_000 // denominator:
                raise RecallContractError("metric ppm is forged")
            return result
        if any(payload[key] is not None for key in ("numerator", "denominator", "value_ppm")):
            raise RecallContractError("unsupported metric contains an invented value")
        return cls(status, None, None, None)


METRIC_NAMES: Final = (
    "candidate_recall_at_50",
    "candidate_recall_at_100",
    "mrr_at_10",
    "ndcg_at_10",
    "false_absence_rate",
    "date_role_accuracy",
    "catalog_coverage",
    "passage_coverage",
    "embedding_coverage",
    "grounded_answer_accuracy",
)
_COVERAGE_METRIC_NAMES: Final = (
    "catalog_coverage",
    "passage_coverage",
    "embedding_coverage",
)
_COVERAGE_LANES_BY_METRIC: Final = {
    "catalog_coverage": frozenset({SearchLane.CATALOG}),
    "passage_coverage": frozenset({SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}),
    "embedding_coverage": frozenset({SearchLane.DENSE}),
}
_SEARCH_CORPUS_BY_ARCHIVE_CORPUS: Final = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
    ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
    ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
    ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
    ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
    ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
}


def _search_corpus_for_archive(corpus: ArchiveSearchCorpus) -> SearchCorpus:
    if type(corpus) is not ArchiveSearchCorpus:
        raise RecallContractError("coverage corpus filter must be typed")
    return _SEARCH_CORPUS_BY_ARCHIVE_CORPUS[corpus]


def _expected_coverage_target_counts(
    corpus: ArchiveSearchCorpus,
) -> tuple[tuple[str, int], ...]:
    if type(corpus) is not ArchiveSearchCorpus:
        raise RecallContractError("coverage plan corpus must be typed")
    request = ArchiveSearchRequest.create(
        query="benchmark coverage plan",
        corpora=(corpus,),
        limit=1,
    )
    targets = canonical_archive_search_targets(request)
    return tuple(
        (
            name,
            sum(lane in lanes for _search_corpus, lane in targets),
        )
        for name in _COVERAGE_METRIC_NAMES
        for lanes in (_COVERAGE_LANES_BY_METRIC[name],)
    )


@dataclass(frozen=True, slots=True)
class RecallCoverageConfigurationV1:
    """One unmapped case's body-free coverage sufficient facts."""

    taxonomy: RecallTaxonomyV1
    expected_corpus: ArchiveSearchCorpus
    absence_oracle_ready: bool
    target_counts: tuple[int, int, int]
    unknown_counts: tuple[int, int, int]
    score_sums_ppm: tuple[int, int, int]
    expected_unknown_counts: tuple[int, int, int]
    expected_score_sums_ppm: tuple[int, int, int]

    def __post_init__(self) -> None:
        if (
            type(self.taxonomy) is not RecallTaxonomyV1
            or type(self.expected_corpus) is not ArchiveSearchCorpus
        ):
            raise RecallContractError("coverage configuration classification must be typed")
        if type(self.absence_oracle_ready) is not bool:
            raise RecallContractError("coverage configuration absence readiness must be boolean")
        for label, values, maximum in (
            ("target", self.target_counts, MAX_COVERAGES),
            ("unknown", self.unknown_counts, MAX_COVERAGES),
            ("score", self.score_sums_ppm, MAX_COVERAGES * _PPM),
            ("expected unknown", self.expected_unknown_counts, MAX_COVERAGES),
            ("expected score", self.expected_score_sums_ppm, MAX_COVERAGES * _PPM),
        ):
            if (
                type(values) is not tuple
                or len(values) != len(_COVERAGE_METRIC_NAMES)
                or any(type(count) is not int or not 0 <= count <= maximum for count in values)
            ):
                raise RecallContractError(f"coverage configuration {label} counts are invalid")
        catalog_targets, passage_targets, embedding_targets = self.target_counts
        message_targets = embedding_targets - catalog_targets
        if (
            not 1 <= embedding_targets <= len(ArchiveSearchCorpus)
            or catalog_targets > len(ArchiveSearchCorpus) - 1
            or not 0 <= message_targets <= 1
            or passage_targets != 2 * embedding_targets - catalog_targets
            or (self.expected_corpus is ArchiveSearchCorpus.MESSAGES and message_targets != 1)
            or (self.expected_corpus is not ArchiveSearchCorpus.MESSAGES and catalog_targets == 0)
        ):
            raise RecallContractError("coverage configuration contradicts archive lanes")
        expected_targets = tuple(
            count for _name, count in _expected_coverage_target_counts(self.expected_corpus)
        )
        for target, unknown, score, expected_target, expected_unknown, expected_score in zip(
            self.target_counts,
            self.unknown_counts,
            self.score_sums_ppm,
            expected_targets,
            self.expected_unknown_counts,
            self.expected_score_sums_ppm,
            strict=True,
        ):
            residual_target = target - expected_target
            residual_unknown = unknown - expected_unknown
            residual_score = score - expected_score
            if (
                expected_target > target
                or unknown > target
                or expected_unknown > expected_target
                or expected_unknown > unknown
                or expected_score > score
                or score > (target - unknown) * _PPM
                or expected_score > (expected_target - expected_unknown) * _PPM
                or residual_unknown < 0
                or residual_unknown > residual_target
                or residual_score < 0
                or residual_score > (residual_target - residual_unknown) * _PPM
            ):
                raise RecallContractError("coverage configuration score allocation is inconsistent")
        if self.absence_oracle_ready and not self.full_known:
            raise RecallContractError("coverage configuration oracle readiness requires full known coverage")

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (
            self.taxonomy.value,
            self.expected_corpus.value,
            self.absence_oracle_ready,
            self.target_counts,
            self.unknown_counts,
            self.score_sums_ppm,
            self.expected_unknown_counts,
            self.expected_score_sums_ppm,
        )

    @property
    def full_known(self) -> bool:
        return not any(self.unknown_counts) and all(
            score == target * _PPM
            for target, score in zip(self.target_counts, self.score_sums_ppm, strict=True)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "absence_oracle_ready": self.absence_oracle_ready,
            "expected_corpus": self.expected_corpus.value,
            "expected_score_sums_ppm": list(self.expected_score_sums_ppm),
            "expected_unknown_counts": list(self.expected_unknown_counts),
            "score_sums_ppm": list(self.score_sums_ppm),
            "target_counts": list(self.target_counts),
            "taxonomy": self.taxonomy.value,
            "unknown_counts": list(self.unknown_counts),
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCoverageConfigurationV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "absence_oracle_ready",
                    "expected_corpus",
                    "expected_score_sums_ppm",
                    "expected_unknown_counts",
                    "score_sums_ppm",
                    "target_counts",
                    "taxonomy",
                    "unknown_counts",
                }
            ),
            label="coverage configuration",
        )

        def vector(name: str, *, maximum: int) -> tuple[int, int, int]:
            raw = payload[name]
            if type(raw) is not list or len(raw) != len(_COVERAGE_METRIC_NAMES):
                raise RecallContractError(f"coverage configuration {name} must be a closed array")
            return cast(
                tuple[int, int, int],
                tuple(
                    bounded_int(
                        item,
                        label=f"coverage configuration {name} item",
                        minimum=0,
                        maximum=maximum,
                    )
                    for item in raw
                ),
            )

        return cls(
            taxonomy=_enum(
                RecallTaxonomyV1,
                payload["taxonomy"],
                label="coverage configuration taxonomy",
            ),
            absence_oracle_ready=cast(bool, payload["absence_oracle_ready"]),
            expected_corpus=_enum(
                ArchiveSearchCorpus,
                payload["expected_corpus"],
                label="coverage configuration expected corpus",
            ),
            target_counts=vector("target_counts", maximum=MAX_COVERAGES),
            unknown_counts=vector("unknown_counts", maximum=MAX_COVERAGES),
            score_sums_ppm=vector("score_sums_ppm", maximum=MAX_COVERAGES * _PPM),
            expected_unknown_counts=vector(
                "expected_unknown_counts",
                maximum=MAX_COVERAGES,
            ),
            expected_score_sums_ppm=vector(
                "expected_score_sums_ppm",
                maximum=MAX_COVERAGES * _PPM,
            ),
        )


def _validate_nested_coverage_configuration(
    value: object,
    *,
    label: str,
) -> RecallCoverageConfigurationV1:
    if type(value) is not RecallCoverageConfigurationV1:
        raise RecallContractError(f"{label} coverage must be typed")
    try:
        validated = RecallCoverageConfigurationV1.from_payload(value.to_payload())
        if validated != value:
            raise RecallContractError(f"{label} coverage is not canonical")
        return validated
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError(f"{label} coverage is malformed") from exc


@dataclass(frozen=True, slots=True)
class RecallNdcgAggregateBucketV1:
    """Count of positive cases sharing one body-free nDCG configuration."""

    expected_grade_counts: tuple[int, int, int]
    expected_temporal_grade_counts: tuple[int, int, int]
    top_10_relevance_grades: tuple[int, ...]
    top_10_temporal_correct: tuple[bool | None, ...]
    rank_11_50_match_counts: tuple[int, ...]
    rank_51_100_match_counts: tuple[int, ...]
    absence_decision: AbsenceDecision
    coverage: RecallCoverageConfigurationV1
    case_count: int

    def __post_init__(self) -> None:
        if (
            type(self.expected_grade_counts) is not tuple
            or len(self.expected_grade_counts) != 3
            or any(
                type(count) is not int or not 0 <= count <= MAX_ALTERNATIVES
                for count in self.expected_grade_counts
            )
            or not 1 <= sum(self.expected_grade_counts) <= MAX_ALTERNATIVES
        ):
            raise RecallContractError("nDCG aggregate expected grades are invalid")
        if (
            type(self.expected_temporal_grade_counts) is not tuple
            or len(self.expected_temporal_grade_counts) != 3
            or any(
                type(count) is not int or not 0 <= count <= self.expected_grade_counts[index]
                for index, count in enumerate(self.expected_temporal_grade_counts)
            )
        ):
            raise RecallContractError("nDCG aggregate temporal qrels are invalid")
        if (
            type(self.top_10_relevance_grades) is not tuple
            or len(self.top_10_relevance_grades) != 10
            or any(type(grade) is not int or not 0 <= grade <= 3 for grade in self.top_10_relevance_grades)
        ):
            raise RecallContractError("nDCG aggregate ranked grades are invalid")
        if (
            type(self.top_10_temporal_correct) is not tuple
            or len(self.top_10_temporal_correct) != 10
            or any(value is not None and type(value) is not bool for value in self.top_10_temporal_correct)
            or any(
                grade == 0 and temporal is not None
                for grade, temporal in zip(
                    self.top_10_relevance_grades,
                    self.top_10_temporal_correct,
                    strict=True,
                )
            )
        ):
            raise RecallContractError("nDCG aggregate ranked temporal facts are invalid")
        for label, values in (
            ("rank 11-50", self.rank_11_50_match_counts),
            ("rank 51-100", self.rank_51_100_match_counts),
        ):
            if (
                type(values) is not tuple
                or len(values) != 9
                or any(type(count) is not int or not 0 <= count <= MAX_ALTERNATIVES for count in values)
            ):
                raise RecallContractError(f"nDCG aggregate {label} match counts are invalid")
        if type(self.absence_decision) is not AbsenceDecision:
            raise RecallContractError("nDCG aggregate absence decision must be typed")
        _validate_nested_coverage_configuration(self.coverage, label="nDCG aggregate")
        matched_counts = self.matched_category_counts
        if any(
            matched_counts[(grade - 1) * 3]
            > self.expected_grade_counts[grade - 1] - self.expected_temporal_grade_counts[grade - 1]
            or sum(matched_counts[(grade - 1) * 3 + 1 : (grade - 1) * 3 + 3])
            > self.expected_temporal_grade_counts[grade - 1]
            for grade in (1, 2, 3)
        ):
            raise RecallContractError("nDCG aggregate matches exceed typed qrels")
        if self.recalled_at_100_count and self.absence_decision is not AbsenceDecision.EVIDENCE_FOUND:
            raise RecallContractError("nDCG aggregate match lacks evidence-found status")
        if self.absence_decision is AbsenceDecision.NOT_ESTABLISHED and self.coverage.absence_oracle_ready:
            raise RecallContractError("uncertain absence contradicts oracle-ready coverage")
        if self.false_absence and (not self.coverage.absence_oracle_ready or not self.coverage.full_known):
            raise RecallContractError("confirmed absence requires oracle-ready full known coverage")
        bounded_int(
            self.case_count,
            label="nDCG aggregate bucket case count",
            minimum=1,
            maximum=MAX_CASES,
        )

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return (
            self.expected_grade_counts,
            self.expected_temporal_grade_counts,
            self.top_10_relevance_grades,
            tuple(0 if value is None else 2 if value else 1 for value in self.top_10_temporal_correct),
            self.rank_11_50_match_counts,
            self.rank_51_100_match_counts,
            self.absence_decision.value,
            self.coverage.canonical_key,
        )

    @property
    def matched_category_counts(self) -> tuple[int, ...]:
        values = [0] * 9
        for grade, temporal in zip(
            self.top_10_relevance_grades,
            self.top_10_temporal_correct,
            strict=True,
        ):
            if grade:
                status_index = 0 if temporal is None else 2 if temporal else 1
                values[(grade - 1) * 3 + status_index] += 1
        for index in range(9):
            values[index] += self.rank_11_50_match_counts[index] + self.rank_51_100_match_counts[index]
        return tuple(values)

    @property
    def qrel_count(self) -> int:
        return sum(self.expected_grade_counts)

    @property
    def recalled_at_50_count(self) -> int:
        return sum(bool(grade) for grade in self.top_10_relevance_grades) + sum(self.rank_11_50_match_counts)

    @property
    def recalled_at_100_count(self) -> int:
        return self.recalled_at_50_count + sum(self.rank_51_100_match_counts)

    @property
    def false_absence(self) -> bool:
        return self.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED

    @property
    def dated_match_count(self) -> int:
        return sum(count for index, count in enumerate(self.matched_category_counts) if index % 3 in (1, 2))

    @property
    def dated_correct_count(self) -> int:
        return sum(count for index, count in enumerate(self.matched_category_counts) if index % 3 == 2)

    @property
    def first_relevant_rank(self) -> int | None:
        return next(
            (rank for rank, grade in enumerate(self.top_10_relevance_grades, start=1) if grade),
            None,
        )

    @property
    def ndcg_ppm(self) -> int:
        observed_dcg = sum(
            ((1 << grade) - 1) * _RANK_DISCOUNT_PPM[rank - 1]
            for rank, grade in enumerate(self.top_10_relevance_grades, start=1)
            if grade
        )
        expected_grades = tuple(
            grade for grade in (3, 2, 1) for _index in range(self.expected_grade_counts[grade - 1])
        )
        ideal_dcg = sum(
            ((1 << grade) - 1) * _RANK_DISCOUNT_PPM[index] for index, grade in enumerate(expected_grades[:10])
        )
        return observed_dcg * _PPM // ideal_dcg

    def to_payload(self) -> dict[str, object]:
        return {
            "absence_decision": self.absence_decision.value,
            "case_count": self.case_count,
            "coverage": self.coverage.to_payload(),
            "expected_grade_counts": list(self.expected_grade_counts),
            "expected_temporal_grade_counts": list(self.expected_temporal_grade_counts),
            "rank_11_50_match_counts": list(self.rank_11_50_match_counts),
            "rank_51_100_match_counts": list(self.rank_51_100_match_counts),
            "top_10_relevance_grades": list(self.top_10_relevance_grades),
            "top_10_temporal_correct": list(self.top_10_temporal_correct),
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallNdcgAggregateBucketV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "absence_decision",
                    "case_count",
                    "coverage",
                    "expected_grade_counts",
                    "expected_temporal_grade_counts",
                    "rank_11_50_match_counts",
                    "rank_51_100_match_counts",
                    "top_10_relevance_grades",
                    "top_10_temporal_correct",
                }
            ),
            label="nDCG aggregate bucket",
        )
        raw_expected = payload["expected_grade_counts"]
        raw_expected_temporal = payload["expected_temporal_grade_counts"]
        raw_ranked = payload["top_10_relevance_grades"]
        raw_ranked_temporal = payload["top_10_temporal_correct"]
        raw_rank_11_50 = payload["rank_11_50_match_counts"]
        raw_rank_51_100 = payload["rank_51_100_match_counts"]
        if any(
            type(item) is not list
            for item in (
                raw_expected,
                raw_expected_temporal,
                raw_ranked,
                raw_ranked_temporal,
                raw_rank_11_50,
                raw_rank_51_100,
            )
        ):
            raise RecallContractError("nDCG aggregate bucket vectors must be arrays")
        expected_values = cast(list[object], raw_expected)
        expected_temporal_values = cast(list[object], raw_expected_temporal)
        ranked_values = cast(list[object], raw_ranked)
        ranked_temporal_values = cast(list[object], raw_ranked_temporal)
        rank_11_50_values = cast(list[object], raw_rank_11_50)
        rank_51_100_values = cast(list[object], raw_rank_51_100)
        if (
            len(expected_values) != 3
            or len(expected_temporal_values) != 3
            or len(ranked_values) != 10
            or len(ranked_temporal_values) != 10
            or len(rank_11_50_values) != 9
            or len(rank_51_100_values) != 9
        ):
            raise RecallContractError("nDCG aggregate bucket vector length is invalid")
        if any(item is not None and type(item) is not bool for item in ranked_temporal_values):
            raise RecallContractError("nDCG aggregate bucket flags are invalid")
        return cls(
            expected_grade_counts=cast(
                tuple[int, int, int],
                tuple(
                    bounded_int(
                        item,
                        label="nDCG aggregate expected grade count",
                        minimum=0,
                        maximum=MAX_ALTERNATIVES,
                    )
                    for item in expected_values
                ),
            ),
            expected_temporal_grade_counts=cast(
                tuple[int, int, int],
                tuple(
                    bounded_int(
                        item,
                        label="nDCG aggregate expected temporal grade count",
                        minimum=0,
                        maximum=MAX_ALTERNATIVES,
                    )
                    for item in expected_temporal_values
                ),
            ),
            top_10_relevance_grades=tuple(
                bounded_int(
                    item,
                    label="nDCG aggregate ranked grade",
                    minimum=0,
                    maximum=3,
                )
                for item in ranked_values
            ),
            top_10_temporal_correct=tuple(cast(bool | None, item) for item in ranked_temporal_values),
            rank_11_50_match_counts=tuple(
                bounded_int(
                    item,
                    label="nDCG aggregate rank 11-50 match count",
                    minimum=0,
                    maximum=MAX_ALTERNATIVES,
                )
                for item in rank_11_50_values
            ),
            rank_51_100_match_counts=tuple(
                bounded_int(
                    item,
                    label="nDCG aggregate rank 51-100 match count",
                    minimum=0,
                    maximum=MAX_ALTERNATIVES,
                )
                for item in rank_51_100_values
            ),
            absence_decision=_enum(
                AbsenceDecision,
                payload["absence_decision"],
                label="nDCG aggregate absence decision",
            ),
            coverage=RecallCoverageConfigurationV1.from_payload(payload["coverage"]),
            case_count=bounded_int(
                payload["case_count"],
                label="nDCG aggregate bucket case count",
                minimum=1,
                maximum=MAX_CASES,
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallMetricAggregateV1:
    """Aggregate-only sufficient facts for every non-coverage metric."""

    expected_hit_case_count: int
    qrel_count: int
    recalled_at_50_count: int
    recalled_at_100_count: int
    first_relevant_rank_counts: tuple[int, ...]
    ndcg_sum_ppm: int
    ndcg_buckets: tuple[RecallNdcgAggregateBucketV1, ...]
    false_absence_count: int
    dated_match_count: int
    dated_correct_count: int

    def __post_init__(self) -> None:
        case_counts = {
            "expected hit case count": self.expected_hit_case_count,
            "false absence count": self.false_absence_count,
        }
        metric_counts = {
            "qrel count": self.qrel_count,
            "recalled at 50 count": self.recalled_at_50_count,
            "recalled at 100 count": self.recalled_at_100_count,
            "nDCG sum": self.ndcg_sum_ppm,
            "dated match count": self.dated_match_count,
            "dated correct count": self.dated_correct_count,
        }
        for label, value in case_counts.items():
            bounded_int(
                value,
                label=f"metric aggregate {label}",
                minimum=0,
                maximum=MAX_CASES,
            )
        for label, value in metric_counts.items():
            bounded_int(
                value,
                label=f"metric aggregate {label}",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            )
        if (
            type(self.first_relevant_rank_counts) is not tuple
            or len(self.first_relevant_rank_counts) != 10
            or any(
                type(item) is not int or item < 0 or item > MAX_CASES
                for item in self.first_relevant_rank_counts
            )
        ):
            raise RecallContractError("metric aggregate rank histogram is invalid")
        if (
            type(self.ndcg_buckets) is not tuple
            or len(self.ndcg_buckets) > MAX_CASES
            or any(type(item) is not RecallNdcgAggregateBucketV1 for item in self.ndcg_buckets)
        ):
            raise RecallContractError("metric aggregate nDCG buckets are invalid")
        try:
            validated_buckets = tuple(
                RecallNdcgAggregateBucketV1.from_payload(item.to_payload()) for item in self.ndcg_buckets
            )
            if validated_buckets != self.ndcg_buckets:
                raise RecallContractError("metric aggregate nDCG bucket is not canonical")
            bucket_keys = tuple(item.canonical_key for item in validated_buckets)
        except RecallContractError:
            raise
        except Exception as exc:
            raise RecallContractError("metric aggregate nDCG bucket is malformed") from exc
        if bucket_keys != tuple(sorted(bucket_keys)) or len(bucket_keys) != len(set(bucket_keys)):
            raise RecallContractError("metric aggregate nDCG buckets are not canonical")
        derived_expected_hit_count = sum(item.case_count for item in validated_buckets)
        if self.expected_hit_case_count != derived_expected_hit_count:
            raise RecallContractError("metric aggregate bucket case count contradicts total")
        derived_qrel_count = sum(item.qrel_count * item.case_count for item in validated_buckets)
        derived_rank_counts = tuple(
            sum(item.case_count for item in validated_buckets if item.first_relevant_rank == rank)
            for rank in range(1, 11)
        )
        derived_ndcg_sum = sum(item.ndcg_ppm * item.case_count for item in validated_buckets)
        derived_recalled_at_50 = sum(
            item.recalled_at_50_count * item.case_count for item in validated_buckets
        )
        derived_recalled_at_100 = sum(
            item.recalled_at_100_count * item.case_count for item in validated_buckets
        )
        derived_false_absence = sum(item.false_absence * item.case_count for item in validated_buckets)
        derived_dated_matches = sum(item.dated_match_count * item.case_count for item in validated_buckets)
        derived_dated_correct = sum(item.dated_correct_count * item.case_count for item in validated_buckets)
        if (
            self.qrel_count != derived_qrel_count
            or self.recalled_at_50_count != derived_recalled_at_50
            or self.recalled_at_100_count != derived_recalled_at_100
            or self.first_relevant_rank_counts != derived_rank_counts
            or self.ndcg_sum_ppm != derived_ndcg_sum
            or self.false_absence_count != derived_false_absence
            or self.dated_match_count != derived_dated_matches
            or self.dated_correct_count != derived_dated_correct
        ):
            raise RecallContractError("metric aggregate nDCG buckets contradict totals")
        ranked_hit_count = sum(self.first_relevant_rank_counts)
        if self.expected_hit_case_count == 0:
            if any((*case_counts.values(), *metric_counts.values())) or ranked_hit_count:
                raise RecallContractError("negative-only aggregate carries expected-hit facts")
            return
        if not (
            self.expected_hit_case_count <= self.qrel_count <= self.expected_hit_case_count * MAX_ALTERNATIVES
            and 0 <= self.recalled_at_50_count <= self.recalled_at_100_count <= self.qrel_count
            and self.false_absence_count <= self.expected_hit_case_count
            and self.recalled_at_100_count
            <= min(
                self.qrel_count - self.false_absence_count,
                (self.expected_hit_case_count - self.false_absence_count) * MAX_ALTERNATIVES,
            )
            and ranked_hit_count
            <= min(
                self.recalled_at_50_count,
                self.expected_hit_case_count - self.false_absence_count,
            )
            and self.dated_correct_count <= self.dated_match_count <= self.recalled_at_100_count
        ):
            raise RecallContractError("metric aggregate counts are inconsistent")

    @property
    def reciprocal_rank_sum_ppm(self) -> int:
        return sum(
            count * reciprocal
            for count, reciprocal in zip(
                self.first_relevant_rank_counts,
                _RECIPROCAL_RANK_PPM,
                strict=True,
            )
        )

    def metrics(
        self,
        coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
    ) -> tuple[tuple[str, MetricValueV1], ...]:
        _validate_coverage_aggregate_items(coverage_facts)
        coverage = {name: facts.metric for name, facts in coverage_facts}
        if self.expected_hit_case_count:
            values = {
                "candidate_recall_at_50": MetricValueV1.ratio(
                    self.recalled_at_50_count,
                    self.qrel_count,
                ),
                "candidate_recall_at_100": MetricValueV1.ratio(
                    self.recalled_at_100_count,
                    self.qrel_count,
                ),
                "mrr_at_10": MetricValueV1.ratio(
                    self.reciprocal_rank_sum_ppm,
                    self.expected_hit_case_count * _PPM,
                ),
                "ndcg_at_10": MetricValueV1.ratio(
                    self.ndcg_sum_ppm,
                    self.expected_hit_case_count * _PPM,
                ),
                "false_absence_rate": MetricValueV1.ratio(
                    self.false_absence_count,
                    self.expected_hit_case_count,
                ),
                "date_role_accuracy": (
                    MetricValueV1.ratio(self.dated_correct_count, self.dated_match_count)
                    if self.dated_match_count
                    else MetricValueV1.unavailable()
                ),
            }
        else:
            values = {
                name: MetricValueV1.unavailable()
                for name in (
                    "candidate_recall_at_50",
                    "candidate_recall_at_100",
                    "mrr_at_10",
                    "ndcg_at_10",
                    "false_absence_rate",
                    "date_role_accuracy",
                )
            }
        values.update(coverage)
        values["grounded_answer_accuracy"] = MetricValueV1.not_measured()
        return tuple((name, values[name]) for name in METRIC_NAMES)

    def to_payload(self) -> dict[str, object]:
        return {
            "dated_correct_count": self.dated_correct_count,
            "dated_match_count": self.dated_match_count,
            "expected_hit_case_count": self.expected_hit_case_count,
            "false_absence_count": self.false_absence_count,
            "first_relevant_rank_counts": list(self.first_relevant_rank_counts),
            "ndcg_buckets": [item.to_payload() for item in self.ndcg_buckets],
            "ndcg_sum_ppm": self.ndcg_sum_ppm,
            "qrel_count": self.qrel_count,
            "recalled_at_100_count": self.recalled_at_100_count,
            "recalled_at_50_count": self.recalled_at_50_count,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallMetricAggregateV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "dated_correct_count",
                    "dated_match_count",
                    "expected_hit_case_count",
                    "false_absence_count",
                    "first_relevant_rank_counts",
                    "ndcg_buckets",
                    "ndcg_sum_ppm",
                    "qrel_count",
                    "recalled_at_100_count",
                    "recalled_at_50_count",
                }
            ),
            label="metric aggregate facts",
        )
        raw_ranks = payload["first_relevant_rank_counts"]
        raw_ndcg_buckets = payload["ndcg_buckets"]
        if type(raw_ranks) is not list or type(raw_ndcg_buckets) is not list:
            raise RecallContractError("metric aggregate vectors must be arrays")
        if len(raw_ranks) != 10 or len(raw_ndcg_buckets) > MAX_CASES:
            raise RecallContractError("metric aggregate vector length is invalid")
        return cls(
            expected_hit_case_count=bounded_int(
                payload["expected_hit_case_count"],
                label="metric aggregate expected hit case count",
                minimum=0,
                maximum=MAX_CASES,
            ),
            qrel_count=bounded_int(
                payload["qrel_count"],
                label="metric aggregate qrel count",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
            recalled_at_50_count=bounded_int(
                payload["recalled_at_50_count"],
                label="metric aggregate recalled at 50 count",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
            recalled_at_100_count=bounded_int(
                payload["recalled_at_100_count"],
                label="metric aggregate recalled at 100 count",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
            first_relevant_rank_counts=tuple(
                bounded_int(
                    item,
                    label="metric aggregate rank count",
                    minimum=0,
                    maximum=MAX_CASES,
                )
                for item in raw_ranks
            ),
            ndcg_sum_ppm=bounded_int(
                payload["ndcg_sum_ppm"],
                label="metric aggregate nDCG sum",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
            ndcg_buckets=tuple(RecallNdcgAggregateBucketV1.from_payload(item) for item in raw_ndcg_buckets),
            false_absence_count=bounded_int(
                payload["false_absence_count"],
                label="metric aggregate false absence count",
                minimum=0,
                maximum=MAX_CASES,
            ),
            dated_match_count=bounded_int(
                payload["dated_match_count"],
                label="metric aggregate dated match count",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
            dated_correct_count=bounded_int(
                payload["dated_correct_count"],
                label="metric aggregate dated correct count",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallCoverageAggregateV1:
    """Aggregate-only sufficient facts for one coverage metric."""

    target_count: int
    unknown_count: int
    score_sum_ppm: int

    def __post_init__(self) -> None:
        target_count = bounded_int(
            self.target_count,
            label="coverage aggregate target count",
            minimum=0,
            maximum=_COUNT_MAX,
        )
        unknown_count = bounded_int(
            self.unknown_count,
            label="coverage aggregate unknown count",
            minimum=0,
            maximum=_COUNT_MAX,
        )
        score_sum = bounded_int(
            self.score_sum_ppm,
            label="coverage aggregate score sum",
            minimum=0,
            maximum=_METRIC_COUNT_MAX,
        )
        if unknown_count > target_count or score_sum > (target_count - unknown_count) * _PPM:
            raise RecallContractError("coverage aggregate facts are inconsistent")

    @property
    def metric(self) -> MetricValueV1:
        if self.target_count == 0 or self.unknown_count:
            return MetricValueV1.unavailable()
        return MetricValueV1.ratio(self.score_sum_ppm, self.target_count * _PPM)

    def to_payload(self) -> dict[str, object]:
        return {
            "score_sum_ppm": self.score_sum_ppm,
            "target_count": self.target_count,
            "unknown_count": self.unknown_count,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCoverageAggregateV1:
        payload = exact_object(
            value,
            frozenset({"score_sum_ppm", "target_count", "unknown_count"}),
            label="coverage aggregate facts",
        )
        return cls(
            target_count=bounded_int(
                payload["target_count"],
                label="coverage aggregate target count",
                minimum=0,
                maximum=_COUNT_MAX,
            ),
            unknown_count=bounded_int(
                payload["unknown_count"],
                label="coverage aggregate unknown count",
                minimum=0,
                maximum=_COUNT_MAX,
            ),
            score_sum_ppm=bounded_int(
                payload["score_sum_ppm"],
                label="coverage aggregate score sum",
                minimum=0,
                maximum=_METRIC_COUNT_MAX,
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallCoveragePlanAggregateV1:
    """Count of cases sharing one body-free archive coverage configuration."""

    coverage: RecallCoverageConfigurationV1
    case_count: int
    false_absence_count: int

    def __post_init__(self) -> None:
        _validate_nested_coverage_configuration(self.coverage, label="coverage plan")
        case_count = bounded_int(
            self.case_count,
            label="coverage plan case count",
            minimum=1,
            maximum=MAX_CASES,
        )
        false_absence_count = bounded_int(
            self.false_absence_count,
            label="coverage plan false absence count",
            minimum=0,
            maximum=MAX_CASES,
        )
        if false_absence_count > case_count:
            raise RecallContractError("coverage plan false absence count exceeds cases")
        if self.false_absence_count and (
            not self.coverage.absence_oracle_ready or not self.coverage.full_known
        ):
            raise RecallContractError(
                "confirmed absence requires oracle-ready full known coverage in its plan"
            )

    @property
    def canonical_key(self) -> tuple[object, ...]:
        return self.coverage.canonical_key

    @property
    def target_counts(self) -> tuple[int, int, int]:
        return self.coverage.target_counts

    @property
    def unknown_counts(self) -> tuple[int, int, int]:
        return self.coverage.unknown_counts

    @property
    def score_sums_ppm(self) -> tuple[int, int, int]:
        return self.coverage.score_sums_ppm

    def to_payload(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "coverage": self.coverage.to_payload(),
            "false_absence_count": self.false_absence_count,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCoveragePlanAggregateV1:
        payload = exact_object(
            value,
            frozenset({"case_count", "coverage", "false_absence_count"}),
            label="coverage plan aggregate",
        )
        return cls(
            coverage=RecallCoverageConfigurationV1.from_payload(payload["coverage"]),
            case_count=bounded_int(
                payload["case_count"],
                label="coverage plan case count",
                minimum=1,
                maximum=MAX_CASES,
            ),
            false_absence_count=bounded_int(
                payload["false_absence_count"],
                label="coverage plan false absence count",
                minimum=0,
                maximum=MAX_CASES,
            ),
        )


def _validate_coverage_aggregate_items(
    values: tuple[tuple[str, RecallCoverageAggregateV1], ...],
) -> None:
    if type(values) is not tuple or len(values) != len(_COVERAGE_METRIC_NAMES):
        raise RecallContractError("coverage aggregate catalog is not closed")
    if any(
        type(item) is not tuple
        or len(item) != 2
        or type(item[0]) is not str
        or type(item[1]) is not RecallCoverageAggregateV1
        for item in values
    ):
        raise RecallContractError("coverage aggregate catalog types are invalid")
    typed_values = cast(tuple[tuple[str, RecallCoverageAggregateV1], ...], values)
    if tuple(item[0] for item in typed_values) != _COVERAGE_METRIC_NAMES:
        raise RecallContractError("coverage aggregate catalog order is invalid")
    try:
        validated = tuple(
            (name, RecallCoverageAggregateV1.from_payload(facts.to_payload())) for name, facts in typed_values
        )
        if validated != typed_values:
            raise RecallContractError("coverage aggregate catalog is not canonical")
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError("coverage aggregate catalog is malformed") from exc


def _coverage_aggregate_items_from_payload(
    value: object,
) -> tuple[tuple[str, RecallCoverageAggregateV1], ...]:
    if type(value) is not dict or frozenset(cast(dict[object, object], value)) != frozenset(
        _COVERAGE_METRIC_NAMES
    ):
        raise RecallContractError("coverage aggregate catalog keys are invalid")
    payload = cast(dict[str, object], value)
    return tuple(
        (name, RecallCoverageAggregateV1.from_payload(payload[name])) for name in _COVERAGE_METRIC_NAMES
    )


@dataclass(frozen=True, slots=True)
class RecallBreakdownV1:
    label: str
    case_count: int
    metric_facts: RecallMetricAggregateV1
    metrics: tuple[tuple[str, MetricValueV1], ...]
    coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...]

    def __post_init__(self) -> None:
        bounded_text(self.label, label="breakdown label", maximum_bytes=80)
        bounded_int(self.case_count, label="breakdown case count", minimum=1, maximum=MAX_CASES)
        if type(self.metric_facts) is not RecallMetricAggregateV1:
            raise RecallContractError("breakdown metric facts must be typed")
        _validate_metric_items(self.metrics)
        _validate_coverage_aggregate_items(self.coverage_facts)
        metrics = dict(self.metrics)
        if any(metrics[name] != facts.metric for name, facts in self.coverage_facts):
            raise RecallContractError("breakdown coverage metrics contradict aggregate facts")
        _validate_aggregate_semantics(
            self.metrics,
            self.metric_facts,
            self.coverage_facts,
            case_count=self.case_count,
            coverage_corpus=(
                ArchiveSearchCorpus(self.label)
                if self.label in {item.value for item in ArchiveSearchCorpus}
                else None
            ),
            coverage_taxonomy=(
                RecallTaxonomyV1(self.label)
                if self.label in {item.value for item in RecallTaxonomyV1}
                else None
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "coverage_facts": {name: facts.to_payload() for name, facts in self.coverage_facts},
            "label": self.label,
            "metric_facts": self.metric_facts.to_payload(),
            "metrics": {name: metric.to_payload() for name, metric in self.metrics},
        }

    @classmethod
    def create(
        cls,
        *,
        label: str,
        cases: Iterable[RecallCaseResultV1],
        coverage_corpus: ArchiveSearchCorpus | None = None,
    ) -> RecallBreakdownV1:
        raw_cases = _bounded_tuple(cases, maximum=MAX_CASES, label="breakdown case results")
        if not raw_cases or any(type(item) is not RecallCaseResultV1 for item in raw_cases):
            raise RecallContractError("breakdown factory requires typed case results")
        case_values = cast(tuple[RecallCaseResultV1, ...], raw_cases)
        metric_facts = _metric_aggregate_from_results(case_values)
        coverage_facts = _coverage_aggregate_from_results(
            case_values,
            coverage_corpus=coverage_corpus,
        )
        return cls(
            label=bounded_text(label, label="breakdown label", maximum_bytes=80),
            case_count=len(case_values),
            metric_facts=metric_facts,
            metrics=metric_facts.metrics(coverage_facts),
            coverage_facts=coverage_facts,
        )

    @classmethod
    def from_payload(cls, value: object) -> RecallBreakdownV1:
        payload = exact_object(
            value,
            frozenset({"case_count", "coverage_facts", "label", "metric_facts", "metrics"}),
            label="recall breakdown",
        )
        return cls(
            label=bounded_text(payload["label"], label="breakdown label", maximum_bytes=80),
            case_count=bounded_int(
                payload["case_count"], label="breakdown case count", minimum=1, maximum=MAX_CASES
            ),
            metric_facts=RecallMetricAggregateV1.from_payload(payload["metric_facts"]),
            metrics=_metric_items_from_payload(payload["metrics"]),
            coverage_facts=_coverage_aggregate_items_from_payload(payload["coverage_facts"]),
        )


@dataclass(frozen=True, slots=True)
class RecallMatchedFactV1:
    """Sufficient body-free fact for one uniquely credited qrel."""

    rank: int
    relevance_grade: int
    temporal_correct: bool | None

    def __post_init__(self) -> None:
        bounded_int(self.rank, label="matched fact rank", minimum=1, maximum=MAX_CANDIDATES)
        bounded_int(
            self.relevance_grade,
            label="matched fact relevance grade",
            minimum=1,
            maximum=3,
        )
        if self.temporal_correct is not None and type(self.temporal_correct) is not bool:
            raise RecallContractError("matched temporal fact must be boolean or unavailable")

    def to_payload(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "relevance_grade": self.relevance_grade,
            "temporal_correct": self.temporal_correct,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallMatchedFactV1:
        payload = exact_object(
            value,
            frozenset({"rank", "relevance_grade", "temporal_correct"}),
            label="matched relevance fact",
        )
        temporal_correct = payload["temporal_correct"]
        if temporal_correct is not None and type(temporal_correct) is not bool:
            raise RecallContractError("matched temporal fact must be boolean or unavailable")
        return cls(
            rank=bounded_int(
                payload["rank"],
                label="matched fact rank",
                minimum=1,
                maximum=MAX_CANDIDATES,
            ),
            relevance_grade=bounded_int(
                payload["relevance_grade"],
                label="matched fact relevance grade",
                minimum=1,
                maximum=3,
            ),
            temporal_correct=cast(bool | None, temporal_correct),
        )


def _ranked_ndcg_ppm(
    expected_relevance_grades: tuple[int, ...],
    matched_facts: tuple[RecallMatchedFactV1, ...],
) -> int:
    observed_dcg = sum(
        ((1 << fact.relevance_grade) - 1) * _RANK_DISCOUNT_PPM[fact.rank - 1]
        for fact in matched_facts
        if fact.rank <= 10
    )
    ideal_dcg = sum(
        ((1 << grade) - 1) * _RANK_DISCOUNT_PPM[index]
        for index, grade in enumerate(expected_relevance_grades[:10])
    )
    return observed_dcg * _PPM // ideal_dcg if ideal_dcg else 0


@dataclass(frozen=True, slots=True)
class RecallCaseResultV1:
    case_id: str
    case_sha256: str
    observation_sha256: str
    taxonomy: RecallTaxonomyV1
    corpus: ArchiveSearchCorpus
    expected_no_hit: bool
    candidate_count: int
    absence_decision: AbsenceDecision
    coverage_authorizes_absence: bool
    outcome: RecallOutcomeV1
    first_relevant_rank: int | None
    expected_relevance_grades: tuple[int, ...]
    expected_temporal_grade_counts: tuple[int, int, int]
    matched_facts: tuple[RecallMatchedFactV1, ...]
    coverage_target_scores: tuple[
        tuple[str, tuple[tuple[SearchCorpus, int | None], ...]],
        ...,
    ]
    metrics: tuple[tuple[str, MetricValueV1], ...]

    def __post_init__(self) -> None:
        _opaque_case_id(self.case_id)
        sha256_text(self.case_sha256, label="result case digest")
        sha256_text(self.observation_sha256, label="result observation digest")
        if type(self.taxonomy) is not RecallTaxonomyV1 or type(self.corpus) is not ArchiveSearchCorpus:
            raise RecallContractError("case result classification must be typed")
        if type(self.expected_no_hit) is not bool:
            raise RecallContractError("case result expected_no_hit must be boolean")
        bounded_int(
            self.candidate_count,
            label="case result candidate count",
            minimum=0,
            maximum=MAX_CANDIDATES,
        )
        if type(self.absence_decision) is not AbsenceDecision:
            raise RecallContractError("case result absence decision must be typed")
        if type(self.coverage_authorizes_absence) is not bool:
            raise RecallContractError("case result absence readiness must be boolean")
        expected_absence_decision = (
            AbsenceDecision.EVIDENCE_FOUND
            if self.candidate_count
            else (
                AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
                if self.coverage_authorizes_absence
                else AbsenceDecision.NOT_ESTABLISHED
            )
        )
        if self.absence_decision is not expected_absence_decision:
            raise RecallContractError("case result coverage contradicts absence decision")
        if type(self.outcome) is not RecallOutcomeV1:
            raise RecallContractError("case outcome must be typed")
        bounded_optional_int(
            self.first_relevant_rank,
            label="first relevant rank",
            minimum=1,
            maximum=MAX_CANDIDATES,
        )
        if (
            type(self.expected_relevance_grades) is not tuple
            or len(self.expected_relevance_grades) > MAX_ALTERNATIVES
            or any(type(grade) is not int or not 1 <= grade <= 3 for grade in self.expected_relevance_grades)
            or self.expected_relevance_grades != tuple(sorted(self.expected_relevance_grades, reverse=True))
            or bool(self.expected_relevance_grades) == self.expected_no_hit
        ):
            raise RecallContractError("expected relevance grades are not canonical")
        if (
            type(self.expected_temporal_grade_counts) is not tuple
            or len(self.expected_temporal_grade_counts) != 3
            or any(type(count) is not int or count < 0 for count in self.expected_temporal_grade_counts)
            or any(
                self.expected_temporal_grade_counts[grade - 1]
                > Counter(self.expected_relevance_grades)[grade]
                for grade in (1, 2, 3)
            )
        ):
            raise RecallContractError("expected temporal grade counts are not canonical")
        if (
            type(self.matched_facts) is not tuple
            or len(self.matched_facts) > len(self.expected_relevance_grades)
            or len(self.matched_facts) > self.candidate_count
            or any(type(item) is not RecallMatchedFactV1 for item in self.matched_facts)
            or tuple(item.rank for item in self.matched_facts)
            != tuple(sorted(item.rank for item in self.matched_facts))
            or len({item.rank for item in self.matched_facts}) != len(self.matched_facts)
            or any(
                count > Counter(self.expected_relevance_grades)[grade]
                for grade, count in Counter(item.relevance_grade for item in self.matched_facts).items()
            )
        ):
            raise RecallContractError("matched relevance facts are not canonical")
        if any(
            sum(
                item.relevance_grade == grade and item.temporal_correct is not None
                for item in self.matched_facts
            )
            > self.expected_temporal_grade_counts[grade - 1]
            or sum(
                item.relevance_grade == grade and item.temporal_correct is None for item in self.matched_facts
            )
            > Counter(self.expected_relevance_grades)[grade] - self.expected_temporal_grade_counts[grade - 1]
            for grade in (1, 2, 3)
        ):
            raise RecallContractError("matched temporal facts exceed typed qrels")
        expected_first_rank = self.matched_facts[0].rank if self.matched_facts else None
        if self.first_relevant_rank != expected_first_rank:
            raise RecallContractError("first relevant rank contradicts matched facts")
        if self.matched_facts and self.candidate_count == 0:
            raise RecallContractError("matched relevance requires an observed candidate")
        if self.matched_facts:
            expected_outcome = RecallOutcomeV1.HIT
        elif not self.expected_no_hit:
            expected_outcome = RecallOutcomeV1.MISS
        elif self.candidate_count:
            expected_outcome = RecallOutcomeV1.FALSE_POSITIVE
        elif self.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED:
            expected_outcome = RecallOutcomeV1.EXPECTED_NO_HIT
        else:
            expected_outcome = RecallOutcomeV1.UNCERTAIN_NO_HIT
        if self.outcome is not expected_outcome:
            raise RecallContractError("case outcome contradicts its bound observation facts")
        raw_coverage_scores = self.coverage_target_scores
        if type(raw_coverage_scores) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not tuple
            for item in raw_coverage_scores
        ):
            raise RecallContractError("case coverage target scores are not closed")
        typed_coverage_scores = cast(
            tuple[tuple[str, tuple[tuple[SearchCorpus, int | None], ...]], ...],
            raw_coverage_scores,
        )
        if (
            tuple(item[0] for item in typed_coverage_scores) != _COVERAGE_METRIC_NAMES
            or not 1 <= sum(len(item[1]) for item in typed_coverage_scores) <= MAX_COVERAGES
            or any(
                len(scores) > MAX_COVERAGES
                or any(
                    type(score_fact) is not tuple
                    or len(score_fact) != 2
                    or type(score_fact[0]) is not SearchCorpus
                    or (
                        score_fact[1] is not None
                        and (type(score_fact[1]) is not int or not 0 <= score_fact[1] <= _PPM)
                    )
                    for score_fact in scores
                )
                for _name, scores in typed_coverage_scores
            )
        ):
            raise RecallContractError("case coverage target scores are not closed")
        if any(
            scores
            != tuple(
                sorted(
                    scores,
                    key=lambda item: (
                        item[0].value,
                        -1 if item[1] is None else item[1],
                    ),
                )
            )
            for _name, scores in typed_coverage_scores
        ):
            raise RecallContractError("case coverage target scores are not canonical")
        _validate_metric_items(self.metrics)
        facts = dict(self.metrics)
        for name, score_facts in self.coverage_target_scores:
            scores = tuple(score for _corpus, score in score_facts)
            expected_metric = (
                MetricValueV1.unavailable()
                if not scores or any(score is None for score in scores)
                else MetricValueV1.ratio(sum(cast(int, score) for score in scores), len(scores) * _PPM)
            )
            if facts[name] != expected_metric:
                raise RecallContractError("coverage metric contradicts its sufficient target facts")
        core_names = (
            "candidate_recall_at_50",
            "candidate_recall_at_100",
            "mrr_at_10",
            "ndcg_at_10",
            "false_absence_rate",
        )
        if self.expected_no_hit:
            if any(
                facts[name].status is not MetricStatusV1.UNAVAILABLE
                for name in (*core_names, "date_role_accuracy")
            ):
                raise RecallContractError("negative case carries invented relevance metrics")
        else:
            reciprocal = (
                _PPM // self.first_relevant_rank
                if self.first_relevant_rank is not None and self.first_relevant_rank <= 10
                else 0
            )
            expected_false_absence = int(
                self.candidate_count == 0
                and self.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
            )
            temporal = tuple(
                item.temporal_correct for item in self.matched_facts if item.temporal_correct is not None
            )
            expected_metrics = {
                "candidate_recall_at_50": MetricValueV1.ratio(
                    sum(item.rank <= 50 for item in self.matched_facts),
                    len(self.expected_relevance_grades),
                ),
                "candidate_recall_at_100": MetricValueV1.ratio(
                    len(self.matched_facts),
                    len(self.expected_relevance_grades),
                ),
                "mrr_at_10": MetricValueV1.ratio(reciprocal, _PPM),
                "ndcg_at_10": MetricValueV1.ratio(
                    _ranked_ndcg_ppm(self.expected_relevance_grades, self.matched_facts),
                    _PPM,
                ),
                "false_absence_rate": MetricValueV1.ratio(expected_false_absence, 1),
                "date_role_accuracy": (
                    MetricValueV1.ratio(sum(temporal), len(temporal))
                    if temporal
                    else MetricValueV1.unavailable()
                ),
            }
            if any(facts[name] != metric for name, metric in expected_metrics.items()):
                raise RecallContractError("case metrics contradict sufficient relevance facts")

    def to_payload(self) -> dict[str, object]:
        return {
            "absence_decision": self.absence_decision.value,
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "candidate_count": self.candidate_count,
            "corpus": self.corpus.value,
            "coverage_authorizes_absence": self.coverage_authorizes_absence,
            "coverage_target_scores": {
                name: [{"corpus": corpus.value, "score_ppm": score} for corpus, score in scores]
                for name, scores in self.coverage_target_scores
            },
            "expected_no_hit": self.expected_no_hit,
            "expected_relevance_grades": list(self.expected_relevance_grades),
            "expected_temporal_grade_counts": list(self.expected_temporal_grade_counts),
            "first_relevant_rank": self.first_relevant_rank,
            "matched_facts": [item.to_payload() for item in self.matched_facts],
            "metrics": {name: metric.to_payload() for name, metric in self.metrics},
            "observation_sha256": self.observation_sha256,
            "outcome": self.outcome.value,
            "taxonomy": self.taxonomy.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCaseResultV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "absence_decision",
                    "case_id",
                    "case_sha256",
                    "candidate_count",
                    "corpus",
                    "coverage_authorizes_absence",
                    "coverage_target_scores",
                    "expected_no_hit",
                    "expected_relevance_grades",
                    "expected_temporal_grade_counts",
                    "first_relevant_rank",
                    "matched_facts",
                    "metrics",
                    "observation_sha256",
                    "outcome",
                    "taxonomy",
                }
            ),
            label="recall case result",
        )
        raw_coverage_scores = payload["coverage_target_scores"]
        if type(raw_coverage_scores) is not dict or frozenset(raw_coverage_scores) != frozenset(
            _COVERAGE_METRIC_NAMES
        ):
            raise RecallContractError("case coverage target scores are invalid")
        coverage_scores = cast(dict[str, object], raw_coverage_scores)
        raw_expected_grades = payload["expected_relevance_grades"]
        raw_expected_temporal = payload["expected_temporal_grade_counts"]
        raw_matched_facts = payload["matched_facts"]
        if (
            type(raw_expected_grades) is not list
            or type(raw_expected_temporal) is not list
            or type(raw_matched_facts) is not list
        ):
            raise RecallContractError("case relevance facts must be arrays")
        if len(raw_expected_temporal) != 3:
            raise RecallContractError("case temporal grade vector length is invalid")
        parsed_coverage_scores: list[tuple[str, tuple[tuple[SearchCorpus, int | None], ...]]] = []
        for name in _COVERAGE_METRIC_NAMES:
            raw_scores = coverage_scores[name]
            if type(raw_scores) is not list:
                raise RecallContractError("case coverage target scores must be arrays")
            parsed: list[tuple[SearchCorpus, int | None]] = []
            for raw_score in raw_scores:
                score_payload = exact_object(
                    raw_score,
                    frozenset({"corpus", "score_ppm"}),
                    label=f"{name} target score",
                )
                score = score_payload["score_ppm"]
                parsed.append(
                    (
                        _enum(
                            SearchCorpus,
                            score_payload["corpus"],
                            label=f"{name} target corpus",
                        ),
                        None
                        if score is None
                        else bounded_int(
                            score,
                            label=f"{name} target score",
                            minimum=0,
                            maximum=_PPM,
                        ),
                    )
                )
            parsed_coverage_scores.append((name, tuple(parsed)))
        return cls(
            case_id=_opaque_case_id(payload["case_id"]),
            case_sha256=sha256_text(payload["case_sha256"], label="result case digest"),
            observation_sha256=sha256_text(payload["observation_sha256"], label="result observation digest"),
            taxonomy=_enum(RecallTaxonomyV1, payload["taxonomy"], label="result taxonomy"),
            corpus=_enum(ArchiveSearchCorpus, payload["corpus"], label="result corpus"),
            expected_no_hit=cast(bool, payload["expected_no_hit"]),
            candidate_count=bounded_int(
                payload["candidate_count"],
                label="case result candidate count",
                minimum=0,
                maximum=MAX_CANDIDATES,
            ),
            absence_decision=_enum(
                AbsenceDecision,
                payload["absence_decision"],
                label="result absence decision",
            ),
            coverage_authorizes_absence=cast(
                bool,
                payload["coverage_authorizes_absence"],
            ),
            outcome=_enum(RecallOutcomeV1, payload["outcome"], label="case outcome"),
            first_relevant_rank=bounded_optional_int(
                payload["first_relevant_rank"],
                label="first relevant rank",
                minimum=1,
                maximum=MAX_CANDIDATES,
            ),
            expected_relevance_grades=tuple(
                bounded_int(
                    grade,
                    label="expected relevance grade",
                    minimum=1,
                    maximum=3,
                )
                for grade in raw_expected_grades
            ),
            expected_temporal_grade_counts=cast(
                tuple[int, int, int],
                tuple(
                    bounded_int(
                        count,
                        label="expected temporal grade count",
                        minimum=0,
                        maximum=MAX_ALTERNATIVES,
                    )
                    for count in raw_expected_temporal
                ),
            ),
            matched_facts=tuple(RecallMatchedFactV1.from_payload(item) for item in raw_matched_facts),
            coverage_target_scores=tuple(parsed_coverage_scores),
            metrics=_metric_items_from_payload(payload["metrics"]),
        )


def _validate_metric_items(values: tuple[tuple[str, MetricValueV1], ...]) -> None:
    if type(values) is not tuple or len(values) != len(METRIC_NAMES):
        raise RecallContractError("metric catalog is not closed")
    if any(
        type(item) is not tuple
        or len(item) != 2
        or type(item[0]) is not str
        or type(item[1]) is not MetricValueV1
        for item in values
    ):
        raise RecallContractError("metric catalog order or types are invalid")
    typed_values = cast(tuple[tuple[str, MetricValueV1], ...], values)
    if tuple(item[0] for item in typed_values) != METRIC_NAMES:
        raise RecallContractError("metric catalog order or types are invalid")
    try:
        validated = tuple(
            (name, MetricValueV1.from_payload(metric.to_payload())) for name, metric in typed_values
        )
        if validated != typed_values:
            raise RecallContractError("metric catalog is not canonical")
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError("metric catalog is malformed") from exc


def _metric_items_from_payload(value: object) -> tuple[tuple[str, MetricValueV1], ...]:
    if type(value) is not dict or frozenset(cast(dict[object, object], value)) != frozenset(METRIC_NAMES):
        raise RecallContractError("metric catalog keys are invalid")
    payload = cast(dict[str, object], value)
    return tuple((name, MetricValueV1.from_payload(payload[name])) for name in METRIC_NAMES)


def _metric_aggregate_from_results(
    results: tuple[RecallCaseResultV1, ...],
) -> RecallMetricAggregateV1:
    if not results or any(type(item) is not RecallCaseResultV1 for item in results):
        raise RecallContractError("metric aggregate requires typed case results")
    positive = tuple(item for item in results if not item.expected_no_hit)
    single_buckets: list[RecallNdcgAggregateBucketV1] = []
    rank_counts = [0] * 10
    for result in positive:
        if result.first_relevant_rank is not None and result.first_relevant_rank <= 10:
            rank_counts[result.first_relevant_rank - 1] += 1
        expected_counts = Counter(result.expected_relevance_grades)
        facts_by_rank = {fact.rank: fact for fact in result.matched_facts}
        rank_11_50_counts = [0] * 9
        rank_51_100_counts = [0] * 9
        for fact in result.matched_facts:
            if fact.rank <= 10:
                continue
            status_index = 0 if fact.temporal_correct is None else 2 if fact.temporal_correct else 1
            target = rank_11_50_counts if fact.rank <= 50 else rank_51_100_counts
            target[(fact.relevance_grade - 1) * 3 + status_index] += 1
        single_buckets.append(
            RecallNdcgAggregateBucketV1(
                expected_grade_counts=cast(
                    tuple[int, int, int],
                    tuple(expected_counts[grade] for grade in (1, 2, 3)),
                ),
                expected_temporal_grade_counts=result.expected_temporal_grade_counts,
                top_10_relevance_grades=tuple(
                    facts_by_rank[rank].relevance_grade if rank in facts_by_rank else 0
                    for rank in range(1, 11)
                ),
                top_10_temporal_correct=tuple(
                    facts_by_rank[rank].temporal_correct if rank in facts_by_rank else None
                    for rank in range(1, 11)
                ),
                rank_11_50_match_counts=tuple(rank_11_50_counts),
                rank_51_100_match_counts=tuple(rank_51_100_counts),
                absence_decision=result.absence_decision,
                coverage=_coverage_configuration_from_result(result),
                case_count=1,
            )
        )
    bucket_counts = Counter(item.canonical_key for item in single_buckets)
    bucket_templates = {item.canonical_key: item for item in single_buckets}
    ndcg_buckets = tuple(
        replace(bucket_templates[key], case_count=count) for key, count in sorted(bucket_counts.items())
    )
    dated = tuple(
        fact.temporal_correct
        for result in positive
        for fact in result.matched_facts
        if fact.temporal_correct is not None
    )
    return RecallMetricAggregateV1(
        expected_hit_case_count=len(positive),
        qrel_count=sum(len(item.expected_relevance_grades) for item in positive),
        recalled_at_50_count=sum(fact.rank <= 50 for item in positive for fact in item.matched_facts),
        recalled_at_100_count=sum(len(item.matched_facts) for item in positive),
        first_relevant_rank_counts=tuple(rank_counts),
        ndcg_sum_ppm=sum(
            _ranked_ndcg_ppm(item.expected_relevance_grades, item.matched_facts) for item in positive
        ),
        ndcg_buckets=ndcg_buckets,
        false_absence_count=sum(
            item.candidate_count == 0
            and item.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
            for item in positive
        ),
        dated_match_count=len(dated),
        dated_correct_count=sum(dated),
    )


def _sum_metric_aggregate_facts(
    values: Iterable[RecallMetricAggregateV1],
) -> RecallMetricAggregateV1:
    facts = tuple(values)
    if not facts or any(type(item) is not RecallMetricAggregateV1 for item in facts):
        raise RecallContractError("metric aggregate summation requires typed facts")
    ndcg_bucket_counts: Counter[tuple[object, ...]] = Counter()
    bucket_templates: dict[tuple[object, ...], RecallNdcgAggregateBucketV1] = {}
    for item in facts:
        for bucket in item.ndcg_buckets:
            ndcg_bucket_counts[bucket.canonical_key] += bucket.case_count
            bucket_templates[bucket.canonical_key] = bucket
    return RecallMetricAggregateV1(
        expected_hit_case_count=sum(item.expected_hit_case_count for item in facts),
        qrel_count=sum(item.qrel_count for item in facts),
        recalled_at_50_count=sum(item.recalled_at_50_count for item in facts),
        recalled_at_100_count=sum(item.recalled_at_100_count for item in facts),
        first_relevant_rank_counts=tuple(
            sum(item.first_relevant_rank_counts[index] for item in facts) for index in range(10)
        ),
        ndcg_sum_ppm=sum(item.ndcg_sum_ppm for item in facts),
        ndcg_buckets=tuple(
            replace(bucket_templates[key], case_count=count)
            for key, count in sorted(ndcg_bucket_counts.items())
        ),
        false_absence_count=sum(item.false_absence_count for item in facts),
        dated_match_count=sum(item.dated_match_count for item in facts),
        dated_correct_count=sum(item.dated_correct_count for item in facts),
    )


def _validate_aggregate_semantics(
    metrics: tuple[tuple[str, MetricValueV1], ...],
    metric_facts: RecallMetricAggregateV1,
    coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
    *,
    case_count: int,
    coverage_corpus: ArchiveSearchCorpus | None = None,
    coverage_taxonomy: RecallTaxonomyV1 | None = None,
) -> None:
    _validate_metric_items(metrics)
    if type(metric_facts) is not RecallMetricAggregateV1:
        raise RecallContractError("metric aggregate facts must be typed")
    try:
        validated_metric_facts = RecallMetricAggregateV1.from_payload(metric_facts.to_payload())
        if validated_metric_facts != metric_facts:
            raise RecallContractError("metric aggregate facts are not canonical")
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError("metric aggregate facts are malformed") from exc
    _validate_coverage_aggregate_items(coverage_facts)
    bounded_int(case_count, label="aggregate case count", minimum=1, maximum=MAX_CASES)
    if metric_facts.expected_hit_case_count > case_count:
        raise RecallContractError("aggregate expected-hit count exceeds case count")
    if metrics != metric_facts.metrics(coverage_facts):
        raise RecallContractError("aggregate metrics contradict sufficient facts")
    if coverage_corpus is not None and any(
        bucket.coverage.expected_corpus is not coverage_corpus for bucket in metric_facts.ndcg_buckets
    ):
        raise RecallContractError("metric coverage configurations contradict corpus scope")
    if coverage_taxonomy is not None and any(
        bucket.coverage.taxonomy is not coverage_taxonomy for bucket in metric_facts.ndcg_buckets
    ):
        raise RecallContractError("metric coverage configurations contradict taxonomy scope")
    coverage = dict(coverage_facts)
    for index, name in enumerate(_COVERAGE_METRIC_NAMES):
        if coverage_corpus is None:
            positive_target_count = sum(
                bucket.coverage.target_counts[index] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
            positive_unknown_count = sum(
                bucket.coverage.unknown_counts[index] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
            positive_score_sum = sum(
                bucket.coverage.score_sums_ppm[index] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
        else:
            positive_target_count = sum(
                dict(_expected_coverage_target_counts(coverage_corpus))[name] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
            positive_unknown_count = sum(
                bucket.coverage.expected_unknown_counts[index] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
            positive_score_sum = sum(
                bucket.coverage.expected_score_sums_ppm[index] * bucket.case_count
                for bucket in metric_facts.ndcg_buckets
            )
        total = coverage[name]
        residual_target = total.target_count - positive_target_count
        residual_unknown = total.unknown_count - positive_unknown_count
        residual_score = total.score_sum_ppm - positive_score_sum
        if (
            residual_target < 0
            or residual_unknown < 0
            or residual_unknown > residual_target
            or residual_score < 0
            or residual_score > (residual_target - residual_unknown) * _PPM
        ):
            raise RecallContractError("metric and coverage sufficient facts cannot share cases")
    coverage_target_count = sum(facts.target_count for _name, facts in coverage_facts)
    if not case_count <= coverage_target_count <= case_count * MAX_COVERAGES:
        raise RecallContractError("aggregate coverage target count escapes the closed case bound")
    coverage_targets = {name: facts.target_count for name, facts in coverage_facts}
    catalog_targets = coverage_targets["catalog_coverage"]
    passage_targets = coverage_targets["passage_coverage"]
    embedding_targets = coverage_targets["embedding_coverage"]
    message_targets = embedding_targets - catalog_targets
    if (
        not case_count <= embedding_targets <= case_count * len(ArchiveSearchCorpus)
        or catalog_targets > case_count * (len(ArchiveSearchCorpus) - 1)
        or not 0 <= message_targets <= case_count
        or passage_targets != 2 * embedding_targets - catalog_targets
    ):
        raise RecallContractError("aggregate coverage targets contradict the archive lane plan")


def _validate_residual_coverage_plan(
    coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
    *,
    case_count: int,
    expected_message_case_count: int,
) -> None:
    _validate_coverage_aggregate_items(coverage_facts)
    bounded_int(case_count, label="residual plan case count", minimum=1, maximum=MAX_CASES)
    bounded_int(
        expected_message_case_count,
        label="residual plan message case count",
        minimum=0,
        maximum=case_count,
    )
    targets = {name: facts.target_count for name, facts in coverage_facts}
    catalog_targets = targets["catalog_coverage"]
    passage_targets = targets["passage_coverage"]
    embedding_targets = targets["embedding_coverage"]
    message_targets = embedding_targets - catalog_targets
    if (
        passage_targets != 2 * embedding_targets - catalog_targets
        or not 0 <= message_targets <= case_count - expected_message_case_count
        or catalog_targets > 5 * case_count + expected_message_case_count
        or embedding_targets > (len(ArchiveSearchCorpus) - 1) * case_count
    ):
        raise RecallContractError("residual coverage targets contradict the archive lane plan")


def _coverage_aggregate_from_results(
    results: tuple[RecallCaseResultV1, ...],
    *,
    coverage_corpus: ArchiveSearchCorpus | None = None,
) -> tuple[tuple[str, RecallCoverageAggregateV1], ...]:
    search_corpus = None if coverage_corpus is None else _search_corpus_for_archive(coverage_corpus)
    values: list[tuple[str, RecallCoverageAggregateV1]] = []
    for name in _COVERAGE_METRIC_NAMES:
        score_facts = tuple(
            score_fact
            for result in results
            for score_fact in dict(result.coverage_target_scores)[name]
            if search_corpus is None or score_fact[0] is search_corpus
        )
        values.append(
            (
                name,
                RecallCoverageAggregateV1(
                    target_count=len(score_facts),
                    unknown_count=sum(score is None for _corpus, score in score_facts),
                    score_sum_ppm=sum(
                        cast(int, score) for _corpus, score in score_facts if score is not None
                    ),
                ),
            )
        )
    return tuple(values)


def _coverage_configuration_from_result(
    result: RecallCaseResultV1,
) -> RecallCoverageConfigurationV1:
    if type(result) is not RecallCaseResultV1:
        raise RecallContractError("coverage configuration requires a typed case result")
    catalog = dict(result.coverage_target_scores)
    expected_corpus = _search_corpus_for_archive(result.corpus)

    def vector(kind: str) -> tuple[int, int, int]:
        values: list[int] = []
        for name in _COVERAGE_METRIC_NAMES:
            facts = catalog[name]
            selected = (
                tuple(item for item in facts if item[0] is expected_corpus)
                if kind.startswith("expected_")
                else facts
            )
            if kind.endswith("targets"):
                values.append(len(selected))
            elif kind.endswith("unknown"):
                values.append(sum(score is None for _corpus, score in selected))
            else:
                values.append(sum(cast(int, score) for _corpus, score in selected if score is not None))
        return cast(tuple[int, int, int], tuple(values))

    return RecallCoverageConfigurationV1(
        taxonomy=result.taxonomy,
        expected_corpus=result.corpus,
        absence_oracle_ready=result.coverage_authorizes_absence,
        target_counts=vector("targets"),
        unknown_counts=vector("unknown"),
        score_sums_ppm=vector("scores"),
        expected_unknown_counts=vector("expected_unknown"),
        expected_score_sums_ppm=vector("expected_scores"),
    )


def _coverage_plan_aggregate_from_results(
    results: tuple[RecallCaseResultV1, ...],
) -> tuple[RecallCoveragePlanAggregateV1, ...]:
    if not results or any(type(item) is not RecallCaseResultV1 for item in results):
        raise RecallContractError("coverage plan aggregate requires typed case results")
    values: dict[tuple[object, ...], list[int]] = {}
    templates: dict[tuple[object, ...], RecallCoverageConfigurationV1] = {}
    for result in results:
        coverage = _coverage_configuration_from_result(result)
        key = coverage.canonical_key
        templates[key] = coverage
        counts = values.setdefault(key, [0, 0])
        counts[0] += 1
        counts[1] += int(
            not result.expected_no_hit
            and result.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
        )
    return tuple(
        RecallCoveragePlanAggregateV1(
            coverage=templates[key],
            case_count=counts[0],
            false_absence_count=counts[1],
        )
        for key, counts in sorted(values.items())
    )


def _validate_coverage_plan_aggregates(
    values: tuple[RecallCoveragePlanAggregateV1, ...],
    *,
    coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
    metric_facts: RecallMetricAggregateV1,
    case_count: int,
) -> None:
    if (
        type(values) is not tuple
        or not values
        or len(values) > MAX_CASES
        or any(type(item) is not RecallCoveragePlanAggregateV1 for item in values)
    ):
        raise RecallContractError("report coverage plan aggregates are invalid")
    try:
        validated = tuple(RecallCoveragePlanAggregateV1.from_payload(item.to_payload()) for item in values)
        if validated != values:
            raise RecallContractError("report coverage plan aggregate is not canonical")
    except RecallContractError:
        raise
    except Exception as exc:
        raise RecallContractError("report coverage plan aggregate is malformed") from exc
    keys = tuple(item.canonical_key for item in validated)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise RecallContractError("report coverage plan aggregates are not canonical")
    if sum(item.case_count for item in validated) != case_count:
        raise RecallContractError("report coverage plans do not cover the manifest")
    if sum(item.false_absence_count for item in validated) != metric_facts.false_absence_count:
        raise RecallContractError("report coverage plans contradict false absence facts")
    plan_case_counts = Counter({item.canonical_key: item.case_count for item in validated})
    plan_false_absence_counts = Counter({item.canonical_key: item.false_absence_count for item in validated})
    positive_case_counts: Counter[tuple[object, ...]] = Counter()
    false_absence_counts: Counter[tuple[object, ...]] = Counter()
    for bucket in metric_facts.ndcg_buckets:
        positive_case_counts[bucket.coverage.canonical_key] += bucket.case_count
        if bucket.false_absence:
            false_absence_counts[bucket.coverage.canonical_key] += bucket.case_count
    if (
        any(count > plan_case_counts[key] for key, count in positive_case_counts.items())
        or false_absence_counts != plan_false_absence_counts
    ):
        raise RecallContractError("report metric and coverage configurations contradict")
    coverage = dict(coverage_facts)
    for index, name in enumerate(_COVERAGE_METRIC_NAMES):
        target_count = sum(item.target_counts[index] * item.case_count for item in validated)
        unknown_count = sum(item.unknown_counts[index] * item.case_count for item in validated)
        score_sum_ppm = sum(item.score_sums_ppm[index] * item.case_count for item in validated)
        facts = coverage[name]
        if (
            facts.target_count != target_count
            or facts.unknown_count != unknown_count
            or facts.score_sum_ppm != score_sum_ppm
        ):
            raise RecallContractError("report coverage facts contradict plan or confirmed absence")


def _validate_coverage_plan_breakdown_allocations(
    values: tuple[RecallCoveragePlanAggregateV1, ...],
    *,
    per_taxonomy: tuple[RecallBreakdownV1, ...],
    per_corpus: tuple[RecallBreakdownV1, ...],
    off_expected_coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
) -> None:
    _validate_coverage_aggregate_items(off_expected_coverage_facts)
    for breakdown in per_taxonomy:
        taxonomy = RecallTaxonomyV1(breakdown.label)
        selected = tuple(item for item in values if item.coverage.taxonomy is taxonomy)
        if sum(item.case_count for item in selected) != breakdown.case_count:
            raise RecallContractError("coverage plans contradict taxonomy counts")
        facts = dict(breakdown.coverage_facts)
        for index, name in enumerate(_COVERAGE_METRIC_NAMES):
            if (
                facts[name].target_count
                != sum(item.target_counts[index] * item.case_count for item in selected)
                or facts[name].unknown_count
                != sum(item.unknown_counts[index] * item.case_count for item in selected)
                or facts[name].score_sum_ppm
                != sum(item.score_sums_ppm[index] * item.case_count for item in selected)
            ):
                raise RecallContractError("coverage plans contradict taxonomy facts")
    for breakdown in per_corpus:
        corpus = ArchiveSearchCorpus(breakdown.label)
        selected = tuple(item for item in values if item.coverage.expected_corpus is corpus)
        if sum(item.case_count for item in selected) != breakdown.case_count:
            raise RecallContractError("coverage plans contradict expected corpus counts")
        expected_targets = tuple(count for _name, count in _expected_coverage_target_counts(corpus))
        facts = dict(breakdown.coverage_facts)
        for index, name in enumerate(_COVERAGE_METRIC_NAMES):
            if (
                facts[name].target_count
                != sum(expected_targets[index] * item.case_count for item in selected)
                or facts[name].unknown_count
                != sum(item.coverage.expected_unknown_counts[index] * item.case_count for item in selected)
                or facts[name].score_sum_ppm
                != sum(item.coverage.expected_score_sums_ppm[index] * item.case_count for item in selected)
            ):
                raise RecallContractError("coverage plans contradict expected-corpus facts")
    residual = dict(off_expected_coverage_facts)
    for index, name in enumerate(_COVERAGE_METRIC_NAMES):
        expected_targets_by_plan = {
            item.canonical_key: dict(_expected_coverage_target_counts(item.coverage.expected_corpus))[name]
            for item in values
        }
        if (
            residual[name].target_count
            != sum(
                (item.target_counts[index] - expected_targets_by_plan[item.canonical_key]) * item.case_count
                for item in values
            )
            or residual[name].unknown_count
            != sum(
                (item.unknown_counts[index] - item.coverage.expected_unknown_counts[index]) * item.case_count
                for item in values
            )
            or residual[name].score_sum_ppm
            != sum(
                (item.score_sums_ppm[index] - item.coverage.expected_score_sums_ppm[index]) * item.case_count
                for item in values
            )
        ):
            raise RecallContractError("coverage plans contradict off-expected facts")


def _sum_coverage_aggregate_items(
    values: Iterable[tuple[tuple[str, RecallCoverageAggregateV1], ...]],
) -> tuple[tuple[str, RecallCoverageAggregateV1], ...]:
    catalogs = tuple(values)
    if not catalogs:
        raise RecallContractError("coverage aggregate summation requires at least one catalog")
    for catalog in catalogs:
        _validate_coverage_aggregate_items(catalog)
    return tuple(
        (
            name,
            RecallCoverageAggregateV1(
                target_count=sum(dict(catalog)[name].target_count for catalog in catalogs),
                unknown_count=sum(dict(catalog)[name].unknown_count for catalog in catalogs),
                score_sum_ppm=sum(dict(catalog)[name].score_sum_ppm for catalog in catalogs),
            ),
        )
        for name in _COVERAGE_METRIC_NAMES
    )


def _subtract_coverage_aggregate_items(
    total: tuple[tuple[str, RecallCoverageAggregateV1], ...],
    included: tuple[tuple[str, RecallCoverageAggregateV1], ...],
) -> tuple[tuple[str, RecallCoverageAggregateV1], ...]:
    _validate_coverage_aggregate_items(total)
    _validate_coverage_aggregate_items(included)
    total_values = dict(total)
    included_values = dict(included)
    result: list[tuple[str, RecallCoverageAggregateV1]] = []
    for name in _COVERAGE_METRIC_NAMES:
        outer = total_values[name]
        inner = included_values[name]
        if (
            inner.target_count > outer.target_count
            or inner.unknown_count > outer.unknown_count
            or inner.score_sum_ppm > outer.score_sum_ppm
        ):
            raise RecallContractError("coverage aggregate subset exceeds its total")
        result.append(
            (
                name,
                RecallCoverageAggregateV1(
                    target_count=outer.target_count - inner.target_count,
                    unknown_count=outer.unknown_count - inner.unknown_count,
                    score_sum_ppm=outer.score_sum_ppm - inner.score_sum_ppm,
                ),
            )
        )
    return tuple(result)


def _aggregate_case_results(
    values: Iterable[RecallCaseResultV1],
    *,
    coverage_corpus: ArchiveSearchCorpus | None = None,
) -> tuple[tuple[str, MetricValueV1], ...]:
    results = tuple(values)
    if not results:
        raise RecallContractError("metric aggregation requires at least one case")
    if any(type(item) is not RecallCaseResultV1 for item in results):
        raise RecallContractError("metric aggregation requires typed case results")
    metric_facts = _metric_aggregate_from_results(results)
    coverage_facts = _coverage_aggregate_from_results(
        results,
        coverage_corpus=coverage_corpus,
    )
    return metric_facts.metrics(coverage_facts)


@dataclass(frozen=True, slots=True)
class RecallCaseBindingV1:
    """The only per-case material retained in a public recall report."""

    case_id: str
    case_sha256: str
    observation_sha256: str

    def __post_init__(self) -> None:
        _opaque_case_id(self.case_id)
        sha256_text(self.case_sha256, label="report case digest")
        sha256_text(self.observation_sha256, label="report observation digest")

    @classmethod
    def from_result(cls, result: RecallCaseResultV1) -> RecallCaseBindingV1:
        if type(result) is not RecallCaseResultV1:
            raise RecallContractError("report binding requires a typed case result")
        return cls(result.case_id, result.case_sha256, result.observation_sha256)

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "case_sha256": self.case_sha256,
            "observation_sha256": self.observation_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> RecallCaseBindingV1:
        payload = exact_object(
            value,
            frozenset({"case_id", "case_sha256", "observation_sha256"}),
            label="recall report case binding",
        )
        return cls(
            case_id=_opaque_case_id(payload["case_id"]),
            case_sha256=sha256_text(payload["case_sha256"], label="report case digest"),
            observation_sha256=sha256_text(
                payload["observation_sha256"],
                label="report observation digest",
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class RecallReportV1:
    evidence_source: RecallEvidenceSourceV1
    release_sha256: str
    case_manifest_sha256: str
    observation_manifest_sha256: str
    case_count: int
    metric_facts: RecallMetricAggregateV1
    metrics: tuple[tuple[str, MetricValueV1], ...]
    coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...]
    coverage_plan_facts: tuple[RecallCoveragePlanAggregateV1, ...]
    off_expected_coverage_facts: tuple[tuple[str, RecallCoverageAggregateV1], ...]
    per_taxonomy: tuple[RecallBreakdownV1, ...]
    per_corpus: tuple[RecallBreakdownV1, ...]
    cases: tuple[RecallCaseBindingV1, ...]
    report_sha256: str

    def __post_init__(self) -> None:
        if type(self.evidence_source) is not RecallEvidenceSourceV1:
            raise RecallContractError("report evidence source must be explicit")
        sha256_text(self.release_sha256, label="report release digest")
        sha256_text(self.case_manifest_sha256, label="case manifest digest")
        sha256_text(self.observation_manifest_sha256, label="observation manifest digest")
        bounded_int(self.case_count, label="report case count", minimum=1, maximum=MAX_CASES)
        _validate_metric_items(self.metrics)
        _validate_coverage_aggregate_items(self.coverage_facts)
        _validate_coverage_aggregate_items(self.off_expected_coverage_facts)
        _validate_aggregate_semantics(
            self.metrics,
            self.metric_facts,
            self.coverage_facts,
            case_count=self.case_count,
        )
        _validate_coverage_plan_aggregates(
            self.coverage_plan_facts,
            coverage_facts=self.coverage_facts,
            metric_facts=self.metric_facts,
            case_count=self.case_count,
        )
        for label, values in (
            ("taxonomy", self.per_taxonomy),
            ("corpus", self.per_corpus),
        ):
            if (
                type(values) is not tuple
                or not values
                or len(values) > MAX_BREAKDOWNS
                or any(type(item) is not RecallBreakdownV1 for item in values)
            ):
                raise RecallContractError(f"report {label} breakdown is not canonical")
            try:
                validated = tuple(RecallBreakdownV1.from_payload(item.to_payload()) for item in values)
                if validated != values:
                    raise RecallContractError(f"report {label} breakdown is not canonical")
                labels = tuple(item.label for item in validated)
            except RecallContractError:
                raise
            except Exception as exc:
                raise RecallContractError(f"report {label} breakdown is malformed") from exc
            if labels != tuple(sorted(labels)) or len(labels) != len(set(labels)):
                raise RecallContractError(f"report {label} breakdown is not canonical")
        if not {item.label for item in self.per_taxonomy} <= {item.value for item in RecallTaxonomyV1}:
            raise RecallContractError("report taxonomy breakdown has an unknown label")
        if not {item.label for item in self.per_corpus} <= {item.value for item in ArchiveSearchCorpus}:
            raise RecallContractError("report corpus breakdown has an unknown label")
        for breakdown in self.per_corpus:
            expected_targets = dict(_expected_coverage_target_counts(ArchiveSearchCorpus(breakdown.label)))
            observed_facts = dict(breakdown.coverage_facts)
            if any(
                observed_facts[name].target_count != breakdown.case_count * expected_targets[name]
                for name in _COVERAGE_METRIC_NAMES
            ):
                raise RecallContractError("corpus coverage targets contradict the shipped lane plan")
            if any(
                observed_facts[name].unknown_count
                > observed_facts[name].target_count
                - breakdown.metric_facts.false_absence_count * expected_targets[name]
                or observed_facts[name].score_sum_ppm
                < breakdown.metric_facts.false_absence_count * expected_targets[name] * _PPM
                for name in _COVERAGE_METRIC_NAMES
            ):
                raise RecallContractError("false absence contradicts expected-corpus coverage")
        if (
            sum(item.case_count for item in self.per_taxonomy) != self.case_count
            or sum(item.case_count for item in self.per_corpus) != self.case_count
        ):
            raise RecallContractError("report breakdown counts do not cover the manifest")
        expected_message_case_count = sum(
            item.case_count for item in self.per_corpus if item.label == ArchiveSearchCorpus.MESSAGES.value
        )
        _validate_coverage_plan_breakdown_allocations(
            self.coverage_plan_facts,
            per_taxonomy=self.per_taxonomy,
            per_corpus=self.per_corpus,
            off_expected_coverage_facts=self.off_expected_coverage_facts,
        )
        _validate_residual_coverage_plan(
            self.off_expected_coverage_facts,
            case_count=self.case_count,
            expected_message_case_count=expected_message_case_count,
        )
        if self.metric_facts != _sum_metric_aggregate_facts(item.metric_facts for item in self.per_taxonomy):
            raise RecallContractError("report metric facts contradict taxonomy aggregates")
        if self.metric_facts != _sum_metric_aggregate_facts(item.metric_facts for item in self.per_corpus):
            raise RecallContractError("report metric facts contradict corpus aggregates")
        metric_catalog = dict(self.metrics)
        if any(metric_catalog[name] != facts.metric for name, facts in self.coverage_facts):
            raise RecallContractError("report coverage metrics contradict aggregate facts")
        if self.coverage_facts != _sum_coverage_aggregate_items(
            item.coverage_facts for item in self.per_taxonomy
        ):
            raise RecallContractError("report coverage facts contradict taxonomy aggregates")
        corpus_coverage_facts = _sum_coverage_aggregate_items(item.coverage_facts for item in self.per_corpus)
        if self.coverage_facts != _sum_coverage_aggregate_items(
            (corpus_coverage_facts, self.off_expected_coverage_facts)
        ):
            raise RecallContractError("report coverage facts contradict corpus aggregates")
        if (
            type(self.cases) is not tuple
            or len(self.cases) != self.case_count
            or any(type(item) is not RecallCaseBindingV1 for item in self.cases)
        ):
            raise RecallContractError("report case bindings do not match case_count")
        try:
            validated_cases = tuple(
                RecallCaseBindingV1.from_payload(item.to_payload()) for item in self.cases
            )
            if validated_cases != self.cases:
                raise RecallContractError("report case binding is not canonical")
        except RecallContractError:
            raise
        except Exception as exc:
            raise RecallContractError("report case binding is malformed") from exc
        case_ids = tuple(item.case_id for item in self.cases)
        if case_ids != tuple(sorted(case_ids)) or len(case_ids) != len(set(case_ids)):
            raise RecallContractError("report cases must be sorted and unique")
        case_digests = tuple(sorted(item.case_sha256 for item in self.cases))
        observation_digests = tuple(sorted(item.observation_sha256 for item in self.cases))
        if len(case_digests) != len(set(case_digests)) or len(observation_digests) != len(
            set(observation_digests)
        ):
            raise RecallContractError("report case or observation digests collide")
        if self.case_manifest_sha256 != canonical_manifest_sha256(
            b"friday/retrieval-recall-case-manifest/v1",
            case_digests,
        ) or self.observation_manifest_sha256 != canonical_manifest_sha256(
            b"friday/retrieval-recall-observation-manifest/v1",
            observation_digests,
        ):
            raise RecallContractError("report manifest digest is forged")
        sha256_text(self.report_sha256, label="report digest")
        if not hmac.compare_digest(self.report_sha256, self._computed_sha256()):
            raise RecallContractError("report digest is forged")
        if len(self.to_json().encode("ascii")) > MAX_CONTRACT_BYTES:
            raise RecallContractError("recall report exceeds its byte bound")

    def __repr__(self) -> str:
        return f"RecallReportV1(case_count={self.case_count}, body_free=True)"

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "case_manifest_sha256": self.case_manifest_sha256,
            "cases": [item.to_payload() for item in self.cases],
            "coverage_facts": {name: facts.to_payload() for name, facts in self.coverage_facts},
            "coverage_plan_facts": [item.to_payload() for item in self.coverage_plan_facts],
            "evidence_source": self.evidence_source.value,
            "metric_facts": self.metric_facts.to_payload(),
            "metrics": {name: metric.to_payload() for name, metric in self.metrics},
            "observation_manifest_sha256": self.observation_manifest_sha256,
            "off_expected_coverage_facts": {
                name: facts.to_payload() for name, facts in self.off_expected_coverage_facts
            },
            "per_corpus": [item.to_payload() for item in self.per_corpus],
            "per_taxonomy": [item.to_payload() for item in self.per_taxonomy],
            "release_sha256": self.release_sha256,
            "schema": RECALL_REPORT_SCHEMA,
        }

    def _computed_sha256(self) -> str:
        return digest_payload(b"friday/retrieval-recall-report/v1", self._payload_without_digest())

    @classmethod
    def create(
        cls,
        *,
        evidence_source: RecallEvidenceSourceV1,
        release_sha256: str,
        case_manifest_sha256: str,
        observation_manifest_sha256: str,
        metrics: tuple[tuple[str, MetricValueV1], ...],
        per_taxonomy: Iterable[RecallBreakdownV1],
        per_corpus: Iterable[RecallBreakdownV1],
        cases: Iterable[RecallCaseResultV1],
    ) -> RecallReportV1:
        raw_taxonomy = _bounded_tuple(
            per_taxonomy,
            maximum=MAX_BREAKDOWNS,
            label="taxonomy breakdown",
        )
        raw_corpus = _bounded_tuple(
            per_corpus,
            maximum=MAX_BREAKDOWNS,
            label="corpus breakdown",
        )
        raw_cases = _bounded_tuple(cases, maximum=MAX_CASES, label="report case results")
        if any(type(item) is not RecallBreakdownV1 for item in (*raw_taxonomy, *raw_corpus)) or any(
            type(item) is not RecallCaseResultV1 for item in raw_cases
        ):
            raise RecallContractError("report factory requires typed facts")
        _validate_metric_items(metrics)
        if type(evidence_source) is not RecallEvidenceSourceV1:
            raise RecallContractError("report evidence source must be explicit")
        taxonomy_values = tuple(
            sorted(cast(tuple[RecallBreakdownV1, ...], raw_taxonomy), key=lambda item: item.label)
        )
        corpus_values = tuple(
            sorted(cast(tuple[RecallBreakdownV1, ...], raw_corpus), key=lambda item: item.label)
        )
        case_results = tuple(
            sorted(cast(tuple[RecallCaseResultV1, ...], raw_cases), key=lambda item: item.case_id)
        )
        metric_values = _metric_aggregate_from_results(case_results)
        if metrics != _aggregate_case_results(case_results):
            raise RecallContractError("aggregate report metrics contradict scored case facts")
        expected_taxonomy = tuple(
            RecallBreakdownV1.create(label=label, cases=items)
            for label, items in sorted(
                (
                    label,
                    tuple(item for item in case_results if item.taxonomy.value == label),
                )
                for label in {item.taxonomy.value for item in case_results}
            )
        )
        if taxonomy_values != expected_taxonomy:
            raise RecallContractError("taxonomy breakdown contradicts scored case facts")
        expected_corpus = tuple(
            RecallBreakdownV1.create(
                label=label,
                cases=items,
                coverage_corpus=ArchiveSearchCorpus(label),
            )
            for label, items in sorted(
                (
                    label,
                    tuple(item for item in case_results if item.corpus.value == label),
                )
                for label in {item.corpus.value for item in case_results}
            )
        )
        if corpus_values != expected_corpus:
            raise RecallContractError("corpus breakdown contradicts scored case facts")
        case_values = tuple(RecallCaseBindingV1.from_result(item) for item in case_results)
        coverage_values = _coverage_aggregate_from_results(case_results)
        coverage_plan_values = _coverage_plan_aggregate_from_results(case_results)
        corpus_coverage_values = _sum_coverage_aggregate_items(item.coverage_facts for item in corpus_values)
        off_expected_coverage_values = _subtract_coverage_aggregate_items(
            coverage_values,
            corpus_coverage_values,
        )
        base: dict[str, object] = {
            "case_count": len(case_values),
            "case_manifest_sha256": sha256_text(case_manifest_sha256, label="case manifest digest"),
            "cases": [item.to_payload() for item in case_values],
            "coverage_facts": {name: facts.to_payload() for name, facts in coverage_values},
            "coverage_plan_facts": [item.to_payload() for item in coverage_plan_values],
            "evidence_source": evidence_source.value,
            "metric_facts": metric_values.to_payload(),
            "metrics": {name: metric.to_payload() for name, metric in metrics},
            "observation_manifest_sha256": sha256_text(
                observation_manifest_sha256,
                label="observation manifest digest",
            ),
            "off_expected_coverage_facts": {
                name: facts.to_payload() for name, facts in off_expected_coverage_values
            },
            "per_corpus": [item.to_payload() for item in corpus_values],
            "per_taxonomy": [item.to_payload() for item in taxonomy_values],
            "release_sha256": sha256_text(release_sha256, label="report release digest"),
            "schema": RECALL_REPORT_SCHEMA,
        }
        return cls(
            evidence_source=evidence_source,
            release_sha256=cast(str, base["release_sha256"]),
            case_manifest_sha256=cast(str, base["case_manifest_sha256"]),
            observation_manifest_sha256=cast(str, base["observation_manifest_sha256"]),
            case_count=len(case_results),
            metric_facts=metric_values,
            metrics=metrics,
            coverage_facts=coverage_values,
            coverage_plan_facts=coverage_plan_values,
            off_expected_coverage_facts=off_expected_coverage_values,
            per_taxonomy=taxonomy_values,
            per_corpus=corpus_values,
            cases=case_values,
            report_sha256=digest_payload(b"friday/retrieval-recall-report/v1", base),
        )

    def to_payload(self) -> dict[str, object]:
        return {**self._payload_without_digest(), "report_sha256": self.report_sha256}

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, value: object) -> RecallReportV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "case_count",
                    "case_manifest_sha256",
                    "cases",
                    "coverage_facts",
                    "coverage_plan_facts",
                    "evidence_source",
                    "metric_facts",
                    "metrics",
                    "observation_manifest_sha256",
                    "off_expected_coverage_facts",
                    "per_corpus",
                    "per_taxonomy",
                    "release_sha256",
                    "report_sha256",
                    "schema",
                }
            ),
            label="recall report",
        )
        if payload["schema"] != RECALL_REPORT_SCHEMA:
            raise RecallContractError("recall report schema is unsupported")
        raw_taxonomy = payload["per_taxonomy"]
        raw_corpus = payload["per_corpus"]
        raw_cases = payload["cases"]
        raw_coverage_plans = payload["coverage_plan_facts"]
        if any(
            type(value) is not list
            for value in (
                raw_taxonomy,
                raw_corpus,
                raw_cases,
                raw_coverage_plans,
            )
        ):
            raise RecallContractError("report collections must be arrays")
        taxonomy_values = cast(list[object], raw_taxonomy)
        corpus_values = cast(list[object], raw_corpus)
        case_values = cast(list[object], raw_cases)
        coverage_plan_values = cast(list[object], raw_coverage_plans)
        return cls(
            evidence_source=_enum(
                RecallEvidenceSourceV1,
                payload["evidence_source"],
                label="report evidence source",
            ),
            release_sha256=sha256_text(payload["release_sha256"], label="report release digest"),
            case_manifest_sha256=sha256_text(payload["case_manifest_sha256"], label="case manifest digest"),
            observation_manifest_sha256=sha256_text(
                payload["observation_manifest_sha256"], label="observation manifest digest"
            ),
            case_count=bounded_int(
                payload["case_count"], label="report case count", minimum=1, maximum=MAX_CASES
            ),
            metric_facts=RecallMetricAggregateV1.from_payload(payload["metric_facts"]),
            metrics=_metric_items_from_payload(payload["metrics"]),
            coverage_facts=_coverage_aggregate_items_from_payload(payload["coverage_facts"]),
            coverage_plan_facts=tuple(
                RecallCoveragePlanAggregateV1.from_payload(item) for item in coverage_plan_values
            ),
            off_expected_coverage_facts=_coverage_aggregate_items_from_payload(
                payload["off_expected_coverage_facts"]
            ),
            per_taxonomy=tuple(RecallBreakdownV1.from_payload(item) for item in taxonomy_values),
            per_corpus=tuple(RecallBreakdownV1.from_payload(item) for item in corpus_values),
            cases=tuple(RecallCaseBindingV1.from_payload(item) for item in case_values),
            report_sha256=sha256_text(payload["report_sha256"], label="report digest"),
        )

    @classmethod
    def parse(cls, value: str | bytes) -> RecallReportV1:
        result = cls.from_payload(parse_canonical_json(value, label="recall report"))
        text = value.decode("ascii") if type(value) is bytes else value
        if text != result.to_json():
            raise RecallContractError("recall report is not semantically canonical")
        return result


def case_manifest_sha256(cases: Iterable[RecallCaseV1]) -> str:
    raw_values = _bounded_tuple(cases, maximum=MAX_CASES, label="case manifest")
    if any(type(item) is not RecallCaseV1 for item in raw_values):
        raise RecallContractError("case manifest requires typed cases")
    values = tuple(sorted(cast(tuple[RecallCaseV1, ...], raw_values), key=lambda item: item.case_id))
    if not values or len(values) > MAX_CASES or any(type(item) is not RecallCaseV1 for item in values):
        raise RecallContractError("case manifest exceeds its closed item contract")
    if len({item.case_id for item in values}) != len(values):
        raise RecallContractError("case manifest contains duplicate case IDs")
    if len({item.privacy_key_hex for item in values}) != len(values):
        raise RecallContractError("case manifest privacy keys must be unique")
    return canonical_manifest_sha256(
        b"friday/retrieval-recall-case-manifest/v1",
        tuple(sorted(item.canonical_sha256 for item in values)),
    )


def observation_manifest_sha256(observations: Iterable[RecallObservationV1]) -> str:
    raw_values = _bounded_tuple(observations, maximum=MAX_CASES, label="observation manifest")
    if any(type(item) is not RecallObservationV1 for item in raw_values):
        raise RecallContractError("observation manifest requires typed observations")
    values = tuple(sorted(cast(tuple[RecallObservationV1, ...], raw_values), key=lambda item: item.case_id))
    if not values or len(values) > MAX_CASES or any(type(item) is not RecallObservationV1 for item in values):
        raise RecallContractError("observation manifest exceeds its closed item contract")
    if len({item.case_id for item in values}) != len(values):
        raise RecallContractError("observation manifest contains duplicate case IDs")
    return canonical_manifest_sha256(
        b"friday/retrieval-recall-observation-manifest/v1",
        tuple(sorted(item.observation_sha256 for item in values)),
    )


__all__ = [
    "MAX_ALTERNATIVES",
    "MAX_CANDIDATES",
    "MAX_CASES",
    "METRIC_NAMES",
    "MetricStatusV1",
    "MetricValueV1",
    "RECALL_CASE_SCHEMA",
    "RECALL_OBSERVATION_SCHEMA",
    "RECALL_REPORT_SCHEMA",
    "RecallAlternativeV1",
    "RecallBreakdownV1",
    "RecallCandidateV1",
    "RecallCaseBindingV1",
    "RecallCaseResultV1",
    "RecallCaseV1",
    "RecallContractError",
    "RecallCoverageConfigurationV1",
    "RecallCoverageV1",
    "RecallCoverageAggregateV1",
    "RecallCoveragePlanAggregateV1",
    "RecallEvidenceSourceV1",
    "RecallMatchedFactV1",
    "RecallMetricAggregateV1",
    "RecallNdcgAggregateBucketV1",
    "RecallObservationV1",
    "RecallOutcomeV1",
    "RecallReportV1",
    "RecallTaxonomyV1",
    "case_manifest_sha256",
    "coverage_absence_oracle",
    "observation_manifest_sha256",
    "opaque_case_identity",
    "opaque_passage_window_identity",
    "opaque_source_identity",
]
