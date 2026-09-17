"""Meaning-level regressions for the A09-06 and A09-18 answer oracles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не «упадет» "
            "или не потеряет данные, когда что-то сломается (например, отвалит сервер, "
            "диск или сеть), и сможет продолжить работу или быстро восстановиться.",
            id="owned-purpose-with-typed-fault-example",
        ),
        pytest.param(
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не упадет "
            "или не потеряет данные, когда что-то сломается (например, отвалит сервер, "
            "диск или сеть), и сможет продолжить работу или быстро восстановиться.",
            id="dequoted-owned-purpose",
        ),
        pytest.param(
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не упадет "
            "или не потеряет данные, когда что-то сломается, и сможет продолжить работу "
            "или быстро восстановиться.",
            id="bounded-recovery-adverb",
        ),
        pytest.param(
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не упадет "
            "или не потеряет данные, когда что-то сломается (например, сервер, диск и сеть), "
            "и сможет продолжить работу или корректно восстановиться.",
            id="bare-component-examples",
        ),
    ],
)
def test_a09_06_accepts_owned_purpose_with_bounded_leftovers(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is True


@pytest.mark.parametrize(
    "message",
    [
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если часть системы "
            "сломается (пользователь, отчёт), остальная часть продолжит работать, "
            "а пользователи не потеряют данные."
        ),
        (
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не упадет "
            "или не потеряет данные (но данные будут потеряны), когда что-то сломается."
        ),
        (
            "Отказоустойчивость проверяют, чтобы убедиться, что система не упадет целиком, "
            "когда выйдет из строя один её компонент (сервер, диск, сеть). "
            "Однако система упадет целиком."
        ),
        (
            "Проверка отказоустойчивости может быть нужна, чтобы система продолжила "
            "работать, когда что-то сломается."
        ),
    ],
)
def test_a09_06_bounded_leftovers_do_not_weaken_actor_polarity_or_hedge_guards(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is False


@pytest.mark.parametrize(
    "subject",
    ["режим работы системы", "принцип работы системы"],
)
def test_a09_18_accepts_work_as_an_optional_subject_modifier(subject: str) -> None:
    message = (
        f"Fail-closed — это {subject}, при котором при сбое или неопределённости она "
        "переходит в безопасное состояние, блокируя доступ или выполнение операций "
        "до устранения проблемы."
    )

    assert battery._a09_18_relation_is_exact(message) is True


@pytest.mark.parametrize(
    "message",
    [
        (
            "Fail-closed — это поведение системы, при котором при сбое она переходит "
            "в безопасное состояние (оставаясь открытой)."
        ),
        (
            "Fail-closed — это режим работы внешней системы, при котором при сбое она "
            "переходит в безопасное состояние, блокируя доступ."
        ),
        (
            "Fail-closed — это режим работы системы, при котором при сбое она "
            "(внешняя система) переходит в безопасное состояние, блокируя доступ."
        ),
        (
            "Fail-closed — это принцип работы системы, при котором при ошибке не "
            "блокируются опасные операции и доступ."
        ),
    ],
)
def test_a09_18_optional_modifier_does_not_hide_open_actor_or_polarity_contradictions(
    message: str,
) -> None:
    assert battery._a09_18_relation_is_exact(message) is False


@pytest.mark.parametrize(
    ("aside", "expected"),
    [
        ("оставаясь открытой", False),
        ("остаётся открытой", False),
        ("остаются открытыми", False),
        ("оставаясь полностью открытой", False),
        ("оставаясь всё ещё открытой", False),
        ("оставаясь по-прежнему открытой", False),
        ("сохраняя доступ открытым", False),
        ("доступ остаётся открытым", False),
        ("сохраняя систему в небезопасном состоянии", False),
        ("оставаясь закрытой", True),
        ("оставаясь полностью закрытой", True),
        ("блокируя доступ", True),
        ("не остаётся открытой", True),
        ("не оставаясь открытой", True),
        ("сохраняя доступ закрытым", True),
    ],
)
def test_a09_18_open_state_relation_keeps_its_polarity(aside: str, expected: bool) -> None:
    message = (
        "Fail-closed — это режим работы системы, при котором при сбое она переходит "
        f"в безопасное состояние ({aside})."
    )
    assert battery._a09_18_relation_is_exact(message) is expected
