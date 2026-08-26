from __future__ import annotations

from typing import Any

import pytest

from friday.execution_kernel import ExecutionKernel, ToolSpec
from friday.host_control.jobs import HostJobStore
from friday.permissions import AuthorizationService

_JOB_ID = "hjob_" + "a" * 32


@pytest.mark.parametrize(
    ("envelope", "expected_approval_status", "expected_success"),
    [
        (
            {
                "effect_boundary_crossed": True,
                "error_code": "package_install_unknown",
                "job_id": _JOB_ID,
                "ok": False,
                "status": "unknown",
            },
            "uncertain",
            False,
        ),
        (
            {
                "effect_boundary_crossed": False,
                "error_code": "package_failed_before_effect",
                "job_id": _JOB_ID,
                "ok": False,
                "status": "failed",
            },
            "failed",
            False,
        ),
        (
            {
                "effect_boundary_crossed": False,
                "job_id": _JOB_ID,
                "ok": True,
                "status": "completed",
            },
            "done",
            True,
        ),
    ],
)
@pytest.mark.parametrize("tool_name", ["host_action_execute", "software_install_execute"])
async def test_execute_approved_settles_host_control_envelopes_by_effect_boundary(
    settings,
    storage,
    envelope: dict[str, Any],
    expected_approval_status: str,
    expected_success: bool,
    tool_name: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user("alice", source="test")
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())

    async def handler(*, actor: object, job_id: str) -> dict[str, Any]:
        del actor, job_id
        return dict(envelope)

    kernel.register(
        ToolSpec(
            name=tool_name,
            description="Synthetic hidden host approval executor.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            security_id="knowledge.delete",
            risk="high",
            handler=handler,
            model_visible=False,
            approval_predicate=lambda _arguments: True,
        )
    )
    approval = storage.create_action_approval(
        actor.user_id,
        tool=tool_name,
        payload={"job_id": _JOB_ID},
        summary="Install the exact approved package plan.",
        risk="high",
        requested_by=actor.own_id,
    )
    assert storage.decide_action_approval(
        approval["id"],
        actor.user_id,
        decision="approve",
        decided_by=actor.own_id,
    )

    result = await kernel.execute_approved(approval["id"], actor=actor)

    assert result.success is expected_success
    assert storage.get_action_approval(approval["id"], actor.user_id)["status"] == (expected_approval_status)
    if expected_success:
        assert result.data == envelope
    else:
        assert result.data == {
            "effect_boundary_crossed": envelope["effect_boundary_crossed"],
            "error_code": envelope["error_code"],
            "job_id": _JOB_ID,
            "status": envelope["status"],
        }


async def test_exact_claimed_host_approval_resumes_only_before_request_send(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user("alice", source="test")
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())
    calls = 0

    async def handler(
        *,
        actor: object,
        job_id: str,
        plan: dict[str, Any],
        plan_digest: str,
    ) -> dict[str, Any]:
        nonlocal calls
        del actor, plan, plan_digest
        calls += 1
        return {
            "effect_boundary_crossed": False,
            "job_id": job_id,
            "ok": True,
            "status": "completed",
        }

    kernel.register(
        ToolSpec(
            name="host_action_execute",
            description="Synthetic hidden host approval executor.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "plan": {"type": "object"},
                    "plan_digest": {"type": "string"},
                },
                "required": ["job_id", "plan", "plan_digest"],
                "additionalProperties": False,
            },
            security_id="knowledge.delete",
            risk="high",
            handler=handler,
            model_visible=False,
            approval_predicate=lambda _arguments: True,
        )
    )
    digest = "a" * 64
    plan = {"schema_version": 1, "exact": "pre-send"}
    jobs = HostJobStore(storage)
    job, created = jobs.create_or_get(
        user_id=actor.user_id,
        actor_own_id=actor.own_id,
        conversation_id=None,
        source_message_id=None,
        host_agent_id="local-user-agent",
        capability_id="network.nmap.scan",
        adapter_id="network.nmap",
        adapter_version=1,
        action_id="discover",
        normalized_arguments={"targets": ["192.168.1.7"]},
        plan=plan,
        plan_digest=digest,
        risk_class="network_observe",
        authorization_basis="explicit_current_user_request",
        idempotency_key="claimed-resume",
        awaiting_approval=True,
        job_id=_JOB_ID,
    )
    assert created is True
    payload = {"job_id": job["id"], "plan": plan, "plan_digest": digest}
    approval = storage.create_action_approval(
        actor.user_id,
        tool="host_action_execute",
        payload=payload,
        summary="Execute the exact host plan.",
        risk="high",
        requested_by=actor.own_id,
    )
    jobs.bind_approval(
        job["id"],
        approval["id"],
        user_id=actor.user_id,
        actor_own_id=actor.own_id,
    )
    assert storage.decide_action_approval(
        approval["id"],
        actor.user_id,
        decision="approve",
        decided_by=actor.own_id,
    )
    claimed = storage.claim_action_approval(
        approval["id"],
        actor.user_id,
        payload=payload,
    )
    assert claimed is not None and claimed["status"] == "claimed"

    first = await kernel.execute_approved(approval["id"], actor=actor)
    second = await kernel.execute_approved(approval["id"], actor=actor)

    assert first.success is True
    assert second.success is False
    assert calls == 1
    assert storage.get_action_approval(approval["id"], actor.user_id)["status"] == "done"
