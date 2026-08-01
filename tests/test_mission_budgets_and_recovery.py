"""Миссия несёт свои ограничения в базе, а не в памяти исполнителя.

Спека v3 §5: «Retry, recursion, token, time, network, storage, process, and
monetary budgets are enforced below the model» и «Crash recovery distinguishes
not-started, executing, succeeded, failed, compensated, cancelled, and
uncertain. Uncertain side effects require reconciliation, not automatic replay.»

Оба требования об одном: миссия обязана пережить перезапуск процесса вместе со
своими границами и со следами того, что успело случиться. Бюджет, живущий в
памяти воркера, после падения превращается в чистый лист — и вторая попытка
тратит его заново.
"""

from __future__ import annotations

import sqlite3

import pytest


def _mission(storage, user_id: str = "alice") -> str:
    from friday.storage.models import new_id, utc_now

    storage.ensure_user(user_id)
    mission_id = new_id("mis")
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO missions(id, user_id, goal, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?)""",
            (mission_id, user_id, "собрать справку", now, now),
        )
    return mission_id


def _task(storage, mission_id: str, user_id: str = "alice", **fields) -> str:
    from friday.storage.models import new_id, utc_now

    task_id = new_id("mst")
    now = utc_now()
    columns = {
        "id": task_id,
        "mission_id": mission_id,
        "user_id": user_id,
        "seq": 1,
        "instruction": "шаг",
        "created_at": now,
        "updated_at": now,
        **fields,
    }
    names = ", ".join(columns)
    holders = ", ".join("?" * len(columns))
    with storage.transaction() as conn:
        conn.execute(f"INSERT INTO mission_tasks({names}) VALUES({holders})", tuple(columns.values()))  # noqa: S608
    return task_id


def test_a_mission_carries_its_budgets_and_deadline(storage):
    """Мутация: убрать колонки бюджета — тест краснеет.

    Ноль значит «без ограничения»: миссия без бюджета — это не миссия с нулевым
    бюджетом, и путать их нельзя.
    """
    mission_id = _mission(storage)
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE missions SET budget_seconds=?, budget_tool_calls=?, budget_retries=?,
               deadline_at=? WHERE id=?""",
            (600, 20, 3, "2026-08-02T12:00:00+00:00", mission_id),
        )
    row = storage.execute(
        """SELECT budget_seconds, budget_tool_calls, budget_retries, deadline_at,
           spent_seconds, spent_tool_calls, spent_retries FROM missions WHERE id=?""",
        (mission_id,),
    ).fetchone()
    assert row["budget_seconds"] == 600
    assert row["budget_tool_calls"] == 20
    assert row["budget_retries"] == 3
    assert row["deadline_at"].startswith("2026-08-02")
    # Потрачено — отдельно от отпущенного: иначе после перезапуска нельзя
    # ответить, сколько бюджета осталось.
    assert row["spent_seconds"] == 0
    assert row["spent_tool_calls"] == 0


def test_spent_budget_survives_a_restart(storage, settings):
    """Потраченное записано в базу, а не в память воркера."""
    mission_id = _mission(storage)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE missions SET spent_seconds=?, spent_tool_calls=?, spent_retries=? WHERE id=?",
            (42, 7, 2, mission_id),
        )
    storage.close(final=False)

    from friday.storage import init_storage

    reopened = init_storage(settings)
    row = reopened.execute(
        "SELECT spent_seconds, spent_tool_calls, spent_retries FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    assert (row["spent_seconds"], row["spent_tool_calls"], row["spent_retries"]) == (42, 7, 2)


@pytest.mark.parametrize("state", ["pending", "running", "done", "failed", "skipped", "uncertain", "compensated"])
def test_every_recovery_state_is_allowed(storage, state):
    """Мутация: вернуть прежний CHECK без `uncertain` — тест краснеет.

    `uncertain` значит «неизвестно, случился ли побочный эффект». База, не
    знающая этого состояния, отвергает запись ровно в тот момент, когда она
    нужнее всего — при разборе сбоя рядом с эффектом.
    """
    mission_id = _mission(storage)
    task_id = _task(storage, mission_id)
    with storage.transaction() as conn:
        conn.execute("UPDATE mission_tasks SET status=? WHERE id=?", (state, task_id))
    assert storage.execute(
        "SELECT status FROM mission_tasks WHERE id=?", (task_id,)
    ).fetchone()["status"] == state


def test_an_invented_state_is_still_refused(storage):
    """Список состояний расширен, но не отменён: опечатка обязана падать."""
    mission_id = _mission(storage)
    task_id = _task(storage, mission_id)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute("UPDATE mission_tasks SET status='почти_готово' WHERE id=?", (task_id,))


def test_a_side_effecting_step_keeps_its_checkpoint_and_compensation(storage):
    """Шаг с побочным эффектом обязан помнить, что откатывать.

    Спека v3 §5: «Side-effecting steps have checkpoints and rollback or
    compensation where safe». Компенсация, известная только автору плана,
    бесполезна после перезапуска.
    """
    mission_id = _mission(storage)
    task_id = _task(
        storage,
        mission_id,
        side_effect=1,
        checkpoint_json='{"entity_id": "ent_1", "name_before": "Иванов И."}',
        compensation="вернуть прежнее имя сущности ent_1",
    )
    row = storage.execute(
        "SELECT side_effect, checkpoint_json, compensation, attempts FROM mission_tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    assert row["side_effect"] == 1
    assert "name_before" in row["checkpoint_json"]
    assert row["compensation"]
    assert row["attempts"] == 0


def test_attempts_are_counted_on_the_step_itself(storage):
    """Счётчик попыток — свойство шага, а не переменная цикла.

    Бюджет повторов нельзя проверить, если после перезапуска неизвестно,
    сколько попыток уже сделано.
    """
    mission_id = _mission(storage)
    task_id = _task(storage, mission_id)
    for expected in (1, 2, 3):
        with storage.transaction() as conn:
            conn.execute("UPDATE mission_tasks SET attempts = attempts + 1 WHERE id=?", (task_id,))
        assert storage.execute(
            "SELECT attempts FROM mission_tasks WHERE id=?", (task_id,)
        ).fetchone()["attempts"] == expected
