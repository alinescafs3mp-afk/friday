from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from friday.organs.engineer.command import CommandError, GeneratedFile
from friday.organs.engineer.command.workspace import JobWorkspace


def _sealed_output(
    tmp_path: Path,
    payload: bytes = b"exact sealed bytes",
    *,
    relative_path: str = "nested/result.bin",
) -> tuple[JobWorkspace, GeneratedFile, Path]:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    workspace = JobWorkspace(job_dir)
    workspace.materialize()
    sealed = workspace.sealed.joinpath(*relative_path.split("/"))
    sealed.parent.mkdir(parents=True, exist_ok=True)
    sealed.write_bytes(payload)
    sealed.chmod(0o400)
    generated = GeneratedFile(
        relative_path=relative_path,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        mode=0o644,
    )
    return workspace, generated, sealed


def test_read_generated_file_verified_returns_exact_nested_bytes(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    workspace = JobWorkspace(job_dir)
    workspace.materialize()
    output = workspace.output / "nested" / "result.bin"
    output.parent.mkdir()
    output.write_bytes(b"exact sealed bytes")
    generated_files = workspace.admit_generated_files()

    assert len(generated_files) == 1
    assert workspace.read_generated_file_verified(generated_files[0]) == b"exact sealed bytes"


@pytest.mark.parametrize(
    "relative_path",
    ("../evidence/stdout.bin", "/etc/passwd", "nested//result.bin", "./result.bin", "result.bin/.."),
)
def test_read_generated_file_verified_refuses_path_escape(tmp_path: Path, relative_path: str) -> None:
    workspace, generated, _sealed = _sealed_output(tmp_path)
    forged = GeneratedFile(
        relative_path=relative_path,
        size_bytes=generated.size_bytes,
        sha256=generated.sha256,
        mode=generated.mode,
    )

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(forged)


@pytest.mark.parametrize("field", ("size", "sha256"))
def test_read_generated_file_verified_refuses_receipt_mismatch(tmp_path: Path, field: str) -> None:
    workspace, generated, _sealed = _sealed_output(tmp_path)
    forged = GeneratedFile(
        relative_path=generated.relative_path,
        size_bytes=generated.size_bytes + (1 if field == "size" else 0),
        sha256="0" * 64 if field == "sha256" else generated.sha256,
        mode=generated.mode,
    )

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(forged)


def test_read_generated_file_verified_refuses_tampered_bytes(tmp_path: Path) -> None:
    workspace, generated, sealed = _sealed_output(tmp_path)
    sealed.chmod(0o600)
    sealed.write_bytes(b"tampr sealed bytes")
    sealed.chmod(0o400)
    assert sealed.stat().st_size == generated.size_bytes

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(generated)


def test_read_generated_file_verified_refuses_file_symlink(tmp_path: Path) -> None:
    workspace, generated, sealed = _sealed_output(tmp_path)
    sealed.unlink()
    sealed.symlink_to("/etc/passwd")

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(generated)


def test_read_generated_file_verified_refuses_intermediate_symlink(tmp_path: Path) -> None:
    workspace, generated, sealed = _sealed_output(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.bin").write_bytes(b"exact sealed bytes")
    sealed.unlink()
    sealed.parent.rmdir()
    sealed.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(generated)


def test_read_generated_file_verified_refuses_hardlink(tmp_path: Path) -> None:
    workspace, generated, sealed = _sealed_output(tmp_path)
    other = sealed.with_name("other.bin")
    os.link(sealed, other)

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(generated)


def test_read_generated_file_verified_detects_path_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * 131072
    workspace, generated, sealed = _sealed_output(tmp_path, payload)
    real_read = os.read
    replaced = False

    def _replace_after_first_read(fd: int, count: int) -> bytes:
        nonlocal replaced
        chunk = real_read(fd, count)
        if chunk and not replaced:
            replacement = sealed.with_name("replacement.bin")
            replacement.write_bytes(payload)
            replacement.chmod(0o400)
            os.replace(replacement, sealed)
            replaced = True
        return chunk

    monkeypatch.setattr(os, "read", _replace_after_first_read)

    with pytest.raises(CommandError, match="corrupt_generated_output"):
        workspace.read_generated_file_verified(generated)
    assert replaced is True
