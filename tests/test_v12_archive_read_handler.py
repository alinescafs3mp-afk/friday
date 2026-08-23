from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import friday.file_evidence_reader as file_evidence_reader
from friday.api.projections import public_chat_ingestion, public_conversation_message
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration import (
    OrchestrationRouter,
    ReadOnlyRouteRequest,
    RouteClass,
    TurnInput,
    TurnPlan,
)
from friday.orchestration.archive_read import V12ArchiveReadHandler
from friday.orchestration.capability_outcome import (
    CapabilityOutcomeStatus,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.file_read import V12FileReadError
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id


def _actor(*, preset_key: str = "owner") -> ActorContext:
    return ActorContext(
        user_id="alice",
        preset_key=preset_key,
        source="v12-archive-handler-test",
    )


def _plan(*, max_items: int = 2) -> TurnPlan:
    return TurnPlan.parse(
        {
            "schema": "friday.turn-plan.v1",
            "route": "archive_read",
            "objective": "Обобщить выбранные ранее загруженные документы",
            "evidence_requests": [
                {
                    "kind": "archive",
                    "query": "",
                    "max_items": max_items,
                    "required": True,
                }
            ],
            "tool_intents": [],
            "output": {
                "format": "text",
                "language": "ru",
                "require_citations": True,
                "one_message": True,
            },
            "confidence": 0.99,
            "fallback": "legacy",
            "reason_code": "bounded_archive_files",
        }
    )


def _request(
    message: str,
    *,
    actor: ActorContext,
    conversation_id: str | None = None,
) -> tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]:
    turn = TurnInput.from_chat(
        message=message,
        actor=actor,
        conversation_id=conversation_id,
        attachments=[],
        enable_tools=True,
        synthetic_document_notice=False,
        mode=None,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    request = ReadOnlyRouteRequest(
        user_id=actor.user_id,
        actor=actor,
        conversation_id=conversation_id,
        attachments=(),
        synthetic_document_notice=False,
        replay_source_message_id=None,
        conversation_mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
        reply_assistant_message_id=None,
        turn_deadline=time.monotonic() + 10,
    )
    return request, turn, _plan()


def _text_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _registered_text_file(
    storage: Any,
    settings: Any,
    *,
    text: str,
    filename: str,
    uploaded_by: str = "alice",
    received_at: datetime | None = None,
    document_date: str | None = None,
) -> str:
    if storage.get_user("alice") is None:
        storage.ensure_user("alice", preset_key="owner", display_name="Alice", username="alice")
    if storage.get_user(uploaded_by) is None:
        storage.ensure_user(
            uploaded_by,
            preset_key="user",
            display_name=uploaded_by.title(),
            username=uploaded_by,
        )
    content = text.encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    relative = f"alice/{digest[:2]}/{digest}-{new_id('file')}.txt"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "mime_type": "text/plain",
            "stored_path": relative,
            "sha256": digest,
            "size_bytes": len(content),
            "uploaded_by": uploaded_by,
            **({"document_date": document_date} if document_date is not None else {}),
            "extraction_receipt_version": 1,
            "extraction_success": True,
            "extraction_error": "",
            "text_extraction_success": True,
            "text_sha256": _text_digest(text),
            "extraction_chars": len(text),
            "text_truncated": False,
            "archive_truncated": False,
            "source_truncated_for_parse": False,
            "parse_deadline_reached": False,
            "parse_pages_read": 0,
            "parse_pages_truncated": False,
            "parse_total_pages": 0,
            "vision_pages_total": 0,
            "vision_pages_read": 0,
            "archive_files": 0,
            "archive_files_read": 0,
            "vision_used": False,
            "vision_review_required": False,
            "unsupported_format": False,
        },
    )
    storage.store_raw_object(raw)
    if received_at is not None:
        storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?",
            (received_at.astimezone(UTC).isoformat(), raw.id),
        )
        storage.commit()
    return raw.id


