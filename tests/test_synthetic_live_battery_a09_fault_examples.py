"""Keep fault examples separate from the owned fault-survival assertion."""

import hashlib

import pytest
from test_synthetic_live_battery_a09_task_faithful import _eval, battery

ACTUAL_D893_A09_06 = (
    "Отказоустойчивость проверяют, чтобы убедиться, что система не «упадёт» "
    "или не потеряет данные, когда что-то пойдёт не так (например, отвалит "
    "сервер, закончится память или прервётся сеть). Простыми словами: это "
    "как страховка, которая гарантирует, что при сбое система либо продолжит "
    "работать, либо корректно восстановится, а не сломается насовсем."
)

ACTUAL_D80C_A09_06 = (
    "Проверять отказоустойчивость нужно, чтобы убедиться, что система не сломается "
    "и продолжит работать (или корректно завершится), когда что-то пойдёт не так — "
    "например, упадёт сервис, закончится память или пропадёт связь. Простыми словами: "
    "это как проверить, есть ли в доме запасной выход, чтобы в случае пожара можно было "
    "выбраться, а не просто надеяться, что пожар не случится."
)


def _assert_relation(message: str, expected: bool) -> None:
    assert battery._a09_06_relation_is_exact(message) is expected
    failures = _eval(5, message)
    assert ("content_semantic_group_missing" not in failures) is expected
    if expected:
        assert "content_required_alternative_missing" not in failures


def test_actual_d893_full160_answer_contains_fault_survival_relation() -> None:
    assert hashlib.sha256(ACTUAL_D893_A09_06.encode()).hexdigest() == (
        "7ce40b0d20a0d328a9489640e47688558545c86157a16730a03236de9e69b4a3"
    )
    _assert_relation(ACTUAL_D893_A09_06, True)


def test_actual_d80c_full160_answer_contains_fault_survival_relation() -> None:
    assert hashlib.sha256(ACTUAL_D80C_A09_06.encode()).hexdigest() == (
        "5a3371a7ec62c99ce6eb703de9f8054f9e6c04d2fe7942db45925471f3e68551"
    )
    _assert_relation(ACTUAL_D80C_A09_06, True)


@pytest.mark.parametrize(
    "illustration",
    [
        " — например, упадёт сервис, закончится память или пропадёт связь",
        " – например, прервётся связь, упадёт сервер или закончится диск",
        " - например упадет процесс, закончится место на диске и оборвется сеть",
        ": упадёт сервис, закончится память или пропадёт связь",
        ": например, придёт кривой ввод, отвалит хранилище или прервётся сеть",
        " (например, упадёт сервер, закончится память или пропадёт связь)",
        " — например, сервер, диск или связь",
        " — например, при падении сервиса, потере связи или нехватке ресурсов",
    ],
    ids=[
        "em-dash-finite-events",
        "en-dash-reordered-events",
        "hyphen-events-without-comma",
        "colon-finite-events",
        "colon-explicit-mixed-events",
        "parenthetical-finite-events",
        "dash-bare-resource-list",
        "dash-nominal-resource-events",
    ],
)
def test_explicit_fault_list_punctuation_and_resource_events_are_equivalent(
    illustration: str,
) -> None:
    message = (
        "Отказоустойчивость проверяют, чтобы система продолжила работать, "
        f"когда что-то пойдёт не так{illustration}."
    )
    _assert_relation(message, True)


