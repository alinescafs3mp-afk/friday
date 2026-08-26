from __future__ import annotations

import json

from fastapi.testclient import TestClient

from friday.execution_kernel import ToolResult
from friday.host_control.jobs import HostJobStore
from friday.server import create_app


def test_approved_host_result_is_returned_and_durably_linked_to_the_original_message(
    settings,
    monkeypatch,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        actor = app.state.auth_service.actor_for_user(
            client.get("/api/admin/users", headers=headers).json()["items"][0]["id"],
            source="test",
        )
        conversation = app.state.storage.create_conversation(actor.own_id, "host approval")
        source = app.state.storage.store_message(
            conversation["id"],
            actor.own_id,
            "user",
            "Install nmap if necessary and scan the approved local target.",
        )
        jobs = HostJobStore(app.state.storage)
        job, created = jobs.create_or_get(
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation["id"],
            source_message_id=source["id"],
            host_agent_id="local-user-agent",
            capability_id="network.nmap.scan",
            adapter_id="network.nmap",
            adapter_version=1,
            action_id="services",
            normalized_arguments={"target": "192.168.1.7"},
            plan={"synthetic": "exact-plan"},
            plan_digest="1" * 64,
            risk_class="network_observe",
            authorization_basis="host.network.observe",
            idempotency_key="approval-final-response",
            awaiting_approval=True,
        )
        assert created is True
        approval = app.state.storage.create_action_approval(
            actor.user_id,
            tool="host_action_execute",
            payload={"job_id": job["id"], "plan": {}, "plan_digest": "1" * 64},
            summary="Run exact local nmap plan",
            requested_by=actor.own_id,
            conversation_id=conversation["id"],
        )
        jobs.bind_approval(job["id"], approval["id"], user_id=actor.user_id, actor_own_id=actor.own_id)
        jobs.transition(
            job["id"],
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="awaiting_approval",
            status="approved",
            stage="approval",
            outcome_code="synthetic_approved",
        )
        jobs.transition(
            job["id"],
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="approved",
            status="admitted",
            stage="agent_admission",
            outcome_code="synthetic_admitted",
        )
        jobs.transition(
            job["id"],
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="admitted",
            status="running",
            stage="host_process",
            outcome_code="synthetic_running",
        )
        jobs.transition(
            job["id"],
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            expected_status="running",
            status="completed",
            stage="receipt",
            outcome_code="synthetic_completed",
            result_ref="evidence/result.json",
            receipt_ref="evidence/receipt.json",
        )
        result_data = {
            "coverage": {
                "accounted": 1,
                "grade": "complete",
                "reasons": [],
                "requested": 1,
                "skipped": 0,
            },
            "evidence": [
                {
                    "evidence_id": "nmap_xml",
                    "media_type": "application/xml",
                    "sha256": "2" * 64,
                    "size_bytes": 321,
                }
            ],
            "job_id": job["id"],
            "receipt_digest": "3" * 64,
            "result": {
                "hosts": [
                    {
                        "addresses": [{"address": "192.168.1.7", "type": "ipv4"}],
                        "hostnames": [],
                        "ports": [
                            {
                                "port": 443,
                                "protocol": "tcp",
                                "service": {"name": "https"},
                                "state": "open",
                            }
                        ],
                        "state": "up",
                    }
                ],
                "hosts_up": 1,
                "open_ports": 1,
            },
            "status": "completed",
            "warnings": [],
        }

        async def execute_approved(approval_id: str, *, actor=None) -> ToolResult:
            del approval_id, actor
            return ToolResult("host_action_execute", True, data=result_data)

        monkeypatch.setattr(app.state.kernel, "execute_approved", execute_approved)
        response = client.post(
            f"/api/approvals/{approval['id']}/decide",
            json={"decision": "approve"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["executed"] is True
        assert body["result"] == result_data
        assert body["final_response_persisted"] is True
        assert "192.168.1.7" in body["final_response"]
        assert "443/tcp https" in body["final_response"]
        assert "Покрытие: complete, учтено 1/1" in body["final_response"]
        assert "nmap_xml sha256:" in body["final_response"]

        rows = app.state.storage.get_conversation_messages(
            conversation["id"],
            user_id=actor.own_id,
        )
        assistant = next(row for row in rows if row["id"] == body["final_response_message_id"])
        metadata = json.loads(assistant["metadata_json"])
        assert assistant["reply_to"] == source["id"]
        assert assistant["content"] == body["final_response"]
        assert metadata["host_control_approval_id"] == approval["id"]
        assert metadata["host_control_job_ids"] == [job["id"]]
        assert metadata["tools_used"] == ["host_action_execute"]


def test_non_host_approval_does_not_gain_a_host_result_surface(settings, monkeypatch) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        owner = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        approval = app.state.storage.create_action_approval(
            owner,
            tool="entity_merge_decide",
            payload={"candidate_id": "res_missing", "decision": "accept"},
            summary="Synthetic non-host approval",
            requested_by=owner,
        )

        async def execute_approved(approval_id: str, *, actor=None) -> ToolResult:
            del approval_id, actor
            return ToolResult("entity_merge_decide", True, data={"private": "generic"})

        monkeypatch.setattr(app.state.kernel, "execute_approved", execute_approved)
        response = client.post(
            f"/api/approvals/{approval['id']}/decide",
            json={"decision": "approve"},
            headers=headers,
        )

        assert response.status_code == 200
        assert set(response.json()) == {"approval", "error", "executed"}


def test_rejected_host_approval_closes_the_exact_job_without_execution(settings, monkeypatch) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        owner = client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]
        actor = app.state.auth_service.actor_for_user(owner, source="test")
        conversation = app.state.storage.create_conversation(actor.own_id, "reject host approval")
        source = app.state.storage.store_message(
            conversation["id"],
            actor.own_id,
            "user",
            "Do not run this reviewed host action.",
        )
        jobs = HostJobStore(app.state.storage)
        job, created = jobs.create_or_get(
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
            conversation_id=conversation["id"],
            source_message_id=source["id"],
            host_agent_id="local-user-agent",
            capability_id="network.nmap.scan",
            adapter_id="network.nmap",
            adapter_version=1,
            action_id="discover",
            normalized_arguments={"target": "192.168.1.7"},
            plan={"synthetic": "rejected-plan"},
            plan_digest="4" * 64,
            risk_class="network_observe",
            authorization_basis="host.network.scan",
            idempotency_key="approval-rejection-final-state",
            awaiting_approval=True,
        )
        assert created is True
        approval = app.state.storage.create_action_approval(
            actor.user_id,
            tool="host_action_execute",
            payload={"job_id": job["id"], "plan": {}, "plan_digest": "4" * 64},
            summary="Reject exact local plan",
            requested_by=actor.own_id,
            conversation_id=conversation["id"],
        )
        jobs.bind_approval(
            job["id"],
            approval["id"],
            user_id=actor.user_id,
            actor_own_id=actor.own_id,
        )

        async def forbidden_execute(*_args, **_kwargs):
            raise AssertionError("a rejected host approval reached execution")

        monkeypatch.setattr(app.state.kernel, "execute_approved", forbidden_execute)
        response = client.post(
            f"/api/approvals/{approval['id']}/decide",
            json={"decision": "reject"},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        assert response.json()["executed"] is False
        assert response.json()["host_job_closed"] is True
        assert response.json()["host_job_id"] == job["id"]
        closed = jobs.get(job["id"], user_id=actor.user_id, actor_own_id=actor.own_id)
        assert closed is not None
        assert closed["status"] == "cancelled"
        assert closed["stage"] == "approval"
        assert closed["error_code"] == "approval_rejected"
        assert (
            jobs.events(job["id"], user_id=actor.user_id, actor_own_id=actor.own_id)[-1]["outcome_code"]
            == "approval_rejected"
        )