class _Model:
    enabled = True
    model = "v12-archive-handler-test"

    def __init__(
        self,
        synthesis: str,
        *,
        labels: tuple[str, ...],
        mutate: Callable[[], None] | None = None,
    ) -> None:
        self.synthesis = synthesis
        self.verifier = json.dumps(
            {
                "schema": "friday.v12-file-verifier.v1",
                "supported": True,
                "citation_labels": list(labels),
                "unsupported_claims": 0,
            }
        )
        self.mutate = mutate
        self.calls: list[dict[str, Any]] = []
        self.lease: ModelProfileLease | None = None

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None:
        assert absolute_deadline > time.monotonic()
        self.lease = ModelProfileLease(
            profile_id="v12-archive-handler-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=requirements.required_context_tokens,
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=requirements.max_tool_steps,
            effect=requirements.effect,
            verifier_required=requirements.verifier_required,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        assert absolute_deadline > time.monotonic()
        return bool(
            lease is self.lease
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
        )

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert await self.lease_is_current(
            lease,
            requirements,
            absolute_deadline=float(kwargs["absolute_deadline"]),
        )
        self.calls.append({"messages": messages, **kwargs})
        if len(self.calls) == 1:
            if self.mutate is not None:
                self.mutate()
            content = self.synthesis
        else:
            content = self.verifier
        return {
            "content": content,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def _handler(
    storage: Any,
    settings: Any,
    model: _Model,
) -> tuple[V12ArchiveReadHandler, AuthorizationService]:
    authorization = AuthorizationService(storage)
    return (
        V12ArchiveReadHandler(
            storage=storage,
            authorization=authorization,
            settings=settings,
            model=model,
        ),
        authorization,
    )


def _recent(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _raw_identity_pin(storage: Any, raw_id: str) -> str:
    raw = storage.get_raw_object(raw_id, "alice")
    if not isinstance(raw, dict):
        return "a" * 64
    return raw_source_identity_sha256(
        {
            **raw,
            "_raw_content": raw.get("raw_content"),
            "_raw_metadata": raw.get("metadata_json"),
        }
    )


def _source_search_conversation(
    storage: Any,
    raw_ids: list[Any],
    *,
    extra_metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if storage.get_user("alice") is None:
        storage.ensure_user("alice", preset_key="owner", display_name="Alice", username="alice")
    conversation = storage.create_conversation("alice")
    conversation_id = str(conversation["id"])
    user_message = storage.store_message(
        conversation_id,
        "alice",
        "user",
        "Найди мои документы по теме проекта",
    )
    identities: dict[str, str] = {}
    if type(raw_ids) is list:
        for value in raw_ids:
            if not isinstance(value, str):
                continue
            raw_id = str(value)
            if raw_id in identities:
                continue
            identities[raw_id] = _raw_identity_pin(storage, raw_id)
    metadata = {
        "private_context_lineage": True,
        "source_search_result_identities": identities,
        "source_search_result_raw_ids": raw_ids,
        "tools_used": ["source_search"],
        **(extra_metadata or {}),
    }
    assistant = storage.store_message(
        conversation_id,
        "alice",
        "assistant",
        "Нашла подходящие документы.",
        metadata=metadata,
        reply_to=str(user_message["id"]),
    )
    return conversation_id, str(assistant["id"])


@pytest.mark.asyncio
async def test_exact_filename_selects_one_unique_registered_file(settings, storage) -> None:
    wanted = _registered_text_file(
        storage,
        settings,
        text="EXACT-WANTED",
        filename="field-report.txt",
        received_at=_recent(2),
    )
    _registered_text_file(
        storage,
        settings,
        text="UNRELATED-DECOY",
        filename="other.txt",
        received_at=_recent(1),
    )
    actor = _actor()
    model = _Model("Найден нужный отчёт. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи файл «field-report.txt»", actor=actor)

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    assert result.citation_labels == ("A1",)
    assert result.outcome.status is CapabilityOutcomeStatus.COMPLETE
    assert result.outcome.route is RouteClass.ARCHIVE_READ
    payload = json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert "EXACT-WANTED" in payload
    assert "UNRELATED-DECOY" not in payload
    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    request_metadata = json.loads(messages[0]["metadata_json"])
    assert "conversation_uploaded_raw_ids" not in request_metadata
    assert request_metadata["conversation_attachment_raw_ids"] == [wanted]
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [wanted]
    receipt = load_accepted_capability_outcome_receipt(
        metadata,
        expected_outcome=result.outcome,
    )
    assert receipt.outcome.route is RouteClass.ARCHIVE_READ
    assert receipt.outcome_sha256 == result.outcome.canonical_sha256()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "ordinal"),
    [
        ("Прочитай первый найденный файл целиком", 1),
        ("Прочитай второй найденный документ", 2),
        ("Пожалуйста, прочти 2-й найденный материал полностью.", 2),
    ],
)
async def test_immediate_source_search_ordinal_reads_only_selected_raw_with_receipt_and_privacy(
    settings,
    storage,
    message: str,
    ordinal: int,
) -> None:
    first = _registered_text_file(storage, settings, text="FIRST-CANDIDATE", filename="first.txt")
    second = _registered_text_file(storage, settings, text="SECOND-CANDIDATE", filename="second.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [first, second])
    model = _Model("Выбранный документ прочитан. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor(), conversation_id=conversation_id)

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    selected = (first, second)[ordinal - 1]
    decoy = (second, first)[ordinal - 1]
    payload = json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert ("FIRST-CANDIDATE", "SECOND-CANDIDATE")[ordinal - 1] in payload
    assert ("SECOND-CANDIDATE", "FIRST-CANDIDATE")[ordinal - 1] not in payload
    messages = storage.get_conversation_messages(conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [selected]
    receipt = load_accepted_capability_outcome_receipt(metadata, expected_outcome=result.outcome)
    assert receipt.outcome.route is RouteClass.ARCHIVE_READ
    assert receipt.outcome_sha256 == result.outcome.canonical_sha256()

    public_message = public_conversation_message(messages[-1])
    public_chat = public_chat_ingestion(
        {
            "message": result.message,
            "accepted_capability_outcome": metadata["accepted_capability_outcome"],
            "source_search_result_identities": {selected: "f" * 64},
        }
    )
    public_payload = json.dumps((public_message, public_chat), ensure_ascii=False)
    assert selected not in public_payload
    assert decoy not in public_payload
    assert "source_search_result_raw_ids" not in public_payload
    assert "source_search_result_identities" not in public_payload
    assert "accepted_capability_outcome" not in public_payload


@pytest.mark.asyncio
async def test_stale_source_search_page_is_not_recovered_past_a_new_dialogue_turn(
    settings,
    storage,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="STALE", filename="stale.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    storage.store_message(conversation_id, "alice", "user", "Это другой вопрос")
    storage.store_message(conversation_id, "alice", "assistant", "Это другой ответ")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_ids",
    [
        "raw_0123456789abcdef",
        ["not-a-raw-id"],
        ["raw_0123456789abcdef", "raw_0123456789abcdef"],
        [1],
        [f"raw_{index:016x}" for index in range(11)],
    ],
)
async def test_malformed_source_search_candidate_metadata_falls_back_before_read(
    settings,
    storage,
    raw_ids: Any,
) -> None:
    conversation_id, _source_message_id = _source_search_conversation(storage, raw_ids)
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_oversized_source_search_metadata_falls_back_before_read(settings, storage) -> None:
    raw_id = _registered_text_file(storage, settings, text="PRIVATE", filename="private.txt")
    conversation_id, source_message_id = _source_search_conversation(storage, [raw_id])
    oversized = json.dumps(
        {
            "private_context_lineage": True,
            "source_search_result_identities": {raw_id: "a" * 64},
            "source_search_result_raw_ids": [raw_id],
            "tools_used": ["source_search"],
            "padding": "x" * 65_536,
        },
        ensure_ascii=False,
    )
    storage.execute("UPDATE messages SET metadata_json=? WHERE id=?", (oversized, source_message_id))
    storage.commit()
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_duplicate_source_search_metadata_key_falls_back_before_read(settings, storage) -> None:
    first = _registered_text_file(storage, settings, text="FIRST", filename="first.txt")
    second = _registered_text_file(storage, settings, text="SECOND", filename="second.txt")
    conversation_id, source_message_id = _source_search_conversation(storage, [first])
    duplicate = (
        '{"private_context_lineage":true,"tools_used":["source_search"],'
        f'"source_search_result_raw_ids":["{first}"],'
        f'"source_search_result_raw_ids":["{second}"]}}'
    )
    storage.execute("UPDATE messages SET metadata_json=? WHERE id=?", (duplicate, source_message_id))
    storage.commit()
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_legacy_source_search_page_without_identity_pins_falls_back(settings, storage) -> None:
    raw_id = _registered_text_file(storage, settings, text="LEGACY", filename="legacy.txt")
    conversation_id, source_message_id = _source_search_conversation(storage, [raw_id])
    storage.execute(
        "UPDATE messages SET metadata_json=? WHERE id=?",
        (
            json.dumps(
                {
                    "private_context_lineage": True,
                    "source_search_result_raw_ids": [raw_id],
                    "tools_used": ["source_search"],
                }
            ),
            source_message_id,
        ),
    )
    storage.commit()
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_source_mutation_after_search_before_followup_prepare_falls_back(
    settings,
    storage,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="ORIGINAL", filename="original.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    storage.execute(
        "UPDATE raw_objects SET raw_content='MUTATED-BEFORE-PREPARE', content_hash=? WHERE id=?",
        ("0" * 64, raw_id),
    )
    storage.commit()
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Прочитай второй найденный файл",
        "Прочитай 3-й найденный документ целиком",
    ],
)
async def test_out_of_range_source_search_ordinal_falls_back(
    settings,
    storage,
    message: str,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="ONLY", filename="only.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor(), conversation_id=conversation_id)

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_tenth_candidate_is_the_closed_source_search_page_limit(settings, storage) -> None:
    candidates = [
        _registered_text_file(
            storage,
            settings,
            text=f"CANDIDATE-{index}",
            filename=f"candidate-{index}.txt",
        )
        for index in range(1, 11)
    ]
    conversation_id, _source_message_id = _source_search_conversation(storage, candidates)
    model = _Model("Десятый документ прочитан. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай 10-й найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [candidates[-1]]
    assert result.outcome.route is RouteClass.ARCHIVE_READ


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Прочитай найденный файл",
        "Прочитай все найденные файлы",
        "Прочитай файл №1",
        "Не читай первый найденный файл",
        "Я сказал: прочитай первый найденный файл",
    ],
)
async def test_non_closed_source_search_followups_remain_legacy_owned(
    settings,
    storage,
    message: str,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="PRIVATE", filename="private.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor(), conversation_id=conversation_id)

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_foreign_source_search_candidate_never_crosses_uploader_boundary(
    settings,
    storage,
) -> None:
    foreign = _registered_text_file(
        storage,
        settings,
        text="BOB-PRIVATE",
        filename="bob.txt",
        uploaded_by="bob",
    )
    conversation_id, _source_message_id = _source_search_conversation(storage, [foreign])
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_source_search_conversation_and_active_person_are_exact_authority_boundaries(
    settings,
    storage,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="ALICE", filename="alice.txt")
    storage.ensure_user("bob", preset_key="user", display_name="Bob", username="bob")
    foreign_conversation = storage.create_conversation("bob")
    storage.store_message(
        str(foreign_conversation["id"]),
        "bob",
        "assistant",
        "Чужая поисковая выдача",
        metadata={
            "private_context_lineage": True,
            "source_search_result_identities": {raw_id: _raw_identity_pin(storage, raw_id)},
            "source_search_result_raw_ids": [raw_id],
            "tools_used": ["source_search"],
        },
    )
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=str(foreign_conversation["id"]),
    )
    assert await handler.prepare(request, turn, plan) is None

    own_conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    storage.update_user("alice", status="disabled")
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=own_conversation_id,
    )
    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "intervening",
        "source_row_marker_removed",
        "deleted",
        "ignored",
        "mutated",
        "candidate_reordered",
    ],
)
async def test_source_search_selection_drift_during_synthesis_rolls_back_publication(
    settings,
    storage,
    drift: str,
) -> None:
    selected = _registered_text_file(storage, settings, text="SELECTED", filename="selected.txt")
    decoy = _registered_text_file(storage, settings, text="DECOY", filename="decoy.txt")
    selected_pin = _raw_identity_pin(storage, selected)
    decoy_pin = _raw_identity_pin(storage, decoy)
    conversation_id, source_message_id = _source_search_conversation(storage, [selected, decoy])

    def mutate() -> None:
        if drift == "intervening":
            storage.store_message(conversation_id, "alice", "assistant", "Вмешавшийся ответ")
        elif drift == "source_row_marker_removed":
            storage.execute(
                "UPDATE messages SET metadata_json=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "private_context_lineage": False,
                            "source_search_result_identities": {
                                selected: selected_pin,
                                decoy: decoy_pin,
                            },
                            "source_search_result_raw_ids": [selected, decoy],
                            "tools_used": ["source_search"],
                        }
                    ),
                    source_message_id,
                ),
            )
            storage.commit()
        elif drift == "deleted":
            storage.execute(
                "UPDATE raw_objects SET deleted_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), selected),
            )
            storage.commit()
        elif drift == "ignored":
            storage.store_inbox_item(
                InboxItem(
                    id=new_id("inbox"),
                    user_id="alice",
                    raw_object_id=selected,
                    status=InboxStatus.IGNORED,
                )
            )
        elif drift == "mutated":
            storage.execute(
                "UPDATE raw_objects SET raw_content='TAMPERED', content_hash=? WHERE id=?",
                ("0" * 64, selected),
            )
            storage.commit()
        elif drift == "candidate_reordered":
            storage.execute(
                "UPDATE messages SET metadata_json=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "private_context_lineage": True,
                            "source_search_result_identities": {
                                selected: selected_pin,
                                decoy: decoy_pin,
                            },
                            "source_search_result_raw_ids": [decoy, selected],
                            "tools_used": ["source_search"],
                        }
                    ),
                    source_message_id,
                ),
            )
            storage.commit()
        else:  # pragma: no cover - closed parameter set above
            raise AssertionError(drift)

    model = _Model("Выбранный документ прочитан. [A1]", labels=("A1",), mutate=mutate)
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Прочитай первый найденный файл целиком",
        actor=_actor(),
        conversation_id=conversation_id,
    )
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    with pytest.raises(V12FileReadError, match="authority changed"):
        await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(conversation_id, user_id="alice")
    assert all(row["content"] != turn.message for row in messages)


