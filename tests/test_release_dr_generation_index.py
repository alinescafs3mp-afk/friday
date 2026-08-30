from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from tools import release_dr_generation_index as dr_index


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _candidate(
    root: Path,
    ordinal: int,
    *,
    source_kind: str = "terminal_activation",
) -> dict[str, Any]:
    backup = _private_directory(root / f"backup-{ordinal}")
    release = _private_directory(root / f"release-{ordinal}")
    digest = lambda offset: f"{ordinal * 16 + offset:064x}"  # noqa: E731
    return {
        "backup_directory": str(backup),
        "backup_record_sha256": digest(1),
        "database_receipt_sha256": digest(2),
        "engineer_receipt_sha256": digest(3),
        "inbox_receipt_sha256": digest(4),
        "obsidian_receipt_sha256": digest(5),
        "restore_release": {
            "commit": f"{ordinal:040x}",
            "max_schema": 50,
            "root": str(release),
            "tree_manifest_sha256": digest(6),
            "version": f"0.207.{ordinal}",
            "wheel_sha256": digest(7),
        },
        "schema": dr_index.GENERATION_CANDIDATE_SCHEMA,
        "source_kind": source_kind,
        "source_receipt_sha256": digest(8),
        "source_transaction_id": digest(9),
    }


def _external_receipt(label: str, ordinal: int) -> dict[str, str]:
    return {
        "schema": f"friday.test-{label}.v1",
        "sha256": f"{ordinal:064x}",
    }


def _index(tmp_path: Path) -> dr_index.DurableDRGenerationIndex:
    state = _private_directory(tmp_path / "state")
    index = dr_index.DurableDRGenerationIndex(state)
    index.initialize()
    return index


def _advance(
    index: dr_index.DurableDRGenerationIndex,
    candidate: dict[str, Any],
    *,
    intent: str,
    ordinal: int,
) -> dict[str, Any]:
    state = index.load()
    state = index.prepare(
        intent=intent,
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_authenticated(
        receipt=_external_receipt("authentication", ordinal),
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", ordinal + 1000),
        expected_journal_sha256=state["journal_sha256"],
    )
    return index.publish(expected_journal_sha256=state["journal_sha256"])


def test_bootstrap_publishes_exact_immutable_receipt_then_clear_cas(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    candidate = _candidate(tmp_path, 1)

    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    assert prepared["phase"] == "prepared"
    assert prepared["base_clear_sha256"] == initial["journal_sha256"]
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 101),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 102),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    reference = rehearsed["pending"]["generation"]
    receipt_path = index.receipt_directory / f"{reference['generation_id']}.json"
    assert not receipt_path.exists()

    published = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    assert published["phase"] == "clear"
    assert published["revision"] == 4
    assert published["base_clear_sha256"] == initial["journal_sha256"]
    assert published["current"] == reference
    assert published["older"] is None
    assert published["pending"] is None
    receipt = json.loads(receipt_path.read_text(encoding="ascii"))
    assert reference["generation_id"] == hashlib.sha256(_canonical(receipt["generation"])).hexdigest()
    receipt_core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    assert reference["receipt_sha256"] == hashlib.sha256(_canonical(receipt_core)).hexdigest()
    status = receipt_path.stat()
    assert stat.S_IMODE(status.st_mode) == 0o400
    assert status.st_nlink == 1


def test_explicit_older_and_rotation_use_only_exact_inputs_not_mtime_or_globs(tmp_path: Path) -> None:
    index = _index(tmp_path)
    current = _candidate(tmp_path, 2)
    adopted_older = _candidate(tmp_path, 3, source_kind="explicit_older_adoption")
    next_current = _candidate(tmp_path, 4)
    decoy = _private_directory(tmp_path / "backup-decoy")
    os.utime(decoy, ns=(9_000_000_000, 9_000_000_000))
    os.utime(Path(adopted_older["backup_directory"]), ns=(1, 1))

    first = _advance(index, current, intent="bootstrap_current", ordinal=20)
    first_ref = first["current"]
    second = _advance(index, adopted_older, intent="fill_older", ordinal=30)
    adopted_ref = second["older"]
    assert {pin.backup_directory for pin in index.pins()} == {
        Path(current["backup_directory"]),
        Path(adopted_older["backup_directory"]),
    }
    assert decoy not in {pin.backup_directory for pin in index.pins()}

    third = _advance(index, next_current, intent="rotate_current", ordinal=40)

    assert third["current"] not in (first_ref, adopted_ref)
    assert third["older"] == first_ref
    assert adopted_ref not in (third["current"], third["older"])


