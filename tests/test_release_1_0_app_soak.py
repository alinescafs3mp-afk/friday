"""Focused mechanics tests for the finite application soak profile.

These tests use pure observations and synthetic request gates.  They do not
claim that the actual Friday app, scheduler or Telegram destination ran.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.release_1_0_app_soak import (
    EXPECTED_SCHEMA,
    POLICY_SCHEMA,
    REMINDER_DEPENDENTS,
    AppSoakError,
    BudgetGate,
    PrincipalPolicy,
    ResourceSample,
    SoakPolicy,
    _digest,
    _reminder_creation_failure,
    app_route_allowed,
    build_expected,
    continuation_safety_proved,
    evaluate_profile,
    independent_evidence_check,
    safe_settings_identity,
)


def _policy_payload() -> dict:
    return {
        "schema": POLICY_SCHEMA,
        "run_id": "0123456789abcdef0123456789abcdef",
        "candidate_sha256": "a" * 64,
        "suite_revision": "r10-app-soak-draft-1",
        "config_sha256": "b" * 64,
        "duration_sec": 1.0,
        "cadence_sec": 0.5,
        "deadline_sec": 2.0,
        "max_requests": 64,
        "request_concurrency": 2,
        "principals": [
            {"user_id": "owner", "chat_id": 5001, "preset_key": "owner"},
            {"user_id": "person-a", "chat_id": 5002, "preset_key": "user"},
            {"user_id": "person-b", "chat_id": 5003, "preset_key": "user"},
            {"user_id": "person-c", "chat_id": 5004, "preset_key": "user"},
        ],
        "resources": {
            "max_rss_bytes": 1 << 30,
            "max_fd_count": 4096,
            "max_thread_count": 1024,
            "max_database_bytes": 1 << 30,
            "max_wal_bytes": 1 << 30,
            "max_evidence_bytes": 1 << 30,
        },
    }


def _policy() -> SoakPolicy:
    return SoakPolicy.from_value(_policy_payload())


def _expected(policy: SoakPolicy) -> dict:
    value = build_expected(policy, local_day="2035-01-02")
    assert value["schema"] == EXPECTED_SCHEMA
    return value


def _operations() -> list[dict]:
    required = (
        "owner_bootstrap",
        "observe_principals",
        "owner_upload",
        "owner_source_query",
        "foreign_source_query",
        "owner_artifact_read",
        "foreign_artifact_refusal",
        "reminder_public_creation",
        "reminder_due_execution",
        "notification_pending_read",
        "notification_claim",
        "destination_write_and_readback",
        "notification_ack",
        "reminder_due_replay",
        "notification_after_replay",
    )
    return [{"name": name, "status": "PASS"} for name in required]


def _sample() -> ResourceSample:
    return ResourceSample(
        sequence=1,
        monotonic_ns=2_000_000_000,
        elapsed_ns=1_000_000_000,
        rss_bytes=64 << 20,
        fd_count=20,
        thread_count=8,
        database_bytes=1 << 20,
        wal_bytes=4096,
        evidence_bytes=8192,
    )


def _core(policy: SoakPolicy, expected: dict) -> dict:
    raw_id = "raw_0123456789abcdef"
    return {
        "bindings": [item.user_id for item in policy.principals],
        "source": {
            "raw_object_id": raw_id,
            "owner_user_id": policy.owner.user_id,
            "source_ref": expected["file"]["source_ref"],
            "canary_observed": True,
            "matching_sources": 1,
        },
        "foreign": {
            "user_id": expected["foreign_user_id"],
            "source_count": 0,
            "source_leak": False,
            "artifact_status": 404,
            "artifact_leak": False,
        },
        "artifact": {"sha256": expected["file"]["sha256"], "readback": True},
        "publication": {
            "creation_status": "PASS",
            "notification_id": "notif_0123456789abcdef",
            "destination": {
                "notification_id": "notif_0123456789abcdef",
                "chat_id": expected["publication"]["destination_chat_id"],
                "body": expected["publication"]["exact_body"],
            },
            "durable_rows": 1,
            "durable_status": "sent",
            "replayed": False,
            "delivery_class": "in_process_synthetic_destination",
        },
    }


def _evaluate(core: dict, **changes) -> dict:
    policy = _policy()
    expected = _expected(policy)
    arguments = {
        "policy": policy,
        "expected": expected,
        "core": core,
        "operations": _operations(),
        "samples": [_sample()],
        "budget": {
            "started": 21,
            "completed": 21,
            "errors": 0,
            "inflight": 0,
            "max_global_inflight": 2,
            "max_principal_inflight": 1,
        },
        "elapsed_sec": 1.0,
        "config_sha256": policy.config_sha256,
        "evidence_mode": "synthetic",
    }
    arguments.update(changes)
    return evaluate_profile(**arguments)


def test_complete_synthetic_observation_is_never_release_go() -> None:
    policy = _policy()
    result = _evaluate(_core(policy, _expected(policy)))

    assert result["status"] == "SYNTHETIC_MECHANICS_OBSERVED"
    assert result["failure_codes"] == []
    assert result["go_emitted"] is False
    assert result["release_ready_claimed"] is False
    assert result["live_telegram_proof"] is False


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("duration_sec", 0, "duration_invalid"),
        ("cadence_sec", float("nan"), "cadence_invalid"),
        ("deadline_sec", 0.5, "policy_time_order_invalid"),
        ("request_concurrency", 5, "request_concurrency_invalid"),
        ("max_requests", 28, "request_budget_cannot_cover_policy"),
    ],
)
def test_policy_rejects_unbounded_or_internally_inconsistent_values(field: str, value, code: str) -> None:
    payload = _policy_payload()
    payload[field] = value

    with pytest.raises(AppSoakError, match=code):
        SoakPolicy.from_value(payload)


def test_policy_has_no_silent_default_or_unknown_field() -> None:
    missing = _policy_payload()
    missing.pop("cadence_sec")
    with pytest.raises(AppSoakError, match="policy_keys_invalid"):
        SoakPolicy.from_value(missing)

    unknown = _policy_payload()
    unknown["frozen_release_scope"] = True
    with pytest.raises(AppSoakError, match="policy_keys_invalid"):
        SoakPolicy.from_value(unknown)


@pytest.mark.parametrize("alias_axis", ["user", "chat"])
def test_four_owner_aliases_or_duplicate_principals_are_rejected(alias_axis: str) -> None:
    payload = _policy_payload()
    if alias_axis == "user":
        payload["principals"][2]["user_id"] = payload["principals"][1]["user_id"]
        code = "principal_user_alias"
    else:
        payload["principals"][2]["chat_id"] = payload["principals"][1]["chat_id"]
        code = "principal_chat_alias"
    with pytest.raises(AppSoakError, match=code):
        SoakPolicy.from_value(payload)

    payload = _policy_payload()
    for principal in payload["principals"]:
        principal["preset_key"] = "owner"
    with pytest.raises(AppSoakError, match="one_owner_three_users_required"):
        SoakPolicy.from_value(payload)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda core: core["bindings"].__setitem__(3, core["bindings"][0]), "binding_observation_changed"),
        (lambda core: core["source"].__setitem__("matching_sources", 0), "source_outcome_invalid"),
        (lambda core: core["source"].__setitem__("source_ref", "wrong-source"), "source_outcome_invalid"),
        (lambda core: core["source"].__setitem__("canary_observed", False), "source_outcome_invalid"),
        (lambda core: core["artifact"].__setitem__("sha256", "0" * 64), "artifact_readback_invalid"),
        (lambda core: core["artifact"].__setitem__("readback", False), "artifact_readback_invalid"),
    ],
)
def test_source_canary_attribution_artifact_and_binding_fail_closed(mutation, code: str) -> None:
    policy = _policy()
    expected = _expected(policy)
    core = _core(policy, expected)
    mutation(core)

    assert code in _evaluate(core)["failure_codes"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_count", 1),
        ("source_leak", True),
        ("artifact_status", 200),
        ("artifact_leak", True),
        ("user_id", "owner"),
    ],
)
def test_foreign_tenant_negatives_are_outcomes_not_http_200(field: str, value) -> None:
    policy = _policy()
    core = _core(policy, _expected(policy))
    core["foreign"][field] = value

    assert "foreign_tenant_negative_invalid" in _evaluate(core)["failure_codes"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("durable_rows", 0),
        ("durable_rows", 2),
        ("durable_status", "pending"),
        ("replayed", True),
        ("delivery_class", "telegram_live"),
    ],
)
def test_missing_duplicate_or_unconfirmed_publication_fails(field: str, value) -> None:
    policy = _policy()
    core = _core(policy, _expected(policy))
    core["publication"][field] = value

    assert "scheduled_publication_invalid" in _evaluate(core)["failure_codes"]


def test_wrong_destination_or_body_fails() -> None:
    policy = _policy()
    expected = _expected(policy)
    for field, value in (("chat_id", "9999"), ("body", "expected-looking-but-not-observed")):
        core = _core(policy, expected)
        core["publication"]["destination"][field] = value
        assert "scheduled_publication_invalid" in _evaluate(core)["failure_codes"]


def test_counter_deadline_and_resource_budgets_are_independent_failures() -> None:
    policy = _policy()
    core = _core(policy, _expected(policy))
    bad_budget = {
        "started": 22,
        "completed": 21,
        "errors": 0,
        "inflight": 1,
        "max_global_inflight": 3,
        "max_principal_inflight": 2,
    }
    result = _evaluate(core, budget=bad_budget, elapsed_sec=2.1)
    assert "request_accounting_invalid" in result["failure_codes"]
    assert "time_contract_invalid" in result["failure_codes"]

    oversized = copy.copy(_sample())
    object.__setattr__(oversized, "rss_bytes", policy.resources.max_rss_bytes + 1)
    result = _evaluate(core, samples=[oversized])
    assert "rss_budget_exceeded" in result["failure_codes"]


def test_missing_or_incomplete_semantic_operation_fails() -> None:
    policy = _policy()
    core = _core(policy, _expected(policy))
    operations = _operations()
    operations[-1]["status"] = "INCOMPLETE_OR_ERROR"
    result = _evaluate(core, operations=operations)

    assert "operation_status_invalid" in result["failure_codes"]

    result = _evaluate(core, operations=operations[:-1])
    assert "required_operation_cardinality_invalid" in result["failure_codes"]


def test_budget_gate_rejects_same_principal_global_overflow_count_and_deadline() -> None:
    policy = _policy()
    now = [0.0]
    gate = BudgetGate(policy, started=0.0, clock=lambda: now[0])

    gate.begin("a")
    with pytest.raises(AppSoakError, match="concurrent_request_for_principal"):
        gate.begin("a")
    gate.begin("b")
    with pytest.raises(AppSoakError, match="global_request_concurrency_exceeded"):
        gate.begin("c")
    gate.finish("a", error=False)
    gate.finish("b", error=True)
    assert gate.snapshot()["max_global_inflight"] == 2
    assert gate.snapshot()["max_principal_inflight"] == 1

    gate.started = policy.max_requests
    with pytest.raises(AppSoakError, match="request_budget_exhausted"):
        gate.begin("c")
    gate.started = 2
    now[0] = policy.deadline_sec
    with pytest.raises(AppSoakError, match="global_deadline_exhausted"):
        gate.begin("c")


@pytest.mark.parametrize(
    ("method", "target", "allowed"),
    [
        ("GET", "/api/me", True),
        ("POST", "/api/files", True),
        ("GET", "/api/knowledge/sources?q=canary&limit=20", True),
        ("GET", "/api/knowledge/sources?limit=20&q=canary&q=other", False),
        ("GET", "/api/search?q=canary", False),
        ("GET", "/api/notifications/pending?limit=100", True),
        ("GET", "/api/notifications/pending?limit=20", False),
        ("POST", "/api/notifications/notif_123/claim", True),
        ("POST", "/api/notifications/ack", True),
        ("POST", "/api/admin/users", False),
    ],
)
def test_signed_workload_route_allowlist_is_exact(method: str, target: str, allowed: bool) -> None:
    assert app_route_allowed(method, target) is allowed


def test_settings_identity_records_bindings_without_secret_values(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        home=tmp_path,
        database_path=tmp_path / "friday.db",
        files_dir=tmp_path / "files",
        profile=SimpleNamespace(name="isolated"),
        shared_archive=False,
        llm_enabled=False,
        local_timezone="Europe/Moscow",
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_effective_allowed_chat_ids=(5001, 5002, 5003, 5004),
        telegram_owner_chat_ids=(5001,),
        api_token="TOP-SECRET-OWNER-TOKEN",
        telegram_bridge_secret="TOP-SECRET-BRIDGE",
    )

    identity = safe_settings_identity(settings)
    encoded = json.dumps(identity)
    assert "TOP-SECRET" not in encoded
    assert identity["api_token_configured"] is True
    assert identity["bridge_secret_configured"] is True
    assert identity["telegram_allowed_chat_ids"] == [5001, 5002, 5003, 5004]


def test_independent_reader_rejects_missing_terminal_and_changed_run_identity(tmp_path: Path) -> None:
    policy = _policy()
    expected = _expected(policy)
    (tmp_path / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    (tmp_path / "destination-000001.json").write_text(
        json.dumps(
            {
                "chat_id": expected["publication"]["destination_chat_id"],
                "body": expected["publication"]["exact_body"],
            }
        ),
        encoding="utf-8",
    )
    session = tmp_path / "session-owner"
    session.mkdir()
    (session / "request-000001.start.json").write_text("{}", encoding="utf-8")
    provisional = {
        "candidate_sha256": "c" * 64,
        "suite_revision": policy.suite_revision,
        "config_sha256": policy.config_sha256,
        "request_accounting": {
            "started": 1,
            "completed": 1,
            "errors": 0,
            "inflight": 0,
        },
    }

    result = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=provisional,
    )

    assert result["status"] == "EVIDENCE_INCOMPLETE"
    assert "attempt_evidence_incomplete" in result["failure_codes"]
    assert "run_identity_changed" in result["failure_codes"]


def test_budget_gate_is_thread_safe_for_four_distinct_principals() -> None:
    payload = _policy_payload()
    payload["request_concurrency"] = 4
    policy = SoakPolicy.from_value(payload)
    gate = BudgetGate(policy, started=0.0, clock=lambda: 0.0)
    barrier = threading.Barrier(4)
    failures: list[str] = []

    def request(principal: PrincipalPolicy) -> None:
        try:
            gate.begin(principal.user_id)
            barrier.wait(timeout=2)
            gate.finish(principal.user_id, error=False)
        except Exception as exc:  # pragma: no cover - assertion reports exact failure
            failures.append(type(exc).__name__)

    threads = [threading.Thread(target=request, args=(item,)) for item in policy.principals]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert failures == []
    assert gate.snapshot() == {
        "started": 4,
        "completed": 4,
        "errors": 0,
        "inflight": 0,
        "max_global_inflight": 4,
        "max_principal_inflight": 1,
    }


def test_foreign_empty_source_search_allows_only_the_exact_public_query_echo() -> None:
    from tools.release_1_0_app_soak import validate_foreign_source_response

    validate_foreign_source_response(
        {"query": "private-canary", "items": [], "count": 0, "excludes": "ignored"},
        canary="private-canary",
        raw_id="raw_0123456789abcdef",
        source_ref="private-source",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"items": [{"excerpt": "private-canary"}], "count": 1},
        {"items": [{"excerpt": "private-canary"}]},
        {"query": "private-canary extra-source-content"},
        {"excerpt": "private-canary"},
        {"metadata": {"id": "raw_0123456789abcdef"}},
        {"metadata": {"source_ref": "private-source"}},
        {"items": None},
        {"count": False},
    ],
)
def test_public_query_echo_exception_does_not_hide_source_fields(changes: dict) -> None:
    from tools.release_1_0_app_soak import validate_foreign_source_response

    response = {"query": "private-canary", "items": [], "count": 0, "excludes": "ignored", **changes}
    with pytest.raises(AppSoakError):
        validate_foreign_source_response(
            response,
            canary="private-canary",
            raw_id="raw_0123456789abcdef",
            source_ref="private-source",
        )


def _failed_creation_operations() -> list[dict]:
    failure = "reminder_creation_not_observed"
    outcomes = _operations()
    for item in outcomes:
        item["principal"] = (
            "all"
            if item["name"] == "observe_principals"
            else "product-reminder-worker"
            if item["name"] in {"reminder_due_execution", "reminder_due_replay"}
            else "owner"
        )
        if item["name"] == "reminder_public_creation":
            item.update({"status": "FAIL", "reason_code": failure})
        elif item["name"] in REMINDER_DEPENDENTS:
            item.update(
                {
                    "status": "NOT_RUN",
                    "reason_code": "dependency_failed",
                    "depends_on": "reminder_public_creation",
                    "dependency_reason_code": failure,
                }
            )
    return outcomes


def test_http_200_without_remind_attribution_or_row_is_one_functional_failure() -> None:
    assert (
        _reminder_creation_failure(
            tools_used=[],
            rows=[],
            owner_user_id="owner",
            reminder_name="APP-SOAK-REMINDER-ONE",
            occurred_at="2035-01-02",
        )
        == "reminder_creation_not_observed"
    )
    policy = _policy()
    expected = _expected(policy)
    core = _core(policy, expected)
    core["publication"] = {
        "creation_status": "FAIL",
        "creation_failure_code": "reminder_creation_not_observed",
        "notification_id": None,
        "destination": None,
        "durable_rows": 0,
        "durable_status": "NOT_RUN",
        "replayed": None,
        "delivery_class": "in_process_synthetic_destination",
    }
    result = _evaluate(
        core,
        operations=_failed_creation_operations(),
        budget={
            "started": 17,
            "completed": 17,
            "errors": 0,
            "inflight": 0,
            "max_global_inflight": 1,
            "max_principal_inflight": 1,
        },
        duration_coverage={
            "status": "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL",
            "identity_get_rounds": 1,
            "reminder_branch": "MISSING_AFTER_CREATION_FAIL",
        },
    )

    assert result["status"] == "APP_SOAK_PROFILE_FAILED"
    assert result["functional_status"] == "FAIL"
    assert result["failure_codes"] == ["reminder_creation_not_observed"]
    assert result["operation_outcomes"] == {"PASS": 7, "FAIL": 1, "NOT_RUN": 7, "BLOCKED": 0}
    assert "scheduled_publication_invalid" not in result["failure_codes"]


def test_safe_duration_continuation_is_narrow_and_requires_proof() -> None:
    policy = _policy()
    base = [
        {"name": name, "status": "PASS"}
        for name in (
            "owner_bootstrap",
            "observe_principals",
            "owner_upload",
            "owner_source_query",
            "foreign_source_query",
            "owner_artifact_read",
            "foreign_artifact_refusal",
        )
    ]
    sessions = {item.user_id: object() for item in policy.principals}
    effect = {"tools_used": [], "rows": [], "http_status": 200}
    assert continuation_safety_proved(
        failure_code="reminder_creation_not_observed",
        operations=base,
        sessions=sessions,
        policy=policy,
        reminder_effect=effect,
    )
    assert not continuation_safety_proved(
        failure_code="foreign_source_visible",
        operations=base,
        sessions=sessions,
        policy=policy,
        reminder_effect=effect,
    )
    assert not continuation_safety_proved(
        failure_code="reminder_creation_not_observed",
        operations=base[:-1],
        sessions=sessions,
        policy=policy,
        reminder_effect=effect,
    )
    assert not continuation_safety_proved(
        failure_code="reminder_creation_not_observed",
        operations=base,
        sessions={policy.owner.user_id: object()},
        policy=policy,
        reminder_effect=effect,
    )
    assert not continuation_safety_proved(
        failure_code="reminder_creation_not_observed",
        operations=base,
        sessions=sessions,
        policy=policy,
        reminder_effect={**effect, "rows": [{"unexpected": "effect"}]},
    )


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _truthful_red_evidence(root: Path) -> tuple[SoakPolicy, dict, dict]:
    config = {"schema": "app-soak-test-config", "identity": "isolated"}
    payload = _policy_payload()
    payload["config_sha256"] = _digest(config)
    policy = SoakPolicy.from_value(payload)
    expected = _expected(policy)
    _write_json(root / "expected.json", expected)
    _write_json(root / "config-identity.json", config)
    for principal in policy.principals:
        (root / f"session-{principal.user_id}").mkdir()
    (root / "admin").mkdir()

    raw_id = "raw_0123456789abcdef"
    response = json.dumps({"message": "offline", "tools_used": []}, sort_keys=True).encode()

    def request(
        directory: Path,
        sequence: int,
        method: str,
        target: str,
        response_bytes: bytes = b"{}",
        status_code: int = 200,
    ) -> None:
        if method == "GET":
            request_bytes = b""
        elif target == "/api/chat":
            request_bytes = json.dumps(
                {
                    "message": expected["publication"]["request"],
                    "enable_tools": True,
                    "telegram_user": {
                        "id": policy.owner.chat_id,
                        "first_name": "AppSoak",
                        "language_code": "ru",
                    },
                },
                sort_keys=True,
            ).encode()
        else:
            request_bytes = b"{}"
        prefix = directory / f"request-{sequence:06d}"
        prefix.with_suffix(".request.bin").write_bytes(request_bytes)
        _write_json(
            prefix.with_suffix(".start.json"),
            {
                "sequence": sequence,
                "method": method,
                "path": target,
                "started_monotonic_ns": sequence,
                "started_unix_s": 2,
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "request_bytes": len(request_bytes),
            },
        )
        prefix.with_suffix(".response.bin").write_bytes(response_bytes)
        _write_json(
            prefix.with_suffix(".terminal.json"),
            {
                "sequence": sequence,
                "outcome": "HTTP_OBSERVED",
                "status_code": status_code,
                "elapsed_ns": 1,
                "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                "response_bytes": len(response_bytes),
            },
        )

    admin = root / "admin"
    for sequence in range(1, 7):
        request(
            admin,
            sequence,
            "POST",
            "/api/admin/users" if sequence % 2 else "/api/admin/identities",
        )
    owner_session = root / f"session-{policy.owner.user_id}"

    def identity_response(principal: PrincipalPolicy) -> bytes:
        return json.dumps(
            {
                "actor": {
                    "user_id": principal.user_id,
                    "preset_key": principal.preset_key,
                    "source": "telegram-bridge",
                },
                "user": {
                    "id": principal.user_id,
                    "preset_key": principal.preset_key,
                    "status": "active",
                },
            },
            sort_keys=True,
        ).encode()

    owner_identity = identity_response(policy.owner)
    request(owner_session, 1, "GET", "/api/me", owner_identity)
    request(owner_session, 2, "GET", "/api/me", owner_identity)
    request(
        owner_session,
        3,
        "POST",
        "/api/files",
        json.dumps({"raw_object_id": raw_id}, sort_keys=True).encode(),
    )
    query = f"/api/knowledge/sources?q={expected['file']['canary']}&limit=20"
    owner_query = {
        "query": expected["file"]["canary"],
        "items": [
            {
                "id": raw_id,
                "source_ref": expected["file"]["source_ref"],
                "excerpt": expected["file"]["text"],
            }
        ],
        "count": 1,
        "excludes": "ignored",
    }
    request(owner_session, 4, "GET", query, json.dumps(owner_query, sort_keys=True).encode())
    request(owner_session, 5, "GET", f"/api/files/{raw_id}", expected["file"]["text"].encode())
    request(owner_session, 6, "POST", "/api/chat", response)
    foreign = next(item for item in policy.principals if item.user_id == expected["foreign_user_id"])
    foreign_session = root / f"session-{foreign.user_id}"
    request(foreign_session, 1, "GET", "/api/me", identity_response(foreign))
    foreign_query = {
        "query": expected["file"]["canary"],
        "items": [],
        "count": 0,
        "excludes": "ignored",
    }
    request(
        foreign_session,
        2,
        "GET",
        query,
        json.dumps(foreign_query, sort_keys=True).encode(),
    )
    request(
        foreign_session,
        3,
        "GET",
        f"/api/files/{raw_id}",
        b'{"detail":"not found"}',
        404,
    )
    for principal in policy.principals:
        if principal.user_id not in {policy.owner.user_id, foreign.user_id}:
            request(
                root / f"session-{principal.user_id}",
                1,
                "GET",
                "/api/me",
                identity_response(principal),
            )

    outcomes = _failed_creation_operations()
    raw_outcomes = []
    for sequence, outcome in enumerate(outcomes, 1):
        prefix = root / f"operation-{sequence:04d}"
        if outcome["status"] == "NOT_RUN":
            _write_json(prefix.with_suffix(".not-run.json"), outcome)
            raw_outcomes.append(outcome)
            continue
        _write_json(
            prefix.with_suffix(".start.json"),
            {
                "name": outcome["name"],
                "principal": outcome["principal"],
                "started_monotonic_ns": sequence,
            },
        )
        terminal = {
            **outcome,
            "elapsed_ns": 1,
        }
        if outcome["status"] == "PASS":
            terminal["details"] = {}
            _write_json(prefix.with_suffix(".terminal.json"), terminal)
        else:
            terminal["exception_type"] = "AppSoakError"
            terminal["details"] = {
                "expected": {"tools_used": ["remind"], "durable_rows": 1},
                "actual": {
                    "tools_used": [],
                    "durable_rows": 0,
                    "effect_ref": "effect-reminder-creation.json",
                    "response_ref": f"session-{policy.owner.user_id}/request-000006.response.bin",
                },
            }
            _write_json(prefix.with_suffix(".error.json"), terminal)
        raw_outcomes.append(terminal)
    _write_json(
        root / "operations.json",
        {"items": raw_outcomes, "count": len(raw_outcomes)},
    )

    _write_json(
        root / "effect-source-row.json",
        {
            "schema": "friday.app-soak-source-effect.v1",
            "raw_object_id": raw_id,
            "user_id": policy.owner.user_id,
            "source_ref": expected["file"]["source_ref"],
            "content_hash": expected["file"]["sha256"],
        },
    )
    _write_json(
        root / "effect-reminder-creation.json",
        {
            "schema": "friday.app-soak-reminder-creation-effect.v1",
            "owner_user_id": policy.owner.user_id,
            "reminder_name": expected["publication"]["canary"],
            "http_status": 200,
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "response_ref": f"session-{policy.owner.user_id}/request-000006.response.bin",
            "tools_used": [],
            "rows": [],
        },
    )
    core = _core(policy, expected)
    core["publication"] = {
        "creation_status": "FAIL",
        "creation_failure_code": "reminder_creation_not_observed",
        "notification_id": None,
        "destination": None,
        "durable_rows": 0,
        "durable_status": "NOT_RUN",
        "replayed": None,
        "delivery_class": "in_process_synthetic_destination",
    }
    _write_json(root / "core-observation.json", core)
    _write_json(
        root / "resource-000001.json",
        {
            "sequence": 1,
            "monotonic_ns": 2,
            "elapsed_ns": 1,
            "rss_bytes": 64 << 20,
            "fd_count": 20,
            "thread_count": 8,
            "database_bytes": 1 << 20,
            "wal_bytes": 4096,
            "evidence_bytes": 8192,
        },
    )
    provisional = {
        "schema": "friday.release-1-0-app-soak.v1",
        "status": "APP_SOAK_PROFILE_FAILED",
        "functional_status": "FAIL",
        "failure_codes": ["reminder_creation_not_observed"],
        "candidate_sha256": policy.candidate_sha256,
        "suite_revision": policy.suite_revision,
        "config_sha256": policy.config_sha256,
        "request_accounting": {
            "started": 17,
            "completed": 17,
            "errors": 0,
            "inflight": 0,
            "max_global_inflight": 1,
            "max_principal_inflight": 1,
        },
        "resource_samples": 1,
        "duration_coverage": {
            "status": "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL",
            "identity_get_rounds": 0,
            "requests_per_round": 4,
            "reminder_branch": "MISSING_AFTER_CREATION_FAIL",
            "full_planned_mixed_soak": False,
        },
    }
    return policy, expected, provisional


def test_independent_reader_accepts_complete_truthful_red_without_promoting_it(
    tmp_path: Path,
) -> None:
    policy, expected, provisional = _truthful_red_evidence(tmp_path)

    result = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=provisional,
    )

    assert result["status"] == "EVIDENCE_COMPLETE"
    assert result["observed_profile_status"] == "FAIL"
    promoted = {**provisional, "status": "APP_SOAK_PROFILE_OBSERVED", "functional_status": "PASS"}
    rejected = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=promoted,
    )
    assert rejected["status"] == "EVIDENCE_INCOMPLETE"
    assert "functional_failure_promoted" in rejected["failure_codes"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_start",
        "missing_terminal",
        "orphan_response",
        "wrong_sequence",
        "duplicate_operation",
        "corrupt_terminal_hash",
        "wrong_accounting",
        "mixed_identity",
        "semantic_artifact_change",
    ],
)
def test_independent_reader_rejects_missing_or_corrupt_raw_predecessor(
    tmp_path: Path,
    fault: str,
) -> None:
    policy, expected, provisional = _truthful_red_evidence(tmp_path)
    session = tmp_path / f"session-{policy.owner.user_id}"
    if fault == "missing_start":
        (session / "request-000001.start.json").unlink()
    elif fault == "missing_terminal":
        (session / "request-000006.terminal.json").unlink()
    elif fault == "orphan_response":
        (session / "request-000007.response.bin").write_bytes(b"orphan")
    elif fault == "wrong_sequence":
        for path in list(session.glob("request-000006.*")):
            path.rename(path.with_name(path.name.replace("000006", "000008")))
    elif fault == "duplicate_operation":
        for suffix in ("start", "terminal"):
            path = tmp_path / f"operation-0002.{suffix}.json"
            value = json.loads(path.read_text())
            value["name"] = "owner_bootstrap"
            _write_json(path, value)
        aggregate_path = tmp_path / "operations.json"
        aggregate = json.loads(aggregate_path.read_text())
        aggregate["items"][1]["name"] = "owner_bootstrap"
        _write_json(aggregate_path, aggregate)
    elif fault == "corrupt_terminal_hash":
        terminal = json.loads((session / "request-000001.terminal.json").read_text())
        terminal["response_sha256"] = "0" * 64
        _write_json(session / "request-000001.terminal.json", terminal)
    elif fault == "wrong_accounting":
        provisional["request_accounting"] = {**provisional["request_accounting"], "started": 2}
    elif fault == "mixed_identity":
        for suffix in ("start", "terminal"):
            path = tmp_path / f"operation-0001.{suffix}.json"
            value = json.loads(path.read_text())
            value["principal"] = "foreign-run-owner"
            _write_json(path, value)
        aggregate_path = tmp_path / "operations.json"
        aggregate = json.loads(aggregate_path.read_text())
        aggregate["items"][0]["principal"] = "foreign-run-owner"
        _write_json(aggregate_path, aggregate)
    else:
        response_path = session / "request-000005.response.bin"
        changed = b"internally consistent but wrong artifact"
        response_path.write_bytes(changed)
        terminal_path = session / "request-000005.terminal.json"
        terminal = json.loads(terminal_path.read_text())
        terminal.update(
            {
                "response_sha256": hashlib.sha256(changed).hexdigest(),
                "response_bytes": len(changed),
            }
        )
        _write_json(terminal_path, terminal)

    result = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=provisional,
    )

    assert result["status"] == "EVIDENCE_INCOMPLETE"
    if fault == "missing_start":
        assert "request_predecessor_missing" in result["failure_codes"]
    elif fault == "missing_terminal":
        assert "request_terminal_cardinality_invalid" in result["failure_codes"]
    elif fault == "orphan_response":
        assert "request_predecessor_missing" in result["failure_codes"]
    elif fault == "wrong_sequence":
        assert "request_sequence_invalid" in result["failure_codes"]
    elif fault == "duplicate_operation":
        assert "operation_identity_or_duplicate_invalid" in result["failure_codes"]
    elif fault == "corrupt_terminal_hash":
        assert "request_terminal_evidence_invalid" in result["failure_codes"]
    elif fault == "wrong_accounting":
        assert "attempt_evidence_incomplete" in result["failure_codes"]
    elif fault == "mixed_identity":
        assert "operation_principal_identity_changed" in result["failure_codes"]
    else:
        assert "owner_artifact_raw_observation_invalid" in result["failure_codes"]


def test_reminder_effect_disagreements_are_stopping_failures() -> None:
    row = {
        "entity_id": "entity-one",
        "user_id": "owner",
        "name": "APP-SOAK-REMINDER-ONE",
        "entity_type": "event",
        "time_user_id": "owner",
        "occurred_at": "2035-01-02",
        "occurred_end": None,
        "precision": "day",
        "source": "reminder:owner",
        "person_id": "owner",
        "privacy_kind": "reminder",
    }
    arguments = {
        "owner_user_id": "owner",
        "reminder_name": "APP-SOAK-REMINDER-ONE",
        "occurred_at": "2035-01-02",
    }
    assert _reminder_creation_failure(tools_used=["remind"], rows=[row], **arguments) == ""
    assert (
        _reminder_creation_failure(tools_used=[], rows=[row], **arguments)
        == "reminder_unattributed_effect_observed"
    )
    assert (
        _reminder_creation_failure(tools_used=["remind"], rows=[], **arguments)
        == "reminder_tool_effect_without_durable_row"
    )
    assert (
        _reminder_creation_failure(tools_used=["remind"], rows=[row, row], **arguments)
        == "reminder_duplicate_effect_observed"
    )
    assert (
        _reminder_creation_failure(
            tools_used=["remind"],
            rows=[{**row, "occurred_at": "2035-01-03"}],
            **arguments,
        )
        == "reminder_effect_identity_invalid"
    )
    assert (
        _reminder_creation_failure(tools_used=["remind"], rows=[1], **arguments)
        == "reminder_effect_identity_invalid"
    )


def test_independent_reader_requires_unsafe_creation_failure_to_stop(tmp_path: Path) -> None:
    policy, expected, provisional = _truthful_red_evidence(tmp_path)
    failure = "reminder_tool_effect_without_durable_row"
    response = json.dumps(
        {"message": "claimed", "tools_used": ["remind"]},
        sort_keys=True,
    ).encode()
    session = tmp_path / f"session-{policy.owner.user_id}"
    response_path = session / "request-000006.response.bin"
    response_path.write_bytes(response)
    terminal_path = session / "request-000006.terminal.json"
    terminal = json.loads(terminal_path.read_text())
    terminal.update(
        {
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "response_bytes": len(response),
        }
    )
    _write_json(terminal_path, terminal)
    effect_path = tmp_path / "effect-reminder-creation.json"
    effect = json.loads(effect_path.read_text())
    effect.update(
        {
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "tools_used": ["remind"],
        }
    )
    _write_json(effect_path, effect)

    operations_path = tmp_path / "operations.json"
    operations = json.loads(operations_path.read_text())
    reminder = operations["items"][7]
    reminder["reason_code"] = failure
    reminder["details"]["actual"]["tools_used"] = ["remind"]
    for item in operations["items"][8:]:
        item["dependency_reason_code"] = failure
    _write_json(operations_path, operations)
    _write_json(tmp_path / "operation-0008.error.json", reminder)
    for sequence, item in enumerate(operations["items"][8:], 9):
        _write_json(tmp_path / f"operation-{sequence:04d}.not-run.json", item)
    core_path = tmp_path / "core-observation.json"
    core = json.loads(core_path.read_text())
    core["publication"]["creation_failure_code"] = failure
    _write_json(core_path, core)

    provisional["failure_codes"] = [failure]
    provisional["duration_coverage"] = {
        "status": "NOT_RUN_SAFETY_STOP",
        "identity_get_rounds": 0,
        "requests_per_round": 4,
        "reminder_branch": "MISSING_AFTER_CREATION_FAIL",
        "full_planned_mixed_soak": False,
    }
    result = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=provisional,
    )
    assert result["status"] == "EVIDENCE_COMPLETE"
    assert result["observed_profile_status"] == "FAIL"

    provisional["duration_coverage"]["status"] = "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL"
    result = independent_evidence_check(
        tmp_path,
        policy=policy,
        expected=expected,
        provisional=provisional,
    )
    assert result["status"] == "EVIDENCE_INCOMPLETE"
    assert "unsafe_failure_did_not_stop" in result["failure_codes"]
