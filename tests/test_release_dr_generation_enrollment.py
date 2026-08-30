from __future__ import annotations

import hashlib
import inspect
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_enrollment as enrollment
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


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = _private_directory(tmp_path / "friday-home")
    _private_directory(home / "data")
    _private_directory(home / "data/state")
    _private_directory(home / "data/backups")
    receipt = tmp_path / "activation-receipt.json"
    receipt.write_text("{}\n", encoding="ascii")
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    return home, receipt


def _candidate(home: Path, ordinal: int) -> dict[str, Any]:
    backup = _private_directory(home / "data/backups" / f"backup-{ordinal}")
    release = _private_directory(home / "wheel-only-releases" / f"{ordinal:040x}")
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
        "source_kind": "terminal_activation",
        "source_receipt_sha256": digest(8),
        "source_transaction_id": digest(9),
    }


def _authentication_receipt(candidate: dict[str, Any], ordinal: int) -> dict[str, Any]:
    status = Path(candidate["backup_directory"]).stat()
    core = {
        "activation_journal_file_sha256": f"{ordinal + 1:064x}",
        "activation_journal_sha256": f"{ordinal + 2:064x}",
        "activation_receipt_file_sha256": f"{ordinal + 3:064x}",
        "activation_receipt_sha256": candidate["source_receipt_sha256"],
        "backup_directory": {"device": status.st_dev, "inode": status.st_ino, "path": candidate["backup_directory"]},
        "backup_manifest_sha256": f"{ordinal + 4:064x}",
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "restore_operator_sha256": f"{ordinal + 5:064x}",
        "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA,
        "status": "authenticated",
        "surface_receipts": {
            "database": candidate["database_receipt_sha256"],
            "engineer": candidate["engineer_receipt_sha256"],
            "inbox": candidate["inbox_receipt_sha256"],
            "obsidian": candidate["obsidian_receipt_sha256"],
        },
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _rehearsal_receipt(
    candidate: dict[str, Any],
    authentication: dict[str, Any],
    state: dict[str, Any],
    _ordinal: int,
) -> dict[str, Any]:
    restore = candidate["restore_release"]
    source_keys = (
        "activation_journal_file_sha256", "activation_journal_sha256",
        "activation_receipt_file_sha256", "activation_receipt_sha256",
        "backup_manifest_sha256", "restore_operator_sha256", "surface_receipts",
    )
    core = {
        "authentication_receipt_sha256": authentication["receipt_sha256"],
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "check_count": len(dr_index.DR_REHEARSAL_CHECKS),
        "checkset_sha256": dr_index.DR_REHEARSAL_CHECKSET_SHA256,
        "database_foreign_keys_clear": True,
        "database_integrity_clear": True,
        "database_reopen_count": 2,
        "database_schema": 46,
        "engineer_authority_present": True,
        "engineer_exact": True,
        "fault_boundary": "after_migration_before_provision_or_network",
        "four_surface_exact": True,
        "four_surface_sha256": hashlib.sha256(
            _canonical(authentication["surface_receipts"])
        ).hexdigest(),
        "index_journal_sha256": state["journal_sha256"],
        "index_revision": state["revision"],
        "index_transaction_id": state["transaction_id"],
        "inbox_foreign_keys_clear": True,
        "inbox_integrity_clear": True,
        "inbox_reopen_count": 2,
        "network_call_count": 0,
        "obsidian_exact": True,
        "production_surface_write_count": 0,
        "restore_release": {key: restore[key] for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")},
        "rollback_restore_observed": True,
        "rollback_tree_sha256": restore["tree_manifest_sha256"],
        "rolled_back": True,
        "schema": dr_index.REHEARSAL_RECEIPT_SCHEMA,
        "scratch_removed": True,
        "source": {key: authentication[key] for key in source_keys},
        "status": "rehearsed",
        "systemctl_call_count": 0,
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _authenticated(
    candidate: dict[str, Any],
    *,
    ordinal: int,
) -> dr_auth.AuthenticatedDRCandidate:
    return dr_auth.AuthenticatedDRCandidate(
        candidate=candidate,
        authentication_receipt=_authentication_receipt(candidate, ordinal),
    )


def _install_authenticator(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    activation_receipt: Path,
    outcomes: list[dr_auth.AuthenticatedDRCandidate | BaseException],
) -> list[tuple[Path, Path, Path]]:
    calls: list[tuple[Path, Path, Path]] = []

    def authenticate(
        *,
        activation_journal: Path,
        activation_receipt: Path,
        backup_root: Path,
    ) -> dr_auth.AuthenticatedDRCandidate:
        calls.append((activation_journal, activation_receipt, backup_root))
        assert activation_journal == home / "data/state/immutable-release-activation.v1.json"
        assert activation_receipt == activation_receipt_path
        assert backup_root == home / "data/backups"
        outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    activation_receipt_path = activation_receipt
    monkeypatch.setattr(dr_auth, "_authenticate_locked", authenticate)
    return calls


def _index(home: Path) -> dr_index.DurableDRGenerationIndex:
    return dr_index.DurableDRGenerationIndex(home / "data/state")


def _publish(
    index: dr_index.DurableDRGenerationIndex,
    candidate: dict[str, Any],
    *,
    ordinal: int,
) -> dict[str, Any]:
    state = index.initialize()
    state = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    authentication = _authentication_receipt(candidate, ordinal)
    state = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authentication, state, ordinal),
        expected_journal_sha256=state["journal_sha256"],
    )
    return index.publish(expected_journal_sha256=state["journal_sha256"])


def test_bootstrap_authenticates_before_admission_and_emits_body_free_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    candidate = _candidate(home, 1)
    authenticated = _authenticated(candidate, ordinal=11)
    calls = _install_authenticator(
        monkeypatch,
        home,
        activation_receipt,
        [authenticated],
    )

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert len(calls) == 2
    state = _index(home).load()
    assert state["phase"] == "authenticated"
    assert state["current"] is None
    assert state["pending"]["candidate"] == candidate
    assert receipt["status"] == "admitted"
    assert receipt["action"] == "prepared_and_authenticated"
    assert receipt["published"] is False
    assert receipt["rehearsal_present"] is False
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    assert receipt["receipt_sha256"] == hashlib.sha256(_canonical(core)).hexdigest()
    assert str(home) not in json.dumps(receipt)
    assert "candidate" not in receipt
    persisted = (
        _index(home).receipt_directory
        / f"authentication-{authenticated.authentication_receipt['receipt_sha256']}.json"
    )
    assert json.loads(persisted.read_text(encoding="ascii")) == authenticated.authentication_receipt
    assert stat.S_IMODE(persisted.stat().st_mode) == 0o400


def test_authenticated_retry_is_idempotent_and_does_not_advance_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    authenticated = _authenticated(_candidate(home, 2), ordinal=22)
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])
    enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)
    before = _index(home).load()

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    after = _index(home).load()
    assert after == before
    assert receipt["action"] == "already_authenticated"
    assert receipt["index_phase"] == "authenticated"


