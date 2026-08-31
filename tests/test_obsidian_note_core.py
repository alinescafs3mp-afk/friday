from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from friday.organs.obsidian.contracts import (
    IdempotencyConflictError,
    InvalidOperationIdError,
    InvalidPropertyError,
    NoteAlreadyExistsError,
    ObsidianVaultConvention,
    PropertyType,
    PropertyValue,
    RevisionConflictError,
    VaultLimitError,
    VaultPathError,
)
from friday.organs.obsidian.operation_receipts import NoteOperationReceiptStore
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultLimits, VaultStore


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


@pytest.fixture
def service(vault: Path) -> ObsidianService:
    return ObsidianService(VaultStore(vault))


@pytest.mark.parametrize(
    "unsafe",
    ["../escape.md", "folder/../../escape.md", "/tmp/escape.md", r"C:\escape.md", r"folder\note.md"],
)
def test_note_paths_are_relative_posix_paths(service: ObsidianService, unsafe: str) -> None:
    with pytest.raises(VaultPathError):
        service.create_note(unsafe, "secret")


def test_reads_writes_and_listing_never_follow_symlinks(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("outside", encoding="utf-8")
    (vault / "linked").symlink_to(outside, target_is_directory=True)
    (vault / "leaf.md").symlink_to(outside / "secret.md")
    service = ObsidianService(VaultStore(vault))

    with pytest.raises(VaultPathError):
        service.read_note("linked/secret.md")
    with pytest.raises(VaultPathError):
        service.create_note("linked/new.md", "must not escape")
    with pytest.raises(VaultPathError):
        service.read_note("leaf.md")

    assert service.list_notes() == ()
    assert not (outside / "new.md").exists()
    assert (outside / "secret.md").read_text(encoding="utf-8") == "outside"


def test_create_read_list_and_sha256_revision(service: ObsidianService, vault: Path) -> None:
    result = service.create_note("Projects/Friday", "# Friday\n\nRelease notes.\n")
    raw = (vault / "Projects" / "Friday.md").read_bytes()

    assert result.path == "Projects/Friday.md"
    assert result.revision == hashlib.sha256(raw).hexdigest()
    assert len(result.revision) == 64
    assert service.read_note("Projects/Friday").body == "# Friday\n\nRelease notes.\n"
    assert [note.path for note in service.list_notes()] == ["Projects/Friday.md"]
    assert not list(vault.rglob(".friday-*.tmp"))

    with pytest.raises(NoteAlreadyExistsError):
        service.create_note("Projects/Friday.md", "replacement")
    with pytest.raises(NoteAlreadyExistsError):
        service.create_note(
            "Projects/Friday.md",
            "# Friday\n\nRelease notes.\n",
            operation_id="ordinary-create-does-not-adopt",
        )
    with pytest.raises(NoteAlreadyExistsError):
        service.create_note(
            "Projects/Friday.md",
            "different content",
            operation_id="exact-adoption-rejects-different-content",
            adopt_existing_exact=True,
        )

    adopted = service.create_note(
        "Projects/Friday.md",
        "# Friday\n\nRelease notes.\n",
        operation_id="explicit-exact-adoption",
        adopt_existing_exact=True,
    )
    replay = service.create_note(
        "Projects/Friday.md",
        "# Friday\n\nRelease notes.\n",
        operation_id="explicit-exact-adoption",
        adopt_existing_exact=True,
    )
    assert adopted.created is False and adopted.applied is False
    assert adopted.previous_revision == result.revision
    assert replay == adopted


def test_template_listing_is_recursive_body_free_sorted_and_symlink_safe(
    vault: Path,
    tmp_path: Path,
) -> None:
    store = VaultStore(vault, limits=VaultLimits(max_list_results=2))
    store.write_text("Templates/zeta.md", "secret zeta", create_only=True)
    store.write_text(
        "Templates/Nested/Alpha.MD",
        "---\ntitle: Alpha template\n---\nsecret alpha",
        create_only=True,
    )
    store.write_text("Templates/beta.md", "secret beta", create_only=True)
    store.write_text("Notes/not-a-template.md", "outside template folder", create_only=True)
    store.write_text("Templates/ignored.sync-conflict-copy.md", "conflict", create_only=True)
    outside = tmp_path / "outside-templates"
    outside.mkdir()
    (outside / "escaped.md").write_text("outside secret", encoding="utf-8")
    (vault / "Templates" / "linked").symlink_to(outside, target_is_directory=True)
    (vault / "Templates" / "leaf.md").symlink_to(outside / "escaped.md")
    service = ObsidianService(store)

    templates = service.list_templates()

    assert [item.path for item in templates] == [
        "Templates/beta.md",
        "Templates/Nested/Alpha.MD",
    ]
    assert [item.name for item in templates] == ["beta", "Nested/Alpha"]
    assert [item.title for item in templates] == ["beta", "Alpha template"]
    assert all(len(item.revision) == 64 and item.modified_at.tzinfo is not None for item in templates)
    assert all(not hasattr(item, "content") and not hasattr(item, "body") for item in templates)


def test_template_listing_absent_folder_is_empty_and_unsafe_configuration_fails(
    vault: Path,
    tmp_path: Path,
) -> None:
    assert ObsidianService(VaultStore(vault)).list_templates() == ()

    escaped = ObsidianService(
        VaultStore(vault),
        convention=ObsidianVaultConvention(template_folder="../outside"),
    )
    with pytest.raises(VaultPathError):
        escaped.list_templates()

    outside = tmp_path / "linked-template-target"
    outside.mkdir()
    (outside / "secret.md").write_text("outside", encoding="utf-8")
    (vault / "LinkedTemplates").symlink_to(outside, target_is_directory=True)
    linked = ObsidianService(
        VaultStore(vault),
        convention=ObsidianVaultConvention(template_folder="LinkedTemplates"),
    )
    with pytest.raises(VaultPathError):
        linked.list_templates()


def test_atomic_replace_leaves_original_note_after_replace_failure(
    service: ObsidianService,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = service.create_note("atomic.md", "original")
    store = service.store

    def fail_exchange(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(store, "_rename_exchange", fail_exchange)
    with pytest.raises(OSError, match="atomic write failed"):
        service.append_note(
            "atomic.md",
            "new text",
            operation_id="atomic-op",
            expected_revision=created.revision,
        )

    assert (vault / "atomic.md").read_text(encoding="utf-8") == "original"
    assert not list(vault.glob(".friday-*.tmp"))


def test_revision_guard_preserves_a_peer_write_racing_validation_and_publish(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = VaultStore(vault)
    original = store.write_text("race.md", "server original", create_only=True)
    real_publish = store._atomic_replace  # noqa: SLF001

    def race_publish(*args: object, **kwargs: object) -> None:
        (vault / "race.md").write_text("android revision", encoding="utf-8")
        real_publish(*args, **kwargs)

    monkeypatch.setattr(store, "_atomic_replace", race_publish)

    with pytest.raises(RevisionConflictError):
        store.write_text(
            "race.md",
            "friday revision",
            expected_revision=original.revision,
        )

    assert (vault / "race.md").read_text(encoding="utf-8") == "android revision"


def test_revision_guard_preserves_both_writes_when_peer_recreates_during_publish(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = VaultStore(vault)
    original = store.write_text("race.md", "server original", create_only=True)
    real_exchange = store._rename_exchange  # noqa: SLF001
    injected = False

    def recreate_after_exchange(*args: object, **kwargs: object) -> None:
        nonlocal injected
        real_exchange(*args, **kwargs)  # type: ignore[arg-type]
        if not injected:
            injected = True
            peer = vault / ".peer-replacement"
            peer.write_text("android revision", encoding="utf-8")
            os.replace(peer, vault / "race.md")

    monkeypatch.setattr(store, "_rename_exchange", recreate_after_exchange)

    with pytest.raises(RevisionConflictError):
        store.write_text(
            "race.md",
            "friday revision",
            expected_revision=original.revision,
        )

    assert (vault / "race.md").read_text(encoding="utf-8") == "android revision"
    conflicts = store.list_sync_conflict_paths()
    assert {store.read(path).text() for path in conflicts} == {"server original", "friday revision"}


def test_abrupt_interruption_after_exchange_recovers_original_without_a_missing_path(vault: Path) -> None:
    store = VaultStore(vault)
    original = store.write_text("recovery.md", "server original", create_only=True)
    script = """
import os
import signal
import sys
from friday.organs.obsidian.vault_store import VaultStore

store = VaultStore(sys.argv[1])
real_exchange = store._rename_exchange
def interrupt_after_exchange(*args, **kwargs):
    real_exchange(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
store._rename_exchange = interrupt_after_exchange
store.write_text("recovery.md", "friday proposal", expected_revision=sys.argv[2])
"""
    interrupted = subprocess.run(  # noqa: S603 - fixed interpreter and local test script
        [sys.executable, "-c", script, str(vault), original.revision],
        check=False,
    )
    assert interrupted.returncode == -signal.SIGKILL
    assert (vault / "recovery.md").exists()

    recovered = VaultStore(vault)

    assert recovered.read("recovery.md").text() == "server original"
    assert {recovered.read(path).text() for path in recovered.list_sync_conflict_paths()} == {
        "friday proposal"
    }
    assert not list(vault.parent.glob(".friday-vault-*.txn*"))
    assert not list(vault.parent.glob(".friday-vault-*.swap"))
    assert not list(vault.parent.glob(".friday-vault-*.proposal"))


def test_open_writer_to_captured_inode_remains_canonical_and_friday_becomes_conflict(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = VaultStore(vault)
    original = store.write_text("open-fd.md", "server original", create_only=True)
    peer_fd = os.open(vault / "open-fd.md", os.O_WRONLY)
    real_exchange = store._rename_exchange  # noqa: SLF001
    exchanged = False

    def mutate_captured_inode(*args: object, **kwargs: object) -> None:
        nonlocal exchanged
        real_exchange(*args, **kwargs)  # type: ignore[arg-type]
        if not exchanged:
            exchanged = True
            os.ftruncate(peer_fd, 0)
            os.write(peer_fd, b"peer write through an already-open descriptor")
            os.fsync(peer_fd)

    monkeypatch.setattr(store, "_rename_exchange", mutate_captured_inode)
    try:
        with pytest.raises(RevisionConflictError):
            store.write_text("open-fd.md", "friday proposal", expected_revision=original.revision)
    finally:
        os.close(peer_fd)

    assert store.read("open-fd.md").text() == "peer write through an already-open descriptor"
    assert {store.read(path).text() for path in store.list_sync_conflict_paths()} == {"friday proposal"}


def test_late_open_request_cannot_interrupt_the_conditional_write_lease(vault: Path) -> None:
    (vault / "late-open.md").write_text("original", encoding="utf-8")
    script = """
import os
import sys
import time
from friday.organs.obsidian.vault_store import VaultStore

root = sys.argv[1]
path = os.path.join(root, "late-open.md")
store = VaultStore(root)
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(write_fd)
    os.read(read_fd, 1)
    descriptor = os.open(path, os.O_RDONLY)
    os.close(descriptor)
    os._exit(0)
os.close(read_fd)
leased = store._lease_and_read(directory_fd, "late-open.md")
assert leased is not None
os.write(write_fd, b"x")
os.close(write_fd)
time.sleep(0.1)
assert os.waitpid(child, os.WNOHANG) == (0, 0)
store._release_lease(leased)
waited, status = os.waitpid(child, 0)
assert waited == child and os.waitstatus_to_exitcode(status) == 0
os.close(directory_fd)
"""

    completed = subprocess.run(  # noqa: S603 - fixed interpreter and local test script
        [sys.executable, "-c", script, str(vault)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_file_to_directory_race_restores_peer_directory_and_preserves_proposal(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = VaultStore(vault)
    original = store.write_text("shape.md", "server original", create_only=True)
    real_exchange = store._rename_exchange  # noqa: SLF001
    injected = False

    def exchange_after_path_type_change(*args: object, **kwargs: object) -> None:
        nonlocal injected
        if not injected:
            injected = True
            (vault / "shape.md").unlink()
            (vault / "shape.md").mkdir()
            (vault / "shape.md" / "peer.txt").write_text("peer directory survives", encoding="utf-8")
        real_exchange(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_rename_exchange", exchange_after_path_type_change)

    with pytest.raises(RevisionConflictError):
        store.write_text("shape.md", "friday proposal", expected_revision=original.revision)

    assert (vault / "shape.md").is_dir()
    assert (vault / "shape.md" / "peer.txt").read_text(encoding="utf-8") == "peer directory survives"
    assert {store.read(path).text() for path in store.list_sync_conflict_paths()} == {"friday proposal"}


def test_append_is_durable_idempotent_and_revision_guarded(service: ObsidianService) -> None:
    created = service.create_note("Log", "first")
    appended = service.append_note(
        "Log",
        "second",
        operation_id="append-1",
        expected_revision=created.revision,
    )
    replay = service.append_note(
        "Log",
        "second",
        operation_id="append-1",
        expected_revision=created.revision,
    )

    assert appended.applied is True
    assert replay.applied is False
    assert replay.revision == appended.revision
    assert service.read_note("Log").body.count("second") == 1
    assert "<!-- friday:" not in service.read_note("Log").content

    with pytest.raises(IdempotencyConflictError):
        service.append_note("Log", "different", operation_id="append-1")
    with pytest.raises(RevisionConflictError) as conflict:
        service.append_note(
            "Log",
            "third",
            operation_id="append-2",
            expected_revision=created.revision,
        )
    assert conflict.value.actual_revision == appended.revision


def test_prepend_preserves_frontmatter_and_is_exactly_once(service: ObsidianService) -> None:
    created = service.create_note(
        "Projects/Friday",
        "---\nstatus: active\n---\n# Friday\n\nExisting.\n",
    )
    prepended = service.prepend_note(
        "Projects/Friday",
        "Context.",
        operation_id="prepend-1",
        expected_revision=created.revision,
    )
    replay = service.prepend_note(
        "Projects/Friday",
        "Context.",
        operation_id="prepend-1",
        expected_revision=created.revision,
    )

    document = service.read_note("Projects/Friday")
    assert document.content == "---\nstatus: active\n---\nContext.\n# Friday\n\nExisting.\n"
    assert document.properties["status"].value == "active"
    assert prepended.applied is True
    assert replay.applied is False
    assert replay.revision == prepended.revision
    assert document.body.count("Context.") == 1
    with pytest.raises(IdempotencyConflictError):
        service.prepend_note(
            "Projects/Friday",
            "Context.",
            operation_id="prepend-1",
            expected_revision=prepended.revision,
        )


@pytest.mark.parametrize(
    ("label", "newline", "content"),
    [
        ("lf", "\n", "---\nstatus: active\n---"),
        ("crlf", "\r\n", "---\r\nstatus: active\r\n---"),
    ],
)
def test_prepend_separates_frontmatter_closing_at_eof_with_its_native_newline(
    service: ObsidianService,
    label: str,
    newline: str,
    content: str,
) -> None:
    created = service.create_note("Frontmatter EOF", content)

    service.prepend_note(
        "Frontmatter EOF",
        "Context.",
        operation_id=f"prepend-eof-{label}",
        expected_revision=created.revision,
    )

    assert service.read_note("Frontmatter EOF").content == f"{content}{newline}Context.{newline}"


def test_full_replace_requires_cas_and_replaces_every_note_byte(
    service: ObsidianService,
) -> None:
    created = service.create_note("Replace", "---\nstatus: old\n---\nOld body.\n")
    replacement = "# New\n\nOnly the requested content.\n"
    replaced = service.replace_note(
        "Replace",
        replacement,
        operation_id="replace-1",
        expected_revision=created.revision,
    )
    replay = service.replace_note(
        "Replace",
        replacement,
        operation_id="replace-1",
        expected_revision=created.revision,
    )

    assert service.read_note("Replace").content == replacement
    assert service.read_note("Replace").properties == {}
    assert replaced.applied is True
    assert replay.applied is False
    assert replay.revision == replaced.revision
    with pytest.raises(IdempotencyConflictError):
        service.replace_note(
            "Replace",
            replacement,
            operation_id="replace-1",
            expected_revision=replaced.revision,
        )
    with pytest.raises(RevisionConflictError):
        service.replace_note(
            "Replace",
            "must not win",
            operation_id="replace-stale",
            expected_revision=created.revision,
        )
    assert service.read_note("Replace").content == replacement


def test_create_operation_id_is_idempotent_across_service_instances(vault: Path) -> None:
    first_service = ObsidianService(VaultStore(vault))
    created = first_service.create_note("once", "payload", operation_id="create-1")
    restarted_service = ObsidianService(VaultStore(vault))

    replay = restarted_service.create_note("once", "payload", operation_id="create-1")

    assert created.revision == replay.revision
    assert replay.applied is False
    assert "<!-- friday:" not in (vault / "once.md").read_text(encoding="utf-8")
    assert not any("receipt" in path.name for path in vault.rglob("*"))
    with pytest.raises(IdempotencyConflictError):
        restarted_service.create_note("once", "other payload", operation_id="create-1")


def test_direct_append_recovers_after_file_commit_before_external_receipt(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ObsidianService(VaultStore(vault))
    created = service.create_note("receipt-gap", "start")
    real_commit = NoteOperationReceiptStore.commit
    fail_once = True

    def lose_receipt(
        receipt_store: NoteOperationReceiptStore,
        operation_digest: str,
    ):  # noqa: ANN202
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic receipt interruption")
        return real_commit(receipt_store, operation_digest)

    monkeypatch.setattr(NoteOperationReceiptStore, "commit", lose_receipt)
    with pytest.raises(OSError, match="receipt interruption"):
        service.append_note(
            "receipt-gap",
            "once only",
            operation_id="direct-gap",
            expected_revision=created.revision,
        )

    restarted = ObsidianService(VaultStore(vault))
    replay = restarted.append_note(
        "receipt-gap",
        "once only",
        operation_id="direct-gap",
        expected_revision=created.revision,
    )

    content = restarted.read_note("receipt-gap").content
    assert replay.applied is False
    assert content.count("once only") == 1
    assert "<!-- friday:" not in content


def test_direct_service_recognizes_and_cleans_one_legacy_append_marker(vault: Path) -> None:
    service = ObsidianService(VaultStore(vault))
    base = service.create_note("legacy", "first")
    text = "second"
    operation_id = "legacy-append"
    operation_digest = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments_digest = hashlib.sha256(text.encode()).hexdigest()
    marker = f'<!-- friday:append operation="{operation_digest}" arguments="{arguments_digest}" -->'
    clean = service.render_append_content("first", text)
    service.store.write_text(
        "legacy.md",
        service.render_append_content(clean, marker),
        expected_revision=base.revision,
    )

    replay = service.append_note(
        "legacy",
        text,
        operation_id=operation_id,
        expected_revision=base.revision,
    )

    content = service.read_note("legacy").content
    assert replay.applied is False
    assert content == clean
    assert "<!-- friday:" not in content


def test_direct_create_replay_cleans_legacy_marker_after_later_append(vault: Path) -> None:
    service = ObsidianService(VaultStore(vault))
    operation_id = "legacy-create-with-later-append"
    operation = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"created first").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    service.store.write_text(
        "legacy-create.md",
        f"created first\n{marker}\nlater user text\n",
        create_only=True,
    )

    replay = service.create_note(
        "legacy-create",
        "created first",
        operation_id=operation_id,
    )

    assert replay.applied is False
    assert service.read_note("legacy-create").content == "created first\nlater user text\n"


def test_direct_legacy_create_receipt_never_resurrects_a_deleted_note(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ObsidianService(VaultStore(vault))
    operation_id = "legacy-create-deleted-during-gap"
    operation = hashlib.sha256(operation_id.encode()).hexdigest()
    arguments = hashlib.sha256(b"created first").hexdigest()
    marker = f'<!-- friday:create operation="{operation}" arguments="{arguments}" -->'
    legacy = service.store.write_text(
        "legacy-gap.md",
        f"created first\n{marker}\nlater user text\n",
        create_only=True,
    )
    write_text = service.store.write_text

    class SyntheticCrash(BaseException):
        pass

    def crash_before_cleanup(*_args: object, **_kwargs: object) -> object:
        raise SyntheticCrash

    monkeypatch.setattr(service.store, "write_text", crash_before_cleanup)
    with pytest.raises(SyntheticCrash):
        service.create_note("legacy-gap", "created first", operation_id=operation_id)
    monkeypatch.setattr(service.store, "write_text", write_text)
    service.store.delete("legacy-gap.md", expected_revision=legacy.revision)

    restarted = ObsidianService(VaultStore(vault))
    with pytest.raises(RevisionConflictError):
        restarted.create_note("legacy-gap", "created first", operation_id=operation_id)
    assert not restarted.store.exists("legacy-gap.md")


def test_receipt_store_is_private_and_rejects_symlink_targets(vault: Path, tmp_path: Path) -> None:
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir(mode=0o755)
    store = NoteOperationReceiptStore(receipt_root, vault_root=vault)
    store.prepare(
        operation_digest="1" * 64,
        method="append",
        arguments_digest="2" * 64,
        note_path="Note.md",
        base_revision="3" * 64,
        target_revision="4" * 64,
        created=False,
    )

    assert receipt_root.stat().st_mode & 0o777 == 0o700
    assert (receipt_root / "receipts.sqlite3").stat().st_mode & 0o777 == 0o600

    linked_root = tmp_path / "linked-receipts"
    linked_root.symlink_to(receipt_root, target_is_directory=True)
    with pytest.raises(VaultPathError, match="symbolic link"):
        NoteOperationReceiptStore(linked_root, vault_root=vault)

    bad_root = tmp_path / "bad-receipts"
    bad_root.mkdir()
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"")
    (bad_root / "receipts.sqlite3").symlink_to(outside)
    with pytest.raises(VaultPathError, match="regular file"):
        NoteOperationReceiptStore(bad_root, vault_root=vault)


def test_default_receipts_do_not_cross_replaced_vault_identity(vault: Path, tmp_path: Path) -> None:
    first = ObsidianService(VaultStore(vault))
    first.create_note("identity", "payload", operation_id="same-operation")
    vault.rename(tmp_path / "retired-vault")
    vault.mkdir()

    replacement = ObsidianService(VaultStore(vault))
    result = replacement.create_note("identity", "payload", operation_id="same-operation")

    assert result.applied is True
    assert replacement.read_note("identity").content == "payload"


def test_typed_properties_preserve_unknown_frontmatter_and_body(
    service: ObsidianService,
) -> None:
    original_body = "# Body\n\nUser-authored text stays byte-for-byte.\n"
    original = f"---\nplugin_config:\n  nested: true\nstatus: old\n---\n{original_body}"
    created = service.create_note("Properties", original)
    changed = service.set_properties(
        "Properties",
        {
            "status": PropertyValue(PropertyType.TEXT, "review"),
            "tags": ["friday", "release"],
            "aliases": [],
            "weight": 2.5,
            "done": False,
            "due": date(2026, 8, 22),
            "checked_at": datetime(2026, 8, 21, 12, 30, tzinfo=UTC),
        },
        expected_revision=created.revision,
    )
    note = service.read_note("Properties")

    assert note.body == original_body
    assert "plugin_config:\n  nested: true\n" in note.content
    assert note.properties["status"] == PropertyValue(PropertyType.TEXT, "review")
    assert note.properties["tags"].value == ("friday", "release")
    assert note.properties["aliases"] == PropertyValue(PropertyType.LIST, ())
    assert note.properties["weight"].type is PropertyType.NUMBER
    assert note.properties["done"].type is PropertyType.CHECKBOX
    assert note.properties["due"].type is PropertyType.DATE
    assert note.properties["checked_at"].type is PropertyType.DATETIME

    with pytest.raises(RevisionConflictError):
        service.set_properties("Properties", {"status": "stale"}, expected_revision=created.revision)
    assert service.read_note("Properties").revision == changed.revision


def test_search_has_exact_and_lexical_lanes_without_operation_markers(
    service: ObsidianService,
) -> None:
    service.create_note("Architecture", "Индекс документов и поиск.")
    service.create_note("Other", "Ничего похожего.")
    service.append_note("Architecture", "Надёжный индекс.", operation_id="search-op")

    exact = service.search_notes("Architecture")
    lexical = service.search_notes("надёжный индекс")

    assert exact[0].path == "Architecture.md"
    assert "exact_title" in exact[0].match_channels
    assert lexical[0].path == "Architecture.md"
    assert "friday:append" not in lexical[0].excerpt


def test_search_finds_the_battery_paraphrase_and_uses_the_created_date(
    service: ObsidianService,
) -> None:
    service.create_note(
        "Projects/Retrieval Problem",
        (
            "Старые документы иногда исчезали из семантической выдачи, потому что "
            "набор кандидатов ограничивался сравнительно свежими объектами."
        ),
        properties={"created": date(2026, 8, 4)},
    )
    service.create_note(
        "Projects/Lexical Noise",
        "Поиск файлов в начале августа. Поиск, список, файлы, список, поиск.",
        properties={"created": date(2026, 7, 18)},
    )

    paraphrase = service.search_notes(
        "старые файлы не попадали в поиск из-за слишком маленького списка кандидатов"
    )
    dated = service.search_notes("проблемы поиска, которую я делал примерно в начале августа 2026 года")

    assert paraphrase[0].path == "Projects/Retrieval Problem.md"
    assert "semantic" in paraphrase[0].match_channels
    assert dated[0].path == "Projects/Retrieval Problem.md"
    assert "property_date_created" in dated[0].match_channels


def test_search_accepts_android_text_created_date_with_safe_separator(
    service: ObsidianService,
) -> None:
    service.create_note(
        "Projects/Retrival Problem",
        "Старые документы исчезали из поиска из-за списка кандидатов.",
        properties={"created": "2026_08_04"},
    )
    service.create_note(
        "Projects/Noise",
        "Проблемы поиска и список кандидатов.",
        properties={"created": "2026_07_18"},
    )

    dated = service.search_notes("проблемы поиска в начале августа 2026 года")

    assert dated[0].path == "Projects/Retrival Problem.md"
    assert "property_date_created" in dated[0].match_channels


def test_daily_note_uses_convention_and_appends_once(vault: Path) -> None:
    convention = ObsidianVaultConvention(daily_folder="Journal", daily_format="YYYY_MM_DD")
    service = ObsidianService(
        VaultStore(vault),
        clock=lambda: date(2026, 8, 21),
        convention=convention,
    )

    created = service.daily_note(content="Итог дня", operation_id="daily-1")
    replay = service.daily_note(content="Итог дня", operation_id="daily-1")

    assert created.path == "Journal/2026_08_21.md"
    assert created.created is True
    assert replay.applied is False
    assert replay.revision == created.revision
    assert service.read_note(created.path).body.count("Итог дня") == 1

    with pytest.raises(InvalidOperationIdError):
        service.daily_note(content="Ещё строка")


def test_acceptance_daily_section_reuses_heading_and_replays_byte_identically(vault: Path) -> None:
    service = ObsidianService(
        VaultStore(vault),
        clock=lambda: date(2026, 8, 22),
        convention=ObsidianVaultConvention(daily_folder="Daily", daily_format="YYYY-MM-DD"),
    )

    first = service.daily_note(
        section="Friday",
        item="- Проверена интеграция с Obsidian",
        operation_id="battery-daily",
    )
    first_bytes = (vault / first.path).read_bytes()
    replay = service.daily_note(
        section="Friday",
        item="- Проверена интеграция с Obsidian",
        operation_id="battery-daily",
    )

    assert replay.applied is False
    assert replay.revision == first.revision
    assert (vault / first.path).read_bytes() == first_bytes
    body = service.read_note(first.path).body
    assert body.count("## Friday") == 1
    assert body.count("- Проверена интеграция с Obsidian") == 1


def test_local_write_result_does_not_invent_remote_delivery(service: ObsidianService) -> None:
    result = service.create_note("Offline", "saved locally")

    assert result.local_write_complete is True
    assert result.server_scan_complete is False
    assert result.android_connected is False
    assert result.android_completion is None
    assert result.android_received is False
    assert result.obsidian_opened is False


def test_conflict_copies_and_obsidian_configuration_are_not_ordinary_notes(vault: Path) -> None:
    (vault / "normal.md").write_text("normal", encoding="utf-8")
    (vault / "normal.sync-conflict-20260821.md").write_text("conflict", encoding="utf-8")
    configuration = vault / ".obsidian"
    configuration.mkdir()
    (configuration / "plugin.md").write_text("configuration", encoding="utf-8")

    store = VaultStore(vault)
    notes = ObsidianService(store).list_notes()

    assert [note.path for note in notes] == ["normal.md"]
    assert store.list_sync_conflict_paths() == ("normal.sync-conflict-20260821.md",)


def test_store_rejects_a_symlink_swapped_into_the_destination(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    destination = vault / "note.md"
    destination.symlink_to(outside)
    service = ObsidianService(VaultStore(vault))

    with pytest.raises(VaultPathError):
        service.create_note("note.md", "inside")

    assert os.path.islink(destination)
    assert outside.read_text(encoding="utf-8") == "outside"


def test_oversized_notes_are_rejected_before_leaf_read_and_before_write(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = VaultStore(vault, limits=VaultLimits(max_note_bytes=16))
    oversized = vault / "oversized.md"
    oversized.write_bytes(b"x" * 17)
    opened_leaves: list[object] = []
    real_open = os.open

    def tracked_open(path: object, *args: object, **kwargs: object) -> int:
        if path == "oversized.md":
            opened_leaves.append(path)
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("friday.organs.obsidian.vault_store.os.open", tracked_open)

    with pytest.raises(VaultLimitError, match="maximum size of 16 bytes"):
        store.read("oversized.md")
    with pytest.raises(VaultLimitError, match="maximum size of 16 bytes"):
        store.write_text("new.md", "💣" * 5)

    assert opened_leaves == []
    assert not (vault / "new.md").exists()


def test_oversized_properties_are_rejected_before_render(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ObsidianService(VaultStore(vault, limits=VaultLimits(max_note_bytes=64)))

    def unexpected_render(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("oversized properties reached the renderer")

    monkeypatch.setattr("friday.organs.obsidian.service.set_frontmatter_properties", unexpected_render)

    with pytest.raises(VaultLimitError, match="pre-render budget"):
        service.create_note("properties.md", properties={"status": "x" * 20})

    assert not (vault / "properties.md").exists()


def test_cyclic_typed_property_is_rejected_without_recursive_budget_walk(
    service: ObsidianService,
) -> None:
    cyclic: dict[str, object] = {"type": "text"}
    cyclic["value"] = cyclic

    with pytest.raises(InvalidPropertyError):
        service.create_note("cyclic.md", properties={"bad": cyclic})


def test_vault_traversal_rejects_excessive_depth(vault: Path) -> None:
    deep = vault / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "note.md").write_text("deep", encoding="utf-8")
    service = ObsidianService(VaultStore(vault, limits=VaultLimits(max_depth=2)))

    with pytest.raises(VaultLimitError, match="maximum depth of 2 directories"):
        service.list_notes()
    with pytest.raises(VaultLimitError, match="maximum depth of 2 directories"):
        service.read_note("one/two/three/note.md")


def test_wide_vault_stops_at_the_entry_budget(vault: Path) -> None:
    for index in range(4):
        (vault / f"entry-{index}.txt").write_text("ignored", encoding="utf-8")
    service = ObsidianService(VaultStore(vault, limits=VaultLimits(max_entries=3)))

    with pytest.raises(VaultLimitError, match="maximum entry count of 3"):
        service.list_notes()


def test_vault_stops_at_the_markdown_path_budget(vault: Path) -> None:
    for index in range(3):
        (vault / f"note-{index}.md").write_text("note", encoding="utf-8")
    limits = VaultLimits(
        max_markdown_paths=2,
        max_list_results=2,
        max_search_results=2,
    )
    service = ObsidianService(VaultStore(vault, limits=limits))

    with pytest.raises(VaultLimitError, match="maximum Markdown path count of 2"):
        service.search_notes("note", limit=2)


@pytest.mark.parametrize("operation", ["list", "search"])
def test_list_and_search_preflight_the_aggregate_byte_budget(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    (vault / "one.md").write_bytes(b"needle1")
    (vault / "two.md").write_bytes(b"needle2")
    store = VaultStore(
        vault,
        limits=VaultLimits(max_note_bytes=8, max_total_markdown_bytes=12),
    )
    service = ObsidianService(store)
    reads = 0
    real_read = store.read

    def tracked_read(path: str | Path):
        nonlocal reads
        reads += 1
        return real_read(path)

    monkeypatch.setattr(store, "read", tracked_read)

    with pytest.raises(VaultLimitError, match="aggregate read budget of 12 bytes"):
        if operation == "list":
            service.list_notes()
        else:
            service.search_notes("needle")

    assert reads == 0


def test_list_result_cap_fails_closed_before_reading(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        (vault / f"note-{index}.md").write_text("note", encoding="utf-8")
    store = VaultStore(vault, limits=VaultLimits(max_list_results=2))
    service = ObsidianService(store)
    reads = 0
    real_read = store.read

    def tracked_read(path: str | Path):
        nonlocal reads
        reads += 1
        return real_read(path)

    monkeypatch.setattr(store, "read", tracked_read)

    with pytest.raises(VaultLimitError, match="maximum result count of 2"):
        service.list_notes()

    assert reads == 0


def test_search_caps_results_and_reads_each_note_only_once(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(4):
        (vault / f"note-{index}.md").write_text(f"needle {index}", encoding="utf-8")
    store = VaultStore(vault, limits=VaultLimits(max_search_results=2))
    service = ObsidianService(store)
    reads: list[str] = []
    real_read = store.read

    def tracked_read(path: str | Path):
        reads.append(str(path))
        return real_read(path)

    monkeypatch.setattr(store, "read", tracked_read)

    results = service.search_notes("needle", limit=2)

    assert len(results) == 2
    assert sorted(reads) == [f"note-{index}.md" for index in range(4)]
    with pytest.raises(ValueError, match="between 1 and 2"):
        service.search_notes("needle", limit=3)


def test_search_rejects_an_unbounded_number_of_distinct_terms(service: ObsidianService) -> None:
    query = " ".join(f"term{index}" for index in range(33))

    with pytest.raises(ValueError, match="at most 32 distinct terms"):
        service.search_notes(query)


def test_syncthing_internal_roots_never_surface_archived_notes(vault: Path) -> None:
    (vault / "current.md").write_text("current", encoding="utf-8")
    (vault / "current.sync-conflict-20260821.md").write_text("conflict", encoding="utf-8")
    for internal_root in (".stversions", ".stfolder", ".stignore", ".trash"):
        hidden = vault / internal_root
        hidden.mkdir()
        (hidden / "deleted.md").write_text("deleted", encoding="utf-8")
        (hidden / "deleted.sync-conflict-20260821.md").write_text("old conflict", encoding="utf-8")
    store = VaultStore(vault)
    service = ObsidianService(store)

    assert [note.path for note in service.list_notes()] == ["current.md"]
    assert store.list_sync_conflict_paths() == ("current.sync-conflict-20260821.md",)
    for internal_path in (
        ".stversions/deleted.md",
        ".stfolder/deleted.md",
        ".stignore/deleted.md",
        ".trash/deleted.md",
        "current.sync-conflict-20260821.md",
    ):
        with pytest.raises(VaultPathError, match="internal|conflict"):
            service.read_note(internal_path)
    with pytest.raises(VaultPathError, match="reserved internal root"):
        store.read(".stversions/deleted.md")
    with pytest.raises(VaultPathError, match="reserved internal root"):
        store.write_text(".stversions/new.md", "must not enter history")
    with pytest.raises(VaultPathError, match="internal"):
        service.create_note(".stversions/new.md", "must not enter history")
