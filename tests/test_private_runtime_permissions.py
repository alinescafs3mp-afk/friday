"""Local private state never inherits a permissive operator umask."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

from friday.config import ensure_runtime_dirs
from friday.memory import MemoryVault
from friday.storage import init_storage
from friday.telegram_bridge import _UpdateInbox


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_runtime_vault_and_sqlite_paths_are_owner_only_under_umask_022(
    settings,
    tmp_path: Path,
) -> None:
    home = tmp_path / "privacy-home"
    state = home / "state"
    vault_dir = home / "memory-vault"
    secured = replace(
        settings,
        home=home,
        data_dir=home / "data",
        files_dir=home / "data" / "files",
        memory_vault_dir=vault_dir,
        cache_dir=home / "cache",
        log_dir=home / "logs",
        model_root=home / "models",
        model_dir=home / "models" / "active",
        state_dir=state,
        database_path=state / "friday.sqlite3",
        backups_dir=home / "backups",
        exports_dir=home / "exports",
    )
    runtime_dirs = [
        secured.home,
        secured.data_dir,
        secured.files_dir,
        secured.memory_vault_dir,
        secured.cache_dir,
        secured.log_dir,
        secured.model_root,
        secured.model_dir,
        secured.state_dir,
        secured.backups_dir,
        secured.exports_dir,
    ]

    previous_umask = os.umask(0o022)
    storage = None
    inbox = None
    try:
        # Simulate an installation created before the owner-only policy.  Startup
        # must repair existing modes as well as choose safe modes for new paths.
        for directory in runtime_dirs:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o755)
        secured.database_path.touch()
        secured.database_path.chmod(0o644)
        bridge_path = state / "telegram-inbox.sqlite3"
        bridge_path.touch()
        bridge_path.chmod(0o644)

        ensure_runtime_dirs(secured)
        storage = init_storage(secured)
        storage.ensure_user("alice")
        inbox = _UpdateInbox(str(bridge_path))

        vault = MemoryVault(vault_dir)
        note = vault.sync_object(
            {
                "id": "ko-private-mode",
                "user_id": "alice",
                "title": "Private note",
                "content": "private body",
            }
        )
        assert note is not None
        readme = next((vault_dir / "users").glob("*/README.md"))
        readme.chmod(0o644)
        vault.sync_object(
            {
                "id": "ko-private-mode",
                "user_id": "alice",
                "title": "Private note",
                "content": "private body",
            }
        )

        assert all(_mode(directory) == 0o700 for directory in runtime_dirs)
        assert _mode(vault_dir / "users") == 0o700
        assert _mode(readme.parent) == 0o700
        assert _mode(readme) == 0o600
        assert _mode(note) == 0o600

        for database in (secured.database_path, bridge_path):
            sidecars = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
            assert all(path.exists() for path in sidecars)
            assert all(_mode(path) == 0o600 for path in sidecars)
    finally:
        if inbox is not None:
            inbox.close()
        if storage is not None:
            storage.close()
        os.umask(previous_umask)
