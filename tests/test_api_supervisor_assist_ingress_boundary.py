"""Request-bound assist ownership is decided before HTTP text ingestion."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday import execution_kernel as execution_kernel_module
from friday.orchestration.supervisor_assist_controller import AssistPendingGraphDisposition
from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.orchestration.supervisor_assist_runtime import SemanticSupervisorAssistRuntime
from friday.orchestration.turn_context import AuthenticatedTurnContext
from friday.orchestration.turn_context_call_scope import (
    require_current_authenticated_chat_call_scope,
)
from friday.orchestration.turn_context_publication import (
    AUTHENTICATED_TURN_PUBLICATION_METADATA_KEY,
)
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
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


def _result(conversation_id: str = _CONVERSATION_ID) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "message_id": "msg_0123456789abcdef",
        "message": "assist ingress boundary response",
        "message_format": "plain",
        "tools_used": [],
        "files": [],
        "voice": None,
        "context": {"interaction_mode": "dialogue"},
    }


def test_live_predecessor_is_checked_once_then_successor_replays_exactly(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    conversation_id = _CONVERSATION_ID
    classifier_calls: list[SupervisorAssistIngressBindingV1] = []
    reconcile_calls: list[SupervisorAssistPendingDecision] = []
    ingest_calls: list[str] = []
    agent_calls: list[dict[str, Any]] = []
    pending_admission = _pending()

    def classify(
        person_id: str,
        _message: str,
        *,
        conversation_id: str,
        ingress_binding: SupervisorAssistIngressBindingV1,
        **_kwargs: Any,
    ) -> SupervisorAssistPendingDecision:
        assert person_id == LEGACY_OWNER_USER_ID
        assert conversation_id == expected_conversation_id
        assert type(ingress_binding) is SupervisorAssistIngressBindingV1
        classifier_calls.append(ingress_binding)
        return SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.NEW_TURN,
            pending=pending_admission,
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

    async def reconcile(
        _person_id: str,
        _message: str,
        *,
        decision: SupervisorAssistPendingDecision,
        **_kwargs: Any,
    ) -> AssistPendingGraphDisposition:
        reconcile_calls.append(decision)
        return AssistPendingGraphDisposition.LIVE_IN_PROCESS

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        context = kwargs["_authenticated_turn_context"]
        assert type(context) is AuthenticatedTurnContext
        assert context.pending_work_admission is None
        assert kwargs["_pending_durable_admission"] is None
        scope = require_current_authenticated_chat_call_scope(context)
        assert scope.pending_work_bound is False
        agent_calls.append(kwargs)
        return _result(expected_conversation_id)

    expected_conversation_id = conversation_id

    with TestClient(app) as client:
        expected_conversation_id = str(
            app.state.storage.create_conversation(
                LEGACY_OWNER_USER_ID,
                title="assist ingress new turn",
            )["id"]
        )
        pending_admission = _pending(conversation_id=expected_conversation_id)
        payload = {
            "message": "Новая независимая реплика",
            "conversation_id": expected_conversation_id,
            "source_ref": "api-assist:new-turn-once",
        }
        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            classify,
            raising=False,
        )
        monkeypatch.setattr(
            app.state.agent,
            "reconcile_pending_before_turn_admission",
            reconcile,
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
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0].relation is SupervisorAssistPendingRelation.NEW_TURN
    assert reconcile_calls[0].pending is pending_admission
    carried_binding = agent_calls[0]["_semantic_supervisor_ingress_binding"]
    carried_decision = agent_calls[0]["_semantic_supervisor_pending_decision"]
    assert type(carried_binding) is SupervisorAssistIngressBindingV1
    assert carried_binding is classifier_calls[0]
    assert carried_decision is None
    assert agent_calls[0]["ingestion_result"]["action"] == "queued"


def test_uncertain_pre_admission_reconciliation_fails_before_ingestion_or_context(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    calls: list[str] = []

    with TestClient(app, raise_server_exceptions=False) as client:
        conversation_id = str(
            app.state.storage.create_conversation(
                LEGACY_OWNER_USER_ID,
                title="uncertain assist predecessor",
            )["id"]
        )
        pending = _pending(conversation_id=conversation_id)

        def classify(
            _person_id: str,
            _message: str,
            *,
            ingress_binding: SupervisorAssistIngressBindingV1,
            **_kwargs: Any,
        ) -> SupervisorAssistPendingDecision:
            calls.append("classify")
            return SupervisorAssistPendingDecision.for_graph(
                relation=SupervisorAssistPendingRelation.NEW_TURN,
                pending=pending,
                root_request_binding_sha256=_binding("uncertain-root").canonical_sha256(),
                current=ingress_binding,
            )

        async def reconcile(*_args: Any, **_kwargs: Any) -> AssistPendingGraphDisposition:
            assert current_primary_authenticated_turn_context() is None
            calls.append("reconcile")
            return AssistPendingGraphDisposition.UNCERTAIN

        async def forbidden(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            calls.append("forbidden")
            raise AssertionError("uncertain predecessor reached successor work")

        monkeypatch.setattr(
            app.state.agent,
            "classify_supervisor_assist_pending",
            classify,
            raising=False,
        )
        monkeypatch.setattr(
            app.state.agent,
            "reconcile_pending_before_turn_admission",
            reconcile,
            raising=False,
        )
        monkeypatch.setattr(app.state.agent, "chat", forbidden)
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden)
        response = client.post(
            "/api/chat",
            json={
                "message": "Независимый новый ход",
                "conversation_id": conversation_id,
                "source_ref": "api-assist:uncertain-pre-admission",
            },
            headers=headers,
        )

    assert response.status_code == 500
    assert calls == ["classify", "reconcile"]


@pytest.mark.parametrize(
    "disposition",
    (
        AssistPendingGraphDisposition.RETIRED,
        AssistPendingGraphDisposition.LIVE_IN_PROCESS,
    ),
)
def test_new_turn_reconciles_before_successor_publication_and_only_once(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    disposition: AssistPendingGraphDisposition,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    ingest_calls: list[str] = []

    async def ingest_text(_user_id: str, content: str, **_kwargs: Any) -> dict[str, Any]:
        ingest_calls.append(content)
        return {
            "promoted": False,
            "queued_for_review": True,
            "action": "queued",
            "category": "note",
            "reason": "ordinary successor",
        }

    with TestClient(app) as client:
        storage = app.state.storage
        conversation_id = str(
            storage.create_conversation(
                LEGACY_OWNER_USER_ID,
                title=f"assist predecessor {disposition.value}",
            )["id"]
        )
        pending = _pending(conversation_id=conversation_id)
        root_binding_sha256 = _binding(f"predecessor:{disposition.value}").canonical_sha256()

        class Primary:
            def __init__(self) -> None:
                self.calls = 0
                self.pending: list[PendingDurableTurnAdmission | None] = []

            async def chat(self, _user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
                context = kwargs.get("_authenticated_turn_context")
                assert type(context) is AuthenticatedTurnContext
                assert current_primary_authenticated_turn_context(context) is context
                assert context.pending_work_admission is None
                assert require_current_authenticated_chat_call_scope(context).pending_work_bound is False
                self.calls += 1
                self.pending.append(kwargs.get("_pending_durable_admission"))
                storage.store_message(
                    conversation_id,
                    LEGACY_OWNER_USER_ID,
                    "user",
                    message,
                    {"interaction_mode": "dialogue"},
                )
                assistant = storage.store_message(
                    conversation_id,
                    LEGACY_OWNER_USER_ID,
                    "assistant",
                    "successor primary response",
                    {"interaction_mode": "dialogue"},
                )
                return {
                    **_result(conversation_id),
                    "message_id": assistant["id"],
                    "message": "successor primary response",
                }

        class Controller:
            def __init__(self) -> None:
                self.classify_calls = 0
                self.reconcile_calls = 0

            def semantic_supervisor_status(self) -> dict[str, object]:
                return {}

            def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> object:
                return pending

            def classify_supervisor_assist_pending(
                self,
                _person_id: str,
                _message: str,
                *,
                ingress_binding: SupervisorAssistIngressBindingV1,
                **_kwargs: Any,
            ) -> SupervisorAssistPendingDecision:
                self.classify_calls += 1
                return SupervisorAssistPendingDecision.for_graph(
                    relation=SupervisorAssistPendingRelation.NEW_TURN,
                    pending=pending,
                    root_request_binding_sha256=root_binding_sha256,
                    current=ingress_binding,
                )

            async def reconcile_pending_before_legacy(
                self,
                *_args: Any,
                **_kwargs: Any,
            ) -> AssistPendingGraphDisposition:
                assert current_primary_authenticated_turn_context() is None
                assert execution_kernel_module._REQUEST_EFFECTS.get() is None
                self.reconcile_calls += 1
                if disposition is AssistPendingGraphDisposition.RETIRED:
                    storage.store_message(
                        conversation_id,
                        LEGACY_OWNER_USER_ID,
                        "assistant",
                        "predecessor terminal publication",
                        {"owner": "predecessor"},
                    )
                return disposition

            async def execute(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("ordinary successor reached fresh assist execution")

            async def cancel_active(self, *_args: Any, **_kwargs: Any) -> None:
                raise AssertionError("ordinary successor reached cancellation")

            async def close(self) -> None:
                return None

        primary = Primary()
        controller = Controller()
        runtime = SemanticSupervisorAssistRuntime(
            settings=SimpleNamespace(
                semantic_supervisor_mode="assist",
                semantic_supervisor_timeout_sec=12.0,
            ),
            primary=primary,  # type: ignore[arg-type]
            controller=controller,  # type: ignore[arg-type]
            conversation_is_dialogue=lambda *_args: True,
        )
        app.state.agent = runtime
        monkeypatch.setattr(app.state.ingestion, "ingest_text", ingest_text)
        payload = {
            "message": f"Точный новый ход {disposition.value}",
            "conversation_id": conversation_id,
            "source_ref": f"api-assist:pre-admission:{disposition.value}",
        }
        first = client.post("/api/chat", json=payload, headers=headers)
        replay = client.post("/api/chat", json=payload, headers=headers)
        messages = storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
        )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert controller.classify_calls == controller.reconcile_calls == 1
    assert primary.calls == 1
    assert ingest_calls == [payload["message"]]
    successor = [row for row in messages if row["content"] != "predecessor terminal publication"]
    assert [(row["role"], row["content"]) for row in successor] == [
        ("user", payload["message"]),
        ("assistant", "successor primary response"),
    ]
    successor_metadata = [json.loads(str(row["metadata_json"])) for row in successor]
    successor_publications = [
        metadata[AUTHENTICATED_TURN_PUBLICATION_METADATA_KEY] for metadata in successor_metadata
    ]
    assert len({item["turn_id"] for item in successor_publications}) == 1
    assert [item["publication_role"] for item in successor_publications] == [
        "user",
        "assistant",
    ]
    if disposition is AssistPendingGraphDisposition.RETIRED:
        predecessor = [row for row in messages if row["content"] == "predecessor terminal publication"]
        assert len(predecessor) == 1
        predecessor_metadata = json.loads(str(predecessor[0]["metadata_json"]))
        assert AUTHENTICATED_TURN_PUBLICATION_METADATA_KEY not in predecessor_metadata
        assert primary.pending == [None]
    else:
        assert len(messages) == 2
        assert primary.pending == [None]


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
    conversation_id = _CONVERSATION_ID
    decisions: list[SupervisorAssistPendingDecision] = []
    agent_calls: list[dict[str, Any]] = []
    pending_admission = _pending()

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
                conversation_id=conversation_id,
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
                pending=pending_admission,
                root_request_binding_sha256=root_binding.canonical_sha256(),
                current=ingress_binding,
            )
        decisions.append(decision)
        return decision

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"{relation.value} reached ingest_text")

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        context = kwargs["_authenticated_turn_context"]
        if relation is SupervisorAssistPendingRelation.UNCERTAIN:
            assert context is None
            assert kwargs["_pending_durable_admission"] is None
        else:
            assert type(context) is AuthenticatedTurnContext
            assert context.pending_work_admission is not None
            assert context.pending_work_admission.admission is pending_admission
            assert kwargs["_pending_durable_admission"] is pending_admission
            scope = require_current_authenticated_chat_call_scope(context)
            assert scope.pending_work_bound is True
        agent_calls.append(kwargs)
        return _result(conversation_id)

    with TestClient(app) as client:
        conversation_id = str(
            app.state.storage.create_conversation(
                LEGACY_OWNER_USER_ID,
                title=f"assist ingress {relation.value}",
            )["id"]
        )
        pending_admission = _pending(conversation_id=conversation_id)
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
                "conversation_id": conversation_id,
                "source_ref": f"api-assist:suppress:{relation.value}",
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert len(decisions) == 1
    assert len(agent_calls) == 1
    assert agent_calls[0]["ingestion_result"] is None
    assert agent_calls[0]["_semantic_supervisor_pending_decision"] is decisions[0]
    assert type(agent_calls[0]["_semantic_supervisor_ingress_binding"]) is (SupervisorAssistIngressBindingV1)


@pytest.mark.parametrize(
    ("relation", "message"),
    (
        (SupervisorAssistPendingRelation.EXPLICIT_CANCEL, "обычный вопрос"),
        (SupervisorAssistPendingRelation.NEW_TURN, "cancel"),
    ),
)
def test_relation_that_disagrees_with_message_becomes_scoped_uncertainty(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    relation: SupervisorAssistPendingRelation,
    message: str,
) -> None:
    from friday.orchestration.supervisor_assist_runtime import SupervisorAssistRuntimeError
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    agent_calls: list[dict[str, Any]] = []

    def classify(
        _person_id: str,
        _message: str,
        *,
        ingress_binding: SupervisorAssistIngressBindingV1,
        **_kwargs: Any,
    ) -> SupervisorAssistPendingDecision:
        return SupervisorAssistPendingDecision.for_graph(
            relation=relation,
            pending=_pending(),
            root_request_binding_sha256=_binding("message-mismatch-root").canonical_sha256(),
            current=ingress_binding,
        )

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("message/relation mismatch reached ingestion")

    async def chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        agent_calls.append(kwargs)
        decision = kwargs["_semantic_supervisor_pending_decision"]
        if (
            type(decision) is SupervisorAssistPendingDecision
            and decision.relation is SupervisorAssistPendingRelation.UNCERTAIN
        ):
            raise SupervisorAssistRuntimeError("durable assist ownership is uncertain")
        raise AssertionError("message/relation mismatch escaped fail-closed routing")

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
                "message": message,
                "conversation_id": _CONVERSATION_ID,
                "source_ref": f"api-assist:message-mismatch:{relation.value}",
            },
            headers=headers,
        )

    assert response.status_code == 500
    assert len(agent_calls) == 1
    carried = agent_calls[0]["_semantic_supervisor_pending_decision"]
    assert type(carried) is SupervisorAssistPendingDecision
    assert carried.relation is SupervisorAssistPendingRelation.UNCERTAIN
    assert agent_calls[0]["ingestion_result"] is None


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
