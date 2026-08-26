"""Focused functional and security contracts for the Ghidra worker boundary."""

from __future__ import annotations

import hashlib
import json
import pathlib
import signal
import subprocess
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from friday.organs.engineer import artifacts, decompiler, sandbox, worker

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _minimal_elf() -> bytes:
    header = bytearray(64)
    header[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")
    header[20:24] = (1).to_bytes(4, "little")
    header[52:54] = (64).to_bytes(2, "little")
    return bytes(header)


def _minimal_pe(pe_offset: int = 128) -> bytes:
    optional_size = 112
    payload = bytearray(pe_offset + 24 + optional_size + 40)
    payload[:2] = b"MZ"
    payload[60:64] = pe_offset.to_bytes(4, "little")
    payload[pe_offset : pe_offset + 4] = b"PE\0\0"
    payload[pe_offset + 4 : pe_offset + 6] = (0x8664).to_bytes(2, "little")
    payload[pe_offset + 6 : pe_offset + 8] = (1).to_bytes(2, "little")
    payload[pe_offset + 20 : pe_offset + 22] = optional_size.to_bytes(2, "little")
    payload[pe_offset + 24 : pe_offset + 26] = (0x20B).to_bytes(2, "little")
    return bytes(payload)


def _fake_toolchain(tmp_path: pathlib.Path, monkeypatch) -> tuple[pathlib.Path, pathlib.Path]:
    ghidra = tmp_path / "ghidra-12.1.3"
    jdk = tmp_path / "jdk-21.0.12.1+1"
    launcher = ghidra / "support/analyzeHeadless"
    java = jdk / "bin/java"
    launcher.parent.mkdir(parents=True)
    java.parent.mkdir(parents=True)
    launcher.write_bytes(b"pinned-ghidra-launcher")
    java.write_bytes(b"pinned-temurin-java")
    ghidra.chmod(0o700)
    launcher.parent.chmod(0o700)
    jdk.chmod(0o700)
    java.parent.chmod(0o700)
    launcher.chmod(0o700)
    java.chmod(0o700)
    monkeypatch.setattr(
        decompiler,
        "_GHIDRA_FILES",
        {"support/analyzeHeadless": _sha256(b"pinned-ghidra-launcher")},
    )
    monkeypatch.setattr(
        decompiler,
        "_JDK_FILES",
        {"bin/java": _sha256(b"pinned-temurin-java")},
    )
    # pytest's tmp_path is intentionally below world-writable /tmp.  Production
    # roots are fixed below the private owner directory and are checked all the
    # way to '/'; this override lets the fixture exercise the tree itself.
    monkeypatch.setattr(decompiler, "_safe_ancestors", lambda _path: True)
    monkeypatch.setattr(
        decompiler,
        "GHIDRA_TREE_SHA256",
        decompiler._tree_identity(  # noqa: SLF001
            ghidra, maximum_bytes=decompiler.MAX_GHIDRA_TREE_BYTES
        ),
    )
    monkeypatch.setattr(
        decompiler,
        "JDK_TREE_SHA256",
        decompiler._tree_identity(  # noqa: SLF001
            jdk, maximum_bytes=decompiler.MAX_JDK_TREE_BYTES
        ),
    )
    return ghidra, jdk


def test_toolchain_paths_and_versions_are_fixed_not_path_discovered() -> None:
    assert pathlib.Path("/home/jericho/.jericho/tools/ghidra-12.1.3") == decompiler.GHIDRA_ROOT
    assert pathlib.Path("/home/jericho/.jericho/tools/jdk-21.0.12.1+1") == decompiler.JDK_ROOT
    assert decompiler.GHIDRA_VERSION == "12.1.3"
    assert decompiler.JDK_VERSION == "21.0.12.1+1"
    assert decompiler.GHIDRA_ARCHIVE_SHA256 == (
        "93a5d11a9ad510622acaaf908c556a7b9b764d338e78a7567f3689bf5081fd54"
    )
    assert decompiler.JDK_ARCHIVE_SHA256 == (
        "ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94"
    )
    assert decompiler.GHIDRA_TREE_SHA256 == (
        "7e40cc12fd330b50478fa3d199a5399a313cc247d4226f7ce674f409727c0200"
    )
    assert decompiler.JDK_TREE_SHA256 == ("662d527334f464cb798d04518f2b1c9fbeea75d8a8230a1127fbc5deff8142a4")
    assert decompiler.host_toolchain_preflight().get("reason") in {
        None,
        "toolchain_missing",
        "toolchain_incomplete",
        "toolchain_untrusted",
    }


def test_exact_files_and_safe_permissions_admit_a_toolchain(tmp_path, monkeypatch) -> None:
    ghidra, jdk = _fake_toolchain(tmp_path, monkeypatch)

    assert decompiler.verify_toolchain(ghidra, jdk) == {
        "ok": True,
        "identity": "pinned_full_tree",
        "tool_name": "ghidra-headless",
        "tool_version": "12.1.3",
        "jdk_version": "21.0.12.1+1",
    }


def test_changed_or_writable_toolchain_is_rejected(tmp_path, monkeypatch) -> None:
    ghidra, jdk = _fake_toolchain(tmp_path, monkeypatch)
    (jdk / "bin/java").write_bytes(b"changed-java")
    assert decompiler.verify_toolchain(ghidra, jdk) == {
        "ok": False,
        "reason": "toolchain_untrusted",
    }

    (jdk / "bin/java").write_bytes(b"pinned-temurin-java")
    (ghidra / "support/analyzeHeadless").chmod(0o720)
    assert decompiler.verify_toolchain(ghidra, jdk) == {
        "ok": False,
        "reason": "toolchain_untrusted",
    }


def test_toolchain_symlink_may_not_escape_its_read_only_tree(tmp_path, monkeypatch) -> None:
    ghidra, jdk = _fake_toolchain(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.write_text("not part of the toolchain", encoding="ascii")
    (ghidra / "escape").symlink_to(outside)

    assert decompiler.verify_toolchain(ghidra, jdk) == {
        "ok": False,
        "reason": "toolchain_untrusted",
    }


def test_unexpected_safe_file_changes_the_full_tree_identity(tmp_path, monkeypatch) -> None:
    ghidra, jdk = _fake_toolchain(tmp_path, monkeypatch)
    (ghidra / "unexpected.jar").write_bytes(b"unchecked executable code")
    (ghidra / "unexpected.jar").chmod(0o600)

    assert decompiler.verify_toolchain(ghidra, jdk) == {
        "ok": False,
        "reason": "toolchain_untrusted",
    }


def test_decompiler_bwrap_mounts_only_exact_read_only_tool_trees() -> None:
    normal = sandbox._sandbox_argv(pathlib.Path("/private/work"))  # noqa: SLF001
    argv = sandbox._sandbox_argv(  # noqa: SLF001
        pathlib.Path("/private/work"), mount_decompiler=True
    )

    assert "--unshare-all" in argv
    assert "--unshare-user" in argv
    assert str(decompiler.GHIDRA_ROOT) not in normal
    assert str(decompiler.JDK_ROOT) not in normal
    assert argv[argv.index(str(decompiler.GHIDRA_ROOT)) - 1] == "--ro-bind"
    assert argv[argv.index(str(decompiler.GHIDRA_ROOT)) + 1] == str(decompiler.SANDBOX_GHIDRA_ROOT)
    assert argv[argv.index(str(decompiler.JDK_ROOT)) - 1] == "--ro-bind"
    assert argv[argv.index(str(decompiler.JDK_ROOT)) + 1] == str(decompiler.SANDBOX_JDK_ROOT)
    assert "/home" not in argv
    assert str(decompiler.TOOLS_ROOT) not in argv


def test_decompiler_has_distinct_finite_process_limits() -> None:
    argv = sandbox._limited_sandbox_argv(  # noqa: SLF001
        pathlib.Path("/private/work"), action="decompile", mount_decompiler=True
    )

    assert argv[:7] == [
        "/usr/bin/prlimit",
        "--core=0:0",
        "--cpu=960:961",
        "--fsize=134217728:134217728",
        "--as=8589934592:8589934592",
        "--nofile=512:512",
        "--",
    ]
    assert sandbox.DECOMPILE_MAX_WALL_SECONDS == 240.0
    assert decompiler.HEADLESS_TIMEOUT_SECONDS < sandbox.DECOMPILE_MAX_WALL_SECONDS


def test_decompiler_single_flight_rejects_concurrent_sync_call(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    completed_report = {"ok": True, "status": "completed"}

    def blocking_worker(action, *_args, **_kwargs):  # noqa: ANN001
        calls.append(action)
        entered.set()
        assert release.wait(timeout=5)
        return completed_report, None

    monkeypatch.setattr(sandbox, "_run_worker", blocking_worker)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(sandbox.decompile_artifact, b"MZ", "sample.exe")
        assert entered.wait(timeout=2)
        try:
            with pytest.raises(sandbox.EngineerSandboxError) as captured:
                sandbox.decompile_artifact(b"MZ", "second.exe")
            assert captured.value.code == "decompiler_busy"
            assert calls == ["decompile"]
        finally:
            release.set()
        assert first.result(timeout=2) == completed_report

    # The physical slot is released after the first worker terminates.
    release.set()
    assert sandbox.decompile_artifact(b"MZ", "third.exe") == completed_report
    assert calls == ["decompile", "decompile"]


def test_expired_worker_deadline_never_enters_preflight_or_spawns(monkeypatch) -> None:
    monkeypatch.setattr(
        sandbox,
        "preflight",
        lambda: pytest.fail("expired worker entered sandbox preflight"),
    )
    monkeypatch.setattr(
        sandbox.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("expired worker spawned a process"),
    )

    with pytest.raises(sandbox.EngineerSandboxError) as captured:
        sandbox._run_worker(  # noqa: SLF001
            "decompile",
            _minimal_pe(),
            "expired.exe",
            deadline=time.monotonic() - 1.0,
        )

    assert captured.value.code == "deadline_expired"


def test_post_spawn_deadline_failure_kills_and_reaps_worker_group(
    monkeypatch,
    tmp_path,
) -> None:
    waits: list[float | None] = []
    kills: list[tuple[int, signal.Signals]] = []

    class SpawnedProcess:
        pid = 4242
        returncode: int | None = None

        def wait(self, timeout=None):  # noqa: ANN001
            waits.append(timeout)
            if timeout is not None:
                raise sandbox.EngineerSandboxError("deadline_expired")
            self.returncode = -signal.SIGKILL
            return self.returncode

        def poll(self):  # noqa: ANN201
            return self.returncode

    process = SpawnedProcess()
    monkeypatch.setattr(sandbox, "preflight", lambda: {"ok": True})
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def killpg(pid: int, sent_signal: signal.Signals) -> None:
        kills.append((pid, sent_signal))

    monkeypatch.setattr(sandbox.os, "killpg", killpg)

    with pytest.raises(sandbox.EngineerSandboxError) as captured:
        sandbox._run_worker(  # noqa: SLF001
            "analyze",
            b"bounded-input",
            "sample.bin",
            deadline=time.monotonic() + 30.0,
            workspace_root=tmp_path,
        )

    assert captured.value.code == "deadline_expired"
    assert kills == [(4242, signal.SIGKILL)]
    assert len(waits) == 2
    assert waits[0] is not None and 0 < waits[0] <= 30.0
    assert waits[1] is None
    assert process.returncode == -signal.SIGKILL


def test_headless_argv_is_static_read_only_and_has_no_raw_shell() -> None:
    argv = decompiler._headless_argv(  # noqa: SLF001
        pathlib.Path("/work/input.bin"),
        pathlib.Path("/work/ghidra-decompile.json"),
        pathlib.Path("/app/friday/organs/engineer/ghidra_scripts"),
    )

    assert argv[0] == "/opt/friday-ghidra/support/analyzeHeadless"
    assert argv[argv.index("-import") + 1] == "/work/input.bin"
    assert "-readOnly" in argv
    assert "-deleteProject" in argv
    assert argv[argv.index("-recursive") + 1] == "0"
    assert "-process" not in argv
    assert "-preScript" not in argv
    assert not any(value in {"sh", "bash", "-c"} for value in argv)
    source = (ROOT / "friday/organs/engineer/decompiler.py").read_text(encoding="utf-8")
    assert "shell=False" in source
    assert "os.system" not in source


def test_unsupported_format_does_not_enter_toolchain_or_spawn(monkeypatch, tmp_path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"not a native executable")
    monkeypatch.setattr(
        decompiler,
        "sandbox_toolchain_preflight",
        lambda: pytest.fail("unsupported input reached the toolchain"),
    )
    monkeypatch.setattr(
        decompiler.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("unsupported input was launched"),
    )

    result = decompiler.decompile_artifact(artifact, "macho", {"ok": True})

    assert result["ok"] is False
    assert result["status"] == "unsupported"
    assert result["error"] == "unsupported_format"
    assert result["sample_executed"] is False


def test_missing_toolchain_error_is_fixed_and_content_free(monkeypatch, tmp_path) -> None:
    artifact = tmp_path / "owned-secret-name"
    artifact.write_bytes(_minimal_elf() + b"secret-material")
    monkeypatch.setattr(
        decompiler.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("missing toolchain spawned a process"),
    )

    result = decompiler.decompile_artifact(
        artifact,
        "elf",
        {"ok": False, "reason": "secret-material /private/path"},
    )

    assert result["status"] == "unavailable"
    assert result["error"] == "toolchain_untrusted"
    assert "secret" not in json.dumps(result)
    assert str(artifact) not in json.dumps(result)


def test_headless_result_is_revalidated_and_bounded(monkeypatch, tmp_path) -> None:
    artifact = tmp_path / "input.bin"
    artifact.write_bytes(_minimal_elf())
    captured: dict[str, Any] = {}

    class _CompletedProcess:
        pid = 12345
        returncode: int | None = None

        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            payload = {
                "schema": decompiler.SCHEMA,
                "language_id": "x86:LE:64:default",
                "compiler_spec_id": "gcc",
                "analysis_timed_out": False,
                "function_count_lower_bound": 1,
                "function_index_truncated": False,
                "pseudocode_chars": 20_000,
                "output_truncated": False,
                "functions": [
                    {
                        "address": "00100000",
                        "name": "f" * 400,
                        "signature": "int main(void)",
                        "pseudocode": "A" * 20_000,
                        "decompile_status": "completed",
                        "pseudocode_truncated": True,
                        "thunk": False,
                        "unadmitted": "ignored",
                    }
                ],
            }
            pathlib.Path(argv[-1]).write_text(json.dumps(payload), encoding="utf-8")

        def wait(self, timeout=None) -> int:
            captured["timeout"] = timeout
            self.returncode = 0
            return 0

        def poll(self) -> int | None:
            return self.returncode

    monkeypatch.setattr(decompiler, "sandbox_toolchain_preflight", lambda: {"ok": True})
    monkeypatch.setattr(decompiler.subprocess, "Popen", _CompletedProcess)

    result = decompiler.decompile_artifact(artifact, "elf", {"ok": True})

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert len(result["functions"]) == 1
    assert len(result["functions"][0]["name"]) == decompiler.MAX_FUNCTION_NAME_CHARS
    assert len(result["functions"][0]["pseudocode"]) == (decompiler.MAX_PSEUDOCODE_CHARS_PER_FUNCTION)
    assert "unadmitted" not in result["functions"][0]
    assert captured["timeout"] == decompiler.HEADLESS_TIMEOUT_SECONDS
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdout"] is subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is subprocess.DEVNULL
    environment = captured["kwargs"]["env"]
    assert set(environment).isdisjoint({"HTTP_PROXY", "HTTPS_PROXY", "SSH_AUTH_SOCK"})
    assert environment["GHIDRA_HEADLESS_MAXMEM"] == "3072M"
    assert "-Xmx3072m" in environment["JAVA_TOOL_OPTIONS"]
    assert "-XX:ActiveProcessorCount=4" in environment["JAVA_TOOL_OPTIONS"]
    assert "-Dcpu.core.override=4" in environment["GHIDRA_HEADLESS_JAVA_OPTIONS"]


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("fake.exe", b"plain text"),
        ("fake.so", b"plain text"),
        ("fake.exe", b"MZ" + b"A" * 30 + b"PE\0\0" + b"A" * 80),
    ],
)
def test_spoofed_native_headers_never_enter_toolchain(
    monkeypatch, tmp_path, filename: str, payload: bytes
) -> None:
    request = tmp_path / "request.json"
    artifact = tmp_path / "input.bin"
    result = tmp_path / "result.json"
    output = tmp_path / "output.bin"
    request.write_text(
        json.dumps(
            {
                "protocol": worker.PROTOCOL_VERSION,
                "action": "decompile",
                "filename": filename,
                "decompiler_toolchain": {"ok": True},
            }
        ),
        encoding="utf-8",
    )
    artifact.write_bytes(payload)
    result.write_bytes(b"")
    output.write_bytes(b"")
    monkeypatch.setattr(
        decompiler,
        "sandbox_toolchain_preflight",
        lambda: pytest.fail("malformed native header reached the toolchain"),
    )
    monkeypatch.setattr(
        decompiler.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("malformed native header was launched"),
    )

    assert worker.run(request, artifact, result, output) == 0
    parsed = json.loads(result.read_text(encoding="utf-8"))
    assert parsed["status"] == "unsupported"
    assert parsed["error"] == "unsupported_format"
    assert parsed["format"] == "unknown"


def test_fd_header_parser_admits_pe_with_long_dos_stub(monkeypatch, tmp_path) -> None:
    artifact = tmp_path / "long-stub.bin"
    artifact.write_bytes(_minimal_pe(pe_offset=2048))
    assert artifacts.classify_kind(artifact.read_bytes(), artifact.name) == "dos"
    monkeypatch.setattr(
        decompiler,
        "sandbox_toolchain_preflight",
        lambda: pytest.fail("missing toolchain reached mounted preflight"),
    )

    result = decompiler.decompile_artifact(
        artifact,
        "dos",
        {"ok": False, "reason": "toolchain_missing"},
    )

    assert result["status"] == "unavailable"
    assert result["error"] == "toolchain_missing"
    assert result["format"] == "pe"


def test_worker_reports_unsupported_before_missing_toolchain(tmp_path) -> None:
    request = tmp_path / "request.json"
    artifact = tmp_path / "input.bin"
    result = tmp_path / "result.json"
    output = tmp_path / "output.bin"
    request.write_text(
        json.dumps(
            {
                "protocol": worker.PROTOCOL_VERSION,
                "action": "decompile",
                "filename": "note.txt",
                "decompiler_toolchain": {"ok": False, "reason": "toolchain_missing"},
            }
        ),
        encoding="utf-8",
    )
    artifact.write_bytes(b"plain data")
    result.write_bytes(b"")
    output.write_bytes(b"")

    assert worker.run(request, artifact, result, output) == 0
    parsed = json.loads(result.read_text(encoding="utf-8"))
    assert parsed["protocol"] == worker.PROTOCOL_VERSION
    assert parsed["status"] == "unsupported"
    assert parsed["error"] == "unsupported_format"


def test_java_export_and_wheel_data_are_explicitly_bounded() -> None:
    java = (ROOT / "friday/organs/engineer/ghidra_scripts/FridayDecompile.java").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "MAX_FUNCTIONS = 32" in java
    assert "MAX_SCANNED_FUNCTIONS = 4096" in java
    assert "MAX_TOTAL_PSEUDOCODE_CHARS = 160000" in java
    assert "MAX_JSON_BYTES = 512 * 1024" in java
    assert "StandardOpenOption.CREATE_NEW" in java
    assert "Runtime.getRuntime" not in java
    assert "ProcessBuilder" not in java
    assert project["tool"]["setuptools"]["package-data"]["friday.organs.engineer"] == [
        "ghidra_scripts/*.java"
    ]


def test_decompiled_json_reader_refuses_oversized_output(tmp_path) -> None:
    path = tmp_path / "ghidra-decompile.json"
    path.write_bytes(b"{" + b"x" * decompiler.MAX_HEADLESS_JSON_BYTES + b"}")

    assert decompiler._read_headless_json(path) is None  # noqa: SLF001
