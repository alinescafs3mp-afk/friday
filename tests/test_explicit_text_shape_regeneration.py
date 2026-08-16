from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import friday.agent_runtime as runtime_module
import friday.text_shape as text_shape_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _attachment_reference_kind,
    _intra_file_record_set_count,
    file_turn_authority,
)
from friday.permissions import ActorContext
from friday.telegram_bridge import TelegramBridge
from friday.text_shape import (
    TEXT_SHAPE_INVALID,
    TEXT_SHAPE_UNOWNED,
    TEXT_SHAPE_VALID,
    explicit_text_shape_status,
    regenerable_text_shape_contract,
    render_structured_list_regeneration,
    repair_explicit_text_shape,
)

RU_WORD_LIST = "Составь список из трёх слов. Включи маркер MARK-SEG-12. Контроль CTRL-12."
RU_ITEM_LIST = "Оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20."
RU_SENTENCE = "Напиши ответ одним предложением. Включи маркер MARK-SEG-14. Контроль CTRL-14."
RU_BOLD_PHRASE = (
    "Сформируй одну короткую фразу с жирным Markdown-выделением для транспортного теста. "
    "Включи маркер SYN-TELEGRAM-A10-02. Контроль SYN-A10-02."
)
STRUCTURED_ITEMS = '["First item", "Second item"]'
STRUCTURED_LIST = "- First item MARK-SEG-20\n- Second item"
FORMAT_FAILURE_RU = "Не удалось сформировать ответ в запрошенном формате."
QUOTE_REQUEST = (
    "Return one quote and one separate explanation line. Include marker QUOTE-GAP-43. Control: CHECK-43."
)
QUOTE_COLLAPSED_DRAFT = "> “Short quote.” Explanation QUOTE-GAP-43. Control: CHECK-43."
QUOTE_EXACT_DRAFT = "> “Short quote.”\nExplanation QUOTE-GAP-43. Control: CHECK-43."
QUOTE_COLLAPSED = "> “Short quote.” Explanation QUOTE-GAP-43."
QUOTE_EXACT = "> “Short quote.”\nExplanation QUOTE-GAP-43."


def test_an_answer_list_shape_is_not_a_private_file_reference() -> None:
    assert _intra_file_record_set_count(RU_ITEM_LIST) is None
    assert _attachment_reference_kind(RU_ITEM_LIST) == ""
    assert not file_turn_authority(RU_ITEM_LIST).proved("local_read")

    # Genuine current-file record selectors retain the fail-closed file route.
    assert _intra_file_record_set_count("оба пункта") == 2
    assert _attachment_reference_kind("оба пункта") == "deictic"
    assert file_turn_authority("две строки этого файла").proved("local_read")


@pytest.mark.parametrize(
    ("user_text", "answer", "kind", "count"),
    [
        (
            RU_WORD_LIST,
            "- слово\n- MARK-SEG-12\n- другое",
            "list",
            3,
        ),
        (
            RU_ITEM_LIST,
            "- Первый пункт\n- Второй MARK-SEG-20",
            "list",
            2,
        ),
        (
            "Return a list of 10 items. Include marker MARK-SEG-10. Control: CTRL-10.",
            "\n".join(
                [
                    "1. marker item MARK-SEG-10",
                    *[f"{index}. item{index}" for index in range(2, 11)],
                ]
            ),
            "list",
            10,
        ),
        (
            RU_SENTENCE,
            "Готовый ответ MARK-SEG-14.",
            "single_sentence",
            None,
        ),
        (
            "Write a single sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Ready MARK-SEG-14.",
            "single_sentence",
            None,
        ),
    ],
)
def test_closed_regeneration_contract_accepts_only_complete_valid_shapes(
    user_text: str,
    answer: str,
    kind: str,
    count: int | None,
) -> None:
    contract = regenerable_text_shape_contract(user_text)

    assert contract is not None
    assert contract.kind == kind
    assert contract.count == count
    assert explicit_text_shape_status(user_text, answer) == TEXT_SHAPE_VALID


def test_closed_bold_phrase_contract_owns_the_full_transport_shape() -> None:
    contract = regenerable_text_shape_contract(RU_BOLD_PHRASE)

    assert contract is not None
    assert contract.kind == "single_sentence"
    assert contract.emphasis_style == "bold"
    assert (
        explicit_text_shape_status(
            RU_BOLD_PHRASE,
            "**Короткая транспортная фраза SYN-TELEGRAM-A10-02.**",
        )
        == TEXT_SHAPE_VALID
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Короткая транспортная фраза SYN-TELEGRAM-A10-02.",
        "*Короткая транспортная фраза SYN-TELEGRAM-A10-02.*",
        "__Короткая транспортная фраза SYN-TELEGRAM-A10-02.__",
        "**Короткая транспортная фраза SYN-TELELEGRAM-A10-02.**",
        "**Короткая транспортная фраза SYN-TELEGRAM-A10-02 SYN-TELEGRAM-A10-02.**",
        "**Короткая транспортная фраза SYN-TELEGRAM-A10-02.** снаружи",
        "снаружи **Короткая транспортная фраза SYN-TELEGRAM-A10-02.**",
        "****Короткая транспортная фраза SYN-TELEGRAM-A10-02.****",
        "**Короткая транспортная фраза SYN-TELEGRAM-A10-02.**\nлишняя строка",
        "**Отказываюсь включать SYN-TELEGRAM-A10-02.**",
    ],
)
def test_closed_bold_phrase_contract_rejects_marker_and_emphasis_mutations(answer: str) -> None:
    assert regenerable_text_shape_contract(RU_BOLD_PHRASE) is not None
    assert explicit_text_shape_status(RU_BOLD_PHRASE, answer) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize(
    "user_text",
    [
        (
            "Если получится, сформируй одну короткую фразу с жирным Markdown-выделением для "
            "транспортного теста. Включи маркер SYN-TELEGRAM-A10-02. Контроль SYN-A10-02."
        ),
        (
            "Сформируй одну короткую фразу без жирного Markdown-выделения для транспортного "
            "теста. Включи маркер SYN-TELEGRAM-A10-02. Контроль SYN-A10-02."
        ),
        (
            "Сформируй одну короткую фразу с жирным Markdown-выделением по данным файла. "
            "Включи маркер SYN-TELEGRAM-A10-02. Контроль SYN-A10-02."
        ),
        (
            "Сформируй две короткие фразы с жирным Markdown-выделением для транспортного теста. "
            "Включи маркер SYN-TELEGRAM-A10-02. Контроль SYN-A10-02."
        ),
    ],
)
def test_bold_phrase_regeneration_authority_stays_fail_closed(user_text: str) -> None:
    assert regenerable_text_shape_contract(user_text) is None


def test_structured_retry_renders_semantics_and_inserts_literal_in_code() -> None:
    request = "Оформи нумерованный список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20."
    contract = regenerable_text_shape_contract(request)
    assert contract is not None

    rendered = render_structured_list_regeneration(
        request,
        contract,
        ["Первый нейтральный пункт", "Второй нейтральный пункт"],
    )

    assert rendered == ("1. Первый нейтральный пункт MARK-SEG-20\n2. Второй нейтральный пункт")
    assert explicit_text_shape_status(request, rendered) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, rendered) == rendered


@pytest.mark.parametrize(
    ("user_text", "expected_style"),
    [
        (RU_ITEM_LIST, "bullet"),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
            "bullet",
        ),
        (
            "Оформи нумерованный список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
            "numbered",
        ),
    ],
)
def test_list_style_is_owned_by_the_closed_contract_parser(
    user_text: str,
    expected_style: str,
) -> None:
    contract = regenerable_text_shape_contract(user_text)

    assert contract is not None
    assert contract.list_style == expected_style


def test_ambiguous_list_style_is_not_owned() -> None:
    request = (
        "Оформи маркированный список из двух пунктов для нумерованного списка. "
        "Включи маркер MARK-SEG-20. Контроль CTRL-20."
    )

    assert regenerable_text_shape_contract(request) is None


@pytest.mark.parametrize(
    "items",
    [
        ["only one"],
        {"items": ["first", "second"]},
        "scalar",
        ["", "second"],
        [" first", "second"],
        ["first ", "second"],
        ["first MARK-SEG-20", "second"],
        ["first CTRL-20", "second"],
        ["first FOREIGN-SEG-99", "second"],
        ["first OTHER-99", "second"],
        ["first SYN-TEЛЕГРАМ-A10-03", "second"],
        ["- prefixed", "second"],
        ["**bold**", "second"],
        ["https://example.invalid", "second"],
        ["first\nextra", "second"],
        ["first\x00extra", "second"],
        ["first\textra", "second"],
        ["first\x1fextra", "second"],
        ["first\x7fextra", "second"],
        ["first\u0085extra", "second"],
        ["first\u2028extra", "second"],
        ["first\u2029extra", "second"],
        ["first\ud800extra", "second"],
        ["first\udfffextra", "second"],
        ["x" * 1_001, "second"],
        ["first", 2],
    ],
)
def test_structured_retry_rejects_nonsemantic_or_identifier_items(items: object) -> None:
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    assert (
        render_structured_list_regeneration(
            RU_ITEM_LIST,
            contract,
            items,
            source_text="FOREIGN-SEG-99 OTHER-99 SYN-TEЛЕГРАМ-A10-03",
        )
        == ""
    )


def test_structured_retry_forbids_only_draft_identifiers_not_grounded_by_request() -> None:
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    forbidden = text_shape_module._structured_regeneration_forbidden_identifiers(
        "Compare UTF-8 with RFC-822.",
        "Malformed UTF-8 and RFC-822 output with OTHER-99.",
        contract,
    )

    assert "utf-8" not in forbidden
    assert "rfc-822" not in forbidden
    assert "other-99" in forbidden
    assert contract.literal.casefold() in forbidden
    assert contract.control.casefold() in forbidden


def test_structured_retry_leaves_benign_dense_prose_to_the_final_validator() -> None:
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    rendered = render_structured_list_regeneration(
        RU_ITEM_LIST,
        contract,
        ["Use UTF-8 — it’s safe 😀", "It's ordinary prose"],
    )

    assert rendered == "- Use UTF-8 — it’s safe 😀 MARK-SEG-20\n- It's ordinary prose"
    assert explicit_text_shape_status(RU_ITEM_LIST, rendered) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(RU_ITEM_LIST, rendered) == rendered


