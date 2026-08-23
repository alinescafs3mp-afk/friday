from __future__ import annotations

import copy
import hashlib
import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from friday.organs.obsidian.contracts import NoteDocument
from friday.organs.obsidian.operations import ObsidianOperationService
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.retrieval.archive_search_obsidian_reader import (
    ArchiveObsidianExactReadError,
    BoundArchiveObsidianExactFileReader,
    bind_archive_obsidian_exact_file_reader,
)
from friday.storage import FridayStorage

OWNER = "archive-reader-owner"
PRIVATE_BODY = "Секретный QNAP и Nextcloud\n"


def _service(
    storage: FridayStorage,
    tmp_path: Path,
    *,
    owner_id: str = OWNER,
) -> tuple[ObsidianOperationService, ObsidianService, str]:
    storage.ensure_user(owner_id)
    root = tmp_path / f"vault-{owner_id}"
    notes = ObsidianService(VaultStore(root))
    bundle = storage.create_obsidian_bundle(
        owner_id,
        config_root=str(tmp_path / f"config-{owner_id}"),
        database_root=str(tmp_path / f"database-{owner_id}"),
        api_endpoint=f"unix://{tmp_path}/run-{owner_id}.sock",
        api_key_ref=f"secret:obsidian:{owner_id}",
        server_path=str(root),
        folder_id=f"friday-{owner_id}",
        setup_token_hash=hashlib.sha256(f"token:{owner_id}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    service = ObsidianOperationService(storage, notes, owner_id=owner_id)
    return service, notes, str(bundle["vault"]["id"])


def _assert_body_free(error: BaseException) -> None:
    rendered = str(error) + repr(error)
    assert PRIVATE_BODY not in rendered
    assert "Infrastructure/QNAP.md" not in rendered


def test_bound_reader_returns_only_exact_canonical_utf8_bytes(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    written = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )

    assert reader(vault_id, written.path, written.revision) == PRIVATE_BODY.encode("utf-8")
    assert reader.attests_owner(OWNER) is True
    assert reader.attests_owner("foreign-owner") is False
    assert repr(reader) == "<BoundArchiveObsidianExactFileReader sealed private>"


def test_reader_rejects_cross_vault_revision_and_non_exact_path_body_free(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    written = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )

    attempts = (
        ("foreign-vault", written.path, written.revision),
        (vault_id, written.path, "0" * 64),
        (vault_id, "Infrastructure/QNAP", written.revision),
        (vault_id, "../Infrastructure/QNAP.md", written.revision),
    )
    for attempt in attempts:
        with pytest.raises(ArchiveObsidianExactReadError) as captured:
            reader(*attempt)
        _assert_body_free(captured.value)
        assert captured.value.__cause__ is None


def test_factory_requires_exact_trusted_owner_and_vault_binding(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, _notes, vault_id = _service(storage, tmp_path)

    for owner_id, candidate_vault in (("foreign", vault_id), (OWNER, "foreign-vault")):
        with pytest.raises(ArchiveObsidianExactReadError) as captured:
            bind_archive_obsidian_exact_file_reader(
                service,
                owner_id=owner_id,
                vault_id=candidate_vault,
            )
        _assert_body_free(captured.value)

    with pytest.raises(ArchiveObsidianExactReadError):
        BoundArchiveObsidianExactFileReader(
            service,
            owner_id=OWNER,
            vault_id=vault_id,
            _factory=object(),
        )


@pytest.mark.parametrize("corruption", ["path", "revision", "size", "bytes", "unicode"])
def test_reader_rechecks_every_note_document_integrity_field(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    written = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    document = service.read_note(written.path)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )
    if corruption == "path":
        corrupted = replace(document, path="Infrastructure/Other.md")
    elif corruption == "revision":
        corrupted = replace(document, revision="f" * 64)
    elif corruption == "size":
        corrupted = replace(document, size_bytes=document.size_bytes + 1)
    elif corruption == "bytes":
        corrupted = replace(document, content="different", size_bytes=len(b"different"))
    else:
        corrupted = replace(document, content="\ud800", size_bytes=1)

    def forged_read(_self: ObsidianOperationService, _path: object) -> NoteDocument:
        return corrupted

    monkeypatch.setattr(ObsidianOperationService, "read_note", forged_read)
    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, written.path, written.revision)
    _assert_body_free(captured.value)
    assert captured.value.__cause__ is None


def test_reader_reuses_canonical_service_owner_check_each_time(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    written = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )
    storage.execute("DELETE FROM obsidian_vaults WHERE user_id=?", (OWNER,))

    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, written.path, written.revision)
    _assert_body_free(captured.value)
    assert captured.value.__cause__ is None


def test_reader_is_copy_pickle_and_tamper_resistant(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    written = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )

    with pytest.raises(TypeError, match="process-private"):
        copy.copy(reader)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(reader)
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(reader)

    object.__setattr__(reader, "_vault_id", "foreign-vault")
    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, written.path, written.revision)
    _assert_body_free(captured.value)


def test_reader_rejects_cross_root_service_rewire(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    service, notes, vault_id = _service(storage, tmp_path)
    trusted = notes.create_note("Infrastructure/QNAP.md", PRIVATE_BODY)
    reader = bind_archive_obsidian_exact_file_reader(
        service,
        owner_id=OWNER,
        vault_id=vault_id,
    )
    foreign_notes = ObsidianService(VaultStore(tmp_path / "foreign-vault"))
    foreign = foreign_notes.create_note("Infrastructure/QNAP.md", "foreign bytes")

    class ForgedStorage:
        def get_obsidian_vault(self, _owner_id: str) -> dict[str, str]:
            return {
                "id": vault_id,
                "server_path": str(foreign_notes.store.root),
                "convention_json": "{}",
            }

    service._storage = ForgedStorage()  # type: ignore[assignment]  # noqa: SLF001
    service._notes = foreign_notes  # noqa: SLF001

    with pytest.raises(ArchiveObsidianExactReadError) as captured:
        reader(vault_id, foreign.path, foreign.revision)
    _assert_body_free(captured.value)
    assert notes.store.read(trusted.path).content == PRIVATE_BODY.encode("utf-8")
