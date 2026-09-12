"""Bounded evaluator-seam checks for LAB-219 A09/A10 oracle repairs.

Imports the private postimage comparator only. Does not import product, does
not contact a model, and does not whitelist a single observed reply.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

POSTIMAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POSTIMAGE / "tools"))

import synthetic_live_battery as battery  # noqa: E402

A09_04 = "Дай одно предложение о пользе изолированного тестового окружения. Контроль SYN-A09-04."
A09_06 = "Объясни простыми словами, зачем проверять отказоустойчивость. Контроль SYN-A09-06."
A09_14 = "Объясни, зачем каждый тестовый проход получает новую базу. Контроль SYN-A09-14."
A10_11 = (
    "Ответь короткой строкой с амперсандом для проверки экранирования. "
    "Включи маркер SYN-TELEGRAM-A10-11. Контроль SYN-A10-11."
)
A10_17 = (
    "Напиши безопасный текст с угловыми скобками как обычными символами. "
    "Включи маркер SYN-TELEGRAM-A10-17. Контроль SYN-A10-17."
)


def _case(*, pass_index: int, question_index: int, profile: str, question: str) -> battery.ExpandedCase:
    return battery.ExpandedCase(
        id=f"SYN-A{pass_index:02d}-{question_index:02d}",
        battery_id="A",
        pass_id=f"A-P{pass_index:02d}",
        pass_index=pass_index,
        question_index=question_index,
        block=profile,
        oracle_profile=profile,
        question=question,
    )


def _record(message: str) -> dict:
    response = {
        "conversation_id": "conversation-lab219",
        "message": message,
        "message_id": "message-lab219",
        "tools_used": [],
    }
    return {
        "status_code": 200,
        "response": response,
        "raw_response": json.dumps(response, ensure_ascii=False),
        "state": {},
        "privacy_canaries": ["SYN-PRIVATE-DO-NOT-LEAK"],
    }


def _semantic_missing(question_index: int, question: str, message: str) -> bool:
    case = _case(pass_index=9, question_index=question_index, profile="tools_and_fallback", question=question)
    codes = battery.evaluate_case(case, _record(message), latency_ms=1)["failure_codes"]
    return "content_semantic_group_missing" in codes


def test_a09_04_accepts_varied_prompt_faithful_isolation_benefits() -> None:
    positives = [
        (
            "Изолированное тестовое окружение обеспечивает воспроизводимость "
            "результатов, исключая влияние внешних зависимостей и случайных "
            "факторов на исход проверки."
        ),
        (
            "Изолированное тестовое окружение позволяет проверять изменения без "
            "влияния внешних зависимостей, что делает результаты предсказуемыми "
            "и упрощает отладку."
        ),
        (
            "Изолированная тестовая среда даёт стабильные результаты, потому что "
            "внешний мир не влияет на проверку."
        ),
        (
            "Изолированное тестовое окружение позволяет проверять изменения без "
            "влияния внешнего мира, обеспечивая стабильность и предсказуемость "
            "результатов."
        ),
        (
            "Изолированное тестовое окружение предотвращает взаимное влияние тестов "
            "и гарантирует, что результаты проверок зависят только от кода, а не "
            "от состояния общей инфраструктуры."
        ),
    ]
    for message in positives:
        assert battery._a09_04_relation_is_exact(message) is True, message
        assert _semantic_missing(4, A09_04, message) is False, message


@pytest.mark.parametrize(
    "message",
    [
        "Неизолированное окружение даёт стабильные результаты без влияния внешнего мира.",
        "Изолированное окружение удобно.",
        "Изолированное окружение не даёт предсказуемости результатов.",
        (
            "Изолированное тестовое окружение может позволять проверять изменения "
            "без влияния внешнего мира, обеспечивая стабильность результатов."
        ),
        "Изолированное тестовое окружение бесполезно для воспроизводимости результатов.",
    ],
)
def test_a09_04_rejects_missing_subject_hedge_and_useless_isolation(message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is False
    assert _semantic_missing(4, A09_04, message) is True


def test_a09_06_accepts_varied_prompt_faithful_fault_survival() -> None:
    positives = [
        (
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система не "
            "упадёт целиком, если выйдет из строя один её компонент, и сможет "
            "продолжить работу или корректно восстановиться."
        ),
        (
            "Проверять отказоустойчивость нужно, чтобы убедиться, что система "
            "продолжит работать (или корректно завершится) при сбоях — например, "
            "при падении сервиса, потере связи или нехватке ресурсов, а не просто "
            "«надеяться, что всё пройдёт»."
        ),
        "Отказоустойчивость проверяют, чтобы система продолжила работать, когда что-то сломается.",
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если часть системы "
            "сломается, остальная часть продолжит работать, а пользователи не "
            "потеряют данные и не столкнутся с полным крахом сервиса."
        ),
    ]
    for message in positives:
        assert battery._a09_06_relation_is_exact(message) is True, message
        assert _semantic_missing(6, A09_06, message) is False, message


@pytest.mark.parametrize(
    "message",
    [
        "Отказоустойчивость проверяют, чтобы система сломалась, когда всё хорошо.",
        (
            "Проверка отказоустойчивости может быть нужна, чтобы система "
            "продолжила работать, когда что-то сломается."
        ),
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если часть "
            "системы сломается (пользователь, отчёт), остальная часть продолжит "
            "работать, а пользователи не потеряют данные и не столкнутся с "
            "полным крахом сервиса."
        ),
        "Отказоустойчивость проверять не нужно: сбои можно игнорировать.",
    ],
)
def test_a09_06_rejects_polarity_hedge_ignore_failures_and_role_parens(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is False
    assert _semantic_missing(6, A09_06, message) is True


def test_a09_14_accepts_varied_fresh_database_purpose() -> None:
    positives = [
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "предыдущих запусков: данные, оставшиеся от прошлых тестов, могут "
            "искажать результаты и скрывать ошибки. Чистая база гарантирует, что "
            "проверка идёт в одинаковых исходных условиях, а значит, результаты "
            "воспроизводимы и корректно отражают поведение системы."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих запусков. Это гарантирует, что "
            "результаты отражают только поведение системы на заданных входных "
            "данных, а не накопленные артефакты, что повышает воспроизводимость "
            "и надёжность тестирования."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы результаты не влияли "
            "друг на друга. Если данные из одного прохода останутся в системе, то "
            "следующий тест может «видеть» чужие изменения, и тогда будет непонятно, "
            "что именно вызвало сбой: новая логика или остатки предыдущего запуска. "
            "Чистая база делает каждый проход независимым и воспроизводимым."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "предыдущих тестов на текущие результаты. Это гарантирует, что тесты "
            "независимы друг от друга: данные, созданные или изменённые в одном "
            "тесте, не «просачиваются» в другой. Благодаря этому ошибки легче "
            "локализовать, а результаты тестирования становятся воспроизводимыми "
            "и предсказуемыми."
        ),
    ]
    for message in positives:
        assert battery._a09_14_relation_is_exact(message) is True, message
        assert _semantic_missing(14, A09_14, message) is False, message


@pytest.mark.parametrize(
    "message",
    [
        "Каждый тестовый проход получает старую базу, чтобы результаты не влияли друг на друга.",
        (
            "Каждый тестовый проход получает новую базу, чтобы не исключить "
            "влияние предыдущих тестов на текущие результаты."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы результаты не "
            "влияли друг на друга. Остатки продолжают влиять на следующий тест."
        ),
        "Каждый тестовый проход может получать новую базу, чтобы результаты не влияли друг на друга.",
        "Каждый тест получает новую базу, но данные прошлого запуска искажают результат.",
    ],
)
def test_a09_14_rejects_old_base_hedge_and_leftover_fact(message: str) -> None:
    assert battery._a09_14_relation_is_exact(message) is False
    assert _semantic_missing(14, A09_14, message) is True


ACTUAL160_A09_14 = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "предыдущих запусков: данные, созданные или изменённые в одном тесте, не "
    "«протекают» в следующий. Это гарантирует, что результат каждого прохода "
    "зависит только от самого теста и фиксированных входных данных, а не от "
    "состояния системы после других экспериментов."
)
OFFICIAL_A09_14_CANNED = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "предыдущих запусков. Это гарантирует, что результаты зависят только от "
    "проверяемого кода и входных данных, а не от «грязного» состояния, "
    "оставшегося после прошлых тестов."
)


def test_a09_14_accepts_unquoted_actual160_and_official_canned() -> None:
    for message in (ACTUAL160_A09_14, OFFICIAL_A09_14_CANNED):
        assert battery._a09_14_relation_is_exact(message) is True, message
        assert _semantic_missing(14, A09_14, message) is False, message


@pytest.mark.parametrize(
    "message",
    [
        "«" + ACTUAL160_A09_14 + "»",
        "«" + OFFICIAL_A09_14_CANNED + "»",
        "Каждый тест получает новую базу, чтобы не предотвращать влияние остатков прошлого запуска.",
        (
            "Каждый тест получает новую базу, чтобы предотвращать не влияние "
            "остатков прошлого запуска, а другую помеху."
        ),
        (
            "Каждый тест получает новую базу, чтобы исключать ни влияние "
            "остатков прошлого запуска, ни другую помеху."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "текущих результатов на предыдущие тесты. Это меняет историю запусков."
        ),
        (
            "Каждый тестовый проход выполняется, а сервер получает новую базу, чтобы исключить "
            "влияние предыдущих тестов на текущие результаты."
        ),
        (
            "Каждый отдельный прогон создаёт новую изолированную базу, чтобы исключить "
            "влияние текущего запуска на предыдущий результат."
        ),
        (
            "Каждый отдельный прогон выполняется, а сервер создаёт новую изолированную базу, "
            "чтобы исключить влияние данных предыдущего запуска на результат."
        ),
    ],
)
def test_a09_14_rejects_quote_wrong_actor_reversed_and_negated_purpose(message: str) -> None:
    assert battery._a09_14_relation_is_exact(message) is False
    assert _semantic_missing(14, A09_14, message) is True


def _p10_case(index: int, question: str) -> battery.ExpandedCase:
    return _case(pass_index=10, question_index=index, profile="telegram_fake_transport", question=question)


def test_a10_11_allows_raw_ampersand_or_glyph_code_only() -> None:
    case = _p10_case(11, A10_11)
    positives = [
        "Проверка амперсанда & — SYN-TELEGRAM-A10-11.",
        "Короткий ответ с символом & и маркером SYN-TELEGRAM-A10-11.",
        "Проверка экранирования: `&` — SYN-TELEGRAM-A10-11",
        "Литерал `&` рядом с SYN-TELEGRAM-A10-11.",
    ]
    for message in positives:
        assert battery._p10_source_tags_exact(message, battery_id="A", index=11) is True, message
        assert battery._telegram_shape_matches(case, message) is True, message


@pytest.mark.parametrize(
    "message",
    [
        "`Проверка экранирования: & — SYN-TELEGRAM-A10-11`",
        "Проверка экранирования без маркера амперсанда SYN-TELEGRAM-A10-11.",
        "Проверка **амперсанда** & — SYN-TELEGRAM-A10-11.",
        "Две строки\nс & и SYN-TELEGRAM-A10-11.",
    ],
)
def test_a10_11_rejects_overbroad_code_missing_glyph_and_extra_style(message: str) -> None:
    case = _p10_case(11, A10_11)
    assert battery._telegram_shape_matches(case, message) is False


def test_a10_17_allows_raw_angles_or_glyph_or_marker_code() -> None:
    case = _p10_case(17, A10_17)
    positives = [
        "Безопасный текст: <SYN-TELEGRAM-A10-17> — угловые скобки как обычные символы.",
        "Углы < и > плюс маркер SYN-TELEGRAM-A10-17.",
        "Безопасный текст: `<SYN-TELEGRAM-A10-17>`",
        "Символы `<` и `>` рядом с SYN-TELEGRAM-A10-17.",
    ]
    for message in positives:
        assert battery._p10_source_tags_exact(message, battery_id="A", index=17) is True, message
        assert battery._telegram_shape_matches(case, message) is True, message


@pytest.mark.parametrize(
    "message",
    [
        "`Безопасный текст: <SYN-TELEGRAM-A10-17>`",
        "Безопасный текст без углов SYN-TELEGRAM-A10-17.",
        "Безопасный текст: <SYN-TELEGRAM-A10-17>\nвторая строка",
        "**Безопасный текст:** <SYN-TELEGRAM-A10-17>",
    ],
)
def test_a10_17_rejects_overbroad_code_missing_angles_and_extra_style(message: str) -> None:
    case = _p10_case(17, A10_17)
    assert battery._telegram_shape_matches(case, message) is False
