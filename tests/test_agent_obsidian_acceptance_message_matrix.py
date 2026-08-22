"""Full-chat routing matrix for the Tier A/B Obsidian acceptance messages.

The onboarding ``/obsidian`` command is owned by the Telegram command bridge and
does not enter :meth:`AgentRuntime.chat`; scenarios 1 through 18 do.  Companion
or foreground-plugin messages are deliberately outside this matrix.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ToolResult
from friday.organs.obsidian.workflow_intents import WORKFLOW_READ_TOOL, WORKFLOW_WRITE_TOOL
from friday.permissions import ActorContext

_BATTERY_TEXT = (
    Path(__file__).parents[1] / "outer_sol" / "OBSIDIAN_INTEGRATION_ACCEPTANCE_BATTERY.md"
).read_text(encoding="utf-8")
_REVISION = "b" * 64
_PREVIOUS_REVISION = "a" * 64

_NOTE_01 = (
    "Создай в Obsidian заметку `Projects/Friday Test.md`. Заголовок: «Тест интеграции "
    "Friday». Внутри напиши, что заметка создана через Telegram, и добавь текущую дату."
)
_NOTE_02 = (
    "Добавь в конец заметки `Projects/Friday Test.md` раздел «Проверка дополнения» "
    "и одну строку: «Этот текст был добавлен отдельной командой»."
)
_DAILY_01 = (
    "Добавь в сегодняшнюю ежедневную заметку раздел «Friday» и пункт: «Проверена интеграция с Obsidian»."
)
_TASK_01_ADD = "Добавь в сегодняшнюю заметку задачу проверить поиск в Obsidian завтра в 10 утра."
_TASK_01_QUERY = "Покажи незавершённые задачи про Obsidian."
_META_01 = (
    "У заметки `Projects/Friday Test.md` поставь статус `review`, проект `Friday` "
    "и добавь теги `integration`, `obsidian` и `test`."
)
_SEARCH_01 = (
    "Найди в Obsidian заметку, где мы обсуждали, что старые файлы не попадали в поиск "
    "из-за слишком маленького списка кандидатов."
)
_SEARCH_02 = "Найди заметку про проблемы поиска, которую я делал примерно в начале августа 2026 года."
_CONT_01_SEARCH = "Найди все заметки про Friday и поиск."
_CONT_01_SELECT = "Открой вторую."
_CONT_01_APPEND = "Добавь туда раздел «Следующие шаги» и пункт про проверку семантического индекса."
_SYNC_01 = "Найди заметку про фиолетовый маршрутизатор."
_LINK_01 = "Какие заметки ссылаются на `Projects/Friday`?"
_MOVE_01 = "Перемести `Projects/Friday.md` в `Architecture/Friday.md` и обнови ссылки на неё."
_MOVE_01_BACKLINKS = "Какие заметки теперь ссылаются на архитектуру Friday?"
_TEMPLATE_01 = (
    "Создай по шаблону Meeting заметку о проверке интеграции Obsidian. Проект Friday, "
    "участники Алиса и Борис. В обсуждение добавь, что базовая синхронизация работает. "
    "В действия добавь задачу проверить конфликты."
)
_WORK_01 = (
    "Сохрани краткие итоги нашего текущего разговора в Obsidian. Создай заметку "
    "`Research/Conversation Summary.md`, отдельно укажи выводы, нерешённые вопросы "
    "и следующие действия."
)
_WORK_01_LINKS = "Добавь туда ссылки на заметки, которые мы сегодня использовали."
_BASE_01 = (
    "Создай Base `Friday Active Notes`, который показывает заметки проекта Friday "
    "со статусом не `done`. Выведи название, статус и дату изменения."
)
_OFFLINE_01 = (
    "Создай заметку `Offline/Pending Delivery.md` и напиши, что она была создана, пока телефон был offline."
)
_CONFLICT_01_REPLACE = "Замени раздел «Проверка дополнения» текстом: «Версия, записанная Friday»."
_CONFLICT_01_PREVIEW = "Покажи различия и собери объединённую версию, сохранив оба изменения."
_RECOVERY_01_APPEND = "Добавь в ежедневную заметку строку «Проверка идемпотентности»."
_RECOVERY_01_RESUME = "Продолжай предыдущую задачу."
_DELETE_01 = "Удали тестовую заметку `Scratch/Delete Me.md`."
_DELETE_01_SEARCH = "Найди заметку Delete Me."


def _schema(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "deterministic Obsidian acceptance routing capability",
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
        raise AssertionError("an exact Obsidian acceptance message reached the model")


class _AllowAllAuthorization:
    @staticmethod
    def authorize(actor, capability):  # noqa: ANN001, ARG004
        return SimpleNamespace(allowed=True)


class _AcceptanceRoutingKernel:
    """Validate the expected route before returning one contract-valid receipt."""

    kg = None
    _workflow_fields = {
        name: {"type": "string"}
        for name in (
            "operation_id",
            "action",
            "query",
            "path",
            "source_path",
            "destination_path",
            "day",
            "due_date",
            "due_time",
            "text",
            "section",
            "item",
            "template_name",
            "title",
            "project",
            "status",
            "discussion",
            "actions",
            "name",
            "excluded_status",
            "target_path",
        )
    }
    _workflow_fields.update(
        {
            "incomplete_only": {"type": "boolean"},
            "ordinal": {"type": "integer"},
            "update_links": {"type": "boolean"},
            "tags": {"type": "array"},
            "participants": {"type": "array"},
            "columns": {"type": "array"},
        }
    )

    def __init__(self, expected_steps: tuple[tuple[str, str | None], ...]) -> None:
        self.expected_steps = expected_steps
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.target_calls: list[tuple[str, dict[str, Any]]] = []
        self.target_sessions: list[str] = []
        self.authorization = _AllowAllAuthorization()
        self._searched_sessions: set[str] = set()
        self._selected_sessions: set[str] = set()

    @classmethod
    def get_tool_definitions(cls, actor, topic=""):  # noqa: ANN001, ARG003
        operation = {"operation_id": {"type": "string"}}
        path = {"path": {"type": "string"}}
        return [
            _schema("obsidian_list_vaults", {}),
            _schema(
                "obsidian_search_notes",
                {"query": {"type": "string"}, "limit": {"type": "integer"}},
            ),
            _schema(
                "obsidian_create_note",
                {
                    **operation,
                    **path,
                    "content": {"type": "string"},
                    "properties": {"type": "object"},
                    "work_item_id": {"type": "string"},
                },
            ),
            _schema(
                "obsidian_append_note",
                {
                    **operation,
                    **path,
                    "text": {"type": "string"},
                    "expected_revision": {"type": "string"},
                    "work_item_id": {"type": "string"},
                },
            ),
            _schema(
                "obsidian_daily_note",
                {
                    **operation,
                    "day": {"type": "string"},
                    "content": {"type": "string"},
                    "section": {"type": "string"},
                    "item": {"type": "string"},
                    "expected_revision": {"type": "string"},
                    "work_item_id": {"type": "string"},
                },
            ),
            _schema(WORKFLOW_READ_TOOL, cls._workflow_fields),
            _schema(WORKFLOW_WRITE_TOOL, cls._workflow_fields),
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
        if name in {"obsidian_list_vaults", "obsidian_search_notes", WORKFLOW_READ_TOOL}:
            return SimpleNamespace(security_id="obsidian.read", risk="observe")
        if name in {
            "obsidian_create_note",
            "obsidian_append_note",
            "obsidian_daily_note",
            WORKFLOW_WRITE_TOOL,
        }:
            return SimpleNamespace(security_id="obsidian.write", risk="mutate")
        if name == "make_file":
            return SimpleNamespace(security_id="file.write", risk="mutate")
        return None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        tool_name = str(name)
        payload = dict(arguments)
        self.executed.append((tool_name, payload))
        if tool_name == "obsidian_list_vaults":
            return self._vault_receipt()

        index = len(self.target_calls)
        assert index < len(self.expected_steps), f"unexpected extra target call: {tool_name} {payload}"
        expected_tool, expected_action = self.expected_steps[index]
        assert tool_name == expected_tool
        if expected_action is None:
            assert "action" not in payload
        else:
            assert payload.get("action") == expected_action

        session = str(getattr(actor, "session_id", "") or "")
        assert session, "Obsidian target call lost its conversation session"
        self.target_calls.append((tool_name, payload))
        self.target_sessions.append(session)
        self._assert_continuation_order(tool_name, payload, session)

        if tool_name == "obsidian_search_notes":
            return self._search_receipt(payload)
        if tool_name in {"obsidian_create_note", "obsidian_append_note", "obsidian_daily_note"}:
            return self._mutation_receipt(tool_name, payload)
        return self._workflow_receipt(tool_name, payload)

    def assert_complete(self) -> None:
        assert len(self.target_calls) == len(self.expected_steps)
        assert "make_file" not in {name for name, _payload in self.executed}

    def _assert_continuation_order(self, tool_name: str, payload: dict[str, Any], session: str) -> None:
        if tool_name == "obsidian_search_notes" and payload.get("query") == "Friday и поиск":
            self._searched_sessions.add(session)
        elif tool_name == WORKFLOW_READ_TOOL and payload.get("action") == "select_candidate":
            assert session in self._searched_sessions
            self._selected_sessions.add(session)
        elif tool_name == WORKFLOW_WRITE_TOOL and payload.get("action") == "append_active_section":
            assert session in self._selected_sessions

    @staticmethod
    def _vault_receipt() -> ToolResult:
        return ToolResult(
            "obsidian_list_vaults",
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

    @staticmethod
    def _delivery() -> dict[str, object]:
        return {
            "local_write_complete": True,
            "server_scan_complete": True,
            "android_connected": True,
            "android_completion": 100.0,
            "android_received": True,
            "obsidian_opened": False,
        }

    @staticmethod
    def _open_uri(path: str) -> str:
        return "obsidian://open?" + urllib.parse.urlencode({"vault": "Friday", "file": path})

    def _search_receipt(self, payload: dict[str, Any]) -> ToolResult:
        query = str(payload["query"])
        if query.casefold() == "delete me":
            rows = [self._match("Scratch/Delete Me.md", "Delete Me", ("tombstone",))]
        elif query == "Friday и поиск":
            rows = [
                self._match("Projects/First.md", "First", ("lexical",)),
                self._match("Projects/Second.md", "Second", ("lexical",)),
            ]
        else:
            rows = [
                self._match(
                    "Projects/Retrieval Problem.md",
                    "Retrieval Problem",
                    ("semantic",),
                )
            ]
        return ToolResult(
            "obsidian_search_notes",
            True,
            data={"matches": rows, "count": len(rows)},
        )

    @staticmethod
    def _match(path: str, title: str, channels: tuple[str, ...]) -> dict[str, object]:
        return {
            "path": path,
            "title": title,
            "revision": _REVISION,
            "modified_at": "2026-08-22T09:00:00+03:00",
            "excerpt": "Детерминированный acceptance-фрагмент.",
            "score": 42.0,
            "match_channels": list(channels),
        }

    def _mutation_receipt(self, tool_name: str, payload: dict[str, Any]) -> ToolResult:
        path = str(payload.get("path") or "Daily/2026-08-22.md")
        method = {
            "obsidian_create_note": "create",
            "obsidian_append_note": "append",
            "obsidian_daily_note": "daily_note",
        }[tool_name]
        return ToolResult(
            tool_name,
            True,
            data={
                "operation_id": payload["operation_id"],
                "method": method,
                "status": "delivered",
                "path": path,
                "revision": _REVISION,
                "previous_revision": _PREVIOUS_REVISION if tool_name == "obsidian_append_note" else None,
                "created": tool_name != "obsidian_append_note",
                "applied": True,
                "replayed": False,
                "open_uri": self._open_uri(path),
                "delivery": self._delivery(),
            },
        )

    def _workflow_receipt(self, tool_name: str, payload: dict[str, Any]) -> ToolResult:
        action = str(payload["action"])
        if tool_name == WORKFLOW_READ_TOOL:
            path = {
                "search_tasks": None,
                "select_candidate": "Projects/Second.md",
                "backlinks": str(payload.get("target_path") or "Architecture/Friday.md"),
                "conflict_preview": "Projects/Friday Test.md",
            }[action]
            changed_paths = (
                ["Notes/Search.md", "Notes/Obsidian.md"]
                if action == "backlinks"
                else []
                if path is None
                else [path]
            )
            data = {
                "action": action,
                "status": "selected"
                if action == "select_candidate"
                else "preview"
                if action == "conflict_preview"
                else "completed",
                "path": path,
                "revision": _REVISION if path else None,
                "operation_id": None,
                "changed_paths": changed_paths,
                "body": f"Deterministic workflow read: {action}",
                "open_uri": None if path is None else self._open_uri(path),
                "delivery": None,
            }
        else:
            path = {
                "add_task": "Daily/2026-08-22.md",
                "update_metadata": str(payload.get("path") or "Projects/Friday Test.md"),
                "append_active_section": "Projects/Second.md",
                "move_note": str(payload.get("destination_path") or "Architecture/Friday.md"),
                "create_from_template": "Meetings/2026-08-22 Проверка интеграции Obsidian.md",
                "save_summary": str(payload.get("path") or "Research/Conversation Summary.md"),
                "append_summary_links": "Research/Conversation Summary.md",
                "create_base": "Bases/Friday Active Notes.base",
                "replace_active_section": "Projects/Friday Test.md",
                "accept_conflict_merge": "Projects/Friday Test.md",
                "resume_previous": "Daily/2026-08-22.md",
                "delete_note": str(payload.get("path") or "Scratch/Delete Me.md"),
            }[action]
            deleted = action == "delete_note"
            data = {
                "action": action,
                "status": "resumed" if action == "resume_previous" else "completed",
                "path": path,
                "revision": None if deleted else _REVISION,
                "operation_id": payload["operation_id"],
                "changed_paths": [path],
                "body": f"Deterministic workflow write: {action}",
                "open_uri": None if deleted or path.endswith(".base") else self._open_uri(path),
                "delivery": self._delivery(),
            }
        return ToolResult(tool_name, True, data=data)


_SINGLE_TURN_CASES = (
    pytest.param(_NOTE_01, "obsidian_create_note", None, id="OBS-NOTE-01"),
    pytest.param(_NOTE_02, "obsidian_append_note", None, id="OBS-NOTE-02"),
    pytest.param(_DAILY_01, "obsidian_daily_note", None, id="OBS-DAILY-01"),
    pytest.param(_TASK_01_ADD, WORKFLOW_WRITE_TOOL, "add_task", id="OBS-TASK-01-add"),
    pytest.param(_TASK_01_QUERY, WORKFLOW_READ_TOOL, "search_tasks", id="OBS-TASK-01-query"),
    pytest.param(_META_01, WORKFLOW_WRITE_TOOL, "update_metadata", id="OBS-META-01"),
    pytest.param(_SEARCH_01, "obsidian_search_notes", None, id="OBS-SEARCH-01"),
    pytest.param(_SEARCH_02, "obsidian_search_notes", None, id="OBS-SEARCH-02"),
    pytest.param(_SYNC_01, "obsidian_search_notes", None, id="OBS-SYNC-01"),
    pytest.param(_LINK_01, WORKFLOW_READ_TOOL, "backlinks", id="OBS-LINK-01"),
    pytest.param(_MOVE_01, WORKFLOW_WRITE_TOOL, "move_note", id="OBS-MOVE-01-move"),
    pytest.param(_MOVE_01_BACKLINKS, WORKFLOW_READ_TOOL, "backlinks", id="OBS-MOVE-01-backlinks"),
    pytest.param(
        _TEMPLATE_01,
        WORKFLOW_WRITE_TOOL,
        "create_from_template",
        id="OBS-TEMPLATE-01",
    ),
    pytest.param(_WORK_01, WORKFLOW_WRITE_TOOL, "save_summary", id="OBS-WORK-01-save"),
    pytest.param(
        _WORK_01_LINKS,
        WORKFLOW_WRITE_TOOL,
        "append_summary_links",
        id="OBS-WORK-01-links",
    ),
    pytest.param(_BASE_01, WORKFLOW_WRITE_TOOL, "create_base", id="OBS-BASE-01"),
    pytest.param(_OFFLINE_01, "obsidian_create_note", None, id="OBS-OFFLINE-01"),
    pytest.param(
        _CONFLICT_01_REPLACE,
        WORKFLOW_WRITE_TOOL,
        "replace_active_section",
        id="OBS-CONFLICT-01-replace",
    ),
    pytest.param(
        _CONFLICT_01_PREVIEW,
        WORKFLOW_READ_TOOL,
        "conflict_preview",
        id="OBS-CONFLICT-01-preview",
    ),
    pytest.param(_RECOVERY_01_APPEND, "obsidian_daily_note", None, id="OBS-RECOVERY-01-append"),
    pytest.param(
        _RECOVERY_01_RESUME,
        WORKFLOW_WRITE_TOOL,
        "resume_previous",
        id="OBS-RECOVERY-01-resume",
    ),
    pytest.param(_DELETE_01, WORKFLOW_WRITE_TOOL, "delete_note", id="OBS-DELETE-01-delete"),
    pytest.param(_DELETE_01_SEARCH, "obsidian_search_notes", None, id="OBS-DELETE-01-search"),
)


def _runtime(settings, storage, kernel: _AcceptanceRoutingKernel) -> AgentRuntime:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(
            settings,
            verify_answers=False,
            obsidian_public_base_url="https://friday.example",
        ),
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    async def forbidden_general_context(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("an exact Obsidian acceptance message entered general retrieval")

    runtime._prepare_context = forbidden_general_context  # type: ignore[method-assign]
    return runtime


def _assert_literal_battery_message(message: str) -> None:
    assert f"```text\n{message}\n```" in _BATTERY_TEXT


@pytest.mark.parametrize(("message", "expected_tool", "expected_action"), _SINGLE_TURN_CASES)
@pytest.mark.asyncio
async def test_every_exact_tier_a_b_message_routes_through_full_chat_once(
    settings,
    storage,
    message: str,
    expected_tool: str,
    expected_action: str | None,
) -> None:
    _assert_literal_battery_message(message)
    kernel = _AcceptanceRoutingKernel(((expected_tool, expected_action),))
    runtime = _runtime(settings, storage, kernel)

    reply = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    kernel.assert_complete()
    assert [name for name, _payload in kernel.executed] == ["obsidian_list_vaults", expected_tool]
    assert reply["tools_used"] == ["obsidian_list_vaults", expected_tool]
    assert reply["message_format"] == "plain"
    assert kernel.target_sessions == [str(reply["conversation_id"])]
    target_arguments = kernel.target_calls[0][1]
    if expected_action is not None:
        assert target_arguments["action"] == expected_action
    if expected_tool in {
        "obsidian_create_note",
        "obsidian_append_note",
        "obsidian_daily_note",
        WORKFLOW_WRITE_TOOL,
    }:
        assert str(target_arguments["operation_id"]).startswith("obsop_")
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["structural"]["obsidian_owned"] is True


@pytest.mark.asyncio
async def test_obs_cont_01_keeps_all_three_exact_messages_in_one_full_chat_session(
    settings,
    storage,
) -> None:
    for message in (_CONT_01_SEARCH, _CONT_01_SELECT, _CONT_01_APPEND):
        _assert_literal_battery_message(message)
    steps = (
        ("obsidian_search_notes", None),
        (WORKFLOW_READ_TOOL, "select_candidate"),
        (WORKFLOW_WRITE_TOOL, "append_active_section"),
    )
    kernel = _AcceptanceRoutingKernel(steps)
    runtime = _runtime(settings, storage, kernel)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    first = await runtime.chat("alice", _CONT_01_SEARCH, actor=actor)
    conversation_id = str(first["conversation_id"])
    second = await runtime.chat(
        "alice",
        _CONT_01_SELECT,
        actor=actor,
        conversation_id=conversation_id,
    )
    third = await runtime.chat(
        "alice",
        _CONT_01_APPEND,
        actor=actor,
        conversation_id=conversation_id,
    )

    kernel.assert_complete()
    assert [name for name, _payload in kernel.executed] == [
        "obsidian_list_vaults",
        "obsidian_search_notes",
        "obsidian_list_vaults",
        WORKFLOW_READ_TOOL,
        "obsidian_list_vaults",
        WORKFLOW_WRITE_TOOL,
    ]
    assert kernel.target_sessions == [conversation_id, conversation_id, conversation_id]
    assert str(second["conversation_id"]) == conversation_id
    assert str(third["conversation_id"]) == conversation_id
    assert kernel.target_calls[0][1] == {"query": "Friday и поиск", "limit": 20}
    assert kernel.target_calls[1][1] == {"action": "select_candidate", "ordinal": 2}
    append = dict(kernel.target_calls[2][1])
    operation_id = str(append.pop("operation_id"))
    assert operation_id.startswith("obsop_")
    assert append == {
        "action": "append_active_section",
        "section": "Следующие шаги",
        "item": "- проверку семантического индекса",
    }
    assert first["tools_used"] == ["obsidian_list_vaults", "obsidian_search_notes"]
    assert second["tools_used"] == ["obsidian_list_vaults", WORKFLOW_READ_TOOL]
    assert third["tools_used"] == ["obsidian_list_vaults", WORKFLOW_WRITE_TOOL]


@pytest.mark.asyncio
async def test_explicit_conflict_acceptance_routes_to_the_durable_write_action(
    settings,
    storage,
) -> None:
    kernel = _AcceptanceRoutingKernel(((WORKFLOW_WRITE_TOOL, "accept_conflict_merge"),))
    runtime = _runtime(settings, storage, kernel)

    reply = await runtime.chat(
        "alice",
        "Прими эту объединённую версию.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    kernel.assert_complete()
    assert kernel.target_calls[0][1]["action"] == "accept_conflict_merge"
    assert str(kernel.target_calls[0][1]["operation_id"]).startswith("obsop_")
    assert reply["tools_used"] == ["obsidian_list_vaults", WORKFLOW_WRITE_TOOL]
