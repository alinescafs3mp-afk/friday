"""Request-bound assist ownership is decided before HTTP text ingestion."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import LEGACY_OWNER_USER_ID

_CONVERSATION_ID = "conv_0123456789abcdef"
_FOREIGN_CONVERSATION_ID = "conv_fedcba9876543210"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(label: str) -> SupervisorAssistIngressBindingV1:
    return SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref=f"api-assist:{label}",
        request_fingerprint_sha256=_sha256(f"request:{label}"),
    )


def _pending(
    *,
    person_id: str = LEGACY_OWNER_USER_ID,
    conversation_id: str = _CONVERSATION_ID,
    revision: int = 7,
) -> PendingDurableTurnAdmission:
    return PendingDurableTurnAdmission.owned(
        person_id=person_id,
        conversation_id=conversation_id,
        work_graph_id="graph_0123456789abcdef",
        revision=revision,
    )


def _result() -> dict[str, Any]:
    return {
        "conversation_id": _CONVERSATION_ID,
        "message_id": "msg_0123456789abcdef",
        "message": "assist ingress boundary response",
        "message_format": "plain",
        "tools_used": [],
        "files": [],
        "voice": None,
        "context": {"interaction_mode": "dialogue"},
    }


def test_active_new_turn_ingests_once_and_exact_replay_never_reaches_agent(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    classifier_calls: list[SupervisorAssistIngressBindingV1] = []
    ingest_calls: list[str] = []
    agent_calls: list[dict[str, Any]] = []

    def classify(
        person_id: str,
        _message: str,
        *,
        conversation_id: str,
        ingress_binding: SupervisorAssistIngressBindingV1,
        **_kwargs: Any,
    ) -> SupervisorAssistPendingDecision:
        assert person_id == LEGACY_OWNER_USER_ID
        assert conversation_id == _CONVERSATION_ID
        assert type(ingress_binding) is SupervisorAssistIngressBindingV1
        classifier_calls.append(ingress_binding)
        return SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.NEW_TURN,
            pending=_pending(),
            root_request_binding_sha256=_binding("active-root").canonical_sha256(),
            current=ingress_binding,
        )

    async def ingest_text(_user_id: str, content: str, **_kwargs: Any) -> dict[str, Any]:
        ingest_calls.append(content)
        return {
            "promoted": False,
            "queued_for_review": True,
            "action": "queued",
            "category": "note",
            "reason": "ordinary new turn",
        }

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        return _result()

    payload = {
        "message": "Новая независимая реплика",
        "conversation_id": _CONVERSATION_ID,
        "source_ref": "api-assist:new-turn-once",
    }
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            classify,
            raising=False,
        )
        monkeypatch.setattr(app.state.agent, "chat", chat)
        monkeypatch.setattr(app.state.ingestion, "ingest_text", ingest_text)
        first = client.post("/api/chat", json=payload, headers=headers)
        replay = client.post("/api/chat", json=payload, headers=headers)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert ingest_calls == [payload["message"]]
    assert len(classifier_calls) == 1
    assert len(agent_calls) == 1
    carried_binding = agent_calls[0]["_semantic_supervisor_ingress_binding"]
    carried_decision = agent_calls[0]["_semantic_supervisor_pending_decision"]
    assert type(carried_binding) is SupervisorAssistIngressBindingV1
    assert carried_binding is classifier_calls[0]
    assert type(carried_decision) is SupervisorAssistPendingDecision
    assert carried_decision.relation is SupervisorAssistPendingRelation.NEW_TURN
    assert carried_decision.pending == _pending()
    assert carried_decision.current_request_binding_sha256 == carried_binding.canonical_sha256()
    assert agent_calls[0]["ingestion_result"]["action"] == "queued"


@pytest.mark.parametrize(
    "relation",
    (
        SupervisorAssistPendingRelation.ROOT_REPLAY,
        SupervisorAssistPendingRelation.EXPLICIT_CANCEL,
        SupervisorAssistPendingRelation.UNCERTAIN,
    ),
)
def test_non_new_assist_relation_suppresses_ingestion(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    relation: SupervisorAssistPendingRelation,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    decisions: list[SupervisorAssistPendingDecision] = []
    agent_calls: list[dict[str, Any]] = []

    def classify(
        _person_id: str,
        _message: str,
        *,
        ingress_binding: SupervisorAssistIngressBindingV1,
        **_kwargs: Any,
    ) -> SupervisorAssistPendingDecision:
        if relation is SupervisorAssistPendingRelation.UNCERTAIN:
            decision = SupervisorAssistPendingDecision.uncertain(
                person_id=LEGACY_OWNER_USER_ID,
                conversation_id=_CONVERSATION_ID,
                current=ingress_binding,
            )
        else:
            root_binding = (
                ingress_binding
                if relation is SupervisorAssistPendingRelation.ROOT_REPLAY
                else _binding("cancelled-root")
            )
            decision = SupervisorAssistPendingDecision.for_graph(
                relation=relation,
                pending=_pending(),
                root_request_binding_sha256=root_binding.canonical_sha256(),
                current=ingress_binding,
            )
        decisions.append(decision)
        return decision

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"{relation.value} reached ingest_text")

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        return _result()

    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            classify,
            raising=False,
        )
        monkeypatch.setattr(app.state.agent, "chat", chat)
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden_ingest)
        response = client.post(
            "/api/chat",
            json={
                "message": "cancel" if relation is SupervisorAssistPendingRelation.EXPLICIT_CANCEL else "x",
                "conversation_id": _CONVERSATION_ID,
                "source_ref": f"api-assist:suppress:{relation.value}",
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert len(decisions) == 1
    assert len(agent_calls) == 1
    assert agent_calls[0]["ingestion_result"] is None
    assert agent_calls[0]["_semantic_supervisor_pending_decision"] is decisions[0]
    assert type(agent_calls[0]["_semantic_supervisor_ingress_binding"]) is (
        SupervisorAssistIngressBindingV1
    )


@pytest.mark.parametrize("forgery", ("stale_binding", "foreign_scope"))
def test_mismatched_carried_assist_identity_becomes_scoped_uncertainty(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    from friday.orchestration.supervisor_assist_runtime import SupervisorAssistRuntimeError
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    forged: list[SupervisorAssistPendingDecision] = []
    agent_calls: list[dict[str, Any]] = []

    def classify(
        _person_id: str,
        _message: str,
        *,
        ingress_binding: SupervisorAssistIngressBindingV1,
        **_kwargs: Any,
    ) -> SupervisorAssistPendingDecision:
        if forgery == "stale_binding":
            stale = _binding("stale-request")
            decision = SupervisorAssistPendingDecision.for_graph(
                relation=SupervisorAssistPendingRelation.ROOT_REPLAY,
                pending=_pending(),
                root_request_binding_sha256=stale.canonical_sha256(),
                current=stale,
            )
        else:
            decision = SupervisorAssistPendingDecision.for_graph(
                relation=SupervisorAssistPendingRelation.NEW_TURN,
                pending=_pending(
                    person_id="local:foreign-assist-owner",
                    conversation_id=_FOREIGN_CONVERSATION_ID,
                ),
                root_request_binding_sha256=_binding("foreign-root").canonical_sha256(),
                current=ingress_binding,
            )
        forged.append(decision)
        return decision

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("mismatched carried decision reached ingest_text")

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        decision = kwargs["_semantic_supervisor_pending_decision"]
        if (
            type(decision) is SupervisorAssistPendingDecision
            and decision.relation is SupervisorAssistPendingRelation.UNCERTAIN
        ):
            raise SupervisorAssistRuntimeError("durable assist ownership is uncertain")
        raise AssertionError("mismatched carried identity escaped fail-closed routing")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            classify,
            raising=False,
        )
        monkeypatch.setattr(app.state.agent, "chat", chat)
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden_ingest)
        response = client.post(
            "/api/chat",
            json={
                "message": "adversarial carried identity",
                "conversation_id": _CONVERSATION_ID,
                "source_ref": f"api-assist:forgery:{forgery}",
            },
            headers=headers,
        )

    assert response.status_code == 500, response.text
    assert len(forged) == 1
    assert len(agent_calls) == 1
    carried = agent_calls[0]["_semantic_supervisor_pending_decision"]
    binding = agent_calls[0]["_semantic_supervisor_ingress_binding"]
    assert type(carried) is SupervisorAssistPendingDecision
    assert carried is not forged[0]
    assert carried.relation is SupervisorAssistPendingRelation.UNCERTAIN
    assert carried.person_id == LEGACY_OWNER_USER_ID
    assert carried.conversation_id == _CONVERSATION_ID
    assert carried.pending is None
    assert type(binding) is SupervisorAssistIngressBindingV1
    assert carried.current_request_binding_sha256 == binding.canonical_sha256()
    assert agent_calls[0]["ingestion_result"] is None


@pytest.mark.parametrize(
    ("claim", "status_code"),
    (
        ({"status": "conflict"}, 409),
        ({"status": "in_progress"}, 409),
        ({"status": "acquired", "lease_token": ""}, 500),
    ),
)
def test_failed_idempotency_acquisition_never_mints_assist_binding(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    claim: dict[str, str],
    status_code: int,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    minted: list[dict[str, str]] = []
    agent_calls: list[str] = []

    def forbidden_mint(
        _cls: type[SupervisorAssistIngressBindingV1],
        **kwargs: str,
    ) -> SupervisorAssistIngressBindingV1:
        minted.append(kwargs)
        raise AssertionError("assist binding was minted without a claimed request")

    def forbidden_agent(*_args: Any, **_kwargs: Any) -> bool:
        agent_calls.append("called")
        raise AssertionError("failed acquisition reached the agent")

    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(
            app.state.storage,
            "idempotency_claim",
            lambda *_args, **_kwargs: dict(claim),
        )
        monkeypatch.setattr(
            SupervisorAssistIngressBindingV1,
            "from_claimed_request",
            classmethod(forbidden_mint),
        )
        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            forbidden_agent,
            raising=False,
        )
        monkeypatch.setattr(app.state.agent, "chat", forbidden_agent)
        response = client.post(
            "/api/chat",
            json={
                "message": "must remain before assist ingress",
                "conversation_id": _CONVERSATION_ID,
                "source_ref": "api-assist:failed-acquisition",
            },
            headers=headers,
        )

    assert response.status_code == status_code
    assert minted == []
    assert agent_calls == []
