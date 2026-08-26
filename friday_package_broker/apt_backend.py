"""Closed python-apt backend: in-memory planning and exact transaction commit."""

from __future__ import annotations

import errno
import fcntl
import importlib
import os
import select
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    MAX_SERVICE_UNIT_OBSERVATIONS,
    AptTransaction,
    BrokerContractError,
    InstalledPackage,
    PackageAction,
    PackageChange,
    PackageEvidenceReference,
    PackagePostconditionState,
    PackageRef,
    RepositoryOrigin,
    ServiceUnitChange,
    ServiceUnitObservation,
    ServiceUnitState,
    TransactionOutcome,
)
from .evidence import (
    MAX_CAPTURE_BYTES_PER_STREAM,
    MAX_PROGRESS_EVENTS,
    BoundedDigestSink,
    OutputCapture,
    PackageEvidenceError,
    PackageEvidenceStore,
)


class AptBackendError(RuntimeError):
    """A bounded backend failure whose exception text is never a public receipt."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AptBackendHealth:
    available: bool
    manager: str
    manager_version: str
    error_code: str | None = None
    dpkg_journal_dirty: bool = False
    broken_package_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "broken_package_count": self.broken_package_count,
            "dpkg_journal_dirty": self.dpkg_journal_dirty,
            "error_code": self.error_code,
            "manager": self.manager,
            "manager_version": self.manager_version,
        }


@dataclass(frozen=True, slots=True)
class AptExecutionResult:
    outcome: TransactionOutcome
    effect_boundary_crossed: bool
    started_at: int
    finished_at: int
    exit_code: int | None
    lock_state: str
    before: tuple[InstalledPackage, ...]
    after: tuple[InstalledPackage, ...]
    output_capture_status: str = "unavailable"
    stdout_sha256: str | None = None
    stdout_size_bytes: int | None = None
    stderr_sha256: str | None = None
    stderr_size_bytes: int | None = None
    output_truncated: bool = False
    reboot_required: bool = False
    stdout_total_size_bytes: int | None = None
    stderr_total_size_bytes: int | None = None
    stdout_total_size_complete: bool = False
    stderr_total_size_complete: bool = False
    evidence_refs: tuple[PackageEvidenceReference, ...] = ()
    service_unit_observation_status: str = "unavailable"
    service_unit_observations: tuple[ServiceUnitObservation, ...] = ()
    error_code: str | None = None
    manager_version: str = "unknown"
    observed_transaction_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TransactionOutcome):
            raise BrokerContractError("APT execution outcome is invalid")
        if not isinstance(self.effect_boundary_crossed, bool):
            raise BrokerContractError("APT execution effect marker is invalid")
        if (
            self.outcome
            in {
                TransactionOutcome.ALREADY_SATISFIED,
                TransactionOutcome.FAILED_BEFORE_EFFECT,
                TransactionOutcome.CANCELLED_BEFORE_COMMIT,
            }
            and self.effect_boundary_crossed
        ):
            raise BrokerContractError("APT execution outcome contradicts its effect marker")
        if self.outcome is TransactionOutcome.COMPLETED and not self.effect_boundary_crossed:
            raise BrokerContractError("completed APT execution lacks its effect marker")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.started_at, self.finished_at)
        ):
            raise BrokerContractError("APT execution numeric evidence is invalid")
        if self.started_at > self.finished_at:
            raise BrokerContractError("APT execution timestamps are invalid")
        if self.output_capture_status not in {"captured", "not_applicable", "unavailable"}:
            raise BrokerContractError("APT execution output capture status is invalid")
        if self.output_capture_status != "captured":
            if (
                any(
                    value is not None
                    for value in (
                        self.stdout_sha256,
                        self.stdout_size_bytes,
                        self.stdout_total_size_bytes,
                        self.stderr_sha256,
                        self.stderr_size_bytes,
                        self.stderr_total_size_bytes,
                    )
                )
                or self.stdout_total_size_complete
                or self.stderr_total_size_complete
                or self.output_truncated
            ):
                raise BrokerContractError("uncaptured APT output cannot claim evidence")
        else:
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (self.stdout_size_bytes, self.stderr_size_bytes)
            ):
                raise BrokerContractError("APT execution output sizes are invalid")
            assert self.stdout_size_bytes is not None and self.stderr_size_bytes is not None
            if not 0 <= self.stdout_size_bytes <= MAX_CAPTURE_BYTES_PER_STREAM:
                raise BrokerContractError("APT execution stdout evidence is invalid")
            if not 0 <= self.stderr_size_bytes <= MAX_CAPTURE_BYTES_PER_STREAM:
                raise BrokerContractError("APT execution stderr evidence is invalid")
            for retained, total, complete in (
                (
                    self.stdout_size_bytes,
                    self.stdout_total_size_bytes,
                    self.stdout_total_size_complete,
                ),
                (
                    self.stderr_size_bytes,
                    self.stderr_total_size_bytes,
                    self.stderr_total_size_complete,
                ),
            ):
                if (
                    isinstance(total, bool)
                    or not isinstance(total, int)
                    or not isinstance(complete, bool)
                    or total < retained
                    or total > 2**63 - 1
                ):
                    raise BrokerContractError("APT execution output total size is invalid")
                if (not complete or total > retained) and not self.output_truncated:
                    raise BrokerContractError("APT execution output completeness is contradictory")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not -255 <= self.exit_code <= 255
        ):
            raise BrokerContractError("APT execution exit code is invalid")
        if self.lock_state not in {"not_started", "held", "released", "unknown"}:
            raise BrokerContractError("APT execution lock evidence is invalid")
        if len(self.before) > 256 or len(self.after) > 256:
            raise BrokerContractError("APT execution package snapshot is oversized")
        for digest in (self.stdout_sha256, self.stderr_sha256):
            if digest is None and self.output_capture_status != "captured":
                continue
            assert digest is not None
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise BrokerContractError("APT execution output digest is invalid")
        if not isinstance(self.output_truncated, bool) or not isinstance(self.reboot_required, bool):
            raise BrokerContractError("APT execution boolean evidence is invalid")
        if (
            not isinstance(self.evidence_refs, tuple)
            or len(self.evidence_refs) > 8
            or any(not isinstance(item, PackageEvidenceReference) for item in self.evidence_refs)
            or len({item.ref for item in self.evidence_refs}) != len(self.evidence_refs)
        ):
            raise BrokerContractError("APT execution evidence references are invalid")
        evidence_by_kind = {item.kind: item for item in self.evidence_refs}
        expected_evidence_kinds = {"apt_dpkg_transaction", "apt_stderr", "apt_stdout"}
        if self.evidence_refs and (
            set(evidence_by_kind) != expected_evidence_kinds
            or len(evidence_by_kind) != len(self.evidence_refs)
        ):
            raise BrokerContractError("APT execution has incomplete raw-output evidence")
        if evidence_by_kind and (
            evidence_by_kind["apt_stdout"].sha256 != self.stdout_sha256
            or evidence_by_kind["apt_stdout"].size_bytes != self.stdout_size_bytes
            or evidence_by_kind["apt_stderr"].sha256 != self.stderr_sha256
            or evidence_by_kind["apt_stderr"].size_bytes != self.stderr_size_bytes
        ):
            raise BrokerContractError("APT execution raw-output evidence mismatches capture")
        if self.service_unit_observation_status not in {
            "captured",
            "not_applicable",
            "partial",
            "unavailable",
        }:
            raise BrokerContractError("APT execution service observation status is invalid")
        if (
            not isinstance(self.service_unit_observations, tuple)
            or len(self.service_unit_observations) > MAX_SERVICE_UNIT_OBSERVATIONS
            or any(not isinstance(item, ServiceUnitObservation) for item in self.service_unit_observations)
        ):
            raise BrokerContractError("APT execution service observations are invalid")
        if self.service_unit_observations and self.service_unit_observation_status not in {
            "captured",
            "partial",
        }:
            raise BrokerContractError("APT execution service observations contradict capture status")
        if self.outcome is TransactionOutcome.COMPLETED:
            if self.output_capture_status != "captured" or not evidence_by_kind:
                raise BrokerContractError("completed APT execution lacks bounded evidence")
            if self.service_unit_observation_status not in {"captured", "partial"}:
                raise BrokerContractError("completed APT execution lacks service observations")
        if self.error_code is not None and (
            not self.error_code
            or len(self.error_code) > 80
            or not self.error_code.replace("_", "a").isalnum()
        ):
            raise BrokerContractError("APT execution error code is invalid")
        if not isinstance(self.manager_version, str) or not 1 <= len(self.manager_version) <= 160:
            raise BrokerContractError("APT manager version is invalid")
        if self.observed_transaction_digest is not None and (
            len(self.observed_transaction_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.observed_transaction_digest)
        ):
            raise BrokerContractError("observed APT transaction digest is invalid")


@dataclass(frozen=True, slots=True)
class AptReconciliationResult:
    postcondition_state: PackagePostconditionState
    installed: tuple[InstalledPackage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.postcondition_state, PackagePostconditionState):
            raise BrokerContractError("APT reconciliation state is invalid")
        if (
            not isinstance(self.installed, tuple)
            or len(self.installed) > 256
            or any(not isinstance(item, InstalledPackage) for item in self.installed)
            or tuple(sorted(self.installed, key=lambda item: (item.name, item.architecture)))
            != self.installed
            or len({(item.name, item.architecture) for item in self.installed}) != len(self.installed)
        ):
            raise BrokerContractError("APT reconciliation snapshot is invalid")
        if self.postcondition_state is PackagePostconditionState.UNAVAILABLE and self.installed:
            raise BrokerContractError("unavailable APT reconciliation claims a snapshot")

    def is_consistent_with(self, transaction: AptTransaction) -> bool:
        if not isinstance(transaction, AptTransaction) or not transaction.changes:
            return False
        if self.postcondition_state is PackagePostconditionState.UNAVAILABLE:
            return not self.installed
        if _postcondition_matches(transaction, self.installed):
            expected = PackagePostconditionState.DESIRED
        elif _precondition_matches(transaction, self.installed):
            expected = PackagePostconditionState.PRE_STATE
        else:
            expected = PackagePostconditionState.MIXED
        return self.postcondition_state is expected


class AptBackend(Protocol):
    def health(self) -> AptBackendHealth: ...

    def plan(self, requested: tuple[PackageRef, ...]) -> AptTransaction: ...

    def execute_exact(
        self, transaction: AptTransaction, *, deadline: int | None = None
    ) -> AptExecutionResult: ...

    def reconcile_exact(self, transaction: AptTransaction) -> AptReconciliationResult: ...


class PythonAptBackend:
    """Use only candidates exposed by the host's already configured APT cache."""

    def __init__(
        self,
        *,
        cache_factory: Callable[[], Any] | None = None,
        system_lock_factory: Callable[[], Any] | None = None,
        manager_version: str | None = None,
        reboot_required_path: str | Path = "/var/run/reboot-required",
        evidence_store: PackageEvidenceStore | None = None,
        dpkg_info_dir: str | Path = "/var/lib/dpkg/info",
        systemctl_query: Callable[[str], ServiceUnitState] | None = None,
    ) -> None:
        self._cache_factory = cache_factory
        self._system_lock_factory = system_lock_factory
        self._manager_version = manager_version
        self._reboot_required_path = Path(reboot_required_path)
        self._evidence_store = evidence_store
        self._dpkg_info_dir = Path(dpkg_info_dir)
        self._systemctl_query = systemctl_query
        self._execution_lock = threading.Lock()

    def health(self) -> AptBackendHealth:
        try:
            version = self._version()
            cache = self._new_cache()
            dirty = bool(getattr(cache, "dpkg_journal_dirty", False))
            broken = max(0, int(getattr(cache, "broken_count", 0)))
        except AptBackendError as exc:
            return AptBackendHealth(
                available=False,
                manager="apt",
                manager_version=self._manager_version or "unavailable",
                error_code=exc.code,
            )
        if dirty or broken:
            return AptBackendHealth(
                available=False,
                manager="apt",
                manager_version=version,
                error_code="dpkg_journal_dirty" if dirty else "apt_cache_broken",
                dpkg_journal_dirty=dirty,
                broken_package_count=broken,
            )
        return AptBackendHealth(
            available=True,
            manager="apt",
            manager_version=version,
            error_code=None,
        )

    def plan(self, requested: tuple[PackageRef, ...]) -> AptTransaction:
        _validate_request_set(requested)
        _cache, transaction = self._prepare_cache(requested)
        return transaction

    def reconcile_exact(self, transaction: AptTransaction) -> AptReconciliationResult:
        """Observe only the exact transaction package set; never plan or commit."""

        if not isinstance(transaction, AptTransaction) or not transaction.changes:
            return AptReconciliationResult(PackagePostconditionState.UNAVAILABLE, ())
        if not self._execution_lock.acquire(blocking=False):
            return AptReconciliationResult(PackagePostconditionState.UNAVAILABLE, ())
        try:
            try:
                with self._system_lock():
                    cache = self._new_cache()
                    observed = self._reconciliation_snapshot(cache, transaction)
            except Exception:
                return AptReconciliationResult(PackagePostconditionState.UNAVAILABLE, ())
            if _postcondition_matches(transaction, observed):
                state = PackagePostconditionState.DESIRED
            elif _precondition_matches(transaction, observed):
                state = PackagePostconditionState.PRE_STATE
            else:
                state = PackagePostconditionState.MIXED
            return AptReconciliationResult(state, observed)
        finally:
            self._execution_lock.release()

    def execute_exact(
        self, transaction: AptTransaction, *, deadline: int | None = None
    ) -> AptExecutionResult:
        started = int(time.time())
        if not isinstance(transaction, AptTransaction):
            raise AptBackendError("invalid_transaction")
        if not self._execution_lock.acquire(blocking=False):
            return self._failure_before_effect(started, "broker_execution_busy", before=(), after=())
        try:
            if deadline is not None and int(time.time()) >= deadline:
                return self._failure_before_effect(
                    started,
                    "request_expired",
                    before=(),
                    after=(),
                )
            effect_boundary_crossed = False
            lock_acquired = False
            before: tuple[InstalledPackage, ...] = ()
            fresh_digest: str | None = None
            capture = OutputCapture.unavailable()
            before_unit_status = "unavailable"
            before_units: dict[tuple[str, str, str], ServiceUnitState] = {}
            try:
                # Hold the host package-system lock from the final resolve through
                # postcondition capture. Cache.commit nests this lock safely.
                with self._system_lock():
                    lock_acquired = True
                    if deadline is not None and int(time.time()) >= deadline:
                        return self._failure_before_effect(started, "request_expired", before=(), after=())
                    try:
                        cache, fresh = self._prepare_cache(transaction.requested)
                    except AptBackendError as exc:
                        return self._failure_before_effect(started, exc.code, before=(), after=())
                    fresh_digest = fresh.digest
                    before = self._snapshot(cache, fresh)
                    if fresh.digest != transaction.digest:
                        return self._failure_before_effect(
                            started,
                            "plan_drift",
                            before=before,
                            after=before,
                            observed_transaction_digest=fresh.digest,
                        )
                    if deadline is not None and int(time.time()) >= deadline:
                        return self._failure_before_effect(
                            started,
                            "request_expired",
                            before=before,
                            after=before,
                            observed_transaction_digest=fresh.digest,
                        )
                    if not transaction.changes:
                        if not _postcondition_matches(transaction, before):
                            return self._failure_before_effect(
                                started,
                                "package_postcondition_failed",
                                before=before,
                                after=before,
                                observed_transaction_digest=fresh.digest,
                            )
                        return AptExecutionResult(
                            outcome=TransactionOutcome.ALREADY_SATISFIED,
                            effect_boundary_crossed=False,
                            started_at=started,
                            finished_at=int(time.time()),
                            exit_code=0,
                            lock_state="released",
                            before=before,
                            after=before,
                            output_capture_status="not_applicable",
                            reboot_required=self._reboot_required(),
                            service_unit_observation_status="not_applicable",
                            manager_version=self._version(),
                            observed_transaction_digest=fresh.digest,
                        )

                    before_unit_status, before_units = self._unit_snapshot(transaction, after=False)
                    # Override host APT configuration explicitly: even a machine
                    # with AllowUnauthenticated enabled cannot weaken this broker.
                    effect_boundary_crossed = True
                    if self._cache_factory is None:
                        progress = self._capturing_install_progress()
                        try:
                            with progress:
                                committed = cache.commit(
                                    install_progress=progress,
                                    allow_unauthenticated=False,
                                )
                        finally:
                            capture = progress.capture_result()
                    else:
                        capture = OutputCapture.empty()
                        committed = cache.commit(allow_unauthenticated=False)
                        # Synthetic/cache-injected backends have no dpkg child.  An
                        # empty captured stream is still exact evidence, not an
                        # assertion that production output was discarded.
                    if committed is not True:
                        raise AptBackendError("apt_commit_incomplete")
                    observed_cache = self._new_cache()
                    after = self._snapshot(observed_cache, transaction)
                    postcondition_ok = _postcondition_matches(transaction, after)
            except Exception:
                if effect_boundary_crossed:
                    return self._unknown_after_boundary(
                        started,
                        before,
                        transaction,
                        fresh_digest or transaction.digest,
                        capture=capture,
                        before_unit_status=before_unit_status,
                        before_units=before_units,
                    )
                return self._failure_before_effect(
                    started,
                    "apt_lock_unavailable" if not lock_acquired else "apt_preflight_failed",
                    before=before,
                    after=before,
                    observed_transaction_digest=fresh_digest,
                )
            after_unit_status, after_units = self._unit_snapshot(transaction, after=True)
            unit_status, unit_observations = _unit_observation_diff(
                before_unit_status,
                before_units,
                after_unit_status,
                after_units,
            )
            return self._effect_result(
                outcome=(TransactionOutcome.COMPLETED if postcondition_ok else TransactionOutcome.UNKNOWN),
                error_code=None if postcondition_ok else "package_postcondition_failed",
                started=started,
                exit_code=0,
                lock_state="released",
                before=before,
                after=after,
                transaction_digest=fresh_digest or transaction.digest,
                capture=capture,
                service_unit_observation_status=unit_status,
                service_unit_observations=unit_observations,
            )
        finally:
            self._execution_lock.release()

    def _prepare_cache(self, requested: tuple[PackageRef, ...]) -> tuple[Any, AptTransaction]:
        try:
            cache = self._new_cache()
            if bool(getattr(cache, "dpkg_journal_dirty", False)):
                raise AptBackendError("dpkg_journal_dirty")
            if int(getattr(cache, "broken_count", 0)) != 0:
                raise AptBackendError("apt_cache_broken")
            for reference in sorted(
                requested, key=lambda item: (item.name, item.architecture or "", item.version or "")
            ):
                package = self._package(cache, reference)
                candidate = self._candidate(package, reference)
                architecture = _architecture(candidate)
                PackageRef(reference.name, str(candidate.version), architecture)
                installed = getattr(package, "installed", None)
                if installed is not None and str(installed.version) == str(candidate.version):
                    continue
                if _is_held(package):
                    raise AptBackendError("held_package_conflict")
                package.candidate = candidate
                package.mark_install(auto_fix=True, auto_inst=True, from_user=True)

            changed_packages = tuple(cache.get_changes())
            if any(_is_held(item) for item in changed_packages):
                raise AptBackendError("held_package_conflict")
            if int(getattr(cache, "broken_count", 0)) != 0:
                raise AptBackendError("apt_dependency_resolution_failed")
            changes = tuple(
                sorted(
                    (self._change(item) for item in changed_packages),
                    key=lambda item: (item.name, item.architecture),
                )
            )
            resolved = tuple(
                self._resolved_request(cache, reference)
                for reference in sorted(
                    requested,
                    key=lambda item: (item.name, item.architecture or "", item.version or ""),
                )
            )
            transaction = AptTransaction(
                schema_version=1,
                requested=resolved,
                changes=changes,
                download_bytes=_bounded_integer(
                    getattr(cache, "required_download", 0), "APT required download"
                ),
                installed_delta_bytes=_bounded_signed_integer(
                    getattr(cache, "required_space", 0), "APT required space"
                ),
                warnings=(),
            )
            return cache, transaction
        except AptBackendError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, SystemError) as exc:
            raise AptBackendError("apt_resolution_failed") from exc

    def _new_cache(self) -> Any:
        try:
            if self._cache_factory is not None:
                cache = self._cache_factory()
            else:
                apt_module = importlib.import_module("apt")
                cache = apt_module.Cache()
            if cache is None or not hasattr(cache, "get_changes"):
                raise AptBackendError("apt_cache_unavailable")
            return cache
        except AptBackendError:
            raise
        except (ImportError, AttributeError, OSError, SystemError) as exc:
            raise AptBackendError("python_apt_unavailable") from exc

    def _system_lock(self) -> Any:
        if self._system_lock_factory is not None:
            return self._system_lock_factory()
        if self._cache_factory is not None:
            return nullcontext()
        try:
            apt_pkg = importlib.import_module("apt_pkg")
            # Never inherit an operator-wide lock wait that can outlive a signed
            # request. A busy package manager fails immediately before effects.
            apt_pkg.config.set("DPkg::Lock::Timeout", "0")
            return apt_pkg.SystemLock()
        except (ImportError, AttributeError) as exc:
            raise AptBackendError("python_apt_unavailable") from exc

    @staticmethod
    def _capturing_install_progress() -> Any:
        try:
            base = importlib.import_module("apt.progress.base")
        except (ImportError, AttributeError) as exc:
            raise AptBackendError("python_apt_progress_unavailable") from exc

        class CapturingInstallProgress(base.InstallProgress):  # type: ignore[name-defined]
            """Drain dpkg output, retaining exact bounded prefixes and event counts."""

            def __init__(self) -> None:
                super().__init__()
                self._stdout_read, self._stdout_write = os.pipe2(os.O_CLOEXEC)
                self._stderr_read, self._stderr_write = os.pipe2(os.O_CLOEXEC)
                self._stdout_sink = BoundedDigestSink()
                self._stderr_sink = BoundedDigestSink()
                self._event_counts = {
                    "conffile": 0,
                    "dpkg_status": 0,
                    "error": 0,
                    "processing": 0,
                    "status": 0,
                }
                self._event_counts_truncated = False

            def _count(self, kind: str) -> None:
                if self._event_counts[kind] >= MAX_PROGRESS_EVENTS:
                    self._event_counts_truncated = True
                self._event_counts[kind] = min(
                    MAX_PROGRESS_EVENTS,
                    self._event_counts[kind] + 1,
                )

            def error(self, pkg: str, errormsg: str) -> None:
                del pkg, errormsg
                self._count("error")

            def conffile(self, current: str, new: str) -> None:
                del current, new
                self._count("conffile")

            def status_change(self, pkg: str, percent: float, status: str) -> None:
                del pkg, percent, status
                self._count("status")

            def dpkg_status_change(self, pkg: str, status: str) -> None:
                del pkg, status
                self._count("dpkg_status")

            def processing(self, pkg: str, stage: str) -> None:
                del pkg, stage
                self._count("processing")

            def fork(self) -> int:
                pid = os.fork()
                if pid == 0:
                    try:
                        os.close(self._stdout_read)
                        os.close(self._stderr_read)
                        os.dup2(self._stdout_write, 1)
                        os.dup2(self._stderr_write, 2)
                    finally:
                        if self._stdout_write > 2:
                            os.close(self._stdout_write)
                        if self._stderr_write > 2:
                            os.close(self._stderr_write)
                else:
                    os.close(self._stdout_write)
                    os.close(self._stderr_write)
                    self._stdout_write = -1
                    self._stderr_write = -1
                    for descriptor in (self._stdout_read, self._stderr_read):
                        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
                        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                return pid

            def _drain(self, descriptor: int, sink: BoundedDigestSink) -> bool:
                if descriptor < 0:
                    return False
                open_stream = True
                while True:
                    try:
                        chunk = os.read(descriptor, 64 * 1024)
                    except OSError as exc:
                        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            break
                        raise
                    if not chunk:
                        open_stream = False
                        break
                    sink.feed(chunk)
                return open_stream

            def wait_child(self) -> int:
                result = 0
                child_finished = False
                stdout_open = True
                stderr_open = True
                while not child_finished:
                    watched: list[Any] = [self.status_stream]
                    if stdout_open:
                        watched.append(self._stdout_read)
                    if stderr_open:
                        watched.append(self._stderr_read)
                    try:
                        ready, _write, _error = select.select(
                            watched,
                            [],
                            [],
                            self.select_timeout,
                        )
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            continue
                        raise
                    if self.status_stream in ready:
                        self.update_interface()
                    if self._stdout_read in ready:
                        stdout_open = self._drain(self._stdout_read, self._stdout_sink)
                    if self._stderr_read in ready:
                        stderr_open = self._drain(self._stderr_read, self._stderr_sink)
                    try:
                        pid, result = os.waitpid(self.child_pid, os.WNOHANG)
                        child_finished = pid == self.child_pid
                    except OSError as exc:
                        if exc.errno == errno.ECHILD:
                            child_finished = True
                        elif exc.errno != errno.EINTR:
                            raise
                # Do not wait for a maintainer-script grandchild that retained an
                # inherited descriptor. Drain what is already buffered and close;
                # an open writer is recorded as truncation, never as completeness.
                if self._drain(self._stdout_read, self._stdout_sink):
                    self._stdout_sink.mark_truncated()
                if self._drain(self._stderr_read, self._stderr_sink):
                    self._stderr_sink.mark_truncated()
                return result

            def __exit__(self, type_: object, value: object, traceback: object) -> None:
                try:
                    super().__exit__(type_, value, traceback)
                finally:
                    for field in (
                        "_stdout_read",
                        "_stdout_write",
                        "_stderr_read",
                        "_stderr_write",
                    ):
                        descriptor = int(getattr(self, field, -1))
                        if descriptor >= 0:
                            with suppress(OSError):
                                os.close(descriptor)
                            setattr(self, field, -1)

            def capture_result(self) -> OutputCapture:
                return OutputCapture(
                    status="captured",
                    stdout_bytes=self._stdout_sink.retained_bytes,
                    stderr_bytes=self._stderr_sink.retained_bytes,
                    stdout_total_size_bytes=self._stdout_sink.total_size,
                    stderr_total_size_bytes=self._stderr_sink.total_size,
                    stdout_total_size_complete=self._stdout_sink.total_size_complete,
                    stderr_total_size_complete=self._stderr_sink.total_size_complete,
                    progress_event_counts=tuple(
                        (kind, count) for kind, count in sorted(self._event_counts.items()) if count
                    ),
                    progress_events_truncated=self._event_counts_truncated,
                )

        return CapturingInstallProgress()

    def _unit_snapshot(
        self,
        transaction: AptTransaction,
        *,
        after: bool,
    ) -> tuple[str, dict[tuple[str, str, str], ServiceUnitState]]:
        states: dict[tuple[str, str, str], ServiceUnitState] = {}
        incomplete = False
        selected = tuple(
            (change.name, change.architecture)
            for change in transaction.changes
            if (change.to_version if after else change.from_version) is not None
        )
        for package_name, architecture in selected:
            try:
                unit_names = self._package_unit_names(package_name, architecture)
            except PackageEvidenceError:
                incomplete = True
                continue
            for unit_name in unit_names:
                if len(states) >= MAX_SERVICE_UNIT_OBSERVATIONS:
                    incomplete = True
                    break
                try:
                    state = (
                        self._systemctl_query(unit_name)
                        if self._systemctl_query is not None
                        else self._query_systemctl(unit_name)
                    )
                    if not isinstance(state, ServiceUnitState):
                        raise PackageEvidenceError("systemctl query returned an invalid state")
                except (
                    BrokerContractError,
                    OSError,
                    PackageEvidenceError,
                    subprocess.SubprocessError,
                ):
                    incomplete = True
                    continue
                states[(package_name, architecture, unit_name)] = state
        return ("partial" if incomplete else "captured"), states

    def _package_unit_names(self, package_name: str, architecture: str) -> tuple[str, ...]:
        PackageRef(package_name, architecture=architecture)
        candidates = (
            self._dpkg_info_dir / f"{package_name}:{architecture}.list",
            self._dpkg_info_dir / f"{package_name}.list",
        )
        descriptor = -1
        for candidate in candidates:
            try:
                descriptor = os.open(
                    candidate,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                )
                break
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PackageEvidenceError("dpkg package file list is unavailable") from exc
        if descriptor < 0:
            raise PackageEvidenceError("dpkg package file list is missing")
        maximum = 2 * 1024 * 1024
        try:
            observed = os.fstat(descriptor)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or observed.st_size > maximum
            ):
                raise PackageEvidenceError("dpkg package file list metadata is unsafe")
            payload = bytearray()
            while len(payload) <= maximum:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > maximum:
                raise PackageEvidenceError("dpkg package file list is oversized")
        except OSError as exc:
            raise PackageEvidenceError("dpkg package file list could not be read") from exc
        finally:
            os.close(descriptor)
        allowed_parents = (
            "/etc/systemd/system/",
            "/lib/systemd/system/",
            "/usr/lib/systemd/system/",
        )
        units: set[str] = set()
        for raw_line in bytes(payload).splitlines():
            if len(raw_line) > 512:
                continue
            try:
                path = raw_line.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
            if not path.startswith(allowed_parents):
                continue
            unit_name = path.rsplit("/", 1)[-1]
            try:
                # The public contract owns the complete unit-name grammar.
                ServiceUnitObservation(
                    package_name=package_name,
                    package_architecture=architecture,
                    unit_name=unit_name,
                    before=None,
                    after=ServiceUnitState("unknown", "unknown", "unknown", "unknown", 0),
                    changes=(ServiceUnitChange.NEWLY_PRESENT,),
                )
            except BrokerContractError:
                continue
            units.add(unit_name)
            if len(units) > MAX_SERVICE_UNIT_OBSERVATIONS:
                raise PackageEvidenceError("package owns too many systemd units")
        return tuple(sorted(units))

    @staticmethod
    def _query_systemctl(unit_name: str) -> ServiceUnitState:
        executable = None
        for candidate in (Path("/usr/bin/systemctl"), Path("/bin/systemctl")):
            try:
                observed = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if (
                stat.S_ISREG(observed.st_mode)
                and observed.st_uid == 0
                and not observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                and observed.st_mode & stat.S_IXUSR
            ):
                executable = candidate
                break
        if executable is None:
            raise PackageEvidenceError("systemctl executable is unavailable")
        command = [
            str(executable),
            "--no-pager",
            "show",
            "--property=LoadState",
            "--property=UnitFileState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=ActiveEnterTimestampMonotonic",
            unit_name,
        ]
        process = subprocess.Popen(  # noqa: S603 - fixed root-owned executable and closed argv
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "SYSTEMD_COLORS": "0",
                "SYSTEMD_PAGER": "",
            },
        )
        try:
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            deadline = time.monotonic() + 2.0
            recovered = bytearray()
            reached_eof = False
            while not reached_eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PackageEvidenceError("systemctl query timed out")
                ready, _write, _error = select.select([descriptor], [], [], min(0.1, remaining))
                if descriptor in ready:
                    try:
                        chunk = os.read(descriptor, 4097 - len(recovered))
                    except OSError as exc:
                        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                            continue
                        raise
                    if not chunk:
                        reached_eof = True
                    else:
                        recovered.extend(chunk)
                        if len(recovered) > 4096:
                            raise PackageEvidenceError("systemctl response is oversized")
                elif process.poll() is not None:
                    # The writer is closed after exit; one final readiness pass
                    # drains any bytes already buffered by the kernel.
                    continue
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            payload = bytes(recovered)
        except BaseException:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
            raise
        try:
            values = {}
            for line in payload.decode("ascii", errors="strict").splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {
                    "ActiveEnterTimestampMonotonic",
                    "ActiveState",
                    "LoadState",
                    "SubState",
                    "UnitFileState",
                }:
                    values[key] = value
            timestamp = int(values.get("ActiveEnterTimestampMonotonic") or "0")
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackageEvidenceError("systemctl response is invalid") from exc
        normalize = lambda value: str(value or "unknown").strip().casefold().replace("-", "_")  # noqa: E731
        return ServiceUnitState(
            load_state=normalize(values.get("LoadState")),
            unit_file_state=normalize(values.get("UnitFileState")),
            active_state=normalize(values.get("ActiveState")),
            sub_state=normalize(values.get("SubState")),
            active_enter_timestamp_monotonic=timestamp,
        )

    def _effect_result(
        self,
        *,
        outcome: TransactionOutcome,
        error_code: str | None,
        started: int,
        exit_code: int | None,
        lock_state: str,
        before: tuple[InstalledPackage, ...],
        after: tuple[InstalledPackage, ...],
        transaction_digest: str,
        capture: OutputCapture,
        service_unit_observation_status: str,
        service_unit_observations: tuple[ServiceUnitObservation, ...],
    ) -> AptExecutionResult:
        evidence_refs: tuple[PackageEvidenceReference, ...] = ()
        final_outcome = outcome
        final_error = error_code
        if self._evidence_store is not None:
            try:
                evidence_refs = self._evidence_store.persist_transaction(
                    transaction_digest=transaction_digest,
                    outcome=outcome.value,
                    error_code=error_code,
                    output=capture,
                    service_unit_observation_status=service_unit_observation_status,
                    service_unit_observations=service_unit_observations,
                )
            except PackageEvidenceError:
                final_outcome = TransactionOutcome.UNKNOWN
                final_error = "evidence_persistence_failed"
        else:
            final_outcome = TransactionOutcome.UNKNOWN
            final_error = "evidence_persistence_failed"
        return AptExecutionResult(
            outcome=final_outcome,
            effect_boundary_crossed=True,
            started_at=started,
            finished_at=int(time.time()),
            exit_code=exit_code,
            lock_state=lock_state,
            before=before,
            after=after,
            output_capture_status=capture.status,
            stdout_sha256=capture.stdout_sha256,
            stdout_size_bytes=capture.stdout_size_bytes,
            stderr_sha256=capture.stderr_sha256,
            stderr_size_bytes=capture.stderr_size_bytes,
            output_truncated=capture.truncated,
            reboot_required=self._reboot_required(),
            stdout_total_size_bytes=capture.stdout_total_size_bytes,
            stderr_total_size_bytes=capture.stderr_total_size_bytes,
            stdout_total_size_complete=capture.stdout_total_size_complete,
            stderr_total_size_complete=capture.stderr_total_size_complete,
            evidence_refs=evidence_refs,
            service_unit_observation_status=service_unit_observation_status,
            service_unit_observations=service_unit_observations,
            error_code=final_error,
            manager_version=self._version(),
            observed_transaction_digest=transaction_digest,
        )

    @staticmethod
    def _resolved_request(cache: Any, reference: PackageRef) -> PackageRef:
        package = PythonAptBackend._package(cache, reference)
        marked = any(
            bool(getattr(package, field, False))
            for field in (
                "marked_install",
                "marked_upgrade",
                "marked_downgrade",
                "marked_reinstall",
            )
        )
        target = getattr(package, "candidate", None) if marked else getattr(package, "installed", None)
        if target is None:
            raise AptBackendError("requested_package_not_resolved")
        resolved = PackageRef(reference.name, str(target.version), _architecture(target))
        if reference.version is not None and resolved.version != reference.version:
            raise AptBackendError("requested_version_resolution_changed")
        if reference.architecture is not None and resolved.architecture != reference.architecture:
            raise AptBackendError("requested_architecture_resolution_changed")
        return resolved

    @staticmethod
    def _package(cache: Any, reference: PackageRef) -> Any:
        names: tuple[str, ...]
        if reference.architecture is None:
            names = (reference.name,)
        else:
            names = (f"{reference.name}:{reference.architecture}", reference.name)
        for name in names:
            try:
                package = cache[name]
            except KeyError:
                continue
            candidate = getattr(package, "candidate", None)
            if candidate is not None and (
                reference.architecture is None
                or _architecture(candidate) == reference.architecture
                or any(_architecture(item) == reference.architecture for item in package.versions)
            ):
                return package
        raise AptBackendError("package_not_found")

    @staticmethod
    def _candidate(package: Any, reference: PackageRef) -> Any:
        candidates = tuple(getattr(package, "versions", ()))
        candidate = getattr(package, "candidate", None)
        if (
            reference.version is None
            and candidate is not None
            and (reference.architecture is None or _architecture(candidate) == reference.architecture)
        ):
            return candidate
        if reference.version is not None:
            candidates = tuple(item for item in candidates if str(item.version) == reference.version)
        if reference.architecture is not None:
            candidates = tuple(item for item in candidates if _architecture(item) == reference.architecture)
        if candidates:
            return candidates[0]
        if candidate is None:
            raise AptBackendError("package_candidate_unavailable")
        if reference.version is not None and str(candidate.version) != reference.version:
            raise AptBackendError("package_version_unavailable")
        if reference.architecture is not None and _architecture(candidate) != reference.architecture:
            raise AptBackendError("package_architecture_unavailable")
        return candidate

    @staticmethod
    def _change(package: Any) -> PackageChange:
        installed = getattr(package, "installed", None)
        candidate = getattr(package, "candidate", None)
        if bool(getattr(package, "marked_delete", False)):
            action = PackageAction.REMOVE
            version = installed
        elif installed is None:
            action = PackageAction.INSTALL
            version = candidate
        elif candidate is not None:
            comparison = _debian_version_compare(str(candidate.version), str(installed.version))
            if comparison < 0:
                action = PackageAction.DOWNGRADE
            elif comparison > 0:
                action = PackageAction.UPGRADE
            elif bool(
                getattr(package, "marked_reinstall", False) or getattr(package, "marked_install", False)
            ):
                action = PackageAction.REINSTALL
            else:
                raise AptBackendError("unclassified_package_change")
            version = candidate
        else:
            raise AptBackendError("unclassified_package_change")
        if version is None:
            raise AptBackendError("package_change_lacks_version")
        from_version = None if installed is None else str(installed.version)
        to_version = None if action is PackageAction.REMOVE else str(version.version)
        installed_size = (
            0
            if installed is None
            else _bounded_integer(getattr(installed, "installed_size", 0), "installed package size")
        )
        target_size = (
            0
            if action is PackageAction.REMOVE
            else _bounded_integer(getattr(version, "installed_size", 0), "target package size")
        )
        return PackageChange(
            action=action,
            name=str(package.name).split(":", 1)[0],
            architecture=_architecture(version),
            from_version=from_version,
            to_version=to_version,
            download_bytes=0
            if action is PackageAction.REMOVE
            else _bounded_integer(getattr(version, "size", 0), "package download size"),
            installed_delta_bytes=target_size - installed_size,
            archive_sha256=None if action is PackageAction.REMOVE else _archive_sha256(version),
            origins=()
            if action is PackageAction.REMOVE
            else tuple(
                sorted(
                    (_origin(item) for item in getattr(version, "origins", ())),
                    key=lambda item: (
                        item.origin,
                        item.label,
                        item.archive,
                        item.site,
                        item.component,
                    ),
                )
            ),
        )

    @staticmethod
    def _snapshot(cache: Any, transaction: AptTransaction) -> tuple[InstalledPackage, ...]:
        observed: list[InstalledPackage] = []
        targets = {(item.name, item.architecture or "") for item in transaction.requested} | {
            (item.name, item.architecture) for item in transaction.changes
        }
        for name, architecture in sorted(targets):
            try:
                package = cache[f"{name}:{architecture}"]
            except KeyError:
                try:
                    package = cache[name]
                except KeyError:
                    continue
            installed = getattr(package, "installed", None)
            if installed is None:
                continue
            observed.append(
                InstalledPackage(
                    name=name,
                    version=str(installed.version),
                    architecture=_architecture(installed),
                )
            )
        return tuple(sorted(observed, key=lambda item: (item.name, item.architecture)))

    @staticmethod
    def _reconciliation_snapshot(cache: Any, transaction: AptTransaction) -> tuple[InstalledPackage, ...]:
        observed: list[InstalledPackage] = []
        targets = {(item.name, item.architecture or "") for item in transaction.requested} | {
            (item.name, item.architecture) for item in transaction.changes
        }
        for name, architecture in sorted(targets):
            try:
                package = cache[f"{name}:{architecture}"]
            except KeyError:
                try:
                    package = cache[name]
                except KeyError as exc:
                    raise AptBackendError("package_state_unavailable") from exc
            installed = getattr(package, "installed", None)
            if installed is None:
                continue
            observed.append(
                InstalledPackage(
                    name=name,
                    version=str(installed.version),
                    architecture=_architecture(installed),
                )
            )
        return tuple(sorted(observed, key=lambda item: (item.name, item.architecture)))

    def _failure_before_effect(
        self,
        started: int,
        code: str,
        *,
        before: tuple[InstalledPackage, ...],
        after: tuple[InstalledPackage, ...],
        observed_transaction_digest: str | None = None,
    ) -> AptExecutionResult:
        return AptExecutionResult(
            outcome=TransactionOutcome.FAILED_BEFORE_EFFECT,
            effect_boundary_crossed=False,
            started_at=started,
            finished_at=int(time.time()),
            exit_code=None,
            lock_state="not_started",
            before=before,
            after=after,
            reboot_required=self._reboot_required(),
            error_code=code,
            manager_version=self._version(),
            observed_transaction_digest=observed_transaction_digest,
        )

    def _unknown_after_boundary(
        self,
        started: int,
        before: tuple[InstalledPackage, ...],
        transaction: AptTransaction,
        observed_transaction_digest: str,
        *,
        capture: OutputCapture,
        before_unit_status: str,
        before_units: dict[tuple[str, str, str], ServiceUnitState],
    ) -> AptExecutionResult:
        after: tuple[InstalledPackage, ...] = ()
        after_unit_status = "unavailable"
        after_units: dict[tuple[str, str, str], ServiceUnitState] = {}
        try:
            cache = self._new_cache()
            after = self._snapshot(cache, transaction)
            after_unit_status, after_units = self._unit_snapshot(transaction, after=True)
        except AptBackendError:
            pass
        unit_status, unit_observations = _unit_observation_diff(
            before_unit_status,
            before_units,
            after_unit_status,
            after_units,
        )
        return self._effect_result(
            outcome=TransactionOutcome.UNKNOWN,
            error_code="apt_commit_outcome_unknown",
            started=started,
            exit_code=None,
            lock_state="unknown",
            before=before,
            after=after,
            transaction_digest=observed_transaction_digest,
            capture=capture,
            service_unit_observation_status=unit_status,
            service_unit_observations=unit_observations,
        )

    def _version(self) -> str:
        if self._manager_version:
            return self._manager_version
        try:
            apt_pkg = importlib.import_module("apt_pkg")
            value = str(apt_pkg.VERSION)
        except (ImportError, AttributeError) as exc:
            if self._cache_factory is not None:
                return "test-backend"
            raise AptBackendError("python_apt_unavailable") from exc
        if not value or len(value) > 160:
            raise AptBackendError("apt_version_unavailable")
        self._manager_version = value
        return value

    def _reboot_required(self) -> bool:
        try:
            return self._reboot_required_path.is_file()
        except OSError:
            return False


