from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from friday import __version__, semantic_supervisor_policy
from friday.interaction_control_plane.runtime_trace import build_committed_direct_trace
from friday.interaction_control_plane.turn_trace import (
    CapabilityClass,
    CountAccounting,
    IntentClass,
    PlaybookClass,
)
from friday.orchestration import supervisor_representative_window_attestation as window_module
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass, canonical_sha256
from friday.orchestration.supervisor_observation import parsed_observation
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PROMOTED_PRODUCT_EVENT,
    PromotedObservationEligibility,
    PromotedSupervisorProductObservation,
    PromotedUserVisibleOutcome,
    build_production_baseline,
)
from friday.orchestration.supervisor_promoted_product_event import SupervisorLatencyBudgetDocument
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC,
    REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA,
    REPRESENTATIVE_WINDOW_ISSUE_REQUEST_SCHEMA,
    AcceptedRepresentativeWindowAttestation,
    RepresentativeWindowAttestationError,
    _server_identity_matches,
    consume_representative_window_attestation,
    is_accepted_representative_window_attestation,
    issue_representative_window_attestation,
    refresh_representative_window_runtime_admission,
    representative_window_current_server_identity,
    representative_window_observer_runner_sha256,
    representative_window_sha256,
    representative_window_target_server_identity_after_restart,
    verify_persisted_consumed_representative_window_issue,
)
from friday.orchestration.supervisor_trace_join import (
    SUPERVISOR_TRACE_EVENT,
    SUPERVISOR_TRACE_JOIN_SCHEMA,
    PrimaryTraceProjection,
)
from friday.secondary_brain import (
    ModelWorkload,
    SecondaryMode,
    SecondaryState,
    SecondaryStatus,
)
from friday.secondary_brain.profiles import SecondaryProfileAdmission
from friday.secondary_brain.scheduler import SecondaryBrainScheduler

NOW = 1_800_000_000
SOURCE = "a" * 64
OBSERVED_SOURCE = "b" * 64
REGISTRY = "c" * 64
PRECURSOR = "d" * 64


class _HealthySecondaryClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            profile_id=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
            served_model_alias="gpt-oss-20b",
            max_context_tokens=131_072,
            max_output_tokens=1_024,
            health_interval_sec=60.0,
        )

    def status(self) -> SecondaryStatus:
        return SecondaryStatus(
            state=SecondaryState.HEALTHY,
            last_failure=None,
            selected_total=0,
            success_total=0,
            skipped_total=0,
            fallback_total=0,
            active_requests=0,
            context_cap_tokens=131_072,
            served_model_match=True,
            profile_manifest_match=True,
        )

    def protocol_rejection_counts(self) -> dict[object, int]:
        return {}

    async def aclose(self) -> None:
        return None


