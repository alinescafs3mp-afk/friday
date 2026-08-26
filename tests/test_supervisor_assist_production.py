from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import friday.orchestration.supervisor_assist_production as production
from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    CompareCurrentFileWebStepKind,
)
from friday.orchestration.router import ReadOnlyAttachmentReference
from friday.orchestration.supervisor_assist_graph_adapter import (
    AssistCapabilityBoundary,
)
from friday.orchestration.supervisor_assist_production import (
    AssistConversationModeReader,
    CurrentTurnAssistFileEvidenceReader,
    SupervisorAssistActorBinding,
    SupervisorAssistAuthorityGate,
    SupervisorPromotedProductObserver,
    supervisor_assist_read_only_effect_gate,
)
from friday.orchestration.supervisor_assist_surface import CurrentFileWebAssistSurface
from friday.permissions import ActorContext, AuthorizationService

_DIGEST = "a" * 64


def _capability_boundary(
    actor: ActorContext,
    *,
    security_id: str | None,
) -> AssistCapabilityBoundary:
    return AssistCapabilityBoundary(
        actor=actor,
        graph_id="graph_0000000000000001",
        user_id=actor.own_id,
        conversation_id="conv_0000000000000001",
        revision=1,
        step_kind=CompareCurrentFileWebStepKind.FILE_READ,
        step_id="file_read",
        capability_id="file_read",
        security_id=security_id,
        adapter_id="friday.orchestration.file_read.V12FileReadHandler",
        attempt=1,
        input_identity_sha256=_DIGEST,
        accepted_plan_sha256=_DIGEST,
        adapter_registry_sha256=_DIGEST,
        current_file_raw_object_id="raw_0000000000000001",
        current_file_source_identity_sha256=_DIGEST,
        current_file_content_sha256=_DIGEST,
    )


def test_production_authority_is_fresh_exact_and_default_deny(storage) -> None:
    storage.ensure_user("alice", preset_key="user")
    authorization = AuthorizationService(storage)
    authorization.grant_permission("alice", "web.compare.transient")
    actor = authorization.actor_for_user("alice", source="test")
    gate = SupervisorAssistAuthorityGate(storage, authorization)

    file_boundary = _capability_boundary(actor, security_id="files.read")
    web_boundary = replace(file_boundary, security_id="web.compare.transient")
    assert gate(actor, file_boundary) is True
    assert gate(actor, web_boundary) is True

    # An equal value is not the process-owned actor carried by this boundary.
    copied_actor = ActorContext(
        user_id=actor.user_id,
        preset_key=actor.preset_key,
        source=actor.source,
    )
    assert copied_actor == actor and copied_actor is not actor
    assert gate(copied_actor, file_boundary) is False

    authorization.deny_permission("alice", "files.read")
    assert gate(actor, file_boundary) is False
    authorization.grant_permission("alice", "files.read")
    storage.update_user("alice", status="disabled")
    assert gate(actor, file_boundary) is False


def test_production_authority_keeps_shared_archive_owner(storage) -> None:
    storage.ensure_user("owner", preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant="owner")
    authorization.grant_permission("owner", "web.compare.transient")
    actor = authorization.actor_for_user("owner", source="test")
    assert actor.shared_tenant is True
    assert actor.user_id == actor.own_id == "owner"

    assert (
        SupervisorAssistAuthorityGate(storage, authorization)(
            actor,
            _capability_boundary(actor, security_id="files.read"),
        )
        is True
    )


def test_production_effect_gate_has_only_the_fixed_read_surface(storage) -> None:
    storage.ensure_user("alice", preset_key="user")
    actor = AuthorizationService(storage).actor_for_user("alice", source="test")
    boundary = _capability_boundary(actor, security_id="files.read")

    assert supervisor_assist_read_only_effect_gate(boundary) is True
    assert (
        supervisor_assist_read_only_effect_gate(
            replace(boundary, security_id="web.compare.transient")
        )
        is True
    )
    assert supervisor_assist_read_only_effect_gate(replace(boundary, security_id=None)) is True
    assert supervisor_assist_read_only_effect_gate(replace(boundary, security_id="code.run")) is False
    assert supervisor_assist_read_only_effect_gate(object()) is False


def test_conversation_reader_requires_live_personal_dialogue(storage) -> None:
    storage.ensure_user("alice", preset_key="user")
    dialogue = storage.create_conversation("alice", mode="dialogue")
    engineer = storage.create_conversation("alice", mode="engineer")
    reader = AssistConversationModeReader(storage)

    assert reader("alice", dialogue["id"]) is True
    assert reader("bob", dialogue["id"]) is False
    assert reader("alice", engineer["id"]) is False
    storage.archive_conversation(dialogue["id"], "alice")
    assert reader("alice", dialogue["id"]) is False


def test_actor_binding_is_stable_and_actor_specific(storage) -> None:
    storage.ensure_user("alice", preset_key="user")
    storage.ensure_user("bob", preset_key="user")
    authorization = AuthorizationService(storage)
    alice = authorization.actor_for_user("alice", source="test")
    bob = authorization.actor_for_user("bob", source="test")
    binding = SupervisorAssistActorBinding(storage)

    first = binding(alice)
    assert binding(alice) == first
    assert binding(bob) != first
    assert len(first) == 64


