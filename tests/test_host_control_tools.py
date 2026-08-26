from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from friday.host_control import tools as host_tools
from friday.host_control.contracts import ContractError
from friday.host_control.service import HostActionUnknown

_JOB_ID = "hjob_" + "a" * 32
_PLAN_DIGEST = "b" * 64
_PACKAGE_PLAN = {"schema_version": 1, "transaction": {"requested": [{"name": "nmap"}]}}


def _build_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    package_install_enabled: bool,
    available: bool = True,
    result: object | None = None,
    failure: BaseException | None = None,
) -> tuple[tuple[object, ...], SimpleNamespace]:
    class Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def available(self, *, timeout_sec: float) -> bool:
            assert timeout_sec == 0.5
            return available

    execute = AsyncMock(side_effect=failure, return_value=result)
    service = SimpleNamespace(
        execute_approved_action=AsyncMock(return_value=result),
        execute_approved_install=execute,
        prepare_network_action=AsyncMock(),
        prepare_file_action=AsyncMock(return_value={"status": "missing_package"}),
        request_action_approval=Mock(),
        run_prepared=AsyncMock(),
    )
    monkeypatch.setattr(host_tools, "HostControlClient", Client)
    monkeypatch.setattr(host_tools, "HostControlService", lambda _ctx, _client: service)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            host_action_default_timeout_sec=60.0,
            host_agent_id="host-agent:test",
            host_agent_key_file=Path("/run/friday-host-control/agent.key"),
            host_agent_socket=Path("/run/friday-host-control/agent.sock"),
            host_control_enabled=True,
            host_package_install_enabled=package_install_enabled,
        )
    )
    return host_tools.build_host_control_tools(ctx), service


def _executor(tools: tuple[object, ...]):  # noqa: ANN202
    matches = [tool for tool in tools if getattr(tool, "name", None) == "software_install_execute"]
    assert len(matches) == 1
    tool = matches[0]
    assert tool.handler is not None
    return tool


def _json_extract(tools: tuple[object, ...]):  # noqa: ANN202
    matches = [tool for tool in tools if getattr(tool, "name", None) == "host_json_extract"]
    assert len(matches) == 1
    tool = matches[0]
    assert tool.handler is not None
    return tool


def _tool(tools: tuple[object, ...], name: str):  # noqa: ANN202
    matches = [tool for tool in tools if getattr(tool, "name", None) == name]
    assert len(matches) == 1
    tool = matches[0]
    assert tool.handler is not None
    return tool


def test_install_executor_registration_is_hidden_conditional_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled, _service = _build_tools(monkeypatch, package_install_enabled=False)
    disabled_names = {tool.name for tool in disabled}
    assert "software_install_execute" not in disabled_names
    assert "software_search" not in disabled_names
    assert "software_install" not in disabled_names

    enabled, _service = _build_tools(monkeypatch, package_install_enabled=True)
    executor = _executor(enabled)
    assert executor.model_visible is False
    assert executor.risk == "high"
    assert executor.security_id == "host.packages.install"
    assert executor.timeout_sec > 3_600
    assert executor.approval_predicate is not None
    assert executor.approval_predicate({}) is True
    assert executor.parameters == {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "pattern": "^hjob_[0-9a-f]{32}$"},
            "package_plan": {"type": "object"},
            "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["job_id", "package_plan", "plan_digest"],
        "additionalProperties": False,
    }


async def test_jq_file_action_is_model_visible_but_accepts_no_program_or_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, service = _build_tools(monkeypatch, package_install_enabled=False)
    jq = _json_extract(tools)
    assert jq.model_visible is True
    assert jq.risk == "observe"
    assert jq.security_id == "host.files.read"
    assert set(jq.parameters["properties"]) == {"compact", "fields"}
    assert jq.parameters["required"] == ["fields"]
    assert "program" not in str(jq.parameters).casefold()
    assert "path" not in str(jq.parameters).casefold()

    actor = object()
    response = await jq.handler(
        actor=actor,
        fields=["name", "nested.value"],
        compact=True,
        _conversation_id="conversation:test",
        _raw_id="raw_0123456789abcdef",
        _source_message_id="message:test",
    )

    assert response == {
        "effect_boundary_crossed": False,
        "error_code": "host_capability_unavailable",
        "job_id": "",
        "ok": False,
        "status": "missing_package",
    }
    service.prepare_file_action.assert_awaited_once_with(
        actor=actor,
        capability_id="data.jq.extract",
        action_id="extract_fields",
        raw_id="raw_0123456789abcdef",
        fields=["name", "nested.value"],
        compact=True,
        conversation_id="conversation:test",
        source_message_id="message:test",
    )


