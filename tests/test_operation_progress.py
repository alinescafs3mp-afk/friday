"""Closed operation-progress projection: truthful plan, one focus, no ETA."""

from __future__ import annotations

import pytest

from friday.orchestration.operation_progress import (
    OPERATION_PROGRESS_SCHEMA,
    OperationProgressError,
    build_operation_progress,
    render_operation_progress,
)


def _base(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": OPERATION_PROGRESS_SCHEMA,
        "operation_id": "chat:8801",
        "authenticated_turn_id": "turn:aa11",
        "revision": 3,
        "terminal": False,
        "mode": "chat",
        "title": "Выполняю задачу",
        "ordered_steps": [
            {
                "step_id": "file_report",
                "safe_label": "Обрабатываю «report.pdf»",
                "state": "completed",
                "completed_units": 1,
                "total_units": 1,
                "percentage": 100,
                "evidence_class": "files",
            },
            {
                "step_id": "file_contract",
                "safe_label": "Ищу «contract.docx»",
                "state": "running",
                "completed_units": 2,
                "total_units": 4,
                "percentage": None,
                "evidence_class": "sources",
            },
            {
                "step_id": "compare",
                "safe_label": "Сопоставляю документы",
                "state": "pending",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "none",
            },
            {
                "step_id": "web",
                "safe_label": "Ищу актуальные данные в интернете",
                "state": "pending",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "none",
            },
            {
                "step_id": "table",
                "safe_label": "Формирую сводную таблицу",
                "state": "pending",
                "completed_units": None,
                "total_units": None,
                "percentage": None,
                "evidence_class": "none",
            },
        ],
        "active_step_id": "file_contract",
        "elapsed_sec": 84,
        "hard_deadline_remaining_sec": None,
        "result_delivery_state": "not_started",
        "plan_generation": 1,
    }
    payload.update(overrides)
    return payload


def test_chat_plan_matches_the_owner_two_message_status_shape() -> None:
    projection = build_operation_progress(_base())
    assert render_operation_progress(projection) == (
        "⏳ Выполняю задачу\n"
        "\n"
        "✅ Обрабатываю «report.pdf» - 100%\n"
        "▶️ **Ищу «contract.docx» - 2 из 4 источников**\n"
        "▫️ Сопоставляю документы - 0%\n"
        "▫️ Ищу актуальные данные в интернете - 0%\n"
        "▫️ Формирую сводную таблицу - 0%\n"
        "\n"
        "Прошло: 1 мин 24 с"
    )


def test_engineer_scan_renders_one_focus_and_measured_host_counts() -> None:
    projection = build_operation_progress(
        _base(
            operation_id="engineer:job1",
            mode="engineer",
            title="Проверяю сеть 192.168.1.0/24",
            elapsed_sec=372,
            ordered_steps=[
                {
                    "step_id": "hosts",
                    "safe_label": "Обнаруживаю активные хосты",
                    "state": "completed",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": 100,
                    "evidence_class": "hosts",
                },
                {
                    "step_id": "services",
                    "safe_label": "Определяю сервисы и версии",
                    "state": "completed",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": 100,
                    "evidence_class": "none",
                },
                {
                    "step_id": "cves",
                    "safe_label": "Сопоставляю сервисы с кандидатами уязвимостей",
                    "state": "running",
                    "completed_units": 18,
                    "total_units": 31,
                    "percentage": None,
                    "evidence_class": "none",
                },
                {
                    "step_id": "probe",
                    "safe_label": "Безопасно проверяю применимость",
                    "state": "pending",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": None,
                    "evidence_class": "none",
                },
                {
                    "step_id": "report",
                    "safe_label": "Формирую отчёт",
                    "state": "pending",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": None,
                    "evidence_class": "none",
                },
            ],
            active_step_id="cves",
        )
    )
    assert render_operation_progress(projection) == (
        "⏳ Проверяю сеть 192.168.1.0/24\n"
        "\n"
        "✅ Обнаруживаю активные хосты - 100%\n"
        "✅ Определяю сервисы и версии - 100%\n"
        "▶️ **Сопоставляю сервисы с кандидатами уязвимостей - 18 из 31**\n"
        "▫️ Безопасно проверяю применимость - 0%\n"
        "▫️ Формирую отчёт - 0%\n"
        "\n"
        "Прошло: 6 мин 12 с"
    )


