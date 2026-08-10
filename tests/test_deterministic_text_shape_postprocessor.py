"""The text-shape seam repairs only structurally unambiguous model slips."""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime
from friday.text_shape import (
    TEXT_SHAPE_INVALID,
    TEXT_SHAPE_VALID,
    exact_quote_explanation_shape_owned,
    explicit_text_shape_status,
    regenerable_text_shape_contract,
    repair_collapsed_quote_explanation_shape,
    repair_explicit_text_shape,
    strip_parser_control_metadata,
)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Ready MARK-SEG-14 CTRL-14.", "Ready MARK-SEG-14."),
        ("Ready MARK-SEG-14. Control: CTRL-14.", "Ready MARK-SEG-14."),
        ("Ready MARK-SEG-14. Check token: ctrl-14!", "Ready MARK-SEG-14."),
        (
            "First Control: CTRL-14. Second MARK-SEG-14.",
            "First. Second MARK-SEG-14.",
        ),
        ("- First MARK-SEG-14\n- CTRL-14", "- First MARK-SEG-14"),
        ("CTRL-14\nReady MARK-SEG-14\nКонтроль: CTRL-14", "Ready MARK-SEG-14"),
        ("Ready MARK-SEG-14\nControl:\n- CTRL-14", "Ready MARK-SEG-14\nControl:"),
        ("Ready MARK-SEG-14 XCTRL-14Y.", "Ready MARK-SEG-14 XCTRL-14Y."),
        ("Ready MARK-SEG-14 ControlCTRL-14.", "Ready MARK-SEG-14 ControlCTRL-14."),
        ("CTRL-14", ""),
    ],
)
def test_parser_control_metadata_is_removed_from_every_fallback_shape(
    answer: str,
    expected: str,
) -> None:
    request = "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14."

    stripped = strip_parser_control_metadata(request, answer)

    assert stripped == expected
    assert strip_parser_control_metadata(request, stripped) == stripped


def test_regeneration_contract_rejects_control_embedded_in_required_literal() -> None:
    request = "Write one sentence. Include marker X-CTRL-12-Y. Control: CTRL-12."

    assert regenerable_text_shape_contract(request) is None


@pytest.mark.parametrize(
    "user_text",
    [
        "Explain identifier CTRL-14. Control: CTRL-14.",
        "Rewrite the supplied file. Include marker MARK-14. Control: CTRL-14.",
        "If useful, write one sentence. Include marker MARK-14. Control: CTRL-14.",
    ],
)
def test_non_owned_control_wording_is_never_sanitized(user_text: str) -> None:
    answer = "The requested subject is CTRL-14."

    assert regenerable_text_shape_contract(user_text) is None
    assert strip_parser_control_metadata(user_text, answer) == answer


def test_ordinary_quote_control_word_is_never_deleted_from_semantic_content() -> None:
    user_text = "Return one quote and one separate explanation line. Include marker MARK-43. Control: banana."
    answer = "> “Fruit matters.” Explanation likes banana MARK-43."

    owned, candidate = repair_collapsed_quote_explanation_shape(user_text, answer)

    assert strip_parser_control_metadata(user_text, answer) == answer
    assert candidate == answer
    assert owned is True  # recognizable but deliberately not exact-owned
    assert not exact_quote_explanation_shape_owned(user_text, answer)


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            " Ready MARK-SEG-14.",
        ),
        (
            "Write one sentence. Include marker MARK-SEG-14. Control: CTRL-14.",
            "Ready MARK-SEG-14.  ",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20.",
            "- First MARK-SEG-20\r\n- Second\r\n",
        ),
    ],
)
def test_control_sanitizer_preserves_unrelated_whitespace_and_line_endings(
    user_text: str,
    answer: str,
) -> None:
    assert strip_parser_control_metadata(user_text, answer) == answer


def test_control_sanitizer_never_deletes_an_unrelated_punctuation_line() -> None:
    user_text = "Оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20."
    answer = "- First MARK-SEG-20\n---\nControl: CTRL-20."

    stripped = strip_parser_control_metadata(user_text, answer)

    assert stripped == "- First MARK-SEG-20\n---"
    assert explicit_text_shape_status(user_text, stripped) == TEXT_SHAPE_INVALID


def test_word_list_absorbs_a_terminal_required_literal_without_growing() -> None:
    request = "Составь список из трёх слов. Включи маркер CONTROL-731."
    answer = "- альфа\n- бета\n- гамма\nCONTROL-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- альфа\n- бета\n- CONTROL-731"
    assert len(repaired.splitlines()) == 3
    assert repaired.count("CONTROL-731") == 1


def test_word_list_discards_model_selected_terminal_prose() -> None:
    request = "Составь список из трёх слов. Включи маркер CONTROL-731."
    answer = "- альфа\n- бета\n- гамма\nОбязательный маркер CONTROL-731 включён."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- альфа\n- бета\n- CONTROL-731"
    assert repaired.count("CONTROL-731") == 1


def test_word_list_removes_parser_metadata_from_a_bounded_suffix() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль: TRACE-731."
    answer = "- alpha1\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: TRACE-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- alpha1\n- beta2\n- REQUIRED-731"
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_numbered_word_list_absorbs_one_metadata_suffix_without_a_blank() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731."
    answer = "1. alpha1\n2. beta2\n3. gamma3\nMarker: REQUIRED-731"

    assert repair_explicit_text_shape(request, answer) == ("1. alpha1\n2. beta2\n3. REQUIRED-731")


