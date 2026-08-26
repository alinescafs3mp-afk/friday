"""Focused functional and security contracts for bounded Java compilation."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import pathlib
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from friday.organs.engineer import compiler, decompiler, sandbox, worker


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _class_bytes(body: bytes = b"bounded") -> bytes:
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + body


def _fake_jdk(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    jdk = tmp_path / "jdk-21.0.12.1+1"
    javac = jdk / "bin/javac"
    javac.parent.mkdir(parents=True)
    javac.write_bytes(b"pinned-temurin-javac")
    jdk.chmod(0o700)
    javac.parent.chmod(0o700)
    javac.chmod(0o700)
    monkeypatch.setattr(compiler, "_JDK_FILES", {"bin/javac": _sha256(javac.read_bytes())})
    monkeypatch.setattr(decompiler, "_safe_ancestors", lambda _path: True)
    monkeypatch.setattr(
        compiler,
        "JDK_TREE_SHA256",
        compiler._tree_identity(jdk, maximum_bytes=compiler.MAX_JDK_TREE_BYTES),  # noqa: SLF001
    )
    return jdk


def _real_sandbox_workspace(
    tmp_path: pathlib.Path,
    *,
    request: dict[str, object] | None = None,
    source: bytes = b"probe",
) -> pathlib.Path:
    workspace = tmp_path / "sandbox-work"
    workspace.mkdir(mode=0o700)
    payloads = {
        "request.json": json.dumps(request or {}).encode("utf-8"),
        "input.bin": source,
        "result.json": b"",
        "output.bin": b"",
    }
    for name, payload in payloads.items():
        path = workspace / name
        path.write_bytes(payload)
        path.chmod(0o600)
    return workspace


def test_compiler_profile_and_owner_local_jdk_are_exact() -> None:
    assert compiler.PROFILE == "java21_single_source_library_jar_v1"
    assert compiler.SCHEMA == "friday.engineer.compile.v1"
    assert compiler.JDK_VERSION == "21.0.12.1+1"
    assert pathlib.Path("/home/jericho/.jericho/tools/jdk-21.0.12.1+1") == compiler.JDK_ROOT
    assert compiler.JDK_TREE_SHA256 == decompiler.JDK_TREE_SHA256
    assert compiler.JAVAC_SHA256 == ("55859b80e7a9c4c4736be19ad3addeb35112ca6d17a30c4e0e116afc0a499bdb")
    assert compiler.MAX_SOURCE_BYTES == 1024 * 1024
    assert compiler.MAX_CLASS_FILES == 256
    assert compiler.MAX_CLASS_BYTES == 8 * 1024 * 1024
    assert compiler.MAX_JAR_BYTES == 16 * 1024 * 1024


def test_compiler_jdk_identity_requires_exact_tree_file_and_safe_permissions(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    jdk = _fake_jdk(tmp_path, monkeypatch)

    assert compiler.verify_toolchain(jdk) == {
        "ok": True,
        "identity": "pinned_full_tree",
        "tool_name": "temurin-javac",
        "tool_version": "21.0.12.1+1",
        "jdk_version": "21.0.12.1+1",
    }

    (jdk / "bin/javac").write_bytes(b"changed")
    assert compiler.verify_toolchain(jdk) == {"ok": False, "reason": "toolchain_untrusted"}


def test_compiler_jdk_identity_rejects_symlink_escape(tmp_path: pathlib.Path, monkeypatch) -> None:
    jdk = _fake_jdk(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="ascii")
    (jdk / "escape").symlink_to(outside)

    assert compiler.verify_toolchain(jdk) == {"ok": False, "reason": "toolchain_untrusted"}


def test_shared_tool_tree_identity_has_profile_specific_entry_and_byte_caps(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    jdk = _fake_jdk(tmp_path, monkeypatch)

    assert (
        decompiler._tree_identity(  # noqa: SLF001
            jdk,
            maximum_bytes=compiler.MAX_JDK_TREE_BYTES,
            maximum_entries=1,
        )
        is None
    )
    assert (
        decompiler._tree_identity(  # noqa: SLF001
            jdk,
            maximum_bytes=1,
            maximum_entries=8,
        )
        is None
    )
    assert (
        decompiler._tree_identity(  # noqa: SLF001
            jdk,
            maximum_bytes=compiler.MAX_JDK_TREE_BYTES,
            maximum_entries=8,
        )
        is not None
    )


def test_javac_argv_is_fixed_and_disables_processors_and_implicit_sources() -> None:
    argv = compiler._javac_argv(  # noqa: SLF001
        pathlib.Path("/work/java-src/Main.java"),
        pathlib.Path("/work/java-classes"),
        pathlib.Path("/work/java-empty"),
    )

    assert argv[0] == "/opt/friday-jdk/bin/javac"
    assert argv[-1] == "/work/java-src/Main.java"
    assert argv[argv.index("--release") + 1] == "21"
    assert "-proc:none" in argv
    assert "-implicit:none" in argv
    assert "-g:none" in argv
    assert "-classpath" in argv
    assert "-sourcepath" in argv
    assert not any(value in {"sh", "bash", "-c", "java", "jar"} for value in argv)


@pytest.mark.parametrize(
    ("filename", "payload", "error"),
    (
        ("../Main.java", b"class Main {}", "invalid_filename"),
        ("Main.txt", b"class Main {}", "invalid_filename"),
        ("Main.java", b"", "input_size_invalid"),
        ("Main.java", b"\xff", "input_encoding_invalid"),
        ("Main.java", b"class Main {\x00}", "input_encoding_invalid"),
    ),
)
def test_invalid_source_never_enters_toolchain_or_javac(
    monkeypatch,
    tmp_path: pathlib.Path,
    filename: str,
    payload: bytes,
    error: str,
) -> None:
    source = tmp_path / "input.bin"
    source.write_bytes(payload)
    monkeypatch.setattr(
        compiler,
        "sandbox_toolchain_preflight",
        lambda: pytest.fail("invalid source reached mounted toolchain preflight"),
    )
    monkeypatch.setattr(
        compiler.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid source launched javac"),
    )

    output, report = compiler.compile_artifact(source, filename, {"ok": True})

    assert output is None
    assert report["ok"] is False
    assert report["error"] == error
    assert report["sample_executed"] is False
    assert report["network"] == "none"


def test_compile_emits_reproducible_stored_jar_and_bounded_receipt(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "input.bin"
    source.write_text("public class Main {}\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    class CompletedJavac:
        pid = 4242
        returncode: int | None = None

        def __init__(self, argv, **kwargs) -> None:  # noqa: ANN001
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            classes = pathlib.Path(argv[argv.index("-d") + 1])
            (classes / "nested").mkdir(parents=True)
            (classes / "nested/Helper.class").write_bytes(_class_bytes(b"helper"))
            (classes / "Main.class").write_bytes(_class_bytes(b"main"))

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            captured["timeout"] = timeout
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(compiler, "sandbox_toolchain_preflight", lambda: {"ok": True})
    monkeypatch.setattr(compiler.subprocess, "Popen", CompletedJavac)

    first, first_report = compiler.compile_artifact(source, "Main.java", {"ok": True})
    second, second_report = compiler.compile_artifact(source, "Main.java", {"ok": True})

    assert first is not None and first == second
    assert first_report == second_report
    assert first_report == {
        "ok": True,
        "status": "completed",
        "schema": compiler.SCHEMA,
        "profile": compiler.PROFILE,
        "tool_name": "temurin-javac",
        "tool_version": "21.0.12.1+1",
        "jdk_version": "21.0.12.1+1",
        "source_sha256": _sha256(source.read_bytes()),
        "jar_sha256": _sha256(first),
        "source_size_bytes": len(source.read_bytes()),
        "class_files": 2,
        "class_bytes": len(_class_bytes(b"helper")) + len(_class_bytes(b"main")),
        "jar_size_bytes": len(first),
        "java_release": 21,
        "class_major_version": 65,
        "archive": "jar",
        "compression": "stored",
        "signed": False,
        "manifest": False,
        "runtime_validation": "not_performed",
        "sample_executed": False,
        "network": "none",
    }
    assert captured["timeout"] == compiler.JAVAC_TIMEOUT_SECONDS
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL
    assert set(captured["kwargs"]["env"]).isdisjoint({"PATH", "HOME", "HTTP_PROXY", "CLASSPATH"})
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["Main.class", "nested/Helper.class"]
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert "META-INF/MANIFEST.MF" not in archive.namelist()


def test_compile_rejects_wrong_class_version_without_leaking_content(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "secret-source"
    source.write_text("class Main {} // SECRET", encoding="utf-8")

    class WrongVersionJavac:
        pid = 4242
        returncode: int | None = None

        def __init__(self, argv, **_kwargs) -> None:  # noqa: ANN001
            classes = pathlib.Path(argv[argv.index("-d") + 1])
            classes.mkdir(parents=True, exist_ok=True)
            (classes / "SECRET.class").write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x3d")

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(compiler, "sandbox_toolchain_preflight", lambda: {"ok": True})
    monkeypatch.setattr(compiler.subprocess, "Popen", WrongVersionJavac)

    output, report = compiler.compile_artifact(source, "Main.java", {"ok": True})

    assert output is None
    assert report["error"] == "compiler_output_invalid"
    serialized = json.dumps(report, sort_keys=True)
    assert "SECRET" not in serialized
    assert str(source) not in serialized


def test_canonical_jar_revalidation_rejects_trailing_or_changed_bytes() -> None:
    jar = compiler._deterministic_jar(  # noqa: SLF001
        [("Main.class", _class_bytes(b"main"))]
    )
    assert jar is not None
    assert compiler.validate_jar(jar) == {
        "class_files": 1,
        "class_bytes": len(_class_bytes(b"main")),
    }
    assert compiler.validate_jar(jar + b"trailing") is None
    assert compiler.validate_jar(jar[:-1] + bytes([jar[-1] ^ 1])) is None


def test_compile_worker_reads_only_source_cap_and_writes_bounded_output(
    monkeypatch,
    tmp_path: pathlib.Path,
) -> None:
    request = tmp_path / "request.json"
    source = tmp_path / "input.bin"
    result = tmp_path / "result.json"
    output = tmp_path / "output.bin"
    request.write_text(
        json.dumps(
            {
                "protocol": worker.PROTOCOL_VERSION,
                "action": "compile_java",
                "filename": "Main.java",
                "compiler_toolchain": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    source.write_text("class Main {}", encoding="utf-8")
    result.write_bytes(b"")
    output.write_bytes(b"")
    jar = b"PK\x03\x04bounded"
    report = {"ok": True, "status": "completed", "jar_size_bytes": len(jar)}
    monkeypatch.setattr(compiler, "compile_artifact", lambda *_args: (jar, report))

    assert worker.run(request, source, result, output) == 0
    assert output.read_bytes() == jar
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "completed"


def test_compiler_bwrap_mounts_only_jdk_read_only_and_has_finite_limits() -> None:
    workspace = pathlib.Path("/private/work")
    argv = sandbox._sandbox_argv(workspace, mount_jdk=True)  # noqa: SLF001
    limited = sandbox._limited_sandbox_argv(  # noqa: SLF001
        workspace, action="compile_java", mount_jdk=True
    )

    assert "--unshare-all" in argv
    assert str(decompiler.GHIDRA_ROOT) not in argv
    assert argv[argv.index(str(compiler.JDK_ROOT)) - 1] == "--ro-bind"
    assert argv[argv.index(str(compiler.JDK_ROOT)) + 1] == str(compiler.SANDBOX_JDK_ROOT)
    assert argv[argv.index("--tmpfs") - 2 : argv.index("--tmpfs") + 2] == [
        "--size",
        str(sandbox.COMPILE_WORKSPACE_TMPFS_BYTES),
        "--tmpfs",
        "/tmp",
    ]
    for directory in ("/etc", "/app", "/opt", "/root", "/work"):
        assert ["--perms", "0555", "--dir", directory] in [
            argv[index : index + 4] for index in range(len(argv) - 3)
        ]
    assert ["--remount-ro", "/dev"] in [argv[index : index + 2] for index in range(len(argv) - 1)]
    assert ["--remount-ro", "/"] in [argv[index : index + 2] for index in range(len(argv) - 1)]
    assert [(argv[index + 1], argv[index + 2]) for index, value in enumerate(argv) if value == "--bind"] == [
        (str(workspace / "result.json"), "/work/result.json"),
        (str(workspace / "output.bin"), "/work/output.bin"),
    ]
    assert (str(workspace / "request.json"), "/work/request.json") in [
        (argv[index + 1], argv[index + 2]) for index, value in enumerate(argv) if value == "--ro-bind"
    ]
    assert (str(workspace / "input.bin"), "/work/input.bin") in [
        (argv[index + 1], argv[index + 2]) for index, value in enumerate(argv) if value == "--ro-bind"
    ]
    assert str(workspace) not in argv
    assert "/home" not in argv
    assert limited[:7] == [
        "/usr/bin/prlimit",
        "--core=0:0",
        f"--cpu={sandbox.COMPILE_MAX_CPU_SECONDS}:{sandbox.COMPILE_MAX_CPU_SECONDS + 1}",
        f"--fsize={sandbox.COMPILE_MAX_FILE_BYTES}:{sandbox.COMPILE_MAX_FILE_BYTES}",
        f"--as={sandbox.COMPILE_MAX_ADDRESS_SPACE_BYTES}:{sandbox.COMPILE_MAX_ADDRESS_SPACE_BYTES}",
        "--nofile=128:128",
        "--",
    ]
    assert compiler.JAVAC_TIMEOUT_SECONDS < sandbox.COMPILE_MAX_WALL_SECONDS


def test_real_compiler_bwrap_denies_extra_scratch_and_exhausts_only_bounded_tmpfs(
    tmp_path: pathlib.Path,
) -> None:
    workspace = _real_sandbox_workspace(tmp_path)
    argv = sandbox._limited_sandbox_argv(  # noqa: SLF001
        workspace,
        action="compile_java",
        mount_jdk=True,
    )
    boundary = max(index for index, value in enumerate(argv) if value == "--")
    probe = r"""