def _validate_request_set(requested: tuple[PackageRef, ...]) -> None:
    if not isinstance(requested, tuple) or not requested or len(requested) > 16:
        raise AptBackendError("invalid_package_request")
    if any(not isinstance(item, PackageRef) for item in requested):
        raise AptBackendError("invalid_package_request")
    keys = {(item.name, item.architecture) for item in requested}
    if len(keys) != len(requested):
        raise AptBackendError("duplicate_package_request")


def _architecture(version: Any) -> str:
    value = getattr(version, "architecture", None)
    if callable(value):
        value = value()
    if not isinstance(value, str) or not value:
        raise AptBackendError("package_architecture_unavailable")
    return value


def _origin(value: Any) -> RepositoryOrigin:
    return RepositoryOrigin(
        origin=str(getattr(value, "origin", "")),
        label=str(getattr(value, "label", "")),
        archive=str(getattr(value, "archive", "")),
        site=str(getattr(value, "site", "")),
        component=str(getattr(value, "component", "")),
        trusted=getattr(value, "trusted", None) is True,
    )


def _archive_sha256(version: Any) -> str:
    value = getattr(version, "sha256", None)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AptBackendError("package_archive_hash_unavailable")
    return value


def _is_held(package: Any) -> bool:
    if bool(getattr(package, "is_held", False)):
        return True
    internal = getattr(package, "_pkg", None)
    selected = getattr(internal, "selected_state", None)
    if selected is None:
        return False
    try:
        apt_pkg = importlib.import_module("apt_pkg")
        return selected == apt_pkg.SELSTATE_HOLD
    except (ImportError, AttributeError):
        return False


