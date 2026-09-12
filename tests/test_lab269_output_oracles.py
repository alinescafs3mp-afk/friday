"""Focused adversarial checks for LAB-269 output-oracle repairs.

B05: balanced Markdown inline-code URL lexer; wrong URL/label in code still fails.
B07: attachment identity may repeat without a prompt-owned exact-once constraint.
B09-20: bounded independent-structural-rule relation, not a live-answer whitelist.
These tests are authored, not executed, in the lab turn.
"""

from __future__ import annotations

from tools import synthetic_live_battery as battery

_B05_LABEL = "SYN-LINK-B05-02"
_B05_URL = "https://example.invalid/b/02"
_B05_ANCHOR = f"[{_B05_LABEL}]({_B05_URL})"
_ATTACHMENT = "SYN-ATTACHMENT-6D674204785F7EACF10B"
_FOREIGN_ATTACHMENT = "SYN-ATTACHMENT-5EBAA1B0771DEA06825A"


def test_b05_accepts_identical_label_and_url_in_single_and_double_backticks() -> None:
    single = f"{_B05_ANCHOR}\nТекст `{_B05_LABEL}` и адрес `{_B05_URL}`."
    double = f"{_B05_ANCHOR}\nАдрес ``{_B05_URL}`` и label ``{_B05_LABEL}``."
    assert battery._markdown_source_exact(single, label=_B05_LABEL, url=_B05_URL) is True
    assert battery._markdown_source_exact(double, label=_B05_LABEL, url=_B05_URL) is True


def test_b05_unrelated_loss_phrase_remains_acceptable() -> None:
    message = f"Label сохранён без потерь:\n\n{_B05_ANCHOR}"
    assert battery._markdown_source_exact(message, label=_B05_LABEL, url=_B05_URL) is True


def test_b05_rejects_wrong_url_or_label_inside_code() -> None:
    wrong_url = f"{_B05_ANCHOR}\nАдрес `https://evil.example/x`."
    wrong_label = f"{_B05_ANCHOR}\nТекст `SYN-LINK-B05-03`."
    assert battery._markdown_source_exact(wrong_url, label=_B05_LABEL, url=_B05_URL) is False
    assert battery._markdown_source_exact(wrong_label, label=_B05_LABEL, url=_B05_URL) is False


def test_b05_rejects_extra_or_code_only_or_broken_anchors() -> None:
    extra = f"{_B05_ANCHOR} {_B05_ANCHOR}"
    code_only = f"`{_B05_ANCHOR}`"
    broken = "[SYN-LINK-B05-02](https://example.invalid/b/02"
    encoded = f"{_B05_ANCHOR} <a href='https://example.invalid/b/02'>x</a>"
    assert battery._markdown_source_exact(extra, label=_B05_LABEL, url=_B05_URL) is False
    assert battery._markdown_source_exact(code_only, label=_B05_LABEL, url=_B05_URL) is False
    assert battery._markdown_source_exact(broken, label=_B05_LABEL, url=_B05_URL) is False
    assert battery._markdown_source_exact(encoded, label=_B05_LABEL, url=_B05_URL) is False


def test_b05_unbalanced_delimiter_and_href_backtick_do_not_normalize() -> None:
    trailing = f"{_B05_ANCHOR}\nАдрес {_B05_URL}`"
    href_tick = f"[{_B05_LABEL}]({_B05_URL}`)"
    assert battery._markdown_source_exact(trailing, label=_B05_LABEL, url=_B05_URL) is False
    assert battery._markdown_source_exact(href_tick, label=_B05_LABEL, url=_B05_URL) is False


def test_b05_link_only_and_forbid_bare_url_remain() -> None:
    with_prose = f"Ссылка: {_B05_ANCHOR}"
    with_bare = f"{_B05_ANCHOR}\nАдрес: {_B05_URL}"
    assert (
        battery._markdown_source_exact(with_prose, label=_B05_LABEL, url=_B05_URL, only=True)
        is False
    )
    assert battery._markdown_source_exact(_B05_ANCHOR, label=_B05_LABEL, url=_B05_URL, only=True) is True
    assert (
        battery._markdown_source_exact(
            with_bare, label=_B05_LABEL, url=_B05_URL, forbid_bare_url=True
        )
        is False
    )
    assert (
        battery._markdown_source_exact(_B05_ANCHOR, label=_B05_LABEL, url=_B05_URL, forbid_bare_url=True)
        is True
    )


def test_b05_anchor_local_negation_still_rejected() -> None:
    message = f"Не используй {_B05_ANCHOR}"
    assert battery._markdown_source_exact(message, label=_B05_LABEL, url=_B05_URL) is False


def test_b07_repeated_identical_attachment_is_accepted_without_exact_once() -> None:
    quoted = (
        f"Canary `{_ATTACHMENT}` и полная строка: Контрольный маркер: {_ATTACHMENT}."
    )
    assert (
        battery._closed_marker_exact(quoted, _ATTACHMENT, kind="ATTACHMENT", exact_once=False)
        is True
    )