def test_prepared_restart_records_full_authentication_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    candidate = _candidate(home, 3)
    authenticated = _authenticated(candidate, ordinal=33)
    index = _index(home)
    state = index.initialize()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    admitted = index.load()
    assert admitted["phase"] == "authenticated"
    assert admitted["revision"] == prepared["revision"] + 1
    assert receipt["action"] == "resumed_and_authenticated"


def test_conflicting_pending_candidate_fails_closed_without_index_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    state = index.initialize()
    before = index.prepare(
        intent="bootstrap_current",
        candidate=_candidate(home, 4),
        expected_journal_sha256=state["journal_sha256"],
    )
    authenticated = _authenticated(_candidate(home, 5), ordinal=55)
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_pending_conflict$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert index.load() == before


def test_clear_index_rotates_only_a_distinct_backup_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    current = _candidate(home, 6)
    before = _publish(index, current, ordinal=66)
    following = _candidate(home, 7)
    authenticated = _authenticated(following, ordinal=77)
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    admitted = index.load()
    assert admitted["current"] == before["current"]
    assert admitted["phase"] == "authenticated"
    assert admitted["pending"]["intent"] == "rotate_current"
    assert admitted["pending"]["candidate"] == following
    assert receipt["intent"] == "rotate_current"


def test_exact_already_current_backup_is_body_free_and_does_not_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    current = _candidate(home, 8)
    before = _publish(index, current, ordinal=88)
    authenticated = _authenticated(current, ordinal=88)
    calls = _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])
    receipts_before = {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()}

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert len(calls) == 2
    assert index.load() == before
    assert {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()} == receipts_before
    assert receipt["action"] == "already_current"
    assert receipt["intent"] == "rotate_current"
    assert receipt["published"] is True
    assert receipt["rehearsal_present"] is True
    assert receipt["index_phase"] == "clear"
    assert receipt["index_revision"] == before["revision"]
    assert str(home) not in json.dumps(receipt)
    assert "candidate" not in receipt