def test_coding_running_step_may_show_derived_percentage() -> None:
    projection = build_operation_progress(
        _base(
            mode="coding",
            title="Дорабатываю проект «photo-indexer»",
            elapsed_sec=60,
            ordered_steps=[
                {
                    "step_id": "inspect",
                    "safe_label": "Проверяю исходный архив",
                    "state": "completed",
                    "completed_units": 1,
                    "total_units": 1,
                    "percentage": 100,
                    "evidence_class": "files",
                },
                {
                    "step_id": "map",
                    "safe_label": "Строю карту проекта",
                    "state": "completed",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": 100,
                    "evidence_class": "none",
                },
                {
                    "step_id": "plan",
                    "safe_label": "Планирую изменения",
                    "state": "completed",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": 100,
                    "evidence_class": "none",
                },
                {
                    "step_id": "implement",
                    "safe_label": "Реализую импорт метаданных",
                    "state": "running",
                    "completed_units": 9,
                    "total_units": 14,
                    "percentage": 64,
                    "evidence_class": "tasks",
                },
                {
                    "step_id": "tests",
                    "safe_label": "Запускаю тесты",
                    "state": "pending",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": None,
                    "evidence_class": "tests",
                },
            ],
            active_step_id="implement",
        )
    )
    rendered = render_operation_progress(projection)
    assert rendered.startswith("🛠 Дорабатываю проект «photo-indexer»")
    assert "▶️ **Реализую импорт метаданных - 9 из 14 задач, 64%**" in rendered
    assert "ETA" not in rendered and "осталось примерно" not in rendered


def test_open_ended_running_step_has_no_percentage() -> None:
    projection = build_operation_progress(
        _base(
            ordered_steps=[
                {
                    "step_id": "think",
                    "safe_label": "Разбираю запрос",
                    "state": "running",
                    "completed_units": None,
                    "total_units": None,
                    "percentage": None,
                    "evidence_class": "none",
                }
            ],
            active_step_id="think",
            elapsed_sec=9,
        )
    )
    assert render_operation_progress(projection) == (
        "⏳ Выполняю задачу\n\n▶️ **Разбираю запрос**\n\nПрошло: 9 с"
    )


def test_plan_revision_inserts_one_monotonic_notice() -> None:
    projection = build_operation_progress(_base(plan_generation=2, elapsed_sec=12))
    text = render_operation_progress(projection)
    assert text.count("План уточнён") == 1
    assert text.splitlines()[2] == "План уточнён"


def test_uncertain_delivery_edits_status_instead_of_duplicating_a_result() -> None:
    steps = [
        {
            "step_id": "done",
            "safe_label": "Формирую отчёт",
            "state": "completed",
            "completed_units": None,
            "total_units": None,
            "percentage": 100,
            "evidence_class": "none",
        }
    ]
    projection = build_operation_progress(
        _base(
            terminal=True,
            ordered_steps=steps,
            active_step_id="done",
            result_delivery_state="uncertain",
            elapsed_sec=30,
        )
    )
    text = render_operation_progress(projection)
    assert text.startswith("⚠️ Выполняю задачу")
    assert "⚠️ Доставка результата не подтверждена." in text


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param(
            {
                "ordered_steps": [
                    {
                        "step_id": "a",
                        "safe_label": "Первый",
                        "state": "running",
                        "completed_units": None,
                        "total_units": None,
                        "percentage": None,
                        "evidence_class": "none",
                    },
                    {
                        "step_id": "b",
                        "safe_label": "Второй",
                        "state": "running",
                        "completed_units": None,
                        "total_units": None,
                        "percentage": None,
                        "evidence_class": "none",
                    },
                ],
                "active_step_id": "a",
            },
            "multiple_running_steps",
            id="two_running_steps",
        ),
        pytest.param(
            {
                "ordered_steps": [
                    {
                        "step_id": "a",
                        "safe_label": "Бегу",
                        "state": "running",
                        "completed_units": 3,
                        "total_units": 10,
                        "percentage": 63,
                        "evidence_class": "tasks",
                    }
                ],
                "active_step_id": "a",
            },
            "percentage_unmeasured",
            id="fabricated_percent",
        ),
        pytest.param({"title": "/etc/passwd"}, "title_path", id="title_path"),
        pytest.param({"title": "см. https://example.com/x"}, "title_url", id="title_url"),
        pytest.param({"title": "token=abc"}, "title_secret", id="title_secret"),
        pytest.param(
            {
                "ordered_steps": [
                    {
                        "step_id": "a",
                        "safe_label": "../../secret",
                        "state": "pending",
                        "completed_units": None,
                        "total_units": None,
                        "percentage": None,
                        "evidence_class": "none",
                    }
                ],
                "active_step_id": None,
                "terminal": True,
                "result_delivery_state": "confirmed",
            },
            "safe_label_path",
            id="label_path",
        ),
    ],
)
def test_contract_fails_closed_on_unmeasured_or_unsafe_facts(
    overrides: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(OperationProgressError, match=code):
        build_operation_progress(_base(**overrides))


def test_round_trip_mapping_is_closed_and_stable() -> None:
    projection = build_operation_progress(_base())
    again = build_operation_progress(projection.to_mapping())
    assert again == projection
    assert again.to_mapping()["schema"] == OPERATION_PROGRESS_SCHEMA
    assert "chain_of_thought" not in again.to_mapping()
