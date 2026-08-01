"""Расписание жило только в памяти процесса — редкие задачи не шли НИ РАЗУ.

Задача с `run_immediately=False` спала полный интервал от МОМЕНТА СТАРТА, а фаза нигде
не сохранялась. Значит суточная задача не выполняется никогда, если сервис
перезапускают чаще раза в сутки.

Не гипотеза, замерено на живой установке: за всё время жизни системы нет ни одной
записи `chronicle` и `reflection` в исходящих уведомлениях; `knowledge_quality_scan`
(12 ч) последний раз отработал за 4.5 суток и девять пропущенных окон до проверки, а у
второго арендатора не отрабатывал ни разу. Тем же затронуты `retrieval_eval` (сутки) и
`knowledge_dedup` (6 часов).

Вторая половина той же беды: `_publish` мержит в пустой словарь, а состояние пишется в
хранилище ЦЕЛИКОМ — поэтому первый же publish после перезапуска затирал историю и
обнулял счётчик отказов. Задача с интервалом 12 часов, падающая каждый прогон, не
могла добраться до порога деградации при ежедневной перезагрузке.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from friday.workers import IntervalTask, WorkerSupervisor


def _task(name: str = "daily", interval: float = 86_400.0) -> IntervalTask:
    async def _noop() -> None: ...

    return IntervalTask(name=name, func=_noop, interval_sec=interval, run_immediately=False)


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def test_a_task_overdue_since_the_last_run_starts_at_once():
    """Ровно случай владельца: суточная задача и перезапуск раз в несколько часов."""
    supervisor = WorkerSupervisor()
    supervisor.restore({"daily": {"last_finished_at": _ago(90_000)}})

    assert supervisor._initial_delay(_task()) == 0.0  # noqa: SLF001


def test_a_task_that_ran_recently_waits_out_the_remainder():
    """Иначе частые перезапуски превратились бы в непрерывный прогон."""
    supervisor = WorkerSupervisor()
    supervisor.restore({"daily": {"last_finished_at": _ago(3_600)}})

    delay = supervisor._initial_delay(_task())  # noqa: SLF001
    assert 82_000 < delay <= 83_000, f"остаток интервала посчитан неверно: {delay}"


def test_an_unknown_task_still_waits_a_full_interval():
    """Первый запуск на установке: старт не должен бить залпом по всем задачам."""
    supervisor = WorkerSupervisor()
    assert supervisor._initial_delay(_task()) == 86_400.0  # noqa: SLF001


def test_a_broken_timestamp_is_treated_as_unknown():
    supervisor = WorkerSupervisor()
    supervisor.restore({"daily": {"last_finished_at": "позавчера"}})
    assert supervisor._initial_delay(_task()) == 86_400.0  # noqa: SLF001


def test_restored_history_is_not_wiped_by_the_first_publish():
    """Состояние пишется в хранилище ЦЕЛИКОМ, поэтому мержить надо в восстановленное."""
    written: dict[str, dict] = {}
    supervisor = WorkerSupervisor(lambda name, state: written.__setitem__(name, state))
    supervisor.restore({"daily": {"last_success_at": "2026-07-01T00:00:00+00:00", "consecutive_failures": 2}})

    supervisor._publish(_task(), status="scheduled")  # noqa: SLF001

    state = supervisor.snapshot()["daily"]
    assert state["last_success_at"] == "2026-07-01T00:00:00+00:00", "история затёрта"
    assert state["consecutive_failures"] == 2, "счётчик отказов обнулён перезапуском"


# --- диагностика --------------------------------------------------------------


def _worker_state(**overrides):
    base = {
        "name": "chronicle",
        "interval_sec": 86_400.0,
        "timeout_sec": 300.0,
        "enabled": True,
        "status": "scheduled",
        "consecutive_failures": 0,
    }
    return {**base, **overrides}


def test_a_worker_that_never_ran_is_not_reported_healthy(settings, storage):
    """Он был освобождён от проверки ПО ПОСТРОЕНИЮ: нет `last_finished`, статус
    «scheduled» — то есть чем дольше он мёртв, тем надёжнее выглядит здоровым."""
    import dataclasses
    import json

    from friday.diagnostics import _worker_status

    # В общей фикстуре воркеры выключены, и тогда состояние даже не читается.
    settings = dataclasses.replace(settings, workers_enabled=True)
    storage.kv_set(
        "workers:health:chronicle",
        json.dumps(_worker_state(next_run_at=_ago(400_000)), ensure_ascii=False),
    )
    status = _worker_status(settings, storage)

    assert "chronicle" in status["stale_tasks"], "ни разу не запускавшийся воркер считается здоровым"
    assert status["healthy"] is False


def test_a_worker_merely_waiting_its_turn_is_left_alone(settings, storage):
    """Просрочки нет — значит и жалобы быть не должно, иначе шум перекроет сигнал."""
    import dataclasses
    import json

    from friday.diagnostics import _worker_status

    settings = dataclasses.replace(settings, workers_enabled=True)
    future = (datetime.now(UTC) + timedelta(seconds=3_600)).isoformat(timespec="seconds")
    storage.kv_set(
        "workers:health:chronicle", json.dumps(_worker_state(next_run_at=future), ensure_ascii=False)
    )
    status = _worker_status(settings, storage)

    assert status["stale_tasks"] == []
    assert status["healthy"] is True
