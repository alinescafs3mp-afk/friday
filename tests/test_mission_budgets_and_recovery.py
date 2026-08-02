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


def test_spend_accumulates_without_losing_a_tick(storage):
    """Мутация: заменить `spent = spent + ?` чтением и записью — тест краснеет.

    Два тика, прочитавшие одно значение, потеряли бы один из расходов, и бюджет
    никогда бы не кончился.
    """
    mission_id = _mission(storage)
    for _ in range(5):
        storage.add_mission_spend(mission_id, "alice", seconds=3, tool_calls=2, retries=1)
    row = storage.execute(
        "SELECT spent_seconds, spent_tool_calls, spent_retries FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    assert (row["spent_seconds"], row["spent_tool_calls"], row["spent_retries"]) == (15, 10, 5)


def test_an_empty_spend_touches_nothing(storage):
    mission_id = _mission(storage)
    before = storage.execute("SELECT updated_at FROM missions WHERE id=?", (mission_id,)).fetchone()
    storage.add_mission_spend(mission_id, "alice", seconds=0, tool_calls=0)
    after = storage.execute("SELECT updated_at FROM missions WHERE id=?", (mission_id,)).fetchone()
    assert before["updated_at"] == after["updated_at"]


def test_attempts_are_counted_before_the_work(storage):
    """Счёт до работы: процесс, умерший в середине шага, не должен обнулять его."""
    mission_id = _mission(storage)
    task_id = _task(storage, mission_id)
    assert storage.bump_mission_task_attempt(task_id, "alice") == 1
    assert storage.bump_mission_task_attempt(task_id, "alice") == 2


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, ""),
        ({"budget_seconds": 600, "spent_seconds": 10}, ""),
        ({"budget_seconds": 600, "spent_seconds": 600}, "исчерпан бюджет «время»"),
        ({"budget_tool_calls": 5, "spent_tool_calls": 9}, "исчерпан бюджет «вызовы инструментов»"),
        ({"budget_retries": 2, "spent_retries": 2}, "исчерпан бюджет «повторы»"),
        ({"deadline_at": "2020-01-01T00:00:00+00:00"}, "истёк срок миссии"),
        # Ноль — «без ограничения», а не «нулевой бюджет».
        ({"budget_seconds": 0, "spent_seconds": 10_000}, ""),
    ],
)
def test_the_budget_verdict_names_what_ran_out(fields, expected):
    """Мутация: сравнивать `spent > budget` вместо `>=` — тест краснеет.

    «Потрачено ровно столько, сколько отпущено» — это исчерпанный бюджет, а не
    последний разрешённый шаг.
    """
    from friday.executive.service import ExecutiveService

    mission = {"id": "m1", "user_id": "alice", **fields}
    verdict = ExecutiveService._budget_verdict(mission)  # noqa: SLF001
    if expected:
        assert expected in verdict
    else:
        assert verdict == ""


def test_a_mission_out_of_budget_is_stopped_before_the_next_step():
    """Мутация: убрать проверку из `_advance_mission` — тест краснеет.

    Проверка стоит ПЕРЕД выбором шага: миссия, у которой кончилось время, иначе
    сделает ещё один шаг, и именно он может оказаться с побочным эффектом.
    """
    import inspect

    from friday.executive.service import ExecutiveService

    source = inspect.getsource(ExecutiveService._advance_mission)  # noqa: SLF001
    assert "_budget_verdict(mission)" in source, "бюджет миссии никто не проверяет"
    assert source.index("_budget_verdict") < source.index("_pick_runnable"), (
        "бюджет проверяется уже после выбора шага"
    )


def test_an_interrupted_side_effect_is_never_replayed_blindly(storage, settings):
    """Мутация: убрать ветку `side_effect` из `_reclaim_stale_tasks` — тест краснеет.

    Спека v3 §5: «Uncertain side effects require reconciliation, not automatic
    replay». Шаг, оборвавшийся на середине побочного эффекта, возвращался в
    очередь наравне с любым другим — то есть письмо уходило второй раз, слияние
    выполнялось второй раз, и откатить это уже нельзя.
    """
    from datetime import UTC, datetime, timedelta

    from friday.executive.service import ExecutiveService
    from friday.storage.models import TaskStatus

    mission_id = _mission(storage)
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    risky = _task(storage, mission_id, seq=1, side_effect=1, status="running", started_at=long_ago)
    plain = _task(storage, mission_id, seq=2, side_effect=0, status="running", started_at=long_ago)

    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(  # noqa: SLF001
        service, {"id": mission_id, "user_id": "alice"}
    )

    states = {
        row["id"]: row["status"]
        for row in storage.execute("SELECT id, status FROM mission_tasks WHERE mission_id=?", (mission_id,))
    }
    assert states[risky] == TaskStatus.UNCERTAIN.value, "прерванный побочный эффект отправлен на повтор"
    assert states[plain] == TaskStatus.PENDING.value, "обычный шаг перестал переигрываться"


