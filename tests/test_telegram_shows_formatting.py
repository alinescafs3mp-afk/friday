"""Разметка модели должна выглядеть разметкой, а не звёздочками.

Заказ владельца 2026-08-02: «добавь markdown разметку в телеграмме, чтобы код
вылазил как код, чтобы форматирование текста модели смотрелось как задумано,
например то, **что они выделяют текст вот так вот**, в данный момент он прямо так
и приходит со звёздочками».

`sendMessage` уходил без `parse_mode` вовсе — Telegram и не мог ничего разобрать.

Второе требование теста жёстче первого: разметка НЕ ДОЛЖНА стоить сообщения.
Именно «ничего не приходит» владелец разбирал сегодня утром, и получить это ещё
раз из-за незакрытого тега было бы худшим исходом, чем звёздочки.
"""

from __future__ import annotations

import pytest

from friday.telegram_bridge._markup import to_telegram_html


def test_bold_becomes_bold() -> None:
    assert to_telegram_html("**срок поверки** — 4 года") == "<b>срок поверки</b> — 4 года"


def test_code_stays_code_and_is_escaped() -> None:
    """Внутри кода разметки нет, а `<` и `&` обязаны быть экранированы."""
    result = to_telegram_html("```\nif a < b & c:\n    print(**x**)\n```")
    assert result == "<pre><code>if a &lt; b &amp; c:\n    print(**x**)</code></pre>"


def test_inline_code_survives_next_to_prose() -> None:
    assert to_telegram_html("смотри `fgis.gost.ru` там") == "смотри <code>fgis.gost.ru</code> там"


def test_a_link_keeps_its_address() -> None:
    result = to_telegram_html("источник: [ЦБ РФ](https://www.cbr.ru/)")
    assert result == 'источник: <a href="https://www.cbr.ru/">ЦБ РФ</a>'


def test_a_heading_becomes_a_bold_line_without_eating_the_blank_line() -> None:
    """Мутация: вернуть `\\s*$` в хвосте заголовка — тест краснеет.

    `\\s` включает перевод строки, и жадный хвост съедал пустую строку под
    заголовком: абзац слипался с ним.
    """
    assert to_telegram_html("## Справка\n\nтекст") == "<b>Справка</b>\n\nтекст"


@pytest.mark.parametrize(
    "plain",
    [
        "цена 5 * 3 рубля",  # умножение — не курсив
        "файл my_file_name.doc",  # подчёркивания в имени — не курсив
        "2 * 2 = 4 и 3 _ 4",
    ],
)
def test_ordinary_text_is_left_alone(plain: str) -> None:
    """Разметка не должна возникать там, где человек её не ставил."""
    assert "<i>" not in to_telegram_html(plain)
    assert "<b>" not in to_telegram_html(plain)


def test_html_special_characters_are_escaped() -> None:
    """Иначе Telegram отвергнет сообщение целиком, а человек не получит ничего."""
    assert to_telegram_html("если 2 < 3 и A & B") == "если 2 &lt; 3 и A &amp; B"


def test_bullets_become_bullets() -> None:
    assert to_telegram_html("- первый\n- второй") == "• первый\n• второй"


def test_empty_input_stays_empty() -> None:
    assert to_telegram_html("   ") == ""


def test_the_sender_falls_back_to_plain_text_on_a_rejection() -> None:
    """Мутация: убрать ветку на 400 — тест краснеет.

    Разметка важна, доставка важнее: любая неожиданная последовательность не
    должна стоить человеку сообщения.
    """
    import inspect

    from friday.telegram_bridge._transport import TransportMixin

    source = inspect.getsource(TransportMixin._send_message)
    assert '"parse_mode": "HTML"' in source, "разметка снова не включена"
    assert "status_code == 400" in source, "нет запасного пути — отказ разбора съест сообщение"
    assert 'payload.pop("parse_mode"' in source, "повтор идёт с той же разметкой, что и отказала"


def test_the_split_happens_before_the_markup() -> None:
    """Резать надо сырой текст: граница внутри тега — отвергнутый кусок."""
    import inspect

    from friday.telegram_bridge._transport import TransportMixin

    source = inspect.getsource(TransportMixin._send_message)
    assert source.index("split_for_telegram(text)") < source.index("to_telegram_html(chunk)")