import json
import os

denied = {}
for path in (
    "/friday-probe",
    "/etc/friday-probe",
    "/app/friday-probe",
    "/opt/friday-probe",
    "/dev/friday-probe",
    "/dev/shm/friday-probe",
    "/root/friday-probe",
    "/work/friday-probe",
):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        denied[path] = exc.errno
    else:
        os.close(descriptor)
        denied[path] = 0

immutable_inputs = {}
for path in ("/work/request.json", "/work/input.bin"):
    try:
        descriptor = os.open(path, os.O_WRONLY)
    except OSError as exc:
        immutable_inputs[path] = exc.errno
    else:
        os.close(descriptor)
        immutable_inputs[path] = 0

carriers = {}
for path in ("/work/result.json", "/work/output.bin"):
    descriptor = os.open(path, os.O_WRONLY)
    try:
        carriers[path] = os.write(descriptor, b"x") == 1
    finally:
        os.close(descriptor)

null_descriptor = os.open("/dev/null", os.O_WRONLY)
try:
    device_write = os.write(null_descriptor, b"device") == 6
finally:
    os.close(null_descriptor)
random_descriptor = os.open("/dev/urandom", os.O_RDONLY)
try:
    device_read = len(os.read(random_descriptor, 1)) == 1
finally:
    os.close(random_descriptor)

