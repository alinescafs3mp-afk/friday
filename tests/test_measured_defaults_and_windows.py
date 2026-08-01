"""Four small places where a number meant one thing and was used as another.

* `jericho init` wrote `FRIDAY_DEDUP_THRESHOLD=0.92` — the value the default was
  RAISED from, because 0.92 sits inside the measured distribution of non-duplicates
  (two weekly meeting notes from one template scored 0.928). Every fresh install
  reproduced the false-merge behaviour the 0.95 default exists to prevent.
* `eval` published MRR over the whole result list while `limit=max(k, 20)`, so a
  metric printed beside `recall@k` and `precision@k` measured a window the caller
  never sees.
* The near-duplicate scan compared a count taken at the ADVANCED watermark against
  one recorded at the previous watermark, so any ordinary new object looked like a
  row that had appeared below the cursor.
* `inspect_process_lease` claimed the very anchor it was inspecting.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from friday.config import load_settings
from friday.dedup import _MEASURED_NON_DUPLICATE_CEILING
from friday.eval import reciprocal_rank

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _template_values(text: str) -> dict[str, str]:
    return dict(re.findall(r"^(FRIDAY_[A-Z0-9_]+)=(.*)$", text, re.M))


def _init_template() -> dict[str, str]:
    source = (ROOT / "friday" / "cli.py").read_text(encoding="utf-8")
    return _template_values(source)


def test_the_shipped_threshold_is_above_the_measured_ceiling():
    for name, template in (("jericho init", _init_template()),):
        raw = template.get("FRIDAY_DEDUP_THRESHOLD")
        assert raw, f"{name} no longer ships the setting"
        assert float(raw) > _MEASURED_NON_DUPLICATE_CEILING, (
            f"{name} ships {raw}, at or below the measured non-duplicate ceiling "
            f"{_MEASURED_NON_DUPLICATE_CEILING} — the false-merge region"
        )


def test_the_default_itself_is_above_it_too(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path))
    monkeypatch.delenv("FRIDAY_DEDUP_THRESHOLD", raising=False)
    assert load_settings().dedup_threshold > _MEASURED_NON_DUPLICATE_CEILING


@pytest.mark.parametrize("k", [1, 5, 10])
def test_mrr_is_measured_over_the_same_window_as_recall(k):
    """A hit outside k contributes nothing to a metric published as @k."""
    retrieved = [f"ko_{index}" for index in range(20)]
    expected = {"ko_15"}  # position 16, outside every k above
    assert reciprocal_rank(retrieved[:k], expected) == 0.0
    assert reciprocal_rank(retrieved, expected) > 0.0  # what it used to report


def test_eval_slices_before_scoring():
    source = (ROOT / "friday" / "eval.py").read_text(encoding="utf-8")
    assert "reciprocal_rank(retrieved[:k], expected)" in source


def test_the_lease_probe_never_binds():
    """Binding is how a lease is CLAIMED; an inspection that binds is not read-only."""
    source = (ROOT / "friday" / "diagnostics" / "runtime_lease.py").read_text(encoding="utf-8")
    inspect = source.split("def inspect_process_lease", 1)[1]
    assert "probe.bind(" not in inspect, "the inspection still takes the anchor it inspects"
    assert "probe.connect(" in inspect


def test_a_probe_never_takes_the_lease_it_inspects(tmp_path):
    """Sequential probes never showed this: each closed its socket before the next.

    The window is the ~26 microseconds between bind and close. Two overlapping probes
    read each other as an active lease with no live pid — but the sharper consequence
    is this one: a probe landing inside a starting process's acquire window took the
    anchor, and the backend or the Telegram bridge failed to start.
    """
    import sys
    import threading

    if not sys.platform.startswith("linux"):
        pytest.skip("the abstract-socket anchor is Linux-only")

    from friday.diagnostics.runtime_lease import (
        ProcessLease,
        RuntimeLeaseError,
        inspect_process_lease,
    )

    path = tmp_path / "runtime.lease"
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            inspect_process_lease(path, protocol="test")

    probes = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
    for thread in probes:
        thread.start()
    try:
        stolen = 0
        for _ in range(200):
            lease = ProcessLease(path, protocol="test")
            try:
                lease.acquire()
            except RuntimeLeaseError:
                stolen += 1
            else:
                lease.release()
    finally:
        stop.set()
        for thread in probes:
            thread.join(timeout=5)

    assert stolen == 0, f"a read-only inspection took the lease from {stolen} of 200 starts"