def test_unfinished_transaction_pins_current_older_and_pending(tmp_path: Path) -> None:
    index = _index(tmp_path)
    current = _candidate(tmp_path, 5)
    older = _candidate(tmp_path, 6, source_kind="explicit_older_adoption")
    pending = _candidate(tmp_path, 7)
    _advance(index, current, intent="bootstrap_current", ordinal=50)
    clear = _advance(index, older, intent="fill_older", ordinal=60)

    prepared = index.prepare(
        intent="rotate_current",
        candidate=pending,
        expected_journal_sha256=clear["journal_sha256"],
    )
    pins = index.pins()

    assert [pin.role for pin in pins] == ["current", "older", "pending"]
    assert {pin.backup_directory for pin in pins} == {
        Path(current["backup_directory"]),
        Path(older["backup_directory"]),
        Path(pending["backup_directory"]),
    }
    assert pins[-1].generation_id is None
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 70),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 71),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending_pin = index.pins()[-1]
    assert pending_pin.generation_id == rehearsed["pending"]["generation"]["generation_id"]
    assert pending_pin.receipt_path is not None
    assert not pending_pin.receipt_path.exists()


def test_stale_cas_and_out_of_order_transitions_leave_state_unchanged(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 8),
        expected_journal_sha256=initial["journal_sha256"],
    )

    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_cas_mismatch"):
        index.record_authenticated(
            receipt=_external_receipt("authentication", 80),
            expected_journal_sha256=initial["journal_sha256"],
        )
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_transition_invalid"):
        index.record_rehearsed(
            receipt=_external_receipt("rehearsal", 81),
            expected_journal_sha256=prepared["journal_sha256"],
        )
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_recovery_receipt_required",
    ):
        index.recover(expected_journal_sha256=prepared["journal_sha256"])
    assert index.load() == prepared


def test_crash_after_no_replace_receipt_before_state_cas_recovers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 9),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 90),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 91),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    real_cas = index._cas_replace_locked  # noqa: SLF001

    def crash(
        _current: dict[str, Any],
        _following: dict[str, Any],
        _pins: Any,
    ) -> dict[str, Any]:
        raise OSError("simulated crash")

    monkeypatch.setattr(index, "_cas_replace_locked", crash)
    with pytest.raises(OSError, match="simulated crash"):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    reference = rehearsed["pending"]["generation"]
    receipt_path = index.receipt_directory / f"{reference['generation_id']}.json"
    assert receipt_path.is_file()
    assert index.load() == rehearsed

    monkeypatch.setattr(index, "_cas_replace_locked", real_cas)
    recovered = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    assert recovered["phase"] == "clear"
    assert recovered["current"] == reference
    assert index.recover(expected_journal_sha256=recovered["journal_sha256"]) == recovered


def test_foreign_receipt_at_exact_generation_name_is_never_overwritten(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 10),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 100),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 101),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    generation_id = rehearsed["pending"]["generation"]["generation_id"]
    target = index.receipt_directory / f"{generation_id}.json"
    index_raw = index.path.read_bytes()
    target.write_bytes(b'{"foreign":true}\n')
    target.chmod(0o400)

    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_invalid",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert target.read_bytes() == b'{"foreign":true}\n'
    assert index.path.read_bytes() == index_raw


def test_committed_receipt_mode_link_or_body_drift_fails_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    clear = _advance(index, _candidate(tmp_path, 11), intent="bootstrap_current", ordinal=110)
    reference = clear["current"]
    receipt = index.receipt_directory / f"{reference['generation_id']}.json"

    receipt.chmod(0o600)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()
    receipt.chmod(0o400)
    alias = index.receipt_directory / "forbidden-hardlink"
    os.link(receipt, alias)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()
    alias.unlink()
    raw = receipt.read_bytes()
    receipt.chmod(0o600)
    receipt.write_bytes(raw.replace(b"terminal_activation", b"explicit_older_adoption"))
    receipt.chmod(0o400)
    with pytest.raises(dr_index.DRGenerationIndexError, match="generation_receipt_invalid"):
        index.load()


def test_index_symlink_hardlink_or_digest_drift_fails_closed(tmp_path: Path) -> None:
    index = _index(tmp_path)
    raw = index.path.read_bytes()
    alias = index.state_directory / "index-hardlink"
    os.link(index.path, alias)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_index_invalid"):
        index.load()
    alias.unlink()

    payload = json.loads(raw)
    payload["revision"] = 999
    index.path.write_bytes(_canonical(payload) + b"\n")
    index.path.chmod(0o600)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_index_digest_mismatch",
    ):
        index.load()

    index.path.unlink()
    outside = index.state_directory / "outside"
    outside.write_bytes(raw)
    outside.chmod(0o600)
    index.path.symlink_to(outside)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_index_invalid"):
        index.load()


