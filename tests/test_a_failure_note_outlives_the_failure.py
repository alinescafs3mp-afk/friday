"""Текст сбоя, который ложится в базу навсегда, — не то же, что показанный сейчас.

Немедленный ответ инструмента уже безопасен: «Tool failed: RuntimeError», без
подробностей. А в заявку (`action_approvals.error`) писался полный текст
исключения, и оттуда он отдаётся наружу через `GET /api/me/approvals` целиком.

Что туда попадает на практике: адрес с токеном в query-строке, заголовок
`Authorization`, абсолютный путь к файлу человека, кусок документа из сообщения
об ошибке разбора. Разбор Сола §16 воспроизвёл это синтетическим `credential=…`
и путём.

Сообщение нельзя безопасно «вычистить»: обычная русская фраза может быть именем
или фрагментом документа. Здесь проверяется более строгий контракт — остаётся
только allowlisted имя класса, без единого слова сообщения.
"""

from __future__ import annotations

from friday.failures import safe_failure_text


def test_an_address_with_a_token_does_not_survive():
    text = safe_failure_text(RuntimeError("GET https://api.example.com/v1?token=abc123 failed"))

    assert text == "RuntimeError"


def test_an_authorization_header_does_not_survive():
    text = safe_failure_text(RuntimeError("authorization: Bearer sk-secret-value-here"))

    assert text == "RuntimeError"


def test_a_path_to_the_persons_files_does_not_survive():
    text = safe_failure_text(ValueError("cannot open /home/jericho/.jericho/data/state/db.sqlite3"))

    assert text == "ValueError"


def test_a_plain_explanation_does_not_survive():
    """Естественный язык как раз нельзя отличить от личного содержимого."""

    text = safe_failure_text(RuntimeError("в документе «Рапорт Иванова» строка 12 не разобрана"))

    assert text == "RuntimeError"


def test_a_long_text_is_not_retained_at_all():

    text = safe_failure_text(RuntimeError("подробности " * 100))

    assert text == "RuntimeError"


def test_a_bare_class_name_is_enough():
    assert safe_failure_text(TimeoutError()) == "TimeoutError"


def test_a_string_or_custom_class_must_match_the_class_allowlist():
    PrivateMedicalSentinel = type("PrivateMedicalSentinel", (RuntimeError,), {})

    assert safe_failure_text(PrivateMedicalSentinel("secret")) == "Error"
    assert safe_failure_text("RuntimeError: secret") == "RuntimeError"
    assert safe_failure_text("PRIVATE_MEDICAL_SENTINEL") == "Error"
