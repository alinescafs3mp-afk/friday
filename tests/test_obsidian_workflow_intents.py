from __future__ import annotations

from datetime import date

import pytest

from friday.organs.obsidian.workflow_intents import (
    WORKFLOW_READ_TOOL,
    WORKFLOW_WRITE_TOOL,
    parse_obsidian_workflow_intent,
)

TODAY = date(2026, 8, 22)


@pytest.mark.parametrize(
    ("message", "tool", "action"),
    [
        (
            "Добавь в сегодняшнюю заметку задачу проверить поиск в Obsidian завтра в 10 утра.",
            WORKFLOW_WRITE_TOOL,
            "add_task",
        ),
        ("Покажи незавершённые задачи про Obsidian.", WORKFLOW_READ_TOOL, "search_tasks"),
        ("Открой вторую.", WORKFLOW_READ_TOOL, "select_candidate"),
        (
            "Добавь туда раздел «Следующие шаги» и пункт про проверку семантического индекса.",
            WORKFLOW_WRITE_TOOL,
            "append_active_section",
        ),
        ("Какие заметки ссылаются на `Projects/Friday`?", WORKFLOW_READ_TOOL, "backlinks"),
        (
            "Перемести `Projects/Friday.md` в `Architecture/Friday.md` и обнови ссылки на неё.",
            WORKFLOW_WRITE_TOOL,
            "move_note",
        ),
        (
            "Какие заметки теперь ссылаются на архитектуру Friday?",
            WORKFLOW_READ_TOOL,
            "backlinks",
        ),
        (
            "Создай по шаблону Meeting заметку о проверке интеграции Obsidian. Проект Friday, "
            "участники Алиса и Борис. В обсуждение добавь, что базовая синхронизация работает. "
            "В действия добавь задачу проверить конфликты.",
            WORKFLOW_WRITE_TOOL,
            "create_from_template",
        ),
        (
            "Сохрани краткие итоги нашего текущего разговора в Obsidian. Создай заметку "
            "`Research/Conversation Summary.md`, отдельно укажи выводы, нерешённые вопросы и "
            "следующие действия.",
            WORKFLOW_WRITE_TOOL,
            "save_summary",
        ),
        (
            "Добавь туда ссылки на заметки, которые мы сегодня использовали.",
            WORKFLOW_WRITE_TOOL,
            "append_summary_links",
        ),
        (
            "Создай Base `Friday Active Notes`, который показывает заметки проекта Friday со "
            "статусом не `done`. Выведи название, статус и дату изменения.",
            WORKFLOW_WRITE_TOOL,
            "create_base",
        ),
        (
            "Покажи актуальные заметки из Base `Friday Active Notes`.",
            WORKFLOW_READ_TOOL,
            "query_base",
        ),
        (
            "Замени раздел «Проверка дополнения» текстом: «Версия, записанная Friday».",
            WORKFLOW_WRITE_TOOL,
            "replace_active_section",
        ),
        (
            "Покажи различия и собери объединённую версию, сохранив оба изменения.",
            WORKFLOW_READ_TOOL,
            "conflict_preview",
        ),
        (
            "Прими эту объединённую версию.",
            WORKFLOW_WRITE_TOOL,
            "accept_conflict_merge",
        ),
        ("Продолжай предыдущую задачу.", WORKFLOW_WRITE_TOOL, "resume_previous"),
        (
            "Удали тестовую заметку `Scratch/Delete Me.md`.",
            WORKFLOW_WRITE_TOOL,
            "delete_note",
        ),
    ],
)
def test_battery_workflow_intents(message: str, tool: str, action: str) -> None:
    intent = parse_obsidian_workflow_intent(message, today=TODAY)

    assert intent is not None
    assert intent.tool_name == tool
    assert intent.arguments["action"] == action


