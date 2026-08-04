"""Текст сбоя, который ложится в базу навсегда, — не то же, что показанный сейчас.

Немедленный ответ инструмента уже безопасен: «Tool failed: RuntimeError», без
подробностей. А в заявку (`action_approvals.error`) писался полный текст
исключения, и оттуда он отдаётся наружу через `GET /api/me/approvals` целиком.

Что туда попадает на практике: адрес с токеном в query-строке, заголовок
`Authorization`, абсолютный путь к файлу человека, кусок документа из сообщения
об ошибке разбора. Разбор Сола §16 воспроизвёл это синтетическим `credential=…`
и путём.

Здесь проверяется, что остаётся ровно то, что объясняет сбой, и что вычищенное
названо меткой, а не вырезано молча: по записи должно быть видно, что там
что-то было.
"""

from __future__ import annotations

from friday.failures import safe_failure_text


def test_an_address_with_a_token_does_not_survive():
    text = safe_failure_text(RuntimeError("GET https://api.example.com/v1?token=abc123 failed"))

    assert "token=abc123" not in text
    assert "api.example.com" not in text
    assert "‹адрес›" in text
    assert text.startswith("RuntimeError:"), "класс сбоя обязан остаться — иначе запись бесполезна"


def test_an_authorization_header_does_not_survive():
    text = safe_failure_text(RuntimeError("authorization: Bearer sk-secret-value-here"))

    assert "sk-secret-value-here" not in text
    assert "‹секрет›" in text


def test_a_path_to_the_persons_files_does_not_survive():
    text = safe_failure_text(ValueError("cannot open /home/jericho/.jericho/data/state/db.sqlite3"))

    assert "jericho" not in text
    assert "‹путь›" in text


def test_a_plain_explanation_survives_intact():
    """Контроль: чистка не должна съедать объяснение, ради которого запись и ведётся."""

    text = safe_failure_text(RuntimeError("в документе «Рапорт Иванова» строка 12 не разобрана"))

    assert text == "RuntimeError: в документе «Рапорт Иванова» строка 12 не разобрана"


def test_a_long_text_is_cut_and_says_so():
    """Молча укороченный текст читается как полный."""

    text = safe_failure_text(RuntimeError("подробности " * 100))

    assert len(text) <= 320
    assert text.endswith("…")


def test_a_bare_class_name_is_enough():
    assert safe_failure_text(TimeoutError()) == "TimeoutError"