@pytest.mark.asyncio
async def test_real_router_dispatches_immediate_source_search_ordinal_to_archive_read(
    settings,
    storage,
) -> None:
    raw_id = _registered_text_file(storage, settings, text="ROUTER-SOURCE", filename="router.txt")
    conversation_id, _source_message_id = _source_search_conversation(storage, [raw_id])
    phrase = "Прочитай первый найденный файл целиком"

    class _Planner:
        def __init__(self) -> None:
            self.turns: list[TurnInput] = []

        async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
            del turn, turn_deadline
            raise AssertionError("V12 router must use attested planning")

        async def plan_attested(
            self,
            turn: TurnInput,
            *,
            turn_deadline: float | None = None,
        ) -> TurnPlan:
            del turn_deadline
            self.turns.append(turn)
            return _plan()

    class _NeverLegacy:
        async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
            del user_id, message, kwargs
            raise AssertionError("eligible source result must not fall back to legacy")

    planner = _Planner()
    model = _Model("Маршрутизированный документ прочитан. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    router = OrchestrationRouter(
        _NeverLegacy(),
        planner,
        mode="v12",
        allowed_routes=("archive_read",),
        route_handlers={RouteClass.ARCHIVE_READ: handler},
        planner_timeout_sec=1.0,
        preparation_timeout_sec=2.0,
        route_timeout_sec=10.0,
    )

    result = await router.chat(
        "alice",
        phrase,
        actor=_actor(),
        conversation_id=conversation_id,
        attachments=[],
        enable_tools=True,
        turn_deadline=time.monotonic() + 60,
    )

    assert result["message"] == "Маршрутизированный документ прочитан. [A1]"
    assert len(planner.turns) == 1
    assert planner.turns[0].message == phrase
    assert [item.status for item in router.observations[-2:]] == ["selected", "completed"]
    messages = storage.get_conversation_messages(conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    receipt = load_accepted_capability_outcome_receipt(metadata)
    assert receipt.outcome.route is RouteClass.ARCHIVE_READ


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "requested", "available", "excluded"),
    [
        ("Обобщи документы за сегодня", 1, ("TODAY-ONE",), ()),
        ("Обобщи документы за сегодня", 2, ("TODAY-OLD", "TODAY-NEW"), ()),
        ("Обобщи последний 1 файл", 1, ("LATEST", "OLDER"), ("OLDER",)),
        ("Обобщи последние 2 файла", 2, ("LATEST", "MIDDLE", "OLDER"), ("OLDER",)),
    ],
)
async def test_date_and_latest_select_exactly_one_or_two_files(
    settings,
    storage,
    message: str,
    requested: int,
    available: tuple[str, ...],
    excluded: tuple[str, ...],
) -> None:
    for index, text in enumerate(available, start=1):
        _registered_text_file(
            storage,
            settings,
            text=text,
            filename=f"{text.casefold()}.txt",
            received_at=_recent(index),
        )
    labels = tuple(f"A{index}" for index in range(1, requested + 1))
    answer = " ".join(f"Источник {label}. [{label}]" for label in labels)
    model = _Model(answer, labels=labels)
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor())

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    assert result.citation_labels == labels
    payload = json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    selected = available[:requested] if "последн" in message else available
    assert all(text in payload for text in selected)
    assert all(text not in payload for text in excluded)


