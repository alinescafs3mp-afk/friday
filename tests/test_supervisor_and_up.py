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

from jericho.supervisor import ChildSpec, Supervisor


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


def test_install_services_writes_units(tmp_path, settings):
    from jericho.cli import _install_services

    args = argparse.Namespace(dir=str(tmp_path / "units"))
    assert _install_services(args) == 0
    backend = (tmp_path / "units" / "jericho-backend.service").read_text(encoding="utf-8")
    bridge = (tmp_path / "units" / "jericho-bridge.service").read_text(encoding="utf-8")
    assert "ExecStart=" in backend and " server" in backend
    assert "Restart=on-failure" in backend
    assert f"Environment=JERICHO_HOME={settings.home}" in backend
    assert " telegram-bridge" in bridge
    assert "After=network-online.target jericho-backend.service" in bridge