def test_structured_word_list_asks_for_only_nonliteral_semantic_items() -> None:
    contract = regenerable_text_shape_contract(RU_WORD_LIST)
    assert contract is not None

    rendered = render_structured_list_regeneration(RU_WORD_LIST, contract, ["альфа", "бета"])

    assert rendered == "- MARK-SEG-12\n- альфа\n- бета"
    assert explicit_text_shape_status(RU_WORD_LIST, rendered) == TEXT_SHAPE_VALID


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (RU_WORD_LIST, "- one\n- two\n- three"),
        (RU_WORD_LIST, "- one MARK-SEG-12\n- two\n- CTRL-12"),
        (RU_WORD_LIST, "- one\n- MARK-SEG-12\n- CTRL-12\n- extra"),
        (RU_WORD_LIST, "1. one\n- MARK-SEG-12\n- CTRL-12"),
        (RU_WORD_LIST, "- one\n- MARK-SEG-12\n- CTRL-12 CTRL-12"),
        (RU_WORD_LIST, "- one\n- MARK-SEG-12\n- XCTRL-12Y"),
        (RU_ITEM_LIST, "- First\n- Second\nMARK-SEG-20 CTRL-20"),
        (RU_ITEM_LIST, "- First MARK-SEG-20\n- Second\nCTRL-20"),
        (RU_ITEM_LIST, "- First [MARK-SEG-20](https://invalid.example)\n- Second CTRL-20"),
        (RU_SENTENCE, "Первое MARK-SEG-14. Второе CTRL-14."),
        (RU_SENTENCE, "Первая строка MARK-SEG-14.\nВторая CTRL-14."),
        (RU_SENTENCE, "Отказываюсь добавлять MARK-SEG-14."),
    ],
)
def test_owned_regeneration_contract_rejects_malformed_or_ambiguous_answers(
    user_text: str,
    answer: str,
) -> None:
    assert regenerable_text_shape_contract(user_text) is not None
    assert explicit_text_shape_status(user_text, answer) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize(
    "answer",
    [
        "Готово без ошибок MARK-SEG-14.",
        "MARK-SEG-14 передан без ошибок.",
        "MARK-SEG-14 корректен, а не ошибочен.",
        "Ready without errors MARK-SEG-14.",
        "MARK-SEG-14 returned without errors.",
        "MARK-SEG-14 is not invalid.",
        "MARK-SEG-14 is not actually invalid.",
        "MARK-SEG-14 isn't wrong.",
        "MARK-SEG-14 is neither wrong nor invalid.",
        "MARK-SEG-14 is valid, not wrong.",
        "MARK-SEG-14 is correct rather than wrong.",
        "MARK-SEG-14 не является ошибочным.",
    ],
)
def test_benign_no_error_phrase_does_not_negate_the_requested_marker(answer: str) -> None:
    assert explicit_text_shape_status(RU_SENTENCE, answer) == TEXT_SHAPE_VALID


