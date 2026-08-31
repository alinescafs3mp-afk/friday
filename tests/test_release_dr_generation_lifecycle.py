from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
from tools import release_artifact_retention_operator as retention_apply
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_index as dr_index
from tools import release_dr_generation_lifecycle as lifecycle
from tools import release_dr_generation_rehearsal as rehearsal

pytestmark = pytest.mark.usefixtures("isolated_operator_transaction_domain")


@pytest.fixture(autouse=True)
def _synthetic_engineer_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _private(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def test_publish_blocks_on_unfinished_retention_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    state_dir = home / "data/state"
    monkeypatch.setattr(
        retention_apply,
        "_load_journal",
        lambda path: {"phase": "prepared"} if path == state_dir / retention_apply.APPLY_JOURNAL_NAME else None,
    )
    monkeypatch.setattr(
        dr_auth,
        "_authenticate_material_locked",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("authentication must not start")),
    )

    with pytest.raises(
        lifecycle.DRGenerationLifecycleError,
        match="^unfinished_retention_apply_requires_recovery$",
    ):
        lifecycle.publish_or_recover_authenticated_generation(
            activation_receipt=activation_receipt,
        )


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = _private(tmp_path / "friday-home")
    data = _private(home / "data")
    _private(data / "state")
    _private(data / "backups")
    receipt = tmp_path / "activation-receipt.json"
    receipt.write_text("{}\n", encoding="ascii")
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    return home, receipt