def test_word_list_removes_control_from_paired_parser_metadata() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731."
    answer = "- alpha1\n- beta2\n- gamma3\n\nМаркер: REQUIRED-731, контроль TRACE-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- alpha1\n- beta2\n- REQUIRED-731"
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_word_list_removes_explicitly_delimited_english_control_metadata() -> None:
    request = "Return a list of three words. Include marker REQUIRED-731. Control: TRACE-731."
    answer = "1. alpha1\n2. beta2\n3. gamma3\nMarker: REQUIRED-731; Control: TRACE-731."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "1. alpha1\n2. beta2\n3. REQUIRED-731"
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_exact_word_list_collapses_inline_marker_and_control_metadata() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731."
    answer = "- слово1\n- слово2\n- слово3 — маркер REQUIRED-731, контроль TRACE-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- слово1\n- слово2\n- REQUIRED-731"
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Подготовь компактный список из трёх тестовых слов. Включи маркер WORD-731. Контроль TRACE-731.",
            "- альфа\n- бета\n- Маркер [WORD-731]. Контроль TRACE-731",
            "- альфа\n- бета\n- WORD-731",
        ),
        (
            "Return a compact list of three test words. Include marker WORD-731. Control: TRACE-731.",
            "1. alpha\n2. Marker [WORD-731]. Control: TRACE-731.\n3. beta",
            "1. alpha\n2. WORD-731\n3. beta",
        ),
    ],
)
def test_word_list_projects_one_square_bracketed_metadata_row(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == expected
    assert explicit_text_shape_status(user_text, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Составь список из двух слов. Включи маркер WORD-742. Контроль TRACE-742.",
            "- alpha\n- Маркер [WORD-742]. Контроль TRACE-742",
            "- alpha\n- WORD-742",
        ),
        (
            "Return a list of ten words. Include marker WORD-750. Control: TRACE-750.",
            "\n".join(
                [
                    *(f"{index}. word{index}" for index in range(1, 10)),
                    "10. Marker [WORD-750]. Control: TRACE-750",
                ]
            ),
            "\n".join(
                [
                    *(f"{index}. word{index}" for index in range(1, 10)),
                    "10. WORD-750",
                ]
            ),
        ),
    ],
)
def test_inline_word_list_projection_is_bounded_from_two_to_ten(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == expected
    assert explicit_text_shape_status(user_text, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    "answer",
    [
        "- alpha\n- beta\n- Маркер [word-731]. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркер [XWORD-731Y]. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроль OTHER-731",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроль trace-731",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроль XTRACE-731Y",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроль TRACE-731 TRACE-731",
        "- alpha\n- beta WORD-731\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркер [WORD-731. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркер [[WORD-731]]. Контроль TRACE-731",
        "- alpha\n- beta\n- два слова — маркер WORD-731, контроль TRACE-731",
        "- alpha\n- beta\n- если — маркер WORD-731, контроль TRACE-731",
        "- alpha\n1. beta\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- alpha\n- alpha\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- alpha value\n- beta\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- alpha\n- 🙂\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- **alpha**\n- beta\n- Маркер [WORD-731]. Контроль TRACE-731",
        "- alpha\n- beta\n- Статус [WORD-731]. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркеризация [WORD-731]. Контроль TRACE-731",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроллер TRACE-731",
        "- alpha\n- beta\n- Идентификаторизация [WORD-731]. Проверка TRACE-731",
        "- alpha\n- beta\n- Токенизация [WORD-731]. Проверочная TRACE-731",
        "- alpha\n- beta\n- Маркер [WORD-731]. Контроль TRACE-731 EXTRA",
    ],
)
def test_inline_word_list_metadata_ambiguities_are_owned_fixed_points(answer: str) -> None:
    request = "Подготовь компактный список из трёх тестовых слов. Включи маркер WORD-731. Контроль TRACE-731."

    once = repair_explicit_text_shape(request, answer)

    assert once == answer
    assert repair_explicit_text_shape(request, once) == answer
    assert explicit_text_shape_status(request, answer) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("- alpha\n- beta\n- WORD-731", TEXT_SHAPE_VALID),
        ("- alpha\n- WORD-731\n- TRACE-731", TEXT_SHAPE_INVALID),
        ("- alpha\n- WORD-731\n- trace-731", TEXT_SHAPE_INVALID),
        ("- alpha\n- WORD-731\n- XTRACE-731Y", TEXT_SHAPE_INVALID),
        ("- alpha\n- WORD-731\n- OTHER-731", TEXT_SHAPE_INVALID),
    ],
)
def test_word_list_requires_marker_once_and_rejects_parser_control(
    answer: str,
    expected: str,
) -> None:
    request = "Составь список из трёх слов. Включи маркер WORD-731. Контроль TRACE-731."

    assert explicit_text_shape_status(request, answer) == expected


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731.",
        ),
        (
            "Return a list of three words. Include marker REQUIRED-731. Control: TRACE-731.",
            "1. alpha1\n2. Marker REQUIRED-731. Control: TRACE-731.\n3. beta2",
        ),
    ],
)
def test_word_list_removes_period_delimited_request_bound_inline_control(
    user_text: str,
    answer: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert "TRACE-731" not in repaired
    assert repaired.count("REQUIRED-731") == 1
    assert explicit_text_shape_status(user_text, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль OTHER-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731 пройден.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731? Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731! Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731: Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731.Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731.. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731 extra. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731 с пояснением. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Статус REQUIRED-731. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731. Контроль TRACE-731.",
        ),
        (
            "Составь из сообщения список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Не включай маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Не включай REQUIRED-731. Контроль TRACE-731.",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731. EXTRA-731",
        ),
    ],
)
def test_period_delimited_inline_control_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_period_delimited_inline_projection_does_not_follow_markup_cleanup() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731."
    answer = "- **alpha1**\n- beta2\n- Маркер REQUIRED-731. Контроль TRACE-731."

    assert repair_explicit_text_shape(request, answer) == answer
    assert repair_explicit_text_shape(request, repair_explicit_text_shape(request, answer)) == answer
    assert explicit_text_shape_status(request, answer) == TEXT_SHAPE_INVALID