@pytest.mark.parametrize(
    "answer",
    [
        "Ответ будет без MARK-SEG-14.",
        "Я не включу маркер MARK-SEG-14.",
        "I will not include marker MARK-SEG-14.",
        "MARK-SEG-14 неверный.",
        "MARK-SEG-14 is wrong.",
        "MARK-SEG-14 is false.",
        "MARK-SEG-14 is invalid.",
    ],
)
def test_literal_bound_refusal_remains_invalid(answer: str) -> None:
    assert explicit_text_shape_status(RU_SENTENCE, answer) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize(
    "user_text",
    [
        "Оформи из файла список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
        "Оформи список из двух пунктов: alpha, beta. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
        "Если проверка пройдёт, оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
        "Оформи список из двух пунктов. Включи маркер MARK-SEG-20, если нужно. Контроль CTRL-20.",
        "Оформи список из двух пунктов. Не включай маркер MARK-SEG-20. Контроль CTRL-20.",
        "Обсуди фразу: оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
        "Write a list of two items? Include marker MARK-SEG-20. Control: CTRL-20.",
        "Write a list of two items. Include marker MARK-SEG-20? Control: CTRL-20.",
        "Write a list of two items. Include marker MARK-SEG-20. Control: CTRL-20. Extra.",
        "Write a list of two items and set a reminder. Include marker MARK-SEG-20. Control: CTRL-20.",
        "Напиши одно предложение и отправь сообщение. Включи маркер MARK-SEG-14. Контроль CTRL-14.",
        "Напиши одно предложение про голосовое сообщение. Включи маркер MARK-SEG-14. Контроль CTRL-14.",
        "Write a single sentence about a web search. Include marker MARK-SEG-14. Control: CTRL-14.",
        "Напиши одно предложение, обсудив термин, без переносов. Включи маркер MARK-SEG-14. Контроль CTRL-14.",
        "Напиши одно предложение, выразив отказ, без переносов. Включи маркер MARK-SEG-14. Контроль CTRL-14.",
        "Rewrite one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
        "Оформи список из двух пунктов. Включи маркер 42. Контроль 43.",
        "Return a list of two items. Include marker MARK-20. Control: CTRL-ONLY.",
        "Write a short line with an ampersand. Include marker MARK-SEG-11. Control: CTRL-11.",
        "Write a phrase of one substantive word. Include marker MARK-SEG-13. Control: CTRL-13.",
    ],
)
def test_regeneration_authority_fails_closed_for_non_direct_contracts(user_text: str) -> None:
    assert regenerable_text_shape_contract(user_text) is None
    assert explicit_text_shape_status(user_text, "unchanged") == TEXT_SHAPE_UNOWNED


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Верни одну цитату и одну отдельную строку пояснения. "
            "Включи маркер QUOTE-GAP-42. Контроль CHECK-42.",
            "> «Короткая цитата.» Пояснение QUOTE-GAP-42. Контроль CHECK-42.",
            "> «Короткая цитата.»\nПояснение QUOTE-GAP-42.",
        ),
        (
            "Return one quote and one separate explanation line. "
            "Include marker QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
    ],
)
def test_collapsed_quote_explanation_repair_changes_only_one_horizontal_boundary(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == expected
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Верни одну цитату и одну отдельную строку пояснения. "
            "Включи маркер QUOTE-GAP-42. Контроль CHECK-42.",
            "> «Цитата.» Пояснение QUOTE-GAP-42 QUOTE-GAP-42.",
        ),
        (
            "Return one quote and one separate explanation line. "
            "Include marker QUOTE-GAP-42. Control: CHECK-42.",
            "> “Quote.” Explanation **QUOTE-GAP-42**.",
        ),
        (
            "Return one quote and one quoted explanation line. "
            "Include marker QUOTE-GAP-42. Control: CHECK-42.",
            "> “Quote.” Explanation QUOTE-GAP-42.",
        ),
        (
            "Return one quote and one separate explanation line from the file. "
            "Include marker QUOTE-GAP-42. Control: CHECK-42.",
            "> “Quote.” Explanation QUOTE-GAP-42.",
        ),
    ],
)
def test_collapsed_quote_explanation_ambiguity_preserves_original_bytes(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer
    assert repair_explicit_text_shape(user_text, repair_explicit_text_shape(user_text, answer)) == answer


def test_nonmatching_quote_control_is_preserved_while_the_boundary_is_repaired() -> None:
    user_text = (
        "Return one quote and one separate explanation line. Include marker QUOTE-GAP-42. Control: CHECK-42."
    )
    answer = "> “Quote.” Explanation QUOTE-GAP-42. Control: WRONG-42."
    expected = "> “Quote.”\nExplanation QUOTE-GAP-42. Control: WRONG-42."

    assert repair_explicit_text_shape(user_text, answer) == expected
    assert repair_explicit_text_shape(user_text, expected) == expected


class _FakeRouter:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self, response: dict[str, object] | BaseException) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        self.calls.append((messages, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _SequenceRouter(_FakeRouter):
    def __init__(self, responses: list[dict[str, object] | BaseException]) -> None:
        super().__init__({"content": "unused"})
        self.responses = list(responses)

    async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
        self.calls.append((messages, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _runtime_with_router(router: _FakeRouter) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.llm = router
    runtime.settings = SimpleNamespace(llm_base_url="http://127.0.0.1:8001/v1")
    return runtime


class _OneSchemaKernel:
    def get_tool_definitions(self, actor: ActorContext, *, topic: str = "") -> list[dict[str, object]]:
        del actor, topic
        return [
            {
                "type": "function",
                "function": {
                    "name": "synthetic_effect",
                    "description": "must be suppressed",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]


async def _run_chat_with_invalid_shape(
    *,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
    router: _FakeRouter,
    first_response_override: dict[str, object] | None = None,
    knowledge_hits: list[dict[str, object]] | None = None,
    verify_answers: bool = False,
    reject_later_model_calls: bool = False,
    reject_late_file_effect: bool = False,
    answer_with_voice: bool = False,
    outward_verdict: tuple[str, str | None] | None = None,
    answer_mode: str = "general_conversation",
    retrieval_confidence: float = 0.0,
    retrieval_trace: list[dict[str, object]] | None = None,
    feedback_summary: dict[str, object] | None = None,
    prepare_context_calls: list[str] | None = None,
    semantic_arbiter_calls: list[str] | None = None,
    selector_prefetch_calls: list[str] | None = None,
    force_late_shape_context: bool = False,
    interaction_mode: str = "dialogue",
    message: str = RU_ITEM_LIST,
    structural_answer: str = "",
    verification_calls: list[str] | None = None,
    conversation_id: str | None = None,
    ingestion_result: dict[str, object] | None = None,
    context_overrides: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    storage.ensure_user("alice", preset_key="owner")
    runtime_settings = replace(
        settings,
        llm_enabled=True,
        verify_answers=verify_answers,
        **({"verify_min_answer_chars": 1} if verification_calls is not None else {}),
    )
    runtime = AgentRuntime(
        runtime_settings,
        storage,
        llm=router,
        kernel=_OneSchemaKernel(),
    )

    async def prepare(
        user_id: str,
        prepared_message: str,
        conversation_id: str,
        **kwargs: object,
    ) -> AgentContext:
        del prepared_message
        if prepare_context_calls is not None:
            prepare_context_calls.append("prepare")
        prior_history = [dict(item) for item in (kwargs.get("prior_history") or []) if isinstance(item, dict)]
        previous_user_turn = next(
            (
                str(item.get("content") or "")
                for item in reversed(prior_history)
                if item.get("role") == "user"
            ),
            "",
        )
        previous_answer = next(
            (
                str(item.get("content") or "")
                for item in reversed(prior_history)
                if item.get("role") == "assistant"
            ),
            "",
        )
        prepared_context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            conversation_history=prior_history,
            previous_user_turn=previous_user_turn,
            previous_answer=previous_answer,
            knowledge_hits=list(knowledge_hits or []),
            outward_verdict=outward_verdict,
            answer_mode=answer_mode,
            retrieval_confidence=retrieval_confidence,
            retrieval_trace=list(retrieval_trace or []),
            feedback_summary=dict(feedback_summary or {}),
            interaction_mode=interaction_mode,
            structural_answer=structural_answer,
            remainder_known=bool(structural_answer),
            open_remainder=message if structural_answer else "",
        )
        for key, value in (context_overrides or {}).items():
            setattr(prepared_context, key, value)
        return prepared_context

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    if force_late_shape_context:
        monkeypatch.setattr(runtime_module, "owns_closed_text_shape", lambda _message: False)
    if semantic_arbiter_calls is not None:

        def forbidden_semantic_arbiter(name: str):
            async def arbitrate(*args: object, **kwargs: object) -> object:
                del args, kwargs
                semantic_arbiter_calls.append(name)
                raise AssertionError(f"closed shape executed {name} arbiter")

            return arbitrate

        monkeypatch.setattr(
            runtime,
            "_office_intent_arbiter",
            forbidden_semantic_arbiter("office"),
        )
        monkeypatch.setattr(
            runtime,
            "_standing_rule_by_arbiter",
            forbidden_semantic_arbiter("standing_rule"),
        )
        monkeypatch.setattr(
            runtime,
            "_web_query_by_arbiter",
            forbidden_semantic_arbiter("web_query"),
        )
        monkeypatch.setattr(
            runtime,
            "_is_small_talk_by_arbiter",
            forbidden_semantic_arbiter("small_talk"),
        )
    if selector_prefetch_calls is not None:

        def forbidden_selector_prefetch(name: str):
            async def prefetch(*args: object, **kwargs: object) -> bool:
                del args, kwargs
                selector_prefetch_calls.append(name)
                raise AssertionError(f"closed shape executed {name} prefetch")

            return prefetch

        monkeypatch.setattr(
            runtime,
            "_prefetch_person_activity",
            forbidden_selector_prefetch("person"),
        )
        monkeypatch.setattr(
            runtime,
            "_prefetch_the_web_if_asked",
            forbidden_selector_prefetch("web"),
        )
    if first_response_override is not None:

        async def generate(
            context: AgentContext,
            message: str,
            attachments: object,
        ) -> dict[str, object]:
            del context, message, attachments
            return dict(first_response_override)

        monkeypatch.setattr(runtime, "_generate_response", generate)
    if reject_later_model_calls:

        async def unexpected_model_call(*args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            raise AssertionError("owned shape attempted a generic verifier or repair call")

        monkeypatch.setattr(runtime, "_verify_response", unexpected_model_call)
        monkeypatch.setattr(runtime, "_repair_once", unexpected_model_call)
    if verification_calls is not None:

        async def record_verification(
            question: str,
            answer: str,
            context: AgentContext,
            **kwargs: object,
        ) -> dict[str, object]:
            del question, answer, context, kwargs
            verification_calls.append("verify")
            return {"status": "skipped", "ok": True, "score": None, "issues": []}

        monkeypatch.setattr(runtime, "_verify_response", record_verification)
    if reject_late_file_effect:

        async def unexpected_file_effect(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("owned shape attempted a late file effect")

        monkeypatch.setattr(runtime_module, "_is_direct_file_request", lambda message: bool(message))
        monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", unexpected_file_effect)
    result = await runtime.chat(
        "alice",
        message,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation_id,
        enable_tools=True,
        answer_with_voice=answer_with_voice,
        ingestion_result=ingestion_result,
    )
    stored = storage.get_message(str(result["message_id"]), "alice")
    assert stored is not None
    assert stored["content"] == result["message"]
    metadata = json.loads(str(stored["metadata_json"]))
    return result, metadata


@pytest.mark.asyncio
async def test_regeneration_call_is_one_short_local_tool_free_json_call() -> None:
    router = _FakeRouter({"content": '["one", "two"]'})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "invalid draft",
        contract,
        timeout_sec=5.0,
    )

    assert answer == "- one MARK-SEG-20\n- two"
    assert len(router.calls) == 1
    messages, kwargs = router.calls[0]
    assert kwargs == {
        "tools": [],
        "temperature": 0.0,
        "max_tokens": 384,
        "priority": "foreground",
    }
    serialized_messages = json.dumps(messages, ensure_ascii=False)
    assert "MARK-SEG-20" not in serialized_messages
    assert "CTRL-20" not in serialized_messages
    assert RU_ITEM_LIST not in serialized_messages
    assert "invalid draft" not in serialized_messages
    payload = json.loads(messages[1]["content"].partition("\n")[2])
    assert payload == {
        "code_contract": {
            "item_count": 2,
            "language": "ru",
            "one_token": False,
        }
    }


@pytest.mark.asyncio
async def test_word_list_retry_has_one_item_count_authority_and_code_adds_marker() -> None:
    router = _FakeRouter({"content": '["альфа", "бета"]'})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_WORD_LIST)
    assert contract is not None

    answer, reason = await runtime._regenerate_explicit_text_shape_once_with_reason(
        RU_WORD_LIST,
        "invalid draft SYN-FOREIGN-99",
        contract,
        timeout_sec=5.0,
    )

    assert reason == "accepted"
    assert answer == "- MARK-SEG-12\n- альфа\n- бета"
    payload = json.loads(router.calls[0][0][1]["content"].partition("\n")[2])
    assert payload == {
        "code_contract": {
            "item_count": 2,
            "language": "ru",
            "one_token": True,
        }
    }
    assert "3" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_word_list_retry_rejects_model_that_uses_final_count_as_semantic_count() -> None:
    router = _FakeRouter({"content": '["альфа", "бета", "гамма"]'})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_WORD_LIST)
    assert contract is not None

    answer, reason = await runtime._regenerate_explicit_text_shape_once_with_reason(
        RU_WORD_LIST,
        "invalid draft",
        contract,
        timeout_sec=5.0,
    )

    assert answer == ""
    assert reason == "arity"


@pytest.mark.parametrize(
    "raw_reply",
    [
        "",
        " \n\t ",
        'Here is the JSON: ["one", "two"]',
        '```json\n["one", "two"]\n```',
        '{"items": ["one", "two"]}',
        '"scalar"',
        "[]",
        '["one"]',
        '["one", "two", "three"]',
        '["one", 2]',
        '["", "two"]',
        '["   ", "two"]',
        '[" one", "two"]',
        '["one ", "two"]',
        json.dumps(["one\x00extra", "two"]),
        json.dumps(["one\textra", "two"]),
        json.dumps(["one\x1fextra", "two"]),
        json.dumps(["one\x7fextra", "two"]),
        json.dumps(["one\u0085extra", "two"]),
        json.dumps(["one\u2028extra", "two"]),
        json.dumps(["one\u2029extra", "two"]),
        json.dumps(["one\ud800extra", "two"]),
        json.dumps(["one\udfffextra", "two"]),
        json.dumps(["x" * 1_001, "two"]),
        '["one MARK-SEG-20", "two"]',
        '["one CTRL-20", "two"]',
        '["one FOREIGN-SEG-99", "two"]',
        '["one OTHER-99", "two"]',
        json.dumps(["memory_search.search(query=foo)", "two"]),
        json.dumps(["Call: web.search", "two"]),
        json.dumps(["{name: web_search, args: none}", "two"]),
    ],
)
@pytest.mark.asyncio
async def test_structured_regeneration_rejects_nonexact_json_and_unsafe_items(
    raw_reply: str,
) -> None:
    router = _FakeRouter({"content": raw_reply})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "invalid draft FOREIGN-SEG-99 OTHER-99",
        contract,
        timeout_sec=5.0,
    )

    assert answer == ""
    assert len(router.calls) == 1


@pytest.mark.parametrize(
    ("raw_reply", "draft", "expected_reason"),
    [
        ("not json", "invalid draft", "json"),
        ('{"items": ["one", "two"]}', "invalid draft", "type"),
        ('["one"]', "invalid draft", "arity"),
        (json.dumps(["Call: web.search", "two"]), "invalid draft", "item"),
        ('["one OTHER-99", "two"]', "invalid draft OTHER-99", "foreign_id"),
        (json.dumps(["https://example.invalid", "two"]), "invalid draft", "render"),
    ],
)
@pytest.mark.asyncio
async def test_structured_regeneration_reports_closed_rejection_reason(
    raw_reply: str,
    draft: str,
    expected_reason: str,
) -> None:
    router = _FakeRouter({"content": raw_reply})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer, reason = await runtime._regenerate_explicit_text_shape_once_with_reason(
        RU_ITEM_LIST,
        draft,
        contract,
        timeout_sec=5.0,
    )

    assert answer == ""
    assert reason == expected_reason


@pytest.mark.asyncio
async def test_structured_regeneration_accepts_only_outer_json_whitespace() -> None:
    router = _FakeRouter({"content": f" \r\n{STRUCTURED_ITEMS}\t "})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "invalid draft",
        contract,
        timeout_sec=5.0,
    )

    assert answer == STRUCTURED_LIST


@pytest.mark.parametrize("wrapper", ["\x0b", "\x0c", "\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("side", ["prefix", "suffix"])
@pytest.mark.asyncio
async def test_structured_regeneration_rejects_non_json_outer_whitespace(
    wrapper: str,
    side: str,
) -> None:
    raw_reply = f"{wrapper}{STRUCTURED_ITEMS}" if side == "prefix" else f"{STRUCTURED_ITEMS}{wrapper}"
    router = _FakeRouter({"content": raw_reply})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "invalid draft",
        contract,
        timeout_sec=5.0,
    )

    assert answer == ""


@pytest.mark.asyncio
async def test_structured_regeneration_accepts_valid_astral_unicode() -> None:
    router = _FakeRouter({"content": json.dumps(["Safe 😀", "Ordinary prose"])})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "invalid draft",
        contract,
        timeout_sec=5.0,
    )

    answer.encode("utf-8")
    assert explicit_text_shape_status(RU_ITEM_LIST, answer) == TEXT_SHAPE_VALID


@pytest.mark.asyncio
async def test_regeneration_timeout_closes_call_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowRouter(_FakeRouter):
        async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
            self.calls.append((messages, kwargs))
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(runtime_module, "_TEXT_SHAPE_REGEN_MIN_REMAINING_SEC", 0.0)
    router = _SlowRouter({"content": "unused"})
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    answer = await runtime._regenerate_explicit_text_shape_once(
        RU_ITEM_LIST,
        "byte exact original",
        contract,
        timeout_sec=0.01,
    )

    assert answer == ""
    assert len(router.calls) == 1


@pytest.mark.asyncio
async def test_regeneration_propagates_outer_cancellation() -> None:
    router = _FakeRouter(asyncio.CancelledError())
    runtime = _runtime_with_router(router)
    contract = regenerable_text_shape_contract(RU_ITEM_LIST)
    assert contract is not None

    with pytest.raises(asyncio.CancelledError):
        await runtime._regenerate_explicit_text_shape_once(
            RU_ITEM_LIST,
            "byte exact original",
            contract,
            timeout_sec=5.0,
        )

    assert len(router.calls) == 1


@pytest.mark.asyncio
async def test_chat_regenerates_once_without_exposing_tool_schemas_and_audits_acceptance(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regenerated = STRUCTURED_LIST
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}, {"content": STRUCTURED_ITEMS}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == regenerated
    assert result["tools_used"] == []
    assert len(router.calls) == 2
    assert "tools" not in router.calls[0][1] or router.calls[0][1]["tools"] == []
    assert router.calls[1][1]["tools"] == []
    assert result["files"] == []
    assert result["voice"] is None
    assert result["web_query_notice"] == ""
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert metadata["text_shape_regeneration"] == {"accepted": True, "attempted": True}
    assert metadata["text_shape_regeneration_reason"] == "accepted"
    messages = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert all(item["content"] != original for item in messages if item["role"] == "assistant")


@pytest.mark.asyncio
async def test_chat_regenerates_a_misspelled_bold_transport_marker_once(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = "**привет SYN-TELELEGRAM-A10-02**"
    corrected = "**Привет SYN-TELEGRAM-A10-02.**"
    router = _SequenceRouter([{"content": malformed}, {"content": corrected}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=RU_BOLD_PHRASE,
    )

    assert result["message"] == corrected
    assert len(router.calls) == 2
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert metadata["text_shape_regeneration"] == {"accepted": True, "attempted": True}
    assert metadata["text_shape_regeneration_reason"] == "accepted"
    payload = json.loads(router.calls[1][0][1]["content"].partition("\n")[2])
    assert payload["code_contract"] == {
        "count": None,
        "emphasis_style": "bold",
        "kind": "single_sentence",
        "literal": "SYN-TELEGRAM-A10-02",
        "word_list": False,
    }


@pytest.mark.asyncio
async def test_a10_12_word_list_retry_is_accepted_end_to_end(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter(
        [
            {"content": "Invalid word-list draft."},
            {"content": '["альфа", "бета"]'},
        ]
    )

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=RU_WORD_LIST,
    )

    assert result["message"] == "- MARK-SEG-12\n- альфа\n- бета"
    assert result["exact_text_shape_owned"] is True
    assert metadata["text_shape_regeneration"] == {"accepted": True, "attempted": True}
    assert metadata["text_shape_regeneration_reason"] == "accepted"


@pytest.mark.parametrize(
    ("second", "initial", "expected_reason"),
    [
        ('["one", "two"]', "Invalid draft.", "accepted"),
        ("not json", "Invalid draft.", "json"),
        ('{"items": ["one", "two"]}', "Invalid draft.", "type"),
        ('["one"]', "Invalid draft.", "arity"),
        (json.dumps(["Call: web.search", "two"]), "Invalid draft.", "item"),
        ('["one OTHER-99", "two"]', "Invalid draft OTHER-99.", "foreign_id"),
        (json.dumps(["https://example.invalid", "two"]), "Invalid draft.", "render"),
        (RuntimeError("closed fake failure"), "Invalid draft.", "call"),
    ],
)
@pytest.mark.asyncio
async def test_chat_persists_only_closed_regeneration_reason(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
    second: str | BaseException,
    initial: str,
    expected_reason: str,
) -> None:
    second_response: dict[str, object] | BaseException = (
        second if isinstance(second, BaseException) else {"content": second}
    )
    router = _SequenceRouter([{"content": initial}, second_response])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert metadata["text_shape_regeneration_reason"] == expected_reason
    assert metadata["text_shape_regeneration"] == {
        "accepted": expected_reason == "accepted",
        "attempted": True,
    }
    assert isinstance(metadata["text_shape_regeneration_reason"], str)
    if expected_reason != "accepted":
        assert result["message"] == FORMAT_FAILURE_RU


@pytest.mark.asyncio
async def test_closed_shape_isolates_ambient_history_private_lineage_and_runtime_context(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_user = "PRIOR-USER-SHAPE-SENTINEL"
    prior_assistant = "PRIOR-ASSISTANT-SHAPE-SENTINEL"
    private_lineage = "PRIVATE-LINEAGE-SHAPE-SENTINEL"
    feedback = "FEEDBACK-SHAPE-SENTINEL"
    retrieval = "RETRIEVAL-SHAPE-SENTINEL"
    dynamic_sentinels = {
        "DYNAMIC-USER-MODEL-SENTINEL",
        "DYNAMIC-INSTRUCTIONS-SENTINEL",
        "DYNAMIC-RULES-SENTINEL",
        "DYNAMIC-CORRECTIONS-SENTINEL",
    }
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="shape history")
    conversation_id = str(conversation["id"])
    storage.store_message(conversation_id, "alice", "user", prior_user)
    storage.store_message(
        conversation_id,
        "alice",
        "assistant",
        f"{prior_assistant} {private_lineage}",
        metadata={"private_context_lineage": True},
    )
    regenerated = STRUCTURED_LIST
    router = _SequenceRouter(
        [
            {"content": "Invalid current-turn draft."},
            {"content": STRUCTURED_ITEMS},
        ]
    )
    dynamic_fetches: list[str] = []
    prepare_context_calls: list[str] = []
    semantic_arbiter_calls: list[str] = []

    def forbidden_dynamic_fetch(name: str, value: object):
        def fetch(*args: object, **kwargs: object) -> object:
            del args, kwargs
            dynamic_fetches.append(name)
            return value

        return fetch

    monkeypatch.setattr(
        AgentRuntime,
        "_user_model_payload",
        forbidden_dynamic_fetch("user_model", {"sentinel": "DYNAMIC-USER-MODEL-SENTINEL"}),
    )
    monkeypatch.setattr(
        AgentRuntime,
        "_custom_instructions",
        forbidden_dynamic_fetch("instructions", "DYNAMIC-INSTRUCTIONS-SENTINEL"),
    )
    monkeypatch.setattr(
        AgentRuntime,
        "_standing_rules",
        forbidden_dynamic_fetch("rules", ["DYNAMIC-RULES-SENTINEL"]),
    )
    monkeypatch.setattr(
        AgentRuntime,
        "_corrections",
        forbidden_dynamic_fetch("corrections", ["DYNAMIC-CORRECTIONS-SENTINEL"]),
    )

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        conversation_id=conversation_id,
        retrieval_trace=[{"title": retrieval, "reason": "dropped"}],
        feedback_summary={"sentinel": feedback, "negative": 1},
        prepare_context_calls=prepare_context_calls,
        semantic_arbiter_calls=semantic_arbiter_calls,
    )

    assert result["message"] == regenerated
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert metadata["text_shape_regeneration"] == {"accepted": True, "attempted": True}
    assert prepare_context_calls == []
    assert semantic_arbiter_calls == []
    assert dynamic_fetches == []
    assert len(router.calls) == 2
    for messages, kwargs in router.calls:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        assert prior_user not in payload
        assert prior_assistant not in payload
        assert private_lineage not in payload
        assert feedback not in payload
        assert retrieval not in payload
        assert all(sentinel not in payload for sentinel in dynamic_sentinels)
        assert kwargs.get("tools", []) == []


@pytest.mark.parametrize(
    "outward_verdict",
    [("интернет", None), ("человек", "MISROUTED-PERSON-SENTINEL")],
)
@pytest.mark.asyncio
async def test_closed_shape_parser_precedes_an_unsubstantiated_semantic_selector(
    outward_verdict: tuple[str, str | None],
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_sentinel = "PRIOR-SELECTOR-SHAPE-SENTINEL"
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="shape selector")
    conversation_id = str(conversation["id"])
    storage.store_message(conversation_id, "alice", "assistant", prior_sentinel)
    regenerated = STRUCTURED_LIST
    router = _SequenceRouter(
        [
            {"content": "Invalid selector-routed draft."},
            {"content": STRUCTURED_ITEMS},
        ]
    )
    prepare_context_calls: list[str] = []
    semantic_arbiter_calls: list[str] = []
    selector_prefetch_calls: list[str] = []

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        conversation_id=conversation_id,
        outward_verdict=outward_verdict,
        prepare_context_calls=prepare_context_calls,
        semantic_arbiter_calls=semantic_arbiter_calls,
        selector_prefetch_calls=selector_prefetch_calls,
    )

    assert result["message"] == regenerated
    assert result["tools_used"] == []
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert metadata["text_shape_regeneration"] == {"accepted": True, "attempted": True}
    assert prepare_context_calls == []
    assert semantic_arbiter_calls == []
    assert selector_prefetch_calls == []
    assert len(router.calls) == 2
    for index, (messages, kwargs) in enumerate(router.calls):
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        assert (RU_ITEM_LIST in payload) is (index == 0)
        assert prior_sentinel not in payload
        assert "MISROUTED-PERSON-SENTINEL" not in payload
        assert kwargs.get("tools", []) == []


@pytest.mark.asyncio
async def test_chat_repairs_lossy_word_list_metadata_without_regeneration(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "- alpha\n- beta\n- Маркер MARK-SEG-12. Контроль CTRL-12."
    repaired = "- alpha\n- beta\n- MARK-SEG-12"
    router = _SequenceRouter([{"content": original}])

    assert repair_explicit_text_shape(RU_WORD_LIST, original) == repaired
    assert explicit_text_shape_status(RU_WORD_LIST, repaired) == TEXT_SHAPE_VALID
    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=RU_WORD_LIST,
    )

    assert result["message"] == repaired
    assert explicit_text_shape_status(RU_WORD_LIST, str(result["message"])) == TEXT_SHAPE_VALID
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert len(router.calls) == 1
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}


@pytest.mark.asyncio
async def test_chat_invalid_structured_retry_publishes_explicit_format_failure(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}, {"content": "Still invalid."}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert len(router.calls) == 2
    assert result["tools_used"] == []
    assert result["files"] == []
    assert result["voice"] is None
    assert result["web_query_notice"] == ""
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}


@pytest.mark.asyncio
async def test_owned_invalid_fallback_skips_generic_verifier_repair_and_late_effects(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ("Neutral factual baseline with enough length for verification. " * 12).strip()
    router = _SequenceRouter([{"content": original}, {"content": "Still invalid."}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        verify_answers=True,
        reject_later_model_calls=True,
        reject_late_file_effect=True,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["verification_status"] == "skipped"
    assert result["tools_used"] == []
    assert result["files"] == []
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 2
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}
    assert metadata["text_shape_regeneration_reason"] == "json"


@pytest.mark.asyncio
async def test_owned_refusal_fallback_is_not_augmented_after_failed_regeneration(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Не могу выполнить эту форму."
    router = _SequenceRouter([{"content": original}, {"content": "Всё ещё неверно."}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 2
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}


@pytest.mark.asyncio
async def test_owned_fallback_sanitizes_unsupported_citation_before_capture(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Neutral baseline [K999] without shape."
    sanitized = "Neutral baseline without shape."
    router = _SequenceRouter([{"content": original}, {"content": "Still invalid."}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 2
    assert "[K999]" not in router.calls[1][0][1]["content"]
    assert sanitized not in router.calls[1][0][1]["content"]
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}


@pytest.mark.parametrize(
    ("original", "sanitized"),
    [
        ("Fact [K\n1] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [K\u00a01] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [\u00a0K1] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [ [K1] ] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact ［K1］ MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [ᴋ1] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [[K1],K2 MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        ("Fact [K1],K2] MARK-SEG-14 CTRL-14.", "Fact MARK-SEG-14 CTRL-14."),
        (
            "Fact [" + ",".join(f"K{index}" for index in range(1, 35)) + "] MARK-SEG-14 CTRL-14.",
            "Fact MARK-SEG-14 CTRL-14.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_failed_regeneration_never_exact_owns_unresolved_citation_residue(
    original: str,
    sanitized: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": original}, {"content": "Still invalid."}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        interaction_mode="research",
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert runtime_module._citation_like_bracket_spans(str(result["message"])) == []
    assert result["citations"] == []
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}
    assert TelegramBridge._format_response_message(result) == FORMAT_FAILURE_RU  # noqa: SLF001
    rows = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    assert rows[-1]["content"] == FORMAT_FAILURE_RU


@pytest.mark.parametrize(
    ("first_draft", "expected"),
    [
        (
            "- First item MARK-SEG-20\n- Second item",
            "- First item MARK-SEG-20\n- Second item",
        ),
        (
            "- First item\n- Second item\nMARK-SEG-20",
            "- First item\n- Second item MARK-SEG-20",
        ),
    ],
)
@pytest.mark.asyncio
async def test_chat_skips_regeneration_when_first_draft_is_or_repairs_to_valid(
    first_draft: str,
    expected: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": first_draft}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == expected
    assert explicit_text_shape_status(RU_ITEM_LIST, str(result["message"])) == TEXT_SHAPE_VALID
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert len(router.calls) == 1
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}


@pytest.mark.asyncio
async def test_chat_regeneration_timeout_publishes_explicit_format_failure(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowRouter(_FakeRouter):
        async def chat(self, messages: list[dict[str, str]], **kwargs: object) -> dict[str, object]:
            self.calls.append((messages, kwargs))
            if len(self.calls) == 1:
                return {"content": "Byte-exact neutral first draft without the required shape."}
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(runtime_module, "_TEXT_SHAPE_REGEN_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime_module, "_TEXT_SHAPE_REGEN_MIN_REMAINING_SEC", 0.001)
    router = _SlowRouter({"content": "unused"})

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 2
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": True}
    assert metadata["text_shape_regeneration_reason"] == "call"


@pytest.mark.asyncio
async def test_chat_does_not_claim_an_attempt_when_endpoint_is_not_private(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_settings = replace(settings, llm_base_url="https://model.invalid/v1")
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=external_settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 1
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}
    assert metadata["text_shape_regeneration_reason"] == "not_attempted"


@pytest.mark.asyncio
async def test_chat_does_not_attempt_regeneration_with_insufficient_turn_budget(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}])
    monkeypatch.setattr(
        runtime_module,
        "_TEXT_SHAPE_REGEN_MIN_REMAINING_SEC",
        settings.agent_turn_budget_sec + 1.0,
    )

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 1
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}
    assert metadata["text_shape_regeneration_reason"] == "not_attempted"


@pytest.mark.asyncio
async def test_chat_does_not_attempt_regeneration_when_model_is_disabled(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DisabledRouter(_FakeRouter):
        enabled = False

    router = _DisabledRouter({"content": "must not be called"})

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
    )

    assert router.calls == []
    assert result["message"] == FORMAT_FAILURE_RU
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}
    assert metadata["text_shape_regeneration_reason"] == "not_attempted"


@pytest.mark.asyncio
async def test_chat_does_not_own_voice_turn_for_regeneration(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        answer_with_voice=True,
    )

    assert result["message"] == original
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert len(router.calls) == 1
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}
    assert metadata["text_shape_regeneration_reason"] == "not_attempted"


@pytest.mark.parametrize("interaction_mode", ["dialogue", "knowledge_work", "research"])
@pytest.mark.parametrize(
    ("responses", "expected", "calls", "audit", "owned"),
    [
        (
            [{"content": "- First MARK-SEG-20\n- Second"}],
            "- First MARK-SEG-20\n- Second",
            1,
            {"accepted": False, "attempted": False},
            True,
        ),
        (
            [
                {"content": "Invalid first draft."},
                {"content": STRUCTURED_ITEMS},
            ],
            STRUCTURED_LIST,
            2,
            {"accepted": True, "attempted": True},
            True,
        ),
        (
            [{"content": "Invalid first draft."}, {"content": "Still invalid."}],
            FORMAT_FAILURE_RU,
            2,
            {"accepted": False, "attempted": True},
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_context_empty_owned_shape_is_byte_exact_through_every_mode_banner(
    interaction_mode: str,
    responses: list[dict[str, object]],
    expected: str,
    calls: int,
    audit: dict[str, bool],
    owned: bool,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter(responses)

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        interaction_mode=interaction_mode,
    )

    assert result["message"] == expected
    assert result["exact_text_shape_owned"] is owned
    assert metadata["exact_text_shape_owned"] is owned
    assert metadata["text_shape_regeneration"] == audit
    assert len(router.calls) == calls
    first = TelegramBridge._format_response_message(result)  # noqa: SLF001
    second = TelegramBridge._format_response_message(result)  # noqa: SLF001
    assert first == second
    if owned:
        assert first == expected
    else:
        assert expected in first


@pytest.mark.parametrize(
    ("late_companion", "visible_fragment"),
    [
        ({"grounding_warning": "GROUNDING WARNING"}, "GROUNDING WARNING"),
        ({"regenerate_notice": "ATTACHMENT NOTICE"}, "ATTACHMENT NOTICE"),
        ({"verification_caution": "VERIFY CAUTION"}, "VERIFY CAUTION"),
        ({"web_query_notice": "WEB NOTICE"}, "WEB NOTICE"),
        ({"citation_notice": "SOURCE LEGEND"}, "SOURCE LEGEND"),
    ],
)
def test_late_truth_companion_revokes_exact_transport_on_delivery_and_retry(
    late_companion: dict[str, object],
    visible_fragment: str,
) -> None:
    message = "- First MARK-SEG-20\n- Second"
    base = {
        "message": message,
        "exact_text_shape_owned": True,
        "context": {"interaction_mode": "research"},
    }
    assert TelegramBridge._format_response_message(base) == message  # noqa: SLF001

    retried = {**base, **late_companion}
    first = TelegramBridge._format_response_message(retried)  # noqa: SLF001
    second = TelegramBridge._format_response_message(retried)  # noqa: SLF001

    assert first == second
    assert first != message
    assert visible_fragment in first


def test_file_lifecycle_bookkeeping_does_not_revoke_exact_transport() -> None:
    message = "- First MARK-SEG-20\n- Second"
    base = {
        "message": message,
        "exact_text_shape_owned": True,
        "context": {"interaction_mode": "research"},
    }

    for receipt in ({"promoted": True}, {"queued_for_review": True, "inbox_id": "in_1"}):
        delivered = TelegramBridge._format_response_message(  # noqa: SLF001
            {**base, "file_ingestion": receipt}
        )
        assert delivered == message


def test_file_reliability_warning_still_revokes_exact_transport() -> None:
    message = "- First MARK-SEG-20\n- Second"
    delivered = TelegramBridge._format_response_message(  # noqa: SLF001
        {
            "message": message,
            "exact_text_shape_owned": True,
            "context": {"interaction_mode": "research"},
            "file_ingestion": {
                "queued_for_review": True,
                "extraction": {"success": False, "text_success": False},
            },
        }
    )

    assert delivered != message
    assert "/inbox" not in delivered
    assert "Текст извлечь не удалось" in delivered


@pytest.mark.parametrize(
    ("confidence", "expected_companion"),
    [(0.5, "grounding_warning"), (0.9, "citation_notice")],
)
@pytest.mark.asyncio
async def test_personal_retrieval_context_is_unowned_and_keeps_truthful_companions(
    confidence: float,
    expected_companion: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "- First MARK-SEG-20\n- Second"
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        knowledge_hits=[
            {
                "id": "ko_synthetic_shape",
                "title": "Synthetic evidence",
                "content": "Synthetic evidence.",
                "score": 1.0,
                "_rerank_score": 0.9,
            }
        ],
        answer_mode="personal_knowledge",
        retrieval_confidence=confidence,
        force_late_shape_context=True,
    )

    assert result["message"] == answer
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}
    assert len(router.calls) == 1
    assert result[expected_companion]
    assert TelegramBridge._format_response_message(result) != answer  # noqa: SLF001


@pytest.mark.parametrize(
    ("user_text", "answer", "warning_expected"),
    [
        (RU_SENTENCE, "По вашей базе знаний MARK-SEG-14.", True),
        (RU_SENTENCE, "В вашей базе записано MARK-SEG-14.", False),
        (RU_SENTENCE, "Согласно вашим документам MARK-SEG-14.", True),
        (RU_SENTENCE, "По вашим данным MARK-SEG-14.", False),
        (RU_SENTENCE, "В ваших записях сказано MARK-SEG-14.", False),
        (RU_SENTENCE, "Ваши документы показывают MARK-SEG-14.", False),
        (RU_SENTENCE, "Найдено в вашем файле MARK-SEG-14.", False),
        (RU_SENTENCE, "На основе ваших документов MARK-SEG-14.", False),
        (RU_SENTENCE, "В соответствии с вашей перепиской MARK-SEG-14.", False),
        (RU_SENTENCE, "Из присланного вами файла следует MARK-SEG-14.", False),
        (RU_SENTENCE, "Сверившись с вашими заметками, MARK-SEG-14.", False),
        (RU_SENTENCE, "Из приложенного документа следует MARK-SEG-14.", False),
        (RU_SENTENCE, "Из высланного файла следует MARK-SEG-14.", False),
        (RU_SENTENCE, "Из направленного документа следует MARK-SEG-14.", False),
        (RU_SENTENCE, "Файл был прислан вами и подтверждает MARK-SEG-14.", False),
        (RU_SENTENCE, "Файл от вас подтверждает MARK-SEG-14.", False),
        (RU_SENTENCE, "Файл, который был прислан вами, подтверждает MARK-SEG-14.", False),
        (RU_SENTENCE, "Файл, который вы мне прислали, подтверждает MARK-SEG-14.", False),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "According to your archive MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Based on the file you sent MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "From our conversation MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Per your archive MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Found in your materials MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Your documents confirm MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "In your notes MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Drawing on your records MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "I found this in your materials MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "As stated in your archive MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The emailed document confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The forwarded file confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The enclosed document confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The file from you confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The file was sent by you and confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The file that you have sent confirms MARK-SEG-14.",
            False,
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "The file I received from you confirms MARK-SEG-14.",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_personal_source_provenance_claim_never_gets_exact_transport_bypass(
    user_text: str,
    answer: str,
    warning_expected: bool,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=user_text,
        interaction_mode="research",
    )

    assert result["message"] == answer
    assert bool(result["grounding_warning"]) is warning_expected
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    delivered = TelegramBridge._format_response_message(result)  # noqa: SLF001
    if warning_expected:
        assert delivered != answer
    else:
        assert delivered == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Archive systems are common MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Records management is useful MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "As stated in documents of the standard the rule applies MARK-SEG-14.",
        ),
        (
            RU_SENTENCE,
            "Как указано в документах стандарта правило действует MARK-SEG-14.",
        ),
        (RU_SENTENCE, "Личные данные защищены законом MARK-SEG-14."),
        (RU_SENTENCE, "Пользовательские данные защищены законом MARK-SEG-14."),
        (RU_SENTENCE, "В базе данных хранится пример MARK-SEG-14."),
        (RU_SENTENCE, "База проекта описывает формат MARK-SEG-14."),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "This file format is ZIP MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "This data type is numeric MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Personal data are protected by law MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "User data policies are public MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Archive reports are public MARK-SEG-14.",
        ),
        (RU_SENTENCE, "Опираясь на переписку MARK-SEG-14."),
        (RU_SENTENCE, "Судя по заметкам MARK-SEG-14."),
        (RU_SENTENCE, "Архив подтверждает MARK-SEG-14."),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "According to the notes MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Records show MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "As documented MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Your archive reports are public MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Your records from the museum confirm MARK-SEG-14.",
        ),
        (RU_SENTENCE, "Ваши записи музея подтверждают MARK-SEG-14."),
    ],
)
@pytest.mark.asyncio
async def test_generic_or_external_source_language_keeps_exact_transport_ownership(
    user_text: str,
    answer: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=user_text,
        interaction_mode="research",
    )

    assert result["message"] == answer
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert TelegramBridge._format_response_message(result) == answer  # noqa: SLF001


@pytest.mark.parametrize(
    "source",
    [
        "ваших документов",
        "вашей переписки",
        "вашего файла",
        "ваших материалов",
        "ваших заметок",
        "ваших записей",
        "вашего архива",
        "вашей базы знаний",
        "вашей базы",
        "ваших данных",
    ],
)
@pytest.mark.parametrize(
    "relation",
    [
        "На основе {source} получен вывод",
        "Из {source} следует вывод",
    ],
)
def test_ru_personal_source_noun_relation_matrix_is_structurally_owned_by_normal_pipeline(
    source: str,
    relation: str,
) -> None:
    assert runtime_module._claims_personal_source_provenance(relation.format(source=source))


@pytest.mark.parametrize(
    "text",
    [
        "В вашей базе записан итог",
        "Итог взят из вашей базы",
        "База пользователя задаёт контекст",
    ],
)
def test_bare_ru_base_requires_an_explicit_personal_owner(text: str) -> None:
    assert runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "text",
    [
        "В базе данных хранится пример",
        "База проекта описывает формат",
        "База университета открыта исследователям",
    ],
)
def test_bare_ru_base_without_personal_owner_is_not_provenance(text: str) -> None:
    assert not runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "text",
    [
        "В соответствии с вашей перепиской итог изменён",
        "Из присланного вами файла следует вывод",
        "Из файла, который вы прислали, следует вывод",
        "Сверившись с вашими заметками, получаем итог",
        "Архив пользователя задаёт контекст",
        "Ваши записи раскрывают деталь",
        "Этот приложенный документ описывает итог",
        "Прикреплённый файл подтверждает итог",
        "Файл был прислан вами и подтверждает итог",
        "Файл от вас подтверждает итог",
        "Файл, который был прислан вами, подтверждает итог",
        "Файл, который вы мне прислали, подтверждает итог",
        "Файл был вам прислан и подтверждает итог",
        "Файл был прислан и подтверждает итог",
        "Мои личные данные подтверждают итог",
        "Загруженные личные данные подтверждают итог",
        "Личные данные от вас подтверждают итог",
    ],
)
def test_ru_personal_source_structure_is_independent_of_reporting_predicate(text: str) -> None:
    assert runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "source",
    [
        "your archive",
        "your notes",
        "your records",
        "your documents",
        "your materials",
        "your conversation",
        "your correspondence",
        "your file",
        "your data",
        "your knowledge base",
    ],
)
@pytest.mark.parametrize(
    "relation",
    [
        "According to {source}, the result follows",
        "Drawing on {source}, the result follows",
        "I found this in {source}",
        "As stated in {source}, the result follows",
    ],
)
def test_en_personal_source_noun_relation_matrix_is_structurally_owned_by_normal_pipeline(
    source: str,
    relation: str,
) -> None:
    assert runtime_module._claims_personal_source_provenance(relation.format(source=source))


@pytest.mark.parametrize(
    "text",
    [
        "Your notes reveal the detail",
        "Your documents establish the outcome",
        "Based on the file you sent, the result follows",
        "The file shared by you contradicts the conclusion",
        "The user's notes reveal the detail",
        "The archive of yours disproves the result",
        "The archive of mine disproves the result",
        "The file which was sent by you confirms the result",
        "The document received from the user confirms the result",
        "The attached document contradicts the conclusion",
        "Our chat establishes the context",
        "The file from you confirms the result",
        "The file was sent by you and confirms the result",
        "The file was emailed to me and confirms the result",
        "The file was forwarded to us and confirms the result",
        "The file that you have sent confirms the result",
        "The file I received from you confirms the result",
        "My personal data confirm the result",
        "The uploaded personal data confirm the result",
        "Personal data from you confirm the result",
    ],
)
def test_en_personal_source_structure_is_independent_of_reporting_predicate(text: str) -> None:
    assert runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "text",
    [
        "Из приложенного документа следует вывод",
        "Из высланного файла следует вывод",
        "Из направленного документа следует вывод",
        "The emailed document confirms the result",
        "The forwarded file confirms the result",
        "The enclosed document confirms the result",
    ],
)
def test_transferred_artifact_role_owns_the_public_reviewer_counterexamples(text: str) -> None:
    assert runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ("мой", True),
        ("твой", True),
        ("ваш", True),
        ("наш", True),
        ("свой", True),
        ("пользовательский", True),
        ("личный", False),
        ("этот", False),
    ],
)
@pytest.mark.parametrize(
    "artifact",
    ["архив", "документ", "файл", "материал", "текст", "чат"],
)
def test_ru_direct_owner_artifact_cross_product_is_owned(
    owner: str,
    expected: bool,
    artifact: str,
) -> None:
    assert runtime_module._claims_personal_source_provenance(f"{owner} {artifact} содержит факт") is expected


