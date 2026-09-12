#!/usr/bin/env python3
"""Run the hosted quality gate in one verified, owned transient user service."""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Protocol

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
MAX_RUNTIME_SECONDS = 10_200
CGROUP_ROOT = Path("/sys/fs/cgroup")
INFRA_EXIT = 125
MANAGER_TIMEOUT = 4.0
STOP_TIMEOUT = 8.0
ACQUIRE_TIMEOUT = 8.0
TERM_GRACE = 3.0
KILL_GRACE = 3.0
CONTROLLER_GRACE = 3.0
READY_TIMEOUT = 15.0
POLL_INTERVAL = 0.05
MAX_MANAGER_OUTPUT = 65536
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_UNIT_COMPONENT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Description",
    "InvocationID",
    "ControlGroup",
    "KillMode",
    "Transient",
    "FragmentPath",
)


class HostedGateError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class InvocationIdentity:
    unit: str
    description: str


@dataclasses.dataclass(frozen=True)
class UnitState:
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    description: str
    invocation_id: str
    control_group: str
    kill_mode: str
    transient: str
    fragment_path: str

    @property
    def absent(self) -> bool:
        return self.load_state == "not-found"


@dataclasses.dataclass(frozen=True)
class OwnedUnit:
    identity: InvocationIdentity
    invocation_id: str
    control_group: str

    def matches(self, state: UnitState) -> bool:
        control_group_matches = bool(
            state.control_group == self.control_group
            or (state.control_group == "" and state.active_state in {"inactive", "failed"})
        )
        return bool(
            not state.absent
            and state.unit == self.identity.unit
            and state.description == self.identity.description
            and state.invocation_id == self.invocation_id
            and control_group_matches
            and state.kill_mode == "control-group"
            and state.transient == "yes"
            and state.fragment_path == f"/run/user/{os.geteuid()}/systemd/transient/{state.unit}"
        )

    def matches_live_cgroup(self, state: UnitState) -> bool:
        return bool(self.matches(state) and state.control_group == self.control_group)


@dataclasses.dataclass(frozen=True)
class GateConfig:
    command: tuple[str, ...]
    log_path: Path
    runner_temp: Path
    run_id: str
    run_attempt: str
    runtime_max_sec: int = MAX_RUNTIME_SECONDS
    timeout_stop_sec: int = 6


class Manager(Protocol):
    def inspect(self, unit: str) -> UnitState: ...
    def kill(self, unit: str, selected: signal.Signals) -> bool: ...
    def stop(self, unit: str) -> bool: ...
    def reset_failed(self, unit: str) -> bool: ...
    def cgroup_empty(self, control_group: str) -> bool: ...


