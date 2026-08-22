"""Full-chat regression for public research followed by a code-owned Obsidian write."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime, _obsidian_result_note_body, _obsidian_result_note_path
from friday.execution_kernel import ToolResult
from friday.organs.obsidian.conversation import obsidian_result_note_request
from friday.permissions import ActorContext

_MESSAGE = (
    "Можно ли развернуть на qnap TVS-675 nextcloud? Создай заметку в obsidian по результатам этой задачи"
)
_TASK = "Можно ли развернуть на qnap TVS-675 nextcloud?"
_FACT = "QNAP TVS-675 поддерживает контейнерный вариант Nextcloud при проверке совместимости пакетов."
_URL = "https://public.synthetic.example.com/qnap-nextcloud"
_REVISION = "a" * 64
_PRIVATE_CANARY = "PRIVATE-OBSIDIAN-HISTORY-CANARY"


def test_result_note_path_is_stable_for_replay_and_unique_for_a_new_turn() -> None:
    request = obsidian_result_note_request(_MESSAGE)

    assert request is not None
    first = _obsidian_result_note_path(request, date(2026, 8, 22), "msg_0123456789abcdef")
    replay = _obsidian_result_note_path(request, date(2026, 8, 22), "msg_0123456789abcdef")
    independent = _obsidian_result_note_path(request, date(2026, 8, 22), "msg_fedcba9876543210")
    assert first == replay
    assert first != independent


def test_result_note_path_and_heading_redact_friday_api_tokens() -> None:
    secret = "jrc_" + "A" * 43
    request = obsidian_result_note_request(
        f"Что означает {secret}? Создай заметку в obsidian по результатам этой задачи"
    )

    assert request is not None
    path = _obsidian_result_note_path(request, date(2026, 8, 22), "msg_0123456789abcdef")
    body = _obsidian_result_note_body(request, "Безопасный ответ.", date(2026, 8, 22), [])
    assert secret not in path
    assert secret not in body


def _schema(name: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic production contract",
            "parameters": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        },
    }


class _AllowAll:
    @staticmethod
    def authorize(actor, capability, **kwargs):  # noqa: ANN001, ARG004
        return SimpleNamespace(allowed=True)


class _Kernel:
    authorization = _AllowAll()

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def get_tool_definitions(actor, topic=""):  # noqa: ANN001, ARG004
        return [
            _schema("web_research", {"query": {"type": "string"}, "max_sources": {"type": "integer"}}),
            _schema("obsidian_list_vaults", {}),
            _schema(
                "obsidian_create_note",
                {
                    "operation_id": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            ),
        ]

    @staticmethod
    def get_tool(name: str) -> Any:
        if name == "web_research":
            return SimpleNamespace(risk="mutate", security_id="web.research")
        if name == "obsidian_list_vaults":
            return SimpleNamespace(risk="observe", security_id="obsidian.read")
        if name == "obsidian_create_note":
            return SimpleNamespace(risk="mutate", security_id="obsidian.write")
        return None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
        payload = dict(arguments)
        self.calls.append((str(name), payload))
        if name == "web_research":
            return ToolResult(
                name,
                True,
                data={
                    "outbound_attempted": True,
                    "sources": [
                        {
                            "url": _URL,
                            "title": "Synthetic QNAP source",
                            "text": _FACT,
                            "text_length": len(_FACT),
                            "status_code": 200,
                            "error": "",
                            "truncated": False,
                        }
                    ],
                    "requested_sources": 1,
                    "completed_sources": 1,
                    "failed_sources": 0,
                    "timed_out_sources": 0,
                    "search_timed_out": False,
                },
            )
        if name == "obsidian_list_vaults":
            return ToolResult(
                name,
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
        assert name == "obsidian_create_note"
        path = str(payload["path"])
        return ToolResult(
            name,
            True,
            data={
                "operation_id": payload["operation_id"],
                "method": "create",
                "status": "scan_pending",
                "path": path,
                "revision": _REVISION,
                "previous_revision": None,
                "created": True,
                "applied": True,
                "replayed": False,
                "open_uri": "obsidian://open?" + urllib.parse.urlencode({"vault": "Friday", "file": path}),
                "delivery": {
                    "local_write_complete": True,
                    "server_scan_complete": False,
                    "android_connected": False,
                    "android_completion": None,
                    "android_received": False,
                    "obsidian_opened": False,
                },
            },
        )


class _Model:
    enabled = True
    model = "synthetic-result-note"
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.tool_names: list[set[str]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.tool_names.append(
            {
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (tools or [])
            }
        )
        rendered = json.dumps(messages, ensure_ascii=False)
        assert _TASK in rendered
        assert "Создай заметку" not in rendered
        assert _PRIVATE_CANARY not in rendered
        if _FACT not in rendered:
            return {
                "content": json.dumps(
                    {"вид": "интернет", "запрос": _TASK, "кто": "", "дни": []},
                    ensure_ascii=False,
                ),
                "tool_calls": None,
            }
        return {"content": _FACT, "tool_calls": None, "_queue_wait_sec": 0.0}


class _ForbiddenModel:
    enabled = True
    model = "forbidden-source-carrier-model"
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        raise AssertionError("a source-carried compound request reached the model")


class _ChangingAnswerModel(_Model):
    def __init__(self) -> None:
        super().__init__()
        self.accepted_answers = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.tool_names.append(
            {
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (tools or [])
            }
        )
        rendered = json.dumps(messages, ensure_ascii=False)
        assert _TASK in rendered
        assert "Создай заметку" not in rendered
        if _FACT not in rendered:
            return {
                "content": json.dumps(
                    {"вид": "интернет", "запрос": _TASK, "кто": "", "дни": []},
                    ensure_ascii=False,
                ),
                "tool_calls": None,
            }
        self.accepted_answers += 1
        return {
            "content": f"{_FACT} Версия принятого ответа {self.accepted_answers}.",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


class _IdempotentKernel(_Kernel):
    def __init__(self) -> None:
        super().__init__()
        self.create_effects = 0
        self.first_create_payload: dict[str, Any] | None = None
        self.first_create_receipt: dict[str, Any] | None = None

    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        if name != "obsidian_create_note":
            return await super().execute(name, arguments, actor=actor)
        payload = dict(arguments)
        if self.first_create_payload is None:
            result = await super().execute(name, payload, actor=actor)
            assert isinstance(result.data, dict)
            self.first_create_payload = payload
            self.first_create_receipt = dict(result.data)
            self.create_effects += 1
            return result

        self.calls.append((str(name), payload))
        assert payload == self.first_create_payload, "replay changed the frozen root write arguments"
        assert self.first_create_receipt is not None
        return ToolResult(
            name,
            True,
            data={
                **self.first_create_receipt,
                "applied": False,
                "replayed": True,
            },
        )


class _KnowledgeVerdictModel(_Model):
    def __init__(self, verdict: str = "знание") -> None:
        super().__init__()
        self.verdict = verdict
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        self.tool_names.append(
            {
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (tools or [])
            }
        )
        rendered = json.dumps(messages, ensure_ascii=False)
        assert _TASK in rendered
        assert "Создай заметку" not in rendered
        if self.calls == 1:
            return {
                "content": json.dumps(
                    {"вид": self.verdict, "запрос": "", "кто": "", "дни": []},
                    ensure_ascii=False,
                ),
                "tool_calls": None,
            }
        return {
            "content": _FACT,
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


@pytest.mark.parametrize(
    "carrier_kind",
    ["attachment", "reply", "replay-attachment", "replay-invalid"],
)
@pytest.mark.asyncio
async def test_source_carried_compound_request_is_refused_before_model_web_or_obsidian(
    settings,
    storage,
    carrier_kind: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    model = _ForbiddenModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    kwargs: dict[str, Any] = {}
    if carrier_kind == "attachment":
        kwargs["attachments"] = [
            {
                "filename": "private.txt",
                "content": "private source bytes",
                "mime_type": "text/plain",
            }
        ]
    elif carrier_kind == "reply":
        kwargs["reply_to"] = "Приватная цитата из предыдущего сообщения."
    else:
        conversation = storage.create_conversation("alice", title="compound replay carrier")
        kwargs["conversation_id"] = str(conversation["id"])
        if carrier_kind == "replay-attachment":
            source = storage.store_message(
                str(conversation["id"]),
                "alice",
                "user",
                _MESSAGE,
                metadata={"had_attachments": True, "attachment_count": 1},
            )
            kwargs["replay_source_message_id"] = str(source["id"])
        else:
            kwargs["replay_source_message_id"] = "msg_deadbeefdeadbeef"

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        **kwargs,
    )

    assert model.calls == 0
    assert kernel.calls == []
    assert reply["tools_used"] == []
    lowered = str(reply["message"]).casefold()
    assert any(
        marker in lowered
        for marker in (
            "запись заметки не запуск",
            "производные файлы не опублик",
            "отправить отдельным сообщением",
        )
    )
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"]["model_spoke"] is False


@pytest.mark.asyncio
async def test_valid_regenerate_reuses_the_root_receipt_across_a_new_answer_and_local_day(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _IdempotentKernel()
    model = _ChangingAnswerModel()
    runtime = AgentRuntime(
        replace(
            settings,
            verify_answers=False,
            obsidian_public_base_url="https://friday.example",
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    async def forbidden_general_context(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a valid compound replay left the isolated lane")

    runtime._prepare_context = forbidden_general_context  # type: ignore[method-assign]
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    first = await runtime.chat("alice", _MESSAGE, actor=actor)
    rows = storage.get_conversation_messages(str(first["conversation_id"]), user_id="alice")
    root = next(item for item in rows if str(item.get("role") or "") == "user")
    first_create = dict(kernel.first_create_payload or {})
    assert "Версия принятого ответа 1." in first["message"]
    assert "2026-08-22" in str(first_create["path"])

    runtime._local_today = lambda: date(2026, 8, 23)  # type: ignore[method-assign]
    regenerated = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=actor,
        conversation_id=str(first["conversation_id"]),
        replay_source_message_id=str(root["id"]),
    )

    creates = [payload for name, payload in kernel.calls if name == "obsidian_create_note"]
    assert kernel.create_effects == 1
    assert len(creates) in {1, 2}
    assert all(payload == first_create for payload in creates)
    assert all("2026-08-23" not in str(payload["path"]) for payload in creates)
    assert "Версия принятого ответа 2." in regenerated["message"]
    assert "повторной записи не было" in regenerated["message"]


@pytest.mark.asyncio
async def test_knowledge_verdict_saves_the_accepted_answer_without_web_and_marks_the_limitation(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    runtime = AgentRuntime(
        replace(
            settings,
            verify_answers=False,
            obsidian_public_base_url="https://friday.example",
        ),
        storage,
        llm=_KnowledgeVerdictModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _payload in kernel.calls] == [
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    create = kernel.calls[-1][1]
    assert _FACT in str(create["content"])
    assert "без интернет-проверки" in str(create["content"]).casefold()
    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    assert "Заметка создана" in reply["message"]


@pytest.mark.parametrize("verdict", ["архив", "человек"])
@pytest.mark.asyncio
async def test_private_source_verdicts_remain_fail_closed_for_the_compound_request(
    settings,
    storage,
    verdict: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_KnowledgeVerdictModel(verdict),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert kernel.calls == []
    assert reply["tools_used"] == []
    assert "Заметка в Obsidian не создана" in reply["message"]


@pytest.mark.asyncio
async def test_public_result_is_saved_only_after_the_accepted_answer(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    model = _Model()
    runtime = AgentRuntime(
        replace(
            settings,
            verify_answers=False,
            obsidian_public_base_url="https://friday.example",
        ),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]
    conversation = storage.create_conversation("alice", title="private prior turn")
    storage.store_message(str(conversation["id"]), "alice", "user", "Приватный контекст")
    storage.store_message(
        str(conversation["id"]),
        "alice",
        "assistant",
        _PRIVATE_CANARY,
        metadata={"private_context_lineage": True},
    )

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=str(conversation["id"]),
    )

    assert [name for name, _payload in kernel.calls] == [
        "web_research",
        "obsidian_list_vaults",
        "obsidian_create_note",
    ], (reply, model.tool_names, kernel.calls)
    assert kernel.calls[0][1]["query"] == _TASK
    create = kernel.calls[2][1]
    assert str(create["operation_id"]).startswith("obsop_")
    assert str(create["path"]).startswith("Research/qnap TVS-675 nextcloud — 2026-08-22 (")
    assert str(create["path"]).endswith(").md")
    assert _FACT in str(create["content"])
    assert _URL in str(create["content"])
    assert "## Ограничения" in str(create["content"])
    assert all(names <= {"web_research"} for names in model.tool_names)
    assert _FACT in reply["message"]
    assert "Заметка создана" in reply["message"]
    assert reply["tools_used"] == [
        "web_research",
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    stored = storage.get_message(str(reply["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["web_evidence_status"] == "sourced"
    assert metadata["structural"]["obsidian_result_note_owned"] is True
    assert reply["message_format"] == "markdown"
    assert reply["obsidian_open_url"].startswith("https://friday.example/")


@pytest.mark.asyncio
async def test_failed_public_research_does_not_create_a_note(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()

    async def failed_execute(name, arguments, *, actor=None):  # noqa: ANN001, ARG001
        if name == "web_research":
            kernel.calls.append((str(name), dict(arguments)))
            return ToolResult(name, False, error="synthetic provider failure")
        raise AssertionError("Obsidian write ran without sourced web evidence")

    kernel.execute = failed_execute  # type: ignore[method-assign]

    class _FailureModel(_Model):
        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            self.tool_names.append(set())
            if len(self.tool_names) == 1:
                return {
                    "content": json.dumps(
                        {"вид": "интернет", "запрос": _TASK, "кто": "", "дни": []},
                        ensure_ascii=False,
                    ),
                    "tool_calls": None,
                }
            return {"content": "Не удалось получить сведения.", "tool_calls": None}

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_FailureModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _payload in kernel.calls] == ["web_research"]
    assert "Заметка в Obsidian не создана" in reply["message"]


@pytest.mark.asyncio
async def test_malformed_write_receipt_never_becomes_a_success_claim(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    real_execute = kernel.execute

    async def malformed_execute(name, arguments, *, actor=None):  # noqa: ANN001
        if name == "obsidian_create_note":
            kernel.calls.append((str(name), dict(arguments)))
            return ToolResult(name, True, data={"operation_id": arguments["operation_id"]})
        return await real_execute(name, arguments, actor=actor)

    kernel.execute = malformed_execute  # type: ignore[method-assign]
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_Model(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    runtime._local_today = lambda: date(2026, 8, 22)  # type: ignore[method-assign]

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _payload in kernel.calls] == [
        "web_research",
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    assert "неполную проверяемую квитанцию" in reply["message"]
    assert "Заметка создана" not in reply["message"]