@pytest.mark.parametrize(
    "transfer",
    [
        "присланный",
        "отправленный",
        "пересланный",
        "высланный",
        "направленный",
        "загруженный",
        "предоставленный",
        "переданный",
        "приложенный",
        "прикреплённый",
        "вставленный",
        "процитированный",
        "импортированный",
        "скинутый",
    ],
)
@pytest.mark.parametrize("artifact", ["документ", "файл", "материал", "текст"])
def test_ru_transfer_artifact_cross_product_is_owned(transfer: str, artifact: str) -> None:
    assert runtime_module._claims_personal_source_provenance(f"{transfer} {artifact} содержит факт")


@pytest.mark.parametrize(
    "transfer",
    [
        "вставлен",
        "выслан",
        "загружен",
        "импортирован",
        "направлен",
        "отправлен",
        "передан",
        "переслан",
        "получен",
        "предоставлен",
        "прикреплён",
        "приложен",
        "прислан",
        "процитирован",
        "скинут",
    ],
)
@pytest.mark.parametrize("actor", ["вами", "мной", "нами", "тобой"])
def test_ru_passive_transfer_actor_cross_product_is_owned(transfer: str, actor: str) -> None:
    assert runtime_module._claims_personal_source_provenance(
        f"Файл был {transfer} {actor} и подтверждает итог"
    )