@pytest.mark.parametrize(
    "message",
    [
        (
            "Отказоустойчивость проверяют, чтобы система продолжила работать, когда "
            "что-то пойдёт не так — например, не упадёт сервис или не пропадёт связь."
        ),
        (
            "Отказоустойчивость проверяют, чтобы система продолжила работать, когда "
            "что-то пойдёт не так — например, упадёт пользователь или пропадёт связь."
        ),
        (
            "Отказоустойчивость проверяют, чтобы система продолжила работать, когда "
            "что-то пойдёт не так — например, закончится отчёт или пропадёт связь."
        ),
        (
            "Отказоустойчивость проверяют, чтобы система продолжила работать, когда "
            "что-то пойдёт не так например, упадёт сервис или пропадёт связь."
        ),
        ACTUAL_D80C_A09_06.replace("система не сломается", "отчёт не сломается"),
        ACTUAL_D80C_A09_06.replace(
            "Проверять отказоустойчивость нужно", "Проверять отказоустойчивость не нужно"
        ),
        f"Цитата: «{ACTUAL_D80C_A09_06}»",
        "В отчёте написано: " + ACTUAL_D80C_A09_06,
        ACTUAL_D80C_A09_06 + " Однако система всё равно упадёт.",
        (
            "Простыми словами: это как запасной выход — например, упадёт сервис, "
            "закончится память или пропадёт связь."
        ),
        (
            "Отказоустойчивость проверяют, чтобы система продолжила работать, когда "
            "что-то пойдёт не так — например, упадёт сервис или загорится отчёт."
        ),
    ],
    ids=[
        "negated-events",
        "wrong-fault-actor",
        "unsupported-resource-failure",
        "missing-list-boundary",
        "wrong-whole-subject",
        "negated-purpose",
        "unowned-quoted-claim",
        "third-party-report",
        "later-owned-contradiction",
        "analogy-only",
        "unsupported-event",
    ],
)
def test_fault_list_forms_do_not_weaken_owned_meaning_guards(message: str) -> None:
    _assert_relation(message, False)


@pytest.mark.parametrize(
    "examples",
    [
        "отвалит сервер, закончится память или прервётся сеть",
        "прервётся сеть, отвалит сервер или закончится память",
        "закончится память или прервётся сеть",
        "оборвётся сеть и отвалит диск",
        "пропадёт сеть, упадёт сервер или закончится место на диске",
        "отвалит хранилище, закончится память, прервётся сеть или упадёт сервер",
        "отвалит сервер, диск или сеть",
    ],
)
def test_mixed_fault_examples_preserve_the_complete_assertion(examples: str) -> None:
    message = (
        "Отказоустойчивость проверяют, чтобы система продолжила работать, "
        f"когда что-то сломается (например, {examples})."
    )
    _assert_relation(message, True)


@pytest.mark.parametrize(
    "examples",
    [
        "отвалит пользователь, закончится память или прервётся сеть",
        "отвалит сервер, сохранится память или прервётся сеть",
        "отвалит сервер, закончится память или не прервётся сеть",
        "отвалит сервер, закончится память или прервётся отчёт",
        "отвалит сервер, диск, закончится память",
        "отвалит сервер, закончится память, прервётся сеть, упадёт сервер или закончится диск",
    ],
)
def test_examples_cannot_supply_unbound_actors_or_non_fault_events(examples: str) -> None:
    message = (
        "Отказоустойчивость проверяют, чтобы система продолжила работать, "
        f"когда что-то сломается (например, {examples})."
    )
    _assert_relation(message, False)


@pytest.mark.parametrize(
    "message",
    [
        ACTUAL_D893_A09_06.replace("система не «упадёт»", "система «упадёт»"),
        ACTUAL_D893_A09_06.replace("или не потеряет данные", "или потеряет данные"),
        ACTUAL_D893_A09_06.replace("Отказоустойчивость проверяют", "Отказоустойчивость не проверяют"),
        ACTUAL_D893_A09_06.replace("система не «упадёт»", "отчёт не «упадёт»"),
        ACTUAL_D893_A09_06 + " Она всё равно сломается.",
        f"Цитата: «{ACTUAL_D893_A09_06}»",
        "Отказоустойчивость проверяют. Например, отвалит сервер, закончится память или прервётся сеть.",
    ],
)
def test_fault_examples_do_not_replace_owned_affirmative_survival(message: str) -> None:
    _assert_relation(message, False)
