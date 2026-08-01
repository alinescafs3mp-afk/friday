"""Оригиналы файлов обязаны иметь резервную копию.

Бэкап базы уносит извлечённый текст, но PDF, сканы, фото и голосовые жили в
одном экземпляре на том же диске, что и база: смерть диска вернула бы базу из
зеркала, а оригиналы — ничем. Файлы content-addressed и неизменяемы, поэтому
инкремент — «докопируй то, чего ещё нет»; удаления не распространяются.
"""

from __future__ import annotations

from friday.backup_files import backup_files_incremental


def test_incremental_copy_is_idempotent_and_append_only(tmp_path):
    source = tmp_path / "files"
    target = tmp_path / "backups" / "files"
    (source / "u1").mkdir(parents=True)
    (source / "u1" / "a.bin").write_bytes(b"x" * 100)
    (source / "u1" / "b.bin").write_bytes(b"y" * 200)

    first = backup_files_incremental(source, target)
    assert (first["copied"], first["complete"]) == (2, True)
    assert (target / "u1" / "a.bin").read_bytes() == b"x" * 100

    replay = backup_files_incremental(source, target)
    assert replay["copied"] == 0 and replay["complete"] is True

    # Удаление источника НЕ трогает копию: бэкап, повторяющий удаление, — не бэкап.
    (source / "u1" / "a.bin").unlink()
    (source / "u1" / "c.bin").write_bytes(b"z" * 50)
    increment = backup_files_incremental(source, target)
    assert increment["copied"] == 1
    assert (target / "u1" / "a.bin").exists(), "бэкап повторил удаление источника"


def test_missing_files_dir_is_reported_not_raised(tmp_path):
    result = backup_files_incremental(tmp_path / "nope", tmp_path / "b")
    assert result["enabled"] is False and result["complete"] is True


def test_budget_exhaustion_reports_pending_and_next_run_finishes(tmp_path):
    source = tmp_path / "files"
    target = tmp_path / "b" / "files"
    (source / "u1").mkdir(parents=True)
    for index in range(5):
        (source / "u1" / f"f{index}.bin").write_bytes(bytes([index]) * 10)

    clipped = backup_files_incremental(source, target, budget_sec=0.0)
    assert clipped["complete"] is False
    assert clipped["pending"] == 5 - clipped["copied"]

    finished = backup_files_incremental(source, target)
    assert finished["complete"] is True
    assert finished["copied"] == clipped["pending"]


def test_the_scheduled_worker_actually_calls_the_file_backup(settings, storage):
    """Проводка, не механизм: воркер обязан звать копирование и класть отчёт в kv."""
    import json as jsonlib

    source = settings.files_dir
    (source / "u1").mkdir(parents=True, exist_ok=True)
    (source / "u1" / "doc.pdf").write_bytes(b"%PDF-fake")

    import asyncio

    from friday.workers import WorkersManager

    manager = WorkersManager(settings, storage, None, None)
    asyncio.run(manager._scheduled_backup())  # noqa: SLF001

    report = jsonlib.loads(storage.kv_get("workers:last_files_backup") or "{}")
    assert report.get("copied") == 1 and report.get("complete") is True
    assert (settings.backups_dir / "files" / "u1" / "doc.pdf").exists()


def test_diagnostics_warns_when_files_have_no_backup_yet(settings, storage):
    from friday.diagnostics import collect_diagnostics

    storage.ensure_user("alice")
    (settings.files_dir / "u1").mkdir(parents=True, exist_ok=True)
    (settings.files_dir / "u1" / "doc.pdf").write_bytes(b"%PDF-fake")

    result = collect_diagnostics(settings, storage=storage)
    codes = {str(item.get("code")) for item in result["actions"]}
    assert "files_backup_never_ran" in codes

    storage.kv_set(
        "workers:last_files_backup",
        '{"complete": true, "copied": 1, "total": 1, "pending": 0, "failed": 0}',
    )
    result = collect_diagnostics(settings, storage=storage)
    codes = {str(item.get("code")) for item in result["actions"]}
    assert "files_backup_never_ran" not in codes
    assert "files_backup_incomplete" not in codes
