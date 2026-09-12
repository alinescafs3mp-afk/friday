"""Real CLI series-conflict decisions and durable owner isolation in private SQLite."""

from __future__ import annotations

import fcntl
import hashlib
import sys
from datetime import UTC, datetime

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import FridayStorage
from friday.storage.models import KnowledgeObject, RawObject

OWNER = "series-cli-owner"
OTHER = "series-cli-other"
EVENT = "knowledge.series_conflicts_dismissed"
REASON = "Не копия, а соседний документ серии: в именах разные числа: ('12',) и ('13',)"


def _locked(settings, name):
    path = settings.state_dir / f"{name}.lock"
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


def _pair(storage, user, label, titles, texts):
    ids = []
    for side, title, text in zip(("a", "b"), titles, texts, strict=True):
        key = f"{user}-{label}-{side}"
        raw = RawObject(
            id="raw-" + key,
            user_id=user,
            source="test",
            source_ref=key,
            raw_content=text,
            content_type="text",
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id="ko-" + key,
                user_id=user,
                raw_object_id=raw.id,
                content=text,
                content_type="text",
                title=title,
            )
        )
        ids.append("ko-" + key)
    return str(
        storage.store_knowledge_conflict(
            user,
            *ids,
            conflict_type="near_duplicate",
            confidence=0.99,
            evidence={"fixture": label},
        )["id"]
    )


def _snapshot(settings):
    fresh = FridayStorage(settings)
    try:
        return {
            "tables": {
                table: [dict(row) for row in fresh.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
                for table in ("knowledge_conflicts", "knowledge_objects", "raw_objects")
            },
            "events": fresh.list_events(event_type=EVENT),
        }
    finally:
        fresh.close()


@pytest.fixture
def series_context(settings, storage, monkeypatch):
    for user in (OWNER, OTHER):
        storage.ensure_user(user, source="test", display_name=user)
    series = _pair(storage, OWNER, "series", ("12 день.docx", "13 день.docx"), ("Тема 4", "Тема 5"))
    _pair(storage, OWNER, "exact", ("12 день.docx", "13 день.docx"), ("Один текст", "Один  текст"))
    _pair(storage, OWNER, "version", ("Приказ.docx", "Приказ.docx"), ("До 1 июня", "До 15 июня"))
    closed = _pair(storage, OWNER, "human", ("12 день.docx", "13 день.docx"), ("Ранее 4", "Ранее 5"))
    storage.review_knowledge_conflict(
        OWNER, closed, "dismissed", reviewed_by="fixture-human", resolution_note="human decision"
    )
    _pair(storage, OTHER, "foreign", ("12 день.docx", "13 день.docx"), ("Чужое 4", "Чужое 5"))
    storage.close()
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    original = storage_module.init_storage
    opened = []

    def observed_init(current):
        assert current == settings
        assert _locked(settings, "account-deletion") and _locked(settings, "backend")
        opened.append(True)
        return original(current)

    monkeypatch.setattr(storage_module, "init_storage", observed_init)
    return settings, series, opened


def _run(monkeypatch, capsys, *, apply):
    capsys.readouterr()
    args = ["friday", "dismiss-series-conflicts", "--user", OWNER]
    monkeypatch.setattr(sys, "argv", args + (["--apply"] if apply else []))
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


@pytest.mark.parametrize("apply", [False, True], ids=["show", "apply"])
def test_series_cli_changes_only_selected_suggested_neighbour_and_reports_truthfully(
    series_context, monkeypatch, capsys, apply
):
    settings, selected, opened = series_context
    before = _snapshot(settings)
    started = datetime.now(UTC).replace(microsecond=0)
    code, output, error = _run(monkeypatch, capsys, apply=apply)
    ended = datetime.now(UTC)
    expected_output = "Конфликтов в очереди: 3.\nСоседи по серии (не копии): 1.\n    в именах разные числа: 1\nОстаётся человеку: 2.\n"
    if not apply:
        expected_output += "Это ПОКАЗ, очередь не тронута. Чтобы применить, повторите с --apply.\n"
    assert code == 0 and output == expected_output and error == ""
    assert opened == [True]
    after = _snapshot(settings)
    if not apply:
        assert after == before
    else:
        assert after["tables"]["knowledge_objects"] == before["tables"]["knowledge_objects"]
        assert after["tables"]["raw_objects"] == before["tables"]["raw_objects"]
        for old, new in zip(
            before["tables"]["knowledge_conflicts"], after["tables"]["knowledge_conflicts"], strict=True
        ):
            if old["id"] != selected:
                assert new == old
                continue
            assert new == {
                **old,
                "status": "dismissed",
                "reviewed_by": "series_rule",
                "resolution_note": REASON,
                "reviewed_at": new["reviewed_at"],
            }
            assert started <= datetime.fromisoformat(new["reviewed_at"]) <= ended
        assert len(after["events"]) == len(before["events"]) + 1
        assert after["events"][1:] == before["events"]
        assert after["events"][0]["payload"] == {"seen": 3, "dismissed": 1}
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")


def test_series_cli_repeated_apply_preserves_decisions_and_reports_remaining_queue(
    series_context, monkeypatch, capsys
):
    settings, _selected, opened = series_context
    assert _run(monkeypatch, capsys, apply=True)[0] == 0
    before = _snapshot(settings)
    code, output, error = _run(monkeypatch, capsys, apply=True)
    assert code == 0 and error == ""
    assert output == "Конфликтов в очереди: 2.\nСоседи по серии (не копии): 0.\nОстаётся человеку: 2.\n"
    after = _snapshot(settings)
    assert after["tables"] == before["tables"]
    assert after["events"][1:] == before["events"]
    assert after["events"][0]["payload"] == {"seen": 2, "dismissed": 0}
    assert opened == [True, True]


@pytest.mark.parametrize("busy", ["account-deletion", "backend"])
def test_series_cli_refuses_active_role_without_opening_storage_or_changing_data(
    series_context, monkeypatch, capsys, busy
):
    settings, _selected, opened = series_context
    before = _snapshot(settings)
    protocol = "friday.account-deletion.v1" if busy == "account-deletion" else "friday.backend.v1"
    with ProcessLease(settings.state_dir / f"{busy}.lock", protocol=protocol):
        code, output, error = _run(monkeypatch, capsys, apply=True)
        assert code == 2 and output == error == "" and opened == []
        assert _locked(settings, busy)
    assert _snapshot(settings) == before