@pytest.mark.parametrize(
    "verb",
    ["выслали", "загрузили", "направили", "отправили", "передали", "переслали", "прислали"],
)
def test_ru_relative_active_transfer_with_recipient_is_owned(verb: str) -> None:
    assert runtime_module._claims_personal_source_provenance(
        f"Файл, который вы мне {verb}, подтверждает итог"
    )


@pytest.mark.parametrize(
    ("owner", "expected_owner"),
    [
        ("my", True),
        ("your", True),
        ("our", True),
        ("personal", False),
        ("private", False),
        ("this", False),
        ("that", False),
    ],
)
@pytest.mark.parametrize(
    "artifact",
    ["archive", "records", "data", "notes", "messages", "conversation", "files", "documents"],
)
def test_en_direct_owner_artifact_cross_product_is_owned(
    owner: str,
    expected_owner: bool,
    artifact: str,
) -> None:
    expected = expected_owner and not (artifact == "data" and owner in {"personal", "private"})
    assert runtime_module._claims_personal_source_provenance(f"{owner} {artifact} contain a fact") is expected


@pytest.mark.parametrize(
    "transfer",
    [
        "attached",
        "pasted",
        "provided",
        "quoted",
        "sent",
        "shared",
        "submitted",
        "supplied",
        "uploaded",
        "emailed",
        "forwarded",
        "enclosed",
        "received",
    ],
)
@pytest.mark.parametrize("artifact", ["document", "file", "material", "text"])
def test_en_transfer_artifact_cross_product_is_owned(transfer: str, artifact: str) -> None:
    assert runtime_module._claims_personal_source_provenance(f"The {transfer} {artifact} contains a fact")