def test_closed_intents_and_exact_external_receipts_reject_implicit_adoption(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    adoption = _candidate(tmp_path, 12, source_kind="explicit_older_adoption")
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_intent_invalid"):
        index.prepare(
            intent="bootstrap_current",
            candidate=adoption,
            expected_journal_sha256=initial["journal_sha256"],
        )
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_intent_invalid"):
        index.prepare(
            intent="fill_older",
            candidate=_candidate(tmp_path, 13),
            expected_journal_sha256=initial["journal_sha256"],
        )

    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 14),
        expected_journal_sha256=initial["journal_sha256"],
    )
    with pytest.raises(dr_index.DRGenerationIndexError, match="authentication_receipt_invalid"):
        index.record_authenticated(
            receipt={"schema": "friday.test.v1", "sha256": "not-a-digest"},
            expected_journal_sha256=prepared["journal_sha256"],
        )
    assert index.load() == prepared


def test_same_backup_path_cannot_be_republished_as_a_distinct_generation(tmp_path: Path) -> None:
    index = _index(tmp_path)
    candidate = _candidate(tmp_path, 15)
    clear = _advance(index, candidate, intent="bootstrap_current", ordinal=150)
    forged_generation = _candidate(tmp_path, 16)
    forged_generation["backup_directory"] = candidate["backup_directory"]
    prepared = index.prepare(
        intent="rotate_current",
        candidate=forged_generation,
        expected_journal_sha256=clear["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 160),
        expected_journal_sha256=prepared["journal_sha256"],
    )

    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_duplicate_slot"):
        index.record_rehearsed(
            receipt=_external_receipt("rehearsal", 161),
            expected_journal_sha256=authenticated["journal_sha256"],
        )
    assert index.load() == authenticated


def test_bootstrap_crash_after_atomic_noreplace_has_one_link_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _private_directory(tmp_path / "state")
    index = dr_index.DurableDRGenerationIndex(state)
    real_rename = dr_index._rename_noreplace  # noqa: SLF001

    def rename_then_crash(directory_fd: int, source: str, destination: str) -> None:
        real_rename(directory_fd, source, destination)
        if destination == dr_index.INDEX_NAME:
            raise OSError("crash after atomic bootstrap rename")

    monkeypatch.setattr(dr_index, "_rename_noreplace", rename_then_crash)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="dr_generation_index_initialization_failed",
    ):
        index.initialize()

    status = index.path.stat()
    assert status.st_nlink == 1
    assert stat.S_IMODE(status.st_mode) == 0o600
    monkeypatch.setattr(dr_index, "_rename_noreplace", real_rename)
    assert index.initialize()["phase"] == "clear"


def test_receipt_crash_after_atomic_noreplace_recovers_without_two_link_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 17),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 170),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 171),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    reference = rehearsed["pending"]["generation"]
    receipt_name = f"{reference['generation_id']}.json"
    real_rename = dr_index._rename_noreplace  # noqa: SLF001

    def rename_then_crash(directory_fd: int, source: str, destination: str) -> None:
        real_rename(directory_fd, source, destination)
        if destination == receipt_name:
            raise OSError("crash after atomic receipt rename")

    monkeypatch.setattr(dr_index, "_rename_noreplace", rename_then_crash)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_publication_failed",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    receipt_path = index.receipt_directory / receipt_name
    assert receipt_path.stat().st_nlink == 1
    assert index.load() == rehearsed

    monkeypatch.setattr(dr_index, "_rename_noreplace", real_rename)
    recovered = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    assert recovered["current"] == reference
    assert receipt_path.stat().st_nlink == 1


