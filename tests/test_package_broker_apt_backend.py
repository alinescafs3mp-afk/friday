from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import friday_package_broker.apt_backend as apt_backend_module
from friday_package_broker.apt_backend import PythonAptBackend
from friday_package_broker.contracts import (
    InstalledPackage,
    PackagePostconditionState,
    PackageRef,
    ServiceUnitChange,
    ServiceUnitState,
    TransactionOutcome,
)
from friday_package_broker.evidence import (
    MAX_CAPTURE_BYTES_PER_STREAM,
    BoundedDigestSink,
    OutputCapture,
    PackageEvidenceError,
    PackageEvidenceStore,
)


@dataclass
class FakeOrigin:
    origin: str = "Ubuntu"
    label: str = "Ubuntu"
    archive: str = "noble"
    site: str = "archive.ubuntu.com"
    component: str = "main"
    trusted: bool = True


class FakeVersion:
    def __init__(self, version: str, *, site: str, archive_sha256: str | None = None) -> None:
        self.version = version
        self.architecture = "amd64"
        self.origins = [FakeOrigin(site=site)]
        self.sha256 = archive_sha256 or hashlib.sha256(f"{version}:{site}".encode()).hexdigest()
        self.size = 1024
        self.installed_size = 4096


class FakePackage:
    def __init__(self, state: dict[str, Any]) -> None:
        self.name = "nmap"
        self._state = state
        self.candidate = FakeVersion("7.94", site=state["site"], archive_sha256=state["archive_sha256"])
        self.versions = [self.candidate]
        self.installed = (
            None if state["installed"] is None else FakeVersion(state["installed"], site=state["site"])
        )
        self.marked_delete = False
        self.marked_downgrade = False
        self.marked_upgrade = False
        self.marked_reinstall = False
        self.marked_install = False

    def mark_install(self, **kwargs: Any) -> None:
        assert kwargs == {"auto_fix": True, "auto_inst": True, "from_user": True}
        self.marked_install = True


class FakeCache:
    def __init__(self, state: dict[str, Any]) -> None:
        self._state = state
        self.package = FakePackage(state)

    def __getitem__(self, name: str) -> FakePackage:
        if name not in {"nmap", "nmap:amd64"}:
            raise KeyError(name)
        return self.package

    def get_changes(self) -> list[FakePackage]:
        return [self.package] if self.package.marked_install else []

    @property
    def required_download(self) -> int:
        return 1024 if self.package.marked_install else 0

    @property
    def required_space(self) -> int:
        return 4096 if self.package.marked_install else 0

    def commit(self, *, allow_unauthenticated: bool) -> bool:
        assert allow_unauthenticated is False
        self._state["commit_calls"] += 1
        if self._state["commit_error"]:
            raise RuntimeError("simulated commit disconnect")
        self._state["installed"] = self.package.candidate.version
        return True


def backend_state(tmp_path: Path) -> tuple[dict[str, Any], PythonAptBackend]:
    state: dict[str, Any] = {
        "commit_calls": 0,
        "commit_error": False,
        "archive_sha256": None,
        "installed": None,
        "site": "archive.ubuntu.com",
    }
    backend = PythonAptBackend(
        cache_factory=lambda: FakeCache(state),
        manager_version="2.8.0",
        evidence_store=PackageEvidenceStore(tmp_path / "evidence"),
    )
    return state, backend


def test_python_apt_backend_commits_the_fresh_exact_plan_once(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))

    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.COMPLETED
    assert result.effect_boundary_crossed is True
    assert state["commit_calls"] == 1
    assert result.after[0].version == "7.94"
    assert planned.requested == (PackageRef("nmap", "7.94", "amd64"),)
    assert planned.changes[0].origins[0].trusted is True


def test_origin_drift_is_rejected_before_the_commit_boundary(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))
    state["site"] = "changed.example"

    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.FAILED_BEFORE_EFFECT
    assert result.error_code == "plan_drift"
    assert result.effect_boundary_crossed is False
    assert state["commit_calls"] == 0


