"""Body-free evidence contract for the sole ``archive_search`` cutover.

This module is deliberately offline.  It combines the already shipped archive
parity report with the closed R8D/R8E adapter identities and records every
contour which a later shared-runtime cutover must prove.  It never routes a
turn, changes a tool catalogue, or treats the presence of a contract as runtime
parity.

The historical R8 failure inventory is represented by file/count groups and a
digest of the exact 60 node IDs.  Parameter values from those node IDs are not
copied into the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from friday.retrieval.memory_exact_internal import MEMORY_EXACT_ADAPTER_BINDING
from friday.retrieval.message_exact_internal import MESSAGE_EXACT_ADAPTER_BINDING
from friday.retrieval_benchmark._canonical import (
    RecallContractError,
    canonical_json,
    digest_payload,
    exact_object,
    parse_canonical_json,
    sha256_text,
)
from friday.retrieval_benchmark.parity import ParityReportV1
from friday.retrieval_benchmark.release import (
    RecallReleaseIdentityError,
    archive_search_release_sha256,
)

CUTOVER_REPORT_SCHEMA: Final = "friday.archive-search-cutover-readiness.body-free.v1"
CUTOVER_CASE_SCHEMA: Final = "friday.archive-search-cutover-case.body-free.v1"
HISTORICAL_FAILURE_GROUP_SCHEMA: Final = "friday.archive-search-cutover-failure-group.v1"
CUTOVER_CASE_MANIFEST_SHA256: Final = "0e235d31ad7e902d2483b70f8896d8aa953096b87d99587067780165630fe499"

HISTORICAL_CANDIDATE_SHA: Final = "7848cc45ad8ddda3702b1aa560d1d42d5dea2acc"
HISTORICAL_BASE_SHA: Final = "9928e83d26061cc3df1198815ca9ac9f4481080f"
HISTORICAL_FAILURE_COUNT: Final = 60
HISTORICAL_NODE_MANIFEST_SHA256: Final = "e1d8d50860ad84ee3a117d48171af560f47a52750dd2d01ded1460ef792ef8d2"
MESSAGE_FOUNDATION_MEASUREMENT_SHA: Final = "2fa079eb4de1d33535798e24552f85db3b9ccfd2"
MEMORY_FOUNDATION_REVIEWED_SHA: Final = "f44c4e7c2f4a693bcaac91c4a9861fa6e8eef13b"
MEMORY_FOUNDATION_REVIEW_STATUS: Final = "accepted"
R8C_CASE_MANIFEST_SHA256: Final = "20cc50b2676da16da0246ea2899211c8388a02347c81d86e50b8de21fc25f3c5"
R8C_MEASUREMENT_SHA256: Final = "160795299c001efc5f1f0cc322c9275646d978ce5f13fe653339fe6dde40bcf4"

_TOKEN = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_TEST_FILE = re.compile(r"tests/[a-zA-Z0-9_./-]+\.py\Z")
_TEST_NODE = re.compile(r"tests/[a-zA-Z0-9_./-]+\.py::[a-zA-Z0-9_\[\].:-]+\Z")
_REPO_FILE = re.compile(r"(?:friday|tests|tools)/[a-zA-Z0-9_./-]+\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class CutoverReadinessError(RecallContractError):
    """The readiness evidence is open, ambiguous, or self-contradictory."""


class CutoverContour(StrEnum):
    ARCHIVED_SOURCE = "archived_source"
    CURRENT_FILE = "current_file"
    FALLBACK = "fallback"
    FINAL_REAUTHORIZATION = "final_reauthorization"
    FOLLOW_UP = "follow_up"
    MEMORY_GRAPH = "memory_graph"
    MEMORY_TEMPORAL = "memory_temporal_as_of_known_at"
    MESSAGE_TOPIC = "message_topic"
    MESSAGE_WINDOW = "message_window"
    RESTART = "restart"
    SCALAR = "scalar"
    STALE_LEGACY_CALL = "stale_legacy_call"
    V12 = "v12"


class CutoverEvidenceStatus(StrEnum):
    PARITY = "parity"
    PRESERVED = "preserved"
    CONTRACT_ONLY = "contract_only"
    MISMATCH = "mismatch"
    UNMEASURED = "unmeasured"


class HistoricalFailureClass(StrEnum):
    CLASSIFIER_JSON = "classifier_json"
    LEGACY_ADAPTER_RETIREMENT = "legacy_adapter_retirement"
    SMALL_TALK_CATALOG = "small_talk_catalog"
    TOOL_TURN_ADMISSION = "tool_turn_admission"
    TRANSPORT_CAPABILITY = "transport_capability"


def _token(value: object, *, label: str) -> str:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise CutoverReadinessError(f"{label} is invalid")
    return value


def _enum(enum_type: type[StrEnum], value: object, *, label: str) -> StrEnum:
    if type(value) is not str:
        raise CutoverReadinessError(f"{label} is invalid")
    try:
        member = enum_type(value)
    except ValueError:
        raise CutoverReadinessError(f"{label} is invalid") from None
    return member


def _count(value: object, *, label: str, low: int = 0, high: int = 100_000) -> int:
    if type(value) is not int or not low <= value <= high:
        raise CutoverReadinessError(f"{label} is invalid")
    return value


def _sha256(value: object, *, label: str) -> str:
    try:
        return sha256_text(value, label=label)
    except RecallContractError as exc:
        raise CutoverReadinessError(str(exc)) from None


def _has_noncanonical_path_segment(value: str) -> bool:
    return any(segment in {"", ".", ".."} for segment in value.split("/"))


def _test_file(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 240
        or _TEST_FILE.fullmatch(value) is None
        or _has_noncanonical_path_segment(value)
    ):
        raise CutoverReadinessError("historical test file is invalid")
    return value


def _repo_file(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 240
        or _REPO_FILE.fullmatch(value) is None
        or _has_noncanonical_path_segment(value)
    ):
        raise CutoverReadinessError("required shared file is invalid")
    return value


def _test_node(value: object) -> str:
    if (
        type(value) is not str
        or len(value) > 512
        or _TEST_NODE.fullmatch(value) is None
        or _has_noncanonical_path_segment(value.split("::", 1)[0])
    ):
        raise CutoverReadinessError("executable node is invalid")
    return value


def _canonical_tokens(values: object, *, label: str, maximum: int = 32) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise CutoverReadinessError(f"{label} must be one closed collection")
    result = tuple(_token(item, label=label) for item in cast(tuple[object, ...] | list[object], values))
    if not result or len(result) > maximum or result != tuple(sorted(set(result))):
        raise CutoverReadinessError(f"{label} must be sorted, unique, and bounded")
    return result


def _canonical_optional_tokens(values: object, *, label: str, maximum: int = 32) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise CutoverReadinessError(f"{label} must be one closed collection")
    result = tuple(_token(item, label=label) for item in cast(tuple[object, ...] | list[object], values))
    if len(result) > maximum or result != tuple(sorted(set(result))):
        raise CutoverReadinessError(f"{label} must be sorted, unique, and bounded")
    return result


def _canonical_repo_files(values: object) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise CutoverReadinessError("required shared files must be one closed collection")
    result = tuple(_repo_file(item) for item in cast(tuple[object, ...] | list[object], values))
    if len(result) > 16 or result != tuple(sorted(set(result))):
        raise CutoverReadinessError("required shared files must be sorted, unique, and bounded")
    return result


def _canonical_nodes(values: object) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise CutoverReadinessError("executable nodes must be one closed collection")
    result = tuple(_test_node(item) for item in cast(tuple[object, ...] | list[object], values))
    if not result or len(result) > 16 or result != tuple(sorted(set(result))):
        raise CutoverReadinessError("executable nodes must be sorted, unique, and bounded")
    return result


def _canonical_digests(values: object) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise CutoverReadinessError("binding digests must be one closed collection")
    result = tuple(
        _sha256(item, label="cutover binding digest")
        for item in cast(tuple[object, ...] | list[object], values)
    )
    if len(result) > 8 or result != tuple(sorted(set(result))):
        raise CutoverReadinessError("binding digests must be sorted, unique, and bounded")
    return result


@dataclass(frozen=True, slots=True)
class HistoricalFailureGroupV1:
    test_file: str
    failed_nodes: int
    failure_class: HistoricalFailureClass
    closed_foundations: tuple[str, ...]
    remaining_requirement_codes: tuple[str, ...]
    node_manifest_sha256: str

    def __post_init__(self) -> None:
        if type(self.closed_foundations) is not tuple or type(self.remaining_requirement_codes) is not tuple:
            raise CutoverReadinessError("historical failure group collections must be immutable")
        _test_file(self.test_file)
        _count(self.failed_nodes, label="historical failed-node count", low=1, high=60)
        if type(self.failure_class) is not HistoricalFailureClass:
            raise CutoverReadinessError("historical failure class is invalid")
        _canonical_tokens(self.closed_foundations, label="closed foundation")
        _canonical_tokens(self.remaining_requirement_codes, label="remaining requirement")
        _sha256(self.node_manifest_sha256, label="historical failure-group node manifest")

    def to_payload(self) -> dict[str, object]:
        return {
            "closed_foundations": list(self.closed_foundations),
            "failed_nodes": self.failed_nodes,
            "failure_class": self.failure_class.value,
            "node_manifest_sha256": self.node_manifest_sha256,
            "remaining_requirement_codes": list(self.remaining_requirement_codes),
            "schema": HISTORICAL_FAILURE_GROUP_SCHEMA,
            "test_file": self.test_file,
        }

    @classmethod
    def from_payload(cls, value: object) -> HistoricalFailureGroupV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "closed_foundations",
                    "failed_nodes",
                    "failure_class",
                    "node_manifest_sha256",
                    "remaining_requirement_codes",
                    "schema",
                    "test_file",
                }
            ),
            label="historical failure group",
        )
        if payload["schema"] != HISTORICAL_FAILURE_GROUP_SCHEMA:
            raise CutoverReadinessError("historical failure group schema is unsupported")
        return cls(
            test_file=_test_file(payload["test_file"]),
            failed_nodes=_count(
                payload["failed_nodes"], label="historical failed-node count", low=1, high=60
            ),
            failure_class=cast(
                HistoricalFailureClass,
                _enum(
                    HistoricalFailureClass,
                    payload["failure_class"],
                    label="historical failure class",
                ),
            ),
            closed_foundations=_canonical_tokens(payload["closed_foundations"], label="closed foundation"),
            remaining_requirement_codes=_canonical_tokens(
                payload["remaining_requirement_codes"], label="remaining requirement"
            ),
            node_manifest_sha256=_sha256(
                payload["node_manifest_sha256"],
                label="historical failure-group node manifest",
            ),
        )


HISTORICAL_FAILURE_GROUPS: Final = (
    HistoricalFailureGroupV1(
        "tests/test_a_question_about_the_conversation_reaches_the_messages.py",
        6,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT,
        ("r8a", "r8d"),
        ("retirement_sensitive_message_union_not_replayed",),
        "79064be2a1e18c6eb7063a8ce36029cf003e98540632549f5558c0e1e339c862",
    ),
    HistoricalFailureGroupV1(
        "tests/test_agent_tool_capability_boundary.py",
        5,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT,
        ("r8a", "r8b"),
        ("retirement_sensitive_capability_union_not_replayed",),
        "d181128dd4f9359a3aa49df4e1954e5d723ea78d3fa011e8f4430f66a944adef",
    ),
    HistoricalFailureGroupV1(
        "tests/test_agent_tool_capability_boundary.py",
        4,
        HistoricalFailureClass.TRANSPORT_CAPABILITY,
        ("r8a",),
        ("future_catalog_transport_union_not_replayed",),
        "6fe76c5da0ee2f5da14bafd0b2c1c9b3465ae0616132487cbbea8ae2c4c75dd1",
    ),
    HistoricalFailureGroupV1(
        "tests/test_an_interrupted_step_offers_a_real_rollback.py",
        1,
        HistoricalFailureClass.TOOL_TURN_ADMISSION,
        ("r8a",),
        ("future_catalog_tool_admission_union_not_replayed",),
        "3f6f3d2060b45cefeee79551ae49d500ae855dacaf37168985f8a548891e00c6",
    ),
    HistoricalFailureGroupV1(
        "tests/test_autonomous_engineer_runtime.py",
        2,
        HistoricalFailureClass.TOOL_TURN_ADMISSION,
        ("r8a",),
        ("future_catalog_tool_admission_union_not_replayed",),
        "7bccbfff3792986072d4318aa973ea7c2ca9d9040b11074bf295837cdccccaa3",
    ),
    HistoricalFailureGroupV1(
        "tests/test_graph_snapshot_reaches_agent.py",
        19,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT,
        ("r8a", "r8e"),
        ("retirement_sensitive_graph_union_not_replayed",),
        "cba1ea5bf080c079a004f4586fcc1555bf0fc3a552afdbc04409e886e23382f8",
    ),
    HistoricalFailureGroupV1(
        "tests/test_hotfix_02063_adversarial_contracts.py",
        1,
        HistoricalFailureClass.TOOL_TURN_ADMISSION,
        ("r8a",),
        ("future_catalog_tool_admission_union_not_replayed",),
        "e5d8001d5dc8edce225d95ca0f0eb9cb470dab942ad423d2567891cfb783b371",
    ),
    HistoricalFailureGroupV1(
        "tests/test_mission_termination.py",
        1,
        HistoricalFailureClass.TOOL_TURN_ADMISSION,
        ("r8a",),
        ("future_catalog_tool_admission_union_not_replayed",),
        "5d719fa062fddbccacd0b7b3221871c4f17be3789c739f9de51764ef8b1a87a3",
    ),
    HistoricalFailureGroupV1(
        "tests/test_package_b_routing_acceptance.py",
        16,
        HistoricalFailureClass.CLASSIFIER_JSON,
        ("r8a", "r8c", "r8e"),
        ("sole_facade_classifier_union_not_replayed",),
        "9610f36cdaee0600b03f472f4b0c4714e286e2c2fbd3ce337729834e0e44045c",
    ),
    HistoricalFailureGroupV1(
        "tests/test_parallel_results_reach_the_model_whole.py",
        4,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT,
        ("r8a", "r8e"),
        ("retirement_sensitive_parallel_union_not_replayed",),
        "668f42445a541054ba1de1468246df8746fd56ceb08009a552f01b418e62e5e2",
    ),
    HistoricalFailureGroupV1(
        "tests/test_small_talk_costs_nothing.py",
        1,
        HistoricalFailureClass.SMALL_TALK_CATALOG,
        ("r8a",),
        ("future_catalog_small_talk_union_not_replayed",),
        "4397877f8f7810e95c45dd1d35a6177492e68456f0f84d185b0d972e25173ebf",
    ),
)


def _validate_historical_groups(values: tuple[HistoricalFailureGroupV1, ...]) -> None:
    if type(values) is not tuple or any(type(item) is not HistoricalFailureGroupV1 for item in values):
        raise CutoverReadinessError("historical R8 failure inventory is not exact")
    keys = tuple((item.test_file, item.failure_class.value) for item in values)
    if (
        keys != tuple(sorted(keys))
        or len(set(keys)) != len(values)
        or sum(item.failed_nodes for item in values) != HISTORICAL_FAILURE_COUNT
    ):
        raise CutoverReadinessError("historical R8 failure inventory is not exact")


_validate_historical_groups(HISTORICAL_FAILURE_GROUPS)


@dataclass(frozen=True, slots=True)
class CutoverCaseV1:
    contour: CutoverContour
    status: CutoverEvidenceStatus
    evidence_codes: tuple[str, ...]
    blocker_codes: tuple[str, ...]
    required_shared_files: tuple[str, ...]
    executable_nodes: tuple[str, ...]
    binding_sha256s: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            type(values) is not tuple
            for values in (
                self.evidence_codes,
                self.blocker_codes,
                self.required_shared_files,
                self.executable_nodes,
                self.binding_sha256s,
            )
        ):
            raise CutoverReadinessError("cutover case collections must be immutable")
        if type(self.contour) is not CutoverContour or type(self.status) is not CutoverEvidenceStatus:
            raise CutoverReadinessError("cutover case enums are invalid")
        _canonical_tokens(self.evidence_codes, label="cutover evidence code")
        blockers = _canonical_optional_tokens(self.blocker_codes, label="cutover blocker code")
        files = _canonical_repo_files(self.required_shared_files)
        _canonical_nodes(self.executable_nodes)
        _canonical_digests(self.binding_sha256s)
        ready = self.status in {CutoverEvidenceStatus.PARITY, CutoverEvidenceStatus.PRESERVED}
        if ready and (blockers or files):
            raise CutoverReadinessError("ready cutover evidence cannot retain a blocker")
        if not ready and (not blockers or not files):
            raise CutoverReadinessError("incomplete cutover evidence requires exact blockers and files")

    @property
    def ready(self) -> bool:
        return self.status in {CutoverEvidenceStatus.PARITY, CutoverEvidenceStatus.PRESERVED}

    def to_payload(self) -> dict[str, object]:
        return {
            "binding_sha256s": list(self.binding_sha256s),
            "blocker_codes": list(self.blocker_codes),
            "contour": self.contour.value,
            "evidence_codes": list(self.evidence_codes),
            "executable_nodes": list(self.executable_nodes),
            "ready": self.ready,
            "required_shared_files": list(self.required_shared_files),
            "schema": CUTOVER_CASE_SCHEMA,
            "status": self.status.value,
        }

    @classmethod
    def from_payload(cls, value: object) -> CutoverCaseV1:
        payload = exact_object(
            value,
            frozenset(
                {
                    "binding_sha256s",
                    "blocker_codes",
                    "contour",
                    "evidence_codes",
                    "executable_nodes",
                    "ready",
                    "required_shared_files",
                    "schema",
                    "status",
                }
            ),
            label="cutover case",
        )
        if payload["schema"] != CUTOVER_CASE_SCHEMA or type(payload["ready"]) is not bool:
            raise CutoverReadinessError("cutover case schema is unsupported")
        case = cls(
            contour=cast(
                CutoverContour,
                _enum(CutoverContour, payload["contour"], label="cutover contour"),
            ),
            status=cast(
                CutoverEvidenceStatus,
                _enum(CutoverEvidenceStatus, payload["status"], label="cutover status"),
            ),
            evidence_codes=_canonical_tokens(payload["evidence_codes"], label="cutover evidence code"),
            blocker_codes=_canonical_optional_tokens(payload["blocker_codes"], label="cutover blocker code"),
            required_shared_files=_canonical_repo_files(payload["required_shared_files"]),
            executable_nodes=_canonical_nodes(payload["executable_nodes"]),
            binding_sha256s=_canonical_digests(payload["binding_sha256s"]),
        )
        if payload["ready"] is not case.ready:
            raise CutoverReadinessError("cutover case readiness flag is forged")
        return case


_REPORT_KEYS = frozenset(
    {
        "archive_parity_report_sha256",
        "archive_parity_case_manifest_sha256",
        "archive_parity_measurement_sha256",
        "archive_release_sha256",
        "cases",
        "cutover_case_manifest_sha256",
        "cutover_ready",
        "historical_base_sha",
        "historical_candidate_sha",
        "historical_failure_count",
        "historical_failure_groups",
        "historical_node_manifest_sha256",
        "memory_adapter_binding_sha256",
        "memory_foundation_review_status",
        "memory_foundation_reviewed_sha",
        "message_adapter_binding_sha256",
        "message_foundation_measurement_sha",
        "report_sha256",
        "schema",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class CutoverReadinessReportV1:
    archive_release_sha256: str
    archive_parity_report_sha256: str
    message_adapter_binding_sha256: str
    memory_adapter_binding_sha256: str
    cases: tuple[CutoverCaseV1, ...]
    historical_failure_groups: tuple[HistoricalFailureGroupV1, ...]
    report_sha256: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        for label, value in (
            ("archive release digest", self.archive_release_sha256),
            ("archive parity report digest", self.archive_parity_report_sha256),
            ("message adapter binding digest", self.message_adapter_binding_sha256),
            ("memory adapter binding digest", self.memory_adapter_binding_sha256),
            ("cutover report digest", self.report_sha256),
        ):
            _sha256(value, label=label)
        try:
            current_archive_release = archive_search_release_sha256()
        except RecallReleaseIdentityError as exc:
            raise CutoverReadinessError("archive release source identity is unavailable") from exc
        if self.archive_release_sha256 != current_archive_release:
            raise CutoverReadinessError("archive release binding is stale")
        if (
            type(self.cases) is not tuple
            or any(type(item) is not CutoverCaseV1 for item in self.cases)
            or tuple(item.contour.value for item in self.cases)
            != tuple(sorted(item.value for item in CutoverContour))
        ):
            raise CutoverReadinessError("cutover report does not cover every contour exactly once")
        _validate_historical_groups(self.historical_failure_groups)
        if self.historical_failure_groups != HISTORICAL_FAILURE_GROUPS:
            raise CutoverReadinessError("historical R8 failure classification is not authoritative")
        if (
            self.message_adapter_binding_sha256 != MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256()
            or self.memory_adapter_binding_sha256 != MEMORY_EXACT_ADAPTER_BINDING.canonical_sha256()
        ):
            raise CutoverReadinessError("exact adapter binding is not code-owned")
        case_manifest = digest_payload(
            b"friday/s4-r8f-cutover-case-manifest/v1",
            [item.to_payload() for item in self.cases],
        )
        if case_manifest != CUTOVER_CASE_MANIFEST_SHA256:
            raise CutoverReadinessError("cutover case manifest is not authoritative")
        expected = digest_payload(
            b"friday/archive-search-cutover-readiness-report/v1",
            self._payload_without_digest(),
        )
        if expected != self.report_sha256:
            raise CutoverReadinessError("cutover report digest is forged")

    @property
    def cutover_ready(self) -> bool:
        return MEMORY_FOUNDATION_REVIEW_STATUS == "accepted" and all(item.ready for item in self.cases)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(sorted({code for item in self.cases for code in item.blocker_codes}))

    @property
    def minimal_shared_file_set(self) -> tuple[str, ...]:
        return tuple(sorted({path for item in self.cases for path in item.required_shared_files}))

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "archive_parity_report_sha256": self.archive_parity_report_sha256,
            "archive_parity_case_manifest_sha256": R8C_CASE_MANIFEST_SHA256,
            "archive_parity_measurement_sha256": R8C_MEASUREMENT_SHA256,
            "archive_release_sha256": self.archive_release_sha256,
            "cases": [item.to_payload() for item in self.cases],
            "cutover_case_manifest_sha256": CUTOVER_CASE_MANIFEST_SHA256,
            "cutover_ready": self.cutover_ready,
            "historical_base_sha": HISTORICAL_BASE_SHA,
            "historical_candidate_sha": HISTORICAL_CANDIDATE_SHA,
            "historical_failure_count": HISTORICAL_FAILURE_COUNT,
            "historical_failure_groups": [item.to_payload() for item in self.historical_failure_groups],
            "historical_node_manifest_sha256": HISTORICAL_NODE_MANIFEST_SHA256,
            "memory_adapter_binding_sha256": self.memory_adapter_binding_sha256,
            "memory_foundation_review_status": MEMORY_FOUNDATION_REVIEW_STATUS,
            "memory_foundation_reviewed_sha": MEMORY_FOUNDATION_REVIEWED_SHA,
            "message_adapter_binding_sha256": self.message_adapter_binding_sha256,
            "message_foundation_measurement_sha": MESSAGE_FOUNDATION_MEASUREMENT_SHA,
            "schema": CUTOVER_REPORT_SCHEMA,
        }

    def to_payload(self) -> dict[str, object]:
        return {**self._payload_without_digest(), "report_sha256": self.report_sha256}

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @classmethod
    def create(
        cls,
        *,
        archive_release_sha256: str,
        archive_parity_report_sha256: str,
        message_adapter_binding_sha256: str,
        memory_adapter_binding_sha256: str,
        cases: tuple[CutoverCaseV1, ...],
        historical_failure_groups: tuple[HistoricalFailureGroupV1, ...] = HISTORICAL_FAILURE_GROUPS,
    ) -> CutoverReadinessReportV1:
        ordered_cases = tuple(sorted(cases, key=lambda item: item.contour.value))
        ordered_groups = tuple(
            sorted(
                historical_failure_groups,
                key=lambda item: (item.test_file, item.failure_class.value),
            )
        )
        payload = {
            "archive_parity_report_sha256": _sha256(
                archive_parity_report_sha256, label="archive parity report digest"
            ),
            "archive_parity_case_manifest_sha256": R8C_CASE_MANIFEST_SHA256,
            "archive_parity_measurement_sha256": R8C_MEASUREMENT_SHA256,
            "archive_release_sha256": _sha256(archive_release_sha256, label="archive release digest"),
            "cases": [item.to_payload() for item in ordered_cases],
            "cutover_case_manifest_sha256": CUTOVER_CASE_MANIFEST_SHA256,
            "cutover_ready": MEMORY_FOUNDATION_REVIEW_STATUS == "accepted"
            and all(item.ready for item in ordered_cases),
            "historical_base_sha": HISTORICAL_BASE_SHA,
            "historical_candidate_sha": HISTORICAL_CANDIDATE_SHA,
            "historical_failure_count": HISTORICAL_FAILURE_COUNT,
            "historical_failure_groups": [item.to_payload() for item in ordered_groups],
            "historical_node_manifest_sha256": HISTORICAL_NODE_MANIFEST_SHA256,
            "memory_adapter_binding_sha256": _sha256(
                memory_adapter_binding_sha256, label="memory adapter binding digest"
            ),
            "memory_foundation_review_status": MEMORY_FOUNDATION_REVIEW_STATUS,
            "memory_foundation_reviewed_sha": MEMORY_FOUNDATION_REVIEWED_SHA,
            "message_adapter_binding_sha256": _sha256(
                message_adapter_binding_sha256, label="message adapter binding digest"
            ),
            "message_foundation_measurement_sha": MESSAGE_FOUNDATION_MEASUREMENT_SHA,
            "schema": CUTOVER_REPORT_SCHEMA,
        }
        report_sha256 = digest_payload(
            b"friday/archive-search-cutover-readiness-report/v1",
            payload,
        )
        return cls(
            archive_release_sha256=cast(str, payload["archive_release_sha256"]),
            archive_parity_report_sha256=cast(str, payload["archive_parity_report_sha256"]),
            message_adapter_binding_sha256=cast(str, payload["message_adapter_binding_sha256"]),
            memory_adapter_binding_sha256=cast(str, payload["memory_adapter_binding_sha256"]),
            cases=ordered_cases,
            historical_failure_groups=ordered_groups,
            report_sha256=report_sha256,
        )

    @classmethod
    def from_payload(cls, value: object) -> CutoverReadinessReportV1:
        payload = exact_object(value, _REPORT_KEYS, label="cutover report")
        if (
            payload["schema"] != CUTOVER_REPORT_SCHEMA
            or payload["archive_parity_case_manifest_sha256"] != R8C_CASE_MANIFEST_SHA256
            or payload["archive_parity_measurement_sha256"] != R8C_MEASUREMENT_SHA256
            or payload["cutover_case_manifest_sha256"] != CUTOVER_CASE_MANIFEST_SHA256
            or payload["historical_base_sha"] != HISTORICAL_BASE_SHA
            or payload["historical_candidate_sha"] != HISTORICAL_CANDIDATE_SHA
            or payload["historical_failure_count"] != HISTORICAL_FAILURE_COUNT
            or payload["historical_node_manifest_sha256"] != HISTORICAL_NODE_MANIFEST_SHA256
            or payload["memory_foundation_review_status"] != MEMORY_FOUNDATION_REVIEW_STATUS
            or payload["memory_foundation_reviewed_sha"] != MEMORY_FOUNDATION_REVIEWED_SHA
            or payload["message_foundation_measurement_sha"] != MESSAGE_FOUNDATION_MEASUREMENT_SHA
            or type(payload["cutover_ready"]) is not bool
            or type(payload["cases"]) is not list
            or type(payload["historical_failure_groups"]) is not list
        ):
            raise CutoverReadinessError("cutover report binding is unsupported")
        report = cls(
            archive_release_sha256=_sha256(payload["archive_release_sha256"], label="archive release digest"),
            archive_parity_report_sha256=_sha256(
                payload["archive_parity_report_sha256"], label="archive parity report digest"
            ),
            message_adapter_binding_sha256=_sha256(
                payload["message_adapter_binding_sha256"], label="message adapter binding digest"
            ),
            memory_adapter_binding_sha256=_sha256(
                payload["memory_adapter_binding_sha256"], label="memory adapter binding digest"
            ),
            cases=tuple(CutoverCaseV1.from_payload(item) for item in cast(list[object], payload["cases"])),
            historical_failure_groups=tuple(
                HistoricalFailureGroupV1.from_payload(item)
                for item in cast(list[object], payload["historical_failure_groups"])
            ),
            report_sha256=_sha256(payload["report_sha256"], label="cutover report digest"),
        )
        if payload["cutover_ready"] is not report.cutover_ready:
            raise CutoverReadinessError("cutover report readiness flag is forged")
        return report

    @classmethod
    def parse(cls, value: str) -> CutoverReadinessReportV1:
        parsed = parse_canonical_json(value, label="cutover report")
        report = cls.from_payload(parsed)
        if report.to_json() != value:
            raise CutoverReadinessError("cutover report is not canonical")
        return report


_AGENT_RUNTIME = "friday/agent_runtime/__init__.py"
_TOOL_PROTOCOL = "friday/agent_runtime/tool_protocol.py"
_CAPABILITY_BINDING = "friday/orchestration/capability_binding.py"
_ARCHIVE_CONTRACT = "friday/retrieval/archive_search_contract.py"
_ARCHIVE_SERVICE = "friday/retrieval/archive_search_service.py"
_MESSAGE_SELECTOR = "friday/storage/_archive_search_messages.py"
_EXECUTION_KERNEL = "friday/execution_kernel/__init__.py"
_SERVER = "friday/server.py"
_TURN_INTENT_POLICY = "friday/turn_intent_policy.py"


def _validate_parity_provenance(parity: ParityReportV1) -> None:
    try:
        current_release = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise CutoverReadinessError("archive parity source identity is unavailable") from exc
    measurement_sha256 = digest_payload(
        b"friday/s4-r8f-r8c-measurement/v1",
        {
            "cases": [item.to_payload() for item in parity.cases],
            "dimensions": [item.to_payload() for item in parity.dimensions],
        },
    )
    if (
        parity.release_sha256 != current_release
        or parity.case_manifest_sha256 != R8C_CASE_MANIFEST_SHA256
        or measurement_sha256 != R8C_MEASUREMENT_SHA256
    ):
        raise CutoverReadinessError("archive parity report is not the exact R8C measurement")


def _adapter_case(
    parity: ParityReportV1,
    *,
    contour: CutoverContour,
    adapter: str,
    nodes: tuple[str, ...],
    mismatch_file: str,
) -> CutoverCaseV1:
    cases = tuple(item for item in parity.cases if item.adapter == adapter)
    if not cases:
        raise CutoverReadinessError(f"archive parity report has no {adapter} cases")
    membership = sum(item.membership_status == "mismatch" for item in cases)
    order = sum(item.order_status == "mismatch" for item in cases)
    evidence = [f"{adapter}_membership_{len(cases) - membership}_of_{len(cases)}"]
    comparable = sum(item.order_status != "not_comparable" for item in cases)
    evidence.append(f"{adapter}_order_{comparable - order}_of_{comparable}")
    if not membership and not order:
        return CutoverCaseV1(
            contour=contour,
            status=CutoverEvidenceStatus.PARITY,
            evidence_codes=tuple(sorted(evidence)),
            blocker_codes=(),
            required_shared_files=(),
            executable_nodes=nodes,
        )
    blockers = []
    if membership:
        blockers.append(f"{adapter}_candidate_membership_mismatch")
    if order:
        blockers.append(f"{adapter}_candidate_order_mismatch")
    return CutoverCaseV1(
        contour=contour,
        status=CutoverEvidenceStatus.MISMATCH,
        evidence_codes=tuple(sorted(evidence)),
        blocker_codes=tuple(sorted(blockers)),
        required_shared_files=(mismatch_file,),
        executable_nodes=nodes,
    )


def _blocked_case(
    contour: CutoverContour,
    status: CutoverEvidenceStatus,
    evidence: tuple[str, ...],
    blockers: tuple[str, ...],
    files: tuple[str, ...],
    nodes: tuple[str, ...],
    bindings: tuple[str, ...] = (),
) -> CutoverCaseV1:
    return CutoverCaseV1(
        contour=contour,
        status=status,
        evidence_codes=tuple(sorted(evidence)),
        blocker_codes=tuple(sorted(blockers)),
        required_shared_files=tuple(sorted(files)),
        executable_nodes=tuple(sorted(nodes)),
        binding_sha256s=tuple(sorted(bindings)),
    )


def _preserved_case(
    contour: CutoverContour,
    evidence: tuple[str, ...],
    nodes: tuple[str, ...],
    bindings: tuple[str, ...] = (),
) -> CutoverCaseV1:
    return CutoverCaseV1(
        contour=contour,
        status=CutoverEvidenceStatus.PRESERVED,
        evidence_codes=tuple(sorted(evidence)),
        blocker_codes=(),
        required_shared_files=(),
        executable_nodes=tuple(sorted(nodes)),
        binding_sha256s=tuple(sorted(bindings)),
    )


def build_cutover_readiness_report(parity: ParityReportV1) -> CutoverReadinessReportV1:
    """Classify current contracts without pretending that unwired lanes are live."""

    if type(parity) is not ParityReportV1:
        raise CutoverReadinessError("cutover readiness requires the exact archive parity report")
    _validate_parity_provenance(parity)
    message_binding = MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256()
    memory_binding = MEMORY_EXACT_ADAPTER_BINDING.canonical_sha256()
    cases = [
        _adapter_case(
            parity,
            contour=CutoverContour.SCALAR,
            adapter="memory_search",
            nodes=(
                "tests/test_archive_search_facade_parity.py::test_literal_promoted_knowledge_matches_memory_fallback_body_free",
            ),
            mismatch_file=_ARCHIVE_SERVICE,
        ),
        _adapter_case(
            parity,
            contour=CutoverContour.ARCHIVED_SOURCE,
            adapter="source_search",
            nodes=(
                "tests/test_archive_search_facade_parity.py::test_pending_source_matches_source_search_but_cannot_be_publication_evidence",
            ),
            mismatch_file=_ARCHIVE_SERVICE,
        ),
        _adapter_case(
            parity,
            contour=CutoverContour.MESSAGE_TOPIC,
            adapter="message_search",
            nodes=(
                "tests/test_archive_search_message_parity.py::test_archive_selector_matches_legacy_keyboard_layout_recall",
            ),
            mismatch_file=_MESSAGE_SELECTOR,
        ),
        _preserved_case(
            CutoverContour.CURRENT_FILE,
            (
                "current_file_guard_holds_after_catalog_hide",
                "current_file_v12_contract_remains_separate",
            ),
            (
                "tests/test_v12_file_read_handler.py::test_authenticated_current_file_route_keeps_exact_context_and_effect_owner",
            ),
        ),
        _preserved_case(
            CutoverContour.MESSAGE_WINDOW,
            (
                "message_exact_wired_to_archive_search",
                "queryless_current_conversation_exact_contract",
            ),
            (
                "tests/test_archive_search_exact_dispatch.py::test_exact_window_dispatches_message_lane_through_archive_search",
                "tests/test_message_exact_internal.py::test_queryless_current_scope_preserves_message_and_reply_identity",
                "tests/test_message_exact_internal.py::test_role_and_microsecond_half_open_window_are_exact",
            ),
            (message_binding,),
        ),
        _preserved_case(
            CutoverContour.MEMORY_TEMPORAL,
            (
                "archive_handler_expresses_as_of_known_at",
                "memory_exact_as_of_known_at_contract",
            ),
            (
                "tests/test_archive_search_exact_dispatch.py::test_as_of_and_graph_dispatch_memory_lane_through_archive_search",
                "tests/test_memory_exact_internal.py::test_as_of_graph_context_is_bounded_id_free_and_source_bound",
                "tests/test_memory_exact_internal.py::test_known_at_relation_history_matches_legacy_snapshot",
            ),
            (memory_binding,),
        ),
        _preserved_case(
            CutoverContour.MEMORY_GRAPH,
            (
                "memory_exact_bounded_graph_projection",
                "memory_exact_graph_wired_to_archive_search",
            ),
            (
                "tests/test_archive_search_exact_dispatch.py::test_kernel_include_graph_uses_the_same_dispatch_owner",
                "tests/test_memory_exact_internal.py::test_current_implicit_graph_keeps_cooccurrence_and_local_grounding",
                "tests/test_memory_exact_internal.py::test_explicit_legacy_merged_endpoint_is_exactly_canonicalized",
            ),
            (memory_binding,),
        ),
        _preserved_case(
            CutoverContour.FOLLOW_UP,
            (
                "follow_up_guards_hold_after_catalog_hide",
                "legacy_follow_up_state_is_distinct_from_archive_cursor",
            ),
            (
                "tests/test_followup_query_order.py::test_real_follow_ups_still_get_the_context",
                "tests/test_v12_archive_read_handler.py::test_non_closed_source_search_followups_remain_legacy_owned",
            ),
        ),
        _preserved_case(
            CutoverContour.V12,
            (
                "v12_archive_reader_remains_separate",
                "v12_guard_holds_after_catalog_hide",
            ),
            (
                "tests/test_v12_archive_router.py::test_exact_archive_shape_dispatches_to_the_registered_read_only_handler",
            ),
        ),
        _preserved_case(
            CutoverContour.RESTART,
            (
                "exact_cursors_survive_storage_reopen",
                "exact_lane_restart_is_fail_closed",
            ),
            (
                "tests/test_memory_exact_internal.py::test_signed_continuation_survives_storage_restart",
                "tests/test_message_exact_internal.py::test_equal_timestamp_restart_paging_is_chronological_and_never_deduplicates",
            ),
            (memory_binding, message_binding),
        ),
        _preserved_case(
            CutoverContour.FALLBACK,
            (
                "fallback_guard_holds_after_catalog_hide",
                "released_primary_only_fallback_is_unchanged",
            ),
            (
                "tests/test_v12_archive_router.py::test_archive_plan_with_insufficient_max_items_falls_back_before_preparation",
            ),
        ),
        _preserved_case(
            CutoverContour.STALE_LEGACY_CALL,
            (
                "legacy_dialogue_calls_fail_closed",
                "legacy_internal_adapters_remain_executable",
            ),
            (
                "tests/test_archive_search_model_discovery.py::test_legacy_archive_retrieval_tools_are_stale_and_internally_executable",
            ),
        ),
        _preserved_case(
            CutoverContour.FINAL_REAUTHORIZATION,
            (
                "memory_exact_late_reauthorization_contract",
                "message_exact_late_reauthorization_contract",
                "single_final_publisher_consumes_exact_receipts",
            ),
            (
                "tests/test_memory_exact_internal.py::test_fresh_read_and_one_shot_publication_authority",
                "tests/test_message_exact_internal.py::test_late_revoke_returns_a_source_free_denial",
            ),
            (memory_binding, message_binding),
        ),
    ]
    return CutoverReadinessReportV1.create(
        archive_release_sha256=parity.release_sha256,
        archive_parity_report_sha256=parity.report_sha256,
        message_adapter_binding_sha256=message_binding,
        memory_adapter_binding_sha256=memory_binding,
        cases=tuple(cases),
    )


__all__ = [
    "CUTOVER_CASE_MANIFEST_SHA256",
    "CUTOVER_CASE_SCHEMA",
    "CUTOVER_REPORT_SCHEMA",
    "HISTORICAL_BASE_SHA",
    "HISTORICAL_CANDIDATE_SHA",
    "HISTORICAL_FAILURE_COUNT",
    "HISTORICAL_FAILURE_GROUPS",
    "HISTORICAL_FAILURE_GROUP_SCHEMA",
    "HISTORICAL_NODE_MANIFEST_SHA256",
    "MEMORY_FOUNDATION_REVIEWED_SHA",
    "MEMORY_FOUNDATION_REVIEW_STATUS",
    "MESSAGE_FOUNDATION_MEASUREMENT_SHA",
    "R8C_CASE_MANIFEST_SHA256",
    "R8C_MEASUREMENT_SHA256",
    "CutoverCaseV1",
    "CutoverContour",
    "CutoverEvidenceStatus",
    "CutoverReadinessError",
    "CutoverReadinessReportV1",
    "HistoricalFailureClass",
    "HistoricalFailureGroupV1",
    "build_cutover_readiness_report",
]