def test_authorized_partial_receipt_staging_is_recovered_without_scanning(tmp_path: Path) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 20),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 200),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 201),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending = rehearsed["pending"]
    _reference, receipt_raw = dr_index._generation_receipt(  # noqa: SLF001
        {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
            "rehearsal_receipt": pending["rehearsal_receipt"],
            "schema": dr_index.GENERATION_SCHEMA,
        }
    )
    generation_id = pending["generation"]["generation_id"]
    receipt_name = f"{generation_id}.json"
    staging = index.receipt_directory / f".{receipt_name}.{hashlib.sha256(receipt_raw).hexdigest()}.new"
    staging.write_bytes(receipt_raw[: len(receipt_raw) // 2])
    staging.chmod(0o600)

    clear = index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    assert clear["phase"] == "clear"
    assert not staging.exists()
    assert (index.receipt_directory / receipt_name).stat().st_nlink == 1


def test_state_directory_swap_during_load_cannot_redirect_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    original_state = index.state_directory
    displaced_state = tmp_path / "state-displaced"
    real_read = dr_index._stable_private_file_at  # noqa: SLF001
    swapped = False

    def swap_then_read(
        directory_fd: int,
        name: str,
        *,
        mode: int,
        maximum_bytes: int,
        code: str,
    ) -> bytes:
        nonlocal swapped
        if name == dr_index.INDEX_NAME and not swapped:
            swapped = True
            original_state.rename(displaced_state)
            _private_directory(original_state)
            _private_directory(original_state / dr_index.RECEIPT_DIRECTORY_NAME)
            (original_state / "replacement-marker").write_bytes(b"replacement")
        return real_read(
            directory_fd,
            name,
            mode=mode,
            maximum_bytes=maximum_bytes,
            code=code,
        )

    monkeypatch.setattr(dr_index, "_stable_private_file_at", swap_then_read)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.load()

    assert not (original_state / dr_index.INDEX_NAME).exists()
    assert (original_state / "replacement-marker").read_bytes() == b"replacement"
    assert (displaced_state / dr_index.INDEX_NAME).is_file()


def test_state_directory_swap_during_cas_never_writes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 18),
        expected_journal_sha256=initial["journal_sha256"],
    )
    original_state = index.state_directory
    displaced_state = tmp_path / "state-displaced"
    real_replace = dr_index._replace_private_durable_at  # noqa: SLF001
    swapped = False

    def swap_then_replace(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            original_state.rename(displaced_state)
            _private_directory(original_state)
            _private_directory(original_state / dr_index.RECEIPT_DIRECTORY_NAME)
            (original_state / "replacement-marker").write_bytes(b"replacement")
        real_replace(directory_fd, name, raw, code=code)

    monkeypatch.setattr(dr_index, "_replace_private_durable_at", swap_then_replace)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.record_authenticated(
            receipt=_external_receipt("authentication", 180),
            expected_journal_sha256=prepared["journal_sha256"],
        )

    assert not (original_state / dr_index.INDEX_NAME).exists()
    assert (original_state / "replacement-marker").read_bytes() == b"replacement"
    displaced_payload = json.loads((displaced_state / dr_index.INDEX_NAME).read_text(encoding="ascii"))
    assert displaced_payload["phase"] == "authenticated"


def test_receipt_directory_swap_during_publish_never_writes_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 19),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 190),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 191),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    state_raw = index.path.read_bytes()
    displaced_receipts = index.state_directory / "receipts-displaced"
    real_rename = dr_index._rename_noreplace  # noqa: SLF001
    swapped = False

    def swap_then_rename(directory_fd: int, source: str, destination: str) -> None:
        nonlocal swapped
        if destination != dr_index.INDEX_NAME and not swapped:
            swapped = True
            index.receipt_directory.rename(displaced_receipts)
            _private_directory(index.receipt_directory)
            (index.receipt_directory / "replacement-marker").write_bytes(b"replacement")
        real_rename(directory_fd, source, destination)

    monkeypatch.setattr(dr_index, "_rename_noreplace", swap_then_rename)
    with pytest.raises(dr_index.DRGenerationIndexError, match="dr_generation_directory_changed"):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])

    generation_id = rehearsed["pending"]["generation"]["generation_id"]
    assert index.path.read_bytes() == state_raw
    assert not (index.receipt_directory / f"{generation_id}.json").exists()
    assert (index.receipt_directory / "replacement-marker").read_bytes() == b"replacement"
    assert (displaced_receipts / f"{generation_id}.json").stat().st_nlink == 1


