"""Strict boundaries for repairing model-flattened Telegram markup."""

from __future__ import annotations

import pytest

from friday.telegram_bridge._markup import to_telegram_html


def test_partial_bold_markdown_link_labels_are_not_reflowed() -> None:
    source = (
        "[см. **Первый**](https://example.invalid/1). "
        "[см. **Второй**](https://example.invalid/2). "
        "[см. **Третий**](https://example.invalid/3)."
    )

    assert to_telegram_html(source) == (
        '<a href="https://example.invalid/1">см. <b>Первый</b></a>. '
        '<a href="https://example.invalid/2">см. <b>Второй</b></a>. '
        '<a href="https://example.invalid/3">см. <b>Третий</b></a>.'
    )


def test_mixed_inline_markup_inside_link_labels_survives() -> None:
    source = (
        "[см. *раз* и **Первый**](https://example.invalid/1). "
        "[см. *два* и **Второй**](https://example.invalid/2). "
        "[см. *три* и **Третий**](https://example.invalid/3)."
    )

    rendered = to_telegram_html(source)

    assert rendered.count("<a href=") == 3
    assert rendered.count("<i>") == 3
    assert rendered.count("<b>") == 3
    assert "](https://" not in rendered
    assert "\n" not in rendered


def test_malformed_links_stay_literal_without_section_reflow() -> None:
    source = (
        "[см. **Первый**](https://example.invalid/1 "
        "[см. **Второй**](https://example.invalid/2 "
        "[см. **Третий**](https://example.invalid/3"
    )

    assert to_telegram_html(source) == (
        "[см. <b>Первый</b>](https://example.invalid/1 "
        "[см. <b>Второй</b>](https://example.invalid/2 "
        "[см. <b>Третий</b>](https://example.invalid/3"
    )


def test_flattened_unpunctuated_bullets_after_bold_label_become_list() -> None:
    source = "**Особенности:** *   Первый пункт *   Второй пункт *   Третий пункт"

    assert to_telegram_html(source) == ("<b>Особенности:</b>\n• Первый пункт\n• Второй пункт\n• Третий пункт")


def test_live_shaped_flattened_review_restores_fields_sections_and_bullets() -> None:
    source = (
        "**Документ:** synthetic.pdf **Дата:** `17.08.2026` **Класс:** внутренний. "
        "Ниже краткое ревью. ### 1. Назначение и структура Описание раздела: "
        "*   Первый пункт. *   Второй пункт. *   Третий пункт. "
        "### 2. Ключевые данные *   **Роль:** инженер. *   **Код:** SYN-42. "
        "### 3. Вывод *   **Итог:** проверка завершена. *   **Риск:** отсутствует."
    )

    rendered = to_telegram_html(source)

    assert "###" not in rendered
    assert "*   " not in rendered
    assert rendered.startswith(
        "<b>Документ:</b> synthetic.pdf\n<b>Дата:</b> <code>17.08.2026</code>\n"
        "<b>Класс:</b> внутренний. Ниже краткое ревью."
    )
    assert "\n\n<b>1.</b> Назначение и структура Описание раздела:\n• Первый пункт." in rendered
    assert "\n\n<b>2.</b> Ключевые данные\n• <b>Роль:</b> инженер." in rendered
    assert "\n\n<b>3.</b> Вывод\n• <b>Итог:</b> проверка завершена." in rendered


def test_two_inline_heading_examples_are_not_reflowed_as_a_document() -> None:
    source = "Синтаксис: ### 1. Первый пример и ### 2. Второй пример."

    assert to_telegram_html(source) == source


@pytest.mark.parametrize(
    ("source", "rendered"),
    (
        ("**Итог:** 5 *   3 *   2", "<b>Итог:</b> 5 *   3 *   2"),
        (
            "Обычная фраза *   один *   два *   три без списка",
            "Обычная фраза *   один *   два *   три без списка",
        ),
        (
            "Значения: *   один *   два *   три",
            "Значения: *   один *   два *   три",
        ),
        (
            "**Особенности:** *   Первый пункт *   Второй пункт",
            "<b>Особенности:</b> *   Первый пункт *   Второй пункт",
        ),
    ),
)
def test_unlabelled_or_insufficient_shapes_are_not_rewritten_as_lists(
    source: str,
    rendered: str,
) -> None:
    assert to_telegram_html(source) == rendered
