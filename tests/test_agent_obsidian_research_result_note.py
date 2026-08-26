"""Full-chat regression for public research followed by a code-owned Obsidian write."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentRuntime,
    _obsidian_result_note_body,
    _obsidian_result_note_missing_facets,
    _obsidian_result_note_path,
)
from friday.execution_kernel import ToolResult
from friday.organs.obsidian.conversation import obsidian_result_note_request
from friday.permissions import ActorContext

_MESSAGE = (
    "Можно ли развернуть на qnap TVS-675 nextcloud? Создай заметку в obsidian по результатам этой задачи"
)
_TASK = "Можно ли развернуть на qnap TVS-675 nextcloud?"
_CHARACTERISTICS_MESSAGE = "Создай заметку в обсидиан с характеристиками qnap TVS-675"
_CHARACTERISTICS_HEADING = "Характеристики qnap TVS-675"
_CHARACTERISTICS_TASK = (
    "Полные характеристики qnap TVS-675: процессор/CPU, память/RAM/DDR4, "
    "SATA/M.2, Ethernet, USB/HDMI/PCIe, габариты, питание/энергопотребление?"
)
_CHARACTERISTICS_FACT = "\n".join(
    (
        "Процессор: ZhaoXin KX-U6580, 8 ядер, 2,5 ГГц.",
        "Оперативная память: 8 ГБ DDR4, расширение до 64 ГБ.",
        "Дисковая подсистема: 6 отсеков SATA и 2 слота M.2 2280.",
        "Сетевые интерфейсы: 2 порта Ethernet 2,5 Гбит/с RJ-45.",
        "Порты: 2 USB 3.2 Gen 1, 2 USB 3.2 Gen 2 и HDMI 2.0.",
        "Расширение: 2 слота PCIe Gen 3 x4.",
        "Габариты: 180,2 × 264,3 × 279,6 мм; масса 7 кг.",
        "Питание: блок питания 250 Вт.",
    )
)
_CHARACTERISTICS_ONE_FACT = "QNAP TVS-675 использует 8-ядерный процессор ZhaoXin KX-U6580 с частотой 2,5 ГГц."
_FACT = "QNAP TVS-675 поддерживает контейнерный вариант Nextcloud при проверке совместимости пакетов."
_URL = "https://public.synthetic.example.com/qnap-nextcloud"
_REVISION = "a" * 64
_PRIVATE_CANARY = "PRIVATE-OBSIDIAN-HISTORY-CANARY"
_LONG_WEB_HEAD = "QNAP-OFFICIAL-EVIDENCE-HEAD"
_LONG_WEB_TAIL = (
    "QNAP-OFFICIAL-EVIDENCE-TAIL: TVS-675 uses ZhaoXin KX-U6580, 8 cores, 2.5 GHz; "
    "Container Station runs Docker containers; Nextcloud publishes a community Docker image."
)
_UNSUPPORTED_CPU = (
    "Да, Nextcloud можно запустить через Container Station/Docker. "
    "TVS-675 обычно оснащается процессором Intel Core i5 или i7."
)
_REPAIRED_CPU = (
    "Да, Nextcloud можно запустить через Container Station/Docker. "
    "TVS-675 оснащён 8-ядерным процессором ZhaoXin KX-U6580 с частотой 2,5 ГГц."
)


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


def test_required_characteristics_facet_accepts_explicit_source_unavailability() -> None:
    request = obsidian_result_note_request(_CHARACTERISTICS_MESSAGE)
    assert request is not None
    sourced = "\n".join(
        line for line in _CHARACTERISTICS_FACT.splitlines() if not line.startswith("Расширение:")
    )
    answer = f"{sourced}\nPCIe: в доступном официальном источнике не указано."
    evidence = ToolResult(
        "web_research",
        True,
        data={
            "sources": [
                {
                    "url": _URL,
                    "title": "Synthetic QNAP source",
                    "text": sourced,
                }
            ]
        },
    ).to_llm_message()

    assert (
        _obsidian_result_note_missing_facets(
            request,
            answer,
            [{"tool": "web_research", "output": evidence}],
        )
        == ()
    )


def test_source_unavailability_does_not_support_a_positive_model_fact() -> None:
    request = obsidian_result_note_request(_CHARACTERISTICS_MESSAGE)
    assert request is not None
    sourced = _CHARACTERISTICS_FACT.replace(
        "Процессор: ZhaoXin KX-U6580, 8 ядер, 2,5 ГГц.",
        "Процессор: в источнике не указано.",
    )
    evidence = ToolResult(
        "web_research",
        True,
        data={"sources": [{"url": _URL, "text": sourced}]},
    ).to_llm_message()

    assert _obsidian_result_note_missing_facets(
        request,
        _CHARACTERISTICS_FACT,
        [{"tool": "web_research", "output": evidence}],
    ) == ("processor",)


def _bounded_live_shaped_characteristics_evidence(*, include_pcie: bool = True) -> str:
    """Mirror a long product page plus two noisy peers in the 11.9k tool slot."""

    records = [
        "Процессор\nВосьмиядерный ZhaoXin KX-U6580 2,5 ГГц",
        "Память\n8 ГБ (DDR4)\nМожет быть расширена до 64 ГБ",
        "Дисковая подсистема\n6 отсеков SATA\nСлоты M.2\n2",
        "Сетевые интерфейсы\n2 порта Ethernet 2,5 Гбит/с RJ-45",
        "Порты USB 3.2 Gen2\n2\nHDMI-порт\nHDMI 2.0",
        "Слоты расширения\n2\nОписание слотов расширения\nPCIe Gen3 x4",
        "Габариты, (мм.)\n180,2 × 264,3 × 279,6\nМасса, (кг.)\n7",
        "Электропитание\nБлок питания\nВстроенный\nМощность\n250 Вт",
    ]
    if not include_pcie:
        records = [record for record in records if "PCIe" not in record]
    navigation = "Навигация каталога, описание серии и условия поставки. " * 22
    long_product_page = "\n".join(part for record in records for part in (navigation, record))
    sources = [
        {
            "url": "https://qnap.synthetic.example/tvs-675",
            "title": "QNAP TVS-675",
            "text": long_product_page,
        },
        {
            "url": "https://shop.synthetic.example/tvs-675",
            "title": "Store listing",
            "text": "Меню магазина и условия доставки. " * 600,
        },
        {
            "url": "https://market.synthetic.example/tvs-675",
            "title": "Marketplace listing",
            "text": "Карточка продавца без технических характеристик. " * 500,
        },
    ]
    return ToolResult(
        "web_research",
        True,
        data={"query": _CHARACTERISTICS_TASK, "sources": sources},
    ).to_llm_message()


def test_long_multi_record_characteristics_survive_the_actual_bounded_evidence_slot() -> None:
    request = obsidian_result_note_request(_CHARACTERISTICS_MESSAGE)
    assert request is not None

    evidence = _bounded_live_shaped_characteristics_evidence()
    payload = json.loads(evidence.partition("\n")[2])

    assert len(evidence) <= 12_100
    assert len(payload["sources"]) == 3
    projected = payload["sources"][0]["text"]
    assert not [
        marker
        for marker in ("KX-U6580", "64 ГБ", "M.2", "RJ-45", "USB", "HDMI", "PCIe", "279,6", "250 Вт")
        if marker not in projected
    ]
    assert (
        _obsidian_result_note_missing_facets(
            request,
            _CHARACTERISTICS_FACT,
            [{"tool": "web_research", "output": evidence}],
        )
        == ()
    )


def test_complete_model_text_cannot_substitute_for_a_facet_missing_from_bounded_evidence() -> None:
    request = obsidian_result_note_request(_CHARACTERISTICS_MESSAGE)
    assert request is not None

    evidence = _bounded_live_shaped_characteristics_evidence(include_pcie=False)

    assert _obsidian_result_note_missing_facets(
        request,
        _CHARACTERISTICS_FACT,
        [{"tool": "web_research", "output": evidence}],
    ) == ("ports_expansion",)


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

    def __init__(self, *, fact: str = _FACT) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fact = fact

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
                    "query": payload["query"],
                    "outbound_attempted": True,
                    "sources": [
                        {
                            "url": _URL,
                            "title": "Synthetic QNAP source",
                            "text": self.fact,
                            "text_length": len(self.fact),
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


class _LongEvidenceKernel(_Kernel):
    async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
        if name != "web_research":
            return await super().execute(name, arguments, actor=actor)
        payload = dict(arguments)
        self.calls.append((str(name), payload))
        source_text = f"{_LONG_WEB_HEAD}\n{'Проверяемые сведения QNAP. ' * 140}\n{_LONG_WEB_TAIL}"
        return ToolResult(
            name,
            True,
            data={
                "outbound_attempted": True,
                "query": _TASK,
                "sources": [
                    {
                        "url": _URL,
                        "title": "Synthetic official QNAP specification",
                        "text": source_text,
                        "text_length": len(source_text),
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


class _Model:
    enabled = True
    model = "synthetic-result-note"
    total_budget_sec = 1.0

    def __init__(
        self,
        *,
        task: str = _TASK,
        fact: str = _FACT,
        evidence_marker: str | None = None,
    ) -> None:
        self.tool_names: list[set[str]] = []
        self.task = task
        self.fact = fact
        self.evidence_marker = evidence_marker or fact

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.tool_names.append(
            {
                str((item.get("function") or {}).get("name") or item.get("name") or "")
                for item in (tools or [])
            }
        )
        rendered = json.dumps(messages, ensure_ascii=False)
        assert self.task in rendered
        assert "Создай заметку" not in rendered
        assert _PRIVATE_CANARY not in rendered
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
                "finish_reason": "stop",
            }
        if self.evidence_marker not in rendered:
            return {
                "content": json.dumps(
                    {"вид": "интернет", "запрос": self.task, "кто": "", "дни": []},
                    ensure_ascii=False,
                ),
                "tool_calls": None,
            }
        return {"content": self.fact, "tool_calls": None, "_queue_wait_sec": 0.0}


class _RepairingGroundedModel:
    enabled = True
    model = "synthetic-grounded-result-note"
    total_budget_sec = 1.0

    def __init__(self) -> None:
        self.verifier_calls = 0
        self.repair_calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        rendered = json.dumps(messages, ensure_ascii=False)
        assert _TASK in rendered
        assert "Создай заметку" not in rendered
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            self.verifier_calls += 1
            assert _LONG_WEB_HEAD in rendered
            assert _LONG_WEB_TAIL in rendered
            assert "похожая модель" in rendered
            passed = self.verifier_calls == 2
            return {
                "content": json.dumps(
                    {
                        "ok": passed,
                        "request_satisfied": True,
                        "score": 1.0 if passed else 0.0,
                        "issues": [] if passed else ["Intel CPU is unsupported by the evidence"],
                    }
                ),
                "finish_reason": "stop",
            }
        if "FRIDAY_REPAIR_DATA" in rendered:
            self.repair_calls += 1
            assert _LONG_WEB_HEAD in rendered
            assert _LONG_WEB_TAIL in rendered
            assert "похожая модель" in rendered
            return {"content": _REPAIRED_CPU, "finish_reason": "stop"}
        if _LONG_WEB_TAIL in rendered:
            return {"content": _UNSUPPORTED_CPU, "finish_reason": "stop"}
        return {
            "content": json.dumps(
                {"вид": "интернет", "запрос": _TASK, "кто": "", "дни": []},
                ensure_ascii=False,
            ),
            "tool_calls": None,
        }


class _UnknownGroundingModel(_Model):
    def __init__(self) -> None:
        super().__init__()
        self.verifier_calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        rendered = json.dumps(messages, ensure_ascii=False)
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            self.verifier_calls += 1
            self.tool_names.append(set())
            return {"content": "verifier unavailable", "finish_reason": "stop"}
        return await super().chat(messages, tools=tools, **kwargs)


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
        if "FRIDAY_VERIFICATION_DATA" in rendered:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
                "finish_reason": "stop",
            }
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
async def test_exact_characteristics_prompt_researches_verifies_and_saves_the_answer(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel(fact=_CHARACTERISTICS_FACT)
    model = _Model(
        task=_CHARACTERISTICS_TASK,
        fact=_CHARACTERISTICS_FACT,
        evidence_marker="Процессор: ZhaoXin KX-U6580",
    )
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

    reply = await runtime.chat(
        "alice",
        _CHARACTERISTICS_MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _payload in kernel.calls] == [
        "web_research",
        "obsidian_list_vaults",
        "obsidian_create_note",
    ], (reply, model.tool_names, kernel.calls)
    assert kernel.calls[0][1]["query"] == _CHARACTERISTICS_TASK
    create = kernel.calls[-1][1]
    assert str(create["path"]).startswith("Research/Характеристики qnap TVS-675 — 2026-08-22 (")
    missing_lines = [
        line for line in _CHARACTERISTICS_FACT.splitlines() if line not in str(create["content"])
    ]
    assert not missing_lines, (missing_lines, create["content"])
    assert str(create["content"]).startswith(f"# {_CHARACTERISTICS_HEADING}\n")
    assert reply["verification_status"] == "passed"
    assert reply["verified"] is True
    assert not [line for line in _CHARACTERISTICS_FACT.splitlines() if line not in reply["message"]]
    assert "Заметка создана" in reply["message"]


@pytest.mark.asyncio
async def test_one_fact_characteristics_answer_is_not_written_even_when_verifier_passes(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel(fact=_CHARACTERISTICS_ONE_FACT)
    model = _Model(task=_CHARACTERISTICS_TASK, fact=_CHARACTERISTICS_ONE_FACT)
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

    reply = await runtime.chat(
        "alice",
        _CHARACTERISTICS_MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert [name for name, _payload in kernel.calls] == ["web_research"]
    assert reply["tools_used"] == ["web_research"]
    assert reply["verification_status"] == "passed"
    assert reply["verified"] is True
    assert _CHARACTERISTICS_ONE_FACT in reply["message"]
    assert "не покрывает обязательные разделы характеристик" in reply["message"]
    assert "Заметка создана" not in reply["message"]


@pytest.mark.asyncio
async def test_long_web_envelope_is_shared_with_judge_and_repair_before_note_write(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _LongEvidenceKernel()
    model = _RepairingGroundedModel()
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

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert model.verifier_calls == 2
    assert model.repair_calls == 1
    assert [name for name, _payload in kernel.calls] == [
        "web_research",
        "obsidian_list_vaults",
        "obsidian_create_note",
    ]
    create = kernel.calls[-1][1]
    assert _REPAIRED_CPU in str(create["content"])
    assert "Intel Core" not in str(create["content"])
    assert "не получил статус полностью проверенного" not in str(create["content"])
    assert reply["verification_status"] == "passed"
    assert reply["verified"] is True
    assert "Заметка создана" in reply["message"]


@pytest.mark.asyncio
async def test_unknown_scoped_verifier_blocks_durable_web_result_note_even_when_global_judge_is_off(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    model = _UnknownGroundingModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    reply = await runtime.chat(
        "alice",
        _MESSAGE,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    assert model.verifier_calls == 1
    assert [name for name, _payload in kernel.calls] == ["web_research"]
    assert reply["tools_used"] == ["web_research"]
    assert reply["verification_status"] == "unknown"
    assert reply["verified"] is False
    assert "не прошёл проверку" in reply["message"]
    assert "Заметка создана" not in reply["message"]


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