def test_state_lock_serializes_two_writers_across_receipt_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_a = _index(tmp_path)
    index_b = dr_index.DurableDRGenerationIndex(index_a.state_directory)
    initial = index_a.load()
    prepared = index_a.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 21),
        expected_journal_sha256=initial["journal_sha256"],
    )
    real_replace = dr_index._replace_private_durable_at  # noqa: SLF001
    a_at_replace = threading.Event()
    allow_a_replace = threading.Event()
    b_started = threading.Event()
    b_done = threading.Event()
    outcomes: dict[str, object] = {}

    def gated_replace(directory_fd: int, name: str, raw: bytes, *, code: str) -> None:
        if threading.current_thread().name == "dr-writer-a":
            a_at_replace.set()
            if not allow_a_replace.wait(timeout=5):
                raise AssertionError("writer A gate timeout")
        real_replace(directory_fd, name, raw, code=code)

    monkeypatch.setattr(dr_index, "_replace_private_durable_at", gated_replace)

    def writer_a() -> None:
        try:
            outcomes["a"] = index_a.record_authenticated(
                receipt=_external_receipt("authentication-a", 210),
                expected_journal_sha256=prepared["journal_sha256"],
            )
        except BaseException as exc:  # noqa: BLE001 - exact concurrent outcome under test.
            outcomes["a"] = exc

    def writer_b() -> None:
        b_started.set()
        try:
            outcomes["b"] = index_b.record_authenticated(
                receipt=_external_receipt("authentication-b", 211),
                expected_journal_sha256=prepared["journal_sha256"],
            )
        except BaseException as exc:  # noqa: BLE001 - exact concurrent outcome under test.
            outcomes["b"] = exc
        finally:
            b_done.set()

    thread_a = threading.Thread(target=writer_a, name="dr-writer-a")
    thread_a.start()
    assert a_at_replace.wait(timeout=5)
    displaced_receipts = index_a.state_directory / "receipt-lock-domain-old"
    index_a.receipt_directory.rename(displaced_receipts)
    _private_directory(index_a.receipt_directory)

    thread_b = threading.Thread(target=writer_b, name="dr-writer-b")
    thread_b.start()
    assert b_started.wait(timeout=5)
    assert not b_done.wait(timeout=0.1), "writer B escaped the pinned state-directory lock"
    allow_a_replace.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()

    assert isinstance(outcomes["a"], dr_index.DRGenerationIndexError)
    assert "dr_generation_directory_changed" in str(outcomes["a"])
    assert isinstance(outcomes["b"], dr_index.DRGenerationIndexError)
    assert "dr_generation_cas_mismatch" in str(outcomes["b"])
    durable = index_b.load()
    assert durable["phase"] == "authenticated"
    assert durable["revision"] == prepared["revision"] + 1
    assert durable["pending"]["authentication_receipt"] == _external_receipt("authentication-a", 210)


@pytest.mark.parametrize(
    ("boundary", "staging_mode"),
    (("content_fsync_0600", 0o600), ("metadata_fsync_0400", 0o400)),
)
def test_receipt_staging_crash_boundaries_recover_after_ordered_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    staging_mode: int,
) -> None:
    index = _index(tmp_path)
    initial = index.load()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(tmp_path, 22),
        expected_journal_sha256=initial["journal_sha256"],
    )
    authenticated = index.record_authenticated(
        receipt=_external_receipt("authentication", 220),
        expected_journal_sha256=prepared["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_external_receipt("rehearsal", 221),
        expected_journal_sha256=authenticated["journal_sha256"],
    )
    pending = rehearsed["pending"]
    _reference, receipt_raw = dr_index._generation_receipt(  # noqa: SLF001
        {
            "authentication_receipt": pending["authentication_receipt"],
            "candidate": pending["candidate"],
            "rehearsal_receipt": pending["rehearsal_receipt"],
            "schema": dr_index.GENERATION_SCHEMA,
        }
    )
    generation_id = pending["generation"]["generation_id"]
    receipt_name = f"{generation_id}.json"
    staging = index.receipt_directory / f".{receipt_name}.{hashlib.sha256(receipt_raw).hexdigest()}.new"
    real_fsync = os.fsync
    crashed = False

    def crash_at_boundary(descriptor: int) -> None:
        nonlocal crashed
        status = os.fstat(descriptor)
        mode = stat.S_IMODE(status.st_mode)
        real_fsync(descriptor)
        should_crash = (
            boundary == "content_fsync_0600" and stat.S_ISREG(status.st_mode) and mode == 0o600
        ) or (boundary == "metadata_fsync_0400" and stat.S_ISREG(status.st_mode) and mode == 0o400)
        if should_crash and not crashed:
            crashed = True
            raise OSError(f"crash at {boundary}")

    monkeypatch.setattr(dr_index.os, "fsync", crash_at_boundary)
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="generation_receipt_publication_failed",
    ):
        index.publish(expected_journal_sha256=rehearsed["journal_sha256"])
    assert crashed
    assert staging.read_bytes() == receipt_raw
    assert stat.S_IMODE(staging.stat().st_mode) == staging_mode
    assert staging.stat().st_nlink == 1

    monkeypatch.setattr(dr_index.os, "fsync", real_fsync)
    clear = index.recover(expected_journal_sha256=rehearsed["journal_sha256"])
    receipt = index.receipt_directory / receipt_name
    assert clear["phase"] == "clear"
    assert not staging.exists()
    assert receipt.stat().st_nlink == 1
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o400
