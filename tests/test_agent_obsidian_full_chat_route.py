"""Full-chat regression for the live Telegram Obsidian create request."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ToolResult
from friday.permissions import ActorContext

_LIVE_PROMPT = (
    "Создай в Obsidian заметку Projects/Friday Test.md. "
    "Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана "
    "через Telegram, и добавь текущую дату."
)
_REVISION = "b" * 64


def _schema(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic full-chat capability",
            "parameters": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        },
    }


class _NoModel:
    enabled = False
    total_budget_sec = 1.0

    async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("the deterministic live Obsidian request reached the model")


class _MutableAuthorization:
    def __init__(self) -> None:
        self.denied: set[str] = set()

    def authorize(self, actor, capability):  # noqa: ANN001, ARG002
        return SimpleNamespace(allowed=str(capability) not in self.denied)


class _FullChatKernel:
    kg = None

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.authorization = _MutableAuthorization()

    @staticmethod
    def get_tool_definitions(actor, topic=""):  # noqa: ANN001, ARG004
        # Advertising make_file reproduces the collision which caused a path
        # ending in .md to be routed to the generic Telegram file builder.
        return [
            _schema("obsidian_list_vaults", {}),
            _schema(
                "obsidian_create_note",
                {
                    "operation_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            ),
            _schema(
                "make_file",
                {
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
            ),
        ]

    @staticmethod
    def get_tool(name: str) -> Any:
        if name == "obsidian_list_vaults":
            return SimpleNamespace(security_id="obsidian.read", risk="observe")
        if name == "obsidian_create_note":
            return SimpleNamespace(security_id="obsidian.write", risk="mutate")
        if name == "make_file":
            return SimpleNamespace(security_id="file.write", risk="mutate")
        return None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        arguments = dict(arguments)
        self.executed.append((str(name), arguments))
        if name == "obsidian_list_vaults":
            return ToolResult(
                str(name),
                True,
                data={
                    "vaults": [
                        {
                            "id": "obsvault_0123456789abcdef",
                            "name": "Friday",
                            "state": "ready",
                            "android_alias": "Friday",
                        }
                    ],
                    "count": 1,
                },
            )
        if name == "obsidian_create_note":
            return ToolResult(
                str(name),
                True,
                data={
                    "operation_id": arguments["operation_id"],
                    "method": "create",
                    "status": "delivered",
                    "path": arguments["path"],
                    "revision": _REVISION,
                    "previous_revision": None,
                    "created": True,
                    "applied": True,
                    "replayed": False,
                    "delivery": {
                        "local_write_complete": True,
                        "server_scan_complete": True,
                        "android_connected": True,
                        "android_completion": 100.0,
                        "android_received": True,
                        "obsidian_opened": False,
                    },
                },
            )
        raise AssertionError(f"unexpected full-chat tool call: {name} {arguments}")


class _FullChatReadKernel(_FullChatKernel):
    @staticmethod
    def get_tool_definitions(actor, topic=""):  # noqa: ANN001, ARG004
        return [
            _schema("obsidian_list_vaults", {}),
            _schema("obsidian_read_note", {"path": {"type": "string"}}),
            _schema(
                "make_file",
                {
                    "kind": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
            ),
        ]

    @staticmethod
    def get_tool(name: str) -> Any:
        if name in {"obsidian_list_vaults", "obsidian_read_note"}:
            return SimpleNamespace(security_id="obsidian.read", risk="observe")
        if name == "make_file":
            return SimpleNamespace(security_id="file.write", risk="mutate")
        return None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        arguments = dict(arguments)
        self.executed.append((str(name), arguments))
        if name == "obsidian_list_vaults":
            return ToolResult(
                str(name),
                True,
                data={
                    "vaults": [
                        {
                            "id": "obsvault_0123456789abcdef",
                            "name": "Friday",
                            "state": "ready",
                            "android_alias": "Friday",
                        }
                    ],
                    "count": 1,
                },
            )
        if name == "obsidian_read_note":
            body = "# Literal\n\nСсылка [K1] и https://example.test остаются текстом заметки.\n"
            return ToolResult(
                str(name),
                True,
                data={
                    "path": arguments["path"],
                    "title": "Literal",
                    "content": body,
                    "body": body,
                    "properties": {},
                    "revision": _REVISION,
                    "size_bytes": len(body.encode("utf-8")),
                    "modified_at": "2026-08-22T09:00:00+03:00",
                },
            )
        raise AssertionError(f"unexpected full-chat read call: {name} {arguments}")


class _RevokeAfterReadKernel(_FullChatReadKernel):
    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        result = await super().execute(name, arguments, actor=actor)
        if name == "obsidian_read_note":
            self.authorization.denied.add("obsidian.read")
        return result


class _RevokeAfterWriteKernel(_FullChatKernel):
    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        result = await super().execute(name, arguments, actor=actor)
        if name == "obsidian_create_note":
            self.authorization.denied.add("obsidian.write")
        return result


class _DeactivateAfterWriteKernel(_FullChatKernel):
    def __init__(self, storage) -> None:  # noqa: ANN001
        super().__init__()
        self.storage = storage

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        result = await super().execute(name, arguments, actor=actor)
        if name == "obsidian_create_note":
            self.storage.update_user("alice", status="disabled")
        return result


@pytest.mark.asyncio
async def test_live_create_prompt_uses_one_obsidian_mutation_and_never_make_file(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _FullChatKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    async def forbidden_general_context(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("deterministic Obsidian command entered general retrieval/arbiters")

    runtime._prepare_context = forbidden_general_context  # type: ignore[method-assign]

    reply = await runtime.chat(
        "alice",
        _LIVE_PROMPT,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    names = [name for name, _arguments in kernel.executed]
    assert names == ["obsidian_list_vaults", "obsidian_create_note"]
    assert names.count("obsidian_create_note") == 1
    assert "make_file" not in names
    create = kernel.executed[1][1]
    assert create["path"] == "Projects/Friday Test.md"
    assert create["content"] == (
        "# Тест интеграции Friday\n\nЗаметка создана через Telegram.\n\n2026-08-22\n"
    )
    assert create["operation_id"].startswith("obsop_")
    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    assert reply["message_format"] == "plain"
    assert reply["message"].startswith("Заметка создана в локальной серверной копии vault.")
    assert "Путь: Projects/Friday Test.md" in reply["message"]
    assert "Получение этой revision на Android: подтверждено." in reply["message"]
    assert reply["files"] == []
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["private_context_lineage"] is True
    assert metadata["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["structural"]["verdict_kind"] == "obsidian"
    assert metadata["structural"]["obsidian_owned"] is True
    assert metadata["structural"]["remainder_known"] is True
    assert not metadata["structural"].get("output_guards", {}).get("supported_deed_replaced")


@pytest.mark.asyncio
async def test_owned_obsidian_read_survives_model_only_citation_and_url_guards(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _FullChatReadKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    reply = await runtime.chat(
        "alice",
        "Прочитай в Obsidian заметку `Projects/Literal.md`.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _arguments in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_read_note",
    ]
    assert reply["message_format"] == "plain"
    assert "Ссылка [K1] и https://example.test остаются текстом заметки." in reply["message"]
    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_read_note"]
    assert reply["files"] == []


@pytest.mark.asyncio
async def test_read_result_is_hidden_when_capability_is_revoked_during_call(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _RevokeAfterReadKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    reply = await runtime.chat(
        "alice",
        "Прочитай в Obsidian заметку `Projects/Literal.md`.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _arguments in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_read_note",
    ]
    assert reply["obsidian_authority_changed_before_publication"] is True
    assert reply["verification"]["issues"] == ["obsidian_authority_changed_before_publication"]
    assert "Ссылка [K1]" not in reply["message"]
    assert "https://example.test" not in reply["message"]
    assert "отозвано" in reply["message"]
    assert "не показан" in reply["message"]
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert str(stored["content"]) == reply["message"]
    assert "Ссылка [K1]" not in str(stored["metadata_json"])
    assert "https://example.test" not in str(stored["metadata_json"])


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["capability", "principal"])
async def test_write_receipt_is_hidden_when_authority_is_revoked_during_call(
    settings,
    storage,
    revocation: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _RevokeAfterWriteKernel() if revocation == "capability" else _DeactivateAfterWriteKernel(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    reply = await runtime.chat(
        "alice",
        _LIVE_PROMPT,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    names = [name for name, _arguments in kernel.executed]
    assert names == ["obsidian_list_vaults", "obsidian_create_note"]
    assert names.count("obsidian_create_note") == 1
    assert reply["obsidian_authority_changed_before_publication"] is True
    assert reply["verification"]["issues"] == ["obsidian_authority_changed_before_publication"]
    assert "Заметка создана" not in reply["message"]
    assert "Путь:" not in reply["message"]
    assert _REVISION not in reply["message"]
    assert "Не могу подтвердить" in reply["message"]
    assert "автоматически его не повторяю" in reply["message"]
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert str(stored["content"]) == reply["message"]


@pytest.mark.asyncio
async def test_regenerate_after_midnight_reuses_the_root_date_and_operation_id(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _FullChatKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    first = await runtime.chat("alice", _LIVE_PROMPT, actor=actor)
    rows = storage.get_conversation_messages(str(first["conversation_id"]), user_id="alice")
    root = next(row for row in rows if str(row.get("role") or "") == "user")
    root_metadata = json.loads(str(root["metadata_json"] or "{}"))
    assert root_metadata["obsidian_effect_local_date"] == "2026-08-22"

    runtime._local_today = lambda: date(2026, 8, 23)  # type: ignore[method-assign]
    await runtime.chat(
        "alice",
        _LIVE_PROMPT,
        actor=actor,
        conversation_id=str(first["conversation_id"]),
        replay_source_message_id=str(root["id"]),
    )

    creates = [arguments for name, arguments in kernel.executed if name == "obsidian_create_note"]
    assert len(creates) == 2
    assert creates[0]["operation_id"] == creates[1]["operation_id"]
    assert creates[0]["content"] == creates[1]["content"]
    assert creates[1]["content"].endswith("2026-08-22\n")
    replay_rows = storage.get_conversation_messages(str(first["conversation_id"]), user_id="alice")
    replay_user = [row for row in replay_rows if str(row.get("role") or "") == "user"][-1]
    replay_metadata = json.loads(str(replay_user["metadata_json"] or "{}"))
    assert replay_metadata["obsidian_effect_local_date"] == "2026-08-22"
    assert replay_metadata["regenerate_root_user_message_id"] == root["id"]


@pytest.mark.asyncio
async def test_obsidian_note_bytes_never_enter_tts_before_publication_reauth(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _FullChatReadKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    async def forbidden_voice(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Obsidian note bytes reached TTS before final reauthorization")

    runtime._voice_of_the_final_answer = forbidden_voice  # type: ignore[method-assign]
    reply = await runtime.chat(
        "alice",
        "Прочитай в Obsidian заметку `Projects/Literal.md`.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        answer_with_voice=True,
    )

    assert "Ссылка [K1]" in reply["message"]
    assert reply.get("voice") is None