@pytest.mark.parametrize(
    "transfer",
    ["emailed", "enclosed", "forwarded", "provided", "sent", "shared", "submitted", "uploaded"],
)
@pytest.mark.parametrize("relation", ["by you", "to me", "to us", "from you"])
def test_en_passive_transfer_actor_cross_product_is_owned(transfer: str, relation: str) -> None:
    assert runtime_module._claims_personal_source_provenance(
        f"The file was {transfer} {relation} and confirms the result"
    )


@pytest.mark.parametrize("perfect", ["have", "had"])
@pytest.mark.parametrize("transfer", ["emailed", "forwarded", "provided", "sent", "shared", "uploaded"])
def test_en_relative_perfect_transfer_is_owned(perfect: str, transfer: str) -> None:
    assert runtime_module._claims_personal_source_provenance(
        f"The file that you {perfect} {transfer} confirms the result"
    )


@pytest.mark.parametrize(
    "text",
    [
        "По данным Росстата инфляция снизилась",
        "Согласно приказу срок продлён",
        "На основе уравнения получен ответ",
        "Сверившись с календарём, выбрали дату",
        "Формат архива — ZIP",
        "Используй слово архив в примере",
        "Данные — это значения",
        "Заметки помогают памяти",
        "Файлы бывают текстовыми",
        "Из материалов ГОСТ следует правило",
        "Согласно публичной документации это верно",
        "According to Reuters, the rate changed",
        "According to NASA data, the rate changed",
        "According to public documents, the rate changed",
        "Based on the equation, the result follows",
        "Based on common knowledge, the result follows",
        "Drawing on experience, the answer follows",
        "I found this in a public encyclopedia",
        "As stated in section two, the result follows",
        "The archive format is ZIP",
        "Use the word records in the example",
        "Materials are synthetic",
        "Files can be large",
        "Data are values",
        "Notes help memory",
        "Messages are text units",
        "Conversation is a general concept",
        "Archive systems are common",
        "Archive processing is expensive",
        "Records management is useful",
        "Records storage is useful",
        "Records policies are public",
        "As stated in documents of the standard, the rule applies",
        "As documented by NASA data, the rule applies",
        "As recorded using public documents, the rule applies",
        "Как указано в документах стандарта, правило действует",
        "Архив опровергает вывод",
        "В сообщениях выше упоминается деталь",
        "Это данные для примера",
        "File formats are documented",
        "Data types are useful",
        "This file format is ZIP",
        "This data type is numeric",
        "Personal data are protected by law",
        "User data policies are public",
        "Личные данные защищены законом",
        "Пользовательские данные защищены законом",
        "Archive reports are public",
        "Records contradict the conclusion",
        "The archive disproves the result",
        "As documented, the result follows",
        "Messages above reveal the detail",
        "This archive report was public",
        "Your documents of the standard confirm the rule",
        "The emailed document from Reuters confirms the result",
        "Ваши документы стандарта подтверждают правило",
        "Файл был прислан музеем и подтверждает результат",
        "Файл прислан музеем и подтверждает результат",
        "Файл, присланный музеем, подтверждает результат",
        "This archive reports are public",
        "Your archive reports are public",
        "The emailed archive reports are public",
        "Your records from the museum confirm the rule",
        "Ваши записи музея подтверждают правило",
    ],
)
def test_benign_non_personal_source_language_does_not_lose_exact_shape_ownership(text: str) -> None:
    assert not runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "text",
    [
        "ваш внешний файл содержит факт",
        "присланный внешний документ содержит факт",
        "этот публичный материал содержит факт",
        "your public file contains a fact",
        "the uploaded external document contains a fact",
        "this unrelated material contains a fact",
        "вашеский файл содержит факт",
        "присланныйлишний документ содержит факт",
        "ваш документик содержит факт",
        "the uploadedness document contains a fact",
        "your filetype contains a fact",
        "the file received updates",
    ],
)
def test_unknown_token_breaks_direct_owner_or_transfer_binding(text: str) -> None:
    assert not runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize("artifact", ["archive", "data", "documents", "file", "records"])
@pytest.mark.parametrize(
    "head", ["format", "management", "policies", "processing", "storage", "systems", "type"]
)
def test_en_generic_compound_head_never_becomes_personal_provenance(
    artifact: str,
    head: str,
) -> None:
    assert not runtime_module._claims_personal_source_provenance(f"This {artifact} {head} is public")