def test_inline_marker_row_with_a_non_word_atom_is_a_fixed_point() -> None:
    request = "Return a list of three words. Include marker REQUIRED-731. Control: TRACE-731."
    answer = "1. alpha1\n2. marker REQUIRED-731; Control: TRACE-731\n3. 🙂"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == answer
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_INVALID
    assert repair_explicit_text_shape(request, repaired) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Составь из сообщения список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов: alpha1, beta2, gamma3. "
            "Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов и добавь пояснение. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha one\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- alpha1\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n1. beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль OTHER-731",
        ),
        (
            "Составь список из трёх слов. Контроль TRACE-731. "
            "Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            '- "alpha1"\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731',
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731\n- extra",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- REQUIRED-731\n- beta2",
        ),
    ],
)
def test_ambiguous_inline_marker_rows_are_byte_preserved(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_inline_marker_projection_does_not_follow_markup_cleanup() -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731."
    answer = "- **alpha1**\n- beta2\n- gamma3 — маркер REQUIRED-731, контроль TRACE-731"

    assert repair_explicit_text_shape(request, answer) == answer
    assert repair_explicit_text_shape(request, repair_explicit_text_shape(request, answer)) == answer
    assert explicit_text_shape_status(request, answer) == TEXT_SHAPE_INVALID


@pytest.mark.parametrize(
    "marker_row",
    [
        "gamma3 — не REQUIRED-731",
        "gamma3 — без REQUIRED-731",
        "gamma3 — не включай REQUIRED-731",
        "gamma3 — не надо включать REQUIRED-731",
        "gamma3 — ignore REQUIRED-731",
        "gamma3 — omit REQUIRED-731",
        "gamma3 — exclude the marker REQUIRED-731",
        "gamma3 — not REQUIRED-731",
        "gamma3 — without REQUIRED-731",
        "gamma3 — do not include REQUIRED-731",
        "gamma3 — don't add REQUIRED-731",
        "gamma3 — never output REQUIRED-731",
        "gamma3 — не включай, пожалуйста, REQUIRED-731",
        "gamma3 — запрещено включать REQUIRED-731",
        "gamma3 — не выводи ни при каких условиях REQUIRED-731",
        "gamma3 — следует полностью исключить из ответа REQUIRED-731",
        "gamma3 — skip REQUIRED-731",
        "gamma3 — remove REQUIRED-731",
        "gamma3 — do not, please, include REQUIRED-731",
    ],
)
def test_negated_inline_marker_rows_are_byte_preserved(marker_row: str) -> None:
    request = "Составь список из трёх слов. Включи маркер REQUIRED-731."
    answer = f"- alpha1\n- beta2\n- {marker_row}"

    assert repair_explicit_text_shape(request, answer) == answer


@pytest.mark.parametrize(
    "user_text",
    [
        "Составь из сообщения список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из чата список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из переписки список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из письма список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из приведённого текста список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из цитаты список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из пользовательской формулировки список из трёх слов. Включи маркер REQUIRED-731.",
        "Составь из «alpha1 beta2 gamma3» список из трёх слов. Включи маркер REQUIRED-731.",
        "Return from the message a list of three words. Include marker REQUIRED-731.",
        "Return from the chat a list of three words. Include marker REQUIRED-731.",
        "Return from the correspondence a list of three words. Include marker REQUIRED-731.",
        "Return from the email a list of three words. Include marker REQUIRED-731.",
        "Return from the supplied text a list of three words. Include marker REQUIRED-731.",
        "Return from the quote a list of three words. Include marker REQUIRED-731.",
        "Return from the user-provided wording a list of three words. Include marker REQUIRED-731.",
        'Return from "alpha1 beta2 gamma3" a list of three words. Include marker REQUIRED-731.',
    ],
)
def test_source_owned_word_list_suffix_blocks_are_byte_preserved(user_text: str) -> None:
    answer = "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731"

    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    "user_text",
    [
        "Составь из сообщения список из трёх слов. Включи маркер WORD-731.",
        "Return from the supplied text a list of three words. Include marker WORD-731.",
    ],
)
def test_source_owned_word_list_values_are_byte_preserved_without_a_suffix(user_text: str) -> None:
    answer = "- first value\n- second value\n- third WORD-731"

    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    "user_text",
    [
        "Составь для сообщения список из трёх новых слов. Включи маркер REQUIRED-731.",
        "Return a list of three invented words for a chat reply. Include marker REQUIRED-731.",
    ],
)
def test_direct_word_list_composition_still_repairs_with_destination_mentions(user_text: str) -> None:
    answer = "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731"

    assert repair_explicit_text_shape(user_text, answer) == "- alpha1\n- beta2\n- REQUIRED-731"


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: OTHER-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731\nControl: TRACE-731\nCheck: EXTRA-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731\nControl: TRACE-731\nToken: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731\nToken: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов и добавь пояснение. "
            "Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731 дважды.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов из файла. Включи маркер REQUIRED-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "- alpha1\n1. beta2\n- gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "- Alpha1\n- alpha1\n- gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "1. alpha1\n3. beta2\n4. gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Проверь TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: TRACE-731",
        ),
        (
            "Составь список из трёх слов. Контроль: TRACE-731. "
            "Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль: TRACE-731.",
            "- TRACE-731\n- beta2\n- gamma3\n\nMarker: REQUIRED-731\nControl: TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731.",
            "- **alpha1**\n- beta2\n- gamma3\nMarker: REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nМаркер: REQUIRED-731, контроль OTHER-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nМаркер: REQUIRED-731, контроль TRACE-731 пройден",
        ),
        (
            "Return a list of three words. Include marker REQUIRED-731. Control: TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: REQUIRED-731, control TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nМаркер REQUIRED-731, контроль TRACE-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nМаркер: TRACE-731, контроль REQUIRED-731",
        ),
        (
            "Составь список из трёх слов. Включи маркер REQUIRED-731. Контроль TRACE-731.",
            "- alpha1\n- beta2\n- gamma3\nMarker: TRACE-731\nControl: REQUIRED-731",
        ),
    ],
)
def test_unsafe_or_ambiguous_word_list_suffix_blocks_are_unchanged(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_multiword_non_marker_rows_are_byte_preserved() -> None:
    request = "Подготовь список из трёх тестовых слов. Включи маркер WORD-731."
    answer = "- Тестовое слово 1\n- Тестовое слово 2\n- Тестовое слово 3 (маркер WORD-731)"

    assert repair_explicit_text_shape(request, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Подготовь список из трёх слов: альфа, бета, гамма. Включи маркер WORD-731.",
            "- длинная альфа\n- длинная бета\n- длинная гамма WORD-731",
        ),
        (
            "Подготовь три слова из файла списком. Включи маркер WORD-731.",
            "- первое значение\n- второе значение\n- третье значение WORD-731",
        ),
        (
            "Подготовь список из трёх слов. Включи маркер WORD-731.",
            "- повтор\n- повтор\n- значение WORD-731",
        ),
        (
            "Подготовь список из трёх слов. Включи маркер WORD-731.",
            "- `первое слово`\n- второе слово\n- третье WORD-731",
        ),
    ],
)
def test_ambiguous_or_user_owned_word_lists_are_not_projected(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_word_list_emphasis_cleanup_does_not_collapse_multiword_rows() -> None:
    request = "Подготовь список из трёх слов. Включи маркер WORD-731."
    answer = "- **первое слово**\n- второе слово\n- третье WORD-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- первое слово\n- второе слово\n- третье WORD-731"
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Оформи два шага в виде списка. Включи идентификатор REF-42.",
            "- первый шаг\n- второй шаг\nREF-42",
            "- первый шаг\n- второй шаг REF-42",
        ),
        (
            "Верни два пункта списком. Включи токен ITEM-204.",
            "1. первый пункт\n2. второй пункт\nITEM-204",
            "1. первый пункт\n2. второй пункт ITEM-204",
        ),
    ],
)
def test_ordinary_list_absorbs_literal_into_an_existing_item(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == expected


def test_ordinary_list_absorbs_the_entire_terminal_overflow() -> None:
    request = "Оформи два шага в виде списка. Включи идентификатор REF-42."
    answer = "- первый шаг\n- второй шаг\nКонтрольный идентификатор REF-42 добавлен."

    assert repair_explicit_text_shape(request, answer) == (
        "- первый шаг\n- второй шаг Контрольный идентификатор REF-42 добавлен."
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "- первый шаг\n- второй шаг\n- Маркер: REF-42",
            "- первый шаг\n- второй шаг Маркер: REF-42",
        ),
        (
            "1. первый шаг\n2. второй шаг\n3. REF-42",
            "1. первый шаг\n2. второй шаг REF-42",
        ),
    ],
)
def test_ordinary_list_merges_one_terminal_literal_item_without_content_loss(
    answer: str,
    expected: str,
) -> None:
    request = "Оформи список из двух шагов. Включи маркер REF-42."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == expected
    assert repaired.splitlines()[0] == answer.splitlines()[0]
    assert all(value in repaired for value in ("первый шаг", "второй шаг", "REF-42"))
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Оформи два факта из файла списком. Включи маркер REF-42.",
            "- первый факт\n- второй факт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов: alpha, beta. Включи маркер REF-42.",
            "- alpha\n- beta\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов и добавь пояснение. Включи маркер REF-42.",
            "- первый пункт\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух слов. Включи маркер REF-42.",
            "- alpha\n- beta\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Повтори маркер REF-42 дважды.",
            "- первый пункт\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Не включай маркер REF-42.",
            "- первый пункт\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42, но не добавляй его.",
            "- первый пункт\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Return a list of two items. Include marker REF-42, but do not include it.",
            "- first item\n- second item\n- Marker: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- **первый пункт**\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            '- "первый пункт"\n- второй пункт\n- Маркер: REF-42',
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n2. второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "1. первый пункт\n3. второй пункт\n4. Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n-  второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый REF-42\n- второй пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n- второй пункт\n- Не включай REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n- второй пункт\n- Контрольный маркер REF-42 добавлен.",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n- второй REF-42\n- третий пункт",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n- второй пункт\n- третий пункт\n- Маркер: REF-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- первый пункт\n- второй пункт\n- Маркер: REF-42\n",
        ),
    ],
)
def test_terminal_literal_list_item_overflow_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer", "literal"),
    [
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
            "REF-SEG-42",
        ),
        (
            "Return a list of two items. Include marker REF-SEG-42. Control: TRACE-42.",
            "1. first item: check ready\n2. second item: Control: TRACE-42",
            "REF-SEG-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер SYN-TELEGRAM-A10-20. Контроль SYN-A10-20.",
            "- первый пункт: проверка готова\n- второй пункт: контроль SYN-A10-20",
            "SYN-TELEGRAM-A10-20",
        ),
        (
            "Сформируй три коротких пункта для итоговой проверки канала. "
            "Добавь маркер REF-SEG-43. Контроль TRACE-43.",
            "- первый пункт\n- второй пункт\n- третий пункт: контроль TRACE-43",
            "REF-SEG-43",
        ),
        (
            "Подготовь десять элементов для итоговой проверки канала. "
            "Вставь маркер REF-SEG-50. Контроль TRACE-50.",
            "\n".join([*(f"- пункт {index}" for index in range(1, 10)), "- контроль TRACE-50"]),
            "REF-SEG-50",
        ),
        (
            "Write three concise items for final channel check. Add marker REF-SEG-53. Control: TRACE-53.",
            "- first item\n- second item\n- third item: Control: TRACE-53",
            "REF-SEG-53",
        ),
        (
            "Create a list of ten items for final delivery validation. "
            "Append marker REF-SEG-60. Control: TRACE-60.",
            "\n".join([*(f"- item {index}" for index in range(1, 10)), "- Control: TRACE-60"]),
            "REF-SEG-60",
        ),
    ],
)
def test_missing_literal_is_appended_to_a_request_bound_control_row_without_content_loss(
    user_text: str,
    answer: str,
    literal: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == f"{answer} {literal}"
    assert repaired[: len(answer)] == answer
    assert repaired.count(literal) == 1
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    "user_text",
    [
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Добавь маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Помести маркер REF-SEG-42. Контроль TRACE-42.",
        "Подготовь два нейтральных пункта для финальной проверки доставки. "
        "Вставь маркер REF-SEG-42. Контроль TRACE-42.",
        "Составь список из двух элементов. Добавьте маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Add marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Append marker REF-SEG-42. Control: TRACE-42.",
        "Write the list of two items. Insert the marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов. Пожалуйста, включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items. Please include the marker REF-SEG-42. Control: TRACE-42.",
    ],
)
def test_missing_literal_append_requires_a_local_positive_imperative(user_text: str) -> None:
    answer = "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42"

    assert repair_explicit_text_shape(user_text, answer) == f"{answer} REF-SEG-42"


