"""The text-shape seam repairs only structurally unambiguous model slips."""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime
from friday.text_shape import repair_explicit_text_shape


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


def test_exact_word_list_projects_generated_phrases_to_distinct_atoms() -> None:
    request = "Подготовь список из трёх тестовых слов. Включи маркер WORD-731."
    answer = "- Тестовое слово 1\n- Тестовое слово 2\n- Тестовое слово 3 (маркер WORD-731)"

    assert repair_explicit_text_shape(request, answer) == ("- Тестовое\n- слово\n- WORD-731")


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


def test_word_list_with_emphasis_reaches_both_closed_contracts_in_one_call() -> None:
    request = "Подготовь список из трёх слов. Включи маркер WORD-731."
    answer = "- **первое слово**\n- второе слово\n- третье WORD-731"

    repaired = repair_explicit_text_shape(request, answer)

    assert repaired == "- первое\n- второе\n- WORD-731"
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


def test_repair_is_idempotent() -> None:
    request = "Оформи два шага списком. Включи идентификатор IDEMPOTENT-7."
    answer = "- первый шаг\n- второй шаг\nIDEMPOTENT-7"

    once = repair_explicit_text_shape(request, answer)

    assert once != answer
    assert repair_explicit_text_shape(request, once) == once


def test_runtime_seam_follows_content_guards_and_precedes_model_said() -> None:
    source = inspect.getsource(AgentRuntime.chat)

    seam = source.index("repair_explicit_text_shape(asked_of_model, content)")
    assert seam > source.index("office_model_claim_rejected =")
    assert seam > source.index("supported_deed_replaced = True")
    assert seam < source.index("model_said =")
    assert 'response["voice_clip"] = None' in source[seam : source.index("model_said =")]
