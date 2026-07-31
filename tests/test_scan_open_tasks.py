"""Deterministic open-G scanner used by the task watcher must not miss work."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCANNER = Path(__file__).resolve().parents[1] / "grok" / "scan_open_tasks.py"
_SPEC = importlib.util.spec_from_file_location("scan_open_tasks", _SCANNER)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
parse_tasks = _MOD.parse_tasks


SAMPLE = """
# Grok tasks

## G1 (#43). First task — **сделано**

Body can say сделано without closing anything.

## G2 (#44). Still open, no marker

Work remains.

## G3 (#45). ASCII closed DONE

## G4 (#48). Closed Russian **закрыто**

## G10. No issue number yet
"""


def test_parse_open_vs_done_markers():
    tasks = {t["id"]: t for t in parse_tasks(SAMPLE)}
    assert tasks["G1"]["open"] is False
    assert tasks["G2"]["open"] is True
    assert tasks["G3"]["open"] is False
    assert tasks["G4"]["open"] is False
    assert tasks["G10"]["open"] is True
    assert tasks["G2"]["issue"] == 44
    assert tasks["G10"]["issue"] is None


def test_body_text_sdelano_does_not_close_open_heading():
    text = "## G7 (#54). Bug\n\nЭта задача **сделано** в прошлом — нет, маркер только в заголовке.\n"
    tasks = parse_tasks(text)
    assert len(tasks) == 1
    assert tasks[0]["open"] is True


def test_mutation_missing_done_marker_keeps_task_open():
    """If the done regex is weakened to always-match, open work disappears."""
    heading = "## G8 (#55). Sources path"
    assert parse_tasks(heading)[0]["open"] is True
    closed = "## G8 (#55). Sources path — **сделано**"
    assert parse_tasks(closed)[0]["open"] is False