@pytest.mark.parametrize(
    "user_text",
    [
        "Оформи список из двух пунктов. Маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Для справки: маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Маркер REF-SEG-42 необязателен. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Маркер REF-SEG-42 можно добавить. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Если нужно, добавь маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Маркер REF-SEG-42 должен отсутствовать. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Устаревший маркер REF-SEG-42 игнорируй. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи заголовок. Маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42, но это указание отменено. "
        "Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42, но вместо этого используй "
        "заголовок. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42, а лучше используй заголовок. "
        "Контроль TRACE-42.",
        "Оформи список из двух пунктов. Добавь заголовок вместо маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Добавь заголовок и опиши маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. При желании включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи по возможности маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42 по желанию. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42, если проверка пройдёт. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Если проверка пройдёт, включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42 при условии успеха. Контроль TRACE-42.",
        "Return a list of two items. Marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. For reference: marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Marker REF-SEG-42 is optional. Control: TRACE-42.",
        "Return a list of two items. You may add marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. If needed, add marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Marker REF-SEG-42 must be absent. Control: TRACE-42.",
        "Return a list of two items. Ignore deprecated marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include a heading. Marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42, but that instruction is cancelled. "
        "Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42, but instead use a heading. "
        "Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42, but rather use a heading. Control: TRACE-42.",
        "Return a list of two items. Add a heading instead of marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Add a heading and describe marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. You can include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. You could include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42 if desired. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42 if you want. Control: TRACE-42.",
        "Return a list of two items. Feel free to include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42 if the check passes. Control: TRACE-42.",
        "Return a list of two items. If the check passes, include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42 unless the check fails. Control: TRACE-42.",
        "Оформи список из двух пунктов. Когда проверка завершится, включи маркер REF-SEG-42. "
        "Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42, только при успешной проверке. "
        "Контроль TRACE-42.",
        "Return a list of two items. When the check passes, include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. As long as the check passes, include marker REF-SEG-42. "
        "Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42 where appropriate. Control: TRACE-42.",
        "Return a list of two items. Depending on the check, include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов. Если проверка пройдёт; включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. При условии успеха; добавь маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Если проверка пройдёт\nвключи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items. If the check passes; include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Provided that the check passes; add marker REF-SEG-42. "
        "Control: TRACE-42.",
        "Return a list of two items. If the check passes\ninclude marker REF-SEG-42. Control: TRACE-42.",
        "Если проверка пройдёт. Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов с примечанием. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Для справки. Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Это важно. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items if the check passes. Include marker REF-SEG-42. Control: TRACE-42.",
        "For reference. Return a list of two items. Include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов? Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42? Контроль TRACE-42.",
        "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42?",
        "Return a list of two items? Include marker REF-SEG-42. Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42? Control: TRACE-42.",
        "Return a list of two items. Include marker REF-SEG-42. Control: TRACE-42?",
        "Оформи список из двух пунктов для быстрой локальной финальной штатной проверки. "
        "Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items for quick local final routine validation. "
        "Include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов для быстрой, локальной проверки. "
        "Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items for quick, local validation. Include marker REF-SEG-42. "
        "Control: TRACE-42.",
        "Оформи список из двух пунктов для трёх проверок. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items for three checks. Include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов! Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items! Include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов; Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items; Include marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов.\nВключи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items.\nInclude marker REF-SEG-42. Control: TRACE-42.",
        "Оформи список из двух пунктов. Сохрани стиль. Включи маркер REF-SEG-42. Контроль TRACE-42.",
        "Return a list of two items. Keep the style. Include marker REF-SEG-42. Control: TRACE-42.",
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. "
            + "служебный блок " * 16
            + "Но он необязателен. Контроль TRACE-42."
        ),
        (
            "Return a list of two items. Include marker REF-SEG-42. "
            + "neutral filler " * 16
            + "However, omit it. Control: TRACE-42."
        ),
    ],
)
def test_missing_literal_append_rejects_label_optional_reference_and_unrelated_authority(
    user_text: str,
) -> None:
    answer = "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42"

    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Оформи список из двух пунктов. Включи маркер REF-42. Контроль TRACE-42.",
            "- первый пункт: код REF-99\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF42. Контроль TRACE-42.",
            "- первый пункт: код REF43\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: контроль OTHER-99\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: проверка готова\n- второй пункт: контроль trace-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: контроль TRACE-42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: контроль TRACE-42\n- второй пункт: проверка готова",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: XTRACE-42Y\n- второй пункт: проверка готова",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: метка OTHER-42\n- второй пункт: контроль TRACE-42",
        ),
    ],
)
def test_missing_literal_append_owned_ambiguities_block_later_cleanup(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Оформи из файла список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов: alpha, beta. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- alpha: проверка готова\n- beta: контроль TRACE-42",
        ),
        (
            "Оформи список из двух слов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- alpha\n- контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов и добавь пояснение. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Повтори маркер REF-SEG-42 дважды. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42, но не добавляй его. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42. "
            "Учитывай код REF-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль OTHER-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: контроль TRACE-42\n- второй пункт: проверка готова",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: контроль TRACE-42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42 выполнен",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42 REF-SEG-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: XREF-SEG-42Y\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: XTRACE-42Y\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: код REF-42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: код RFX-SEG-42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: код REF.SEG.42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: код REF-SEG-43\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: маркер OTHER-42\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- единственный пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42\n- третий пункт",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\nобычная строка: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n1. второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n* второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "1. первый пункт: проверка готова\n2) второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42\n",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- первый пункт: проверка готова\n- второй пункт: контроль TRACE-42 ",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- **первый пункт**: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- `первый пункт`: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            '- "первый пункт": проверка готова\n- второй пункт: контроль TRACE-42',
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- https://example.invalid\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- пояснение: проверка готова\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- не добавляй значение\n- второй пункт: контроль TRACE-42",
        ),
        (
            "Оформи список из двух пунктов. Включи маркер REF-SEG-42. Контроль TRACE-42.",
            "- повтори значение\n- второй пункт: контроль TRACE-42",
        ),
    ],
)
def test_missing_literal_control_anchor_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "- REF-42: первый пункт\n-: второй пункт",
            "- REF-42: первый пункт\n- второй пункт",
        ),
        (
            "•: первый пункт\n• второй пункт REF-42",
            "• первый пункт\n• второй пункт REF-42",
        ),
    ],
)
def test_exact_list_removes_only_one_colon_inside_a_bullet_prefix(
    answer: str,
    expected: str,
) -> None:
    request = "Оформи маркированный список из двух пунктов. Включи маркер REF-42."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == expected
    assert repaired.count("REF-42") == 1
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_telegram_two_bullet_draft_repairs_one_malformed_prefix_without_echoing_control() -> None:
    request = (
        "Ответь коротким маркированным списком из двух пунктов для Telegram. "
        "Включи маркер TEST-MARK-01. Контроль TEST-CONTROL-01."
    )
    draft = "- TEST-MARK-01: первый пункт\n-: второй пункт"

    repaired = repair_explicit_text_shape(request, draft)

    assert repaired == "- TEST-MARK-01: первый пункт\n- второй пункт"
    assert "TEST-CONTROL-01" not in repaired
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Оформи список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи нумерованный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи два факта из файла маркированным списком. Включи маркер REF-42.",
            "- REF-42: первый факт\n-: второй факт",
        ),
        (
            "Оформи маркированный список из двух пунктов: alpha, beta. Включи маркер REF-42.",
            "- REF-42: alpha\n-: beta",
        ),
        (
            "Оформи маркированный список из двух пунктов и добавь пояснение. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух слов. Включи маркер REF-42.",
            "- REF-42: alpha\n-: beta",
        ),
        (
            "Оформи маркированный список из двух пунктов. Не включай маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42, но не добавляй его.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- **REF-42: первый пункт**\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            '- "REF-42: первый пункт"\n-: второй пункт',
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: https://example.invalid\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n*: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "1. REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-:  второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "-: REF-42: первый пункт\n-: второй пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\nобычная строка",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт\n- третий пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй REF-42",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- первый: пункт REF-42\n- второй: пункт",
        ),
        (
            "Оформи маркированный список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт\n",
        ),
        (
            "Оформи не список из двух пунктов. Включи маркер REF-42.",
            "- REF-42: первый пункт\n-: второй пункт",
        ),
    ],
)
def test_bullet_colon_prefix_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Напиши короткую строку с амперсандом для проверки экранирования. Включи маркер AMP-42.",
            "Левая часть < 5 и правая > 3. AMP-42",
            "Левая часть < 5 & правая > 3. AMP-42",
        ),
        (
            "Write a short line with an ampersand for an escaping check. Include marker AMP-42.",
            "left side and right side AMP-42",
            "left side & right side AMP-42",
        ),
    ],
)
def test_explicit_ampersand_line_replaces_only_one_conjunction_carrier(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == expected
    assert repaired.count("&") == 1
    assert repaired.count("AMP-42") == 1
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "левая часть & правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "левая и средняя и правая части AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "единая часть AMP-42",
        ),
        (
            "Напиши строку с амперсандом. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Write a short line and avoid ampersand. Include marker AMP-42.",
            "left side and right side AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "левая часть и правая часть\nAMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом из файла. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку без амперсанда. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом, но не используй его. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку со словом амперсанд. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом &. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом \\&. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            '"левая часть и правая часть" AMP-42',
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "`левая часть и правая часть` AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "**левая часть и правая часть** AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "ссылка https://example.invalid и текст AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "<b>левая</b> и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Не включай маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом и пояснение. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши разбор фразы «короткая строка с амперсандом». Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42",
        ),
        (
            "Напиши короткую строку с амперсандом. Включи маркер AMP-42.",
            "левая часть и правая часть AMP-42 AMP-42",
        ),
    ],
)
def test_ampersand_carrier_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_unrequested_blockquote_around_exact_list_is_removed_without_content_loss() -> None:
    request = "Ответь маркированным списком из двух пунктов. Включи маркер LIST-42."
    answer = "> LIST-42\n> - Первый пункт\n> - Второй пункт\n> Контроль CHECK-7"

    assert repair_explicit_text_shape(request, answer) == (
        "- LIST-42 Первый пункт\n- Второй пункт Контроль CHECK-7"
    )


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Оформи цитату со списком из двух пунктов. Включи маркер LIST-42.",
            "> LIST-42\n> - Первый\n> - Второй",
        ),
        (
            "Ответь списком из двух пунктов. Включи маркер LIST-42.",
            "> LIST-42\n> - Первый\n- Второй",
        ),
        (
            "Ответь списком из двух пунктов. Включи маркер LIST-42.",
            "> LIST-42\n> вступление\n> примечание\n> пояснение\n> - Первый\n> - Второй",
        ),
        (
            "Ответь списком из двух пунктов. Включи маркер LIST-42.",
            "> LIST-42\n> - Первый\n> пояснение между пунктами\n> - Второй",
        ),
        (
            "Ответь списком из двух пунктов. Включи маркер LIST-42.",
            ">> LIST-42\n>> - Первый\n>> - Второй",
        ),
        (
            "Ответь двумя фактами из файла списком. Включи маркер LIST-42.",
            "> LIST-42\n> - Первый\n> - Второй",
        ),
    ],
)
def test_ambiguous_or_requested_blockquote_list_is_unchanged(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "- **Первый пункт EMPH-42**\n- **Второй пункт**",
            "- Первый пункт EMPH-42\n- Второй пункт",
        ),
        (
            "1. *Первый пункт EMPH-42*\n2. *Второй пункт*",
            "1. Первый пункт EMPH-42\n2. Второй пункт",
        ),
        (
            "- __Первый пункт EMPH-42__\n- _Второй пункт_",
            "- Первый пункт EMPH-42\n- Второй пункт",
        ),
    ],
)
def test_unrequested_balanced_emphasis_is_unwrapped_without_content_loss(
    answer: str,
    expected: str,
) -> None:
    request = "Сделай список из двух пунктов без внешних данных. Включи маркер EMPH-42."

    assert repair_explicit_text_shape(request, answer) == expected


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Сделай два пункта жирным шрифтом. Включи маркер BOLD-42.",
            "- **Первый BOLD-42**\n- **Второй**",
        ),
        (
            "Сделай два пункта курсивом. Включи маркер ITALIC-42.",
            "- *Первый ITALIC-42*\n- *Второй*",
        ),
    ],
)
def test_explicitly_requested_emphasis_is_unchanged(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Сформируй краткий ответ с выделением слова «готово». Включи маркер EMPH-42.",
            "готово (EMPH-42)",
            "**готово** (EMPH-42)",
        ),
        (
            "Сформируй краткий ответ с выделением слова «готово». Включи маркер EMPH-42.",
            "Готово (EMPH-42)",
            "**Готово** (EMPH-42)",
        ),
        (
            "Сформируй краткий ответ с выделением слова «готово».",
            "Статус: готово.",
            "Статус: **готово**.",
        ),
        (
            "Напиши строку, выдели курсивом слово «готово».",
            "Статус готово.",
            "Статус *готово*.",
        ),
        (
            "Напиши строку, выдели жирным слово «готово».",
            "Статус готово.",
            "Статус **готово**.",
        ),
    ],
)
def test_missing_explicit_single_word_emphasis_is_added(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == expected


@pytest.mark.parametrize("answer", ["**готово** (EMPH-42)", "*готово* (EMPH-42)"])
def test_generic_emphasis_keeps_an_already_valid_style(answer: str) -> None:
    request = "Сформируй краткий ответ с выделением слова «готово». Включи маркер EMPH-42."

    assert repair_explicit_text_shape(request, answer) == answer


def test_mixed_case_duplicate_emphasis_targets_remain_unchanged() -> None:
    request = "Сформируй краткий ответ с выделением слова «готово». Включи маркер EMPH-42."
    answer = "Готово, снова готово (EMPH-42)."

    assert repair_explicit_text_shape(request, answer) == answer


@pytest.mark.parametrize("unicode_i", ["İ", "ı"])
def test_unicode_i_variants_do_not_gain_ascii_i_emphasis(unicode_i: str) -> None:
    request = 'Write a short reply and emphasize the word "i". Include marker EMPH-42.'
    answer = f"{unicode_i} (EMPH-42)"

    assert repair_explicit_text_shape(request, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Напиши разбор фразы «выдели слово “готово”».",
            "готово",
        ),
        (
            "Сформируй ответ без выделения слова «готово».",
            "готово",
        ),
        (
            "Сформируй ответ, выдели слова «готово» и «принято».",
            "готово и принято",
        ),
        (
            "Сформируй ответ с выделением слов «полностью готово».",
            "полностью готово",
        ),
        (
            "Сформируй ответ с выделением слова «готово».",
            "готово, снова готово",
        ),
        (
            "Сформируй ответ с выделением слова «готово».",
            "`готово`",
        ),
        (
            "Сформируй ответ с выделением слова «готово».",
            "**важно**: готово",
        ),
        (
            "Сформируй ответ с выделением слова «готово».",
            "<b>готово</b>",
        ),
        (
            "Сформируй ответ, выдели жирным и курсивом слово «готово».",
            "готово",
        ),
    ],
)
def test_ambiguous_or_unsafe_emphasis_requests_fail_closed(
    user_text: str,
    answer: str,
) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    "answer",
    [
        "- **Незакрытый MALFORMED-42\n- второй",
        "- **Внешний *внутренний* MALFORMED-42**\n- второй",
        "- `код` и **MALFORMED-42**\n- второй",
        "- [**MALFORMED-42**](https://example.invalid)\n- второй",
    ],
)
def test_malformed_nested_code_or_link_emphasis_fails_closed(answer: str) -> None:
    request = "Сделай список из двух пунктов. Включи маркер MALFORMED-42."

    assert repair_explicit_text_shape(request, answer) == answer


