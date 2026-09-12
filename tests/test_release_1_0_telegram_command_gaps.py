"""Deterministic adapter oracles for the remaining Telegram command surfaces.

The backend and Telegram clients are scripted transports.  Every case enters
through ``TelegramBridge._process_update`` and proves only adapter routing,
signed person/chat context and the text/buttons emitted by the bridge.  It does
not claim backend persistence, a live bot round-trip or product execution.
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import quote

import pytest

import friday.telegram_bridge._commands as command_module
from friday.telegram_bridge import TelegramBridge, TelegramConfig
from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient

CHAT_ID = 5001
USER_ID = 1001
USER = {"id": USER_ID, "is_bot": False, "first_name": "Алиса"}


@pytest.fixture
def bridge_factory(tmp_path):
    made = []

    def make(**overrides):
        bridge = TelegramBridge(
            TelegramConfig(
                bot_token="123:token",
                bridge_secret="B" * 48,
                allowed_chat_ids=[CHAT_ID],
                inbox_db_path=str(tmp_path / f"telegram-{len(made)}.sqlite3"),
                **overrides,
            )
        )
        made.append(bridge)
        return bridge

    yield make
    for bridge in reversed(made):
        bridge._inbox.close()  # noqa: SLF001


def _message(text: str, update_id: int = 1, *, chat_id: int = CHAT_ID, chat_type: str = "private"):
    user = dict(USER) if chat_id == CHAT_ID else {"id": chat_id, "is_bot": False, "first_name": "Чужой"}
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100 + update_id,
            "chat": {"id": chat_id, "type": chat_type},
            "from": user,
            "text": text,
        },
    }


def _callback(data: str, update_id: int, *, reply_markup=None):
    message = {
        "message_id": 900 + update_id,
        "chat": {"id": CHAT_ID, "type": "private"},
    }
    if reply_markup is not None:
        message["reply_markup"] = reply_markup
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": dict(USER),
            "data": data,
            "message": message,
        },
    }


async def _run_command(bridge, telegram, backend, text: str, update_id: int = 1, **message_kwargs):
    await bridge._process_update(  # noqa: SLF001
        telegram,
        backend,
        _message(text, update_id, **message_kwargs),
        cached_response=None,
    )


async def _run_callback(bridge, telegram, backend, data: str, update_id: int, *, reply_markup=None):
    await bridge._process_update(  # noqa: SLF001
        telegram,
        backend,
        _callback(data, update_id, reply_markup=reply_markup),
        cached_response=None,
    )


def _telegram_payloads(telegram, method: str):
    return [payload for url, payload in telegram.calls if str(url).endswith(f"/{method}")]


def _messages(telegram):
    return _telegram_payloads(telegram, "sendMessage")


def _last_message(telegram):
    sent = _messages(telegram)
    assert sent, telegram.calls
    return sent[-1]


def _button_rows(payload):
    return (payload.get("reply_markup") or {}).get("inline_keyboard") or []


def _assert_call(call, method: str, path: str, body):
    assert (call["method"], call["path"], call["body"]) == (method, path, body)
    headers = call["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Friday-User"] == str(USER_ID)
    assert headers["X-Friday-Chat"] == str(CHAT_ID)
    assert headers["X-Friday-Timestamp"].isdigit()
    assert len(headers["X-Friday-Nonce"]) == 32
    assert len(headers["X-Friday-Signature"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "mode", "label", "extra", "engineer_enabled"),
    [
        ("/chat", "dialogue", "Обычный диалог", "", False),
        ("/work", "knowledge_work", "Работа со знаниями", "", False),
        (
            "/engineer",
            "engineer",
            "Инженерный разбор",
            " Назовите хост, URL или киньте exe/apk — пойду сразу. "
            "Координаты берёт из чата. Эксплойт-пейлоадов нет.",
            True,
        ),
    ],
    ids=["chat", "work", "engineer"],
)
async def test_mode_commands_reach_exact_backend_and_label(
    bridge_factory, command, mode, label, extra, engineer_enabled
):
    bridge = bridge_factory(engineer_mode_enabled=engineer_enabled)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/mode": {"mode": mode}})

    await _run_command(bridge, telegram, backend, command)

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        "/api/conversations/channel/mode",
        {
            "channel": "telegram",
            "channel_id": str(CHAT_ID),
            "mode": mode,
            "telegram_user": USER,
        },
    )
    assert [item["text"] for item in _messages(telegram)] == [f"Режим: {label}.{extra}"]


@pytest.mark.asyncio
async def test_engineer_disabled_is_local_and_never_calls_backend(bridge_factory):
    bridge = bridge_factory(engineer_mode_enabled=False)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/engineer")

    assert backend.calls == []
    assert [item["text"] for item in _messages(telegram)] == [
        "Инженерный режим не включён в этом экземпляре Friday."
    ]


@pytest.mark.asyncio
async def test_engineer_forbidden_is_named_and_keeps_person_chat_context(bridge_factory):
    bridge = bridge_factory(engineer_mode_enabled=True)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/mode": (403, {"detail": "forbidden"})})

    await _run_command(bridge, telegram, backend, "/engineer")

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        "/api/conversations/channel/mode",
        {
            "channel": "telegram",
            "channel_id": str(CHAT_ID),
            "mode": "engineer",
            "telegram_user": USER,
        },
    )
    assert [item["text"] for item in _messages(telegram)] == ["Инженерный режим доступен только владельцу."]


@pytest.mark.asyncio
async def test_history_without_query_is_usage_only(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/history")

    assert backend.calls == []
    assert _last_message(telegram)["text"] == "Использование: /history запрос — поиск по истории переписки"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"results": []},
            "В переписке по запросу «калибровка весов» ничего не нашлось.",
        ),
        (
            {
                "results": [
                    {"role": "user", "created_at": "2026-08-02T10:11:12+00:00", "content": "Где акт?"},
                    {
                        "role": "assistant",
                        "created_at": "2026-08-02T10:12:13+00:00",
                        "content": "Акт найден.",
                    },
                ]
            },
            "В переписке по запросу «калибровка весов»:\n"
            "1. [user] 2026-08-02T10:11:12\n"
            "  Где акт?\n"
            "2. [assistant] 2026-08-02T10:12:13\n"
            "  Акт найден.",
        ),
    ],
    ids=["empty", "hits"],
)
async def test_history_reaches_own_message_search_and_renders_result(bridge_factory, response, expected):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = f"/api/me/messages/search?q={quote('калибровка весов', safe='')}&limit=8"
    backend = _FakeBackendClient({path: response})

    await _run_command(bridge, telegram, backend, "/history калибровка весов")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", path, {"telegram_user": USER})
    assert _last_message(telegram)["text"] == expected


@pytest.mark.asyncio
async def test_timeline_rejects_unparsed_period_without_guessing(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/timeline прошлой весной")

    assert backend.calls == []
    assert _last_message(telegram)["text"] == (
        "Не понял период. Примеры: /timeline 2023, /timeline март 2023, "
        "/timeline 2023-03, /timeline 2020-01-01..2020-03-31, /timeline неделя. "
        "Без периода — за последние 30 дней."
    )


@pytest.mark.asyncio
async def test_timeline_default_reaches_both_views_and_emits_document_button(bridge_factory, monkeypatch):
    class _PinnedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 31)

    monkeypatch.setattr(command_module, "date", _PinnedDate)
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    documents_path = "/api/knowledge/by-date?since=2026-07-01&until=2026-07-31&limit=10"
    events_path = "/api/kg/timeline?start=2026-07-01&end=2026-07-31&limit=10"
    backend = _FakeBackendClient(
        {
            documents_path: {
                "items": [{"id": "ko_march", "title": "Приказ", "document_date": "2026-07-20"}],
                "total": 1,
            },
            events_path: {
                "items": [{"kind": "event", "at": "2026-07-21", "name": "Приёмка"}],
                "total": 1,
            },
        }
    )

    await _run_command(bridge, telegram, backend, "/timeline")

    assert len(backend.calls) == 2
    _assert_call(backend.calls[0], "GET", documents_path, None)
    _assert_call(backend.calls[1], "GET", events_path, None)
    card = _last_message(telegram)
    assert card["text"] == (
        "Хроника за 30 дней:\n\n"
        "События:\n"
        "• 2026-07-21 — Приёмка\n\n"
        "Документы по их собственной дате:\n"
        "1. 2026-07-20 — Приказ\n\n"
        "Кнопкой ниже — открыть документ целиком."
    )
    assert _button_rows(card) == [[{"text": "1", "callback_data": "doc:show:ko_march"}]]


@pytest.mark.asyncio
async def test_graph_without_pair_is_usage_only(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/graph Иванов")

    assert backend.calls == []
    assert _last_message(telegram)["text"] == (
        "Использование: /graph первый объект =&gt; второй объект\n\n"
        "Например: /graph Иванов =&gt; Заря. Покажу цепочку связей между ними. "
        "Карточка одного объекта: /profile имя"
    )


def _graph_path():
    return f"/api/kg/graph-path?source={quote('Иванов', safe='')}&target={quote('Проект Маяк', safe='')}"


@pytest.mark.asyncio
async def test_graph_reaches_path_and_renders_direction(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = _graph_path()
    backend = _FakeBackendClient(
        {
            path: {
                "found": True,
                "path": [
                    {
                        "from": {"name": "Иванов"},
                        "to": {"name": "Отдел Заря"},
                        "relation_type": "member_of",
                        "forward": True,
                    },
                    {
                        "from": {"name": "Отдел Заря"},
                        "to": {"name": "Проект Маяк"},
                        "relation_type": "works_on",
                        "forward": False,
                    },
                ],
            }
        }
    )

    await _run_command(bridge, telegram, backend, "/graph Иванов => Проект Маяк")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", path, {"telegram_user": USER})
    assert _last_message(telegram)["text"] == (
        "🔗 Как связаны «Иванов» и «Проект Маяк» — 2 шага(ов):\n"
        "1. Иванов →(member_of) Отдел Заря\n"
        "2. Отдел Заря ←(works_on) Проект Маяк\n\n"
        "Карточка любого из них: /profile имя"
    )


@pytest.mark.asyncio
async def test_graph_not_found_names_bound_and_no_fake_path(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = _graph_path()
    backend = _FakeBackendClient({path: {"found": False, "path": [], "depth_searched": 4}})

    await _run_command(bridge, telegram, backend, "/graph Иванов => Проект Маяк")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", path, {"telegram_user": USER})
    assert _last_message(telegram)["text"] == (
        "Связи между «Иванов» и «Проект Маяк» не нашлось в пределах 4 шагов.\n\n"
        "Совместная встречаемость в путь не входит намеренно: «упомянуты в одном "
        "документе» — это не связь, и цепочка через неё была бы выдумкой."
    )


@pytest.mark.asyncio
async def test_graph_forbidden_is_not_reported_as_absence(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = _graph_path()
    backend = _FakeBackendClient({path: (403, {"detail": "forbidden"})})

    await _run_command(bridge, telegram, backend, "/graph Иванов => Проект Маяк")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", path, {"telegram_user": USER})
    assert _last_message(telegram)["text"] == (
        "Смотреть объекты вам сейчас не разрешено — попросите доступ у владельца."
    )


@pytest.mark.asyncio
async def test_watch_without_topic_is_usage_only(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/watch")

    assert backend.calls == []
    assert _last_message(telegram)["text"] == (
        "Использование: /watch тема\n\n"
        "Например: /watch поверка весов. Сообщу, когда появится новое по теме. "
        "Список слежений: /watching"
    )


@pytest.mark.asyncio
async def test_watch_creates_exact_person_monitor(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me/monitors": {"monitor": {"id": "mon_1", "query": "поверка весов"}}})

    await _run_command(bridge, telegram, backend, "/watch поверка весов")

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        "/api/me/monitors",
        {"query": "поверка весов", "telegram_user": USER},
    )
    assert _last_message(telegram)["text"] == (
        "Слежу за темой «поверка весов». Сообщу, когда появится НОВОЕ по ней — "
        "то, что уже есть, показывать не буду: для этого /search поверка весов"
    )


@pytest.mark.asyncio
async def test_watching_lists_and_stops_exact_monitor(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/me/monitors": {
                "items": [
                    {"id": "mon_1", "query": "поверка весов", "matches_reported": 2},
                    {"id": "mon_2", "query": "проект Маяк", "matches_reported": 0},
                ]
            },
            "/api/me/monitors/mon_1/stop": {},
        }
    )

    await _run_command(bridge, telegram, backend, "/watching", update_id=1)

    _assert_call(backend.calls[0], "GET", "/api/me/monitors", {"telegram_user": USER})
    card = _last_message(telegram)
    assert card["text"] == (
        "Слежу за темами: 2.\n"
        "• поверка весов — сообщений: 2\n"
        "• проект Маяк\n\n"
        "Кнопкой ниже — снять слежение по номеру."
    )
    assert _button_rows(card) == [
        [
            {"text": "✕ 1", "callback_data": "mon:stop:mon_1"},
            {"text": "✕ 2", "callback_data": "mon:stop:mon_2"},
        ]
    ]

    await _run_callback(
        bridge,
        telegram,
        backend,
        "mon:stop:mon_1",
        update_id=2,
        reply_markup=card["reply_markup"],
    )

    assert len(backend.calls) == 2
    _assert_call(
        backend.calls[1],
        "POST",
        "/api/me/monitors/mon_1/stop",
        {"telegram_user": USER},
    )
    assert _telegram_payloads(telegram, "answerCallbackQuery")[-1] == {
        "callback_query_id": "cb-2",
        "text": "Слежение снято",
        "show_alert": False,
    }
    assert _last_message(telegram)["text"] == "Больше не слежу за этой темой. Список: /watching"
    assert _telegram_payloads(telegram, "editMessageReplyMarkup")[-1]["reply_markup"] == {
        "inline_keyboard": []
    }


@pytest.mark.asyncio
async def test_watching_empty_is_honest(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me/monitors": {"items": []}})

    await _run_command(bridge, telegram, backend, "/watching")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", "/api/me/monitors", {"telegram_user": USER})
    assert _last_message(telegram)["text"] == "Пока ни за чем не слежу. Начать: /watch тема"


@pytest.mark.asyncio
async def test_watch_is_refused_before_backend_for_unallowed_chat(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    await _run_command(bridge, telegram, backend, "/watch чужая тема", chat_id=9001)

    assert backend.calls == []
    assert telegram.calls == []


@pytest.mark.asyncio
async def test_approvals_private_lists_pending_and_uncertain_with_buttons(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    pending_path = "/api/me/approvals?status=pending"
    uncertain_path = "/api/me/approvals?status=uncertain"
    backend = _FakeBackendClient(
        {
            pending_path: {
                "items": [{"id": "apr_1", "summary": "Удалить архив", "tool": "cleanup"}],
                "total": 1,
            },
            uncertain_path: {
                "items": [{"id": "apr_u", "summary": "Отправить письмо", "error": "таймаут"}],
                "total": 1,
            },
        }
    )

    await _run_command(bridge, telegram, backend, "/approvals")

    assert len(backend.calls) == 2
    _assert_call(backend.calls[0], "GET", pending_path, {"telegram_user": USER})
    _assert_call(backend.calls[1], "GET", uncertain_path, {"telegram_user": USER})
    sent = _messages(telegram)
    assert [item["text"] for item in sent] == [
        "Ждут вашего решения: 1.",
        "1. Удалить архив",
        "⚠️ Действий с НЕИЗВЕСТНЫМ исходом: 1.\n"
        "Их исполнение оборвалось на середине: эффект мог случиться, а мог и нет.\n"
        "• Отправить письмо — таймаут\n"
        "Проверьте по этим действиям сами, что получилось. Повторять их автоматически "
        "нельзя: повтор дал бы второй побочный эффект по одному решению.",
    ]
    assert _button_rows(sent[1]) == [
        [
            {"text": "✓ 1", "callback_data": "apr:yes:apr_1"},
            {"text": "✕ 1", "callback_data": "apr:no:apr_1"},
        ]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "decision", "backend_response", "toast", "message"),
    [
        (
            "yes",
            "approve",
            {"executed": True, "final_response": "Архив удалён."},
            "Выполнено",
            "Архив удалён.",
        ),
        ("no", "reject", {}, "Отклонено", "Действие отклонено — оно не выполнено."),
    ],
    ids=["approve", "reject"],
)
async def test_approval_decision_callbacks_reach_exact_action(
    bridge_factory, action, decision, backend_response, toast, message
):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = "/api/approvals/apr_1/decide"
    backend = _FakeBackendClient({path: backend_response})
    markup = {
        "inline_keyboard": [
            [
                {"text": "✓ 1", "callback_data": "apr:yes:apr_1"},
                {"text": "✕ 1", "callback_data": "apr:no:apr_1"},
            ]
        ]
    }

    await _run_callback(
        bridge,
        telegram,
        backend,
        f"apr:{action}:apr_1",
        update_id=7,
        reply_markup=markup,
    )

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        path,
        {"telegram_user": USER, "decision": decision, "telegram_update_id": 7},
    )
    assert _telegram_payloads(telegram, "answerCallbackQuery")[-1] == {
        "callback_query_id": "cb-7",
        "text": toast,
        "show_alert": False,
    }
    assert _last_message(telegram)["text"] == message
    assert _telegram_payloads(telegram, "editMessageReplyMarkup")[-1]["reply_markup"] == {
        "inline_keyboard": []
    }


@pytest.mark.asyncio
async def test_mission_without_goal_is_usage_and_registration_only(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"actor": {"preset_key": "user"}, "user": {}}})

    await _run_command(bridge, telegram, backend, "/mission")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", "/api/me", {"telegram_user": USER})
    assert _last_message(telegram)["text"] == (
        "Использование: /mission цель миссии\n\n"
        "Я разобью её на шаги и, при включённой автономии, начну выполнять в фоне. "
        "Итоги придут в Inbox на review."
    )


@pytest.mark.asyncio
async def test_mission_create_reaches_backend_and_renders_plan(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/missions": {
                "mission": {
                    "id": "msn_1",
                    "title": "Проверка архива",
                    "status": "proposed",
                    "task_count": 2,
                }
            }
        }
    )

    await _run_command(bridge, telegram, backend, "/mission Проверить архив")

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        "/api/missions",
        {"goal": "Проверить архив", "telegram_user": USER},
    )
    assert _last_message(telegram)["text"] == (
        "Миссия принята: Проверка архива\n"
        "Шагов в плане: 2\n"
        "Статус: ожидает запуска.\n"
        "Отправьте /missions, чтобы запустить её кнопкой."
    )


@pytest.mark.asyncio
async def test_missions_list_reaches_view_and_start_callback(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    list_path = "/api/missions?limit=8"
    start_path = "/api/missions/msn_1/start"
    backend = _FakeBackendClient(
        {
            list_path: {
                "items": [
                    {
                        "id": "msn_1",
                        "title": "Архив 2026",
                        "status": "proposed",
                        "done_count": 1,
                        "task_count": 3,
                    }
                ]
            },
            start_path: {},
        }
    )

    await _run_command(bridge, telegram, backend, "/missions", update_id=1)

    _assert_call(backend.calls[0], "GET", list_path, {"telegram_user": USER})
    sent = _messages(telegram)
    assert [item["text"] for item in sent] == [
        "Ваши миссии: 1.",
        "Архив 2026\n\nСтатус: ожидает запуска. Шаги: 1/3.",
    ]
    card = sent[-1]
    assert _button_rows(card) == [
        [
            {"text": "▶ Запустить", "callback_data": "mission:start:msn_1"},
            {"text": "✕ Остановить", "callback_data": "mission:stop:msn_1"},
        ]
    ]

    await _run_callback(
        bridge,
        telegram,
        backend,
        "mission:start:msn_1",
        update_id=2,
        reply_markup=card["reply_markup"],
    )

    assert len(backend.calls) == 2
    _assert_call(backend.calls[1], "POST", start_path, {"telegram_user": USER})
    assert _telegram_payloads(telegram, "answerCallbackQuery")[-1] == {
        "callback_query_id": "cb-2",
        "text": "Миссия запущена",
        "show_alert": False,
    }
    assert _telegram_payloads(telegram, "editMessageReplyMarkup")[-1]["reply_markup"] == {
        "inline_keyboard": []
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"items": [], "total": 0},
            "Сводок пока нет. Они собираются раз в сутки по вашим разговорам за прошедший день.",
        ),
        (
            {
                "items": [
                    {
                        "local_date": "2026-09-09",
                        "counters": {"total_turns": 7},
                        "incidents": [{"text": "повторный вызов", "count": 2}],
                    }
                ],
                "total": 1,
            },
            "Сводки за последние дни:\n\n📅 2026-09-09 · ходов: 7\n  • повторный вызов ×2",
        ),
    ],
    ids=["empty", "incident"],
)
async def test_compact_reaches_exact_view(bridge_factory, response, expected):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    path = "/api/compacts?limit=3"
    backend = _FakeBackendClient({path: response})

    await _run_command(bridge, telegram, backend, "/compact")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", path, {"telegram_user": USER})
    assert _last_message(telegram)["text"] == expected


@pytest.mark.asyncio
async def test_status_reaches_backend_and_renders_exact_counts(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/stats": {
                "interaction_mode": "knowledge_work",
                "knowledge_object_count": 12,
                "entity_count": 4,
                "relation_count": 3,
                "pending_inbox": 2,
                "pending_relation_candidates": 5,
                "pending_conflicts": 1,
                "pending_resolutions": 6,
            }
        }
    )

    await _run_command(bridge, telegram, backend, "/status")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", "/api/kg/stats", None)
    assert _last_message(telegram)["text"] == (
        "Текущий режим: работа со знаниями.\n\n"
        "В вашей базе:\n"
        "• объектов знаний: 12\n"
        "• сущностей: 4\n"
        "• подтверждённых связей: 3\n"
        "• во входящих: 2\n"
        "• связей на review: 5 — /relations\n"
        "• конфликтов на review: 1\n"
        "• предложений объединить сущности: 6"
    )


@pytest.mark.asyncio
async def test_new_resets_exact_channel_and_preserves_knowledge_message(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/reset": {"session": {"mode": "dialogue"}}})

    await _run_command(bridge, telegram, backend, "/new")

    assert len(backend.calls) == 1
    _assert_call(
        backend.calls[0],
        "POST",
        "/api/conversations/channel/reset",
        {"channel": "telegram", "channel_id": str(CHAT_ID), "telegram_user": USER},
    )
    assert _last_message(telegram)["text"] == (
        "Новый диалог начат в обычном режиме. Сама база знаний не очищена."
    )


@pytest.mark.asyncio
async def test_instructions_show_saved_value(bridge_factory):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    metadata = json.dumps({"custom_instructions": "Отвечай кратко"}, ensure_ascii=False)
    backend = _FakeBackendClient({"/api/me": {"user": {"metadata_json": metadata}}})

    await _run_command(bridge, telegram, backend, "/instructions")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", "/api/me", {"telegram_user": USER})
    assert _last_message(telegram)["text"] == "Сейчас: Отвечай кратко"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "saved", "expected"),
    [
        ("/instructions Отвечай по пунктам", "Отвечай по пунктам", "Принято: Отвечай по пунктам"),
        ("/instructions очистить", "", "Пожелание снято."),
    ],
    ids=["set", "clear"],
)
async def test_instructions_mutations_reach_exact_self_route(bridge_factory, command, saved, expected):
    bridge = bridge_factory()
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/me": {"user": {"metadata_json": "{}"}},
            "/api/me/instructions": {"user": {}},
        }
    )

    await _run_command(bridge, telegram, backend, command)

    assert len(backend.calls) == 2
    _assert_call(backend.calls[0], "GET", "/api/me", {"telegram_user": USER})
    _assert_call(backend.calls[1], "PATCH", "/api/me/instructions", {"instructions": saved})
    assert _last_message(telegram)["text"] == expected


def _expected_help(engineer: bool, obsidian: bool) -> str:
    lines = [
        "Команды:",
        "/chat — обычный разговор",
        "/work — работа с личными знаниями",
        "/research — многошаговое исследование",
    ]
    if engineer:
        lines.append("/engineer — разбор файлов и аудит хостов владельца")
    lines.extend(
        [
            "/coding — статический осмотр исходников, без запуска",
            "/mission цель — многошаговая миссия в фоне",
            "/missions — список миссий и управление",
            "/inbox — разобрать ближайшие предложения",
            "/conflicts — разобрать конфликты знаний (порциями)",
            "/relations — принять или отклонить предложенные связи (порциями)",
            "/merges — подтвердить или отклонить объединение дубликатов",
            "/tags — теги базы знаний с количеством записей",
            "/browse тег или название — записи по тегу, проекту или сущности",
            "/profile имя — карточка объекта: документы, теги, даты, связи",
            "/graph первый =&gt; второй — как связаны двое: цепочка связей",
            "/search запрос — найти записи по смыслу, без ответа модели",
            "/history запрос — найти реплики в истории переписки",
            "/status — состояние базы",
            "/why — почему был такой ответ",
            "/new — начать новый диалог",
            "/archive — архивировать текущий разговор",
            "/delete — убрать текущий разговор из списка (переписка сохраняется)",
            "/rename название — переименовать текущий разговор",
            "/note текст — явно сохранить заметку",
            "/instructions — как отвечать: показать, задать или очистить",
            "/retry — сгенерировать ответ на последний вопрос заново",
            "/reminders — предстоящие напоминания; кнопка «Снять» отменяет одно",
            "/export — скачать текущий разговор текстом",
        ]
    )
    if obsidian:
        lines.extend(
            [
                "/obsidian — подключить или проверить Obsidian на Android",
                "/obsidian_alias имя — задать имя Android-vault для ссылок",
            ]
        )
    lines.extend(
        [
            "",
            "Ответы можно оценивать кнопками, а результаты /work, /research и миссий — "
            "отправлять в Inbox на review.",
        ]
    )
    return "\n".join(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engineer", "obsidian"),
    [(False, False), (True, True)],
    ids=["optional-off", "optional-on"],
)
async def test_help_reflects_optional_surfaces_exactly(bridge_factory, engineer, obsidian):
    bridge = bridge_factory(engineer_mode_enabled=engineer, obsidian_enabled=obsidian)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"actor": {"preset_key": "user"}, "user": {}}})

    await _run_command(bridge, telegram, backend, "/help")

    assert len(backend.calls) == 1
    _assert_call(backend.calls[0], "GET", "/api/me", {"telegram_user": USER})
    assert [item["text"] for item in _messages(telegram)] == [_expected_help(engineer, obsidian)]