def _candidate(home: Path, ordinal: int) -> dict[str, Any]:
    backup = _private(home / "data/backups" / f"backup-{ordinal}")
    release = _private(home / "releases" / f"{ordinal:040x}")
    digest = lambda offset: f"{ordinal * 16 + offset:064x}"  # noqa: E731
    return {
        "allowed_rollback_tree_sha256s": sorted({digest(6), "a" * 64}),
        "backup_directory": str(backup),
        "backup_record_sha256": digest(1),
        "database_receipt_sha256": digest(2),
        "database_schema": 46,
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
    digest = lambda offset: f"{ordinal * 32 + offset:064x}"  # noqa: E731
    backup = Path(candidate["backup_directory"])
    backup_status = backup.stat()
    core = {
        "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
        "activation_journal_file_sha256": digest(1),
        "activation_journal_sha256": digest(2),
        "activation_receipt_file_sha256": digest(3),
        "activation_receipt_sha256": candidate["source_receipt_sha256"],
        "backup_directory": {
            "device": backup_status.st_dev,
            "inode": backup_status.st_ino,
            "path": str(backup),
        },
        "backup_manifest_sha256": digest(5),
        "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
        "database_schema": candidate["database_schema"],
        "restore_operator_sha256": digest(6),
        "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA,
        "source_transaction_id": candidate["source_transaction_id"],
        "status": "authenticated",
        "surface_receipts": {
            "database": candidate["database_receipt_sha256"],
            "engineer": candidate["engineer_receipt_sha256"],
            "inbox": candidate["inbox_receipt_sha256"],
            "obsidian": candidate["obsidian_receipt_sha256"],
        },
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _release(record: dict[str, Any]) -> release_operator.ReleaseIdentity:
    return release_operator.ReleaseIdentity(
        root=Path(record["root"]),
        commit=record["commit"],
        version=record["version"],
        tree_manifest_sha256=record["tree_manifest_sha256"],
        max_schema=record["max_schema"],
    )


def _material(candidate: dict[str, Any], ordinal: int) -> dr_auth.AuthenticatedDRMaterial:
    authentication = _authentication_receipt(candidate, ordinal)
    return dr_auth.AuthenticatedDRMaterial(
        authenticated=dr_auth.AuthenticatedDRCandidate(candidate, authentication),
        backup=release_operator.DatabaseBackup(
            candidate["database_schema"],
            candidate["database_receipt_sha256"],
            candidate["inbox_receipt_sha256"],
            obsidian_receipt_sha256=candidate["obsidian_receipt_sha256"],
            engineer_receipt_sha256=candidate["engineer_receipt_sha256"],
        ),
        activation_candidate=release_operator.ReleaseIdentity(
            Path(candidate["restore_release"]["root"]).with_name(f"candidate-{ordinal}"),
            "c" * 40,
            f"0.207.{ordinal}",
            "c" * 64,
            50,
        ),
        activation_previous=release_operator.ReleaseIdentity(
            Path(candidate["restore_release"]["root"]).with_name(f"previous-{ordinal}"),
            "a" * 40,
            f"0.206.{ordinal}",
            "a" * 64,
            50,
        ),
        restore_fallback=_release(candidate["restore_release"]),
    )


def _index(home: Path) -> dr_index.DurableDRGenerationIndex:
    return dr_index.DurableDRGenerationIndex(home / "data/state")


def _prepare_rehearsed(
    index: dr_index.DurableDRGenerationIndex,
    material: dr_auth.AuthenticatedDRMaterial,
    *,
    intent: str,
) -> dict[str, Any]:
    state = index.initialize()
    state = index.prepare(
        intent=intent,
        candidate=material.authenticated.candidate,
        expected_journal_sha256=state["journal_sha256"],
    )
    state = index.record_authenticated(
        receipt=material.authenticated.authentication_receipt,
        expected_journal_sha256=state["journal_sha256"],
    )
    pending = index.pending_generation_identity(
        expected_journal_sha256=state["journal_sha256"],
    )
    receipt = rehearsal._receipt(  # noqa: SLF001
        pending=pending,
        material=material,
        result=rehearsal._RunResult(  # noqa: SLF001
            material.backup.schema_version,
            material.activation_previous.tree_manifest_sha256,
            rehearsal._four_surface_receipt_sha256(material.backup),  # noqa: SLF001
            False,
        ),
    )
    return index.record_rehearsed(
        receipt=receipt,
        expected_journal_sha256=state["journal_sha256"],
    )


def _install_material_authentication(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[dr_auth.AuthenticatedDRMaterial],
) -> None:
    calls = 0

    def authenticate(**_kwargs: Any) -> dr_auth.AuthenticatedDRMaterial:
        nonlocal calls
        outcome = outcomes[min(calls, len(outcomes) - 1)]
        calls += 1
        return outcome

    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", authenticate)


def _displace_transaction_namespace(
    transaction: release_operator.OperatorTransactionLock,
    surface: str,
) -> Callable[[], None]:
    if surface == "local_lock":
        original = transaction.path
    elif surface == "global_lock":
        runtime_root = transaction._runtime_directory  # noqa: SLF001
        assert runtime_root is not None
        runtime_locks = transaction._runtime_descriptors  # noqa: SLF001
        assert len(runtime_locks) == 1
        original = runtime_root / runtime_locks[0][0]
    elif surface == "state_directory":
        original = transaction.state_dir
    elif surface == "runtime_root":
        runtime_root = transaction._runtime_directory  # noqa: SLF001
        assert runtime_root is not None
        original = runtime_root
    else:  # pragma: no cover - parameter allowlist.
        raise AssertionError(surface)

    displaced = original.with_name(f"{original.name}.{surface}.displaced")
    original.rename(displaced)
    if displaced.is_dir():
        original.mkdir(mode=0o700)
        original.chmod(0o700)
    else:
        original.write_bytes(b"")
        original.chmod(0o600)

    def restore() -> None:
        if original.is_dir():
            original.rmdir()
        else:
            original.unlink()
        displaced.rename(original)

    return restore


def test_publish_reauthenticates_rehearsal_and_retry_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    material = _material(_candidate(home, 1), 1)
    index = _index(home)
    rehearsed = _prepare_rehearsed(index, material, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [material])

    first = lifecycle.publish_or_recover_authenticated_generation(
        activation_receipt=activation_receipt,
    )
    clear = index.load()
    retry = lifecycle.publish_or_recover_authenticated_generation(
        activation_receipt=activation_receipt,
    )

    assert first["action"] == "published"
    assert retry == {**first, "action": "already_published"}
    assert clear == index.load()
    assert clear["revision"] == rehearsed["revision"] + 1
    assert clear["current"]["generation_id"] == first["current_generation_id"]
    assert first["older_generation_id"] is None


def test_stale_activation_before_publish_fails_without_index_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    admitted = _material(_candidate(home, 2), 2)
    changed = _material(_candidate(home, 3), 3)
    index = _index(home)
    before = _prepare_rehearsed(index, admitted, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [changed])

    with pytest.raises(
        lifecycle.DRGenerationLifecycleError,
        match="^dr_rehearsal_pending_identity_mismatch$",
    ):
        lifecycle.publish_or_recover_authenticated_generation(
            activation_receipt=activation_receipt,
        )

    assert index.load() == before


def test_source_drift_between_final_observations_fails_without_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    first = _material(_candidate(home, 4), 4)
    changed = dr_auth.AuthenticatedDRMaterial(
        authenticated=first.authenticated,
        backup=release_operator.DatabaseBackup(
            first.backup.schema_version + 1,
            first.backup.receipt_sha256,
            first.backup.inbox_receipt_sha256,
            obsidian_receipt_sha256=first.backup.obsidian_receipt_sha256,
            engineer_receipt_sha256=first.backup.engineer_receipt_sha256,
        ),
        activation_candidate=first.activation_candidate,
        activation_previous=first.activation_previous,
        restore_fallback=first.restore_fallback,
    )
    index = _index(home)
    before = _prepare_rehearsed(index, first, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [first, changed])

    with pytest.raises(
        lifecycle.DRGenerationLifecycleError,
        match="^dr_rehearsal_source_changed$",
    ):
        lifecycle.publish_or_recover_authenticated_generation(
            activation_receipt=activation_receipt,
        )

    assert index.load() == before


def test_index_cas_drift_is_not_blindly_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    material = _material(_candidate(home, 5), 5)
    index = _index(home)
    before = _prepare_rehearsed(index, material, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [material])

    def drift(
        _index: dr_index.DurableDRGenerationIndex,
        *,
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        assert namespace_guard is not None
        namespace_guard()
        assert expected_journal_sha256 == before["journal_sha256"]
        raise dr_index.DRGenerationIndexError("dr_generation_cas_mismatch")

    monkeypatch.setattr(dr_index.DurableDRGenerationIndex, "recover", drift)

    with pytest.raises(
        lifecycle.DRGenerationLifecycleError,
        match="^dr_generation_cas_mismatch$",
    ):
        lifecycle.publish_or_recover_authenticated_generation(
            activation_receipt=activation_receipt,
        )

    assert index.load() == before


@pytest.mark.parametrize(
    "surface",
    ("local_lock", "global_lock", "state_directory", "runtime_root"),
)
def test_final_namespace_displacement_fails_before_publication_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    material = _material(_candidate(home, 51), 51)
    index = _index(home)
    before = _prepare_rehearsed(index, material, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [material])

    original_lock = release_operator.OperatorTransactionLock
    captured: list[release_operator.OperatorTransactionLock] = []

    def lock_factory(path: Path) -> release_operator.OperatorTransactionLock:
        transaction = original_lock(path)
        captured.append(transaction)
        return transaction

    monkeypatch.setattr(release_operator, "OperatorTransactionLock", lock_factory)
    original_validation = rehearsal._validate_rehearsed_pending_locked  # noqa: SLF001
    restore_namespace: Callable[[], None] | None = None

    def validate_then_displace(**kwargs: Any) -> Any:
        nonlocal restore_namespace
        validated = original_validation(**kwargs)
        assert len(captured) == 1
        restore_namespace = _displace_transaction_namespace(captured[0], surface)
        return validated

    recover_called = False

    def forbidden_recover(
        _index: dr_index.DurableDRGenerationIndex,
        *,
        expected_journal_sha256: str,
        namespace_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        nonlocal recover_called
        del expected_journal_sha256, namespace_guard
        recover_called = True
        raise AssertionError("index CAS must not start after namespace displacement")

    monkeypatch.setattr(
        rehearsal,
        "_validate_rehearsed_pending_locked",
        validate_then_displace,
    )
    monkeypatch.setattr(dr_index.DurableDRGenerationIndex, "recover", forbidden_recover)

    try:
        with pytest.raises(
            lifecycle.DRGenerationLifecycleError,
            match="^operator_transaction_lock_changed$",
        ):
            lifecycle.publish_or_recover_authenticated_generation(
                activation_receipt=activation_receipt,
            )
    finally:
        if restore_namespace is not None:
            restore_namespace()

    assert restore_namespace is not None
    assert recover_called is False
    assert index.load() == before


def test_next_terminal_release_rotates_current_into_older(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    index = _index(home)
    first_material = _material(_candidate(home, 6), 6)
    _prepare_rehearsed(index, first_material, intent="bootstrap_current")
    _install_material_authentication(monkeypatch, [first_material])
    first = lifecycle.publish_or_recover_authenticated_generation(
        activation_receipt=activation_receipt,
    )

    second_material = _material(_candidate(home, 7), 7)
    _prepare_rehearsed(index, second_material, intent="rotate_current")
    _install_material_authentication(monkeypatch, [second_material])
    second = lifecycle.publish_or_recover_authenticated_generation(
        activation_receipt=activation_receipt,
    )
    state = index.load()

    assert second["current_generation_id"] != first["current_generation_id"]
    assert second["older_generation_id"] == first["current_generation_id"]
    assert state["older"]["generation_id"] == first["current_generation_id"]


@pytest.mark.parametrize("crash_stage", ("after_rehearsal", "after_publication"))
def test_one_shot_stage_crash_retry_resumes_exact_durable_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_stage: str,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)
    material = _material(_candidate(home, 8), 8)
    monkeypatch.setattr(
        dr_auth,
        "_authenticate_locked",
        lambda **_kwargs: material.authenticated,
    )
    _install_material_authentication(monkeypatch, [material])
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: rehearsal._RunResult(  # noqa: SLF001
            material.backup.schema_version,
            material.activation_previous.tree_manifest_sha256,
            rehearsal._four_surface_receipt_sha256(material.backup),  # noqa: SLF001
            False,
        ),
    )
    original_publish = lifecycle.publish_or_recover_authenticated_generation
    calls = 0

    def crash(*, activation_receipt: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if crash_stage == "after_rehearsal" and calls == 1:
            raise lifecycle.DRGenerationLifecycleError("simulated_stage_crash")
        result = original_publish(activation_receipt=activation_receipt)
        if crash_stage == "after_publication" and calls == 1:
            raise lifecycle.DRGenerationLifecycleError("simulated_stage_crash")
        return result

    monkeypatch.setattr(lifecycle, "publish_or_recover_authenticated_generation", crash)

    with pytest.raises(lifecycle.DRGenerationLifecycleError, match="^simulated_stage_crash$"):
        lifecycle.run_terminal_activation_lifecycle(
            activation_receipt=activation_receipt,
        )

    interrupted = _index(home).load()
    assert interrupted["phase"] == ("rehearsed" if crash_stage == "after_rehearsal" else "clear")

    receipt = lifecycle.run_terminal_activation_lifecycle(
        activation_receipt=activation_receipt,
    )

    assert receipt["status"] == "published"
    assert _index(home).load()["phase"] == "clear"
    assert str(home) not in json.dumps(receipt)


def test_wrong_activation_receipt_fails_before_index_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, activation_receipt = _home(tmp_path, monkeypatch)

    def reject(**_kwargs: Any) -> dr_auth.AuthenticatedDRCandidate:
        raise dr_auth.DRGenerationAuthenticationError("activation_receipt_digest_mismatch")

    monkeypatch.setattr(dr_auth, "_authenticate_locked", reject)

    with pytest.raises(
        lifecycle.DRGenerationLifecycleError,
        match="^activation_receipt_digest_mismatch$",
    ):
        lifecycle.run_terminal_activation_lifecycle(
            activation_receipt=activation_receipt,
        )

    assert not (home / "data/state" / dr_index.INDEX_NAME).exists()


def test_cli_emits_canonical_body_safe_success_and_closed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    activation_receipt = tmp_path / "activation.json"
    success_core = {
        "action": "published",
        "authentication_receipt_sha256": "a" * 64,
        "candidate_sha256": "b" * 64,
        "current_generation_id": "c" * 64,
        "current_generation_receipt_sha256": "d" * 64,
        "enrollment_action": "prepared_and_authenticated",
        "enrollment_receipt_sha256": "e" * 64,
        "index_journal_sha256": "f" * 64,
        "index_revision": 4,
        "intent": "bootstrap_current",
        "older_generation_id": None,
        "rehearsal_receipt_sha256": "1" * 64,
        "schema": lifecycle.LIFECYCLE_RECEIPT_SCHEMA,
        "status": "published",
    }
    success = {
        **success_core,
        "receipt_sha256": hashlib.sha256(_canonical(success_core)).hexdigest(),
    }
    monkeypatch.setattr(
        lifecycle,
        "run_terminal_activation_lifecycle",
        lambda **_kwargs: success,
    )

    assert lifecycle.main(["--activation-receipt", str(activation_receipt)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.encode("ascii") == _canonical(success) + b"\n"

    def failure(error: RuntimeError) -> Callable[..., dict[str, Any]]:
        def raise_error(**_kwargs: Any) -> dict[str, Any]:
            raise error

        return raise_error

    for unexpected in (
        RuntimeError("/secret/operator/path"),
        RuntimeError("looks_like_safe_code"),
    ):
        monkeypatch.setattr(
            lifecycle,
            "run_terminal_activation_lifecycle",
            failure(unexpected),
        )
        assert lifecycle.main(["--activation-receipt", str(activation_receipt)]) == 2
        failed = json.loads(capsys.readouterr().err)
        assert failed == {
            "failure_code": "dr_lifecycle_failed_closed",
            "schema": lifecycle.LIFECYCLE_RECEIPT_SCHEMA,
            "status": "failed_closed",
        }


def test_cli_is_directly_executable_outside_repository(tmp_path: Path) -> None:
    script = Path(lifecycle.__file__).resolve()
    environment = dict(os.environ)
    environment.pop("FRIDAY_HOME", None)
    environment.pop("PYTHONPATH", None)

    # Git records the executable bit, while the checkout umask owns group-write.
    # Direct execution must therefore depend only on the executable bits here;
    # immutable release packaging validates its sealed modes separately.
    assert stat.S_IMODE(script.stat().st_mode) & 0o111 == 0o111

    completed = subprocess.run(  # noqa: S603
        [str(script), "--activation-receipt", str(tmp_path / "missing.json")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert json.loads(completed.stderr) == {
        "failure_code": "dr_enrollment_friday_home_invalid",
        "schema": lifecycle.LIFECYCLE_RECEIPT_SCHEMA,
        "status": "failed_closed",
    }