def test_quote_plus_explanation_closes_the_quote_before_line_two() -> None:
    request = "Верни одну цитату и строку пояснения. Включи маркер QUOTE-42."
    answer = "> Короткая цитата\n> Пояснение QUOTE-42"

    assert repair_explicit_text_shape(request, answer) == "> Короткая цитата\nПояснение QUOTE-42"


@pytest.mark.parametrize(
    ("user_text", "answer", "expected"),
    [
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
            "> Короткая цитата\nПояснение готово QUOTE-GAP-42",
        ),
        (
            "Return one quote and one explanation line. Include marker QUOTE-GAP-43.",
            "> Short quotation\n> \t\n> Explanation is ready QUOTE-GAP-43",
            "> Short quotation\nExplanation is ready QUOTE-GAP-43",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Добавь маркер QUOTE-GAP-44.",
            "  > Цитата QUOTE-GAP-44\n  >\t\n  > Пояснение готово",
            "  > Цитата QUOTE-GAP-44\nПояснение готово",
        ),
        (
            "Return one quote and one separate explanation line. Include marker QUOTE-GAP-45.",
            "> Short quotation\n>\n> Separate explanation QUOTE-GAP-45",
            "> Short quotation\nSeparate explanation QUOTE-GAP-45",
        ),
        (
            "Верни одну цитату и одну отдельную строку пояснения. Включи маркер QUOTE-GAP-46.",
            "> Короткая цитата\n>\n> Отдельное пояснение QUOTE-GAP-46",
            "> Короткая цитата\nОтдельное пояснение QUOTE-GAP-46",
        ),
    ],
)
def test_empty_quote_separator_before_explanation_is_removed_without_content_loss(
    user_text: str,
    answer: str,
    expected: str,
) -> None:
    repaired = repair_explicit_text_shape(user_text, answer)

    assert repaired == expected
    assert repaired.splitlines()[0] == answer.splitlines()[0]
    assert repaired.splitlines()[1] == answer.splitlines()[2].lstrip(" >\t")
    assert repair_explicit_text_shape(user_text, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Верни из файла одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни две цитаты и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и две строки пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения, если проверка успешна. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату; пояснение необязательно. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одно пояснение. Не включай маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одно пояснение. Повтори маркер QUOTE-GAP-42 дважды.",
            "> Короткая цитата QUOTE-GAP-42\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n> служебный текст\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n >\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n > Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42\n> ещё строка",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово XQUOTE-GAP-42Y",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата QUOTE-GAP-42\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово quote-gap-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение без маркера",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> **Короткая цитата**\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> `Пояснение` готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> [Пояснение](https://example.invalid) QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> <b>Пояснение</b> готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n>> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42\n",
        ),
        (
            "Верни одну цитату и одну строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата \n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одно пояснение внутри этой цитаты. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Return one quote and one explanation inside the quote. Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Return one quote and keep one explanation in the same quote. Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Return one explanation as one quote. Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Return text discussing the terms one quote and one explanation line. "
            "Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну строку пояснения, затем упомяни ещё одну цитату. "
            "Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
        (
            "Return one quote and one quoted explanation line. Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Return one quote and one blockquoted explanation line. Include marker QUOTE-GAP-42.",
            "> Short quotation\n>\n> Explanation is ready QUOTE-GAP-42",
        ),
        (
            "Верни одну цитату и одну цитированную строку пояснения. Включи маркер QUOTE-GAP-42.",
            "> Короткая цитата\n>\n> Пояснение готово QUOTE-GAP-42",
        ),
    ],
)
def test_empty_quote_separator_repair_ambiguities_are_byte_preserved(
    user_text: str,
    answer: str,
) -> None:
    first = repair_explicit_text_shape(user_text, answer)

    assert first == answer
    assert repair_explicit_text_shape(user_text, first) == answer