@pytest.mark.parametrize("mismatch", ("candidate", "authentication"))
def test_same_backup_path_identity_mismatch_fails_without_rotation_or_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    current = _candidate(home, 81)
    before = _publish(index, current, ordinal=810)
    candidate = current
    authentication_ordinal = 810
    if mismatch == "candidate":
        candidate = _candidate(home, 82)
        candidate["backup_directory"] = current["backup_directory"]
    else:
        authentication_ordinal = 811
    authenticated = _authenticated(candidate, ordinal=authentication_ordinal)
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])
    receipts_before = {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()}

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_current_conflict$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert index.load() == before
    assert {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()} == receipts_before
    unexpected = (
        index.receipt_directory
        / f"authentication-{authenticated.authentication_receipt['receipt_sha256']}.json"
    )
    if mismatch == "authentication":
        assert not unexpected.exists()


def test_already_current_reauthentication_drift_leaves_authority_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    current = _candidate(home, 83)
    before = _publish(index, current, ordinal=830)
    first = _authenticated(current, ordinal=830)
    second = _authenticated(current, ordinal=831)
    _install_authenticator(monkeypatch, home, activation_receipt, [first, second])
    receipts_before = {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()}

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_reauthentication_mismatch$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert index.load() == before
    assert {path.name: path.read_bytes() for path in index.receipt_directory.iterdir()} == receipts_before


def test_rehearsed_retry_never_publishes_without_a_separate_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    candidate = _candidate(home, 9)
    authenticated = _authenticated(candidate, ordinal=99)
    index = _index(home)
    state = index.initialize()
    state = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_authenticated(
        receipt=authenticated.authentication_receipt,
        expected_journal_sha256=state["journal_sha256"],
    )
    rehearsed = index.record_rehearsed(
        receipt=_rehearsal_receipt(candidate, authenticated.authentication_receipt, state, 99),
        expected_journal_sha256=state["journal_sha256"],
    )
    _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])

    receipt = enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert index.load() == rehearsed
    assert receipt["index_phase"] == "rehearsed"
    assert receipt["rehearsal_present"] is True
    assert receipt["published"] is False


def test_authentication_failure_precedes_all_dr_index_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index_path = home / "data/state" / dr_index.INDEX_NAME
    receipt_directory = home / "data/state" / dr_index.RECEIPT_DIRECTORY_NAME
    calls = _install_authenticator(
        monkeypatch,
        home,
        activation_receipt,
        [dr_auth.DRGenerationAuthenticationError("test_authentication_failed")],
    )

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^test_authentication_failed$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert len(calls) == 1
    assert not index_path.exists()
    assert not receipt_directory.exists()


def test_state_namespace_swap_after_first_authentication_fails_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    authenticated = _authenticated(_candidate(home, 14), ordinal=140)
    calls = _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])
    original_initialize = dr_index.DurableDRGenerationIndex.initialize
    state_directory = home / "data/state"
    displaced = home / "data/state-displaced"

    def swap_then_initialize(
        index: dr_index.DurableDRGenerationIndex,
    ) -> dict[str, Any]:
        state_directory.rename(displaced)
        _private_directory(state_directory)
        return original_initialize(index)

    monkeypatch.setattr(
        dr_index.DurableDRGenerationIndex,
        "initialize",
        swap_then_initialize,
    )

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_generation_state_directory_changed$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert len(calls) == 1
    assert not (state_directory / dr_index.INDEX_NAME).exists()
    assert not (state_directory / dr_index.RECEIPT_DIRECTORY_NAME).exists()