def _parse_show(raw: bytes, *, expected_unit: str) -> UnitState:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise HostedGateError("SYSTEMD_STATE_INVALID") from exc
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in parsed:
            raise HostedGateError("SYSTEMD_STATE_INVALID")
        parsed[key] = value
    if set(parsed) != set(_SHOW_PROPERTIES) or parsed["Id"] != expected_unit:
        raise HostedGateError("SYSTEMD_STATE_INVALID")
    return UnitState(
        unit=parsed["Id"],
        load_state=parsed["LoadState"],
        active_state=parsed["ActiveState"],
        sub_state=parsed["SubState"],
        description=parsed["Description"],
        invocation_id=parsed["InvocationID"].casefold(),
        control_group=parsed["ControlGroup"],
        kill_mode=parsed["KillMode"],
        transient=parsed["Transient"],
        fragment_path=parsed["FragmentPath"],
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


class OutputSink:
    def __init__(self, log_path: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._fd = os.open(log_path, flags, 0o600)
        metadata = os.fstat(self._fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            os.close(self._fd)
            raise HostedGateError("LOG_PATH_UNSAFE")
        os.fchmod(self._fd, 0o600)
        self._lock = threading.Lock()
        self.error: BaseException | None = None

    def write(self, payload: bytes) -> None:
        if not payload:
            return
        try:
            with self._lock:
                _write_all(self._fd, payload)
                _write_all(sys.stdout.fileno(), payload)
        except BaseException as exc:
            self.error = exc

    def diagnostic(self, code: str) -> None:
        if re.fullmatch(r"[A-Z0-9_]+", code) is None:
            code = "INTERNAL_DIAGNOSTIC_INVALID"
        self.write(f"FRIDAY_HOSTED_GATE {code}\n".encode("ascii"))

    def close(self) -> None:
        with self._lock:
            os.fsync(self._fd)
            os.close(self._fd)


class OutputPump(threading.Thread):
    def __init__(self, source: BinaryIO, sink: OutputSink) -> None:
        super().__init__(name="hosted-quality-output", daemon=True)
        self._source = source
        self._sink = sink
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while True:
                chunk = self._source.read(65536)
                if not chunk:
                    return
                self._sink.write(chunk)
                if self._sink.error is not None:
                    raise HostedGateError("OUTPUT_CAPTURE_FAILED")
        except BaseException as exc:
            self.error = exc
        finally:
            with suppress(BaseException):
                self._source.close()


class SystemdManager:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)

    def _run(self, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=self._environment,
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostedGateError("MANAGER_CALL_FAILED") from exc
        if len(completed.stdout) > MAX_MANAGER_OUTPUT or len(completed.stderr) > MAX_MANAGER_OUTPUT:
            raise HostedGateError("MANAGER_OUTPUT_TOO_LARGE")
        return completed

    def inspect(self, unit: str) -> UnitState:
        argv = [SYSTEMCTL, "--user", "show", "--no-pager"]
        argv.extend(f"--property={name}" for name in _SHOW_PROPERTIES)
        argv.append(unit)
        completed = self._run(argv, MANAGER_TIMEOUT)
        if completed.returncode != 0:
            raise HostedGateError("SYSTEMD_STATE_UNAVAILABLE")
        return _parse_show(completed.stdout, expected_unit=unit)

    def kill(self, unit: str, selected: signal.Signals) -> bool:
        completed = self._run(
            [
                SYSTEMCTL,
                "--user",
                "kill",
                "--kill-whom=all",
                f"--signal={selected.name}",
                unit,
            ],
            MANAGER_TIMEOUT,
        )
        return completed.returncode == 0

    def stop(self, unit: str) -> bool:
        completed = self._run(
            [SYSTEMCTL, "--user", "stop", unit],
            STOP_TIMEOUT,
        )
        return completed.returncode == 0

    def reset_failed(self, unit: str) -> bool:
        completed = self._run(
            [SYSTEMCTL, "--user", "reset-failed", unit],
            MANAGER_TIMEOUT,
        )
        return completed.returncode == 0

    def cgroup_empty(self, control_group: str) -> bool:
        if not control_group.startswith("/") or "\x00" in control_group:
            raise HostedGateError("CGROUP_IDENTITY_INVALID")
        try:
            root = CGROUP_ROOT.resolve(strict=True)
            path = (root / control_group.lstrip("/")).resolve(strict=False)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HostedGateError("CGROUP_IDENTITY_INVALID") from exc
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise HostedGateError("CGROUP_STATE_UNAVAILABLE") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostedGateError("CGROUP_STATE_UNAVAILABLE")
        try:
            events = (path / "cgroup.events").read_text(encoding="ascii")
            populated = [
                pieces[1]
                for line in events.splitlines()
                if len(pieces := line.split()) == 2 and pieces[0] == "populated"
            ]
            if populated != ["0"] and populated != ["1"]:
                raise HostedGateError("CGROUP_STATE_UNAVAILABLE")
            for name in ("cgroup.procs", "cgroup.threads"):
                member_path = path / name
                if not member_path.is_file():
                    raise HostedGateError("CGROUP_STATE_UNAVAILABLE")
                if member_path.read_text(encoding="ascii").strip():
                    return False
            return populated == ["0"]
        except FileNotFoundError:
            try:
                os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                return True
            except OSError as exc:
                raise HostedGateError("CGROUP_STATE_UNAVAILABLE") from exc
            raise HostedGateError("CGROUP_STATE_UNAVAILABLE") from None
        except HostedGateError:
            raise
        except (OSError, UnicodeError) as exc:
            raise HostedGateError("CGROUP_STATE_UNAVAILABLE") from exc


class SignalLatch:
    def __init__(self) -> None:
        self.received: signal.Signals | None = None
        self._previous: dict[signal.Signals, object] = {}

    def _handler(self, signum: int, _frame: object) -> None:
        if self.received is None:
            self.received = signal.Signals(signum)

    def install(self) -> None:
        for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self._previous[selected] = signal.getsignal(selected)
            signal.signal(selected, self._handler)

    def restore(self) -> None:
        for selected, previous in self._previous.items():
            signal.signal(selected, previous)
        self._previous.clear()


def _new_identity(run_id: str, run_attempt: str) -> InvocationIdentity:
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise HostedGateError("RUN_ID_INVALID")
    if re.fullmatch(r"[1-9][0-9]*", run_attempt) is None:
        raise HostedGateError("RUN_ATTEMPT_INVALID")
    nonce = secrets.token_hex(12)
    owner = secrets.token_hex(16)
    unit = f"friday-quality-{run_id}-{run_attempt}-{nonce}.service"
    if len(unit) > 240 or _UNIT_COMPONENT.fullmatch(unit.removesuffix(".service")) is None:
        raise HostedGateError("UNIT_IDENTITY_INVALID")
    return InvocationIdentity(
        unit=unit,
        description=f"Friday quality gate owner {owner}",
    )


def _has_creation_identity(identity: InvocationIdentity, state: UnitState) -> bool:
    return bool(
        state.unit == identity.unit
        and state.load_state == "loaded"
        and state.description == identity.description
        and state.kill_mode == "control-group"
        and state.transient == "yes"
        and state.fragment_path == f"/run/user/{os.geteuid()}/systemd/transient/{state.unit}"
    )


def _owned_from_state(identity: InvocationIdentity, state: UnitState) -> OwnedUnit | None:
    if (
        state.unit == identity.unit
        and state.load_state == "loaded"
        and state.active_state in {"activating", "active", "deactivating"}
        and state.description == identity.description
        and _HEX32.fullmatch(state.invocation_id) is not None
        and state.control_group.startswith("/")
        and state.kill_mode == "control-group"
        and state.transient == "yes"
        and state.fragment_path == f"/run/user/{os.geteuid()}/systemd/transient/{state.unit}"
    ):
        return OwnedUnit(identity, state.invocation_id, state.control_group)
    return None


def _environment_names(environment: Mapping[str, str]) -> tuple[str, ...]:
    names = tuple(sorted(name for name in environment if _ENV_NAME.fullmatch(name) is not None))
    if not names or len(names) > 512:
        raise HostedGateError("ENVIRONMENT_NAMES_INVALID")
    return names


def _build_launch(
    config: GateConfig,
    identity: InvocationIdentity,
    birth_path: Path,
    ready_path: Path,
    environment: Mapping[str, str],
) -> list[str]:
    if not config.command or not os.path.isabs(config.command[0]):
        raise HostedGateError("COMMAND_INVALID")
    if not (1 <= config.timeout_stop_sec <= 30):
        raise HostedGateError("STOP_TIMEOUT_INVALID")
    if not (1 <= config.runtime_max_sec <= MAX_RUNTIME_SECONDS):
        raise HostedGateError("RUNTIME_MAX_INVALID")
    argv = [
        SYSTEMD_RUN,
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        "--no-ask-password",
        "--service-type=exec",
        "--same-dir",
        "--expand-environment=no",
        f"--unit={identity.unit}",
        f"--description={identity.description}",
        "--property=KillMode=control-group",
        f"--property=RuntimeMaxSec={config.runtime_max_sec}s",
        f"--property=TimeoutStopSec={config.timeout_stop_sec}s",
    ]
    argv.extend(f"--setenv={name}" for name in _environment_names(environment))
    argv.extend(
        (
            "--",
            str(Path(sys.executable).resolve(strict=True)),
            "-I",
            "-B",
            str(Path(__file__).resolve(strict=True)),
            "_child",
            "--birth-file",
            str(birth_path),
            "--ready-file",
            str(ready_path),
            "--ready-timeout",
            str(int(READY_TIMEOUT)),
            "--",
            *config.command,
        )
    )
    return argv


def _create_private_marker(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_private_marker(path: Path, payload: bytes) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostedGateError("HANDSHAKE_INVALID") from exc
    try:
        metadata = os.fstat(descriptor)
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not (stat.S_IMODE(metadata.st_mode) & 0o077)
            and os.read(descriptor, len(payload) + 1) == payload
        )
    finally:
        os.close(descriptor)


def _child_wait_and_exec(
    birth_file: Path,
    ready_file: Path,
    timeout: float,
    command: Sequence[str],
) -> int:
    if timeout <= 0 or timeout > 30 or not command or not os.path.isabs(command[0]):
        return INFRA_EXIT
    try:
        _create_private_marker(birth_file, b"BORN\n")
    except (OSError, HostedGateError):
        return INFRA_EXIT
    deadline = time.monotonic() + timeout
    while True:
        try:
            ready = _verify_private_marker(ready_file, b"READY\n")
        except HostedGateError:
            return INFRA_EXIT
        if not ready:
            if time.monotonic() >= deadline:
                return INFRA_EXIT
            time.sleep(POLL_INTERVAL)
            continue
        try:
            os.execvpe(command[0], list(command), os.environ)
        except OSError:
            return 126


def _probe_cleanup(manager: Manager, owner: OwnedUnit) -> tuple[bool, UnitState | None]:
    state = manager.inspect(owner.identity.unit)
    if state.absent:
        return manager.cgroup_empty(owner.control_group), state
    if not owner.matches(state):
        return False, state
    empty = manager.cgroup_empty(owner.control_group)
    return bool(empty and state.active_state == "inactive" and state.sub_state in {"dead", "exited"}), state


def _settle(
    manager: Manager,
    owner: OwnedUnit,
    *,
    deadline: float,
    allow_reset: bool,
) -> bool:
    reset_attempted = False
    while True:
        try:
            clear, state = _probe_cleanup(manager, owner)
        except BaseException:
            return False
        if clear:
            return True
        if state is not None and not state.absent and not owner.matches(state):
            return False
        if (
            allow_reset
            and not reset_attempted
            and state is not None
            and owner.matches(state)
            and state.active_state == "failed"
        ):
            try:
                if manager.cgroup_empty(owner.control_group):
                    reset_attempted = True
                    manager.reset_failed(owner.identity.unit)
            except BaseException:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def _mutate_if_owned(
    manager: Manager,
    owner: OwnedUnit,
    operation: str,
    selected: signal.Signals | None = None,
) -> bool:
    try:
        state = manager.inspect(owner.identity.unit)
        if state.absent:
            return manager.cgroup_empty(owner.control_group)
        if not owner.matches(state):
            return False
        if not owner.matches_live_cgroup(state):
            return False
        if operation == "kill" and selected is not None:
            return manager.kill(owner.identity.unit, selected)
        if operation == "stop":
            return manager.stop(owner.identity.unit)
    except BaseException:
        return False
    return False


def cleanup_owned(manager: Manager, owner: OwnedUnit, *, cancellation: bool) -> bool:
    now = time.monotonic()
    if not cancellation and _settle(
        manager,
        owner,
        deadline=now + 1.0,
        allow_reset=True,
    ):
        return True

    _mutate_if_owned(manager, owner, "kill", signal.SIGTERM)
    _mutate_if_owned(manager, owner, "stop")
    if _settle(
        manager,
        owner,
        deadline=time.monotonic() + TERM_GRACE,
        allow_reset=True,
    ):
        return True

    _mutate_if_owned(manager, owner, "kill", signal.SIGKILL)
    _mutate_if_owned(manager, owner, "stop")
    return _settle(
        manager,
        owner,
        deadline=time.monotonic() + KILL_GRACE,
        allow_reset=True,
    )


def _stop_local_controller(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=CONTROLLER_GRACE)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=CONTROLLER_GRACE)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return False
    return process.poll() is not None


def _normalize_status(returncode: int | None, *, fallback: int = INFRA_EXIT) -> int:
    if returncode is None:
        return fallback
    if returncode < 0:
        value = 128 + abs(returncode)
        return value if value <= 255 else fallback
    if returncode > 255:
        return fallback
    return returncode


def _final_status(original: int, cleanup_confirmed: bool) -> int:
    if cleanup_confirmed:
        return original
    return original if original != 0 else INFRA_EXIT


def run_gate(
    config: GateConfig,
    *,
    identity: InvocationIdentity | None = None,
    manager: Manager | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    selected_environment = dict(os.environ if environment is None else environment)
    selected_manager = manager or SystemdManager(selected_environment)
    chosen = identity or _new_identity(config.run_id, config.run_attempt)
    os.umask(0o077)
    config.runner_temp.mkdir(mode=0o700, parents=True, exist_ok=True)
    ready_dir = Path(tempfile.mkdtemp(prefix="friday-quality-ready-", dir=config.runner_temp))
    os.chmod(ready_dir, 0o700)
    birth_file = ready_dir / "born"
    ready_file = ready_dir / "ready"
    sink = OutputSink(config.log_path)
    latch = SignalLatch()
    process: subprocess.Popen[bytes] | None = None
    pump: OutputPump | None = None
    owner: OwnedUnit | None = None
    original = INFRA_EXIT
    cleanup_confirmed = False
    foreign_collision = False

    try:
        latch.install()
        initial = selected_manager.inspect(chosen.unit)
        if not initial.absent:
            foreign_collision = True
            sink.diagnostic("COLLISION_NO_OWNERSHIP")
            return INFRA_EXIT
        if latch.received is not None:
            return 128 + int(latch.received)

        launch = _build_launch(
            config,
            chosen,
            birth_file,
            ready_file,
            selected_environment,
        )
        process = subprocess.Popen(
            launch,
            cwd=Path.cwd(),
            env=selected_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None:
            raise HostedGateError("OUTPUT_PIPE_UNAVAILABLE")
        pump = OutputPump(process.stdout, sink)
        pump.start()

        acquisition_deadline = time.monotonic() + ACQUIRE_TIMEOUT
        while owner is None:
            if process.poll() is not None:
                break
            try:
                observed = selected_manager.inspect(chosen.unit)
            except BaseException:
                observed = None
            if observed is not None and not observed.absent:
                candidate = _owned_from_state(chosen, observed)
                if candidate is None:
                    if not _has_creation_identity(chosen, observed):
                        foreign_collision = True
                        break
                    # StartTransientUnit may expose the registered unit before
                    # InvocationID and ControlGroup exist. Keep the acquisition
                    # deadline; this state never grants mutation authority.
                else:
                    try:
                        born = _verify_private_marker(birth_file, b"BORN\n")
                    except HostedGateError:
                        foreign_collision = True
                        break
                    if born:
                        owner = candidate
                        break
            if time.monotonic() >= acquisition_deadline:
                break
            time.sleep(POLL_INTERVAL)

        if owner is None:
            original = (
                128 + int(latch.received) if latch.received is not None else _normalize_status(process.poll())
            )
            if process.poll() is None:
                _stop_local_controller(process)
            if foreign_collision:
                sink.diagnostic("COLLISION_NO_OWNERSHIP")
            else:
                sink.diagnostic("STOP_UNCONFIRMED")
            return _final_status(original, False)

        if latch.received is None:
            _create_private_marker(ready_file, b"READY\n")

        while process.poll() is None and latch.received is None:
            if pump.error is not None or sink.error is not None:
                break
            time.sleep(POLL_INTERVAL)

        drain_unconfirmed = False
        if process.poll() is not None and latch.received is None:
            pump.join(timeout=CONTROLLER_GRACE)
            drain_unconfirmed = pump.is_alive()
            if drain_unconfirmed:
                sink.diagnostic("OUTPUT_DRAIN_UNCONFIRMED")

        cancellation = bool(
            latch.received is not None
            or pump.error is not None
            or sink.error is not None
            or drain_unconfirmed
        )
        if latch.received is not None:
            original = 128 + int(latch.received)
        elif pump.error is not None or sink.error is not None or drain_unconfirmed:
            original = INFRA_EXIT
        else:
            original = _normalize_status(process.poll())

        cleanup_confirmed = cleanup_owned(
            selected_manager,
            owner,
            cancellation=cancellation,
        )
        if cleanup_confirmed:
            _stop_local_controller(process)
        else:
            sink.diagnostic("STOP_UNCONFIRMED")
            _stop_local_controller(process)

        return _final_status(original, cleanup_confirmed)
    except BaseException:
        if owner is not None:
            cleanup_confirmed = cleanup_owned(selected_manager, owner, cancellation=True)
        if not cleanup_confirmed:
            sink.diagnostic("STOP_UNCONFIRMED")
        if process is not None:
            _stop_local_controller(process)
        return original if original != 0 else INFRA_EXIT
    finally:
        latch.restore()
        if pump is not None:
            pump.join(timeout=CONTROLLER_GRACE)
            if pump.is_alive():
                sink.diagnostic("OUTPUT_DRAIN_UNCONFIRMED")
        with suppress(BaseException):
            sink.close()
        try:
            if ready_file.exists():
                ready_file.unlink()
            if birth_file.exists():
                birth_file.unlink()
            ready_dir.rmdir()
        except OSError:
            pass


def _parse_gate(argv: Sequence[str]) -> GateConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", required=True, type=Path)
    parser.add_argument("--runner-temp", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--runtime-max-sec", type=int, default=MAX_RUNTIME_SECONDS)
    parser.add_argument("--timeout-stop-sec", type=int, default=6)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv))
    command = tuple(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return GateConfig(
        command=command,
        log_path=args.log_path,
        runner_temp=args.runner_temp,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        runtime_max_sec=args.runtime_max_sec,
        timeout_stop_sec=args.timeout_stop_sec,
    )


def _main(argv: Sequence[str]) -> int:
    if argv and argv[0] == "_child":
        parser = argparse.ArgumentParser()
        parser.add_argument("--birth-file", required=True, type=Path)
        parser.add_argument("--ready-file", required=True, type=Path)
        parser.add_argument("--ready-timeout", required=True, type=float)
        parser.add_argument("command", nargs=argparse.REMAINDER)
        args = parser.parse_args(list(argv[1:]))
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        return _child_wait_and_exec(
            args.birth_file,
            args.ready_file,
            args.ready_timeout,
            command,
        )
    return run_gate(_parse_gate(argv))


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