def test_literal_dedupe_can_precede_one_structural_repair() -> None:
    request = "Верни одну цитату и строку пояснения. Включи маркер DUPE-42."
    answer = "> Короткая цитата DUPE-42\n> Пояснение DUPE-42 ."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "> Короткая цитата DUPE-42\nПояснение."
    assert repaired.count("DUPE-42") == 1


def test_explicit_repeat_contract_prevents_literal_dedupe() -> None:
    request = "Напиши одну цитату и пояснение. Повтори маркер REPEAT-42 дважды."
    answer = "> Цитата REPEAT-42\nПояснение REPEAT-42."

    assert repair_explicit_text_shape(request, answer) == answer


def test_redundant_standalone_literal_line_is_removed() -> None:
    request = "Сделай список из двух пунктов. Включи маркер REF-42."
    answer = "- один REF-42\n- два\nREF-42"

    assert repair_explicit_text_shape(request, answer) == "- один REF-42\n- два"


def test_angle_request_wraps_one_terminal_literal_without_moving_it() -> None:
    request = "Напиши одно предложение с символами меньше и больше. Включи маркер ANGLE-42."
    answer = "Безопасная строка готова. ANGLE-42"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "Безопасная строка готова. <ANGLE-42>"
    assert repaired.count("ANGLE-42") == 1


