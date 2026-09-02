from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from friday.agent_runtime.tool_protocol import classify_tool_turn
from friday.execution_kernel import INTERNAL_SEARCH_ADAPTER_TOOLS, ExecutionKernel
from friday.retrieval.archive_search_contract import (
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    RetrievalContractError,
)
from friday.retrieval.memory_exact_contract import MemoryExactRequest
from friday.retrieval.memory_exact_internal import MEMORY_EXACT_ADAPTER_BINDING
from friday.retrieval.message_exact_contract import MessageExactRequest
from friday.retrieval.message_exact_internal import MESSAGE_EXACT_ADAPTER_BINDING
from friday.retrieval_benchmark._canonical import canonical_json
from friday.retrieval_benchmark.cutover_readiness import (
    CUTOVER_CASE_MANIFEST_SHA256,
    HISTORICAL_BASE_SHA,
    HISTORICAL_CANDIDATE_SHA,
    HISTORICAL_FAILURE_COUNT,
    HISTORICAL_FAILURE_GROUPS,
    HISTORICAL_NODE_MANIFEST_SHA256,
    MEMORY_FOUNDATION_REVIEW_STATUS,
    MEMORY_FOUNDATION_REVIEWED_SHA,
    MESSAGE_FOUNDATION_MEASUREMENT_SHA,
    R8C_CASE_MANIFEST_SHA256,
    R8C_MEASUREMENT_SHA256,
    CutoverContour,
    CutoverEvidenceStatus,
    CutoverReadinessReportV1,
    HistoricalFailureClass,
    build_cutover_readiness_report,
)
from friday.retrieval_benchmark.parity import ParityReportV1, run_parity_ephemeral

_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_KEYS = {
    "actor_id",
    "body",
    "conversation_id",
    "excerpt",
    "message_id",
    "path",
    "person_id",
    "principal_id",
    "query",
    "raw_id",
    "source_id",
    "tenant_id",
    "turn_id",
}


@pytest.fixture(scope="module")
def parity_report() -> ParityReportV1:
    return run_parity_ephemeral()


@pytest.fixture(scope="module")
def cutover_report(parity_report: ParityReportV1) -> CutoverReadinessReportV1:
    return build_cutover_readiness_report(parity_report)


def _all_keys(value: object) -> set[str]:
    if type(value) is dict:
        mapping = value
        return set(mapping) | set().union(*(_all_keys(item) for item in mapping.values()))
    if type(value) is list:
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_cutover_report_is_canonical_deterministic_immutable_and_body_free(
    parity_report: ParityReportV1,
    cutover_report: CutoverReadinessReportV1,
) -> None:
    second = build_cutover_readiness_report(parity_report)
    serialized = cutover_report.to_json()
    payload = cutover_report.to_payload()

    assert serialized == second.to_json()
    assert serialized == canonical_json(payload)
    assert serialized.encode("ascii").decode("ascii") == serialized
    assert json.loads(serialized) == payload
    assert not (_all_keys(payload) & _PRIVATE_KEYS)
    assert "/home/" not in serialized
    assert "\\u0421\\u043a\\u043e\\u043b\\u044c\\u043a\\u043e" not in serialized
    with pytest.raises(FrozenInstanceError):
        cutover_report.report_sha256 = "0" * 64  # type: ignore[misc]