@pytest.mark.asyncio
async def test_document_date_selector_is_distinct_from_upload_time(settings, storage) -> None:
    settings = replace(settings, local_timezone="UTC")
    today = datetime.now(ZoneInfo(settings.local_timezone or "UTC")).date()
    yesterday = today - timedelta(days=1)
    wanted = _registered_text_file(
        storage,
        settings,
        text="DOCUMENT-DATE-TODAY",
        filename="dated-today.txt",
        received_at=_recent(60 * 48),
        document_date=today.isoformat(),
    )
    _registered_text_file(
        storage,
        settings,
        text="DOCUMENT-DATE-YESTERDAY",
        filename="dated-yesterday.txt",
        received_at=_recent(1),
        document_date=yesterday.isoformat(),
    )
    model = _Model("Выбран документ с сегодняшней датой. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи документы, датированные сегодня", actor=_actor())

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [wanted]
    payload = json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert "DOCUMENT-DATE-TODAY" in payload
    assert "DOCUMENT-DATE-YESTERDAY" not in payload


@pytest.mark.asyncio
async def test_more_than_two_date_files_and_duplicate_exact_name_fall_back(settings, storage) -> None:
    for index in range(3):
        _registered_text_file(
            storage,
            settings,
            text=f"TODAY-{index}",
            filename=f"today-{index}.txt",
            received_at=_recent(index + 1),
        )
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи документы за сегодня", actor=_actor())
    assert await handler.prepare(request, turn, plan) is None

    _registered_text_file(storage, settings, text="DUPLICATE-A", filename="same.txt")
    _registered_text_file(storage, settings, text="DUPLICATE-B", filename="same.txt")
    request, turn, plan = _request("Обобщи файл same.txt", actor=_actor())
    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_filename_handle_is_data_and_not_a_foreign_uploader(settings, storage) -> None:
    raw_id = _registered_text_file(
        storage,
        settings,
        text="HANDLE-FILENAME",
        filename="@bob.txt",
    )
    model = _Model("Найден собственный файл. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи файл @bob.txt", actor=_actor())

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [raw_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Обобщи файл a.txt и файл b.txt",
        "Обобщи файлы a.txt и b.txt",
        "Обобщи файл a.txt за вчера",
        "Обобщи последние 2 файла за вчера",
        "Обобщи документы за вчера вечером",
        "Обобщи документы за вчера по UTC",
        "Обобщи документы за вчера и сегодня",
        "Обобщи документы до вчера",
        "Обобщи документы до сегодня",
        "Обобщи документы с позавчера",
        "Обобщи документы пользователя @bob и пользователя @mallory",
        "Обобщи документы Боба за вчера",
        "Обобщи файлы от Боба за вчера",
        "Обобщи документы у Боба за вчера",
        "Обобщи Бобовы документы за вчера",
        "Обобщи документы за вчера, которые прислал Боб",
        "Обобщи присланные Бобом вчера документы",
        "Обобщи последние два документа, которые прислал Боб",
        "Обобщи последние два документа, загруженные Бобом",
        "Обобщи последние два документа, автор Боб",
        "Обобщи последние два документа из загрузок Боба",
        "Обобщи его последние два документа",
        "Обобщи её последние два документа",
        "Обобщи их последние два документа",
        "Обобщи наши документы за сегодня",
        "Обобщи все документы за сегодня",
        "Обобщи общие документы за сегодня",
        "Обобщи командные документы за сегодня",
        "Прочитай файл a.txt, который прислал Боб",
        "Прочитай присланный Бобом файл a.txt",
        "Прочитай файл a.txt от моего коллеги Боба",
        "Обобщи файл a.txt Дениса",
        "Обобщи файл a.txt Майи",
        "Обобщи файл a.txt Марта",
        "Обобщи файл a.txt 李雷",
        "Обобщи файл a.txt Δημήτρη",
        "Обобщи файл a.txt أحمد",
        "Вчера я просил: обобщи документы",
        "Не обобщай документы за вчера",
        "Скажи, можно ли обобщить документы за вчера",
        "Найди документы, содержащие данные про вчера",
        "Найди документы про вчера",
        "Обобщи 'мои документы за сегодня'",
        "Обобщи “мои документы за сегодня”",
        "Обобщи „мои документы за сегодня“",
        "Обобщи московские документы за вчера",
        "Обобщи мокрые документы за вчера",
        "Обобщи модифицированные документы за вчера",
        "Обобщи сводные документы за вчера",
        "Обобщи личностные документы за вчера",
        "Обобщи краткие документы за вчера",
        "Обобщи краткий файл a.txt",
        "Обобщи документы за последние два дневниковых дня",
        "Обобщи документы за мартовский март",
        "Обобщение документов за вчера",
        "Обобщённые документы за вчера",
        "Прочитанные документы за вчера",
        "Найденные документы за вчера",
        "Перечисленные документы за вчера",
        "Суммаризация документов за вчера",
        "Перескажи документы за вчера",
        "Сделай файл a.txt",
        "Сделай документы за вчера",
        "Обобщи полученные вчера документы по дате документа",
        "Обобщи документы за вчера, не по дате документа",
    ],
)
async def test_compound_or_lossy_archive_selectors_fall_back_before_model(
    settings,
    storage,
    message: str,
) -> None:
    _registered_text_file(storage, settings, text="A", filename="a.txt")
    _registered_text_file(storage, settings, text="B", filename="b.txt")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor())

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_archive_body_hydration_occurs_only_after_fresh_authorization(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registered_text_file(storage, settings, text="PRIVATE-BODY", filename="private.txt")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи файл private.txt", actor=_actor())
    hydrated: list[bool] = []
    original = storage.get_searchable_file_sources

    def observe_hydration(*args: Any, **kwargs: Any) -> Any:
        hydrated.append(bool(storage.conn.in_transaction and kwargs.get("include_content") is True))
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "get_searchable_file_sources", observe_hydration)
    authorization.deny_permission("alice", "files.read")

    assert await handler.prepare(request, turn, plan) is None
    assert hydrated == []
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "metadata_updates"),
    [
        ("unsupported.pdf", {"mime_type": "application/pdf"}),
        ("advisory-ocr.txt", {"vision_used": True, "vision_pages_total": 1}),
        ("partial.txt", {"text_truncated": True}),
    ],
)
async def test_unsupported_historical_sources_fall_back_before_body_or_file_read(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    metadata_updates: dict[str, Any],
) -> None:
    raw_id = _registered_text_file(storage, settings, text="PRIVATE-BODY", filename=filename)
    row = storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_id,)).fetchone()
    metadata = json.loads(str(row["metadata_json"]))
    metadata.update(metadata_updates)
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), raw_id),
    )
    storage.commit()

    def body_read_forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsupported historical source crossed the metadata-only preflight")

    monkeypatch.setattr(storage, "get_searchable_file_sources", body_read_forbidden)
    monkeypatch.setattr(
        file_evidence_reader,
        "read_authorized_file_in_transaction",
        body_read_forbidden,
    )
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(f"Обобщи файл {filename}", actor=_actor())

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_archive_body_hydration_is_inside_the_authorized_transaction(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registered_text_file(storage, settings, text="AUTHORIZED-BODY", filename="authorized.txt")
    model = _Model("Источник прочитан. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи файл authorized.txt", actor=_actor())
    hydrated: list[bool] = []
    original = storage.get_searchable_file_sources

    def observe_hydration(*args: Any, **kwargs: Any) -> Any:
        hydrated.append(bool(storage.conn.in_transaction and kwargs.get("include_content") is True))
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "get_searchable_file_sources", observe_hydration)

    assert await handler.prepare(request, turn, plan) is not None
    assert hydrated == [True]