def test_angle_request_wraps_one_named_literal_in_place() -> None:
    request = "Напиши одно предложение с символами меньше и больше. Включи маркер ANGLE-42."
    answer = "Значение описано словами. ANGLE-42. Контроль CHECK-42."

    assert repair_explicit_text_shape(request, answer) == (
        "Значение описано словами; <ANGLE-42>; Контроль CHECK-42."
    )


def test_one_sentence_repair_composes_with_angle_repair() -> None:
    request = "Напиши одно предложение с символами меньше и больше. Включи маркер ANGLE-42."
    answer = "Значение 5 больше 3, но меньше 10. ANGLE-42. Контроль CHECK-42."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "Значение 5 больше 3, но меньше 10; <ANGLE-42>; Контроль CHECK-42."
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_one_sentence_repair_accepts_a_clause_ending_in_a_single_digit() -> None:
    request = "Напиши одно предложение с угловыми скобками. Включи маркер ANGLE-42."
    answer = "Значение 5 больше 3, а 1 меньше 2. Маркер <ANGLE-42>, контроль CHECK-42."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == ("Значение 5 больше 3, а 1 меньше 2; Маркер <ANGLE-42>, контроль CHECK-42.")
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_one_sentence_repair_rejects_inline_numeric_enumeration() -> None:
    request = "Напиши одно предложение о вариантах."
    answer = "Варианты 1. Первый готов. 2. Второй готов."

    assert repair_explicit_text_shape(request, answer) == answer


def test_one_sentence_repair_keeps_a_decimal_version_intact() -> None:
    request = "Напиши одно предложение о версии."
    answer = "Версия 3.14 готова. Проверка завершена."

    assert repair_explicit_text_shape(request, answer) == "Версия 3.14 готова; Проверка завершена."


def test_one_sentence_repair_does_not_depend_on_a_named_literal() -> None:
    request = "Напиши одно предложение о готовности сервиса."
    answer = "Сервис готов. Проверка завершена. Всё работает."

    assert repair_explicit_text_shape(request, answer) == ("Сервис готов; Проверка завершена; Всё работает.")


@pytest.mark.parametrize(
    "user_text",
    [
        "Ответь одним предложением о готовности.",
        "Ответь единственным предложением о готовности.",
        "Write a single sentence about readiness.",
    ],
)
def test_single_sentence_contract_accepts_bounded_language_variants(user_text: str) -> None:
    assert repair_explicit_text_shape(user_text, "Сервис готов. Проверка завершена.") == (
        "Сервис готов; Проверка завершена."
    )


@pytest.mark.parametrize(
    "user_text",
    [
        "Напиши предложения про один сервис.",
        "Write sentences about one service.",
        "Напиши предложения, но не одно.",
        "Не пиши одним предложением о готовности.",
    ],
)
def test_unbound_or_negated_one_does_not_create_a_sentence_contract(user_text: str) -> None:
    answer = "Сервис готов. Проверка завершена."

    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    "user_text",
    [
        "Напиши единственно верные предложения о сервисе.",
        "Напиши одно предложение не надо, лучше два предложения.",
        "Напиши одно предложение, а лучше два.",
        "Write one sentence, but actually make it two.",
        "Напиши ответ, не ограничиваясь при этом одним предложением.",
        "Напиши разбор термина одно предложение.",
    ],
)
def test_plural_conflicting_and_metalinguistic_sentence_mentions_fail_closed(user_text: str) -> None:
    answer = "Первое верно. Второе верно."

    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    "answer",
    [
        "А. С. Пушкин готов. Проверка завершена.",
        "1. Первый этап готов. 2. Второй этап готов.",
        "10. Десятый этап готов. 11. Следующий этап готов.",
        "II. Второй этап готов. III. Третий этап готов.",
        "(10). Десятый этап готов. (11). Следующий этап готов.",
        "Ждать 5 мин. Затем запускать. Всё готово.",
        "Ждать 30 сек. Затем запускать. Всё готово.",
        "Первое готово. Второе завершено. Третье без точки",
    ],
)
def test_initials_numbered_prose_and_missing_terminal_punctuation_fail_closed(answer: str) -> None:
    request = "Напиши одно предложение о результате."

    assert repair_explicit_text_shape(request, answer) == answer


def test_requested_emphasis_and_sentence_shape_are_repaired_in_one_call() -> None:
    request = "Напиши одно предложение и выдели слово «готово»."
    answer = "**готово**. Проверка завершена."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "**готово**; Проверка завершена."
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_angle_unwrap_join_and_wrap_reach_a_fixed_point_in_one_call() -> None:
    request = "Напиши одно предложение с угловыми скобками. Включи маркер ANGLE-42."
    answer = "Статус **ANGLE-42**. Проверка завершена."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "Статус <ANGLE-42>; Проверка завершена."
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    "answer",
    [
        "**Внешний _внутренний_**. Проверка завершена.",
        "~~Первое~~. Проверка завершена.",
        "# Первый итог. Проверка завершена.",
    ],
)
def test_nested_or_unsupported_markdown_blocks_sentence_repair(answer: str) -> None:
    request = "Напиши одно предложение о результате."

    assert repair_explicit_text_shape(request, answer) == answer


