"""Backups and exports are private before the first byte is written."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from friday.storage import _maintenance


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backup_has_no_umask_022_creation_window(settings, storage, monkeypatch):
    storage.ensure_user("alice")
    settings.backups_dir.chmod(0o755)
    original_connect = _maintenance.sqlite3.connect
    observed: list[tuple[int, int]] = []

    def observing_connect(database, *args, **kwargs):
        destination = Path(str(database))
        if destination.parent == settings.backups_dir:
            observed.append((_mode(destination.parent), _mode(destination)))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(_maintenance.sqlite3, "connect", observing_connect)
    previous_umask = os.umask(0o022)
    try:
        backup = storage.create_backup(label="private-window")
    finally:
        os.umask(previous_umask)

    database = Path(backup["path"])
    manifest = Path(backup["manifest_path"])
    assert observed == [(0o700, 0o600)]
    assert _mode(database) == 0o600
    assert _mode(manifest) == 0o600


def test_export_repairs_a_legacy_traversable_directory(settings, storage):
    storage.ensure_user("alice")
    settings.exports_dir.chmod(0o755)

    result = storage.export_user("alice")

    assert _mode(settings.exports_dir) == 0o700
    assert _mode(Path(result["path"])) == 0o600