@pytest.mark.parametrize("artifact", ["archive", "documents", "file", "records"])
@pytest.mark.parametrize("external", ["court", "museum", "public project", "Reuters", "standard"])
def test_en_external_source_qualifier_never_becomes_personal_provenance(
    artifact: str,
    external: str,
) -> None:
    assert not runtime_module._claims_personal_source_provenance(
        f"Your {artifact} from the {external} confirms the result"
    )


@pytest.mark.parametrize("artifact", ["архив", "документ", "файл", "записи"])
@pytest.mark.parametrize("external", ["музея", "публичного проекта", "Росстата", "суда", "стандарта"])
def test_ru_external_source_qualifier_never_becomes_personal_provenance(
    artifact: str,
    external: str,
) -> None:
    assert not runtime_module._claims_personal_source_provenance(
        f"Ваш {artifact} {external} подтверждает итог"
    )


@pytest.mark.parametrize("external", ["quuxforge", "nebulacorpus", "zenithowner"])
@pytest.mark.parametrize(
    "template",
    [
        "Your file from {external} confirms the result",
        "The emailed document by {external} confirms the result",
        "The file was forwarded by {external} and confirms the result",
        "Your records {external} confirm the result",
        "The archive of {external} confirms the result",
    ],
)
def test_arbitrary_en_external_source_np_overrides_owner_or_transfer(
    external: str,
    template: str,
) -> None:
    assert not runtime_module._claims_personal_source_provenance(template.format(external=external))


@pytest.mark.parametrize("external", ["кверксом", "небулой", "зенитовладельцем"])
@pytest.mark.parametrize(
    "template",
    [
        "Ваш файл от {external} подтверждает итог",
        "Присланный {external} документ подтверждает итог",
        "Файл был переслан {external} и подтверждает итог",
        "Ваши записи {external} подтверждают итог",
    ],
)
def test_arbitrary_ru_external_source_np_overrides_owner_or_transfer(
    external: str,
    template: str,
) -> None:
    assert not runtime_module._claims_personal_source_provenance(template.format(external=external))


@pytest.mark.parametrize(
    "text",
    [
        "Полученный от вас документ подтверждает итог",
        "Файл был переслан вам вчера",
        "Файл, который был прислан вами, подтверждает итог",
        "Файл, который вы мне прислали, подтверждает итог",
        "The file from you confirms the result",
        "The conversation with you establishes the context",
        "The archive you gave me confirms the result",
        "The document forwarded yesterday by the user confirms the result",
        "The file that you have sent confirms the result",
        "I received the document from you",
    ],
)
def test_user_role_voice_and_relative_order_matrix_is_personal(text: str) -> None:
    assert runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    "text",
    [
        "These files are examples",
        "Those documents are samples",
        "Archive reports are public",
        "Records policies are public",
        "Personal data policies are public",
        "User data formats are documented",
        "Эти файлы являются примерами",
        "Личные данные защищены",
        "Пользовательские данные являются категорией",
    ],
)
def test_bare_or_compound_artifact_np_is_not_personal(text: str) -> None:
    assert not runtime_module._claims_personal_source_provenance(text)


@pytest.mark.parametrize(
    ("content", "available", "expected"),
    [
        ("Fact [K1, K2].", (), "Fact."),
        ("Fact [k1,k2].", set(), "Fact."),
        ("Fact [K1; K2].", (), "Fact."),
        ("Fact [ K1, K2].", (), "Fact."),
        ("Fact [\tK1;\tK2].", (), "Fact."),
        ("Fact [K1, k2].", (), "Fact."),
        ("Fact [K1, K99].", {"K1"}, "Fact [K1]."),
        ("Fact [k1, k99].", {"1"}, "Fact [k1]."),
        ("Fact [k1; K2].", {"K1"}, "Fact [k1]."),
        ("Fact [K2, K1, K2].", {"K1"}, "Fact [K1]."),
        ("Fact [K1, K2].", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact [K1;K2].", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact [ K1;\tK2 ].", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact [K1; k2].", {"K1", "K2"}, "Fact [K1, k2]."),
        ("Fact [K1, K1].", {"K1"}, "Fact [K1]."),
        ("Fact [K1, K2].", {"[K2]"}, "Fact [K2]."),
        ("Fact [ K 1; k\t2].", {"1"}, "Fact [K1]."),
        ("Fact [K1, K2] and [K2, K3].", {"K1", "K3"}, "Fact [K1] and [K3]."),
        ("Fact [K\u00a01].", {"K1"}, "Fact [K1]."),
        ("Fact [\u00a0K1].", {"K1"}, "Fact [K1]."),
        ("Fact [K1].", {"K1"}, "Fact [K1]."),
        ("Fact [К1].", {"K1"}, "Fact [K1]."),
        ("Fact [Κ1].", {"K1"}, "Fact [K1]."),
        ("Fact [K1，K2].", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact [K1；K2].", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact ［Ｋ1；K2］.", {"K1", "K2"}, "Fact [K1, K2]."),
        ("Fact [K\n1].", {"K1"}, "Fact."),
        ("Fact [ [K1] ].", {"K1"}, "Fact."),
        ("Fact [[\nK1]].", {"K1"}, "Fact."),
    ],
)
def test_grouped_citation_sanitation_filters_each_label_without_malformed_residue(
    content: str,
    available: object,
    expected: str,
) -> None:
    cleaned = runtime_module._strip_invented_citations(content, available)

    assert cleaned == expected
    assert "[,]" not in cleaned
    assert "[," not in cleaned
    assert ",]" not in cleaned
    assert runtime_module._strip_invented_citations(cleaned, available) == cleaned


@pytest.mark.parametrize(
    "malformed",
    [
        "[K1,,K2]",
        "[K1;]",
        "[K1/K2]",
        "[K1-K2]",
        "[[K1]]",
        "[K1",
        "[K0]",
        "[K1x]",
        "[ K1,, K2 ]",
        "[K1,\nK2]",
        "[K1,\r\nK2]",
        "[[K1], K2]",
        "[K1,[K2]]",
        "[[K1]",
        "[ K100]",
        "[\tK01]",
        "[K_source]",
        "[K-source]",
        "[K source]",
        "[KB]",
        "[K\n1]",
        "[[\nK1]]",
        "[ [K1] ]",
        "［K1]",
        "[K1］",
        "[ᴋ1]",
        "[ĸ1]",
        "[ᛕ1]",
        "[K1﹐K2]",
        "[K1﹔K2]",
        "[K-12,K1]",
        "[[K1],K2",
        "[K1],K2]",
    ],
)
def test_malformed_or_ambiguous_citation_groups_are_neutralized(malformed: str) -> None:
    cleaned = runtime_module._strip_invented_citations(f"Fact {malformed}.", {"K1", "K2"})

    assert cleaned == "Fact."
    assert runtime_module._citation_labels(cleaned) == []
    assert runtime_module._strip_invented_citations(cleaned, {"K1", "K2"}) == cleaned


@pytest.mark.parametrize(
    "ordinary",
    [
        "Fact [Kafka] remains.",
        "Fact [KPI] remains.",
        "Fact [Key value] remains.",
        "Fact [document] remains.",
        "Fact [label](https://example.invalid) remains.",
        "Fact  with\tordinary spacing remains.",
        "Archive [KPI metrics] remain ordinary.",
        "Word [KBase] remains ordinary.",
        "Fact [ [Kafka] ] remains ordinary.",
        "Слово [Книга] остаётся обычным.",
        "Word [Κόσμος] remains ordinary.",
        "Word [KPI] remains ordinary.",
        "Issue [K-12] remains an ordinary designator.",
        "Issue ［K-12］ remains an ordinary designator.",
    ],
)
def test_ordinary_bracketed_text_and_outside_spacing_are_byte_preserved(ordinary: str) -> None:
    assert runtime_module._strip_invented_citations(ordinary, set()) == ordinary


@pytest.mark.parametrize(
    "content",
    [
        "Before [ K999] after",
        "Before [\tK999] after",
        "Before [K999] after",
        "Before [K999,\nK998] after",
        "Before [[[ K999,\r\nK998 ]]] after",
        "Before [K999 ordinary after",
        "Before [K\n1] after",
        "Before [K\u00a01] after",
        "Before [\u00a0K1] after",
        "Before [[\nK1]] after",
        "Before [ [K1] ] after",
        "Before [К1] after",
        "Before [Κ1] after",
    ],
)
def test_empty_available_set_removes_whole_citation_candidate_without_residue(content: str) -> None:
    cleaned = runtime_module._strip_invented_citations(content, ())

    assert runtime_module._citation_labels(cleaned) == []
    assert runtime_module._citation_like_bracket_spans(cleaned) == []
    assert "]" not in cleaned
    assert runtime_module._strip_invented_citations(cleaned, ()) == cleaned


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Fact[K1]more", "Fact more"),
        ("Fact[ K1]more", "Fact more"),
        ("Fact[K\u00a01]more", "Fact more"),
        ("До[K1]после", "До после"),
        ("Fact_[K1]more", "Fact_ more"),
        ("Fact-[K1]_more", "Fact- _more"),
    ],
)
def test_removed_citation_never_fuses_neighboring_words(content: str, expected: str) -> None:
    cleaned = runtime_module._strip_invented_citations(content, ())

    assert cleaned == expected
    assert runtime_module._strip_invented_citations(cleaned, ()) == cleaned


def test_citation_scanner_bounded_alphabet_property_matrix_is_total_and_idempotent() -> None:
    open_close = [("[", "]"), ("［", "］")]
    k_forms = ["K", "k", "Ｋ", "K", "К", "Κ", "ᴋ"]
    whitespace = ["", " ", "\t", "\u00a0", "\n"]
    separators = [",", ";", "，", "；"]

    for opener, closer in open_close:
        for depth in range(1, 4):
            for k_form in k_forms:
                for gap in whitespace:
                    for separator in separators:
                        candidate = (
                            opener * depth
                            + gap
                            + k_form
                            + gap
                            + "1"
                            + separator
                            + gap
                            + k_form
                            + "2"
                            + closer * depth
                        )
                        cleaned = runtime_module._strip_invented_citations(candidate, ())

                        assert runtime_module._citation_like_bracket_spans(cleaned) == []
                        assert runtime_module._citation_labels(cleaned) == []
                        assert runtime_module._strip_invented_citations(cleaned, ()) == cleaned


def test_citation_group_label_cap_and_extreme_known_keys_fail_closed_without_exception() -> None:
    assert runtime_module._strip_invented_citations("[K]", ()) == "[K]"
    for count in [1, 32, 33, 34]:
        labels = [f"K{index}" for index in range(1, count + 1)]
        candidate = "[" + ",".join(labels) + "]"
        cleaned = runtime_module._strip_invented_citations(candidate, set(labels))

        if count <= runtime_module._CITATION_MAX_LABELS:
            assert runtime_module._citation_labels(cleaned) == labels
        else:
            assert cleaned == ""
            assert runtime_module._citation_like_bracket_spans(cleaned) == []
        assert runtime_module._strip_invented_citations(cleaned, set(labels)) == cleaned

    huge_known = {"K" + "1" * 5000}
    assert runtime_module._strip_invented_citations("[K1]", huge_known) == ""


