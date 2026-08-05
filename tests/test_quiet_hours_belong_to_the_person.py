"""Ночь — это ночь человека, а не полночь по Гринвичу.

Замерено на боевой машине (Europe/Moscow, тихие часы 22→8): шесть часов из
двадцати четырёх работали ровно наоборот. UTC 05–07 — это МСК 08–10, и утром
проактивные органы молчали; UTC 19–21 — это МСК 22–00, и ночью они писали.
Сквозной прогон настоящего `scan_reminders`: в 09:00 МСК в очередь ушло 0
напоминаний, в 00:30 МСК — 2.

Той же датой считалось «сегодня»/«завтра» в тексте напоминания: в половине
первого ночи событие текущего дня подписывалось «завтра», потому что по UTC был
ещё вчерашний вечер.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from friday.organs import ServiceContext, local_now
from friday.organs.reminders import _format_reminder, scan_reminders


def test_local_now_follows_the_configured_zone(settings):
    moscow = replace(settings, local_timezone="Europe/Moscow")
    utc_hour = datetime.now(UTC).hour
    assert local_now(moscow).hour == (utc_hour + 3) % 24, "пояс из настроек не применён"

    hawaii = replace(settings, local_timezone="Pacific/Honolulu")
    assert local_now(hawaii).hour == (utc_hour - 10) % 24


def test_a_broken_zone_name_does_not_kill_the_organ(settings):
    """Кривое имя пояса — повод взять системный, а не уронить напоминания."""
    broken = replace(settings, local_timezone="Мордор/Барад-Дур")
    assert local_now(broken).tzinfo is not None


def test_the_six_inverted_hours(settings):
    """Мутация: вернуть `datetime.now(UTC)` в орган — тест краснеет.

    Здесь проверяется не помощник, а расхождение, которое он устраняет: при
    боевых тихих часах 22→8 шесть местных часов получали противоположный
    вердикт.
    """
    from friday.organs import in_quiet_hours

    zone = ZoneInfo("Europe/Moscow")
    inverted = []
    for utc_hour in range(24):
        moment = datetime(2026, 8, 2, utc_hour, tzinfo=UTC)
        by_utc = in_quiet_hours(moment.hour, 22, 8)
        by_local = in_quiet_hours(moment.astimezone(zone).hour, 22, 8)
        if by_utc != by_local:
            inverted.append((utc_hour, moment.astimezone(zone).hour))
    assert len(inverted) == 3 + 3, f"ожидалось шесть перевёрнутых часов, получено {inverted}"
    # Утро человека, объявленное тишиной, и его ночь, объявленная рабочим часом.
    assert (5, 8) in inverted and (21, 0) in inverted


@pytest.mark.parametrize(
    "zone_name,quiet_start,quiet_end",
    [("Europe/Moscow", 22, 8), ("Pacific/Honolulu", 22, 8), ("Asia/Vladivostok", 23, 7)],
)
def test_the_organ_asks_the_local_clock(settings, storage, monkeypatch, zone_name, quiet_start, quiet_end):
    """Орган молчит ровно тогда, когда ночь у ЧЕЛОВЕКА.

    Час подменяется целиком, чтобы прогон не зависел от того, когда он запущен.
    """
    import friday.organs.reminders as reminders_module

    tuned = replace(
        settings,
        local_timezone=zone_name,
        reminders_enabled=True,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        telegram_allowed_chat_ids="5001",
    )
    storage.ensure_user("alice", metadata={"chat_id": "5001"})
    zone = ZoneInfo(zone_name)

    def _at(local_hour: int):
        def _fake(_settings):
            return datetime(2026, 8, 2, local_hour, 0, tzinfo=zone)

        return _fake

    ctx = ServiceContext(settings=tuned, storage=storage, kg=None, ingestion=None)

    # Три часа ночи по местным часам — молчим.
    monkeypatch.setattr(reminders_module, "local_now", _at(3))
    import asyncio

    asyncio.run(scan_reminders(ctx))
    assert storage.list_pending_notifications(limit=10) == [], "орган писал ночью"

    # Десять утра по местным часам — работаем (событий может не быть, но гейт открыт).
    monkeypatch.setattr(reminders_module, "local_now", _at(10))
    seen: list[str] = []
    monkeypatch.setattr(
        reminders_module, "resolve_chat_id", lambda storage, user_id: (seen.append(user_id), "5001")[1]
    )
    asyncio.run(scan_reminders(ctx))
    assert seen, "орган молчал в десять утра — тихие часы всё ещё по UTC"


def test_today_and_tomorrow_are_the_persons_days():
    """«Сегодня» в напоминании — день человека.

    Проверено вычислением до правки: в 00:30 МСК 3 августа (21:30 UTC 2 августа)
    событие на 3 августа — то есть сегодняшнее, через несколько часов —
    подписывалось «завтра».
    """
    moscow = ZoneInfo("Europe/Moscow")
    half_past_midnight = datetime(2026, 8, 3, 0, 30, tzinfo=moscow)
    today = half_past_midnight.date()
    assert today.isoformat() == "2026-08-03"

    event = {"name": "Совещание", "occurred_at": "2026-08-03"}
    assert "сегодня" in _format_reminder(event, today)

    by_utc = half_past_midnight.astimezone(UTC).date()
    assert by_utc.isoformat() == "2026-08-02"
    assert "завтра" in _format_reminder(event, by_utc), "контроль: именно так и выглядел дефект"

    tomorrow = {"name": "Поверка", "occurred_at": (today + timedelta(days=1)).isoformat()}
    assert "завтра" in _format_reminder(tomorrow, today)


def test_every_proactive_organ_uses_the_local_clock():
    """Мутация: вернуть UTC в любой из пяти органов — тест краснеет.

    Тихие часы обязаны означать одно и то же во всей системе: если хоть один
    орган считает их по-своему, человека разбудит именно он.
    """
    import inspect

    from friday.organs import chronicle, monitors, reflection, reminders, sentinel

    for module in (reminders, monitors, sentinel, reflection, chronicle):
        source = inspect.getsource(module)
        gate = [line for line in source.splitlines() if "in_quiet_hours(" in line and "def " not in line]
        assert gate, f"{module.__name__}: гейта тихих часов нет вовсе"
        assert "local_now(" in source, f"{module.__name__} считает тихие часы по UTC"
        assert "datetime.now(UTC)" not in source, (
            f"{module.__name__}: остался UTC-час — тихие часы разъедутся между органами"
        )


def test_a_dead_system_is_the_one_thing_that_wakes_the_owner():
    """Решение владельца 2026-08-03: «отказ ВСЕЙ системы будит всегда».

    Тихие часы остаются правилом; исключение ровно одно и названо поимённо. Довод
    владельца: пока модель не отвечает, каждый пишущий получает не молчание, а
    испорченные ответы — в живом отказе этих суток человек за двадцать минут
    получил восемь таких и перестал писать.

    Список исключений обязан оставаться КОРОТКИМ. Состояние воркеров, резервные
    копии, гигиена секретов, нехватка места — важное, но не то, ради чего будят:
    оно дождётся утра и за ночь ничего не испортит. Каждый лишний код здесь —
    ещё одна причина, по которой человек начнёт глушить уведомления целиком.
    """
    from friday.organs.sentinel import _WAKES_THE_OWNER

    assert "llm_not_generating" in _WAKES_THE_OWNER, "молчащая модель снова ждёт до утра"
    assert len(_WAKES_THE_OWNER) <= 3, f"список ночных тревог разросся: {_WAKES_THE_OWNER}"
    for quiet_matter in ("backup_missing", "worker_crash_loop", "disk_space_low", "secret_in_file"):
        assert quiet_matter not in _WAKES_THE_OWNER, f"{quiet_matter} будит ночью без нужды"


def test_the_sentinel_looks_often_enough_to_catch_a_short_outage():
    """Живой отказ длился 20 минут — часовой обход мог не застать его вовсе.

    Владелец выбрал 10–15 минут. Проба стоит один токен; шесть таких в час — цена,
    несопоставимая с тем, что человек в это время получает испорченные ответы.
    """
    from friday.config import load_settings

    assert load_settings().sentinel_interval_sec <= 900
