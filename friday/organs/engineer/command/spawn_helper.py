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
HELPER_STDIN = 64
HELPER_STDOUT = 65
HELPER_STDERR = 66
HELPER_EXEC = 67
HELPER_EXPORT = 68
HELPER_READY = 69
HELPER_GO = 70
HELPER_EXIT = 71
HELPER_SCRIPT = 72
HELPER_STDIN_PAYLOAD = 73
HELPER_EXPORT_IMPL = 74
HELPER_PATH_ROOT_BASE = 80
BWRAP_EXEC_FD = 3
BWRAP_SCRIPT_FD = 4
BWRAP_BLOCK_FD = 5
BWRAP_EXPORT_FD = 6
BWRAP_PATH_ROOT_BASE = 7
BWRAP_STDIN_PAYLOAD_FD = 23
BWRAP_EXPORT_IMPL_FD = 24
_RECV_MAX = 512 * 1024
_MAX_FDS = 32
_MAX_PATH_ROOT_FDS = 16
_ACK_TIMEOUT_SEC = 8.0


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


def _send_ready(payload: dict[str, Any], pidfd: int | None = None) -> None:
    sock = socket.socket(fileno=HELPER_READY)
    try:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        _send_fds_message(sock, body, [] if pidfd is None else [pidfd])
    finally:
        sock.detach()


def _pidfd_identity(pidfd: int) -> int | None:
    try:
        raw = Path(f"/proc/self/fdinfo/{int(pidfd)}").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    for line in raw.splitlines():
        if line.startswith("Pid:"):
            value = line.partition(":")[2].strip()
            return int(value) if value.isdigit() else None
    return None


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


def _send_fds_message(sock: socket.socket, payload: bytes, fds: list[int]) -> None:
    """Send one newline-framed request and attach rights to its first bytes."""
    if b"\n" in payload:
        raise ValueError("spawn helper invalid frame")
    frame = payload + b"\n"
    sent = int(socket.send_fds(sock, [frame], fds))
    if sent <= 0:
        raise OSError("spawn helper short send")
    if sent < len(frame):
        sock.sendall(frame[sent:])


def _recv_socket_line(sock: socket.socket, *, timeout: float | None = None) -> bytes:
    buf = bytearray()
    deadline = None if timeout is None else time.monotonic() + timeout
    while b"\n" not in buf:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if remaining is not None:
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                raise TimeoutError("spawn helper socket timeout")
        chunk = sock.recv(min(4096, _RECV_MAX - len(buf) + 1))
        if not chunk:
            raise EOFError("spawn helper socket closed")
        buf.extend(chunk)
        if len(buf) > _RECV_MAX:
            raise ValueError("spawn helper socket overflow")
    line, _, rest = bytes(buf).partition(b"\n")
    if rest:
        raise ValueError("spawn helper socket trailing data")
    return line


def _recv_fds_message(sock: socket.socket) -> tuple[bytes, list[int]]:
    """Receive a complete stream frame; recvmsg is not a message boundary."""
    fds: list[int] = []
    try:
        raw, received, flags, _addr = socket.recv_fds(sock, 4096, _MAX_FDS)
        fds.extend(int(fd) for fd in received)
        if flags & (getattr(socket, "MSG_CTRUNC", 0) | getattr(socket, "MSG_TRUNC", 0)):
            raise ValueError("spawn helper truncated frame")
        if not raw:
            return b"", fds
        buf = bytearray(raw)
        while b"\n" not in buf:
            chunk = sock.recv(min(4096, _RECV_MAX - len(buf) + 1))
            if not chunk:
                raise EOFError("spawn helper socket closed")
            buf.extend(chunk)
            if len(buf) > _RECV_MAX:
                raise ValueError("spawn helper socket overflow")
        line, _, rest = bytes(buf).partition(b"\n")
        if rest:
            raise ValueError("spawn helper socket trailing data")
        return line, fds
    except Exception:
        for received_fd in fds:
            with contextlib.suppress(OSError):
                os.close(received_fd)
        raise