async def test_existing_failed_package_preparation_is_not_an_action_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, service = _build_tools(monkeypatch, package_install_enabled=True)
    service.prepare_network_action.return_value = {
        "job_id": _JOB_ID,
        "package_plan_id": "package-plan:test",
        "status": "failed",
    }

    response = await _tool(tools, "host_action_run").handler(
        actor=object(),
        capability_id="network.nmap.scan",
        action_id="discover",
        targets=["192.0.2.1"],
        ports=None,
        _conversation_id="conversation:test",
        _source_message_id="message:test",
    )

    assert response == {
        "effect_boundary_crossed": False,
        "error_code": "host_action_failed",
        "job_id": _JOB_ID,
        "ok": False,
        "package_plan_id": "package-plan:test",
        "status": "failed",
    }


@pytest.mark.parametrize("tool_name", ["host_action_run", "host_json_extract"])
@pytest.mark.parametrize(
    ("result", "expected_ok", "expected_crossed", "expected_code"),
    [
        (
            {
                "error_code": "process_failed",
                "job_id": _JOB_ID,
                "status": "failed",
                "terminal_outcome": "failed",
            },
            False,
            False,
            "process_failed",
        ),
        (
            {
                "job_id": _JOB_ID,
                "status": "cancelled",
                "terminal_outcome": "cancelled",
            },
            False,
            False,
            "host_action_cancelled",
        ),
        (
            {
                "job_id": _JOB_ID,
                "status": "unknown",
                "terminal_outcome": "unknown",
            },
            False,
            True,
            "host_action_unknown",
        ),
        (
            {"job_id": _JOB_ID, "status": "reconciled"},
            False,
            True,
            "host_action_unknown",
        ),
    ],
)
async def test_host_action_handlers_do_not_turn_terminal_failures_into_success(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    result: dict[str, object],
    expected_ok: bool,
    expected_crossed: bool,
    expected_code: str,
) -> None:
    tools, service = _build_tools(monkeypatch, package_install_enabled=False)
    prepared = SimpleNamespace(job={"id": _JOB_ID, "status": "planned"})
    service.run_prepared.return_value = result
    actor = object()
    if tool_name == "host_action_run":
        service.prepare_network_action.return_value = prepared
        response = await _tool(tools, tool_name).handler(
            actor=actor,
            capability_id="network.nmap.scan",
            action_id="discover",
            targets=["192.0.2.1"],
            ports=None,
            _conversation_id="conversation:test",
            _source_message_id="message:test",
        )
    else:
        service.prepare_file_action.return_value = prepared
        response = await _tool(tools, tool_name).handler(
            actor=actor,
            fields=["name"],
            compact=True,
            _conversation_id="conversation:test",
            _raw_id="raw_0123456789abcdef",
            _source_message_id="message:test",
        )

    assert response["ok"] is expected_ok
    assert response["effect_boundary_crossed"] is expected_crossed
    assert response["error_code"] == expected_code
    if expected_crossed:
        assert response["status"] == "unknown"
    service.run_prepared.assert_awaited_once_with(prepared, actor=actor)


@pytest.mark.parametrize(
    ("status", "terminal_outcome"),
    [("completed", "completed"), ("partial", "partial"), ("reconciled", "completed")],
)
async def test_host_action_run_accepts_only_proven_success_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    terminal_outcome: str,
) -> None:
    tools, service = _build_tools(monkeypatch, package_install_enabled=False)
    prepared = SimpleNamespace(job={"id": _JOB_ID, "status": "planned"})
    service.prepare_network_action.return_value = prepared
    service.run_prepared.return_value = {
        "job_id": _JOB_ID,
        "status": status,
        "terminal_outcome": terminal_outcome,
    }

    response = await _tool(tools, "host_action_run").handler(
        actor=object(),
        capability_id="network.nmap.scan",
        action_id="discover",
        targets=["192.0.2.1"],
        ports=None,
        _conversation_id="conversation:test",
        _source_message_id="message:test",
    )

    assert response["ok"] is True
    assert response["status"] == status
    assert response["terminal_outcome"] == terminal_outcome