def test_uncertain_is_not_a_terminal_state():
    """Миссия с неизвестным исходом не должна тихо «завершиться».

    Человек обязан увидеть её незакрытой — иначе неизвестность превращается в
    молчаливый успех.
    """
    from friday.storage.models import TASK_TERMINAL_STATUSES, TaskStatus

    assert TaskStatus.UNCERTAIN not in TASK_TERMINAL_STATUSES
    assert TaskStatus.COMPENSATED in TASK_TERMINAL_STATUSES


def test_compensation_is_offered_to_a_person_not_executed(storage, settings):
    """Мутация: убрать вызов `_offer_compensation` — тест краснеет.

    Спека v3 §5 разрешает откат «where safe». Здесь безопасного нет: исход
    неизвестен, и автоматическая компенсация НЕСДЕЛАННОГО действия так же
    необратима, как его повтор. Поэтому откат идёт человеку заявкой, тем же
    путём, что любое опасное действие.
    """
    from datetime import UTC, datetime, timedelta

    from friday.executive.service import ExecutiveService

    mission_id = _mission(storage)
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _task(
        storage,
        mission_id,
        side_effect=1,
        status="running",
        started_at=long_ago,
        title="объединить сущности",
        compensation="разъединить сущности ent_1 и ent_2",
        checkpoint_json='{"entity_a": "ent_1", "entity_b": "ent_2"}',
    )

    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001

    pending = storage.list_action_approvals("alice", status="pending")
    assert pending, "откат не предложен человеку"
    record = pending[0]
    assert record["tool"] == "mission_compensation"
    assert "разъединить" in str(record.get("summary") or "")
    # Заявка — это ПРЕДЛОЖЕНИЕ: ничего ещё не выполнено.
    assert record["status"] == "pending"


def test_a_step_without_a_written_compensation_offers_nothing(storage, settings):
    """Пустая заявка «сделайте что-нибудь» хуже её отсутствия."""
    from datetime import UTC, datetime, timedelta

    from friday.executive.service import ExecutiveService

    mission_id = _mission(storage)
    long_ago = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _task(storage, mission_id, side_effect=1, status="running", started_at=long_ago, compensation="")

    service = object.__new__(ExecutiveService)
    service.storage = storage
    ExecutiveService._reclaim_stale_tasks(service, {"id": mission_id, "user_id": "alice"})  # noqa: SLF001

    assert storage.list_action_approvals("alice", status="pending") == []


def test_an_uncertain_step_is_reconciled_by_looking_at_the_world(storage, settings):
    """Мутация: убрать `_reconcile_uncertain` из тика — тест краснеет.

    Спека v3 §5: неизвестный исход побочного эффекта требует СВЕРКИ, а не
    повтора. Сверка не гадает: у части инструментов есть проверка постусловия,
    читающая факт из хранилища. Эффект случился — шаг закрывается сделанным;
    не случился — возвращается в очередь, где повтор уже безопасен.
    """
    from friday.executive.service import ExecutiveService

    mission_id = _mission(storage)
    happened = _task(
        storage,
        mission_id,
        seq=1,
        status="uncertain",
        side_effect=1,
        checkpoint_json='{"tool": "test_effect", "arguments": {"id": "yes"}}',
    )
    missed = _task(
        storage,
        mission_id,
        seq=2,
        status="uncertain",
        side_effect=1,
        checkpoint_json='{"tool": "test_effect", "arguments": {"id": "no"}}',
    )
    unknown = _task(
        storage,
        mission_id,
        seq=3,
        status="uncertain",
        side_effect=1,
        checkpoint_json='{"tool": "no_such_check", "arguments": {}}',
    )

    from friday.execution_kernel import POSTCONDITIONS

    POSTCONDITIONS["test_effect"] = lambda st, uid, args: (
        (True, "запись на месте") if args.get("id") == "yes" else (False, "записи нет")
    )
    try:
        service = object.__new__(ExecutiveService)
        service.storage = storage
        count = ExecutiveService._reconcile_uncertain(  # noqa: SLF001
            service, {"id": mission_id, "user_id": "alice"}
        )
    finally:
        POSTCONDITIONS.pop("test_effect", None)

    states = {
        row["id"]: row["status"]
        for row in storage.execute("SELECT id, status FROM mission_tasks WHERE mission_id=?", (mission_id,))
    }
    assert count == 2
    assert states[happened] == "done", "подтверждённый эффект не закрыт"
    assert states[missed] == "pending", "несостоявшийся эффект не возвращён в очередь"
    assert states[unknown] == "uncertain", (
        "шаг без проверки постусловия решён за человека — а сверять его нечем"
    )


def test_reconciliation_runs_in_the_same_tick_as_the_marking():
    """Шаг, только что признанный неизвестным, проверяется сразу, а не через 15 минут."""
    import inspect

    from friday.executive.service import ExecutiveService

    source = inspect.getsource(ExecutiveService.tick)
    assert "_reconcile_uncertain(" in source, "сверка не вызывается из тика"
    assert source.index("_reclaim_stale_tasks") < source.index("_reconcile_uncertain"), (
        "сверка идёт до пометки — ей нечего будет сверять"
    )