@pytest.mark.asyncio
async def test_archive_replay_stays_with_the_legacy_owner_before_body_read(settings, storage) -> None:
    _registered_text_file(storage, settings, text="HISTORICAL", filename="historical.txt")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи файл historical.txt", actor=_actor())
    request = replace(request, replay_source_message_id="msg_0000000000000001")

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_latest_two_never_silently_shrinks_to_one(settings, storage) -> None:
    _registered_text_file(storage, settings, text="ONLY-ONE", filename="only.txt")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request("Обобщи последние два документа", actor=_actor())

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Пожалуйста обобщи мне кратко содержание моих документов за вчера",
        "Обобщи последний один файл",
    ],
)
async def test_closed_self_archive_phrases_remain_admitted(
    settings,
    storage,
    message: str,
) -> None:
    _registered_text_file(
        storage,
        settings,
        text="SELF-SOURCE",
        filename="self.txt",
        received_at=_recent(60 * 24),
    )
    model = _Model("Собственный источник. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(message, actor=_actor())

    assert await handler.prepare(request, turn, plan) is not None


@pytest.mark.asyncio
async def test_quoted_document_date_words_do_not_change_the_received_clock(settings, storage) -> None:
    wanted = _registered_text_file(
        storage,
        settings,
        text="RECEIVED-TODAY",
        filename="received.txt",
        received_at=_recent(1),
        document_date=(
            datetime.now(ZoneInfo(settings.local_timezone or "UTC")).date() - timedelta(days=1)
        ).isoformat(),
    )
    model = _Model("Выбран присланный сегодня документ. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Обобщи присланные сегодня документы и укажи поле «дата документа»",
        actor=_actor(),
    )

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    messages = storage.get_conversation_messages(result.conversation_id, user_id="alice")
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [wanted]


@pytest.mark.asyncio
async def test_selector_membership_drift_before_selection_keeps_legacy_owner(
    settings,
    storage,
) -> None:
    conversation = storage.create_conversation("alice")
    _registered_text_file(storage, settings, text="ORIGINAL", filename="same.txt")
    model = _Model("Не должен вызываться. [A1]", labels=("A1",))
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Обобщи файл same.txt",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
    )
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    _registered_text_file(storage, settings, text="NEW-MEMBER", filename="same.txt")

    assert await handler.preparation_is_current(request, turn, plan, preparation) is False
    assert model.calls == []
    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["exact_filename", "time_window"])
