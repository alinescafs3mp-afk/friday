"""Sentinel organ (#6) — Friday monitors itself and pushes health alerts.

Covers: a degraded-worker fault becoming a deduplicated outbound alert, the
enable gate, allowlist deny-by-default, quiet-hours suppression, the message
formatting, and registry composition. Diagnostics collection reuses the same
`collect_diagnostics` that powers the admin panel; here it reads the seeded
worker-health kv from the passed storage, so the alert is deterministic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from friday.organs import ServiceContext, build_registry
from friday.organs.sentinel import (
    _GENERATION_AWAIT_TIMEOUT_SEC,
    _GENERATION_PROBE_TIMEOUT_SEC,
    _GENERATION_RECENT_FOREGROUND_SEC,
    SentinelOrgan,
    _format_alert,
    scan_health,
    watch_generation,
    watch_model_gate,
)


def _sentinel_settings(*, quiet_start: int = 0, quiet_end: int = 0):
    from friday.config import load_settings

    return replace(
        load_settings(),
        sentinel_enabled=True,
        sentinel_check_llm=False,  # never probe a real port from the suite
        workers_enabled=True,  # required for worker-health diagnostics
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )


def _seed_telegram_user(storage, chat_id: str, *, preset_key: str = "admin") -> str:
    # Default `admin`, because host diagnostics are privileged: the audience is the
    # accounts that hold `admin.diagnostics`, the same gate the HTTP read uses. The
    # old default (`user`) is what let a guest read this machine's health.
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", preset_key=preset_key, metadata={"chat_id": chat_id})
    return user_id


def _seed_degraded_worker(storage, name: str = "reflection_digest") -> None:
    # consecutive_failures >= 3 marks a task degraded -> an 'error' diagnostics action.
    storage.kv_set(
        f"workers:health:{name}",
        json.dumps(
            {
                "name": name,
                "status": "error",
                "consecutive_failures": 4,
                "interval_sec": 86400,
                "last_finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "error_type": "RuntimeError",
            }
        ),
    )


# --- message formatting ---------------------------------------------------


def test_format_alert_shapes_message():
    msg = _format_alert(
        {"severity": "error", "title": "Сбой", "detail": "деталь", "command": "jericho doctor"}
    )
    assert msg.startswith("🚨 Friday: Сбой")
    assert "деталь" in msg
    assert "→ jericho doctor" in msg
    assert _format_alert({"severity": "warning", "title": "Мелочь"}).startswith("⚠️")


# --- worker scan ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_pushes_alert_for_degraded_workers(storage):
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "5001")  # on the conftest allowlist
    _seed_degraded_worker(storage)

    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert pending
    assert all(n["chat_id"] == "5001" and n["kind"] == "sentinel" for n in pending)
    assert any("Friday:" in n["body"] for n in pending)
    # The failed-workers diagnostics action reached the owner.
    assert any("Фоновые задачи" in n["body"] for n in pending)

    # A persistent fault does not turn into a stream: same day dedups.
    before = len(pending)
    await scan_health(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == before


@pytest.mark.asyncio
async def test_sentinel_disabled_is_silent(storage):
    settings = replace(_sentinel_settings(), sentinel_enabled=False)
    _seed_telegram_user(storage, "5001")
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_sentinel_skips_unallowlisted_chat(storage):
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "999999")  # not on the allowlist
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_sentinel_respects_quiet_hours(storage):
    # Час МЕСТНЫЙ: тихие часы теперь про ночь человека, а не про UTC.
    now_hour = datetime.now().astimezone().hour
    settings = _sentinel_settings(quiet_start=now_hour, quiet_end=(now_hour + 1) % 24)
    _seed_telegram_user(storage, "5001")
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


# --- registry -------------------------------------------------------------


def test_registry_includes_sentinel_worker(settings):
    registry = build_registry(settings)
    assert any(isinstance(o, SentinelOrgan) for o in registry.organs)
    ctx = ServiceContext(settings=settings, storage=None, kg=None, ingestion=None)
    workers = {worker.name: worker for worker in registry.workers(ctx)}
    assert "sentinel_watch" in workers
    assert "sentinel_generation_watch" in workers
    assert "sentinel_model_gate_watch" in workers

    generation = workers["sentinel_generation_watch"]
    assert generation.run_immediately is True
    assert generation.interval_sec <= 60
    assert generation.interval_sec + generation.timeout_sec <= 95
    assert generation.timeout_sec > _GENERATION_AWAIT_TIMEOUT_SEC

    model_gate = workers["sentinel_model_gate_watch"]
    assert model_gate.run_immediately is True
    assert model_gate.interval_sec <= 60
    assert model_gate.timeout_sec <= 5


@pytest.mark.asyncio
async def test_model_gate_watchdog_alerts_once_for_one_revocation_episode(storage):
    settings = replace(_sentinel_settings(), router_mode="canary")
    _seed_telegram_user(storage, "5001", preset_key="owner")
    gate = {
        "status": "canary_ready",
        "reason_code": "live_attestation_clear",
        "route_outcome": "legacy_fallback",
    }
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        model_gate_status=lambda: gate,
    )

    await watch_model_gate(ctx)
    assert storage.list_pending_notifications(limit=100) == []

    gate.update(
        {
            "status": "revoked",
            "reason_code": "private transport detail /srv/secret/model",
        }
    )
    await watch_model_gate(ctx)
    await watch_model_gate(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 1
    assert pending[0]["chat_id"] == "5001"
    assert pending[0]["kind"] == "sentinel"
    assert str(pending[0]["dedup_key"]).startswith("sentinel:v12_model_gate_revoked:episode:")
    assert "legacy-runtime" in str(pending[0]["body"])
    assert "private" not in str(pending[0]["body"])
    assert "/srv/secret" not in str(pending[0]["body"])


@pytest.mark.asyncio
async def test_model_gate_watchdog_rearms_only_after_observed_recovery(storage):
    settings = replace(_sentinel_settings(), router_mode="canary")
    _seed_telegram_user(storage, "5001", preset_key="owner")
    gate = {"status": "revoked", "reason_code": "epoch_invalid"}
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        model_gate_status=lambda: gate,
    )

    await watch_model_gate(ctx)
    first_key = str(storage.list_pending_notifications(limit=100)[0]["dedup_key"])
    first_body = str(storage.list_pending_notifications(limit=100)[0]["body"])
    gate.update({"status": "canary_ready", "reason_code": "live_attestation_clear"})
    await watch_model_gate(ctx)
    # A durable row may be delivered after recovery.  It must describe the
    # observed transition, never claim that the queued state is still current.
    assert "На момент проверки" in first_body
    assert "проверьте текущий" in first_body
    assert "сейчас не участвует" not in first_body
    assert "orchestration.model_gate" in first_body
    assert "‹путь скрыт›" not in first_body
    gate.update({"status": "revoked", "reason_code": "epoch_changed"})
    await watch_model_gate(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 2
    assert str(pending[1]["dedup_key"]) != first_key
    assert "Процесс модели изменился" in str(pending[1]["body"])


@pytest.mark.asyncio
async def test_model_gate_watchdog_rearms_in_legacy_and_reports_observer_failure(storage):
    settings = replace(_sentinel_settings(), router_mode="canary")
    _seed_telegram_user(storage, "5001", preset_key="owner")
    gate = {"status": "unavailable", "reason_code": "observer_unavailable"}
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        model_gate_status=lambda: gate,
    )

    await watch_model_gate(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == 1
    await watch_model_gate(replace(ctx, settings=replace(settings, router_mode="legacy")))
    await watch_model_gate(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 2
    assert pending[0]["dedup_key"] != pending[1]["dedup_key"]
    assert "недоступно наблюдателю" in str(pending[1]["body"])
    assert "не смог подтвердить состояние" in str(pending[1]["body"])
    assert "legacy-runtime" not in str(pending[1]["body"])


@pytest.mark.asyncio
async def test_model_gate_recovery_rearms_without_a_notification_audience(storage):
    settings = replace(_sentinel_settings(), router_mode="canary")
    gate = {"status": "revoked", "reason_code": "epoch_invalid"}
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        model_gate_status=lambda: gate,
    )

    await watch_model_gate(ctx)
    gate.update({"status": "canary_ready", "reason_code": "live_attestation_clear"})
    await watch_model_gate(ctx)
    state = json.loads(str(storage.kv_get("sentinel:v12_model_gate_watchdog")))
    assert state == {"episode": "", "status": "ready", "version": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sentinel_enabled", "router_mode"),
    [(False, "canary"), (True, "legacy")],
)
async def test_model_gate_watchdog_is_silent_when_disabled_or_legacy(
    storage,
    sentinel_enabled,
    router_mode,
):
    settings = replace(
        _sentinel_settings(),
        sentinel_enabled=sentinel_enabled,
        router_mode=router_mode,
    )
    _seed_telegram_user(storage, "5001", preset_key="owner")

    def must_not_observe():
        raise AssertionError("disabled/legacy mode observed the V12 gate")

    await watch_model_gate(
        ServiceContext(
            settings=settings,
            storage=storage,
            kg=None,
            ingestion=None,
            model_gate_status=must_not_observe,
        )
    )
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_model_gate_watchdog_respects_quiet_hours(storage):
    now_hour = datetime.now().astimezone().hour
    settings = replace(
        _sentinel_settings(quiet_start=now_hour, quiet_end=(now_hour + 1) % 24),
        router_mode="canary",
    )
    _seed_telegram_user(storage, "5001", preset_key="owner")
    await watch_model_gate(
        ServiceContext(
            settings=settings,
            storage=storage,
            kg=None,
            ingestion=None,
            model_gate_status=lambda: {
                "status": "revoked",
                "reason_code": "epoch_invalid",
            },
        )
    )
    assert storage.list_pending_notifications(limit=100) == []


def test_generation_watchdog_interval_is_bounded_by_its_detection_contract(monkeypatch, tmp_path):
    from friday.config import load_settings

    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FRIDAY_SENTINEL_GENERATION_INTERVAL_SEC", "999")
    assert load_settings().sentinel_generation_interval_sec == 60

    monkeypatch.setenv("FRIDAY_SENTINEL_GENERATION_INTERVAL_SEC", "1")
    assert load_settings().sentinel_generation_interval_sec == 30


@pytest.mark.asyncio
async def test_generation_watchdog_alerts_without_running_full_diagnostics(
    storage,
    monkeypatch,
):
    import friday.organs.sentinel as sentinel

    settings = replace(
        _sentinel_settings(),
        sentinel_check_llm=True,
        llm_enabled=True,
        sentinel_generation_interval_sec=60,
    )
    _seed_telegram_user(storage, "5001", preset_key="owner")
    probe_calls: list[tuple[str, str, float]] = []

    def failed_probe(base_url: str, model: str, *, timeout: float, **_kwargs):
        probe_calls.append((base_url, model, timeout))
        return {"generates": False, "seconds": timeout, "error": "synthetic timeout"}

    monkeypatch.setattr(sentinel, "_llm_generates", failed_probe)
    monkeypatch.setattr(
        sentinel,
        "collect_diagnostics",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the lightweight watchdog ran full diagnostics")
        ),
    )
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)

    await watch_generation(ctx)
    await watch_generation(ctx)

    assert probe_calls == [
        (settings.llm_base_url, settings.llm_model, _GENERATION_PROBE_TIMEOUT_SEC),
        (settings.llm_base_url, settings.llm_model, _GENERATION_PROBE_TIMEOUT_SEC),
    ]
    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 1, "a persistent stall must remain one owner alert per episode"
    assert pending[0]["chat_id"] == "5001"
    assert pending[0]["kind"] == "sentinel"
    assert "не отвечает" in str(pending[0]["body"])
    assert "ручная проверка" in str(pending[0]["body"])


@pytest.mark.asyncio
async def test_generation_watchdog_owns_a_deadline_and_alerts_when_the_probe_never_returns(
    storage,
    monkeypatch,
):
    import asyncio

    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")

    async def blocked_probe(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(sentinel, "run_blocking", blocked_probe)
    monkeypatch.setattr(sentinel, "_GENERATION_AWAIT_TIMEOUT_SEC", 0.02)

    await watch_generation(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 1
    assert "не отвечает" in str(pending[0]["body"])


@pytest.mark.asyncio
async def test_generation_watchdog_deduplicates_an_episode_but_realerts_after_recovery(
    storage,
    monkeypatch,
):
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")
    outcomes = [False, True, False]

    def scripted_probe(*_args, **_kwargs):
        generates = outcomes.pop(0)
        return {"generates": generates, "seconds": 0.2 if generates else 25.0}

    monkeypatch.setattr(sentinel, "_llm_generates", scripted_probe)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)

    await watch_generation(ctx)  # healthy -> failed: episode 1
    first = storage.list_pending_notifications(limit=100)
    assert len(first) == 1
    first_key = str(first[0]["dedup_key"])
    assert first_key.startswith("sentinel:llm_not_generating:episode:")

    # The heavy scan sees the same continuous outage and must share the exact
    # episode key instead of producing a second daily-dedup alert.
    monkeypatch.setattr(
        sentinel,
        "collect_diagnostics",
        lambda *_args, **_kwargs: {
            "actions": [
                {
                    "code": "llm_not_generating",
                    "severity": "error",
                    "title": "Synthetic duplicate",
                }
            ]
        },
    )
    await scan_health(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == 1

    await watch_generation(ctx)  # failed -> healthy closes episode 1
    await watch_generation(ctx)  # healthy -> failed: episode 2

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 2
    keys = [str(item["dedup_key"]) for item in pending]
    assert keys[0] == first_key
    assert keys[1].startswith("sentinel:llm_not_generating:episode:")
    assert keys[1] != first_key


@pytest.mark.asyncio
async def test_generation_watchdog_is_not_suppressed_by_an_old_daily_alert(
    storage,
    monkeypatch,
):
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    owner = _seed_telegram_user(storage, "5001", preset_key="owner")
    day = datetime.now().astimezone().date().isoformat()
    assert storage.enqueue_notification(
        owner,
        "5001",
        "Old daily sentinel alert",
        kind="sentinel",
        dedup_key=f"sentinel:llm_not_generating:{day}",
    )
    monkeypatch.setattr(
        sentinel,
        "_llm_generates",
        lambda *_args, **_kwargs: {"generates": False, "seconds": 25.0},
    )

    await watch_generation(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    pending = storage.list_pending_notifications(limit=100)
    assert len(pending) == 2
    assert any(str(item["dedup_key"]).startswith("sentinel:llm_not_generating:episode:") for item in pending)


@pytest.mark.asyncio
async def test_generation_watchdog_is_silent_for_a_working_model(storage, monkeypatch):
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")
    monkeypatch.setattr(
        sentinel,
        "_llm_generates",
        lambda *_args, **_kwargs: {"generates": True, "seconds": 0.2},
    )

    await watch_generation(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activity", "marks_healthy"),
    [
        ((True, False), False),
        ((False, True), True),
    ],
)
async def test_generation_watchdog_reuses_real_foreground_activity_without_a_competing_probe(
    storage,
    monkeypatch,
    activity: tuple[bool, bool],
    marks_healthy: bool,
) -> None:
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")
    observed_windows: list[float] = []
    probes = 0

    class _Router:
        def generation_watchdog_activity(self, *, recent_success_sec: float):
            observed_windows.append(recent_success_sec)
            return activity

    def forbidden_probe(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        raise AssertionError("a real foreground signal must suppress the synthetic probe")

    healthy_marks = 0

    def record_healthy(_ctx):
        nonlocal healthy_marks
        healthy_marks += 1

    monkeypatch.setattr(sentinel, "_llm_generates", forbidden_probe)
    monkeypatch.setattr(sentinel, "_mark_generation_healthy", record_healthy)
    await watch_generation(
        ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None, llm=_Router())
    )

    assert observed_windows == [_GENERATION_RECENT_FOREGROUND_SEC]
    assert probes == 0
    assert healthy_marks == int(marks_healthy)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_generation_watchdog_falls_back_to_probe_when_activity_observation_fails(
    storage,
    monkeypatch,
) -> None:
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")
    probes = 0

    class _BrokenRouter:
        def generation_watchdog_activity(self, *, recent_success_sec: float):
            del recent_success_sec
            raise RuntimeError("synthetic observation failure")

    def healthy_probe(*_args, **_kwargs):
        nonlocal probes
        probes += 1
        return {"generates": True, "seconds": 0.1}

    monkeypatch.setattr(sentinel, "_llm_generates", healthy_probe)
    await watch_generation(
        ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None, llm=_BrokenRouter())
    )

    assert probes == 1
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"sentinel_enabled": False, "sentinel_check_llm": True, "llm_enabled": True},
        {"sentinel_enabled": True, "sentinel_check_llm": False, "llm_enabled": True},
        {"sentinel_enabled": True, "sentinel_check_llm": True, "llm_enabled": False},
        {
            "sentinel_enabled": True,
            "sentinel_check_llm": True,
            "llm_enabled": True,
            "telegram_allowed_chat_ids": [],
            "telegram_owner_chat_ids": [],
        },
    ],
)
async def test_generation_watchdog_never_probes_when_it_cannot_alert(
    storage,
    monkeypatch,
    overrides: dict,
):
    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), **overrides)
    calls = 0

    def probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"generates": False, "seconds": 25.0}

    monkeypatch.setattr(sentinel, "_llm_generates", probe)

    await watch_generation(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    assert calls == 0
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_generation_watchdog_probe_does_not_freeze_the_event_loop(storage, monkeypatch):
    import asyncio
    import time

    import friday.organs.sentinel as sentinel

    settings = replace(_sentinel_settings(), sentinel_check_llm=True, llm_enabled=True)
    _seed_telegram_user(storage, "5001", preset_key="owner")

    def slow_healthy_probe(*_args, **_kwargs):
        time.sleep(0.4)
        return {"generates": True, "seconds": 0.4}

    monkeypatch.setattr(sentinel, "_llm_generates", slow_healthy_probe)
    worst = 0.0
    running = True

    async def heartbeat() -> None:
        nonlocal worst
        last = time.perf_counter()
        while running:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        await watch_generation(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))
    finally:
        running = False
        await beat

    assert worst < 0.2, f"the event loop stalled for {worst:.2f}s during the generation probe"


# --- who may be told, and what may be said --------------------------------


@pytest.mark.asyncio
async def test_a_guest_is_never_told_about_the_host(storage):
    """Health of this machine is privileged; the push obeys the same gate as the read.

    The scan fanned out to every *active* account, so anyone provisioned by
    writing once in an allowlisted group — a `guest` — received the worker state,
    the backup state and the secret-hygiene report of a machine that is not
    theirs. Reading the identical report over HTTP requires `admin.diagnostics`.
    """
    from friday.permissions import AuthorizationService

    settings = _sentinel_settings()
    owner = _seed_telegram_user(storage, "5001", preset_key="owner")
    guest = _seed_telegram_user(storage, "5002", preset_key="guest")
    _seed_degraded_worker(storage)

    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        auth=AuthorizationService(storage),
    )
    await scan_health(ctx)

    recipients = {n["user_id"] for n in storage.list_pending_notifications(limit=100)}
    assert owner in recipients
    assert guest not in recipients


def test_no_filesystem_path_is_transmitted():
    """The secret-hygiene detail names a secret AND where it lives. Only one may travel.

    `secret_exposed_in_file` formats as "<path> содержит значение <secret>", and
    `secret_file_permissions` suggests `chmod 600 <path>`. Telegram is off this
    machine; an on-disk location is exactly what must not go there. The alert
    still fires — the path stays behind `jericho doctor` on the host.
    """
    exposed = _format_alert(
        {
            "severity": "error",
            "title": "Секрет Friday лежит в постороннем файле",
            "detail": "/home/jericho/notes/todo.txt содержит значение FRIDAY_TELEGRAM_BOT_TOKEN. "
            "Удалите файл и перевыпустите этот секрет.",
        }
    )
    assert "/home/jericho/notes/todo.txt" not in exposed
    assert "FRIDAY_TELEGRAM_BOT_TOKEN" in exposed  # which secret is still useful
    assert "jericho doctor" in exposed

    perms = _format_alert(
        {
            "severity": "warning",
            "title": "Файл с секретами доступен другим пользователям",
            "detail": "/home/jericho/.jericho/.env.local имеет права 644. Ожидается 600.",
            "command": "chmod 600 /home/jericho/.jericho/.env.local",
        }
    )
    assert ".env.local" not in perms
    assert "chmod 600" in perms

    # A URL is not an on-disk location and must survive intact, or the
    # "vLLM unreachable" alert stops naming the endpoint it could not reach.
    endpoint = _format_alert(
        {"severity": "error", "title": "vLLM недоступен", "detail": "http://127.0.0.1:8001/v1 не отвечает"}
    )
    assert "http://127.0.0.1:8001/v1" in endpoint


@pytest.mark.asyncio
async def test_the_scan_does_not_freeze_the_event_loop(storage, monkeypatch):
    """`collect_diagnostics` is fully synchronous and was awaited as if it were not.

    It does a blocking `socket.create_connection`, a `urllib.request.urlopen`, a
    `PRAGMA integrity_check` over the whole database and a secret-hygiene scan of
    two directory trees. Called straight from the coroutine it froze the loop for
    the whole tick — including the `asyncio.timeout` meant to bound it, which
    cannot fire while the loop is not running.

    Measured as the WORST GAP between heartbeat ticks, with the heartbeat already
    running before the scan starts. Counting ticks over the whole window does not
    work: a scan that blocks first and returns leaves the rest of the window free,
    and the tally comes out the same either way.
    """
    import asyncio
    import time

    import friday.organs.sentinel as sentinel

    def slow_diagnostics(*_args, **_kwargs):
        time.sleep(0.5)
        return {"actions": []}

    monkeypatch.setattr(sentinel, "collect_diagnostics", slow_diagnostics)
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "5001")
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)

    worst = 0.0
    running = True

    async def heartbeat() -> None:
        nonlocal worst
        last = time.perf_counter()
        while running:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # the heartbeat is established BEFORE the scan starts
    try:
        await scan_health(ctx)
    finally:
        running = False
        await beat

    assert worst < 0.25, f"the event loop stalled for {worst:.2f}s during a 0.5s scan"


@pytest.mark.asyncio
async def test_diagnostics_reach_only_the_owner_chats(storage):
    """Мутация: убрать `_is_service_recipient` — тест краснеет.

    Заказ владельца 2 августа: «все служебные сообщения уходят только мне в
    телеграм, другим участникам их слать не надо».

    До этого адресатов выбирало право `admin.diagnostics`. Границей оно быть
    перестало: владелец же попросил заводить каждого написавшего с ПОЛНЫМ
    набором прав (`FRIDAY_NEW_ACCOUNT_PRESET=owner`), и рассылка молча
    расширилась бы на всех — вместе с состоянием воркеров, резервных копий и
    отчётом о гигиене секретов его машины.
    """
    settings = replace(
        _sentinel_settings(),
        telegram_allowed_chat_ids=[5001, 7002],
        telegram_owner_chat_ids=[5001],
    )
    _seed_telegram_user(storage, "5001", preset_key="owner")
    # Тот самый случай: посторонний с полными правами, заведённый автоматически.
    _seed_telegram_user(storage, "7002", preset_key="owner")
    _seed_degraded_worker(storage)

    await scan_health(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    pending = storage.list_pending_notifications(limit=100)
    assert pending, "владелец не получил диагностику"
    recipients = {str(item["chat_id"]) for item in pending}
    assert recipients == {"5001"}, f"служебное ушло посторонним: {sorted(recipients)}"


@pytest.mark.asyncio
async def test_without_owner_chats_the_capability_still_decides(storage):
    """Контроль: не задан список — прежнее правило.

    Молчать совсем хуже, чем сказать тому, кто и так всё видит через админку.
    """
    settings = replace(
        _sentinel_settings(),
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[],
    )
    _seed_telegram_user(storage, "5001", preset_key="owner")
    _seed_degraded_worker(storage)

    await scan_health(ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None))

    assert storage.list_pending_notifications(limit=100), "диагностика пропала совсем"