def test_unrequested_emphasis_and_word_overflow_reach_a_fixed_point() -> None:
    request = "Составь список из трёх слов. Включи маркер WORD-731."
    answer = "- **альфа**\n- бета\n- гамма\nWORD-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- альфа\n- бета\n- WORD-731"
    assert repair_explicit_text_shape(request, repaired) == repaired


def test_unrequested_emphasis_and_quote_repair_reach_a_fixed_point() -> None:
    request = "Верни одну цитату и строку пояснения. Включи маркер QUOTE-42."
    answer = "> **Цитата QUOTE-42**\n> Пояснение."

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "> Цитата QUOTE-42\nПояснение."
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        ("Напиши одно предложение о версии.", "Версия 3.14 и build.v1 готова."),
        ("Напиши одно предложение о сокращении.", "Используй т.е. краткую форму. Затем итог."),
        ("Напиши одно предложение.", "Она сказала: «Готово. Можно запускать». Итог принят."),
        ("Напиши одно предложение.", "Первая строка.\nВторая строка."),
        ("Напиши два предложения.", "Первое готово. Второе готово."),
        ("Оформи одним предложением факты из файла.", "Первый факт. Второй факт."),
        ("Напиши одно предложение.", "Готово? Можно запускать!"),
        ("Напиши одно предложение.", "Смотри [описание](https://example.invalid). Всё готово."),
        ("Напиши одно предложение.", "Верни `alpha.beta`. Затем итог."),
    ],
)
def test_ambiguous_single_sentence_repairs_fail_closed(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Напиши одно предложение: пять меньше десяти и больше нуля. Включи маркер ANGLE-42.",
            "Пять меньше десяти ANGLE-42.",
        ),
        (
            "Напиши одно предложение с фразой «угловые скобки». Включи маркер ANGLE-42.",
            "ANGLE-42 готов.",
        ),
        (
            "Напиши одно предложение с угловыми скобками: оберни слово «готово». Включи маркер ANGLE-42.",
            "готово ANGLE-42.",
        ),
        (
            "Напиши одно предложение с угловыми скобками. Включи маркер «b».",
            "Статус b.",
        ),
    ],
)
def test_ambiguous_or_markup_angle_target_is_unchanged(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_angle_target_in_unrequested_emphasis_is_only_unwrapped() -> None:
    request = "Напиши одно предложение с угловыми скобками. Включи маркер ANGLE-42."
    answer = "Статус **ANGLE-42**."

    assert repair_explicit_text_shape(request, answer) == "Статус <ANGLE-42>."


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Составь список из трёх слов. Включи маркер CONTROL-731.",
            "- альфа\n- бета\n- CONTROL-731",
        ),
        (
            "Верни одну цитату и строку пояснения. Включи маркер QUOTE-42.",
            "> Короткая цитата\nПояснение QUOTE-42",
        ),
        (
            "Напиши одно предложение с угловыми скобками. Включи маркер ANGLE-42.",
            "Безопасная строка <ANGLE-42>.",
        ),
    ],
)
def test_already_valid_answers_are_byte_for_byte_unchanged(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


@pytest.mark.parametrize(
    ("user_text", "answer"),
    [
        (
            "Мы обсуждали список из двух пунктов и маркер REF-42.",
            "- один\n- два\nREF-42",
        ),
        (
            "Напиши разбор фразы «сделай список из двух пунктов и включи маркер REF-42».",
            "- один\n- два\nREF-42",
        ),
        (
            "Сделай список. Включи маркер REF-42.",
            "- один\n- два\nREF-42",
        ),
        (
            "Сделай список из двух пунктов. Включи маркер REF-42 и маркер REF-43.",
            "- один\n- два\nREF-42\nREF-43",
        ),
        (
            "Напиши не список из двух пунктов. Включи маркер REF-42.",
            "- один\n- два\nREF-42",
        ),
        (
            "Оформи два факта из файла списком. Включи маркер REF-42.",
            "- первый факт\n- второй факт\nREF-42",
        ),
        (
            "Оформи два значения из файла списком. Включи маркер REF-42.",
            "- первое значение\n- второе значение\nКонтрольный маркер REF-42 добавлен.",
        ),
        (
            "Составь список из трёх слов: альфа, бета, гамма. Включи маркер REF-42.",
            "- альфа\n- бета\n- гамма\nREF-42",
        ),
        (
            "Сделай список из двух пунктов. Включи маркер REF-42.",
            "- один\n- два\nлишний текст\nREF-42",
        ),
        (
            "Верни две цитаты и строку пояснения. Включи маркер QUOTE-42.",
            "> первая\n> пояснение QUOTE-42",
        ),
        (
            "Напиши одно предложение с угловыми скобками. Включи маркер ANGLE-42.",
            "Ссылка [ANGLE-42](https://example.invalid) уже оформлена",
        ),
    ],
)
def test_ambiguous_or_reported_contracts_fail_closed(user_text: str, answer: str) -> None:
    assert repair_explicit_text_shape(user_text, answer) == answer


def test_exact_literal_tail_after_one_blank_is_merged_into_the_last_list_row() -> None:
    request = "Оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20."
    answer = "1. первый нейтральный пункт\n2. второй нейтральный пункт\n\nMARK-SEG-20"
    expected = "1. первый нейтральный пункт\n2. второй нейтральный пункт MARK-SEG-20"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == expected
    assert explicit_text_shape_status(request, repaired) == TEXT_SHAPE_VALID
    assert repair_explicit_text_shape(request, repaired) == repaired


@pytest.mark.parametrize(
    "answer",
    [
        "1. SYN-TEЛЕГРАМ-A10-03",
        "1. первый пункт\n2. второй пункт\n\nMARK-SEG-20 extra",
        "1. первый пункт\n2. второй пункт\n\n\nMARK-SEG-20",
    ],
)
def test_structural_list_repairs_leave_ambiguous_and_confusable_drafts_unchanged(
    answer: str,
) -> None:
    request = "Оформи список из двух пунктов. Включи маркер MARK-SEG-20. Контроль CTRL-20."

    assert repair_explicit_text_shape(request, answer) == answer


def test_repair_is_idempotent() -> None:
    request = "Оформи два шага списком. Включи идентификатор IDEMPOTENT-7."
    answer = "- первый шаг\n- второй шаг\nIDEMPOTENT-7"

    once = repair_explicit_text_shape(request, answer)

    assert once != answer
    assert repair_explicit_text_shape(request, once) == once


def test_runtime_seam_follows_content_guards_and_precedes_model_said() -> None:
    source = inspect.getsource(AgentRuntime.chat)

    seam = source.index("repair_explicit_text_shape(asked_of_model, content)")
    status = source.index("shape_status = explicit_text_shape_status(asked_of_model, repaired_shape)")
    regeneration_gate = source.index("if shape_contract is not None and shape_status == TEXT_SHAPE_INVALID")
    assert seam > source.index("office_model_claim_rejected =")
    assert seam > source.index("supported_deed_replaced = True")
    assert seam < status < regeneration_gate
    assert seam < source.index("model_said =")
    assert 'response["voice_clip"] = None' in source[seam : source.index("model_said =")]
