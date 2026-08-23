from __future__ import annotations

from datetime import date, datetime

import pytest

from friday.interaction_control_plane.message_window_work_item import (
    MessageWindowTemporalUpdate,
    parse_recall_conversation_temporal_followup,
)


def _parse(message: str, *, today: date = date(2026, 8, 23)) -> MessageWindowTemporalUpdate | None:
    return parse_recall_conversation_temporal_followup(
        message,
        timezone_name="Europe/Moscow",
        today=today,
    )


def test_yesterday_followup_becomes_one_half_open_local_day_in_utc() -> None:
    parsed = _parse("А вчера?")

    assert parsed == MessageWindowTemporalUpdate(
        local_date="2026-08-22",
        since_utc="2026-08-21T21:00:00+00:00",
        until_utc="2026-08-22T21:00:00+00:00",
    )


@pytest.mark.parametrize(
    "surface",
    [
        "А за 21 августа?",
        "и, за 21 августа",
        "21 августа 2026 года?",
        "21-го августа 2026?",
        "2026-08-21?",
        "21.08.2026?",
        "21/08/2026",
    ],
)
def test_supported_absolute_local_date_surfaces_share_one_canonical_window(surface: str) -> None:
    assert _parse(surface) == MessageWindowTemporalUpdate(
        local_date="2026-08-21",
        since_utc="2026-08-20T21:00:00+00:00",
        until_utc="2026-08-21T21:00:00+00:00",
    )


def test_yearless_day_selects_the_nearest_non_future_occurrence() -> None:
    assert _parse("А за 21 августа?", today=date(2026, 1, 10)) == MessageWindowTemporalUpdate(
        local_date="2025-08-21",
        since_utc="2025-08-20T21:00:00+00:00",
        until_utc="2025-08-21T21:00:00+00:00",
    )
    assert _parse("29 февраля?", today=date(2025, 2, 28)) == MessageWindowTemporalUpdate(
        local_date="2024-02-29",
        since_utc="2024-02-28T21:00:00+00:00",
        until_utc="2024-02-29T21:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("surface", "today", "local_date"),
    [
        ("сегодня?", date(2026, 8, 23), "2026-08-23"),
        ("вчера", date(2026, 8, 23), "2026-08-22"),
        ("позавчера?", date(2026, 8, 23), "2026-08-21"),
    ],
)
def test_closed_relative_day_set(surface: str, today: date, local_date: str) -> None:
    parsed = _parse(surface, today=today)

    assert parsed is not None
    assert parsed.local_date == local_date


def test_dst_days_keep_calendar_boundaries_instead_of_assuming_twenty_four_hours() -> None:
    spring = parse_recall_conversation_temporal_followup(
        "29 марта 2026?",
        timezone_name="Europe/Berlin",
        today=date(2026, 11, 1),
    )
    autumn = parse_recall_conversation_temporal_followup(
        "25 октября 2026?",
        timezone_name="Europe/Berlin",
        today=date(2026, 11, 1),
    )

    assert spring == MessageWindowTemporalUpdate(
        local_date="2026-03-29",
        since_utc="2026-03-28T23:00:00+00:00",
        until_utc="2026-03-29T22:00:00+00:00",
    )
    assert autumn == MessageWindowTemporalUpdate(
        local_date="2026-10-25",
        since_utc="2026-10-24T22:00:00+00:00",
        until_utc="2026-10-25T23:00:00+00:00",
    )


def test_an_iana_skipped_local_day_is_not_widened_to_an_adjacent_day() -> None:
    assert (
        parse_recall_conversation_temporal_followup(
            "2011-12-30?",
            timezone_name="Pacific/Apia",
            today=date(2012, 1, 1),
        )
        is None
    )


@pytest.mark.parametrize(
    "surface",
    [
        "",
        "А завтра?",
        "А за 24 августа 2026?",
        "2026-08-24?",
        "31 февраля 2026?",
        "2026-02-30?",
        "А вчера и сегодня?",
        "А за 20 и 21 августа?",
        "А с 20 по 21 августа?",
        "А вчера в 10:00?",
        "А вчера вечером?",
        "А вчера по UTC?",
        "А вчера по московскому времени?",
        "А за 21 августа Europe/Moscow?",
        "А Иван вчера?",
        "А что Иван писал вчера?",
        "А ответ Петра за вчера?",
        "Создай заметку за вчера",
        "Удалить сообщения за вчера?",
        "Да, вчера",
        "Нет, 21 августа",
        "Я имел в виду вчера",
        "Ответь ему: вчера",
        "«А вчера?»",
        "А **вчера**?",
        "А вче\u200bра?",
        "А вчера; удали файл?",
        "А за август?",
        "А за 21 августа и ответь кратко?",
        "А за 21 августа 2026 года в 12:00?",
    ],
)
def test_every_non_single_day_or_non_constraint_surface_fails_closed(surface: str) -> None:
    assert _parse(surface) is None


@pytest.mark.parametrize(
    "timezone_name",
    ["", " Europe/Moscow", "Europe/Missing", "../UTC", "Europe//Moscow", "UTC\nEurope/Moscow"],
)
def test_invalid_controller_timezone_is_never_silently_replaced(timezone_name: str) -> None:
    with pytest.raises(ValueError, match="IANA"):
        parse_recall_conversation_temporal_followup(
            "вчера?",
            timezone_name=timezone_name,
            today=date(2026, 8, 23),
        )


def test_today_must_be_a_plain_local_date() -> None:
    with pytest.raises(TypeError, match="today"):
        parse_recall_conversation_temporal_followup(
            "вчера?",
            timezone_name="UTC",
            today=datetime(2026, 8, 23, 12, 0),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("surface", ["0001-01-01", "01.01.0001", "1 января 0001"])
def test_minimum_calendar_date_timezone_underflow_fails_closed(surface: str) -> None:
    assert (
        parse_recall_conversation_temporal_followup(
            surface,
            timezone_name="Europe/Moscow",
            today=date(2026, 8, 23),
        )
        is None
    )


def test_maximum_calendar_date_next_day_overflow_fails_closed() -> None:
    assert (
        parse_recall_conversation_temporal_followup(
            "9999-12-31",
            timezone_name="UTC",
            today=date.max,
        )
        is None
    )