def _real_admitted_scheduler(
    requested_mode: SupervisorMode,
    *,
    effect_mode: str,
    epoch_admitted: bool = True,
) -> SecondaryBrainScheduler:
    supervisor_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(requested_mode)
    supervisor_admission = semantic_supervisor_policy.evaluate_supervisor_policy_admission(
        requested_mode=requested_mode.value,
        task_allowlist=(TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,),
        max_steps=6,
        max_review_rounds=supervisor_policy.max_review_rounds,
        timeout_sec=12.0,
        allow_private_text=True,
        secondary_runtime_state="configured",
        profile_admission="accepted",
        runtime_profile_id=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        runtime_profile_manifest_sha256=(
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
    )
    effect_admission = semantic_supervisor_policy.evaluate_supervisor_effect_shadow_policy_admission(
        requested_mode=effect_mode,
        allow_private_text=True,
        secondary_runtime_state="configured",
        profile_admission="accepted",
        runtime_profile_id=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        runtime_profile_manifest_sha256=(
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
    )
    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.ASSIST,
        allowed_workloads=frozenset({ModelWorkload.PLAN_CANDIDATE}),
        allow_private_text=True,
        client=_HealthySecondaryClient(),  # type: ignore[arg-type]
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
        supervisor_admission=supervisor_admission,
        effect_shadow_admission=effect_admission,
    )
    scheduler._epoch_admitted = epoch_admitted  # noqa: SLF001 - model one process epoch
    if epoch_admitted:
        scheduler._last_probe_success_monotonic = time.monotonic()  # noqa: SLF001
    return scheduler


class _Storage:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key,value)
            VALUES('audit_privacy_hmac_key','abababababababababababababababababababababababababababababababab');
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE runtime_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE request_idempotency (
                user_id TEXT NOT NULL,
                request_key TEXT NOT NULL,
                request_hash TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'complete',
                lease_token TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, request_key)
            );
            """
        )

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()


def _trace(index: int):
    return build_committed_direct_trace(
        namespace_key=b"p" * 32,
        turn_identifier=f"msg_{index:016x}",
        conversation_identifier="conv_bbbbbbbbbbbbbbbb",
        intent=IntentClass.MIXED,
        playbook=PlaybookClass.COMPARE_INTERNAL_AND_EXTERNAL_SOURCES,
        capabilities=(CapabilityClass.DOCUMENT_RETRIEVAL, CapabilityClass.MODEL_SYNTHESIS),
        latency_ms=432,
        model_calls=1,
        model_call_accounting=CountAccounting.LOWER_BOUND,
        capability_calls=1,
        capability_call_accounting=CountAccounting.COMPLETE,
        authority_rechecked=True,
    )


def _insert_trace(storage: _Storage, index: int):
    trace = _trace(index)
    storage.conn.execute(
        "INSERT INTO messages(id,role,content,metadata_json) VALUES(?,?,?,?)",
        (
            f"msg_{index:016x}",
            "assistant",
            f"PRIVATE BODY {index}",
            json.dumps({"interaction_trace": trace.to_payload()}),
        ),
    )
    return trace


def _seed_shadow(storage: _Storage, count: int = 20) -> None:
    for index in range(count):
        trace = _insert_trace(storage, index)
        projection = PrimaryTraceProjection.from_trace(trace)
        observation = parsed_observation(
            requested_mode="shadow",
            manifest_digest="1" * 64,
            supervisor_input_digest=hashlib.sha256(f"input:{index}".encode()).hexdigest(),
            proposal_digest=hashlib.sha256(f"proposal:{index}".encode()).hexdigest(),
            proposal_parse_status="parsed",
            policy_verdict="valid",
            policy_reason="admitted",
            task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
            step_count=3,
            effect_classes=("read", "read", "read"),
            current_route="legacy",
            endpoint_health_class="accepted",
            accepted_profile_id="accepted-profile",
            planner_latency_bucket="250_999ms",
        ).with_primary_trace(
            trace_digest=projection.trace_digest,
            capability_outcomes=projection.capability_outcomes,
            completion=projection.completion,
            publication=projection.publication,
            authority_rechecked=projection.authority_rechecked,
            state_restored=projection.state_restored,
            retry_occurred=projection.retry_occurred,
        )
        storage.conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
            (
                f"evt_{index:016x}",
                SUPERVISOR_TRACE_EVENT,
                json.dumps(
                    {
                        "schema": SUPERVISOR_TRACE_JOIN_SCHEMA,
                        "supervisor": observation.payload(),
                        "primary_trace": projection.payload(),
                    }
                ),
            ),
        )
    storage.conn.commit()


def _seed_assist(storage: _Storage, count: int = 20) -> None:
    for offset in range(count):
        index = 100 + offset
        trace = _insert_trace(storage, index)
        event = PromotedSupervisorProductObservation(
            mode=SupervisorMode.ASSIST,
            task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            eligibility=PromotedObservationEligibility.PROMOTED_JOURNEY,
            primary_trace_sha256=canonical_sha256(trace.to_payload()),
            promotion_evidence_sha256=PRECURSOR,
            execution_receipt_sha256=hashlib.sha256(f"receipt:{index}".encode()).hexdigest(),
            supervisor_invoked=True,
            user_visible_outcome=PromotedUserVisibleOutcome.NO_REGRESSION,
        )
        storage.conn.execute(
            "INSERT INTO runtime_events(id,event_type,payload) VALUES(?,?,?)",
            (
                f"evt_{index:016x}",
                SUPERVISOR_PROMOTED_PRODUCT_EVENT,
                json.dumps(event.payload()),
            ),
        )
    storage.conn.commit()


def _identity(
    *,
    requested_mode: SupervisorMode = SupervisorMode.SHADOW,
    policy_mode: SupervisorMode | None = None,
) -> dict[str, Any]:
    policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        policy_mode or requested_mode
    )
    return {
        "primary_pid": 4123,
        "primary_process_epoch_sha256": "e" * 64,
        "primary_backend_version": __version__,
        "observed_release_commit": "f" * 40,
        "observed_release_metadata_sha256": "1" * 64,
        "observed_release_tree_sha256": OBSERVED_SOURCE,
        "observed_registry_binding_sha256": REGISTRY,
        "requested_mode": requested_mode.value,
        "supervisor_policy_id": policy.policy_id,
        "supervisor_policy_sha256": policy.policy_sha256,
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
    }


def _predecessor_mode(target: SupervisorMode) -> SupervisorMode:
    return SupervisorMode.SHADOW if target is SupervisorMode.ASSIST else SupervisorMode.ASSIST


def _predecessor_identity(target: SupervisorMode) -> dict[str, Any]:
    return _identity(requested_mode=_predecessor_mode(target))


def _restart_identity(target: SupervisorMode) -> dict[str, Any]:
    identity = _identity(requested_mode=target)
    identity.update(
        {
            "primary_pid": 5123,
            "primary_process_epoch_sha256": "2" * 64,
            "observed_release_commit": "a" * 40,
            "observed_release_metadata_sha256": "3" * 64,
            "observed_release_tree_sha256": SOURCE,
        }
    )
    return identity


def _issue_request(storage: _Storage, target: SupervisorMode) -> dict[str, Any]:
    report = build_production_baseline(storage.conn, limit=100)
    budget = SupervisorLatencyBudgetDocument(
        target_mode=target,
        source_revision_sha256=SOURCE,
        maximum_user_visible_latency_ms=1_000,
    ).payload()
    return {
        "schema": REPRESENTATIVE_WINDOW_ISSUE_REQUEST_SCHEMA,
        "target_mode": target.value,
        "baseline_file_sha256": representative_window_sha256(report),
        "baseline": report,
        "registry_binding_sha256": REGISTRY,
        "latency_budget_file_sha256": representative_window_sha256(budget),
        "latency_budget": budget,
        "precursor_assist_promotion_evidence_sha256": (
            PRECURSOR if target is SupervisorMode.CANARY else None
        ),
    }


def _consume_request(issue: dict[str, Any]) -> dict[str, Any]:
    attestation = issue["server_attestation"]
    return {
        "schema": REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA,
        "attestation_lookup_token": issue["attestation_lookup_token"],
        "server_attestation_sha256": issue["server_attestation_sha256"],
        "target_mode": attestation["target_mode"],
        "baseline_file_sha256": attestation["baseline_file_sha256"],
        "baseline_report_sha256": attestation["baseline_report_sha256"],
        "latency_budget_file_sha256": attestation["latency_budget_file_sha256"],
        "latency_budget_document_sha256": attestation["latency_budget_document_sha256"],
        "source_revision_sha256": attestation["source_revision_sha256"],
        "registry_binding_sha256": attestation["registry_binding_sha256"],
        "observer_runner_sha256": attestation["observer_runner_sha256"],
        "precursor_assist_promotion_evidence_sha256": attestation[
            "precursor_assist_promotion_evidence_sha256"
        ],
    }


@pytest.mark.parametrize("target", (SupervisorMode.ASSIST, SupervisorMode.CANARY))
@pytest.mark.parametrize("effect_mode", ("off", "shadow"))
def test_real_scheduler_remains_valid_for_representative_window_identity(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    target: SupervisorMode,
    effect_mode: str,
) -> None:
    predecessor = _predecessor_mode(target)
    scheduler = _real_admitted_scheduler(predecessor, effect_mode=effect_mode)
    configured = replace(settings, semantic_supervisor_mode=predecessor.value)
    monkeypatch.setattr(
        window_module,
        "_live_release_identity",
        lambda *, verify_tree: {
            "predecessor_release_commit": "f" * 40,
            "predecessor_release_metadata_sha256": "1" * 64,
            "predecessor_release_tree_manifest_sha256": OBSERVED_SOURCE,
        },
    )
    monkeypatch.setattr(
        window_module,
        "operational_capability_snapshot",
        lambda: SimpleNamespace(digest_hex=lambda: REGISTRY),
    )
    try:
        identity = representative_window_current_server_identity(
            configured,
            scheduler,
            target_mode=target,
        )
    finally:
        asyncio.run(scheduler.aclose())

    expected_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(predecessor)
    assert identity["requested_mode"] == predecessor.value
    assert identity["supervisor_policy_id"] == expected_policy.policy_id
    assert identity["supervisor_policy_sha256"] == expected_policy.policy_sha256
    assert identity["observed_registry_binding_sha256"] == REGISTRY


@pytest.mark.parametrize("target", (SupervisorMode.ASSIST, SupervisorMode.CANARY))
@pytest.mark.parametrize("epoch_admitted", (False, True))
@pytest.mark.parametrize("effect_mode", ("off", "shadow"))
def test_target_restart_identity_accepts_exact_optional_scheduler_state(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    target: SupervisorMode,
    epoch_admitted: bool,
    effect_mode: str,
) -> None:
    scheduler = _real_admitted_scheduler(
        target,
        effect_mode=effect_mode,
        epoch_admitted=epoch_admitted,
    )
    configured = replace(settings, semantic_supervisor_mode=target.value)
    monkeypatch.setattr(
        window_module,
        "_live_release_identity",
        lambda *, verify_tree: {
            "predecessor_release_commit": "f" * 40,
            "predecessor_release_metadata_sha256": "1" * 64,
            "predecessor_release_tree_manifest_sha256": SOURCE,
        },
    )
    monkeypatch.setattr(
        window_module,
        "operational_capability_snapshot",
        lambda: SimpleNamespace(digest_hex=lambda: REGISTRY),
    )
    try:
        identity = representative_window_target_server_identity_after_restart(
            configured,
            scheduler,
            target_mode=target,
        )
        with pytest.raises(RepresentativeWindowAttestationError):
            representative_window_target_server_identity_after_restart(
                replace(configured, semantic_supervisor_mode=_predecessor_mode(target).value),
                scheduler,
                target_mode=target,
            )
    finally:
        asyncio.run(scheduler.aclose())

    expected_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(target)
    assert identity["requested_mode"] == target.value
    assert identity["supervisor_policy_id"] == expected_policy.policy_id
    assert identity["observed_release_tree_sha256"] == SOURCE
    assert identity["observed_registry_binding_sha256"] == REGISTRY


@pytest.mark.parametrize("target", (SupervisorMode.ASSIST, SupervisorMode.CANARY))
def test_server_recomputes_consumes_and_restart_verifies_exact_window(target: SupervisorMode) -> None:
    storage = _Storage()
    _seed_shadow(storage)
    if target is SupervisorMode.CANARY:
        _seed_assist(storage)
    request = _issue_request(storage, target)

    issue = issue_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=request,
        current_server_identity=_predecessor_identity(target),
        now=NOW,
    )

    attestation = issue["server_attestation"]
    assert attestation["target_mode"] == target.value
    assert attestation["observed_mode"] == (
        SupervisorMode.SHADOW.value if target is SupervisorMode.ASSIST else SupervisorMode.ASSIST.value
    )
    assert attestation["requested_mode"] == _predecessor_mode(target).value
    assert attestation["source_revision_sha256"] == SOURCE
    assert attestation["observed_release_tree_sha256"] == OBSERVED_SOURCE
    assert attestation["observer_runner_sha256"] == representative_window_observer_runner_sha256()
    assert attestation["server_recomputed"] is True
    assert attestation["synthetic_authority"] is False
    persisted = storage.conn.execute("SELECT response_json FROM request_idempotency").fetchone()[0]
    assert issue["attestation_lookup_token"] not in persisted
    assert "PRIVATE BODY" not in json.dumps(issue)

    consume_request = _consume_request(issue)
    consumed = consume_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=consume_request,
        current_server_identity=_predecessor_identity(target),
        now=NOW + 1,
    )
    assert consumed["status"] == "consumed"

    # Exact retry is read-only/idempotent, including after the short issue TTL.
    retry = consume_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=consume_request,
        current_server_identity=_restart_identity(target),
        now=NOW + REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC + 100,
    )
    assert retry == consumed
    accepted = verify_persisted_consumed_representative_window_issue(
        storage,
        user_id="usr_owner",
        issue_value=issue,
        current_server_identity=_restart_identity(target),
        now=NOW + REPRESENTATIVE_WINDOW_ATTESTATION_TTL_SEC + 100,
    )
    assert type(accepted) is AcceptedRepresentativeWindowAttestation
    assert is_accepted_representative_window_attestation(accepted)
    assert accepted.target_mode is target
    assert accepted.baseline_report_sha256 == request["baseline"]["report_sha256"]
    with pytest.raises(RepresentativeWindowAttestationError, match="not accepted"):
        replace(accepted, source_revision_sha256="9" * 64)


@pytest.mark.parametrize(
    ("target", "requested_mode", "policy_mode"),
    (
        (SupervisorMode.ASSIST, SupervisorMode.SHADOW, SupervisorMode.ASSIST),
        (SupervisorMode.CANARY, SupervisorMode.ASSIST, SupervisorMode.SHADOW),
    ),
)
def test_server_rejects_mixed_predecessor_mode_and_policy_identity(
    target: SupervisorMode,
    requested_mode: SupervisorMode,
    policy_mode: SupervisorMode,
) -> None:
    storage = _Storage()
    _seed_shadow(storage)
    if target is SupervisorMode.CANARY:
        _seed_assist(storage)

    with pytest.raises(RepresentativeWindowAttestationError, match="server identity"):
        issue_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=_issue_request(storage, target),
            current_server_identity=_identity(
                requested_mode=requested_mode,
                policy_mode=policy_mode,
            ),
            now=NOW,
        )


def test_candidate_cannot_self_attest_synthetic_drift_or_wrong_bindings() -> None:
    storage = _Storage()
    _seed_shadow(storage, count=0)
    request = _issue_request(storage, SupervisorMode.ASSIST)
    with pytest.raises(RepresentativeWindowAttestationError, match="complete population"):
        issue_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=request,
            current_server_identity=_identity(),
            now=NOW,
        )

    storage = _Storage()
    _seed_shadow(storage)
    request = _issue_request(storage, SupervisorMode.ASSIST)
    _insert_trace(storage, 999)
    storage.conn.commit()
    with pytest.raises(RepresentativeWindowAttestationError, match="recomputed"):
        issue_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=request,
            current_server_identity=_identity(),
            now=NOW,
        )

    request = _issue_request(storage, SupervisorMode.ASSIST)
    request["registry_binding_sha256"] = "9" * 64
    with pytest.raises(RepresentativeWindowAttestationError, match="registry"):
        issue_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=request,
            current_server_identity=_identity(),
            now=NOW,
        )


def test_consume_is_one_shot_bound_and_failed_attempt_rolls_back() -> None:
    storage = _Storage()
    _seed_shadow(storage)
    request = _issue_request(storage, SupervisorMode.ASSIST)
    issue = issue_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=request,
        current_server_identity=_identity(),
        now=NOW,
    )
    consume_request = _consume_request(issue)
    changed = dict(consume_request)
    changed["observer_runner_sha256"] = "9" * 64
    with pytest.raises(RepresentativeWindowAttestationError, match="binding"):
        consume_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=changed,
            current_server_identity=_identity(),
            now=NOW + 1,
        )
    stored = json.loads(storage.conn.execute("SELECT response_json FROM request_idempotency").fetchone()[0])
    assert stored["consume_state"] == "unused"

    consumed = consume_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=consume_request,
        current_server_identity=_identity(),
        now=NOW + 2,
    )
    assert consumed["status"] == "consumed"
    conflicting = dict(consume_request)
    conflicting["baseline_report_sha256"] = "8" * 64
    with pytest.raises(RuntimeError, match="already consumed"):
        consume_representative_window_attestation(
            storage,
            user_id="usr_owner",
            request_value=conflicting,
            current_server_identity=_identity(),
            now=NOW + 3,
        )

    tampered_issue = json.loads(json.dumps(issue))
    tampered_issue["attestation_lookup_token"] = "7" * 64
    with pytest.raises(RepresentativeWindowAttestationError, match="issue envelope"):
        verify_persisted_consumed_representative_window_issue(
            storage,
            user_id="usr_owner",
            issue_value=tampered_issue,
            current_server_identity=_restart_identity(SupervisorMode.ASSIST),
            now=NOW + 3,
        )


@pytest.mark.asyncio
async def test_refresh_representative_window_runtime_admission_demands_scheduler_probe() -> None:
    calls: list[float] = []

    class Secondary:
        async def refresh_semantic_supervisor_runtime_admission(
            self,
            *,
            absolute_deadline_monotonic: float,
        ) -> bool:
            calls.append(absolute_deadline_monotonic)
            return True

    assert (
        await refresh_representative_window_runtime_admission(
            Secondary(),
            absolute_deadline_monotonic=12.5,
        )
        is True
    )
    assert calls == [12.5]


@pytest.mark.asyncio
async def test_refresh_representative_window_runtime_admission_is_fail_closed() -> None:
    class Missing:
        pass

    class Broken:
        async def refresh_semantic_supervisor_runtime_admission(
            self,
            *,
            absolute_deadline_monotonic: float,
        ) -> bool:
            raise RuntimeError("probe failed")

    class Falsey:
        async def refresh_semantic_supervisor_runtime_admission(
            self,
            *,
            absolute_deadline_monotonic: float,
        ) -> bool:
            return False

    deadline = time.monotonic() + 1.0
    assert (
        await refresh_representative_window_runtime_admission(
            Missing(),
            absolute_deadline_monotonic=deadline,
        )
        is False
    )
    assert (
        await refresh_representative_window_runtime_admission(
            Broken(),
            absolute_deadline_monotonic=deadline,
        )
        is False
    )
    assert (
        await refresh_representative_window_runtime_admission(
            Falsey(),
            absolute_deadline_monotonic=deadline,
        )
        is False
    )
    assert (
        await refresh_representative_window_runtime_admission(
            Missing(),
            absolute_deadline_monotonic=True,  # type: ignore[arg-type]
        )
        is False
    )


@pytest.mark.asyncio
async def test_admin_identity_refresh_runs_before_live_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.admin_api import _semantic_supervisor as admin_mod

    order: list[str] = []

    class Secondary:
        async def refresh_semantic_supervisor_runtime_admission(
            self,
            *,
            absolute_deadline_monotonic: float,
        ) -> bool:
            order.append("refresh")
            assert absolute_deadline_monotonic > time.monotonic()
            return True

    class State:
        secondary_brain = Secondary()

    class App:
        state = State()

    class Request:
        app = App()

    def fake_identity(request: object, mode: SupervisorMode) -> dict[str, object]:
        order.append("identity")
        assert mode is SupervisorMode.ASSIST
        return {"requested_mode": "shadow"}

    monkeypatch.setattr(admin_mod, "_current_identity", fake_identity)
    identity = await admin_mod._identity_after_runtime_refresh(
        Request(),  # type: ignore[arg-type]
        SupervisorMode.ASSIST,
    )
    assert identity == {"requested_mode": "shadow"}
    assert order == ["refresh", "identity"]


def test_after_restart_consume_accepts_predecessor_backend_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _Storage()
    _seed_shadow(storage)
    target = SupervisorMode.ASSIST
    request = _issue_request(storage, target)
    issue = issue_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=request,
        current_server_identity=_predecessor_identity(target),
        now=NOW,
    )
    consume_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=_consume_request(issue),
        current_server_identity=_predecessor_identity(target),
        now=NOW + 1,
    )
    monkeypatch.setattr(window_module, "__version__", "0.209.0")
    restart = _restart_identity(target)
    restart["primary_backend_version"] = "0.209.0"
    retry = consume_representative_window_attestation(
        storage,
        user_id="usr_owner",
        request_value=_consume_request(issue),
        current_server_identity=restart,
        now=NOW + 2,
    )
    assert retry["status"] == "consumed"


def test_after_restart_identity_match_allows_distinct_backend_version() -> None:
    predecessor = _predecessor_identity(SupervisorMode.ASSIST)
    restart = _restart_identity(SupervisorMode.ASSIST)
    attestation = {
        **predecessor,
        "target_mode": SupervisorMode.ASSIST.value,
        "source_revision_sha256": SOURCE,
        "registry_binding_sha256": REGISTRY,
        "primary_backend_version": "0.208.6",
    }
    assert restart["primary_backend_version"] == __version__
    assert attestation["primary_backend_version"] != restart["primary_backend_version"]
    assert _server_identity_matches(attestation, restart, after_restart=True) is True


def test_owner_trust_root_routes_are_exactly_registered() -> None:
    from friday.admin_api._semantic_supervisor import router

    registered = {(route.path, frozenset(route.methods or ())) for route in router.routes}
    assert registered == {
        (
            "/semantic-supervisor-witness/issue-representative-window-attestation",
            frozenset({"POST"}),
        ),
        (
            "/semantic-supervisor-witness/consume-representative-window-attestation",
            frozenset({"POST"}),
        ),
    }