def _kill_pidfd(pidfd: int) -> None:
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
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
    path_root_count = int(req.get("path_root_count") or 0)
    if not 0 <= path_root_count <= _MAX_PATH_ROOT_FDS:
        raise SystemExit(1)
    block_r, block_w = os.pipe()
    os.set_inheritable(block_r, True)
    child_pid = 0
    child_pidfd = -1
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
        for index in range(path_root_count):
            actions.append(
                (os.POSIX_SPAWN_DUP2, HELPER_PATH_ROOT_BASE + index, BWRAP_PATH_ROOT_BASE + index)
            )
        actions.append((os.POSIX_SPAWN_DUP2, HELPER_STDIN_PAYLOAD, BWRAP_STDIN_PAYLOAD_FD))
        actions.append((os.POSIX_SPAWN_DUP2, HELPER_EXPORT_IMPL, BWRAP_EXPORT_IMPL_FD))
        actions.append((os.POSIX_SPAWN_CLOSEFROM, BWRAP_EXPORT_IMPL_FD + 1))
        child_pid = os.posix_spawn(launcher, argv, env, file_actions=actions)
        os.close(block_r)
        block_r = -1
        try:
            child_pidfd = os.pidfd_open(child_pid, 0)
        except OSError as exc:
            # The child is still blocked in bwrap. Closing the writer makes
            # that setup fail without ever signaling a possibly reused PID.
            os.close(block_w)
            block_w = -1
            with contextlib.suppress(ChildProcessError):
                os.waitpid(child_pid, 0)
            _send_ready({"error": "resource_boundary_unproven"})
            raise SystemExit(1) from exc
        # If the original child exited before pidfd_open, its numeric PID may
        # already identify an unrelated process. waitpid still refers only to
        # our child, so reject that case before transferring the pidfd.
        reaped_pid, _reaped_status = os.waitpid(child_pid, os.WNOHANG)
        if reaped_pid != 0:
            _send_ready({"error": "resource_boundary_unproven"})
            raise SystemExit(1)
        try:
            resource.prlimit(child_pid, resource.RLIMIT_FSIZE, (fsize, fsize))
        except (OSError, ValueError) as exc:
            _kill_pidfd(child_pidfd)
            _send_ready({"error": "resource_boundary_unproven"})
            raise SystemExit(1) from exc
        try:
            _move_cgroup(cgroup, child_pid)
        except Exception:
            _kill_pidfd(child_pidfd)
            _send_ready({"error": "resource_boundary_unproven"})
            raise SystemExit(1) from None
        _send_ready(
            {"pid": int(child_pid), "starttime": _pid_starttime(child_pid)},
            child_pidfd,
        )
        os.close(HELPER_READY)
        go = os.read(HELPER_GO, 1)
        os.close(HELPER_GO)
        if go != b"1":
            _kill_pidfd(child_pidfd)
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
        if child_pidfd >= 0:
            _kill_pidfd(child_pidfd)
        with contextlib.suppress(OSError):
            _send_ready({"error": "spawn_failed"})
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
        if child_pidfd >= 0:
            with contextlib.suppress(OSError):
                os.close(child_pidfd)


def _clear_cloexec(fd: int) -> None:
    import fcntl

    flags = int(fcntl.fcntl(fd, fcntl.F_GETFD))
    fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    os.set_inheritable(fd, True)