chunk = b"x" * (1024 * 1024)
tmpfs_bytes = 0
tmpfs_files = 0
tmpfs_error = 0
for index in range(64):
    try:
        descriptor = os.open(
            f"/tmp/friday-fill-{index}",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except OSError as exc:
        tmpfs_error = exc.errno
        break
    try:
        offset = 0
        while offset < len(chunk):
            try:
                written = os.write(descriptor, chunk[offset:])
            except OSError as exc:
                tmpfs_error = exc.errno
                break
            offset += written
            tmpfs_bytes += written
    finally:
        os.close(descriptor)
    tmpfs_files += 1
    if tmpfs_error:
        break

print(json.dumps({
    "carriers": carriers,
    "denied": denied,
    "device_read": device_read,
    "device_write": device_write,
    "immutable_inputs": immutable_inputs,
    "tmpfs_bytes": tmpfs_bytes,
    "tmpfs_error": tmpfs_error,
    "tmpfs_files": tmpfs_files,
}, sort_keys=True))
"""
    completed = subprocess.run(  # noqa: S603 - fixed bwrap/prlimit prefix and test probe
        [
            *argv[: boundary + 1],
            str(sandbox.PYTHON),
            "-I",
            "-S",
            "-B",
            "-c",
            probe,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    proof = json.loads(completed.stdout)
    assert set(proof["denied"]) == {
        "/friday-probe",
        "/app/friday-probe",
        "/dev/friday-probe",
        "/dev/shm/friday-probe",
        "/etc/friday-probe",
        "/opt/friday-probe",
        "/root/friday-probe",
        "/work/friday-probe",
    }
    assert set(proof["denied"].values()) <= {errno.EACCES, errno.EROFS}
    assert set(proof["immutable_inputs"]) == {
        "/work/input.bin",
        "/work/request.json",
    }
    assert set(proof["immutable_inputs"].values()) <= {errno.EACCES, errno.EROFS}
    assert proof["carriers"] == {
        "/work/output.bin": True,
        "/work/result.json": True,
    }
    assert proof["device_read"] is True
    assert proof["device_write"] is True
    assert proof["tmpfs_error"] == errno.ENOSPC
    assert 1 < proof["tmpfs_files"] < 64
    assert 0 < proof["tmpfs_bytes"] <= sandbox.COMPILE_WORKSPACE_TMPFS_BYTES


def test_real_pinned_javac_runs_with_read_only_root_and_devices(tmp_path: pathlib.Path) -> None:
    toolchain = compiler.host_toolchain_preflight()
    if toolchain.get("ok") is not True:
        pytest.skip("pinned owner-local JDK is not installed")
    source = b"public class Main { public int value() { return 21; } }\n"
    workspace = _real_sandbox_workspace(
        tmp_path,
        request={
            "protocol": worker.PROTOCOL_VERSION,
            "action": "compile_java",
            "filename": "Main.java",
            "operations": [],
            "compiler_toolchain": toolchain,
        },
        source=source,
    )

    completed = subprocess.run(  # noqa: S603 - exact production prlimit/bwrap/worker argv
        sandbox._limited_sandbox_argv(  # noqa: SLF001
            workspace,
            action="compile_java",
            mount_jdk=True,
        ),
        check=False,
        capture_output=True,
        timeout=sandbox.COMPILE_MAX_WALL_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    report = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
    jar = (workspace / "output.bin").read_bytes()
    assert report["ok"] is True
    assert report["tool_version"] == compiler.JDK_VERSION
    assert report["source_sha256"] == _sha256(source)
    assert compiler.validate_jar(jar) == {
        "class_files": 1,
        "class_bytes": report["class_bytes"],
    }


def test_compiler_requires_finite_aggregate_pid_and_memory_cgroups(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_current_cgroup_pids_limit", lambda: 512)
    monkeypatch.setattr(sandbox, "_current_cgroup_memory_limit", lambda: 12 * 1024**3)
    monkeypatch.setattr(sandbox, "_current_cgroup_swap_limit", lambda: 0)
    assert sandbox._compile_resource_preflight() == {  # noqa: SLF001
        "ok": True,
        "pids_limit": 512,
        "memory_limit_bytes": 12 * 1024**3,
        "swap_limit_bytes": 0,
    }

    monkeypatch.setattr(sandbox, "_current_cgroup_pids_limit", lambda: 513)
    assert sandbox._compile_resource_preflight() == {  # noqa: SLF001
        "ok": False,
        "reason": "compiler_pid_cgroup_unbounded",
    }

    monkeypatch.setattr(sandbox, "_current_cgroup_pids_limit", lambda: 512)
    monkeypatch.setattr(sandbox, "_current_cgroup_memory_limit", lambda: None)
    assert sandbox._compile_resource_preflight() == {  # noqa: SLF001
        "ok": False,
        "reason": "compiler_memory_cgroup_unbounded",
    }

    monkeypatch.setattr(sandbox, "_current_cgroup_memory_limit", lambda: 12 * 1024**3)
    monkeypatch.setattr(sandbox, "_current_cgroup_swap_limit", lambda: None)
    assert sandbox._compile_resource_preflight() == {  # noqa: SLF001
        "ok": False,
        "reason": "compiler_memory_cgroup_unbounded",
    }


def test_compiler_reads_zero_swap_from_its_exact_unified_cgroup(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    cgroup_root = tmp_path / "cgroup"
    current = cgroup_root / "user.slice" / "friday-backend.service"
    current.mkdir(parents=True)
    swap_limit = current / "memory.swap.max"
    swap_limit.write_text("0\n", encoding="ascii")
    membership = tmp_path / "self.cgroup"
    membership.write_text("0::/user.slice/friday-backend.service\n", encoding="ascii")
    monkeypatch.setattr(sandbox, "CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(sandbox, "SELF_CGROUP", membership)

    assert sandbox._current_cgroup_swap_limit() == 0  # noqa: SLF001
    swap_limit.write_text("max\n", encoding="ascii")
    assert sandbox._current_cgroup_swap_limit() is None  # noqa: SLF001


def test_worker_writes_only_an_empty_precreated_single_link_target(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "result.json"
    with pytest.raises(ValueError, match="output_target_invalid"):
        worker._write_regular(target, b"bounded", 32)  # noqa: SLF001

    target.write_bytes(b"attacker-controlled")
    with pytest.raises(ValueError, match="output_target_invalid"):
        worker._write_regular(target, b"bounded", 32)  # noqa: SLF001
    assert target.read_bytes() == b"attacker-controlled"

    target.unlink()
    target.write_bytes(b"")
    alias = tmp_path / "alias"
    alias.hardlink_to(target)
    with pytest.raises(ValueError, match="output_target_invalid"):
        worker._write_regular(target, b"bounded", 32)  # noqa: SLF001
    assert target.read_bytes() == b""

    alias.unlink()
    worker._write_regular(target, b"bounded", 32)  # noqa: SLF001
    assert target.read_bytes() == b"bounded"


def test_compile_start_audit_is_durable_before_worker_spawn(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(sandbox, "preflight", lambda: {"ok": True})
    monkeypatch.setattr(
        sandbox,
        "_compile_resource_preflight",
        lambda: {"ok": True, "pids_limit": 512, "memory_limit_bytes": 12 * 1024**3},
    )
    monkeypatch.setattr(compiler, "host_toolchain_preflight", lambda: {"ok": True})

    def failed_launch(*_args, **_kwargs):
        order.append("spawn")
        raise OSError("closed test launch")

    monkeypatch.setattr(sandbox.subprocess, "Popen", failed_launch)
    with pytest.raises(sandbox.EngineerSandboxError) as captured:
        sandbox.compile_java_artifact(
            b"class Main {}",
            "Main.java",
            on_started=lambda: order.append("audit"),
        )

    assert captured.value.code == "worker_launch_failed"
    assert captured.value.work_started is True
    assert order == ["audit", "spawn"]


def test_compile_refuses_spawn_when_start_audit_cannot_commit(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(sandbox, "preflight", lambda: {"ok": True})
    monkeypatch.setattr(
        sandbox,
        "_compile_resource_preflight",
        lambda: {"ok": True, "pids_limit": 512, "memory_limit_bytes": 12 * 1024**3},
    )
    monkeypatch.setattr(compiler, "host_toolchain_preflight", lambda: {"ok": True})
    monkeypatch.setattr(
        sandbox.subprocess,
        "Popen",
        lambda *_args, **_kwargs: order.append("spawn"),
    )

    def failed_audit() -> None:
        order.append("audit")
        raise RuntimeError("closed test audit")

    with pytest.raises(sandbox.EngineerSandboxError) as captured:
        sandbox.compile_java_artifact(
            b"class Main {}",
            "Main.java",
            on_started=failed_audit,
        )

    assert captured.value.code == "audit_start_unavailable"
    assert captured.value.work_started is False
    assert order == ["audit"]


def test_decompiler_and_compiler_share_one_physical_heavy_slot(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_worker(action, *_args, **_kwargs):  # noqa: ANN001
        calls.append(action)
        entered.set()
        assert release.wait(timeout=5)
        if action == "compile_java":
            return {"ok": True, "status": "completed"}, b"jar"
        return {"ok": True, "status": "completed"}, None

    monkeypatch.setattr(sandbox, "_run_worker", blocking_worker)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(sandbox.decompile_artifact, b"MZ", "sample.exe")
        assert entered.wait(timeout=2)
        try:
            with pytest.raises(sandbox.EngineerSandboxError) as captured:
                sandbox.compile_java_artifact(b"class Main {}", "Main.java")
            assert captured.value.code == "compiler_busy"
            assert captured.value.work_started is False
            assert calls == ["decompile"]
        finally:
            release.set()
        assert first.result(timeout=2)["ok"] is True

    entered.clear()
    release.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_compile = executor.submit(
            sandbox.compile_java_artifact,
            b"class Main {}",
            "Main.java",
        )
        assert entered.wait(timeout=2)
        try:
            with pytest.raises(sandbox.EngineerSandboxError) as captured:
                sandbox.decompile_artifact(b"MZ", "sample.exe")
            assert captured.value.code == "decompiler_busy"
            assert calls == ["decompile", "compile_java"]
        finally:
            release.set()
        assert first_compile.result(timeout=2) == (b"jar", {"ok": True, "status": "completed"})


@pytest.mark.parametrize(
    "error",
    (
        "toolchain_untrusted",
        "invalid_filename",
        "compiler_launch_failed",
        "compiler_failed",
        "compiler_timeout",
        "compiler_output_invalid",
    ),
)
def test_returned_worker_failure_attests_physical_sandbox_entry(
    monkeypatch,
    error: str,
) -> None:
    monkeypatch.setattr(
        sandbox,
        "_run_worker",
        lambda *_args, **_kwargs: ({"ok": False, "error": error}, None),
    )

    with pytest.raises(sandbox.EngineerSandboxError) as captured:
        sandbox.compile_java_artifact(b"class Main {}", "Main.java")

    assert captured.value.code == error
    assert captured.value.work_started is True