def test_same_version_and_origin_with_changed_archive_hash_invalidates_approval(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))
    state["archive_sha256"] = "f" * 64

    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.FAILED_BEFORE_EFFECT
    assert result.error_code == "plan_drift"
    assert state["commit_calls"] == 0


def test_expired_request_never_crosses_the_commit_boundary(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))

    result = backend.execute_exact(planned, deadline=0)

    assert result.outcome is TransactionOutcome.FAILED_BEFORE_EFFECT
    assert result.error_code == "request_expired"
    assert state["commit_calls"] == 0


def test_commit_exception_is_unknown_and_is_never_retried_inside_the_backend(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))
    state["commit_error"] = True

    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.UNKNOWN
    assert result.effect_boundary_crossed is True
    assert result.error_code == "apt_commit_outcome_unknown"
    assert state["commit_calls"] == 1


def test_already_satisfied_receipt_snapshot_proves_the_exact_requested_version(tmp_path: Path) -> None:
    state, backend = backend_state(tmp_path)
    state["installed"] = "7.94"
    planned = backend.plan((PackageRef("nmap"),))
    assert planned.changes == ()

    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.ALREADY_SATISFIED
    assert result.effect_boundary_crossed is False
    assert result.before == result.after
    assert result.after[0].name == "nmap"
    assert result.after[0].version == "7.94"
    assert state["commit_calls"] == 0


def test_package_system_lock_failure_is_before_effect_and_never_commits(tmp_path: Path) -> None:
    state, planning_backend = backend_state(tmp_path)
    planned = planning_backend.plan((PackageRef("nmap"),))

    class RefusedLock:
        def __enter__(self) -> None:
            raise RuntimeError("lock already held")

        def __exit__(self, *args: Any) -> None:
            raise AssertionError("unacquired lock must not exit")

    backend = PythonAptBackend(
        cache_factory=lambda: FakeCache(state),
        system_lock_factory=RefusedLock,
        manager_version="2.8.0",
        evidence_store=PackageEvidenceStore(tmp_path / "locked-evidence"),
    )
    result = backend.execute_exact(planned, deadline=2**31)

    assert result.outcome is TransactionOutcome.FAILED_BEFORE_EFFECT
    assert result.effect_boundary_crossed is False
    assert result.error_code == "apt_lock_unavailable"
    assert state["commit_calls"] == 0


def test_completed_install_persists_bounded_raw_evidence_and_unit_changes(
    tmp_path: Path,
) -> None:
    state: dict[str, Any] = {
        "commit_calls": 0,
        "commit_error": False,
        "archive_sha256": None,
        "installed": None,
        "site": "archive.ubuntu.com",
    }
    dpkg_info = tmp_path / "dpkg-info"
    dpkg_info.mkdir(mode=0o700)
    package_list = dpkg_info / "nmap:amd64.list"
    package_list.write_text(
        "/usr/bin/nmap\n/usr/lib/systemd/system/nmap-helper.service\n",
        encoding="utf-8",
    )
    package_list.chmod(0o644)
    active = ServiceUnitState(
        load_state="loaded",
        unit_file_state="enabled",
        active_state="active",
        sub_state="running",
        active_enter_timestamp_monotonic=1234,
    )
    evidence_store = PackageEvidenceStore(tmp_path / "evidence")
    backend = PythonAptBackend(
        cache_factory=lambda: FakeCache(state),
        manager_version="2.8.0",
        evidence_store=evidence_store,
        dpkg_info_dir=dpkg_info,
        systemctl_query=lambda unit: active if unit == "nmap-helper.service" else active,
    )

    result = backend.execute_exact(backend.plan((PackageRef("nmap"),)), deadline=2**31)

    assert result.outcome is TransactionOutcome.COMPLETED
    assert result.output_capture_status == "captured"
    assert result.stdout_size_bytes == result.stderr_size_bytes == 0
    assert result.stdout_total_size_bytes == result.stderr_total_size_bytes == 0
    assert result.stdout_total_size_complete is result.stderr_total_size_complete is True
    assert result.service_unit_observation_status == "captured"
    assert len(result.service_unit_observations) == 1
    observation = result.service_unit_observations[0]
    assert observation.unit_name == "nmap-helper.service"
    assert observation.before is None
    assert observation.changes == tuple(
        sorted(
            {
                ServiceUnitChange.ENABLED,
                ServiceUnitChange.NEWLY_PRESENT,
                ServiceUnitChange.STARTED,
            },
            key=lambda item: item.value,
        )
    )
    assert {item.kind for item in result.evidence_refs} == {
        "apt_dpkg_transaction",
        "apt_stderr",
        "apt_stdout",
    }
    references = {item.kind: item for item in result.evidence_refs}
    for reference in result.evidence_refs:
        evidence_path = tmp_path / reference.ref
        evidence = evidence_path.read_bytes()
        assert hashlib.sha256(evidence).hexdigest() == reference.sha256
        assert len(evidence) == reference.size_bytes
        assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert (tmp_path / references["apt_stdout"].ref).read_bytes() == b""
    assert (tmp_path / references["apt_stderr"].ref).read_bytes() == b""
    payload = json.loads((tmp_path / references["apt_dpkg_transaction"].ref).read_bytes())
    assert payload["privacy"] == {
        "progress_callback_messages_retained": False,
        "raw_output_embedded_in_manifest": False,
        "raw_output_projected": False,
        "raw_output_retained_as_private_evidence": True,
        "schema": "bounded_raw_refs_v2",
    }
    assert payload["service_unit_observations"][0]["unit_name"] == "nmap-helper.service"


