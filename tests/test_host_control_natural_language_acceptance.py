from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.host_control import tools as host_control_tools
from friday.host_control.jobs import HostJobStore
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.organs.host_control import HostControlOrgan
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer
from friday_package_broker.contracts import AptInstallPlan
from tests.test_friday_host_agent_execution import _AGENT_ID, _vertical_stack

_REQUEST = "Запусти сетевой аудит nmap для разрешённой локальной цели 192.168.1.7, профиль discover."


class _LiteralNmapModel:
    enabled = True
    model = "synthetic-literal-nmap"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.action_calls = 0
        self.offered_names: list[set[str]] = []
        self.seen_literal_requests = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        offered = {
            str((item.get("function") or {}).get("name") or "")
            for item in (tools or [])
            if isinstance(item, dict)
        }
        self.offered_names.append(offered)
        if not tools:
            return {"content": "Маршрут без инструментов завершён.", "_queue_wait_sec": 0.0}
        last_user_index = max(
            (index for index, item in enumerate(messages) if item.get("role") == "user"),
            default=-1,
        )
        if any(item.get("role") == "tool" for item in messages[last_user_index + 1 :]):
            return {
                "content": "Запрос принят; результат или заявка сохранены.",
                "_queue_wait_sec": 0.0,
            }
        literal_present = any(
            item.get("role") == "user" and _REQUEST in str(item.get("content") or "") for item in messages
        )
        assert literal_present, "model tool selection was not grounded in the literal user request"
        assert "host_action_run" in offered
        self.seen_literal_requests += 1
        self.action_calls += 1
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"call-nmap-{self.action_calls}",
                    "function": {
                        "name": "host_action_run",
                        "arguments": json.dumps(
                            {
                                "action_id": "discover",
                                "capability_id": "network.nmap.scan",
                                "targets": ["192.168.1.7"],
                            }
                        ),
                    },
                }
            ],
        }


def _approval_after(storage: Any, actor: Any, *, excluded: set[str]) -> dict[str, Any]:
    rows = storage.list_action_approvals(
        actor.user_id,
        person_id=actor.own_id,
        limit=20,
    )
    candidates = [row for row in rows if str(row["id"]) not in excluded]
    assert len(candidates) == 1
    return candidates[0]