def _bounded_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AptBackendError("apt_numeric_value_invalid")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AptBackendError("apt_numeric_value_invalid") from exc
    if not 0 <= number <= 2**40:
        raise AptBackendError("apt_numeric_value_invalid")
    return number


def _debian_version_compare(left: str, right: str) -> int:
    try:
        apt_pkg = importlib.import_module("apt_pkg")
        return int(apt_pkg.version_compare(left, right))
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise AptBackendError("apt_version_compare_unavailable") from exc


def _bounded_signed_integer(value: Any, field: str) -> int:
    del field
    if isinstance(value, bool):
        raise AptBackendError("apt_numeric_value_invalid")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AptBackendError("apt_numeric_value_invalid") from exc
    if not -(2**40) <= number <= 2**40:
        raise AptBackendError("apt_numeric_value_invalid")
    return number


def _postcondition_matches(transaction: AptTransaction, observed: tuple[InstalledPackage, ...]) -> bool:
    installed = {(item.name, item.architecture): item.version for item in observed}
    for requested in transaction.requested:
        assert requested.architecture is not None and requested.version is not None
        if installed.get((requested.name, requested.architecture)) != requested.version:
            return False
    for change in transaction.changes:
        key = (change.name, change.architecture)
        if change.action is PackageAction.REMOVE:
            if key in installed:
                return False
        elif installed.get(key) != change.to_version:
            return False
    return True


