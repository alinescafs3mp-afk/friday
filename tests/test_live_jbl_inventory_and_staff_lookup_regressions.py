"""Closed regressions for the 2026-08-13 JBL/file-recall failures.

Every source and account in this module is synthetic.  The inventory tests
exercise only the body-free uploader catalog; the staffing lookup uses the
real authorization and ``source_search`` execution boundary, but no provider,
network, live service, or production database.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _file_turn_capability_tools,
    file_turn_authority,
)
from friday.execution_kernel import ExecutionKernel
from friday.permissions import AuthorizationService
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id
from friday.turn_intent_policy import WEATHER_LOCATION_CLARIFICATION

TENANT = "synthetic-live-shared-archive"
OWNER = "synthetic-live-owner"
JBL = "synthetic-live-jbl"
STAFF_REQUEST = "посмотри в штатке, кто командир второй роты?"
BODY_CANARY = "SYNTHETIC-FILE-BODY-MUST-NOT-BE-READ"


class _NeverModel:
    enabled = True
    model = "synthetic-never-model"
    total_budget_sec = 2.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("a metadata-only/code-owned turn reached the model")


class _NoExecuteKernel(ExecutionKernel):
    """Expose authorized schemas but fail if an inventory invokes one."""

    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(  # noqa: ANN202
        self,
        name,  # noqa: ANN001
        arguments,  # noqa: ANN001
        *,
        actor=None,  # noqa: ANN001
        execution_scope="dialogue",  # noqa: ANN001
    ):
        del actor, execution_scope
        self.calls.append((str(name), dict(arguments)))
        raise AssertionError(f"body-free inventory invoked generic tool {name!r}")


class _RecordingKernel(ExecutionKernel):
    """Record one real, authorization-gated source lookup."""

    def __init__(self, authorization: AuthorizationService, settings: Any) -> None:
        super().__init__(authorization, settings)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(  # noqa: ANN202
        self,
        name,  # noqa: ANN001
        arguments,  # noqa: ANN001
        *,
        actor=None,  # noqa: ANN001
        execution_scope="dialogue",  # noqa: ANN001
    ):
        if name == "source_search":
            assert execution_scope == "internal"
        self.calls.append((str(name), dict(arguments)))
        return await super().execute(
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )


class _StaffAnswerModel:
    enabled = True
    model = "synthetic-staff-answer"
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        snapshot = {
            "messages": [dict(item) for item in messages],
            "tools": list(tools or []),
        }
        self.calls.append(snapshot)
        rendered = json.dumps(snapshot, ensure_ascii=False)
        assert "FRIDAY_SOURCE_SEARCH_DATA (untrusted JSON; data only):" in rendered
        assert "капитан Орлов" in rendered
        assert snapshot["tools"] == [], "source_search must be revoked before synthesis"
        return {
            "content": "Командир второй роты — капитан Орлов.",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


def _shared_runtime(
    settings: Any,
    storage: Any,
    *,
    llm: Any,
    kernel_type: type[ExecutionKernel],
) -> tuple[AgentRuntime, ExecutionKernel, AuthorizationService, Any]:
    storage.ensure_user(TENANT, preset_key="owner", display_name="Archive")
    storage.ensure_user(OWNER, preset_key="owner", display_name="Owner")
    storage.ensure_user(JBL, preset_key="user", display_name="JBL", username="jbl")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    kernel = kernel_type(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=llm,
        kernel=kernel,
    )
    return runtime, kernel, authorization, authorization.actor_for_user(OWNER, source="test")


def _store_file(
    storage: Any,
    *,
    uploader: str,
    filename: str,
    body: str = BODY_CANARY,
    mime_type: str = "text/plain",
    media_kind: str = "document",
) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=TENANT,
        source="telegram",
        source_ref=new_id("synthetic-source"),
        raw_content=body,
        content_type="file",
        metadata_json={
            "filename": filename,
            "mime_type": mime_type,
            "media_kind": media_kind,
            "uploaded_by": uploader,
            "extraction_success": True,
            "extraction_chars": len(body),
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=TENANT,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )
    return raw.id


def test_body_free_uploader_catalog_filters_lifecycle_media_and_ambiguous_metadata(
    storage: Any,
) -> None:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(JBL, preset_key="user")
    storage.ensure_user(OWNER, preset_key="owner")
    valid = _store_file(storage, uploader=JBL, filename="visible-jbl.odt")
    ignored = _store_file(storage, uploader=JBL, filename="ignored-jbl.odt")
    audio = _store_file(
        storage,
        uploader=JBL,
        filename="voice-jbl.ogg",
        mime_type="audio/ogg",
        media_kind="voice",
    )
    deleted = _store_file(storage, uploader=JBL, filename="deleted-jbl.odt")
    wrong = _store_file(storage, uploader=OWNER, filename="owner-only.odt")
    duplicate = _store_file(storage, uploader=JBL, filename="duplicate-jbl.odt")
    malformed = _store_file(storage, uploader=JBL, filename="malformed-jbl.odt")
    inactive_user = "synthetic-live-inactive-jbl"
    storage.ensure_user(inactive_user, preset_key="user")
    inactive = _store_file(storage, uploader=inactive_user, filename="inactive-jbl.odt")
    ignored_row = storage.get_inbox_by_raw(ignored, TENANT)
    assert ignored_row is not None
    assert storage.update_inbox_status(
        str(ignored_row["id"]),
        InboxStatus.IGNORED,
        reviewed_by=OWNER,
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (deleted,))
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            ('{"uploaded_by":"synthetic-live-jbl","uploaded_by":"synthetic-live-owner"}', duplicate),
        )
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            ('{"uploaded_by":', malformed),
        )
        conn.execute("UPDATE users SET status='disabled' WHERE id=?", (inactive_user,))

    rows = storage.list_owned_file_catalog(TENANT, JBL)

    assert [str(row["id"]) for row in rows] == [valid]
    encoded = json.dumps(rows, ensure_ascii=False)
    for hidden in (ignored, audio, deleted, wrong, duplicate, malformed, inactive):
        assert hidden not in encoded
    assert storage.list_owned_file_catalog(TENANT, inactive_user) == []


def _record_attachment_backed_7969_turn(storage: Any, raw_id: str) -> str:
    conversation = storage.create_conversation(OWNER, title="synthetic JBL regression")
    lineage = {
        "conversation_attachment_raw_ids": [raw_id],
        "conversation_attachment_uploaders": {raw_id: JBL},
    }
    storage.store_message(
        str(conversation["id"]),
        OWNER,
        "user",
        "JBL скидывал тебе файл 7969.odt, о чём он?",
        metadata={"private_context_lineage": True, **lineage},
    )
    storage.store_message(
        str(conversation["id"]),
        OWNER,
        "assistant",
        "Синтетическая сводка файла 7969.odt.",
        metadata={"attachment_context_used": True, **lineage},
    )
    return str(conversation["id"])


def _guard_inventory_body_paths(
    runtime: AgentRuntime,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, int]]:
    """Allow only the body-free catalog and trap every generic/body path."""

    catalog_calls: list[tuple[str, str, int]] = []
    original_catalog = storage.list_owned_file_catalog

    def body_free_catalog(
        tenant_id: str,
        uploaded_by: str,
        *,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        catalog_calls.append((tenant_id, uploaded_by, limit))
        rows = original_catalog(tenant_id, uploaded_by, limit=limit)
        assert all(set(row).issubset({"id", "content_type", "received_at", "filename"}) for row in rows)
        assert BODY_CANARY not in json.dumps(rows, ensure_ascii=False)
        return rows

    def forbidden_sync(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("metadata-only inventory restored or hydrated a file body")

    async def forbidden_async(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("metadata-only inventory entered generic context/model generation")

    monkeypatch.setattr(storage, "list_owned_file_catalog", body_free_catalog)
    monkeypatch.setattr(storage, "get_searchable_file_sources", forbidden_sync)
    monkeypatch.setattr(storage, "search_raw_objects_in_set", forbidden_sync)
    monkeypatch.setattr(runtime, "_owned_file_attachment", forbidden_sync)
    monkeypatch.setattr(runtime, "_restore_conversation_attachments", forbidden_sync)
    monkeypatch.setattr(runtime, "_hydrate_legacy_document_metadata", forbidden_async)
    monkeypatch.setattr(runtime, "_prepare_context", forbidden_async)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden_async)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_async)
    return catalog_calls


def _assert_closed_inventory_reply(reply: dict[str, Any], *filenames: str) -> None:
    for filename in filenames:
        assert filename in reply["message"]
    assert BODY_CANARY not in reply["message"]
    assert reply["tools_used"] == []
    assert reply["attachment_context_expected_count"] == 0
    assert reply["attachment_context_readable_count"] == 0


@pytest.mark.asyncio
async def test_live_exact_jbl_file_inventory_is_body_free_and_code_owned(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="7969.odt")
    _store_file(storage, uploader=JBL, filename="jbl-orders.pdf")
    _store_file(storage, uploader="synthetic-live-owner", filename="owner-decoy.txt")
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(
        actor.own_id,
        "какие файлы скидывал JBL?",
        actor=actor,
        enable_tools=True,
    )

    _assert_closed_inventory_reply(reply, "7969.odt", "jbl-orders.pdf")
    assert "owner-decoy.txt" not in reply["message"]
    assert catalog_calls == [(TENANT, JBL, 5_001)]
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    stored = storage.get_message(str(reply["message_id"]), OWNER)
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["person_document_inventory"] is True
    assert metadata["private_context_lineage"] is True
    user_rows = [
        row
        for row in storage.get_conversation_messages(str(reply["conversation_id"]), user_id=OWNER, limit=20)
        if str(row.get("role") or "") == "user"
    ]
    assert user_rows
    user_metadata = json.loads(str(user_rows[-1]["metadata_json"] or "{}"))
    assert user_metadata["private_context_lineage"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Не показывай файлы, которые скидывал JBL.",
        "Пожалуйста, не перечисляй файлы, которые загружал JBL.",
        "Файлы, которые присылал JBL, не показывай.",
        "Я просил не показывать документы, которые отправлял JBL.",
        "Не хочу видеть вложения, которые добавлял JBL.",
        "Не нужно сейчас перечислять файлы, которые скидывал JBL.",
        "Не надо мне показывать файлы, которые скидывал JBL.",
        "Я не хочу, чтобы ты показывала файлы, которые скидывал JBL.",
        "Давай не будем показывать файлы, которые скидывал JBL.",
        "Можешь не показывать файлы, которые скидывал JBL.",
    ],
)
async def test_negated_jbl_inventory_is_a_closed_no_read_command(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="must-not-be-disclosed.pdf")
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(actor.own_id, message, actor=actor, enable_tools=True)

    assert catalog_calls == []
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    assert "must-not-be-disclosed.pdf" not in reply["message"]
    assert "не буду" in reply["message"].casefold()
    assert reply["tools_used"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Ты не показывал файлы, которые скидывал JBL?",
        "Почему ты не показываешь файлы, которые скидывал JBL?",
        "Почему не нужно сейчас перечислять файлы, которые скидывал JBL?",
        "Ты правда просил не показывать файлы, которые скидывал JBL?",
    ],
)
async def test_descriptive_inventory_negation_is_not_rewritten_as_a_new_prohibition(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )

    async def prepared(user_id, question, conversation_id, **kwargs):  # noqa: ANN001
        del kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            search_query=question,
            answer_mode="general_conversation",
        )

    async def generate(context, question, attachments):  # noqa: ANN001
        del context, attachments
        assert question == message
        return {
            "content": "Это вопрос о прошлом показе файлов, а не новая команда скрыть их.",
            "tools_used": [],
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepared)
    monkeypatch.setattr(runtime, "_generate_response", generate)

    reply = await runtime.chat(actor.own_id, message, actor=actor, enable_tools=False)

    assert "вопрос о прошлом показе" in reply["message"]
    assert "Хорошо, не буду" not in reply["message"]
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Какие файлы скидывал JBL? А потом скажи погоду.",
        "Какие файлы скидывал JBL, и какая сегодня погода?",
        "Какая сегодня погода, и какие файлы скидывал JBL?",
        "Скажи погоду, а потом какие файлы скидывал JBL?",
    ],
)
async def test_jbl_inventory_does_not_swallow_an_independent_weather_clause(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="jbl-visible.pdf")
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(actor.own_id, message, actor=actor, enable_tools=True)

    assert "jbl-visible.pdf" in reply["message"]
    assert WEATHER_LOCATION_CLARIFICATION in reply["message"]
    assert catalog_calls == [(TENANT, JBL, 5_001)]
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    assert reply["tools_used"] == []


@pytest.mark.asyncio
async def test_live_other_jbl_files_after_7969_does_not_restore_that_attachment(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    raw_7969 = _store_file(storage, uploader=JBL, filename="7969.odt")
    _store_file(storage, uploader=JBL, filename="jbl-roster.xlsx")
    conversation_id = _record_attachment_backed_7969_turn(storage, raw_7969)
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(
        actor.own_id,
        "а какие ещё файлы JBL скидывал?",
        actor=actor,
        conversation_id=conversation_id,
        enable_tools=True,
    )

    _assert_closed_inventory_reply(reply, "7969.odt", "jbl-roster.xlsx")
    assert catalog_calls == [(TENANT, JBL, 5_001)]
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []


@pytest.mark.asyncio
async def test_unique_approximate_sender_resolves_but_an_ambiguous_one_fails_closed(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="jbl-unique.pdf")
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    unique = await runtime.chat(
        actor.own_id,
        "какие файлы скидывал GBL?",
        actor=actor,
        enable_tools=True,
    )

    _assert_closed_inventory_reply(unique, "jbl-unique.pdf")
    assert catalog_calls == [(TENANT, JBL, 5_001)]

    storage.ensure_user(
        "synthetic-live-hbl",
        preset_key="user",
        display_name="HBL",
        username="hbl",
    )
    _store_file(storage, uploader="synthetic-live-hbl", filename="hbl-private.pdf")
    catalog_calls.clear()
    ambiguous = await runtime.chat(
        actor.own_id,
        "какие файлы скидывал GBL?",
        actor=actor,
        enable_tools=True,
    )

    assert catalog_calls == []
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    assert "jbl-unique.pdf" not in ambiguous["message"]
    assert "hbl-private.pdf" not in ambiguous["message"]
    assert BODY_CANARY not in ambiguous["message"]
    assert ambiguous["tools_used"] == []
    assert any(marker in ambiguous["message"].casefold() for marker in ("однознач", "неизвест", "уточн"))


@pytest.mark.asyncio
async def test_cross_account_body_free_inventory_obeys_the_metadata_capability(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, kernel, authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="denied-jbl.pdf")
    authorization.deny_permission(OWNER, "admin.activity.read")
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(
        actor.own_id,
        "какие файлы скидывал JBL?",
        actor=actor,
        enable_tools=True,
    )

    assert catalog_calls == []
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    assert "denied-jbl.pdf" not in reply["message"]
    assert BODY_CANARY not in reply["message"]
    assert reply["tools_used"] == []
    assert any(marker in reply["message"].casefold() for marker in ("недоступ", "неизвест", "нет доступа"))


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["admin.all_data.read", "admin.activity.read"])
async def test_cross_account_inventory_requires_both_metadata_capabilities(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
) -> None:
    runtime, kernel, authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=_NeverModel(),
        kernel_type=_NoExecuteKernel,
    )
    _store_file(storage, uploader=JBL, filename="capability-denied-jbl.pdf")
    authorization.deny_permission(OWNER, denied)
    catalog_calls = _guard_inventory_body_paths(runtime, storage, monkeypatch)

    reply = await runtime.chat(actor.own_id, "какие файлы скидывал JBL?", actor=actor)

    assert catalog_calls == []
    assert isinstance(kernel, _NoExecuteKernel) and kernel.calls == []
    assert "capability-denied-jbl.pdf" not in reply["message"]
    assert reply["tools_used"] == []


@pytest.mark.asyncio
async def test_live_staffing_question_executes_one_authorized_source_search_after_schema_filtering(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _StaffAnswerModel()
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=model,
        kernel_type=_RecordingKernel,
    )
    _store_file(
        storage,
        uploader=OWNER,
        filename="synthetic-staffing.odt",
        body="2-я РОТА | командир | капитан Орлов",
    )

    authority = file_turn_authority(STAFF_REQUEST)
    assert authority.proved("local_read")
    authorized_schemas = kernel.get_tool_definitions(actor, topic="файл")
    assert any(
        str((item.get("function") or {}).get("name") or "") == "source_search" for item in authorized_schemas
    )
    model_visible_schemas = _file_turn_capability_tools(authorized_schemas, authority)
    assert all(
        str((item.get("function") or {}).get("name") or "") != "source_search"
        for item in model_visible_schemas
    )

    async def forbidden_context(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("explicit staffing lookup entered ambient context retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    reply = await runtime.chat(
        actor.own_id,
        STAFF_REQUEST,
        actor=actor,
        enable_tools=True,
    )

    assert isinstance(kernel, _RecordingKernel)
    assert kernel.calls == [
        (
            "source_search",
            {"query": "2-я рота", "focus": "2-я рота командир рота", "limit": 10},
        )
    ]
    assert reply["tools_used"] == ["source_search"]
    assert "капитан Орлов" in reply["message"]
    assert "локальный поиск недоступен" not in reply["message"].casefold()
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_1342_staffing_lookup_never_restores_an_unrelated_stale_attachment(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact live wording is an archive lookup, not a latest-file follow-up."""

    model = _StaffAnswerModel()
    runtime, kernel, _authorization, actor = _shared_runtime(
        settings,
        storage,
        llm=model,
        kernel_type=_RecordingKernel,
    )
    _store_file(
        storage,
        uploader=OWNER,
        filename="synthetic-staffing.odt",
        body="2-я РОТА | командир | капитан Орлов",
    )
    stale_raw_id = _store_file(
        storage,
        uploader=JBL,
        filename="7849.odt",
        body="STALE-ATTACHMENT-BODY-MUST-STAY-OUT",
    )
    conversation_id = _record_attachment_backed_7969_turn(storage, stale_raw_id)
    storage.store_message(
        conversation_id,
        OWNER,
        "user",
        "STALE-HISTORY-QUESTION-MUST-STAY-OUT",
        metadata={"private_context_lineage": True},
    )
    storage.store_message(
        conversation_id,
        OWNER,
        "assistant",
        "STALE-HISTORY-ANSWER-MUST-STAY-OUT",
        metadata={"private_context_lineage": True},
    )

    async def forbidden_context(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("exact staffing lookup entered ambient context retrieval")

    def forbidden_restore(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("exact staffing lookup attempted latest-attachment restoration")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_context)
    monkeypatch.setattr(runtime, "_restore_conversation_attachments", forbidden_restore)

    reply = await runtime.chat(
        actor.own_id,
        STAFF_REQUEST,
        actor=actor,
        conversation_id=conversation_id,
        enable_tools=True,
    )

    assert isinstance(kernel, _RecordingKernel)
    assert kernel.calls == [
        (
            "source_search",
            {"query": "2-я рота", "focus": "2-я рота командир рота", "limit": 10},
        )
    ]
    assert reply["tools_used"] == ["source_search"]
    assert reply["restored_attachment_count"] == 0
    assert "Командир второй роты — капитан Орлов." in reply["message"]
    rendered = json.dumps([reply, model.calls], ensure_ascii=False)
    assert "STALE-ATTACHMENT-BODY-MUST-STAY-OUT" not in rendered
    assert "STALE-HISTORY-QUESTION-MUST-STAY-OUT" not in rendered
    assert "STALE-HISTORY-ANSWER-MUST-STAY-OUT" not in rendered
    assert "защитная проверка отклонила" not in reply["message"].casefold()