async def test_selector_membership_drift_during_synthesis_rolls_back_publication(
    settings,
    storage,
    selector: str,
) -> None:
    conversation = storage.create_conversation("alice")
    _registered_text_file(
        storage,
        settings,
        text="ORIGINAL",
        filename="stable.txt",
        received_at=_recent(2),
    )

    def add_member() -> None:
        _registered_text_file(
            storage,
            settings,
            text="LATE-MEMBER",
            filename="stable.txt" if selector == "exact_filename" else "late.txt",
            received_at=_recent(1),
        )

    model = _Model("Ответ по исходному набору. [A1]", labels=("A1",), mutate=add_member)
    handler, _authorization = _handler(storage, settings, model)
    message = "Обобщи файл stable.txt" if selector == "exact_filename" else "Обобщи документы за сегодня"
    request, turn, plan = _request(
        message,
        actor=_actor(),
        conversation_id=str(conversation["id"]),
    )
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    with pytest.raises(V12FileReadError, match="authority changed"):
        await handler.handle(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_named_uploader_stays_legacy_owned_before_any_historical_body_read(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="admin", display_name="Alice", username="alice")
    storage.ensure_user("bob", preset_key="user", display_name="Bob", username="bob")
    _registered_text_file(
        storage,
        settings,
        text="BOB-PRIVATE-SOURCE",
        filename="bob-report.txt",
        uploaded_by="bob",
    )
    _registered_text_file(
        storage,
        settings,
        text="ALICE-SAME-NAME-DECOY",
        filename="bob-report.txt",
        uploaded_by="alice",
    )
    actor = _actor(preset_key="admin")
    authorization = AuthorizationService(storage)
    assert authorization.authorize(actor, "files.read").allowed
    assert authorization.authorize(actor, "admin.all_data.read").allowed
    model = _Model("Документ Боба прочитан. [A1]", labels=("A1",))
    handler = V12ArchiveReadHandler(
        storage=storage,
        authorization=authorization,
        settings=settings,
        model=model,
    )
    request, turn, plan = _request(
        "Обобщи файл bob-report.txt пользователя @bob",
        actor=actor,
    )

    assert await handler.prepare(request, turn, plan) is None
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "files_read_denied",
        "actor_inactive",
        "uploader_changed",
        "raw_changed",
        "received_date_changed",
    ],
)
async def test_authority_and_source_drift_never_publish(
    settings,
    storage,
    drift: str,
) -> None:
    preset = "owner"
    storage.ensure_user("alice", preset_key=preset, display_name="Alice", username="alice")
    uploaded_by = "alice"
    raw_id = _registered_text_file(
        storage,
        settings,
        text="IMMUTABLE-SOURCE",
        filename="drift.txt",
        uploaded_by=uploaded_by,
        received_at=_recent(1),
    )
    conversation = storage.create_conversation("alice")
    actor = _actor(preset_key=preset)
    model = _Model("Источник прочитан. [A1]", labels=("A1",))
    handler, authorization = _handler(storage, settings, model)
    message = "Обобщи документы за сегодня" if drift == "received_date_changed" else "Обобщи файл drift.txt"
    request, turn, plan = _request(
        message,
        actor=actor,
        conversation_id=str(conversation["id"]),
    )
    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None

    if drift == "files_read_denied":
        authorization.deny_permission("alice", "files.read")
    elif drift == "actor_inactive":
        storage.update_user("alice", status="disabled")
    elif drift == "uploader_changed":
        storage.ensure_user("mallory", preset_key="user", username="mallory")
        storage.execute(
            "UPDATE raw_objects SET metadata_json=json_set(metadata_json,'$.uploaded_by','mallory') "
            "WHERE id=?",
            (raw_id,),
        )
        storage.commit()
    elif drift == "raw_changed":
        storage.execute(
            "UPDATE raw_objects SET raw_content='TAMPERED', content_hash=? WHERE id=?",
            ("0" * 64, raw_id),
        )
        storage.commit()
    elif drift == "received_date_changed":
        storage.execute(
            "UPDATE raw_objects SET received_at=? WHERE id=?",
            ((_recent(1) - timedelta(days=3)).isoformat(), raw_id),
        )
        storage.commit()
    else:  # pragma: no cover - closed parameter set above
        raise AssertionError(drift)

    with pytest.raises(V12FileReadError, match="authority changed"):
        await handler.handle(request, turn, plan, preparation)

    assert storage.count_messages(str(conversation["id"]), user_id="alice") == 0