def test_raw_apt_bytes_are_exact_bounded_private_blobs_but_never_in_projection(
    tmp_path: Path,
) -> None:
    secret = b"do-not-project-this-secret"
    raw_stdout = secret + b"x" * (MAX_CAPTURE_BYTES_PER_STREAM + 32)
    sink = BoundedDigestSink()
    sink.feed(raw_stdout)
    capture = OutputCapture(
        status="captured",
        stdout_bytes=sink.retained_bytes,
        stderr_bytes=b"private-stderr",
        stdout_total_size_bytes=sink.total_size,
        stderr_total_size_bytes=len(b"private-stderr"),
        stdout_total_size_complete=sink.total_size_complete,
        stderr_total_size_complete=True,
    )
    store = PackageEvidenceStore(tmp_path / "evidence")

    references = store.persist_transaction(
        transaction_digest="a" * 64,
        outcome="unknown",
        error_code="apt_commit_outcome_unknown",
        output=capture,
        service_unit_observation_status="unavailable",
        service_unit_observations=(),
    )

    by_kind = {item.kind: item for item in references}
    stdout_ref = by_kind["apt_stdout"]
    stdout_path = tmp_path / stdout_ref.ref
    retained_stdout = stdout_path.read_bytes()
    assert retained_stdout == raw_stdout[:MAX_CAPTURE_BYTES_PER_STREAM]
    assert hashlib.sha256(retained_stdout).hexdigest() == stdout_ref.sha256 == sink.digest
    assert stdout_ref.size_bytes == MAX_CAPTURE_BYTES_PER_STREAM
    assert stat.S_IMODE(stdout_path.stat().st_mode) == 0o600
    stderr_path = tmp_path / by_kind["apt_stderr"].ref
    assert stderr_path.read_bytes() == b"private-stderr"
    assert stat.S_IMODE(stderr_path.stat().st_mode) == 0o600
    manifest_path = tmp_path / by_kind["apt_dpkg_transaction"].ref
    manifest = manifest_path.read_bytes()
    assert secret not in manifest
    assert b"private-stderr" not in manifest
    assert secret not in repr(capture).encode()
    assert secret not in json.dumps([item.to_payload() for item in references]).encode()
    payload = json.loads(manifest)
    assert payload["output"]["stdout"] == {
        "ref": stdout_ref.to_payload(),
        "retained_size_bytes": MAX_CAPTURE_BYTES_PER_STREAM,
        "total_size_bytes": len(raw_stdout),
        "total_size_complete": True,
        "truncated": True,
    }
    assert payload["output"]["truncated"] is True


