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
