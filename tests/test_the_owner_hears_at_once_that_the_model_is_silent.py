"""Владелец узнаёт о молчащей модели сейчас, а не через час.

Живой отказ 2026-08-03. Сервер модели перестал отвечать на генерацию в 15:45.
Человек писал ещё двадцать минут и получил ВОСЕМЬ испорченных ответов подряд,
потом написал «Плохо» и перестал писать вовсе. Владелец узнал об этом от меня, а
не от системы.

Сторож при этом работал штатно. Он опрашивает раз в час — то есть двадцати-
минутный отказ мог не застать вовсе, — и проверял только порт, который в этом
отказе был открыт: соединение устанавливалось за 0.019 секунды, висела генерация.

Опрос здесь вообще лишний: система УЖЕ знает, что ход провалился, в ту же
секунду. Спрашивать состояние, о котором тебе только что сообщили, — и работа, и
задержка на ровном месте.

Три вещи, которые обязаны выполняться одновременно, и каждая проверяется:
предупреждение уходит; шквал отказов не превращается в шквал сообщений; попытка
предупредить не может уронить ход человека — ответ ему важнее уведомления о нём.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from friday.agent_runtime import AgentRuntime


class _SilentModel:
    """Модель, которая принимает вызов и не отвечает — как в живом отказе."""

    enabled = True
    total_budget_sec = 5.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise TimeoutError("Remote end closed connection without response")


class _WorkingModel:
    enabled = True
    total_budget_sec = 5.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        return {"content": "Готово."}


def _owner(storage, settings, chat_id: str = "100500"):
    """Владелец с личным чатом и этот чат — в списке служебных адресатов."""
    storage.ensure_user("boss", preset_key="owner")
    storage.update_user("boss", metadata_json={"chat_id": chat_id})
    return "boss", replace(settings, telegram_allowed_chat_ids=(int(chat_id),))


def _queued(storage, user_id: str) -> list[dict]:
    rows = storage.execute(
        "SELECT kind, body, dedup_key FROM outbound_notifications WHERE user_id=?", (user_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def test_the_owner_is_warned_the_moment_a_turn_fails(settings, storage) -> None:
    """Мутация: убрать вызов — владелец снова узнаёт через час или от меня."""
    user_id, settings = _owner(storage, settings)
    agent = AgentRuntime(settings, storage)
    agent.llm = _SilentModel()

    agent._tell_the_owner_the_model_is_silent(user_id)

    queued = _queued(storage, user_id)
    assert queued, "модель молчит, а владельцу не сказали"
    assert queued[0]["kind"] == "model_silent"
    body = str(queued[0]["body"])
    assert "не отвечает" in body, "не сказано, что случилось"
    assert "перезапуск" in body.casefold(), "не сказано, что делать"
    assert "испорченные ответы" in body, "не сказано, чем это грозит людям"


def test_a_storm_of_failures_is_one_message(settings, storage) -> None:
    """Отказ модели задевает КАЖДЫЙ ход.

    Без дедупа сегодняшний отказ дал бы восемь уведомлений за двадцать минут — по
    одному на каждый испорченный ответ. Ведро по времени, а не счётчик подряд
    идущих: двое пишущих одновременно — это два потока отказов об одной поломке.
    """
    user_id, settings = _owner(storage, settings)
    agent = AgentRuntime(settings, storage)
    agent.llm = _SilentModel()

    for _ in range(8):
        agent._tell_the_owner_the_model_is_silent(user_id)

    assert len(_queued(storage, user_id)) == 1, "шквал отказов стал шквалом уведомлений"


def test_a_working_model_says_nothing(settings, storage) -> None:
    """Предупреждение не по делу обесценивает те, что по делу."""
    user_id, settings = _owner(storage, settings)
    agent = AgentRuntime(settings, storage)
    agent.llm = _WorkingModel()

    asyncio.run(_chat(agent, user_id))

    assert _queued(storage, user_id) == []


async def _chat(agent: AgentRuntime, user_id: str):
    from friday.permissions import ActorContext

    actor = ActorContext(user_id=user_id, preset_key="owner", source="test")
    return await agent.chat(user_id, "привет", actor=actor)


def test_the_turn_survives_a_broken_notification(settings, storage) -> None:
    """Ответ человеку важнее уведомления о том, что ответа нет.

    Если постановка уведомления упадёт — а она ходит в базу и в настройки, — ход
    обязан дойти до человека. Иначе починка наблюдаемости ломает ровно то, что
    наблюдает.
    """
    user_id, settings = _owner(storage, settings)
    agent = AgentRuntime(settings, storage)
    agent.llm = _WorkingModel()

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise RuntimeError("database is locked")

    agent.storage.enqueue_notification = boom  # type: ignore[method-assign]
    agent._tell_the_owner_the_model_is_silent(user_id)  # не должно бросить

    reply = asyncio.run(_chat(agent, user_id))
    assert str(reply.get("message") or reply.get("content") or "").strip()


def test_a_dead_system_wakes_the_owner_even_at_night(settings, storage) -> None:
    """Решение владельца 2026-08-03, прямым ответом: «отказ ВСЕЙ системы будит всегда».

    Довод его же: пока модель мертва, каждый пишущий получает не молчание, а
    испорченные ответы. В живом отказе этих суток человек за двадцать минут
    получил восемь таких и перестал писать вовсе. Ждать до восьми утра означало
    бы восемь часов того же самого.

    Первая редакция этой правки тихие часы соблюдала — и это было МОЁ решение
    вместо его. Спросила прямо, получила прямой ответ.

    Тихие часы остаются в силе для всего остального: сводок, хроники,
    напоминаний, состояния воркеров и резервных копий. Они дождутся утра и
    ничего за ночь не испортят.
    """
    user_id, settings = _owner(storage, settings)
    settings = replace(settings, quiet_hours_start=0, quiet_hours_end=23)
    agent = AgentRuntime(settings, storage)
    agent.llm = _SilentModel()

    agent._tell_the_owner_the_model_is_silent(user_id)

    queued = _queued(storage, user_id)
    assert queued, "система мертва ночью, и человек об этом не узнал"
    assert queued[0]["kind"] == "model_silent"


def test_the_night_alert_is_still_one_message(settings, storage) -> None:
    """Разбудить один раз — решение владельца. Разбудить восемь — нет.

    Ночью цена шквала выше: дедуп по пятнадцатиминутному ведру здесь не
    украшение, а условие, при котором решение «будить всегда» вообще разумно.
    """
    user_id, settings = _owner(storage, settings)
    settings = replace(settings, quiet_hours_start=0, quiet_hours_end=23)
    agent = AgentRuntime(settings, storage)
    agent.llm = _SilentModel()

    for _ in range(8):
        agent._tell_the_owner_the_model_is_silent(user_id)

    assert len(_queued(storage, user_id)) == 1, "ночной шквал уведомлений"


def test_a_stranger_is_not_told_about_the_host(settings, storage) -> None:
    """Служебное — только владельцу: заказ 2026-08-02, правило общее на все органы."""
    _, settings = _owner(storage, settings, chat_id="100500")
    storage.ensure_user("guest", preset_key="user")
    storage.update_user("guest", metadata_json={"chat_id": "999"})
    agent = AgentRuntime(settings, storage)
    agent.llm = _SilentModel()

    agent._tell_the_owner_the_model_is_silent("guest")

    assert _queued(storage, "guest") == [], "состояние машины владельца ушло постороннему"
    assert _queued(storage, "boss"), "владельцу при этом сказать забыли"


def test_the_alert_is_wired_into_the_turn() -> None:
    """Механизм, который никто не зовёт, работой не является."""
    import inspect

    source = inspect.getsource(AgentRuntime.chat)
    assert "_tell_the_owner_the_model_is_silent(" in source
    assert 'response.get("llm_failed")' in source, "тревога поднимается не по факту отказа"


@pytest.mark.parametrize("enabled", [False])
def test_a_disabled_model_is_not_a_failure(settings, storage, enabled: bool) -> None:
    """Выключенная модель — настройка человека, а не поломка связи."""
    user_id, settings = _owner(storage, settings)
    agent = AgentRuntime(settings, storage)

    class _Off:
        pass

    agent.llm = _Off()
    agent.llm.enabled = enabled
    asyncio.run(_chat(agent, user_id))

    assert _queued(storage, user_id) == []