@pytest.mark.asyncio
async def test_literal_russian_nmap_request_reject_then_approve_resumes_exact_vertical(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unused_service, client, broker, runner = _vertical_stack(storage, tmp_path)
    monkeypatch.setattr(client, "available", lambda *, timeout_sec: timeout_sec == 0.5, raising=False)
    monkeypatch.setattr(
        host_control_tools,
        "HostControlClient",
        lambda *_args, **_kwargs: client,
    )

    configured = replace(
        settings,
        host_control_enabled=True,
        host_package_install_enabled=True,
        host_agent_id=_AGENT_ID,
        host_allowed_cidrs=("192.168.1.0/24",),
        host_public_network_enabled=False,
        host_job_root=tmp_path / "jobs",
        host_approval_signing_key_file=tmp_path / "backend-approval-signing.key",
        host_action_default_timeout_sec=300,
        host_action_max_concurrency=2,
        host_action_max_output_bytes=8 * 1024 * 1024,
        verify_answers=False,
    )
    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    for capability in HostControlOrgan().capabilities():
        authorization.register_capability(capability)
    actor = authorization.actor_for_user("alice", source="test")

    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, configured)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(configured),
        IngestionPipeline(configured, storage, graph),
    )
    context = SimpleNamespace(auth=authorization, settings=configured, storage=storage)
    for spec in host_control_tools.build_host_control_tools(context):
        kernel.register(spec)
    kernel.assert_risk_declarations_agree()

    model = _LiteralNmapModel()
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)

    async def no_prefetch(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "_prefetch_the_web_if_asked", no_prefetch)
    conversation = storage.create_conversation(actor.user_id, "literal nmap acceptance")

    first_turn = await runtime.chat(
        actor.user_id,
        _REQUEST,
        actor=actor,
        conversation_id=conversation["id"],
        enable_tools=True,
    )

    assert first_turn["tools_used"] == ["host_action_run"], (first_turn, model.offered_names)
    first_approval = _approval_after(storage, actor, excluded=set())
    assert first_approval["status"] == "pending"
    assert first_approval["tool"] == "software_install_execute"
    assert first_approval["requested_by"] == actor.own_id
    assert set(first_approval["payload"]) == {"job_id", "package_plan", "plan_digest"}
    first_plan = AptInstallPlan.from_payload(first_approval["payload"]["package_plan"])
    assert first_plan.digest == first_approval["payload"]["plan_digest"]
    assert [item.name for item in first_plan.transaction.requested] == ["nmap"]
    assert "nmap=7.94:amd64" in first_approval["summary"]
    assert "ADD nmap:amd64 ∅ -> 7.94" in first_approval["summary"]
    assert "archive_sha256=" + "c" * 64 in first_approval["summary"]
    assert "site=archive.ubuntu.com" in first_approval["summary"]
    assert f"Original task to resume: {_REQUEST}" in first_approval["summary"]
    assert f"Exact plan sha256: {first_plan.digest}" in first_approval["summary"]
    assert first_plan.original_task_ref in {
        row["id"]
        for row in storage.get_conversation_messages(conversation["id"], user_id=actor.user_id)
        if row["role"] == "user" and row["content"] == _REQUEST
    }
    assert [method for method, _body, _metadata in client.calls] == ["PackagePlanInstall"]
    assert broker.executions == []
    assert runner.calls == 0

    rejected = storage.decide_action_approval(
        first_approval["id"],
        actor.user_id,
        decision="reject",
        decided_by=actor.own_id,
        person_id=actor.own_id,
    )
    assert rejected is not None and rejected["status"] == "rejected"
    rejected_job = HostJobStore(storage).close_rejected_approval(
        first_approval["payload"]["job_id"],
        first_approval["id"],
        user_id=actor.user_id,
        actor_own_id=actor.own_id,
    )
    assert rejected_job["status"] == "cancelled"
    refused_execution = await kernel.execute_approved(first_approval["id"], actor=actor)
    assert refused_execution.success is False
    assert broker.executions == []
    assert runner.calls == 0
    first_job = storage.execute(
        "SELECT status,approval_id,receipt_ref FROM host_action_jobs WHERE id=?",
        (first_approval["payload"]["job_id"],),
    ).fetchone()
    assert tuple(first_job) == ("cancelled", first_approval["id"], None)

    second_turn = await runtime.chat(
        actor.user_id,
        _REQUEST,
        actor=actor,
        conversation_id=conversation["id"],
        enable_tools=True,
    )

    assert second_turn["tools_used"] == ["host_action_run"]
    second_approval = _approval_after(storage, actor, excluded={first_approval["id"]})
    assert second_approval["id"] != first_approval["id"]
    assert second_approval["payload"]["job_id"] != first_approval["payload"]["job_id"]
    second_plan = AptInstallPlan.from_payload(second_approval["payload"]["package_plan"])
    assert second_plan.digest == second_approval["payload"]["plan_digest"]
    assert second_plan.original_task_ref != first_plan.original_task_ref
    assert [method for method, _body, _metadata in client.calls] == [
        "PackagePlanInstall",
        "PackagePlanInstall",
    ]
    assert broker.executions == []
    assert runner.calls == 0

    approved_record = storage.decide_action_approval(
        second_approval["id"],
        actor.user_id,
        decision="approve",
        decided_by=actor.own_id,
        person_id=actor.own_id,
    )
    assert approved_record is not None and approved_record["status"] == "approved"
    approved = await kernel.execute_approved(second_approval["id"], actor=actor)

    assert approved.success is True, approved.error
    assert approved.tool_name == "software_install_execute"
    assert approved.data["ok"] is True
    assert approved.data["status"] == "completed"
    assert approved.data["capability_activated"] is True
    assert approved.data["package_outcome"] == "completed"
    resumed = approved.data["resumed"]
    assert resumed["status"] == "completed"
    assert resumed["coverage"]["grade"] == "complete"
    assert resumed["coverage"]["requested"] == 1
    assert resumed["coverage"]["accounted"] == 1
    assert resumed["result"]["hosts"][0]["addresses"] == [{"address": "192.168.1.7", "type": "ipv4"}]
    assert resumed["evidence"]
    assert all(len(item["sha256"]) == 64 for item in resumed["evidence"])
    assert runner.calls == 1
    assert len(broker.executions) == 1
    assert broker.executions[0]["approval_receipt_id"] == second_approval["id"]
    assert [method for method, _body, _metadata in client.calls][-2:] == [
        "PackageExecuteInstall",
        "RunAction",
    ]

    package_row = storage.execute(
        """SELECT status,stage,source_message_id,approval_id,receipt_ref
           FROM host_action_jobs WHERE id=?""",
        (second_approval["payload"]["job_id"],),
    ).fetchone()
    action_row = storage.execute(
        """SELECT status,stage,source_message_id,result_ref,receipt_ref
           FROM host_action_jobs WHERE id=?""",
        (resumed["job_id"],),
    ).fetchone()
    assert tuple(package_row) == (
        "completed",
        "post_install_attestation",
        second_plan.original_task_ref,
        second_approval["id"],
        "broker:apttxn_0123456789abcdef0123456789abcdef",
    )
    assert action_row["status"] == "completed"
    assert action_row["stage"] == "receipt"
    assert action_row["source_message_id"] == second_plan.original_task_ref
    assert str(action_row["result_ref"]).startswith("evidence/evidence_")
    assert str(action_row["receipt_ref"]).startswith("evidence/evidence_")
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM host_action_events WHERE job_id IN (?, ?)",
            (second_approval["payload"]["job_id"], resumed["job_id"]),
        ).fetchone()[0]
        >= 8
    )

    approval_after = storage.get_action_approval(second_approval["id"], actor.user_id)
    assert approval_after is not None and approval_after["status"] == "done"
    assert approval_after["result"]["resumed"]["job_id"] == resumed["job_id"]
    audits = [
        row for row in storage.list_audit_log(actor.own_id, limit=100) if row["action"] == "tool.invoke"
    ]
    approved_audit = next(
        json.loads(row["after_json"])
        for row in audits
        if row["target_id"] == "software_install_execute"
        and json.loads(row["after_json"])["reason"] == "ok_approved"
    )
    assert approved_audit["approval"] == second_approval["id"]
    assert approved_audit["success"] is True
    host_audits = [json.loads(row["after_json"]) for row in audits if row["target_id"] == "host_action_run"]
    assert [item["reason"] for item in host_audits].count("started") == 2
    assert [item["reason"] for item in host_audits].count("failed") == 2
    assert model.action_calls == 2
    assert model.seen_literal_requests == 2
