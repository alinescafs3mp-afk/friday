"""Explicit text-shape requests reach the model as an exact, static contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from friday.agent_runtime import _TEXT_SHAPE_GUIDANCE, AgentContext, AgentRuntime

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "question",
    [
        "Напиши одно предложение с безопасными символами меньше и больше.",
        "Сделай короткую Markdown-цитату о тестовом сообщении.",
        "Подготовь компактный список из трёх тестовых слов.",
        "Сформируй краткий ответ с выделением слова «готово».",
        "Передай контроль рядом с угловыми скобками как обычным текстом.",
        "Финальная доставка должна содержать два нейтральных пункта.",
    ],
)
def test_explicit_text_shape_contract_is_adjacent_to_the_user_turn(
    settings,
    storage,
    question: str,
) -> None:
    runtime = AgentRuntime(settings, storage)
    context = AgentContext(conversation_id="shape-contract", user_id="alice")

    messages = runtime._build_initial_messages(  # noqa: SLF001
        context,
        question,
        None,
        tool_enabled=False,
    )

    assert messages[-2] == {"role": "system", "content": _TEXT_SHAPE_GUIDANCE}
    assert messages[-1] == {"role": "user", "content": question}


@pytest.mark.parametrize(
    "question",
    [
        "Что произошло 1 мая 2024 года?",
        "Переведи фразу «сделай короткий Markdown-список».",
        "Покажи документы по проекту.",
        "Напиши canary о доступности сервиса.",
    ],
)
def test_unrelated_turns_do_not_receive_a_format_only_system_instruction(
    settings,
    storage,
    question: str,
) -> None:
    runtime = AgentRuntime(settings, storage)
    context = AgentContext(conversation_id="ordinary-turn", user_id="alice")

    messages = runtime._build_initial_messages(  # noqa: SLF001
        context,
        question,
        None,
        tool_enabled=False,
    )

    assert all(item.get("content") != _TEXT_SHAPE_GUIDANCE for item in messages)
    assert messages[-1] == {"role": "user", "content": question}


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_every_frozen_transport_shape_request_receives_the_static_contract(
    settings,
    storage,
    battery_id: str,
) -> None:
    manifest = json.loads(
        (FIXTURES / f"synthetic_live_battery_{battery_id.casefold()}.json").read_text(encoding="utf-8")
    )
    questions = next(
        item["questions"] for item in manifest["passes"] if item["pass_id"] == f"{battery_id}-P10"
    )
    runtime = AgentRuntime(settings, storage)

    for question in questions:
        messages = runtime._build_initial_messages(  # noqa: SLF001
            AgentContext(conversation_id="frozen-shape", user_id="alice"),
            question,
            None,
            tool_enabled=False,
        )
        assert messages[-2] == {"role": "system", "content": _TEXT_SHAPE_GUIDANCE}
        assert messages[-1] == {"role": "user", "content": question}
