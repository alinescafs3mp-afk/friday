"""LAB-339 polarity controls for the LAB-338 B09-14 avoidance alternative.

The new избеж.{0,32}влия stem must be a live, unquoted, unnegated, unhedged
benefit of isolated/different databases.  Original group-3 alts (чист / не влия /
состояни), A09-04, B09-20, B01, B10-16 and TIME stay as LAB-338 left them.
"""

from __future__ import annotations

import hashlib

import pytest
from test_lab338_oracle092_repair import (
    A09_04_ACTUAL,
    B09_14_ACTUAL,
    B09_14_SHA,
    B09_20_ACTUAL,
    B10_16_ACTUAL,
    _eval,
    battery,
)

NEGATED_BENEFIT = "Разные базы не позволяют избежать взаимного влияния проходов."
IMPOSSIBLE_BENEFIT = "Разные базы не помогают избежать взаимного влияния проходов."
QUOTED_DENIED = "В отчёте написано: «Разные базы позволяют избежать взаимного влияния проходов». Это неверно."


def test_observed_lab338_false_positives_now_fail() -> None:
    assert battery._b09_14_avoided_influence_is_live(NEGATED_BENEFIT) is False
    assert battery._b09_14_avoided_influence_is_live(IMPOSSIBLE_BENEFIT) is False
    assert battery._b09_14_avoided_influence_is_live(QUOTED_DENIED) is False
    for message in (NEGATED_BENEFIT, IMPOSSIBLE_BENEFIT, QUOTED_DENIED):
        assert "content_semantic_group_missing" in _eval("B", 9, 14, message)


@pytest.mark.parametrize(
    "message",
    [
        "Разные базы не дают избежать взаимного влияния проходов.",
        "Разные базы не способны избежать взаимного влияния проходов.",
        "Разные базы неспособны избежать взаимного влияния проходов.",
        "Разные базы не могут избежать взаимного влияния проходов.",
        "Разные базы не смогут избежать взаимного влияния проходов.",
        "Разные базы нельзя использовать, чтобы избежать взаимного влияния проходов.",
        "Отдельные базы не обеспечивают избежание взаимного влияния проходов.",
        "Разные базы не позволяют изолировать данные и избежать взаимного влияния.",
        "Изолированные базы не помогают избежать взаимного влияния результатов.",
        "Нельзя избежать взаимного влияния разными базами проходов.",
    ],
)
def test_b09_14_rejects_negated_avoidance_paraphrases(message: str) -> None:
    assert battery._b09_14_avoided_influence_is_live(message) is False
    assert "content_semantic_group_missing" in _eval("B", 9, 14, message)


@pytest.mark.parametrize(
    "message",
    [
        ("Сообщение «разные базы позволяют избежать взаимного влияния проходов» ложно."),
        ("Цитата: «Отдельные базы позволяют избежать взаимного влияния результатов»."),
        'В отчёте написано: "Разные базы позволяют избежать взаимного влияния".',
        ("Утверждение «изолированные базы позволяют избежать взаимного влияния проходов» неверно."),
    ],
)
def test_b09_14_rejects_quoted_or_denied_avoidance(message: str) -> None:
    assert battery._b09_14_avoided_influence_is_live(message) is False
    assert "content_semantic_group_missing" in _eval("B", 9, 14, message)


@pytest.mark.parametrize(
    "message",
    [
        "Разные базы, возможно, позволяют избежать взаимного влияния проходов.",
        "Разные базы могут позволять избежать взаимного влияния проходов.",
        "Отдельные базы, вероятно, позволяют избежать взаимного влияния результатов.",
        "Если использовать разные базы, можно избежать взаимного влияния проходов.",
        "Разные базы иногда позволяют избежать взаимного влияния проходов.",
    ],
)
def test_b09_14_rejects_hedged_avoidance_benefits(message: str) -> None:
    assert battery._b09_14_avoided_influence_is_live(message) is False
    assert "content_semantic_group_missing" in _eval("B", 9, 14, message)


def test_b09_14_retains_actual_avoided_mutual_influence() -> None:
    assert hashlib.sha256(B09_14_ACTUAL.encode()).hexdigest() == B09_14_SHA
    assert battery._b09_14_avoided_influence_is_live(B09_14_ACTUAL) is True
    assert "content_semantic_group_missing" not in _eval("B", 9, 14, B09_14_ACTUAL)


@pytest.mark.parametrize(
    "message",
    [
        "Разные базы позволяют избежать взаимного влияния проходов.",
        "Разные базы способны избежать взаимного влияния проходов.",
        "Отдельные изолированные базы позволяют избежать взаимного влияния результатов.",
        "Разные базы для проходов позволяют избежать взаимного влияния результатов.",
        ("Независимые базы позволяют изолировать данные и избежать взаимного влияния результатов."),
    ],
)
def test_b09_14_retains_independent_positive_avoidance(message: str) -> None:
    assert battery._b09_14_avoided_influence_is_live(message) is True
    assert "content_semantic_group_missing" not in _eval("B", 9, 14, message)


def test_b09_14_keeps_clean_state_and_non_influence_alts() -> None:
    clean = "Разные базы для разных проходов держат чистое состояние данных каждого сценария."
    no_influence = "Разные изолированные базы для проходов, чтобы тесты не влияли друг на друга."
    assert battery._b09_14_avoided_influence_is_live(clean) is False
    assert battery._b09_14_avoided_influence_is_live(no_influence) is False
    assert "content_semantic_group_missing" not in _eval("B", 9, 14, clean)
    assert "content_semantic_group_missing" not in _eval("B", 9, 14, no_influence)


def test_a09_04_and_b09_20_repairs_remain() -> None:
    assert battery._a09_04_relation_is_exact(A09_04_ACTUAL) is True
    assert "content_semantic_group_missing" not in _eval("A", 9, 4, A09_04_ACTUAL)
    assert battery._b09_20_relation_is_exact(B09_20_ACTUAL) is True
    assert "content_semantic_group_missing" not in _eval("B", 9, 20, B09_20_ACTUAL)


def test_b10_16_and_time_unchanged() -> None:
    assert "content_telegram_shape_invalid" in _eval("B", 10, 16, B10_16_ACTUAL)
    marker = "SYN-TIME-44469D7D8D699EBD055F"
    competitor = "SYN-TIME-3E9A49D82512E90B2062"
    assert battery._closed_marker_exact(marker, marker, kind="TIME") is True
    assert battery._closed_marker_exact(f"{marker} {marker}", marker, kind="TIME") is False
    assert battery._closed_marker_exact(f"{marker} {competitor}", marker, kind="TIME") is False
