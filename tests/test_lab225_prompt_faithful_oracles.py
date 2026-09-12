"""LAB-225 prompt-faithful A09-04/06 oracle corrections.

Private harness only. Does not import product, does not contact a model,
and does not treat the two observed live160 strings as the only positives.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tools import synthetic_live_battery as battery

A09_04 = "Дай одно предложение о пользе изолированного тестового окружения. Контроль SYN-A09-04."
A09_06 = "Объясни простыми словами, зачем проверять отказоустойчивость. Контроль SYN-A09-06."

ACTUAL160_A09_04 = (
    "Изолированное тестовое окружение позволяет проверять изменения без "
    "риска повредить рабочие данные или нарушить стабильность основной системы."
)
ACTUAL160_A09_06 = (
    "Отказоустойчивость проверяют, чтобы убедиться, что система не «умрёт» "
    "целиком из-за одной ошибки или сбоя, а продолжит работать или корректно "
    "восстановится.\n"
    "\n"
    "Простыми словами: это как страховка для вашего приложения. Вы намеренно "
    "ломаете отдельные части (сервер, базу данных, сеть) и смотрите, что будет. "
    "Если всё падает — вы знаете, где слабое место, и чините его до того, как "
    "это случится в бою."
)


def _case(question_index: int, question: str) -> battery.ExpandedCase:
    return battery.ExpandedCase(
        id=f"SYN-A09-{question_index:02d}",
        battery_id="A",
        pass_id="A-P09",
        pass_index=9,
        question_index=question_index,
        block="tools_and_fallback",
        oracle_profile="tools_and_fallback",
        question=question,
    )


def _record(message: str) -> dict:
    response = {
        "conversation_id": "conversation-lab225",
        "message": message,
        "message_id": "message-lab225",
        "tools_used": [],
    }
    return {
        "status_code": 200,
        "response": response,
        "raw_response": json.dumps(response, ensure_ascii=False),
        "state": {},
        "privacy_canaries": ["SYN-PRIVATE-DO-NOT-LEAK"],
    }


def _codes(question_index: int, question: str, message: str) -> list[str]:
    return battery.evaluate_case(_case(question_index, question), _record(message), latency_ms=1)[
        "failure_codes"
    ]


def _semantic_missing(question_index: int, question: str, message: str) -> bool:
    return "content_semantic_group_missing" in _codes(question_index, question, message)


def test_actual160_answers_keep_exact_text_and_pass() -> None:
    assert hashlib.sha256(ACTUAL160_A09_04.encode()).hexdigest() == (
        "c15df57ea6ad570c45ef81d3ff776fc84136311cdbb34be5f448e2f135927d68"
    )
    assert hashlib.sha256(ACTUAL160_A09_06.encode()).hexdigest() == (
        "5f1b6a63805bae0a0e5d652a0ced731e93abb81a2292b4ea2d89395ab6940de0"
    )
    assert battery._a09_04_relation_is_exact(ACTUAL160_A09_04) is True
    assert battery._a09_06_relation_is_exact(ACTUAL160_A09_06) is True
    assert _semantic_missing(4, A09_04, ACTUAL160_A09_04) is False
    assert _semantic_missing(6, A09_06, ACTUAL160_A09_06) is False


@pytest.mark.parametrize(
    "message",
    [
        ACTUAL160_A09_04,
        "Изолированное тестовое окружение защищает рабочие данные от повреждений.",
        "Изолированная среда позволяет проверять код, не нарушая основную систему.",
        "Изолированное тестовое окружение позволяет проверять изменения без риска повредить рабочие данные.",
        "Изолированная тестовая среда позволяет безопасно проверять изменения, не затрагивая рабочую систему.",
    ],
)
def test_a09_04_accepts_one_isolation_benefit_not_only_reproducibility(message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is True
    assert _semantic_missing(4, A09_04, message) is False


@pytest.mark.parametrize(
    "message",
    [
        "Изолированное окружение удобно.",
        "Изолированное окружение позволяет проверять изменения без риска.",
        "Изолированное окружение позволяет проверять изменения без риска повредить отчёт.",
        "Неизолированное окружение позволяет проверять изменения без риска повредить рабочие данные.",
        "Изолированное окружение может позволять проверять изменения без риска повредить рабочие данные.",
        "Изолированное окружение позволяет проверять изменения без риска повредить рабочие данные, но затем изменяет рабочие данные.",
        "Изолированное окружение не даёт предсказуемости результатов.",
        "Неизолированное окружение даёт стабильные результаты без влияния внешнего мира.",
    ],
)
def test_a09_04_rejects_vague_wrong_object_hedge_and_harm(message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is False
    assert _semantic_missing(4, A09_04, message) is True


@pytest.mark.parametrize(
    "message",
    [
        ACTUAL160_A09_06,
        "Отказоустойчивость проверяют, чтобы убедиться, что система не умрёт целиком из-за одной ошибки или сбоя, а продолжит работать или корректно восстановится.",
        "Отказоустойчивость проверяют, чтобы система не погибла из-за одной ошибки и продолжила работать.",
        "Проверять отказоустойчивость нужно, чтобы при сбое система корректно восстановилась.",
        "Отказоустойчивость проверяют, чтобы система продолжила работать, когда что-то сломается.",
    ],
)
def test_a09_06_accepts_why_fault_tolerance_with_causal_benefit(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is True
    assert _semantic_missing(6, A09_06, message) is False


@pytest.mark.parametrize(
    "message",
    [
        "Отказоустойчивость проверяют, чтобы система сломалась, когда всё хорошо.",
        "Проверка отказоустойчивости может быть нужна, чтобы система продолжила работать, когда что-то сломается.",
        "Отказоустойчивость проверять не нужно: сбои можно игнорировать.",
        "Отказоустойчивость проверяют из-за одной ошибки.",
        "Отказоустойчивость проверяют, чтобы система умерла из-за сбоя.",
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если часть "
            "системы сломается (пользователь, отчёт), остальная часть продолжит "
            "работать, а пользователи не потеряют данные и не столкнутся с "
            "полным крахом сервиса."
        ),
    ],
)
def test_a09_06_rejects_opposite_hedge_ignore_and_fault_without_benefit(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is False
    assert _semantic_missing(6, A09_06, message) is True