def test_b07_rejects_wrong_truncated_concatenated_and_fake_attachment() -> None:
    wrong = f"{_ATTACHMENT} {_FOREIGN_ATTACHMENT}"
    truncated = f"{_ATTACHMENT} SYN-ATTACHMENT-6D674204785F7EACF10"
    glued = f"{_ATTACHMENT}XX"
    fake = f"X{_ATTACHMENT}Y"
    assert battery._closed_marker_exact(wrong, _ATTACHMENT, kind="ATTACHMENT", exact_once=False) is False
    assert (
        battery._closed_marker_exact(truncated, _ATTACHMENT, kind="ATTACHMENT", exact_once=False)
        is False
    )
    assert battery._closed_marker_exact(glued, _ATTACHMENT, kind="ATTACHMENT", exact_once=False) is False
    assert battery._closed_marker_exact(fake, _ATTACHMENT, kind="ATTACHMENT", exact_once=False) is False


def test_b07_rejects_only_negated_and_mixed_polarity_occurrences() -> None:
    only_negated = f"{_ATTACHMENT} отсутствует"
    mixed = f"{_ATTACHMENT} найден. {_ATTACHMENT} отсутствует"
    assert (
        battery._closed_marker_exact(only_negated, _ATTACHMENT, kind="ATTACHMENT", exact_once=False)
        is False
    )
    assert (
        battery._closed_marker_exact(mixed, _ATTACHMENT, kind="ATTACHMENT", exact_once=False) is False
    )


def test_b07_explicit_exact_once_prompt_rejects_duplicates() -> None:
    question = "Верни token только один раз из текущего вложения."
    assert battery._attachment_output_requires_exact_once(question) is True
    duplicated = f"{_ATTACHMENT} {_ATTACHMENT}"
    assert (
        battery._closed_marker_exact(duplicated, _ATTACHMENT, kind="ATTACHMENT", exact_once=True)
        is False
    )


def test_b07_current_attachment_prompts_do_not_own_output_cardinality() -> None:
    quote = "Обработай document payload в этом же API turn и процитируй его внутренний canary."
    unique_in_file = "Найди единственную строку-идентификатор внутри приложенного TXT."
    assert battery._attachment_output_requires_exact_once(quote) is False
    assert battery._attachment_output_requires_exact_once(unique_in_file) is False


def test_b07_time_reminder_telegram_exact_once_unchanged() -> None:
    time_marker = "SYN-TIME-aaaaaaaaaaaaaaaaaaaa"
    reminder = "SYN-REMINDER-B08-01"
    telegram = "SYN-TELEGRAM-B10-16"
    assert battery._closed_marker_exact(f"{time_marker} {time_marker}", time_marker, kind="TIME") is False
    assert battery._closed_marker_exact(f"{reminder} {reminder}", reminder, kind="REMINDER") is False
    assert battery._closed_marker_exact(f"{telegram} {telegram}", telegram, kind="TELEGRAM") is False
    assert (
        battery._closed_marker_exact(f"{telegram} передан без ошибок.", telegram, kind="TELEGRAM")
        is True
    )


def test_b09_20_accepts_justified_independent_structural_rule_paraphrase() -> None:
    message = (
        "Главное свойство хорошего структурного oracle — независимость от "
        "тестируемого кода: проверка сверяет структуру результата по заранее "
        "известным правилам."
    )
    assert battery._b09_20_relation_is_exact(message) is True


def test_b09_20_substantive_legacy_precision_property_accepted() -> None:
    # Former canned fragment "oracle точн помогает..." is not a property relation.
    message = (
        "Главное свойство структурного oracle — точность: он однозначно "
        "отличает корректную структуру результата от нарушенной."
    )
    assert battery._b09_20_relation_is_exact(message) is True


def test_b09_20_rejects_bare_independence_and_unrelated_stems() -> None:
    bare = "Главное свойство oracle — независимость."
    unrelated = "В отчёте упомянут oracle погоды и независимость транзакций без правил структуры."
    assert battery._b09_20_relation_is_exact(bare) is False
    assert battery._b09_20_relation_is_exact(unrelated) is False


def test_b09_20_rejects_unnecessary_copy_hedge_contradiction_and_advisory() -> None:
    unnecessary = (
        "Главное свойство структурного oracle — независимость от тестируемого "
        "кода не нужна; он проверяет структуру результата по заранее известным правилам."
    )
    copy_rec = (
        "Следует копировать логику системы в структурный oracle, держа "
        "независимость от тестируемого кода и проверяя структуру результата "
        "по заранее известным правилам."
    )
    hedge = (
        "Главное свойство хорошего структурного oracle может быть независимостью "
        "от тестируемого кода: он проверяет структуру результата по заранее "
        "известным правилам."
    )
    contradiction = (
        "Хороший структурный oracle независим от тестируемого кода, но копирует "
        "логику системы, проверяя структуру результата по заранее известным правилам."
    )
    advisory = (
        "Стоит сделать структурный oracle независимым от тестируемого кода и "
        "проверять структуру результата по заранее известным правилам."
    )
    assert battery._b09_20_relation_is_exact(unnecessary) is False
    assert battery._b09_20_relation_is_exact(copy_rec) is False
    assert battery._b09_20_relation_is_exact(hedge) is False
    assert battery._b09_20_relation_is_exact(contradiction) is False
    assert battery._b09_20_relation_is_exact(advisory) is False
