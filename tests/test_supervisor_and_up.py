"""`jericho up` machinery: the generic supervisor + systemd unit generation.

The supervisor must restart a crashed child with backoff, give up on a
crash-looping child with a pointer to its log (an operator mistake becomes a
message, not an infinite restart), keep healthy siblings alive when one child
fails permanently, and terminate everything cleanly on stop.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import suppress

from friday.supervisor import ChildSpec, Supervisor


def _spec(tmp_path, name: str, code: str) -> ChildSpec:
    return ChildSpec(name=name, argv=[sys.executable, "-c", code], log_path=tmp_path / f"{name}.log")


def test_crash_looping_child_is_abandoned_with_log_pointer(tmp_path):
    sup = Supervisor(
        [_spec(tmp_path, "boom", "import sys; sys.exit(3)")],
        backoff_initial=0.05,
        crash_window_sec=5.0,
        max_rapid_crashes=3,
        poll_interval_sec=0.02,
    )
    code = sup.run()
    assert code == 1  # every child failed -> supervision ends with an error
    snapshot = sup.snapshot[0]
    assert snapshot["failed"] is True
    assert snapshot["restarts"] == 2  # third rapid crash abandons instead of restarting
    assert (tmp_path / "boom.log").exists()


def test_failed_child_does_not_take_down_the_healthy_one(tmp_path):
    sup = Supervisor(
        [
            _spec(tmp_path, "steady", "import time; time.sleep(60)"),
            _spec(tmp_path, "boom", "import sys; sys.exit(2)"),
        ],
        backoff_initial=0.05,
        crash_window_sec=5.0,
        max_rapid_crashes=2,
        poll_interval_sec=0.02,
    )
    runner = threading.Thread(target=sup.run)
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            states = {s["name"]: s for s in sup.snapshot}
            if states["boom"]["failed"] and states["steady"]["running"]:
                break
            time.sleep(0.05)
        states = {s["name"]: s for s in sup.snapshot}
        assert states["boom"]["failed"] is True
        assert states["steady"]["running"] is True  # survivor keeps running
    finally:
        sup.stop()
        runner.join(timeout=10)
    assert not runner.is_alive()


def test_child_output_lands_in_its_log(tmp_path):
    sup = Supervisor(
        [_spec(tmp_path, "talker", "print('привет из ребёнка'); import sys; sys.exit(0)")],
        backoff_initial=0.05,
        crash_window_sec=5.0,
        max_rapid_crashes=1,  # a single quick exit is enough to finish the test
        poll_interval_sec=0.02,
    )
    sup.run()
    assert "привет из ребёнка" in (tmp_path / "talker.log").read_text(encoding="utf-8")


# Writes one oversized blob, waits, then writes a marker. Deterministic on purpose:
# the supervisor is guaranteed to see the log over the cap during the sleep, so the
# marker lands *after* rotation without the test having to race a busy writer.
_BLOAT_THEN_MARK = (
    "import sys, time\n"
    "sys.stdout.write('X' * 8192 + '\\n'); sys.stdout.flush()\n"
    "time.sleep(1.5)\n"
    "sys.stdout.write('ПОСЛЕ ПОВОРОТА\\n'); sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # The log does not exist until the supervisor thread starts the child.
        with suppress(FileNotFoundError):
            if predicate():
                return True
        time.sleep(0.02)
    return False


def test_oversized_log_is_rotated_and_the_child_keeps_writing_into_the_live_file(tmp_path):
    """The wiring test: rotation must not orphan the child's inherited fd.

    A rename would leave the child appending to the rotated-away file and the live
    log permanently empty — the mechanism would look fine in isolation and the
    running system would silently stop logging. Asserting the post-rotation marker
    lands in the live file is what actually proves copy-truncate is wired up.
    """
    sup = Supervisor(
        [_spec(tmp_path, "flood", _BLOAT_THEN_MARK)],
        poll_interval_sec=0.02,
        log_max_bytes=4096,
        log_backups=2,
        log_check_interval_sec=0.0,
    )
    live = tmp_path / "flood.log"
    first = tmp_path / "flood.log.1"
    runner = threading.Thread(target=sup.run)
    runner.start()
    try:
        assert _wait_for(first.exists), "лог не провернулся"
        assert _wait_for(lambda: "ПОСЛЕ ПОВОРОТА" in live.read_text(encoding="utf-8")), (
            "ребёнок перестал писать в живой лог после поворота"
        )
        fresh = live.read_text(encoding="utf-8")
    finally:
        sup.stop()
        runner.join(timeout=15)
    assert first.read_text(encoding="utf-8").startswith("X" * 8192)
    # O_APPEND re-seeks to end on every write, so truncation leaves no sparse hole:
    # the live file is exactly the post-rotation output, not 8 KiB of NUL plus it.
    assert fresh == "ПОСЛЕ ПОВОРОТА\n"


def test_rotation_off_by_default_keeps_one_growing_file(tmp_path):
    sup = Supervisor(
        [_spec(tmp_path, "flood", _BLOAT_THEN_MARK)],
        poll_interval_sec=0.02,
        log_max_bytes=0,  # disabled
        log_check_interval_sec=0.0,
    )
    live = tmp_path / "flood.log"
    runner = threading.Thread(target=sup.run)
    runner.start()
    try:
        assert _wait_for(lambda: "ПОСЛЕ ПОВОРОТА" in live.read_text(encoding="utf-8"))
    finally:
        sup.stop()
        runner.join(timeout=15)
    assert not (tmp_path / "flood.log.1").exists()
    assert live.stat().st_size > 8192


def test_generations_shift_and_are_capped(tmp_path):
    sup = Supervisor(
        [_spec(tmp_path, "gen", "import sys; sys.exit(0)")],
        log_max_bytes=100,
        log_backups=2,
        log_check_interval_sec=0.0,
    )
    live = tmp_path / "gen.log"
    for marker in ("первый", "второй", "третий"):
        live.write_text(marker + "!" * 200, encoding="utf-8")
        sup._rotate_logs_if_needed()
        assert live.stat().st_size == 0
    assert (tmp_path / "gen.log.1").read_text(encoding="utf-8").startswith("третий")
    assert (tmp_path / "gen.log.2").read_text(encoding="utf-8").startswith("второй")
    assert not (tmp_path / "gen.log.3").exists()  # capped at log_backups


def test_undersized_log_is_left_alone(tmp_path):
    sup = Supervisor(
        [_spec(tmp_path, "small", "import sys; sys.exit(0)")],
        log_max_bytes=4096,
        log_check_interval_sec=0.0,
    )
    live = tmp_path / "small.log"
    live.write_text("коротко", encoding="utf-8")
    sup._rotate_logs_if_needed()
    assert live.read_text(encoding="utf-8") == "коротко"
    assert not (tmp_path / "small.log.1").exists()


def test_settings_bound_the_child_logs_by_default(settings):
    assert settings.log_max_bytes == 16 * 1024 * 1024
    assert settings.log_backups == 3


def test_telegram_backend_default_url_follows_native_tls(settings, monkeypatch):
    import dataclasses

    from friday.cli import _telegram_backend_url

    monkeypatch.delenv("FRIDAY_BACKEND_URL", raising=False)
    monkeypatch.delenv("JERICHO_BACKEND_URL", raising=False)
    exposed = dataclasses.replace(settings, api_host="0.0.0.0")
    assert _telegram_backend_url(exposed) == "http://127.0.0.1:8000"

    secure = dataclasses.replace(exposed, ssl_certfile="/public.crt", ssl_keyfile="/private.key")
    assert _telegram_backend_url(secure) == "https://127.0.0.1:8000"

    ipv6 = dataclasses.replace(secure, api_host="::")
    assert _telegram_backend_url(ipv6) == "https://[::1]:8000"
    assert ipv6.frontend_origin == "https://[::1]:8000"

    named = dataclasses.replace(secure, api_host="localhost")
    assert _telegram_backend_url(named) == "https://localhost:8000"
    assert named.frontend_origin == "https://localhost:8000"

    # An explicit URL remains authoritative for a reverse proxy or container DNS.
    monkeypatch.setenv("FRIDAY_BACKEND_URL", "https://backend.internal:9443/")
    assert _telegram_backend_url(secure) == "https://backend.internal:9443"


def test_install_services_writes_units(tmp_path, settings):
    from friday.cli import _install_services

    args = argparse.Namespace(dir=str(tmp_path / "units"))
    assert _install_services(args) == 0
    backend = (tmp_path / "units" / "jericho-backend.service").read_text(encoding="utf-8")
    bridge = (tmp_path / "units" / "jericho-bridge.service").read_text(encoding="utf-8")
    assert "ExecStart=" in backend and " server" in backend
    assert "Restart=on-failure" in backend
    assert f"Environment=FRIDAY_HOME={settings.home}" in backend
    assert " telegram-bridge" in bridge
    assert "After=network-online.target jericho-backend.service" in bridge
