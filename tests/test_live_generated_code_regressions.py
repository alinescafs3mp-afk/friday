"""Executable-answer regressions copied from synthetic forms of live failures."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    _BRAINFUCK_PUBLICATION_UTF16_LIMIT,
    _BRAINFUCK_TOO_LONG_RESPONSE,
    AgentContext,
    AgentRuntime,
    _brainfuck_exact_output_target,
    _brainfuck_exact_response,
)
from friday.permissions import ActorContext
from friday.telegram_bridge._base import TELEGRAM_TEXT_LIMIT, split_for_telegram, utf16_length

LIVE_BRAINFUCK_REQUEST = "а hello world на brainfuck напишешь?"
LONG_BRAINFUCK_REQUEST = f"напиши на brainfuck «{'😀' * 32}»"
LIVE_INVALID_BRAINFUCK = (
    "++++++++[>++++[>++>+++>+++>+<<<<-]>+>+>->>+[>]<-]>>.>---.+++++++..+++.>>.<-.<.+++.------.--------.>+.>"
)


def _brainfuck_stdout(program: str, *, max_steps: int = 100_000) -> bytes:
    """Execute the eight-instruction language in a bounded test-only oracle."""

    tape = [0] * 30_000
    pointer = 0
    output = bytearray()
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, instruction in enumerate(program):
        if instruction == "[":
            stack.append(index)
        elif instruction == "]":
            if not stack:
                raise AssertionError("unmatched closing bracket")
            opening = stack.pop()
            pairs[opening] = index
            pairs[index] = opening
    if stack:
        raise AssertionError("unmatched opening bracket")

    cursor = 0
    steps = 0
    while cursor < len(program):
        steps += 1
        if steps > max_steps:
            raise AssertionError("brainfuck program exceeded the deterministic test budget")
        instruction = program[cursor]
        if instruction == ">":
            pointer += 1
            if pointer >= len(tape):
                raise AssertionError("brainfuck pointer left the bounded tape")
        elif instruction == "<":
            pointer -= 1
            if pointer < 0:
                raise AssertionError("brainfuck pointer left the bounded tape")
        elif instruction == "+":
            tape[pointer] = (tape[pointer] + 1) % 256
        elif instruction == "-":
            tape[pointer] = (tape[pointer] - 1) % 256
        elif instruction == ".":
            output.append(tape[pointer])
        elif instruction == ",":
            tape[pointer] = 0
        elif (instruction == "[" and tape[pointer] == 0) or (instruction == "]" and tape[pointer] != 0):
            cursor = pairs[cursor]
        cursor += 1
    return bytes(output)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("а hello world на brainfuck напишешь?", b"Hello World!\n"),
        ("напиши hello world на brainfuck", b"Hello World!\n"),
        ("закодируй снег на brainfuck", "снег".encode()),
        ("напиши на Brainfuck «OK»", b"OK"),
        ('напиши на Brainfuck "hello world"', b"hello world"),
        ("напиши на Brainfuck «hello world»", b"hello world"),
        ("напиши на Brainfuck 'hello world!'", b"hello world!"),
        ("напиши на Brainfuck “OK”", b"OK"),
        ("напиши на Brainfuck « hello world »", b" hello world "),
        ("напиши на Brainfuck «текст hello world»", "текст hello world".encode()),
        ("снег на brainfuck закодируешь?", "снег".encode()),
    ],
)
def test_closed_brainfuck_compiler_proves_each_requested_stdout(
    message: str,
    expected: bytes,
) -> None:
    response = _brainfuck_exact_response(message)
    fenced = re.search(r"```brainfuck\s*([+\-<>\[\].,]+)\s*```", response)

    assert _brainfuck_exact_output_target(message) == expected
    assert fenced is not None
    assert _brainfuck_stdout(fenced.group(1)) == expected
    assert _BRAINFUCK_PUBLICATION_UTF16_LIMIT == TELEGRAM_TEXT_LIMIT
    assert utf16_length(response) <= TELEGRAM_TEXT_LIMIT
    assert split_for_telegram(response) == [response]


@pytest.mark.parametrize(
    "message",
    [
        "расскажи про brainfuck",
        "hello world на python напишешь?",
        "напиши программу на brainfuck, которая читает ввод",
        "hello world на brainfuck; потом отправь файл",
        "покажи hello world на brainfuck",
        "что выведет hello world на brainfuck?",
        "напиши программу hello world на brainfuck",
        "сделай код hello world на brainfuck",
        "напиши на brainfuck текст hello world",
        "напиши на brainfuck строку hello world",
        "напиши на brainfuck слово hello world",
        "можешь написать OK на brainfuck?",
        "создай код, который выводит OK, на brainfuck",
        "напиши пожалуйста OK на brainfuck",
    ],
)
def test_brainfuck_output_contract_does_not_claim_adjacent_requests(message: str) -> None:
    assert _brainfuck_exact_output_target(message) is None
    assert _brainfuck_exact_response(message) == ""


def test_too_long_brainfuck_program_gets_one_transport_safe_owned_refusal() -> None:
    assert _brainfuck_exact_output_target(LONG_BRAINFUCK_REQUEST) == ("😀" * 32).encode()

    response = _brainfuck_exact_response(LONG_BRAINFUCK_REQUEST)

    assert response == _BRAINFUCK_TOO_LONG_RESPONSE
    assert "```" not in response
    assert utf16_length(response) <= TELEGRAM_TEXT_LIMIT
    assert split_for_telegram(response) == [response]


class _LiveInvalidBrainfuckModel:
    enabled = True
    model = "synthetic-live-invalid-brainfuck"
    total_budget_sec = 2.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        del messages, tools
        return {"content": (f"Конечно:\n\n```\n{LIVE_INVALID_BRAINFUCK}\n```\n\nКлассический вариант.")}


@pytest.mark.asyncio
async def test_live_brainfuck_hello_world_is_not_published_until_its_output_matches(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plausible fenced program is a checkable claim, not trusted prose."""

    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="live code validation")
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "OLD-PRIVATE-HISTORY-MUST-NOT-AUTHORIZE-CODE",
        metadata={"private_context_lineage": True},
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_LiveInvalidBrainfuckModel(),
    )

    async def bounded_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            conversation_history=[],
            search_query=message,
            outward_verdict=("знание", None),
        )

    monkeypatch.setattr(runtime, "_prepare_context", bounded_context)
    reply = await runtime.chat(
        "alice",
        LIVE_BRAINFUCK_REQUEST,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=str(conversation["id"]),
        attachments=[],
        enable_tools=False,
    )

    fenced = re.search(
        r"```(?:brainfuck)?\s*([+\-<>\[\].,]+)\s*```",
        str(reply["message"]),
        re.IGNORECASE,
    )
    assert fenced is not None, "the requested program disappeared instead of being corrected"
    assert _brainfuck_stdout(fenced.group(1)) == b"Hello World!\n"


@pytest.mark.asyncio
async def test_live_too_long_brainfuck_program_never_falls_through_to_the_model(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="long code validation")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_LiveInvalidBrainfuckModel(),
    )

    async def bounded_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            conversation_history=[],
            search_query=message,
            outward_verdict=("знание", None),
        )

    monkeypatch.setattr(runtime, "_prepare_context", bounded_context)
    reply = await runtime.chat(
        "alice",
        LONG_BRAINFUCK_REQUEST,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=str(conversation["id"]),
        attachments=[],
        enable_tools=False,
    )

    assert reply["message"] == _BRAINFUCK_TOO_LONG_RESPONSE
    assert split_for_telegram(str(reply["message"])) == [reply["message"]]