@pytest.mark.asyncio
async def test_file_reader_delegates_only_the_exact_current_attachment(
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="user")
    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user("alice", source="test")
    surface = object.__new__(CurrentFileWebAssistSurface)
    attachment = ReadOnlyAttachmentReference(
        ordinal=1,
        raw_object_id="raw_0000000000000001",
        source_identity_sha256=_DIGEST,
        name="current.txt",
        media_type="text/plain",
    )
    object.__setattr__(surface, "actor", actor)
    object.__setattr__(surface, "attachment", attachment)
    captured: list[tuple[Any, ...]] = []
    evidence = object()

    def prepare(*args: Any, **kwargs: Any) -> object:
        captured.append((*args, kwargs))
        return evidence

    monkeypatch.setattr(production, "prepare_current_turn_file_evidence", prepare)
    monkeypatch.setattr(
        production,
        "prepared_file_evidence_is_process_owned",
        lambda value: value is evidence,
    )
    reader = CurrentTurnAssistFileEvidenceReader(
        storage=storage,
        authorization=authorization,
        files_root=tmp_path,
        max_bytes=4096,
    )

    assert await reader.prepare(surface, absolute_deadline=time.monotonic() + 1.0) is evidence
    assert len(captured) == 1
    args = captured[0]
    assert args[:5] == (storage, authorization, tmp_path, actor, (attachment,))
    assert args[5] == {"max_bytes": 4096, "absolute_deadline": pytest.approx(args[5]["absolute_deadline"])}

    with pytest.raises(TimeoutError):
        await reader.prepare(surface, absolute_deadline=time.monotonic() - 1.0)
    with pytest.raises(TypeError):
        await reader.prepare(object(), absolute_deadline=time.monotonic() + 1.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ordinary_observer_emits_exactly_once_and_treats_replay_as_success(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="user")
    actor = AuthorizationService(storage).actor_for_user("alice", source="test")
    decision = SimpleNamespace(promotion_admitted=True)
    promotion = SimpleNamespace(decide=lambda **_kwargs: decision)
    request = object()
    built: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []

    def build_request(*_args: Any, **kwargs: Any) -> object:
        built.append(kwargs)
        return request

    def emit(*_args: Any, **kwargs: Any) -> None:
        emitted.append(kwargs)
        if len(emitted) == 2:
            raise production.PromotedProductEventReplayError("exact replay")

    monkeypatch.setattr(production, "build_promoted_other_turn_emission_request", build_request)
    monkeypatch.setattr(production, "emit_promoted_supervisor_product_event_in_transaction", emit)
    observer = SupervisorPromotedProductObserver(
        storage=storage,
        promotion_evaluator=promotion,  # type: ignore[arg-type]
        actor_binding=lambda carried: "b" * 64 if carried is actor else "",
        binding_snapshot_factory=lambda: object(),  # type: ignore[arg-type,return-value]
    )
    response = {
        "message_id": "msg_0000000000000001",
        "conversation_id": "conv_0000000000000001",
    }

    assert await observer.observe_ordinary(response, actor) is True
    assert await observer.observe_ordinary(response, actor) is True
    assert built == [
        {
            "assistant_message_id": response["message_id"],
            "user_id": "alice",
            "conversation_id": response["conversation_id"],
        },
        {
            "assistant_message_id": response["message_id"],
            "user_id": "alice",
            "conversation_id": response["conversation_id"],
        },
    ]
    assert all(item["promotion_decision"] is decision for item in emitted)
    assert all(item["request"] is request for item in emitted)


@pytest.mark.asyncio
async def test_ordinary_observer_distinguishes_ineligibility_from_dependency_failure(
    storage: Any,
) -> None:
    storage.ensure_user("alice", preset_key="user")
    actor = AuthorizationService(storage).actor_for_user("alice", source="test")
    response = {
        "message_id": "msg_0000000000000001",
        "conversation_id": "conv_0000000000000001",
    }
    ineligible = SupervisorPromotedProductObserver(
        storage=storage,
        promotion_evaluator=SimpleNamespace(decide=lambda **_kwargs: None),  # type: ignore[arg-type]
        actor_binding=lambda _actor: "b" * 64,
        binding_snapshot_factory=lambda: object(),  # type: ignore[arg-type,return-value]
    )
    assert await ineligible.observe_ordinary(response, actor) is False

    def broken_snapshot() -> Any:
        raise RuntimeError("synthetic snapshot failure")

    failed = SupervisorPromotedProductObserver(
        storage=storage,
        promotion_evaluator=SimpleNamespace(decide=lambda **_kwargs: None),  # type: ignore[arg-type]
        actor_binding=lambda _actor: "b" * 64,
        binding_snapshot_factory=broken_snapshot,
    )
    with pytest.raises(RuntimeError, match="synthetic snapshot failure"):
        await failed.observe_ordinary(response, actor)
