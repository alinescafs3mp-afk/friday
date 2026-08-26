"""Out-of-process spawn broker. posix_spawn of bwrap never runs on a worker thread.

Broker process stays single-threaded and execs a per-job helper. The helper is
the parent of bwrap, waitpids it, and reports the exit status on a control
socket. The kernel is not the parent and must not waitpid.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# High numbers so posix_spawn DUP2 destinations do not clobber SCM_RIGHTS
# source fds (which typically land at 3+ in the broker).
HELPER_STDIN = 20
HELPER_STDOUT = 21
HELPER_STDERR = 22
HELPER_EXEC = 23
HELPER_EXPORT = 24
HELPER_READY = 25
HELPER_GO = 26
HELPER_EXIT = 27
HELPER_SCRIPT = 28
BWRAP_EXEC_FD = 3
BWRAP_SCRIPT_FD = 4
BWRAP_BLOCK_FD = 5
BWRAP_EXPORT_FD = 6
_RECV_MAX = 512 * 1024
_MAX_FDS = 16


def _pid_starttime(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _send_json_fd(fd: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _recv_json_fd(fd: int, *, timeout: float | None = None) -> dict[str, Any]:
    buf = bytearray()
    deadline = None if timeout is None else time.monotonic() + timeout
    while b"\n" not in buf:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining is not None:
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError("spawn helper control timeout")
        chunk = os.read(fd, 4096)
        if not chunk:
            raise EOFError("spawn helper control closed")
        buf.extend(chunk)
        if len(buf) > _RECV_MAX:
            raise ValueError("spawn helper control overflow")
    line, _, rest = bytes(buf).partition(b"\n")
    if rest:
        raise ValueError("spawn helper trailing data")
    payload = json.loads(line.decode("ascii"))
    if not isinstance(payload, dict):
        raise ValueError("spawn helper invalid payload")
    return payload


def _kill_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _move_cgroup(cgroup: str, pid: int) -> None:
    procs = os.path.join(cgroup, "cgroup.procs")
    expected = cgroup.removeprefix("/sys/fs/cgroup")
    deadline = time.monotonic() + 1.0
    last = ""
    while time.monotonic() < deadline:
        try:
            fd = os.open(procs, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.write(fd, f"{int(pid)}\n".encode("ascii"))
            finally:
                os.close(fd)
        except OSError as exc:
            last = str(exc)
            time.sleep(0.02)
            continue
        try:
            raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
        except OSError as exc:
            last = str(exc)
            time.sleep(0.02)
            continue
        relative = ""
        for line in raw.splitlines():
            if line.startswith("0::"):
                relative = line[3:]
                break
        if relative == expected or relative.startswith(expected.rstrip("/") + "/"):
            return
        last = relative
        time.sleep(0.02)
    raise RuntimeError(f"cgroup {last!r} != {expected!r}")


def _job_main() -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    raw = sys.argv[2] if len(sys.argv) > 2 else ""
    req = json.loads(raw)
    has_script = bool(req.get("has_script"))
    launcher = str(req["launcher"])
    argv = [str(item) for item in req["argv"]]
    env = {str(k): str(v) for k, v in dict(req["env"]).items()}
    cgroup = str(req["cgroup"])
    fsize = int(req["fsize"])
    block_r, block_w = os.pipe()
    os.set_inheritable(block_r, True)
    child_pid = 0
    try:
        actions: list[tuple] = [
            (os.POSIX_SPAWN_DUP2, HELPER_STDIN, 0),
            (os.POSIX_SPAWN_DUP2, HELPER_STDOUT, 1),
            (os.POSIX_SPAWN_DUP2, HELPER_STDERR, 2),
            (os.POSIX_SPAWN_DUP2, HELPER_EXEC, BWRAP_EXEC_FD),
        ]
        if has_script:
            actions.append((os.POSIX_SPAWN_DUP2, HELPER_SCRIPT, BWRAP_SCRIPT_FD))
        else:
            actions.append((os.POSIX_SPAWN_CLOSE, BWRAP_SCRIPT_FD))
        actions.append((os.POSIX_SPAWN_DUP2, block_r, BWRAP_BLOCK_FD))
        actions.append((os.POSIX_SPAWN_DUP2, HELPER_EXPORT, BWRAP_EXPORT_FD))
        actions.append((os.POSIX_SPAWN_CLOSEFROM, 7))
        child_pid = os.posix_spawn(launcher, argv, env, file_actions=actions)
        os.close(block_r)
        block_r = -1
        try:
            resource.prlimit(child_pid, resource.RLIMIT_FSIZE, (fsize, fsize))
        except (OSError, ValueError) as exc:
            _kill_pid(child_pid)
            _send_json_fd(HELPER_READY, {"error": "resource_boundary_unproven"})
            raise SystemExit(1) from exc
        try:
            _move_cgroup(cgroup, child_pid)
        except Exception:
            _kill_pid(child_pid)
            _send_json_fd(HELPER_READY, {"error": "resource_boundary_unproven"})
            raise SystemExit(1) from None
        _send_json_fd(
            HELPER_READY,
            {"pid": int(child_pid), "starttime": _pid_starttime(child_pid)},
        )
        os.close(HELPER_READY)
        go = os.read(HELPER_GO, 1)
        os.close(HELPER_GO)
        if go != b"1":
            _kill_pid(child_pid)
            raise SystemExit(1)
        os.write(block_w, b"x")
        os.close(block_w)
        block_w = -1
        _, status = os.waitpid(child_pid, 0)
        rc = int(os.waitstatus_to_exitcode(status))
        _send_json_fd(HELPER_EXIT, {"returncode": rc})
        os.close(HELPER_EXIT)
    except SystemExit:
        raise
    except Exception:
        if child_pid:
            _kill_pid(child_pid)
        with contextlib.suppress(OSError):
            _send_json_fd(HELPER_READY, {"error": "spawn_failed"})
        with contextlib.suppress(OSError):
            _send_json_fd(HELPER_EXIT, {"error": "spawn_failed"})
        raise SystemExit(1) from None
    finally:
        if block_r >= 0:
            with contextlib.suppress(OSError):
                os.close(block_r)
        if block_w >= 0:
            with contextlib.suppress(OSError):
                os.close(block_w)


def _clear_cloexec(fd: int) -> None:
    import fcntl

    flags = int(fcntl.fcntl(fd, fcntl.F_GETFD))
    fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    os.set_inheritable(fd, True)


def _broker_spawn_helper(req: dict[str, Any], fds: list[int]) -> int:
    has_script = bool(req.get("has_script"))
    expected = 9 if has_script else 8
    if len(fds) != expected:
        raise RuntimeError(f"fd count {len(fds)} != {expected}")
    stdin_r, stdout_w, stderr_w, exec_fd, export_fd, ready_w, go_r, exit_w = fds[:8]
    script_fd = fds[8] if has_script else -1
    for fd in fds:
        _clear_cloexec(fd)
    helper = str(req.get("helper_path") or Path(__file__).resolve())
    python = str(req.get("python_path") or sys.executable)
    job_req = {
        "argv": req["argv"],
        "cgroup": req["cgroup"],
        "env": req["env"],
        "fsize": req["fsize"],
        "has_script": has_script,
        "launcher": req["launcher"],
    }
    null_fd = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    try:
        actions: list[tuple] = [
            (os.POSIX_SPAWN_DUP2, null_fd, 0),
            (os.POSIX_SPAWN_DUP2, null_fd, 1),
            (os.POSIX_SPAWN_DUP2, null_fd, 2),
            (os.POSIX_SPAWN_DUP2, stdin_r, HELPER_STDIN),
            (os.POSIX_SPAWN_DUP2, stdout_w, HELPER_STDOUT),
            (os.POSIX_SPAWN_DUP2, stderr_w, HELPER_STDERR),
            (os.POSIX_SPAWN_DUP2, exec_fd, HELPER_EXEC),
            (os.POSIX_SPAWN_DUP2, export_fd, HELPER_EXPORT),
            (os.POSIX_SPAWN_DUP2, ready_w, HELPER_READY),
            (os.POSIX_SPAWN_DUP2, go_r, HELPER_GO),
            (os.POSIX_SPAWN_DUP2, exit_w, HELPER_EXIT),
        ]
        if has_script:
            actions.append((os.POSIX_SPAWN_DUP2, script_fd, HELPER_SCRIPT))
        else:
            actions.append((os.POSIX_SPAWN_CLOSE, HELPER_SCRIPT))
        actions.append((os.POSIX_SPAWN_CLOSEFROM, HELPER_SCRIPT + 1))
        env = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            if key in os.environ:
                env[key] = os.environ[key]
        return int(
            os.posix_spawn(
                python,
                [python, helper, "--job", json.dumps(job_req, separators=(",", ":"))],
                env,
                file_actions=actions,
            )
        )
    finally:
        os.close(null_fd)


def _broker_main(sock: socket.socket) -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    while True:
        try:
            raw, fds, _flags, _addr = socket.recv_fds(sock, _RECV_MAX, _MAX_FDS)
        except OSError:
            return
        if not raw:
            return
        try:
            req = json.loads(raw.decode("utf-8").strip() or "{}")
        except Exception:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
            continue
        if not isinstance(req, dict):
            continue
        if req.get("op") == "shutdown":
            with contextlib.suppress(OSError):
                sock.sendall(b'{"ok":true}\n')
            return
        try:
            helper_pid = _broker_spawn_helper(req, list(fds))
            sock.sendall(json.dumps({"ok": True, "helper_pid": helper_pid}).encode("ascii") + b"\n")
        except Exception as exc:
            with contextlib.suppress(OSError):
                sock.sendall(
                    json.dumps(
                        {
                            "error": "spawn_failed",
                            "detail": type(exc).__name__,
                            "message": str(exc)[:200],
                        }
                    ).encode("ascii")
                    + b"\n"
                )
        finally:
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)


@dataclass
class StartedJob:
    pid: int
    starttime: int | None
    pidfd: int | None
    ctrl_fd: int


class SpawnBroker:
    """Kernel-side handle to the single-threaded spawn broker process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(child_sock.fileno(), True)
        helper = str(Path(__file__).resolve())
        env = os.environ.copy()
        env["FRIDAY_SPAWN_SOCKFD"] = str(child_sock.fileno())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._proc = subprocess.Popen(  # noqa: S603
            [sys.executable, helper, "--broker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(child_sock.fileno(),),
            env=env,
            close_fds=True,
        )
        child_sock.close()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with contextlib.suppress(OSError):
                socket.send_fds(self._sock, [b'{"op":"shutdown"}\n'], [])
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                with contextlib.suppress(Exception):
                    self._proc.kill()
            with contextlib.suppress(OSError):
                self._sock.close()

    def start_job(
        self,
        *,
        launcher: str,
        argv: list[str],
        env: dict[str, str],
        cgroup: str,
        fsize: int,
        stdin_r: int,
        stdout_w: int,
        stderr_w: int,
        exec_fd: int,
        export_fd: int,
        script_fd: int | None,
    ) -> StartedJob:
        if self._proc.poll() is not None:
            raise RuntimeError("spawn_helper_unavailable")
        ready_r, ready_w = os.pipe()
        go_r, go_w = os.pipe()
        exit_r, exit_w = os.pipe()
        fds = [stdin_r, stdout_w, stderr_w, exec_fd, export_fd, ready_w, go_r, exit_w]
        if script_fd is not None:
            fds.append(script_fd)
        payload = {
            "argv": argv,
            "cgroup": cgroup,
            "env": env,
            "fsize": int(fsize),
            "has_script": script_fd is not None,
            "helper_path": str(Path(__file__).resolve()),
            "launcher": launcher,
            "python_path": sys.executable,
        }
        try:
            with self._lock:
                socket.send_fds(
                    self._sock,
                    [json.dumps(payload, separators=(",", ":")).encode("utf-8")],
                    fds,
                )
                ack_raw = b""
                while b"\n" not in ack_raw:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        raise RuntimeError("spawn_helper_unavailable")
                    ack_raw += chunk
            os.close(ready_w)
            ready_w = -1
            os.close(go_r)
            go_r = -1
            os.close(exit_w)
            exit_w = -1
            ack = json.loads(ack_raw.split(b"\n", 1)[0].decode("ascii"))
            if not isinstance(ack, dict) or not ack.get("ok"):
                raise RuntimeError(
                    str(ack.get("error") or "spawn_failed")
                    + ":"
                    + str(ack.get("detail") or "")
                    + ":"
                    + str(ack.get("message") or "")
                )
            started = _recv_json_fd(ready_r, timeout=8.0)
            os.close(ready_r)
            ready_r = -1
            if started.get("error"):
                os.write(go_w, b"0")
                raise RuntimeError(str(started.get("error")))
            pid = int(started["pid"])
            starttime = started.get("starttime")
            try:
                pidfd = os.pidfd_open(pid, 0)
            except OSError:
                os.write(go_w, b"0")
                raise
            os.write(go_w, b"1")
            os.close(go_w)
            go_w = -1
            return StartedJob(
                pid=pid,
                starttime=int(starttime) if starttime is not None else None,
                pidfd=pidfd,
                ctrl_fd=exit_r,
            )
        except Exception:
            if exit_r >= 0:
                with contextlib.suppress(OSError):
                    os.close(exit_r)
            raise
        finally:
            for fd in (ready_r, ready_w, go_r, go_w, exit_w):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--job":
        _job_main()
        return
    fd_raw = os.environ.get("FRIDAY_SPAWN_SOCKFD", "")
    if not fd_raw.isdigit():
        raise SystemExit("spawn helper missing socket")
    sock = socket.socket(fileno=int(fd_raw))
    _broker_main(sock)


if __name__ == "__main__":
    main()