@pytest.mark.asyncio
async def test_two_files_use_two_model_calls_all_citations_and_one_atomic_message_pair(
    settings,
    storage,
) -> None:
    conversation = storage.create_conversation("alice", mode="knowledge_work")
    latest = _registered_text_file(
        storage,
        settings,
        text="LATEST-CONTENT",
        filename="latest.txt",
        received_at=_recent(1),
    )
    older = _registered_text_file(
        storage,
        settings,
        text="OLDER-CONTENT",
        filename="older.txt",
        received_at=_recent(2),
    )
    model = _Model(
        "Новый документ: LATEST [A1]. Старый документ: OLDER [A2].",
        labels=("A1", "A2"),
    )
    handler, _authorization = _handler(storage, settings, model)
    request, turn, plan = _request(
        "Обобщи последние 2 файла",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
    )

    preparation = await handler.prepare(request, turn, plan)
    assert preparation is not None
    result = await handler.handle(request, turn, plan, preparation)

    assert result.citation_labels == ("A1", "A2")
    assert result.verified is True
    assert result.interaction_mode == "knowledge_work"
    assert len(model.calls) == 2
    assert [call["max_tokens"] for call in model.calls] == [512, 256]
    assert all(call.get("tools") is None for call in model.calls)
    synthesis = json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert "LATEST-CONTENT" in synthesis and "OLDER-CONTENT" in synthesis
    messages = storage.get_conversation_messages(str(conversation["id"]), user_id="alice")
    assert [(row["role"], row["content"]) for row in messages] == [
        ("user", "Обобщи последние 2 файла"),
        ("assistant", result.message),
    ]
    request_metadata = json.loads(messages[0]["metadata_json"])
    assert "conversation_uploaded_raw_ids" not in request_metadata
    assert request_metadata["conversation_attachment_raw_ids"] == [latest, older]
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["conversation_attachment_raw_ids"] == [latest, older]
    assert metadata["verified"] is True