def test_metadata_is_one_atomic_structured_workflow() -> None:
    intent = parse_obsidian_workflow_intent(
        "У заметки `Projects/Friday Test.md` поставь статус `review`, проект `Friday` и "
        "добавь теги `integration`, `obsidian` и `test`.",
        today=TODAY,
    )

    assert intent is not None
    assert intent.tool_name == WORKFLOW_WRITE_TOOL
    assert intent.arguments == {
        "action": "update_metadata",
        "path": "Projects/Friday Test.md",
        "status": "review",
        "project": "Friday",
        "tags": ["integration", "obsidian", "test"],
    }


def test_live_metadata_wording_accepts_safe_unquoted_literals() -> None:
    intent = parse_obsidian_workflow_intent(
        "У заметки Projects/Friday Test.md поставь статус review, проект Friday и "
        "добавь теги integration, obsidian и test.",
        today=TODAY,
    )

    assert intent is not None
    assert intent.tool_name == WORKFLOW_WRITE_TOOL
    assert intent.arguments == {
        "action": "update_metadata",
        "path": "Projects/Friday Test.md",
        "status": "review",
        "project": "Friday",
        "tags": ["integration", "obsidian", "test"],
    }


def test_temporal_task_and_recovery_daily_values_are_concrete() -> None:
    task = parse_obsidian_workflow_intent(
        "Добавь в сегодняшнюю заметку задачу проверить поиск в Obsidian завтра в 10 утра.",
        today=TODAY,
    )
    append = parse_obsidian_workflow_intent(
        "Добавь в ежедневную заметку строку «Проверка идемпотентности».",
        today=TODAY,
    )

    assert task is not None and task.arguments["due_date"] == "2026-08-23"
    assert task.arguments["due_time"] == "10:00"
    assert append is not None and append.tool_name == "obsidian_daily_note"
    assert append.arguments == {"day": "2026-08-22", "content": "Проверка идемпотентности"}


def test_approximate_date_search_preserves_the_date_constraint() -> None:
    intent = parse_obsidian_workflow_intent(
        "Найди заметку про проблемы поиска, которую я делал примерно в начале августа 2026 года.",
        today=TODAY,
    )

    assert intent is not None and intent.tool_name == "obsidian_search_notes"
    assert intent.arguments == {
        "query": "проблемы поиска, которую я делал примерно в начале августа 2026 года",
        "limit": 20,
    }


def test_live_approximate_date_search_infers_the_current_year() -> None:
    intent = parse_obsidian_workflow_intent(
        "Найди заметку про проблемы поиска, которую я делал примерно в начале августа.",
        today=TODAY,
    )

    assert intent is not None and intent.tool_name == "obsidian_search_notes"
    assert intent.arguments == {
        "query": "проблемы поиска, которую я делал примерно в начале августа 2026 года",
        "limit": 20,
    }


@pytest.mark.parametrize(
    "message",
    [
        "Обобщи все мои сегодняшние заметки в обсидиан",
        "Обобщи все мои сегодняшние заметки в obsidian",
    ],
)
def test_live_today_note_summary_phrasings_bind_the_exact_local_day(message: str) -> None:
    intent = parse_obsidian_workflow_intent(message, today=TODAY)

    assert intent is not None
    assert intent.tool_name == WORKFLOW_READ_TOOL
    assert intent.arguments == {
        "action": "summarize_today_notes",
        "day": "2026-08-22",
    }
    assert intent.explicit_path == ""


@pytest.mark.parametrize(
    "message",
    [
        "Не удаляй тестовую заметку `Scratch/Delete Me.md`.",
        "Объясни фразу: Открой вторую.",
        "Добавь туда что-нибудь.",
        "Да, применяй.",
        "Перемести `../outside.md` в `Safe.md` и обнови ссылки на неё.",
    ],
)
def test_incomplete_meta_or_unsafe_workflows_fail_closed(message: str) -> None:
    assert parse_obsidian_workflow_intent(message, today=TODAY) is None