def test_existing_content_addressed_blob_mismatch_is_rejected(tmp_path: Path) -> None:
    capture = OutputCapture(
        status="captured",
        stdout_bytes=b"exact stdout",
        stderr_bytes=b"exact stderr",
        stdout_total_size_bytes=12,
        stderr_total_size_bytes=12,
        stdout_total_size_complete=True,
        stderr_total_size_complete=True,
    )
    store = PackageEvidenceStore(tmp_path / "evidence")
    kwargs = {
        "transaction_digest": "a" * 64,
        "outcome": "completed",
        "error_code": None,
        "output": capture,
        "service_unit_observation_status": "captured",
        "service_unit_observations": (),
    }
    references = store.persist_transaction(**kwargs)  # type: ignore[arg-type]
    stdout_ref = next(item for item in references if item.kind == "apt_stdout")
    (tmp_path / stdout_ref.ref).write_bytes(b"mutated data")

    with pytest.raises(PackageEvidenceError, match="mismatches"):
        store.persist_transaction(**kwargs)  # type: ignore[arg-type]

    (tmp_path / stdout_ref.ref).write_bytes(b"exact stdout")
    (tmp_path / stdout_ref.ref).chmod(0o640)
    with pytest.raises(PackageEvidenceError, match="metadata is unsafe"):
        store.persist_transaction(**kwargs)  # type: ignore[arg-type]


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_production_install_progress_drains_child_streams_without_logging_raw_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInstallProgress:
        select_timeout = 0.01

        def __init__(self) -> None:
            status_read, status_write = os.pipe()
            self.status_stream = os.fdopen(status_read, "r")
            self.write_stream = os.fdopen(status_write, "w")
            self.child_pid = 0

        def __enter__(self) -> FakeInstallProgress:
            return self

        def __exit__(self, *_args: object) -> None:
            self.write_stream.close()
            self.status_stream.close()

        def update_interface(self) -> None:
            return None

    real_import = apt_backend_module.importlib.import_module
    monkeypatch.setattr(
        apt_backend_module.importlib,
        "import_module",
        lambda name: (
            SimpleNamespace(InstallProgress=FakeInstallProgress)
            if name == "apt.progress.base"
            else real_import(name)
        ),
    )
    progress = PythonAptBackend._capturing_install_progress()  # noqa: SLF001
    stdout = b"private-apt-output:" + b"x" * (MAX_CAPTURE_BYTES_PER_STREAM + 128)
    stderr = b"private-dpkg-error"
    with progress:
        pid = progress.fork()
        if pid == 0:  # pragma: no cover - assertions execute in the parent
            try:
                for descriptor, payload in ((1, stdout), (2, stderr)):
                    view = memoryview(payload)
                    while view:
                        view = view[os.write(descriptor, view) :]
            finally:
                os._exit(0)
        progress.child_pid = pid
        wait_status = progress.wait_child()
    capture = progress.capture_result()

    assert os.WEXITSTATUS(wait_status) == 0
    assert capture.status == "captured"
    assert capture.stdout_size_bytes == MAX_CAPTURE_BYTES_PER_STREAM
    assert capture.stderr_size_bytes == len(stderr)
    assert capture.stdout_total_size_bytes == len(stdout)
    assert capture.stderr_total_size_bytes == len(stderr)
    assert capture.stdout_total_size_complete is capture.stderr_total_size_complete is True
    assert capture.stdout_bytes == stdout[:MAX_CAPTURE_BYTES_PER_STREAM]
    assert capture.stderr_bytes == stderr
    assert capture.stdout_sha256 == hashlib.sha256(stdout[:MAX_CAPTURE_BYTES_PER_STREAM]).hexdigest()
    assert capture.stderr_sha256 == hashlib.sha256(stderr).hexdigest()
    assert capture.truncated is True