def _precondition_matches(transaction: AptTransaction, observed: tuple[InstalledPackage, ...]) -> bool:
    installed = {(item.name, item.architecture): item.version for item in observed}
    for change in transaction.changes:
        key = (change.name, change.architecture)
        if change.from_version is None:
            if key in installed:
                return False
        elif installed.get(key) != change.from_version:
            return False
    return True


def _unit_observation_diff(
    before_status: str,
    before: dict[tuple[str, str, str], ServiceUnitState],
    after_status: str,
    after: dict[tuple[str, str, str], ServiceUnitState],
) -> tuple[str, tuple[ServiceUnitObservation, ...]]:
    if before_status == after_status == "captured":
        status = "captured"
    elif before_status == after_status == "unavailable":
        status = "unavailable"
    else:
        status = "partial"
    observations: list[ServiceUnitObservation] = []
    for package_name, architecture, unit_name in sorted(set(before) | set(after)):
        old = before.get((package_name, architecture, unit_name))
        new = after.get((package_name, architecture, unit_name))
        changes: set[ServiceUnitChange] = set()
        if new is not None:
            if new.load_state == "loaded" and (old is None or old.load_state != "loaded"):
                changes.add(ServiceUnitChange.NEWLY_PRESENT)
            if new.unit_file_state in {"enabled", "enabled_runtime"} and (
                old is None or old.unit_file_state not in {"enabled", "enabled_runtime"}
            ):
                changes.add(ServiceUnitChange.ENABLED)
            if new.active_state == "active" and (old is None or old.active_state != "active"):
                changes.add(ServiceUnitChange.STARTED)
            if (
                old is not None
                and old.active_state == new.active_state == "active"
                and old.active_enter_timestamp_monotonic > 0
                and new.active_enter_timestamp_monotonic > 0
                and old.active_enter_timestamp_monotonic != new.active_enter_timestamp_monotonic
            ):
                changes.add(ServiceUnitChange.RESTARTED)
            if new.active_state == "failed":
                changes.add(ServiceUnitChange.FAILED)
        if not changes:
            continue
        observations.append(
            ServiceUnitObservation(
                package_name=package_name,
                package_architecture=architecture,
                unit_name=unit_name,
                before=old,
                after=new,
                changes=tuple(sorted(changes, key=lambda item: item.value)),
            )
        )
        if len(observations) == MAX_SERVICE_UNIT_OBSERVATIONS:
            if len(set(before) | set(after)) > len(observations):
                status = "partial"
            break
    return status, tuple(observations)


__all__ = [
    "AptBackend",
    "AptBackendError",
    "AptBackendHealth",
    "AptExecutionResult",
    "AptReconciliationResult",
    "PythonAptBackend",
]
