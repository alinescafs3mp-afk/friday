"""Actual CLI archive import/purge on private SQLite, files and legacy vault notes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sys
from pathlib import Path

import pytest

from friday import cli, config
from friday import storage as storage_module
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.memory import MemoryVault, MemoryVaultDeletionHandle, VaultProjectionBoundaryError
from friday.storage import FridayStorage
from friday.storage.models import KnowledgeObject, RawObject

OWN, FOREIGN = "import-purge-owner", "import-purge-foreign"


def _locked(settings, role):
    path = settings.state_dir / f"{role}.lock"
    if not path.exists():
        return False
    with path.open("r+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
    return False


@pytest.fixture
def archive_context(settings, storage, monkeypatch):
    for user in (OWN, FOREIGN):
        storage.ensure_user(user, source="test", display_name=user)
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(config, "load_local_env_file", lambda: None)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    original, opened = storage_module.init_storage, []

    def observed(current):
        assert current == settings
        assert _locked(settings, "account-deletion") and _locked(settings, "backend")
        opened.append(True)
        return original(current)

    monkeypatch.setattr(storage_module, "init_storage", observed)
    return settings, storage, opened


def _run(settings, monkeypatch, capsys, command, *args):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["friday", command, *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert type(exited.value.code) is int
    captured = capsys.readouterr()
    assert not _locked(settings, "account-deletion") and not _locked(settings, "backend")
    return exited.value.code, captured.out, captured.err


def _file(storage, settings, user, key, *, payload=None, deleted=True):
    body = payload if payload is not None else (user + "-PRIVATE-BODY-" + key).encode()
    digest = hashlib.sha256(body).hexdigest()
    path = settings.files_dir / user / digest[:2] / (digest + ".bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    raw = RawObject(
        id="raw-" + user + key,
        user_id=user,
        source="upload",
        source_ref=key,
        raw_content="",
        content_type="file",
        content_hash=digest,
        metadata_json={"stored_path": str(path), "sha256": digest},
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id="ko-" + user + key,
        user_id=user,
        raw_object_id=raw.id,
        content="private archive " + key,
        content_type="file",
        title="private title " + key,
        summary="private summary " + key,
    )
    storage.store_knowledge_object(ko)
    storage.update_knowledge_fields(ko.id, user, summary="private updated " + key)
    note = MemoryVault(settings.memory_vault_dir).sync_object(storage.get_knowledge_object(ko.id, user))
    assert note is not None
    if deleted:
        assert storage.soft_delete_knowledge_object(ko.id, user)
    return ko.id, raw.id, path, note


def _view(settings, user):
    fresh = FridayStorage(settings)
    try:
        return {
            "raw": [
                dict(r)
                for r in fresh.execute("SELECT * FROM raw_objects WHERE user_id=? ORDER BY id", (user,))
            ],
            "knowledge": [
                dict(r)
                for r in fresh.execute("SELECT * FROM knowledge_objects WHERE user_id=? ORDER BY id", (user,))
            ],
            "versions": [
                dict(r)
                for r in fresh.execute(
                    "SELECT v.* FROM knowledge_object_versions v JOIN knowledge_objects k ON k.id=v.knowledge_object_id WHERE k.user_id=? ORDER BY v.rowid",
                    (user,),
                )
            ],
            "inbox": IngestionPipeline(settings, fresh, KnowledgeGraph(fresh), None).list_inbox(user),
            "audit": [
                dict(r)
                for r in fresh.execute("SELECT * FROM audit_log WHERE user_id=? ORDER BY rowid", (user,))
            ],
        }
    finally:
        fresh.close()


def _tree(settings, count):
    root = settings.home.parent / "external-corpus"
    root.mkdir()
    for index in range(count):
        (root / f"note-{index:02d}.md").write_text(
            f"Проект номер {index} стартует в марте. Уникальный текст {index}."
        )
    return root


def _bytes(root):
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("author", [None, FOREIGN])
def test_import_cli_review_gate_author_provenance_repeat_and_foreign_preservation(
    archive_context, monkeypatch, capsys, author
):
    settings, storage, opened = archive_context
    foreign = _file(storage, settings, FOREIGN, "foreign")
    root = _tree(settings, 2)
    storage.close()
    protected, original = _view(settings, FOREIGN), _bytes(root)
    args = [str(root), "--user", OWN, "--quiet"] + (["--uploaded-by", author] if author else [])
    code, output, error = _run(settings, monkeypatch, capsys, "import", *args)
    assert code == 0 and not error and "загружено 2" in output
    first = _view(settings, OWN)
    assert not first["knowledge"] and len(first["raw"]) == len(first["inbox"]) == 2
    assert {row["status"] for row in first["inbox"]} == {"pending"}
    metadata = [json.loads(row["metadata_json"]) for row in first["raw"]]
    assert {row["uploaded_by"] for row in metadata} == {author}
    assert {row["import_source_path"] for row in metadata} == {str(p.resolve()) for p in root.glob("*.md")}
    code, output, error = _run(settings, monkeypatch, capsys, "import", *args)
    assert code == 0 and not error and "загружено 0" in output and "уже было 2" in output
    assert _view(settings, OWN) == first and _view(settings, FOREIGN) == protected
    assert _bytes(root) == original and foreign[2].exists() and foreign[3].exists() and len(opened) == 2


@pytest.mark.parametrize("mode", ["dry", "missing"])
def test_import_cli_planning_and_missing_path_never_open_storage(archive_context, monkeypatch, capsys, mode):
    settings, storage, opened = archive_context
    root = _tree(settings, 1)
    (root / ".hidden.md").write_text("hidden")
    (root / "empty.md").write_text("")
    (root / "ignored.txt").write_text("suffix ignored")
    storage.close()
    before, original = _view(settings, OWN), _bytes(root)
    args = [str(root), "--dry-run", "--suffix", "md"] if mode == "dry" else [str(root / "missing")]
    code, output, error = _run(settings, monkeypatch, capsys, "import", *args)
    assert code == (0 if mode == "dry" else 2)
    if mode == "dry":
        assert "Найдено файлов: 1" in output and "--dry-run" in output and not error
    else:
        assert "Путь не найден" in error
    assert not opened and _view(settings, OWN) == before and _bytes(root) == original


@pytest.mark.parametrize("mode", ["ambiguous-owner", "unknown-author"])
def test_import_cli_refuses_ambiguous_owner_or_unknown_author(archive_context, monkeypatch, capsys, mode):
    settings, storage, opened = archive_context
    root = _tree(settings, 1)
    storage.close()
    before = {user: _view(settings, user) for user in (OWN, FOREIGN)}
    args = [str(root)] + (
        ["--user", OWN, "--uploaded-by", "missing-author"] if mode == "unknown-author" else []
    )
    code, _output, error = _run(settings, monkeypatch, capsys, "import", *args)
    assert code == 2 and (
        "--user" in error if mode == "ambiguous-owner" else "Аккаунт автора загрузки не найден" in error
    )
    assert opened and {user: _view(settings, user) for user in (OWN, FOREIGN)} == before


@pytest.mark.parametrize("prefix", [2, 20])
def test_import_cli_limit_resumes_after_existing_prefix(archive_context, monkeypatch, capsys, prefix):
    settings, storage, _opened = archive_context
    root = _tree(settings, prefix)
    storage.close()
    code, _output, error = _run(settings, monkeypatch, capsys, "import", str(root), "--user", OWN, "--quiet")
    assert code == 0 and not error and len(_view(settings, OWN)["inbox"]) == prefix
    (root / "zz-new.md").write_text("Новый документ должен попасть в следующую партию.")
    original = _bytes(root)
    code, output, error = _run(
        settings, monkeypatch, capsys, "import", str(root), "--user", OWN, "--limit", "1", "--quiet"
    )
    assert code == 0 and not error
    assert len(_view(settings, OWN)["inbox"]) == prefix + 1, output
    assert "загружено 1" in output and _bytes(root) == original


def test_import_cli_one_read_failure_reports_nonzero_and_keeps_other_files(
    archive_context, monkeypatch, capsys
):
    settings, storage, _opened = archive_context
    root = _tree(settings, 3)
    original, real_read = _bytes(root), Path.read_bytes
    storage.close()

    def failing_read(path):
        if path == root / "note-01.md":
            raise OSError("synthetic disk failure")
        return real_read(path)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "read_bytes", failing_read)
        code, output, error = _run(
            settings, monkeypatch, capsys, "import", str(root), "--user", OWN, "--quiet"
        )
    assert code == 1 and "загружено 2" in output and "ошибок 1" in output and "ОШИБКА" in error
    view = _view(settings, OWN)
    assert len(view["inbox"]) == 2 and not view["knowledge"] and _bytes(root) == original


@pytest.mark.parametrize("infer_owner", [False, True])
def test_purge_cli_removes_owned_file_vault_versions_and_audits_private_receipt(
    archive_context, monkeypatch, capsys, infer_owner
):
    settings, storage, opened = archive_context
    own, foreign = _file(storage, settings, OWN, "own"), _file(storage, settings, FOREIGN, "foreign")
    storage.close()
    protected, foreign_bytes = _view(settings, FOREIGN), (foreign[2].read_bytes(), foreign[3].read_bytes())
    args = ["--id", own[0], "--yes"] + ([] if infer_owner else ["--user", OWN])
    code, output, error = _run(settings, monkeypatch, capsys, "purge", *args)
    assert code == 0 and not error and opened
    receipt = json.loads(output[output.index("{") :])
    assert receipt["purged"] == 1 and len(receipt["items"]) == 1
    item = receipt["items"][0]
    assert item["knowledge_object_ref_sha256"] == hashlib.sha256(own[0].encode()).hexdigest()
    assert (
        item["raw_removed"]
        and item["file_unlinked"]
        and item["vault_removed"]
        and item["vault_removed_count"] == 1
    )
    assert all(secret not in output for secret in (own[0], own[1], str(own[2]), OWN, "private updated"))
    assert not own[2].exists() and not own[3].exists()
    view = _view(settings, OWN)
    assert not view["raw"] and not view["knowledge"] and not view["versions"]
    audit = [r for r in view["audit"] if r["action"] == "cli.knowledge.purge"]
    assert len(audit) == 1 and audit[0]["target_id"] != own[0]
    # The durable audit is privacy-projected independently of the public receipt.
    safe_audit = json.loads(audit[0]["after_json"])
    assert safe_audit["status"] == "purged"
    assert set(safe_audit) == {"status", "private_chars", "private_fields_count"}
    assert safe_audit["private_chars"] > 0 and safe_audit["private_fields_count"] > 0
    assert all(
        secret not in audit[0]["after_json"]
        for secret in (own[0], own[1], str(own[2]), item["knowledge_object_ref_sha256"], "private updated")
    )
    assert (
        _view(settings, FOREIGN) == protected
        and (foreign[2].read_bytes(), foreign[3].read_bytes()) == foreign_bytes
    )
    code, _output, _error = _run(settings, monkeypatch, capsys, "purge", *args)
    assert code == 2 and _view(settings, OWN) == view


@pytest.mark.parametrize("mode", ["no-confirm", "active", "wrong-owner", "missing"])
def test_purge_cli_refusals_preserve_archive(archive_context, monkeypatch, capsys, mode):
    settings, storage, opened = archive_context
    own = _file(storage, settings, OWN, "refusal", deleted=mode != "active")
    storage.close()
    before, files = _view(settings, OWN), (own[2].read_bytes(), own[3].read_bytes())
    args = [
        "--id",
        "nonexistent" if mode == "missing" else own[0],
        "--user",
        FOREIGN if mode == "wrong-owner" else OWN,
    ]
    if mode != "no-confirm":
        args.append("--yes")
    code, _output, error = _run(settings, monkeypatch, capsys, "purge", *args)
    assert code == 2 and error and _view(settings, OWN) == before
    assert (own[2].read_bytes(), own[3].read_bytes()) == files
    assert bool(opened) == (mode != "no-confirm")


def test_purge_cli_keeps_shared_file_until_last_reference(archive_context, monkeypatch, capsys):
    settings, storage, _opened = archive_context
    first = _file(storage, settings, OWN, "first", payload=b"shared raw bytes")
    second = _file(storage, settings, OWN, "second", payload=b"shared raw bytes")
    assert first[2] == second[2]
    storage.close()
    for item, final in [(first, False), (second, True)]:
        code, output, error = _run(
            settings, monkeypatch, capsys, "purge", "--id", item[0], "--user", OWN, "--yes"
        )
        assert code == 0 and not error and json.loads(output)["items"][0]["file_unlinked"] is final
        assert item[2].exists() is not final and not item[3].exists()


@pytest.mark.parametrize("fault", [False, True])
def test_purge_cli_batch_limit_retention_and_partial_commit_receipts(
    archive_context, monkeypatch, capsys, fault
):
    settings, storage, _opened = archive_context
    first, second = (
        _file(storage, settings, OWN, "batch-first"),
        _file(storage, settings, OWN, "batch-second"),
    )
    recent, foreign = _file(storage, settings, OWN, "recent"), _file(storage, settings, FOREIGN, "foreign")
    with storage.transaction() as connection:
        for row, date in [(first, "2000-01-01"), (second, "2001-01-01"), (foreign, "2000-01-01")]:
            connection.execute("UPDATE knowledge_objects SET deleted_at=? WHERE id=?", (date, row[0]))
    storage.close()
    protected = _view(settings, FOREIGN)
    delete = MemoryVaultDeletionHandle.delete_object

    def fail_second(self, ko_id, user_id):
        if ko_id == second[0]:
            raise VaultProjectionBoundaryError("synthetic second-item failure")
        return delete(self, ko_id, user_id)

    if fault:
        monkeypatch.setattr(MemoryVaultDeletionHandle, "delete_object", fail_second)
    args = ["--user", OWN, "--older-than-days", "30", "--limit", "10" if fault else "1", "--yes"]
    code, output, error = _run(settings, monkeypatch, capsys, "purge", *args)
    assert code == (2 if fault else 0)
    receipt = json.loads(output)
    assert receipt["purged"] == 1
    if fault:
        assert receipt == {
            "status": "partial_failure",
            "purged": 1,
            "audited": 1,
            "items": [{"knowledge_object_ref_sha256": hashlib.sha256(first[0].encode()).hexdigest()}],
            "failure_type": "VaultProjectionCleanupRequired",
        }
        assert error
    else:
        assert not error
    assert all(
        secret not in output + error
        for row in (first, second, recent, foreign)
        for secret in (row[0], row[1], str(row[2]), "private updated")
    )
    view = _view(settings, OWN)
    assert {r["id"] for r in view["knowledge"]} == {second[0], recent[0]}
    assert len([r for r in view["audit"] if r["action"] == "cli.knowledge.purge"]) == 1
    assert not first[2].exists() and not first[3].exists()
    assert all(p.exists() for row in (second, recent, foreign) for p in row[2:])
    assert _view(settings, FOREIGN) == protected