@pytest.mark.parametrize("failed_write", (1, 2, 3))
def test_any_evidence_blob_or_manifest_write_loss_after_commit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_write: int,
) -> None:
    state, planning_backend = backend_state(tmp_path)
    planned = planning_backend.plan((PackageRef("nmap"),))
    evidence_store = PackageEvidenceStore(tmp_path / f"failed-{failed_write}")
    real_write = evidence_store._write_content_addressed  # noqa: SLF001
    writes = 0

    def selectively_fail(destination: Path, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise PackageEvidenceError("simulated evidence loss")
        real_write(destination, payload)

    monkeypatch.setattr(evidence_store, "_write_content_addressed", selectively_fail)
    backend = PythonAptBackend(
        cache_factory=lambda: FakeCache(state),
        manager_version="2.8.0",
        evidence_store=evidence_store,
    )

    result = backend.execute_exact(planned, deadline=2**31)

    assert state["commit_calls"] == 1
    assert result.outcome is TransactionOutcome.UNKNOWN
    assert result.error_code == "evidence_persistence_failed"
    assert result.evidence_refs == ()


def test_completed_effect_without_durable_evidence_fails_closed_to_unknown(tmp_path: Path) -> None:
    state, planning_backend = backend_state(tmp_path)
    planned = planning_backend.plan((PackageRef("nmap"),))
    backend = PythonAptBackend(
        cache_factory=lambda: FakeCache(state),
        manager_version="2.8.0",
        evidence_store=None,
    )

    result = backend.execute_exact(planned, deadline=2**31)

    assert state["commit_calls"] == 1
    assert result.outcome is TransactionOutcome.UNKNOWN
    assert result.effect_boundary_crossed is True
    assert result.error_code == "evidence_persistence_failed"
    assert result.evidence_refs == ()


def test_reconciliation_only_observes_exact_pre_desired_and_mixed_states_without_commit(
    tmp_path: Path,
) -> None:
    state, backend = backend_state(tmp_path)
    planned = backend.plan((PackageRef("nmap"),))

    before = backend.reconcile_exact(planned)
    state["installed"] = "7.94"
    desired = backend.reconcile_exact(planned)
    state["installed"] = "7.93"
    mixed = backend.reconcile_exact(planned)

    assert before.postcondition_state is PackagePostconditionState.PRE_STATE
    assert before.installed == ()
    assert desired.postcondition_state is PackagePostconditionState.DESIRED
    assert desired.installed == (InstalledPackage("nmap", "7.94", "amd64"),)
    assert mixed.postcondition_state is PackagePostconditionState.MIXED
    assert mixed.installed == (InstalledPackage("nmap", "7.93", "amd64"),)
    assert state["commit_calls"] == 0


def test_reconciliation_snapshot_failure_is_honest_unavailable_without_commit(tmp_path: Path) -> None:
    state, planning_backend = backend_state(tmp_path)
    planned = planning_backend.plan((PackageRef("nmap"),))
    backend = PythonAptBackend(
        cache_factory=lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
        manager_version="2.8.0",
        evidence_store=PackageEvidenceStore(tmp_path / "unused-evidence"),
    )

    result = backend.reconcile_exact(planned)

    assert result.postcondition_state is PackagePostconditionState.UNAVAILABLE
    assert result.installed == ()
    assert state["commit_calls"] == 0


def test_service_unit_diff_records_restart_and_failure_without_free_form_status() -> None:
    key = ("nmap", "amd64", "nmap-helper.service")
    before = ServiceUnitState("loaded", "enabled", "active", "running", 100)
    restarted = ServiceUnitState("loaded", "enabled", "active", "running", 200)
    failed = ServiceUnitState("loaded", "enabled", "failed", "failed", 0)

    status, observations = apt_backend_module._unit_observation_diff(  # noqa: SLF001
        "captured",
        {key: before},
        "captured",
        {key: restarted},
    )
    failed_status, failed_observations = apt_backend_module._unit_observation_diff(  # noqa: SLF001
        "captured",
        {key: restarted},
        "captured",
        {key: failed},
    )

    assert status == failed_status == "captured"
    assert observations[0].changes == (ServiceUnitChange.RESTARTED,)
    assert failed_observations[0].changes == (ServiceUnitChange.FAILED,)