def test_cutover_report_round_trips_only_as_exact_canonical_json(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    parsed = CutoverReadinessReportV1.parse(cutover_report.to_json())

    assert parsed == cutover_report
    assert parsed.to_json() == cutover_report.to_json()


def test_current_parity_mismatch_is_exposed_instead_of_hidden_by_routing(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    cases = {item.contour: item for item in cutover_report.cases}

    assert cases[CutoverContour.SCALAR].status is CutoverEvidenceStatus.PARITY
    assert cases[CutoverContour.ARCHIVED_SOURCE].status is CutoverEvidenceStatus.PARITY
    assert cases[CutoverContour.MESSAGE_TOPIC].status is CutoverEvidenceStatus.MISMATCH
    assert cases[CutoverContour.MESSAGE_TOPIC].evidence_codes == (
        "message_search_membership_3_of_3",
        "message_search_order_2_of_3",
    )
    assert cases[CutoverContour.MESSAGE_TOPIC].blocker_codes == ("message_search_candidate_order_mismatch",)
    assert cutover_report.cutover_ready is False


def test_exact_message_and_memory_foundations_are_not_promoted_to_runtime_parity(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    cases = {item.contour: item for item in cutover_report.cases}
    message = cases[CutoverContour.MESSAGE_WINDOW]
    temporal = cases[CutoverContour.MEMORY_TEMPORAL]
    graph = cases[CutoverContour.MEMORY_GRAPH]
    publication = cases[CutoverContour.FINAL_REAUTHORIZATION]

    assert message.status is CutoverEvidenceStatus.CONTRACT_ONLY
    assert message.binding_sha256s == (MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256(),)
    assert temporal.status is graph.status is CutoverEvidenceStatus.CONTRACT_ONLY
    assert (
        temporal.binding_sha256s
        == graph.binding_sha256s
        == (MEMORY_EXACT_ADAPTER_BINDING.canonical_sha256(),)
    )
    assert publication.binding_sha256s == tuple(
        sorted(
            {
                MEMORY_EXACT_ADAPTER_BINDING.canonical_sha256(),
                MESSAGE_EXACT_ADAPTER_BINDING.canonical_sha256(),
            }
        )
    )
    assert "memory_exact_provider_time_reauthorization_review_open" in publication.blocker_codes
    assert message.ready is temporal.ready is graph.ready is publication.ready is False


def test_archive_model_shape_still_cannot_express_exact_window_or_bitemporal_intent() -> None:
    with pytest.raises(RetrievalContractError):
        ArchiveSearchRequest.create(query="", corpora=(ArchiveSearchCorpus.MESSAGES,))
    with pytest.raises(RetrievalContractError):
        ArchiveSearchRequest.from_model_payload(
            {
                "query": "opaque",
                "corpora": ["knowledge"],
                "as_of": "2026-01-01",
            }
        )
    with pytest.raises(RetrievalContractError):
        ArchiveSearchRequest.from_model_payload(
            {
                "query": "opaque",
                "corpora": ["knowledge"],
                "known_at": "2026-01-01T00:00:00+00:00",
            }
        )

    message = MessageExactRequest.create(
        conversation_id="conv_aaaaaaaaaaaaaaaa",
        accepted_boundary_user_message_id="msg_bbbbbbbbbbbbbbbb",
    )
    memory = MemoryExactRequest.create(
        tenant_id="opaque-tenant",
        principal_id="opaque-principal",
        active_turn_id=f"turn_{'c' * 64}",
        query="opaque",
        as_of="2026-01-01",
        known_at="2026-01-02T03:04:05+00:00",
    )

    assert "continuation" not in message.to_identity_payload()
    assert memory.as_of == "2026-01-01"
    assert memory.known_at == "2026-01-02T03:04:05.000000Z"


def test_legacy_tools_are_intentionally_not_stale_before_the_later_cutover(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    kernel = ExecutionKernel()
    specs = kernel._tools  # noqa: SLF001 - offline cutover catalogue measurement
    legacy = frozenset({"memory_search", "message_search", "source_search"})

    assert legacy == INTERNAL_SEARCH_ADAPTER_TOOLS
    for name in legacy:
        assert specs[name].model_visible is True
        assert "dialogue" in specs[name].allowed_execution_scopes
        assert "internal" in specs[name].allowed_execution_scopes
        assert classify_tool_turn(f'{{"name":"{name}","arguments":{{"query":"opaque"}}}}').kind == "tool"
    assert MESSAGE_EXACT_ADAPTER_BINDING.model_visible is False
    assert MEMORY_EXACT_ADAPTER_BINDING.model_visible is False
    stale = next(item for item in cutover_report.cases if item.contour is CutoverContour.STALE_LEGACY_CALL)
    assert stale.status is CutoverEvidenceStatus.UNMEASURED
    assert stale.blocker_codes == ("legacy_calls_are_not_stale_until_catalog_cutover",)


def test_every_required_contour_has_an_exact_resolvable_guard_reference(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    assert {item.contour for item in cutover_report.cases} == set(CutoverContour)

    for case in cutover_report.cases:
        assert case.executable_nodes
        for node_id in case.executable_nodes:
            relative, function = node_id.split("::", 1)
            function = function.split("[", 1)[0]
            source = (_ROOT / relative).read_text(encoding="utf-8")
            assert re.search(rf"^(?:async )?def {re.escape(function)}\(", source, re.MULTILINE)


def test_minimal_later_shared_file_set_is_exact_and_does_not_include_foundation_files(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    assert cutover_report.minimal_shared_file_set == (
        "friday/agent_runtime/__init__.py",
        "friday/agent_runtime/tool_protocol.py",
        "friday/execution_kernel/__init__.py",
        "friday/orchestration/capability_binding.py",
        "friday/retrieval/archive_search_contract.py",
        "friday/retrieval/archive_search_service.py",
        "friday/server.py",
        "friday/storage/_archive_search_messages.py",
        "friday/turn_intent_policy.py",
    )
    serialized = cutover_report.to_json()
    assert "message_exact_contract.py" not in serialized
    assert "memory_exact_contract.py" not in serialized


def test_historical_failure_inventory_accounts_for_all_sixty_without_node_bodies(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    assert HISTORICAL_BASE_SHA == "9928e83d26061cc3df1198815ca9ac9f4481080f"
    assert HISTORICAL_CANDIDATE_SHA == "7848cc45ad8ddda3702b1aa560d1d42d5dea2acc"
    assert HISTORICAL_FAILURE_COUNT == 60
    assert HISTORICAL_NODE_MANIFEST_SHA256 == (
        "e1d8d50860ad84ee3a117d48171af560f47a52750dd2d01ded1460ef792ef8d2"
    )
    assert sum(item.failed_nodes for item in HISTORICAL_FAILURE_GROUPS) == 60
    assert len({item.node_manifest_sha256 for item in HISTORICAL_FAILURE_GROUPS}) == len(
        HISTORICAL_FAILURE_GROUPS
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", item.node_manifest_sha256) for item in HISTORICAL_FAILURE_GROUPS)
    counts: Counter[HistoricalFailureClass] = Counter()
    for item in HISTORICAL_FAILURE_GROUPS:
        counts[item.failure_class] += item.failed_nodes
    assert counts == {
        HistoricalFailureClass.CLASSIFIER_JSON: 16,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT: 34,
        HistoricalFailureClass.SMALL_TALK_CATALOG: 1,
        HistoricalFailureClass.TOOL_TURN_ADMISSION: 5,
        HistoricalFailureClass.TRANSPORT_CAPABILITY: 4,
    }
    assert cutover_report.historical_failure_groups == HISTORICAL_FAILURE_GROUPS
    serialized = cutover_report.to_json()
    assert "a_deictic_archive_still_means_the_whole_archive[" not in serialized
    assert "test_file" in serialized


def test_report_binds_exact_foundation_review_and_case_manifests(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    payload = cutover_report.to_payload()

    assert payload["archive_parity_case_manifest_sha256"] == R8C_CASE_MANIFEST_SHA256
    assert payload["archive_parity_measurement_sha256"] == R8C_MEASUREMENT_SHA256
    assert payload["cutover_case_manifest_sha256"] == CUTOVER_CASE_MANIFEST_SHA256
    assert payload["message_foundation_measurement_sha"] == MESSAGE_FOUNDATION_MEASUREMENT_SHA
    assert payload["memory_foundation_reviewed_sha"] == MEMORY_FOUNDATION_REVIEWED_SHA
    assert payload["memory_foundation_review_status"] == MEMORY_FOUNDATION_REVIEW_STATUS
    assert MEMORY_FOUNDATION_REVIEW_STATUS == "changes_required"
    assert cutover_report.cutover_ready is False


def test_restart_fallback_followup_v12_and_publication_stay_explicit_blockers(
    cutover_report: CutoverReadinessReportV1,
) -> None:
    cases = {item.contour: item for item in cutover_report.cases}
    expected = {
        CutoverContour.CURRENT_FILE: CutoverEvidenceStatus.UNMEASURED,
        CutoverContour.FALLBACK: CutoverEvidenceStatus.UNMEASURED,
        CutoverContour.FINAL_REAUTHORIZATION: CutoverEvidenceStatus.CONTRACT_ONLY,
        CutoverContour.FOLLOW_UP: CutoverEvidenceStatus.UNMEASURED,
        CutoverContour.RESTART: CutoverEvidenceStatus.CONTRACT_ONLY,
        CutoverContour.STALE_LEGACY_CALL: CutoverEvidenceStatus.UNMEASURED,
        CutoverContour.V12: CutoverEvidenceStatus.UNMEASURED,
    }

    for contour, status in expected.items():
        case = cases[contour]
        assert case.status is status
        assert case.blocker_codes
        assert case.required_shared_files
        assert case.ready is False
