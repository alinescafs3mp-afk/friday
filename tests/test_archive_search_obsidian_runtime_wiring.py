from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel
from friday.organs.obsidian.runtime import ObsidianRuntime
from friday.organs.obsidian.vault_store import VaultStore
from friday.retrieval.archive_search_obsidian_reader import ArchiveObsidianExactReadError
from friday.server import create_app

OWNER = "archive-runtime-owner"
BODY = "Приватная заметка про QNAP и Nextcloud\n"
PATH = "Infrastructure/QNAP.md"


class _UnusedManager:
    def close(self) -> None:
        return None


def _ready_runtime(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> tuple[ObsidianRuntime, str, VaultStore, str]:
    owner = f"{OWNER}-{tmp_path.name}"
    storage.ensure_user(owner)
    root = tmp_path / "vault"
    bundle = storage.create_obsidian_bundle(
        owner,
        config_root=str(tmp_path / "config"),
        database_root=str(tmp_path / "database"),
        api_endpoint=f"unix://{tmp_path}/syncthing.sock",
        api_key_ref=f"secret:obsidian:{owner}",
        server_path=str(root),
        folder_id=f"friday-{owner}",
        setup_token_hash=hashlib.sha256(f"archive-runtime-token:{owner}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    storage.update_obsidian_vault(owner, state="ready")
    runtime = ObsidianRuntime(settings, storage, _UnusedManager())  # type: ignore[arg-type]
    return runtime, str(bundle["vault"]["id"]), VaultStore(root), owner


@pytest.mark.asyncio
async def test_runtime_factory_binds_exact_current_owner_vault_bytes(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    runtime, vault_id, store, owner = _ready_runtime(settings, storage, tmp_path)
    written = store.write_text(PATH, BODY, create_only=True)

    reader = await runtime.bind_archive_exact_file_reader(owner)

    assert reader(vault_id, written.path, written.revision) == BODY.encode("utf-8")


@pytest.mark.asyncio
async def test_runtime_bound_reader_fails_closed_on_vault_path_and_revision_drift(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    runtime, vault_id, store, owner = _ready_runtime(settings, storage, tmp_path)
    written = store.write_text(PATH, BODY, create_only=True)
    reader = await runtime.bind_archive_exact_file_reader(owner)

    attempts = (
        ("foreign-vault", written.path, written.revision),
        (vault_id, "Infrastructure/Other.md", written.revision),
        (vault_id, written.path, "0" * 64),
    )
    for attempt in attempts:
        with pytest.raises(ArchiveObsidianExactReadError) as captured:
            reader(*attempt)
        assert BODY not in str(captured.value) + repr(captured.value)

    revised_body = "Изменённое приватное содержимое\n"
    revised = store.write_text(PATH, revised_body, expected_revision=written.revision)
    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, written.path, written.revision)
    assert BODY not in str(captured.value) + repr(captured.value)
    assert revised_body not in str(captured.value) + repr(captured.value)

    store.move(PATH, "Infrastructure/Moved.md", expected_revision=revised.revision)
    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, written.path, revised.revision)
    assert revised_body not in str(captured.value) + repr(captured.value)

    storage.execute("DELETE FROM obsidian_vaults WHERE user_id=?", (owner,))
    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, "Infrastructure/Moved.md", revised.revision)
    assert BODY not in str(captured.value) + repr(captured.value)


@pytest.mark.asyncio
async def test_runtime_factory_has_no_reader_for_missing_or_cross_owner_vault(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    runtime, _vault_id, _store, _owner = _ready_runtime(settings, storage, tmp_path)
    storage.ensure_user("foreign-owner")

    for owner_id in ("missing-owner", "foreign-owner"):
        assert await runtime.bind_archive_exact_file_reader(owner_id) is None


def test_server_binds_reader_factory_only_when_obsidian_is_enabled(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings: list[tuple[ExecutionKernel, Any]] = []

    def record_binding(self: ExecutionKernel, factory: Any) -> None:
        bindings.append((self, factory))

    monkeypatch.setattr(
        ExecutionKernel,
        "bind_archive_obsidian_exact_file_reader_factory",
        record_binding,
        raising=False,
    )
    with TestClient(create_app(settings)):
        pass
    assert bindings == []

    enabled = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=settings.data_dir / "archive-reader-wiring",
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    with TestClient(create_app(enabled)) as client:
        assert len(bindings) == 1
        kernel, factory = bindings[0]
        runtime = client.app.state.obsidian_runtime
        assert kernel is client.app.state.kernel
        assert factory == runtime.bind_archive_exact_file_reader
