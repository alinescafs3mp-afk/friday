"""Task-faithful A09-04/06/14 oracle corrections.

Model-free. Does not whitelist observed strings as the only positives.
Old code-only / component+user-data / exclude-previous tests stay in
test_synthetic_live_battery.py as extra sufficient paths, not as the
asked-question minimum.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402
from test_synthetic_live_battery import _cases, _satisfying_record  # noqa: E402

FROZEN = {
    "SYN-A09-04": {
        "sha256": "12bf182e34d551cf92d525ca0b8b3ec53ba03cc81142becda1634f12248fecc8",
        "message": (
            "Изолированное тестовое окружение позволяет проверять изменения без "
            "влияния внешнего мира, обеспечивая стабильность и предсказуемость "
            "результатов."
        ),
        "predicate": battery._a09_04_relation_is_exact,
        "case_index": 3,
    },
    "SYN-A09-06": {
        "sha256": "f6a608ec8b18e527cbc42d79be6f7526aeb386db5dc400d94a569d3d662c251d",
        "message": (
            "Отказоустойчивость проверяют, чтобы убедиться, что система не сломается "
            "и продолжит работать (или корректно завершится), когда что-то пойдёт "
            "не так: упадёт сервис, закончится память, пропадёт сеть или придёт "
            "«кривой» ввод.\n"
            "\n"
            "Простыми словами: это как проверить, что парашют раскроется, ещё до "
            "того как прыгать. Без таких проверок вы узнаёте о слабых местах только "
            "в бою — когда уже упали, а не на тренировке."
        ),
        "predicate": battery._a09_06_relation_is_exact,
        "case_index": 5,
    },
    "SYN-A09-14": {
        "sha256": "51762ec412585f537db4f3faede5a16e7bf1dddaba195d0d006233392f7147a1",
        "message": (
            "Каждый тестовый проход получает новую базу, чтобы результаты не влияли "
            "друг на друга. Если данные из одного прохода останутся в системе, то "
            "следующий тест может «видеть» чужие изменения, и тогда будет непонятно, "
            "что именно вызвало сбой: новая логика или остатки предыдущего запуска. "
            "Чистая база делает каждый проход независимым и воспроизводимым."
        ),
        "predicate": battery._a09_14_relation_is_exact,
        "case_index": 13,
    },
}


def _eval(case_index: int, message: str) -> list[str]:
    case = _cases("A", 9)[case_index]
    record = _satisfying_record(case)
    record["response"]["message"] = message
    record["raw_response"] = json.dumps(record["response"], ensure_ascii=False)
    return battery.evaluate_case(case, record, latency_ms=1)["failure_codes"]


@pytest.mark.parametrize("case_id", list(FROZEN))
def test_frozen_answers_match_pinned_sha_and_pass_task_faithful_oracles(case_id: str) -> None:
    spec = FROZEN[case_id]
    assert hashlib.sha256(spec["message"].encode()).hexdigest() == spec["sha256"]
    assert spec["predicate"](spec["message"]) is True
    failures = _eval(spec["case_index"], spec["message"])
    assert "content_semantic_group_missing" not in failures
    assert "content_required_alternative_missing" not in failures


def test_a09_04_accepts_external_world_paraphrase() -> None:
    message = (
        "Изолированная среда даёт предсказуемые результаты, потому что внешний мир не влияет на проверку."
    )
    assert battery._a09_04_relation_is_exact(message) is True
    assert "content_semantic_group_missing" not in _eval(3, message)


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
    ],
)
def test_a09_04_rejects_nearby_negations_and_hedged_main_verb(message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is False
    assert "content_semantic_group_missing" in _eval(3, message)


def test_a09_06_accepts_continue_under_fault_without_component_or_user_data() -> None:
    message = "Отказоустойчивость проверяют, чтобы система продолжила работать, когда что-то сломается."
    assert battery._a09_06_relation_is_exact(message) is True
    assert "content_semantic_group_missing" not in _eval(5, message)
    assert "content_required_alternative_missing" not in _eval(5, message)


@pytest.mark.parametrize(
    "message",
    [
        ("Отказоустойчивость проверяют, чтобы система сломалась, когда всё хорошо."),
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
    ],
)
def test_a09_06_rejects_polarity_hedge_and_parenthetical_roles(message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is False
    assert "content_semantic_group_missing" in _eval(5, message)


def test_a09_14_accepts_owned_exclude_previous_wording() -> None:
    message = (
        "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
        "предыдущих тестов на текущие результаты. Это гарантирует, что тесты "
        "независимы друг от друга: данные, созданные или изменённые в одном "
        "тесте, не «просачиваются» в другой. Благодаря этому ошибки легче "
        "локализовать, а результаты тестирования становятся воспроизводимыми "
        "и предсказуемыми."
    )
    assert battery._a09_14_relation_is_exact(message) is True
    assert "content_semantic_group_missing" not in _eval(13, message)


@pytest.mark.parametrize(
    "message",
    [
        ("Каждый тестовый проход получает старую базу, чтобы результаты не влияли друг на друга."),
        (
            "Каждый тестовый проход получает новую базу, чтобы не исключить "
            "влияние предыдущих тестов на текущие результаты."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы результаты не "
            "влияли друг на друга. Остатки продолжают влиять на следующий тест."
        ),
        ("Каждый тестовый проход может получать новую базу, чтобы результаты не влияли друг на друга."),
        (
            "Каждый отдельный прогон может создавать новую изолированную базу, "
            "чтобы исключить влияние данных предыдущего запуска на результат."
        ),
    ],
)
def test_a09_14_rejects_old_base_negation_leftover_claim_and_main_verb_hedge(
    message: str,
) -> None:
    assert battery._a09_14_relation_is_exact(message) is False
    assert "content_semantic_group_missing" in _eval(13, message)


ASTRA109_A09_06 = [
    (
        "original_negative_flipped_positive",
        False,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если часть системы сломается или перестанет отвечать, всё остальное продолжит работать, а пользователи столкнутся с полным крахом сервиса.",
    ),
    (
        "responsive_component",
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать.",
    ),
    (
        "responsive_parenthetical",
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать (пользователь этого не заметит).",
    ),
    (
        "contradictory_service_collapse",
        False,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, но сервис полностью рухнет.",
    ),
    (
        "purpose_check_negated",
        False,
        "Проверка отказоустойчивости нужна не для того, чтобы убедиться: если компонент сломается, система продолжит работать.",
    ),
]


@pytest.mark.parametrize(
    "case_id,expected,message",
    ASTRA109_A09_06,
    ids=[row[0] for row in ASTRA109_A09_06],
)
def test_astra109_a09_06_root_five_cases(case_id: str, expected: bool, message: str) -> None:
    assert battery._a09_06_relation_is_exact(message) is expected, case_id
    failures = _eval(5, message)
    if expected:
        assert "content_semantic_group_missing" not in failures
        assert "content_required_alternative_missing" not in failures
    else:
        assert "content_semantic_group_missing" in failures


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, и пользователи не столкнутся с полным крахом.",
            True,
        ),
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать (отчёт об этом не нужен).",
            True,
        ),
        (
            "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, но сервис полностью парализуется.",
            False,
        ),
        (
            "Проверка отказоустойчивости нужна не для того, чтобы система продолжила работать, когда что-то сломается.",
            False,
        ),
    ],
)
def test_a09_06_paired_mutations_around_astra109(message: str, expected: bool) -> None:
    assert battery._a09_06_relation_is_exact(message) is expected
    failures = _eval(5, message)
    if expected:
        assert "content_semantic_group_missing" not in failures
    else:
        assert "content_semantic_group_missing" in failures


def test_a09_06_contains_any_is_fault_tolerance_not_component() -> None:
    case = _cases("A", 9)[5]
    content = battery.oracle_for_case(case)["content"]
    assert content["contains_any"] == ["отказоустойчив", "устойчивост"]
    assert "част" not in content["contains_any"]
    assert "компонент" not in content["contains_any"]


SOL047 = [
    (
        "04-positive-core",
        3,
        True,
        "Изолированное тестовое окружение делает результаты стабильнее, потому что внешний мир не влияет на тесты.",
    ),
    (
        "04-positive-work-db",
        3,
        True,
        "Изолированное тестовое окружение защищает рабочую базу данных от случайных изменений.",
    ),
    (
        "04-positive-work-system",
        3,
        True,
        "Изолированная тестовая среда позволяет безопасно проверять изменения, не затрагивая рабочую систему.",
    ),
    (
        "04-negative-not-without",
        3,
        False,
        "Изолированное тестовое окружение даёт стабильные результаты не без влияния внешнего мира.",
    ),
    (
        "04-negative-unrelated",
        3,
        False,
        "Изолированное окружение удобно. Стабильные результаты обещает реклама, а внешний мир не влияет на погоду.",
    ),
    (
        "06-positive-no-component",
        5,
        True,
        "Отказоустойчивость проверяют, чтобы система продолжила работать, когда что-то сломается.",
    ),
    (
        "06-positive-component",
        5,
        True,
        "Проверка отказоустойчивости нужна, чтобы при отказе компонента система продолжила работать.",
    ),
    (
        "06-positive-component-services",
        5,
        True,
        "Отказоустойчивость проверяют, чтобы при сбое одного компонента остальные сервисы продолжили работать.",
    ),
    (
        "06-positive-safe-stop",
        5,
        True,
        "Отказоустойчивость проверяют, чтобы при сбое система корректно завершилась.",
    ),
    (
        "06-negative-negated-purpose",
        5,
        False,
        "Отказоустойчивость проверяют, чтобы система не продолжила работать, когда что-то сломается.",
    ),
    (
        "06-negative-negated-check",
        5,
        False,
        "Отказоустойчивость не проверяют, чтобы система продолжила работать, когда что-то сломается.",
    ),
    (
        "06-negative-contradiction",
        5,
        False,
        "Отказоустойчивость проверяют, чтобы система продолжила работать, когда что-то сломается. На деле при сбое система перестанет работать.",
    ),
    (
        "14-positive-core",
        13,
        True,
        "Каждый тестовый проход получает новую базу, чтобы результаты не влияли друг на друга.",
    ),
    (
        "14-positive-data-causality",
        13,
        True,
        "Каждый тест получает новую базу, чтобы данные прошлого запуска не искажали его результат.",
    ),
    (
        "14-positive-word-order",
        13,
        True,
        "Новая база для каждого прогона нужна, чтобы результаты не зависели от данных прошлого прогона.",
    ),
    (
        "14-positive-clean-synonym",
        13,
        True,
        "Каждый тестовый проход получает отдельную чистую базу, поэтому состояние прошлых тестов не искажает его результат.",
    ),
    (
        "14-negative-contradiction",
        13,
        False,
        "Каждый тестовый проход получает новую базу, чтобы результаты не влияли друг на друга. Но остатки прошлого теста продолжают влиять на следующий.",
    ),
]


@pytest.mark.parametrize("case_id,case_index,expected,message", SOL047, ids=[row[0] for row in SOL047])
def test_sol047_seventeen_independent_messages(
    case_id: str, case_index: int, expected: bool, message: str
) -> None:
    predicate = {
        3: battery._a09_04_relation_is_exact,
        5: battery._a09_06_relation_is_exact,
        13: battery._a09_14_relation_is_exact,
    }[case_index]
    assert predicate(message) is expected, case_id
    failures = _eval(case_index, message)
    if expected:
        assert "content_semantic_group_missing" not in failures
        assert "content_required_alternative_missing" not in failures
    else:
        assert "content_semantic_group_missing" in failures


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "Изолированное тестовое окружение не защищает рабочую базу данных от изменений.",
            False,
        ),
        (
            "Изолированное окружение удобно. Рабочую базу данных защищает отдельный бэкап.",
            False,
        ),
        (
            "Изолированная среда даёт стабильные результаты, потому что внешний мир не влияет на тесты.",
            True,
        ),
        (
            "Проверка отказоустойчивости нужна, чтобы при отказе узла остальные процессы продолжили работать.",
            True,
        ),
        (
            "Отказоустойчивость проверяют, чтобы при сбое система не продолжила работать.",
            False,
        ),
        (
            "Каждый прогон поднимает свежую базу, поэтому прошлые данные не искажают результат.",
            True,
        ),
        (
            "Новая база на каждый тест нужна, чтобы прошлый прогон не влиял на текущий результат.",
            True,
        ),
        (
            "Каждый тест получает новую базу, но данные прошлого запуска искажают результат.",
            False,
        ),
    ],
)
def test_a09_nearby_mutations_around_sol047(message: str, expected: bool) -> None:
    if "изолир" in message.casefold() or "изоляц" in message.casefold():
        predicate = battery._a09_04_relation_is_exact
        case_index = 3
    elif "отказоустойчив" in message.casefold() or "устойчивост" in message.casefold():
        predicate = battery._a09_06_relation_is_exact
        case_index = 5
    else:
        predicate = battery._a09_14_relation_is_exact
        case_index = 13
    assert predicate(message) is expected
    failures = _eval(case_index, message)
    if expected:
        assert "content_semantic_group_missing" not in failures
    else:
        assert "content_semantic_group_missing" in failures


SOL050_CLAIM_SCOPE = [
    (
        "06-safe-collapse-only",
        5,
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, и пользователи не столкнутся с полным крахом.",
    ),
    (
        "06-affirmative-collapse-only",
        5,
        False,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, но сервис полностью рухнет.",
    ),
    (
        "06-safe-then-collapse",
        5,
        False,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, пользователи не столкнутся с полным крахом, но сервис полностью рухнет.",
    ),
    (
        "06-collapse-then-safe",
        5,
        False,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, сервис полностью рухнет, хотя пользователи не столкнутся с полным крахом.",
    ),
    (
        "06-incidental-parentheses",
        5,
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать (пользователь увидит уведомление).",
    ),
    (
        "06-negated-purpose",
        5,
        False,
        "Проверка отказоустойчивости нужна не для того, чтобы при сбое система продолжила работать.",
    ),
    (
        "04-bound-contradiction",
        3,
        False,
        "Изолированное окружение защищает рабочую базу, но всё равно изменяет её данные.",
    ),
    (
        "14-bound-contradiction",
        13,
        False,
        "Новая база нужна для каждого прогона, однако прошлые данные всё равно искажают результат.",
    ),
]

SOL051_SCOPED_VARIANTS = [
    (
        "06-locally-negated-service",
        5,
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, а сервис не рухнет полностью.",
    ),
    (
        "06-locally-negated-after-user",
        5,
        True,
        "Проверка отказоустойчивости нужна, чтобы убедиться: если компонент сломается, система продолжит работать, пользователи не столкнутся с полным крахом, и сервис полностью не рухнет.",
    ),
    (
        "04-locally-negated-mutation",
        3,
        True,
        "Изолированное окружение защищает рабочую базу и не изменяет её данные.",
    ),
    (
        "04-different-object-mutation",
        3,
        True,
        "Изолированное окружение защищает рабочую базу, но изменяет только тестовую копию.",
    ),
    (
        "04-no-mutation-explanation",
        3,
        True,
        "Изолированное окружение защищает рабочую базу, потому что изменения остаются в тестовой копии.",
    ),
    (
        "04-system-same-object",
        3,
        False,
        "Изолированная среда защищает рабочую систему, но всё равно затрагивает её.",
    ),
    (
        "04-system-different-object",
        3,
        True,
        "Изолированная среда защищает рабочую систему, но затрагивает только тестовый сервис.",
    ),
]


@pytest.mark.parametrize(
    "case_id,case_index,expected,message",
    SOL050_CLAIM_SCOPE + SOL051_SCOPED_VARIANTS,
    ids=[row[0] for row in SOL050_CLAIM_SCOPE + SOL051_SCOPED_VARIANTS],
)
def test_a09_claim_scope_regressions(case_id: str, case_index: int, expected: bool, message: str) -> None:
    predicate = {
        3: battery._a09_04_relation_is_exact,
        5: battery._a09_06_relation_is_exact,
        13: battery._a09_14_relation_is_exact,
    }[case_index]
    assert predicate(message) is expected, case_id
    failures = _eval(case_index, message)
    if expected:
        assert "content_semantic_group_missing" not in failures
        assert "content_required_alternative_missing" not in failures
    else:
        assert "content_semantic_group_missing" in failures


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Изолированное окружение защищает рабочую базу, но затем изменяет рабочую базу.", False),
        ("Изолированное окружение защищает рабочую базу. Затем оно изменяет рабочую базу.", False),
        ("Изолированное окружение защищает рабочую базу. Затем оно изменяет её данные.", False),
        ("Изолированное окружение защищает рабочую базу. Оно не изменяет её данные.", True),
        ("Изолированное окружение защищает рабочую базу. Затем оно изменяет тестовую копию.", True),
        (
            "Изолированное окружение защищает рабочую базу. Тестовая копия используется для проверки. Затем система изменяет её данные.",
            True,
        ),
        (
            "Изолированное окружение защищает рабочую базу. Тестовая копия используется для проверки. Затем система изменяет рабочую базу.",
            False,
        ),
        (
            "Изолированное окружение защищает рабочую базу. Следующее предложение лишь объясняет порядок проверки.",
            True,
        ),
    ],
)
def test_a09_04_generation2_claim_scope_controls(answer: str, expected: bool) -> None:
    assert battery._a09_04_working_asset_protection(answer) is expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Не изолированное окружение защищает рабочую базу.", False),
        ("Изолированное окружение предохраняет рабочую базу.", True),
        ("Изолированное окружение не предохраняет рабочую базу.", False),
        (
            "Изолированное окружение защищает рабочую базу. Тестовая копия может изменяться.",
            True,
        ),
    ],
)
def test_a09_04_generation3_regressions(answer: str, expected: bool) -> None:
    assert battery._a09_04_relation_is_exact(answer) is expected


ASTRA118_A09_04_EXTERNAL_NOISE = [
    (
        "external-factors-causal",
        True,
        "Изолированная тестовая среда исключает влияние внешних факторов и тем самым обеспечивает стабильный воспроизводимый результат проверки.",
    ),
    (
        "random-data-causal",
        True,
        "В изолированном тестовом окружении случайные данные не влияют на проверку, поэтому результат стабилен и воспроизводим.",
    ),
    (
        "negated-exclusion",
        False,
        "Изолированное тестовое окружение не исключает влияние внешних факторов, хотя результат стабилен и воспроизводим.",
    ),
    (
        "contradictory-random-data",
        False,
        "Изолированное тестовое окружение исключает внешние факторы, но случайные данные искажают стабильный результат проверки.",
    ),
    (
        "different-object",
        False,
        "Изолированное окружение исключает влияние внешних факторов, а архивная копия обеспечивает стабильный результат отчёта.",
    ),
]


@pytest.mark.parametrize(
    "case_id,expected,message",
    ASTRA118_A09_04_EXTERNAL_NOISE,
    ids=[row[0] for row in ASTRA118_A09_04_EXTERNAL_NOISE],
)
def test_a09_04_external_noise_relation(case_id: str, expected: bool, message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is expected, case_id
    failures = _eval(3, message)
    assert ("content_semantic_group_missing" not in failures) is expected


ASTRA118_A09_14_PRIOR_RUN = [
    (
        "prior-run-exclusion",
        True,
        "Каждый тестовый прогон получает новую базу, чтобы исключить влияние предыдущего запуска. Это делает результаты независимыми и воспроизводимыми.",
    ),
    (
        "no-carryover",
        True,
        "Каждый тест получает новую базу: данные не перетекают из одного теста в другой. Благодаря этому результаты независимы и воспроизводимы.",
    ),
    (
        "negated-prior-exclusion",
        False,
        "Каждый тестовый прогон получает новую базу, чтобы не исключить влияние предыдущего запуска. Это делает результаты независимыми и воспроизводимыми.",
    ),
    (
        "prior-data-carries",
        False,
        "Каждый тестовый прогон получает новую базу, но данные прошлого запуска перетекают в следующий тест. Это делает результаты независимыми и воспроизводимыми.",
    ),
    (
        "different-object",
        False,
        "Каждый тестовый прогон получает новую базу, чтобы исключить влияние предыдущего отчёта на архив. Это делает результаты независимыми и воспроизводимыми.",
    ),
]


@pytest.mark.parametrize(
    "case_id,expected,message",
    ASTRA118_A09_14_PRIOR_RUN,
    ids=[row[0] for row in ASTRA118_A09_14_PRIOR_RUN],
)
def test_a09_14_prior_run_no_carryover_relation(case_id: str, expected: bool, message: str) -> None:
    assert battery._a09_14_relation_is_exact(message) is expected, case_id
    failures = _eval(13, message)
    assert ("content_semantic_group_missing" not in failures) is expected


ASTRA120_A09_04_LATER_NOISE = [
    (
        "affirmative-same-result",
        False,
        "Изолированная тестовая среда исключает влияние внешних факторов и обеспечивает стабильный воспроизводимый результат проверки. Случайные данные влияют на результат этой проверки.",
    ),
    (
        "negated-same-result",
        True,
        "Изолированная тестовая среда исключает влияние внешних факторов и обеспечивает стабильный воспроизводимый результат проверки. Случайные данные не влияют на результат этой проверки.",
    ),
    (
        "affirmative-unrelated-result",
        True,
        "Изолированная тестовая среда исключает влияние внешних факторов и обеспечивает стабильный воспроизводимый результат проверки. Случайные данные влияют на результат другого эксперимента.",
    ),
]


@pytest.mark.parametrize(
    "case_id,expected,message",
    ASTRA120_A09_04_LATER_NOISE,
    ids=[row[0] for row in ASTRA120_A09_04_LATER_NOISE],
)
def test_a09_04_later_noise_same_result_contradiction(case_id: str, expected: bool, message: str) -> None:
    assert battery._a09_04_relation_is_exact(message) is expected, case_id
    failures = _eval(3, message)
    assert ("content_semantic_group_missing" not in failures) is expected


# Actual first responses, retained unchanged as regression examples.
ACTUAL160_A09_06 = "Отказоустойчивость проверяют, чтобы убедиться, что система не «умрёт» или не потеряет данные, когда что-то пойдёт не так (упадёт сервер, оборвётся сеть, закончится диск). Простыми словами: это как проверить парашют перед прыжком — вы не надеетесь, что он сработает, а заранее тестируете, чтобы в критический момент он действительно спас."
ACTUAL160_A09_14 = "Каждый тестовый проход получает новую базу, чтобы исключить влияние предыдущих запусков: данные, созданные или изменённые в одном тесте, не «протекают» в следующий. Это гарантирует, что результат каждого прохода зависит только от самого теста и фиксированных входных данных, а не от состояния системы после других экспериментов."
OFFICIAL_A09_14 = "Каждый тестовый проход получает новую базу, чтобы исключить влияние предыдущих запусков. Это гарантирует, что результаты зависят только от проверяемого кода и входных данных, а не от «грязного» состояния, оставшегося после прошлых тестов."

FIRST_RESPONSE_PINS = [
    ("ACTUAL160_A09_06", 5, "5ca6c4b4e186c20c333ca54df674f34ade5d4308bd617affd40f985567eb2030"),
    ("ACTUAL160_A09_14", 13, "b2ab304ede7936dde9057c14125edd34d097a554a2a50fd63d7ad683378481d9"),
    ("OFFICIAL_A09_14", 13, "d22ea61542b1acdf4791ec778d37855683e03dfdd29b782447c174dfe0958d2d"),
]


@pytest.mark.parametrize("name,index,digest", FIRST_RESPONSE_PINS)
def test_first_responses_keep_exact_text_and_pass(name: str, index: int, digest: str) -> None:
    message = globals()[name]
    assert hashlib.sha256(message.encode()).hexdigest() == digest
    assert _eval(index, message) == []


@pytest.mark.parametrize(
    "message",
    [
        ACTUAL160_A09_06.replace("«умрёт»", "умрёт"),
        ACTUAL160_A09_06.replace("«умрёт»", "“погибнет”"),
        ACTUAL160_A09_14.replace("«протекают»", "протекают"),
        ACTUAL160_A09_14.replace("«протекают»", "“перетекают”"),
        OFFICIAL_A09_14.replace("«грязного»", "грязного"),
        OFFICIAL_A09_14.replace("«грязного»", '"грязного"'),
    ],
)
def test_emphasis_and_metaphor_variants_keep_the_same_claim(message: str) -> None:
    assert _eval(5 if message.startswith("Отказоустойчивость") else 13, message) == []


@pytest.mark.parametrize(
    "message",
    [
        ACTUAL160_A09_06.replace("не «умрёт»", "«умрёт»"),
        ACTUAL160_A09_06.replace("не потеряет данные", "потеряет данные"),
        ACTUAL160_A09_06.replace("система", "отчёт"),
        ACTUAL160_A09_06.replace("проверяют", "не проверяют"),
        ACTUAL160_A09_06.replace("проверяют", "может проверять"),
        ACTUAL160_A09_06.replace("что-то пойдёт не так", "всё хорошо"),
        "«" + ACTUAL160_A09_06 + "»",
        "Отчёт: «" + ACTUAL160_A09_06 + "»",
        ACTUAL160_A09_06 + " Однако система полностью рухнет.",
    ],
)
def test_survival_wording_keeps_fault_subject_polarity_and_authority(message: str) -> None:
    assert "content_semantic_group_missing" in _eval(5, message)


@pytest.mark.parametrize("base", [ACTUAL160_A09_14, OFFICIAL_A09_14])
@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("новую базу", "старую базу"),
        lambda text: text.replace("исключить влияние", "не исключить влияние"),
        lambda text: text.replace("предыдущих запусков", "предыдущих отчётов"),
        lambda text: text.replace("получает", "может получать"),
        lambda text: "«" + text + "»",
        lambda text: "Отчёт: «" + text + "»",
        lambda text: text + " Данные «протекают» в следующий тест.",
        lambda text: text + " Данные не протекают, а остатки «протекают» в следующий тест.",
    ],
)
def test_emphasis_does_not_hide_wrong_database_scope_or_positive_leak(base, mutation) -> None:
    assert "content_semantic_group_missing" in _eval(13, mutation(base))


_SUBJUNCTIVE_CARRYOVER = [
    "Если бы данные прошлого прохода перетекали в следующий, результаты зависели бы от старого состояния.",
    "Если бы данные прошлого прохода «протекали» в следующий, результаты были бы непредсказуемыми.",
    "Например, если бы использовалась та же база, данные прошлого запуска просачивались бы в следующий.",
    "Если бы данные перетекали между тестами, то следующий результат был бы зависим от предыдущего.",
]


@pytest.mark.parametrize("base", [ACTUAL160_A09_14, OFFICIAL_A09_14])
@pytest.mark.parametrize("example", _SUBJUNCTIVE_CARRYOVER)
def test_a09_14_explanatory_if_would_does_not_assert_actual_carryover(base, example):
    assert _eval(13, base + " " + example) == []


@pytest.mark.parametrize("example", _SUBJUNCTIVE_CARRYOVER)
def test_a09_14_counterfactual_is_not_itself_proof_of_fresh_database(example):
    assert "content_semantic_group_missing" in _eval(13, example)


@pytest.mark.parametrize(
    "claim",
    [
        "Данные прошлого прохода перетекают в следующий.",
        "Если бы данные прошлого прохода перетекали в следующий, они действительно перетекают туда сейчас.",
        "Если бы данные перетекали в следующий тест, результаты были бы зависимы, но данные всё равно протекают.",
        "Если бы данные перетекали в следующий тест, результаты были бы зависимы; данные протекают сейчас.",
        "Если бы данные перетекали в следующий тест, результаты были бы зависимы и данные протекают сейчас.",
        _SUBJUNCTIVE_CARRYOVER[0] + " Однако данные протекают в следующий тест.",
        _SUBJUNCTIVE_CARRYOVER[0] + " Используется старая база.",
        "Если бы данные перетекали между тестами, результаты были бы зависимыми и используется старая база.",
    ],
)
def test_a09_14_condition_does_not_launder_a_factual_contradiction(claim):
    assert "content_semantic_group_missing" in _eval(13, OFFICIAL_A09_14 + " " + claim)