@pytest.mark.parametrize(
    "candidate",
    [
        "[K\n1\n,\nK2]",
        "[[K1],K2",
        "[K1],K2]",
        "[K[K[K[K1]1]1]1]",
        "［[K1]］",
    ],
)
def test_deep_cross_line_or_partial_citation_never_leaves_live_residue(candidate: str) -> None:
    cleaned = runtime_module._strip_invented_citations(f"Left-{candidate}_right", ())

    assert cleaned == "Left- _right"
    assert runtime_module._citation_like_bracket_spans(cleaned) == []
    assert runtime_module._citation_labels(cleaned) == []
    assert runtime_module._strip_invented_citations(cleaned, ()) == cleaned


@pytest.mark.parametrize("interaction_mode", ["dialogue", "knowledge_work", "research"])
@pytest.mark.parametrize("initial_answer", [QUOTE_COLLAPSED_DRAFT, QUOTE_EXACT_DRAFT])
@pytest.mark.asyncio
async def test_repaired_quote_explanation_is_byte_exact_through_renderer_replay(
    interaction_mode: str,
    initial_answer: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": initial_answer}])
    prepare_context_calls: list[str] = []
    semantic_arbiter_calls: list[str] = []

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        interaction_mode=interaction_mode,
        message=QUOTE_REQUEST,
        prepare_context_calls=prepare_context_calls,
        semantic_arbiter_calls=semantic_arbiter_calls,
    )

    assert result["message"] == QUOTE_EXACT
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert len(router.calls) == 1
    assert prepare_context_calls == []
    assert semantic_arbiter_calls == []
    assert TelegramBridge._format_response_message(result) == QUOTE_EXACT  # noqa: SLF001
    assert TelegramBridge._format_response_message(result) == QUOTE_EXACT  # noqa: SLF001


@pytest.mark.parametrize("failure_kind", ["stale", "exception"])
@pytest.mark.asyncio
async def test_quote_withdrawal_failure_never_claims_exact_shape(
    failure_kind: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = {
        "action": "review",
        "queued_for_review": True,
        "inbox_id": "inbox_synthetic_stale_shape",
    }

    def fail_update(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        if failure_kind == "exception":
            raise RuntimeError("synthetic storage failure")
        return False

    monkeypatch.setattr(storage, "update_inbox_status", fail_update)
    router = _SequenceRouter([{"content": QUOTE_COLLAPSED_DRAFT}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        ingestion_result=pending,
    )

    assert pending == {
        "action": "review",
        "queued_for_review": True,
        "inbox_id": "inbox_synthetic_stale_shape",
    }
    assert "CHECK-43" not in str(result["message"])
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False


@pytest.mark.parametrize(
    "context_overrides",
    [
        {"current_attachment_present": True},
        {"person_document_inventory_settled": True},
        {"person_activity_resolution_failed": True},
        {"source_search_used": True},
        {"asked_for_an_archive": True},
        {"web_evidence_status": "sourced"},
        {"web_evidence_scope": "open_search"},
        {"web_sources": [{"title": "Synthetic public source"}]},
        {"web_evidence_tools": ["web_research"]},
    ],
    ids=[
        "current-attachment",
        "person-inventory",
        "person-resolution",
        "source-search",
        "archive-request",
        "web-status",
        "web-scope",
        "web-sources",
        "web-tools",
    ],
)
@pytest.mark.asyncio
async def test_late_quote_truth_carrier_never_claims_exact_shape(
    context_overrides: dict[str, object],
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": QUOTE_COLLAPSED_DRAFT}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        force_late_shape_context=True,
        context_overrides=context_overrides,
    )

    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False


@pytest.mark.asyncio
async def test_personal_context_keeps_collapsed_quote_unrepaired_and_warning_visible(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": QUOTE_COLLAPSED_DRAFT}])
    verification_calls: list[str] = []

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        knowledge_hits=[
            {
                "id": "ko_synthetic_shape",
                "title": "Synthetic evidence",
                "content": "Synthetic evidence.",
                "_rerank_score": 0.9,
            }
        ],
        answer_mode="personal_knowledge",
        retrieval_confidence=0.5,
        verify_answers=True,
        verification_calls=verification_calls,
        force_late_shape_context=True,
    )

    assert result["message"] == QUOTE_COLLAPSED
    assert result["grounding_warning"]
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert verification_calls == ["verify"]
    assert TelegramBridge._format_response_message(result) != QUOTE_COLLAPSED  # noqa: SLF001


@pytest.mark.parametrize(
    "answer",
    [
        "> “Short [K999] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
        "> “Short [K999] quote.”\nExplanation QUOTE-GAP-43. Control: CHECK-43.",
    ],
)
@pytest.mark.asyncio
async def test_quote_exact_body_is_citation_sanitized_before_capture_and_restore(
    answer: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        interaction_mode="research",
    )

    assert "[K999]" not in result["message"]
    assert len(str(result["message"]).splitlines()) == 2
    assert result["citations"] == []
    assert result["citation_notice"] == ""
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert TelegramBridge._format_response_message(result) == result["message"]  # noqa: SLF001
    assert TelegramBridge._format_response_message(result) == result["message"]  # noqa: SLF001


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "> “Short [K1, K2] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short quote.” Explanation [K1; K2] QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [K1] quote.” Explanation [K2] QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [\tK1; K2] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [ K1; K2] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short quote.” Explanation [K1,\nK2] QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [[K1], K2] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [K\u00a01] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [\u00a0K1] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [K\n1] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [ [K1] ] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [[\nK1]] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short ［K1］ quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [ᴋ1] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [[K1],K2 quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
        (
            "> “Short [K1],K2] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            "> “Short quote.”\nExplanation QUOTE-GAP-43.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_quote_grouped_unknown_citations_are_sanitized_before_exact_renderer_replay(
    answer: str,
    expected: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        interaction_mode="research",
    )

    assert result["message"] == expected
    assert "[K" not in str(result["message"]) and "[k" not in str(result["message"])
    assert result["citations"] == []
    assert result["exact_text_shape_owned"] is True
    assert metadata["exact_text_shape_owned"] is True
    assert TelegramBridge._format_response_message(result) == expected  # noqa: SLF001
    assert TelegramBridge._format_response_message(result) == expected  # noqa: SLF001
    rows = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    assert rows[-1]["content"] == expected


@pytest.mark.parametrize(
    ("answer", "hits", "sanitized"),
    [
        (
            "> “Short [K1, K99] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            [
                {
                    "id": "ko_known_one",
                    "title": "Synthetic known source one",
                    "content": "Short known quote one.",
                    "score": 1.0,
                    "_rerank_score": 0.9,
                }
            ],
            "> “Short [K1] quote.” Explanation QUOTE-GAP-43.",
        ),
        (
            "> “Short quote.” Explanation [k1; K2] QUOTE-GAP-43. Control: CHECK-43.",
            [
                {
                    "id": "ko_known_one",
                    "title": "Synthetic known source one",
                    "content": "Short known quote one.",
                    "score": 1.0,
                    "_rerank_score": 0.9,
                },
                {
                    "id": "ko_known_two",
                    "title": "Synthetic known source two",
                    "content": "Short known quote two.",
                    "score": 0.9,
                    "_rerank_score": 0.8,
                },
            ],
            "> “Short quote.” Explanation [k1, K2] QUOTE-GAP-43.",
        ),
        (
            "> “Short [K1] quote.” Explanation QUOTE-GAP-43. Control: CHECK-43.",
            [
                {
                    "id": "ko_known_one",
                    "title": "Synthetic known source one",
                    "content": "Short known quote one.",
                    "score": 1.0,
                    "_rerank_score": 0.9,
                }
            ],
            "> “Short [K1] quote.” Explanation QUOTE-GAP-43.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_quote_grouped_mixed_citations_keep_known_subset_in_normal_renderer_replay(
    answer: str,
    hits: list[dict[str, object]],
    sanitized: str,
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        interaction_mode="research",
        knowledge_hits=hits,
        force_late_shape_context=True,
    )

    assert result["message"] == sanitized
    assert "[K99]" not in str(result["message"])
    assert "K1" in runtime_module._citation_labels(str(result["message"]))
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    first = TelegramBridge._format_response_message(result)  # noqa: SLF001
    second = TelegramBridge._format_response_message(result)  # noqa: SLF001
    assert first == second
    assert first != sanitized
    assert "[K99]" not in first


@pytest.mark.asyncio
async def test_quote_citation_sanitation_that_removes_the_only_body_never_claims_exact_shape(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "> “[K1]” Explanation QUOTE-GAP-43. Control: CHECK-43."
    router = _SequenceRouter([{"content": answer}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        interaction_mode="research",
    )

    assert "[K1]" not in str(result["message"])
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False
    assert TelegramBridge._format_response_message(result) == result["message"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_quote_shape_never_owns_structural_turn(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    structural_answer = "Структурный факт подтверждён."
    router = _SequenceRouter([{"content": QUOTE_COLLAPSED_DRAFT}])

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        message=QUOTE_REQUEST,
        structural_answer=structural_answer,
        force_late_shape_context=True,
    )

    assert str(result["message"]).startswith(f"{structural_answer}\n\n")
    assert "Short quote" in str(result["message"])
    assert "QUOTE-GAP-43" in str(result["message"])
    assert result["exact_text_shape_owned"] is False
    assert metadata["exact_text_shape_owned"] is False


@pytest.mark.asyncio
async def test_chat_regeneration_cancellation_propagates_without_storing_an_assistant_draft(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Byte-exact neutral first draft without the required shape."
    router = _SequenceRouter([{"content": original}, asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await _run_chat_with_invalid_shape(
            settings=settings,
            storage=storage,
            monkeypatch=monkeypatch,
            router=router,
        )

    assert len(router.calls) == 2
    conversations = storage.list_conversations("alice")
    assert len(conversations) == 1
    messages = storage.get_conversation_messages(str(conversations[0]["id"]), user_id="alice")
    assert [item["role"] for item in messages] == ["user"]


@pytest.mark.asyncio
async def test_chat_does_not_regenerate_after_any_first_generation_evidence(
    settings: object,
    storage: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "Byte-exact neutral first draft without the required shape."
    router = _FakeRouter({"content": "- First MARK-SEG-20\n- Second"})

    result, metadata = await _run_chat_with_invalid_shape(
        settings=settings,
        storage=storage,
        monkeypatch=monkeypatch,
        router=router,
        first_response_override={
            "content": original,
            "tools_used": [],
            "tool_evidence": [{"tool": "synthetic_effect", "ok": True}],
        },
    )

    assert result["message"] == original
    assert router.calls == []
    assert metadata["text_shape_regeneration"] == {"accepted": False, "attempted": False}


def test_runtime_seam_suppresses_tools_and_revalidates_before_store() -> None:
    source = inspect.getsource(AgentRuntime.chat)

    parse = source.index("parsed_shape_contract = regenerable_text_shape_contract")
    isolate = source.index("generation_context = (", parse)
    suppress = source.index("visible_tools = []", parse)
    generate = source.index("await self._generate_response(generation_context", suppress)
    repair = source.index("repair_explicit_text_shape(asked_of_model, content)", generate)
    final_validate = source.index("text-shape: final validation failed", repair)
    store = source.index("self.storage.store_message(", final_validate)

    assert parse < isolate < suppress < generate < repair < final_validate < store
