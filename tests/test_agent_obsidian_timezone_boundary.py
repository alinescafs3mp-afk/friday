"""Calendar-boundary proof for deterministic Obsidian commands."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import AgentRuntime
from friday.organs.obsidian.conversation import obsidian_conversation_intent

_CREATE_NOTE = (
    "Создай в Obsidian заметку `Projects/Friday Test.md`. "
    "Заголовок: «Тест интеграции Friday». Внутри напиши, что заметка создана "
    "через Telegram, и добавь текущую дату."
)


def test_exact_obsidian_create_uses_berlin_date_across_the_utc_midnight_boundary(
    monkeypatch,
) -> None:
    """21 August UTC is already 22 August in the configured Berlin zone."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            instant = cls(2026, 8, 21, 22, 30, tzinfo=UTC)
            return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(agent_runtime_module, "datetime", FrozenDateTime)
    runtime = object.__new__(AgentRuntime)
    runtime.settings = SimpleNamespace(local_timezone="Europe/Berlin")

    local_day = runtime._local_today()
    intent = obsidian_conversation_intent(_CREATE_NOTE, today=local_day)

    assert local_day.isoformat() == "2026-08-22"
    assert intent is not None and not intent.error
    assert intent.tool_name == "obsidian_create_note"
    assert intent.explicit_path == "Projects/Friday Test.md"
    assert intent.direct_arguments == {
        "path": "Projects/Friday Test.md",
        "content": ("# Тест интеграции Friday\n\nЗаметка создана через Telegram.\n\n2026-08-22\n"),
    }
    assert intent.resolved_local_date == "2026-08-22"