def test_state_namespace_is_pinned_before_outer_lock_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    authenticated = _authenticated(_candidate(home, 15), ordinal=150)
    calls = _install_authenticator(monkeypatch, home, activation_receipt, [authenticated])
    state_directory = home / "data/state"
    displaced = home / "data/state-before-lock"

    class SwappingLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self) -> SwappingLock:
            state_directory.rename(displaced)
            _private_directory(state_directory)
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

    monkeypatch.setattr(release_operator, "OperatorTransactionLock", SwappingLock)

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_generation_state_directory_changed$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert len(calls) == 1
    assert not (state_directory / dr_index.INDEX_NAME).exists()


def test_reauthentication_drift_leaves_pinned_admission_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    first = _authenticated(_candidate(home, 10), ordinal=100)
    changed = _authenticated(_candidate(home, 11), ordinal=110)
    _install_authenticator(
        monkeypatch,
        home,
        activation_receipt,
        [first, changed],
    )

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_reauthentication_mismatch$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    state = _index(home).load()
    assert state["phase"] == "authenticated"
    assert state["pending"]["candidate"] == first.candidate


def test_outer_operator_lock_covers_both_authentication_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    authenticated = _authenticated(_candidate(home, 12), ordinal=120)
    held = False
    calls = 0

    class TrackingLock:
        def __init__(self, path: Path) -> None:
            assert path == home / "data/state/immutable-release-operator.v1.lock"

        def __enter__(self) -> TrackingLock:
            nonlocal held
            assert held is False
            held = True
            return self

        def __exit__(self, *_args: Any) -> None:
            nonlocal held
            assert held is True
            held = False

    def authenticate(**_kwargs: Any) -> dr_auth.AuthenticatedDRCandidate:
        nonlocal calls
        assert held is True
        calls += 1
        return authenticated

    monkeypatch.setattr(release_operator, "OperatorTransactionLock", TrackingLock)
    monkeypatch.setattr(dr_auth, "_authenticate_locked", authenticate)

    enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert held is False
    assert calls == 2


def test_authenticated_receipt_conflict_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    candidate = _candidate(home, 13)
    first = _authenticated(candidate, ordinal=130)
    index = _index(home)
    state = index.initialize()
    state = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    before = index.record_authenticated(
        receipt=first.authentication_receipt,
        expected_journal_sha256=state["journal_sha256"],
    )
    conflicting = _authenticated(candidate, ordinal=131)
    _install_authenticator(monkeypatch, home, activation_receipt, [conflicting])

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_pending_conflict$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=activation_receipt)

    assert index.load() == before


@pytest.mark.parametrize("value", [None, "relative/home", "/tmp/../tmp/friday"])
def test_rejects_missing_or_noncanonical_friday_home_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    receipt = tmp_path / "activation.json"
    receipt.write_text("{}\n", encoding="ascii")
    if value is None:
        monkeypatch.delenv("FRIDAY_HOME", raising=False)
    else:
        monkeypatch.setenv("FRIDAY_HOME", value)
    called = False

    def unexpected(**_kwargs: Any) -> dr_auth.AuthenticatedDRCandidate:
        nonlocal called
        called = True
        raise AssertionError("authentication must not run")

    monkeypatch.setattr(dr_auth, "_authenticate_locked", unexpected)

    with pytest.raises(
        enrollment.DRGenerationEnrollmentError,
        match="^dr_enrollment_friday_home_invalid$",
    ):
        enrollment.enroll_terminal_activation_backup(activation_receipt=receipt)

    assert called is False
    assert tuple(inspect.signature(enrollment.enroll_terminal_activation_backup).parameters) == (
        "activation_receipt",
    )