def _broker_spawn_helper(req: dict[str, Any], fds: list[int]) -> int:
    has_script = bool(req.get("has_script"))
    path_root_count = int(req.get("path_root_count") or 0)
    if not 0 <= path_root_count <= _MAX_PATH_ROOT_FDS:
        raise RuntimeError("invalid path root fd count")
    expected = 10 + (1 if has_script else 0) + path_root_count
    if len(fds) != expected:
        raise RuntimeError(f"fd count {len(fds)} != {expected}")
    if any(fd >= HELPER_STDIN for fd in fds):
        raise RuntimeError("received fd range overlaps helper destinations")
    (
        stdin_r,
        stdout_w,
        stderr_w,
        exec_fd,
        export_fd,
        export_impl_fd,
        stdin_payload_fd,
        ready_w,
        go_r,
        exit_w,
    ) = fds[:10]
    script_fd = fds[10] if has_script else -1
    root_offset = 10 + (1 if has_script else 0)
    path_root_fds = fds[root_offset:]
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
        "path_root_count": path_root_count,
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
            (os.POSIX_SPAWN_DUP2, export_impl_fd, HELPER_EXPORT_IMPL),
            (os.POSIX_SPAWN_DUP2, stdin_payload_fd, HELPER_STDIN_PAYLOAD),
            (os.POSIX_SPAWN_DUP2, ready_w, HELPER_READY),
            (os.POSIX_SPAWN_DUP2, go_r, HELPER_GO),
            (os.POSIX_SPAWN_DUP2, exit_w, HELPER_EXIT),
        ]
        if has_script:
            actions.append((os.POSIX_SPAWN_DUP2, script_fd, HELPER_SCRIPT))
        else:
            actions.append((os.POSIX_SPAWN_CLOSE, HELPER_SCRIPT))
        for index, root_fd in enumerate(path_root_fds):
            actions.append((os.POSIX_SPAWN_DUP2, root_fd, HELPER_PATH_ROOT_BASE + index))
        for source_fd in fds:
            actions.append((os.POSIX_SPAWN_CLOSE, source_fd))
        actions.append((os.POSIX_SPAWN_CLOSEFROM, HELPER_PATH_ROOT_BASE + path_root_count))
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
    os.set_inheritable(sock.fileno(), False)
    while True:
        fds: list[int] = []
        try:
            raw, fds = _recv_fds_message(sock)
        except (EOFError, OSError, ValueError):
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
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
            continue
        if req.get("op") == "shutdown":
            with contextlib.suppress(OSError):
                sock.sendall(b'{"ok":true}\n')
            for fd in fds:
                with contextlib.suppress(OSError):
                    os.close(fd)
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
                _send_fds_message(self._sock, b'{"op":"shutdown"}', [])
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
        export_impl_fd: int,
        stdin_payload_fd: int,
        script_fd: int | None,
        path_root_fds: list[int],
    ) -> StartedJob:
        if self._closed or self._proc.poll() is not None:
            raise RuntimeError("spawn_helper_unavailable")
        if len(path_root_fds) > _MAX_PATH_ROOT_FDS:
            raise RuntimeError("invalid path root fd count")
        ready_parent, ready_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        ready_r = ready_parent.detach()
        ready_w = ready_child.detach()
        go_r, go_w = os.pipe()
        exit_r, exit_w = os.pipe()
        pidfd = -1
        fds = [
            stdin_r,
            stdout_w,
            stderr_w,
            exec_fd,
            export_fd,
            export_impl_fd,
            stdin_payload_fd,
            ready_w,
            go_r,
            exit_w,
        ]
        if script_fd is not None:
            fds.append(script_fd)
        fds.extend(path_root_fds)
        payload = {
            "argv": argv,
            "cgroup": cgroup,
            "env": env,
            "fsize": int(fsize),
            "has_script": script_fd is not None,
            "helper_path": str(Path(__file__).resolve()),
            "launcher": launcher,
            "path_root_count": len(path_root_fds),
            "python_path": sys.executable,
        }
        try:
            with self._lock:
                try:
                    _send_fds_message(
                        self._sock,
                        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                        fds,
                    )
                    ack_raw = _recv_socket_line(self._sock, timeout=_ACK_TIMEOUT_SEC)
                except (EOFError, OSError, TimeoutError, ValueError) as exc:
                    self._closed = True
                    with contextlib.suppress(OSError):
                        self._sock.close()
                    with contextlib.suppress(Exception):
                        self._proc.terminate()
                        self._proc.wait(timeout=2)
                    raise RuntimeError("spawn_helper_unavailable") from exc
            os.close(ready_w)
            ready_w = -1
            os.close(go_r)
            go_r = -1
            os.close(exit_w)
            exit_w = -1
            ack = json.loads(ack_raw.decode("ascii"))
            if not isinstance(ack, dict) or not ack.get("ok"):
                raise RuntimeError(
                    str(ack.get("error") or "spawn_failed")
                    + ":"
                    + str(ack.get("detail") or "")
                    + ":"
                    + str(ack.get("message") or "")
                )
            ready_sock = socket.socket(fileno=ready_r)
            ready_r = -1
            try:
                ready_sock.settimeout(8.0)
                ready_raw, ready_fds = _recv_fds_message(ready_sock)
            finally:
                ready_sock.close()
            try:
                started = json.loads(ready_raw.decode("ascii"))
            except (ValueError, UnicodeError) as exc:
                for received_fd in ready_fds:
                    with contextlib.suppress(OSError):
                        os.close(received_fd)
                raise RuntimeError("spawn_failed") from exc
            if not isinstance(started, dict):
                for received_fd in ready_fds:
                    with contextlib.suppress(OSError):
                        os.close(received_fd)
                raise RuntimeError("spawn_failed")
            if started.get("error"):
                for received_fd in ready_fds:
                    with contextlib.suppress(OSError):
                        os.close(received_fd)
                os.write(go_w, b"0")
                raise RuntimeError(str(started.get("error")))
            pid = int(started["pid"])
            starttime = started.get("starttime")
            if len(ready_fds) != 1:
                for received_fd in ready_fds:
                    with contextlib.suppress(OSError):
                        os.close(received_fd)
                os.write(go_w, b"0")
                raise RuntimeError("resource_boundary_unproven")
            pidfd = int(ready_fds[0])
            if _pidfd_identity(pidfd) != pid:
                os.close(pidfd)
                pidfd = -1
                os.write(go_w, b"0")
                raise RuntimeError("resource_boundary_unproven")
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
            if pidfd >= 0:
                with contextlib.suppress(OSError):
                    os.close(pidfd)
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