@pytest.mark.parametrize(
    "result",
    [
        {"job_id": _JOB_ID, "status": "failed", "terminal_outcome": "completed"},
        {"job_id": "hjob_" + "f" * 32, "status": "completed"},
        {"job_id": _JOB_ID, "status": "running"},
    ],
)
async def test_approved_host_action_executor_fails_closed_on_invalid_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
) -> None:
    tools, service = _build_tools(monkeypatch, package_install_enabled=False)
    service.execute_approved_action.return_value = result

    response = await _tool(tools, "host_action_execute").handler(
        actor=object(),
        job_id=_JOB_ID,
        plan={"schema_version": 1},
        plan_digest=_PLAN_DIGEST,
    )

    assert response == {
        "effect_boundary_crossed": True,
        "error_code": "host_action_unknown",
        "job_id": _JOB_ID,
        "ok": False,
        "status": "unknown",
    }


def test_install_executor_is_absent_when_agent_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        available=False,
    )
    assert tools == ()


async def test_install_executor_forwards_the_exact_approved_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable_result = {
        "job_id": _JOB_ID,
        "package_outcome": "completed",
        "receipt_digest": "c" * 64,
        "status": "completed",
    }
    tools, service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        result=durable_result,
    )
    executor = _executor(tools)
    actor = object()

    response = await executor.handler(
        actor=actor,
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    service.execute_approved_install.assert_awaited_once_with(
        actor=actor,
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )
    assert response == {
        **durable_result,
        "effect_boundary_crossed": True,
        "ok": True,
    }


async def test_already_satisfied_install_does_not_invent_a_package_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable_result = {
        "job_id": _JOB_ID,
        "package_outcome": "already_satisfied",
        "receipt_digest": "c" * 64,
        "status": "partial",
    }
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        result=durable_result,
    )
    executor = _executor(tools)

    response = await executor.handler(
        actor=object(),
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    assert response == {
        **durable_result,
        "effect_boundary_crossed": False,
        "ok": True,
    }


@pytest.mark.parametrize(
    ("result", "error_code"),
    [
        (
            {
                "error_code": "apt_lock_unavailable",
                "job_id": _JOB_ID,
                "package_outcome": "failed_before_effect",
                "status": "failed",
            },
            "apt_lock_unavailable",
        ),
        (
            {
                "job_id": _JOB_ID,
                "package_outcome": "cancelled_before_commit",
                "status": "cancelled",
            },
            "cancelled_before_commit",
        ),
    ],
)
async def test_install_executor_preserves_proven_no_effect_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
    error_code: str,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        result=result,
    )
    executor = _executor(tools)

    response = await executor.handler(
        actor=object(),
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    assert response["ok"] is False
    assert response["effect_boundary_crossed"] is False
    assert response["error_code"] == error_code
    assert response["status"] == result["status"]


@pytest.mark.parametrize(
    "failure",
    [HostActionUnknown("lost broker result"), RuntimeError("unclassified service failure")],
)
async def test_install_executor_marks_uncertain_failures_unknown(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        failure=failure,
    )
    executor = _executor(tools)

    response = await executor.handler(
        actor=object(),
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    assert response == {
        "effect_boundary_crossed": True,
        "error_code": "package_install_unknown",
        "job_id": _JOB_ID,
        "ok": False,
        "status": "unknown",
    }


async def test_install_executor_rejects_a_mismatched_service_result_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        result={
            "job_id": "hjob_" + "f" * 32,
            "package_outcome": "completed",
            "status": "completed",
        },
    )
    executor = _executor(tools)

    response = await executor.handler(
        actor=object(),
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    assert response["ok"] is False
    assert response["effect_boundary_crossed"] is True
    assert response["error_code"] == "package_install_unknown"
    assert response["job_id"] == _JOB_ID


async def test_install_executor_keeps_contract_refusals_before_the_effect_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        failure=ContractError("plan drifted"),
    )
    executor = _executor(tools)

    response = await executor.handler(
        actor=object(),
        job_id=_JOB_ID,
        package_plan=_PACKAGE_PLAN,
        plan_digest=_PLAN_DIGEST,
    )

    assert response == {
        "effect_boundary_crossed": False,
        "error_code": "invalid_host_action",
        "job_id": _JOB_ID,
        "ok": False,
        "status": "failed",
    }


async def test_install_executor_does_not_convert_task_cancellation_to_a_known_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, _service = _build_tools(
        monkeypatch,
        package_install_enabled=True,
        failure=asyncio.CancelledError(),
    )
    executor = _executor(tools)

    with pytest.raises(asyncio.CancelledError):
        await executor.handler(
            actor=object(),
            job_id=_JOB_ID,
            package_plan=_PACKAGE_PLAN,
            plan_digest=_PLAN_DIGEST,
        )
