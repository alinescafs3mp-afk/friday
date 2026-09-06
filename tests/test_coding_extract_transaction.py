"""Archive-write faults must not alter existing files or claim a complete workspace."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

import friday.organs.coding.extract as extract
from tests.test_coding_archive_extract import _zip_bytes


def _observe(workspace: Path, raw: bytes):
    return extract.observe_coding_archive_extract(
        extract_id="extract.txn", authenticated_turn_id="turn.txn", workspace=workspace, raw=raw
    )


@pytest.mark.parametrize("kind", ["root_alias", "ancestor_alias", "member_alias", "broken_alias"])
def test_extract_transaction_refuses_existing_aliases(tmp_path, kind) -> None:
    root = tmp_path / "work"
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "root_alias":
        root.symlink_to(outside, target_is_directory=True)
    elif kind == "ancestor_alias":
        root.symlink_to(outside, target_is_directory=True)
        root = root / "nested"
    else:
        root.mkdir()
        (root / "app.py").symlink_to(outside / ("real.py" if kind == "member_alias" else "missing.py"))
        if kind == "member_alias":
            (outside / "real.py").write_bytes(b"owner source")
    result = _observe(root, _zip_bytes({"app.py": b"new source"}))
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert not (outside / "app.py").exists()
    assert not (outside / "nested").exists()
    assert not (outside / "missing.py").exists()
    if kind == "member_alias":
        assert (outside / "real.py").read_bytes() == b"owner source"


def test_extract_transaction_rolls_back_files_after_a_late_write_failure(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    original = os.write
    calls = 0

    def fail_later(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated full disk")
        return original(descriptor, data)

    monkeypatch.setattr(os, "write", fail_later)
    result = _observe(root, _zip_bytes({"a.py": b"first", "nested/b.py": b"second"}))
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert calls == 2
    assert not list(root.rglob("*"))


def test_extract_transaction_cannot_clobber_a_file_created_after_preflight(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    original = os.open
    raced = False

    def race(path, flags, *args, **kwargs):
        nonlocal raced
        if not raced and str(path).endswith("app.py") and flags & os.O_CREAT:
            raced = True
            (root / "app.py").write_bytes(b"concurrent owner source")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, race})
    result = _observe(root, _zip_bytes({"app.py": b"replacement"}))
    assert raced
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert (root / "app.py").read_bytes() == b"concurrent owner source"


def test_extract_transaction_rejects_oversized_input_before_zip_parser(tmp_path, monkeypatch) -> None:
    raw = _zip_bytes({"app.py": b"safe"})
    monkeypatch.setattr(extract, "MAX_INPUT_ARCHIVE_BYTES", len(raw) - 1, raising=False)
    monkeypatch.setattr(extract.zipfile, "ZipFile", lambda *a, **k: pytest.fail("oversized parser input"))
    result = _observe(tmp_path / "work", raw)
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED


def test_extract_attachment_size_is_bounded_before_base64_decode(monkeypatch) -> None:
    monkeypatch.setattr(extract, "MAX_INPUT_ARCHIVE_BYTES", 10, raising=False)
    monkeypatch.setattr(base64, "b64decode", lambda *a, **k: pytest.fail("oversized decode"))
    assert extract.archive_bytes_from_attachment({"content_b64": "A" * 100}) is None
    assert extract.archive_bytes_from_attachment({"content": b"x" * 11}) is None


def test_extract_member_stream_uses_an_explicit_read_bound(tmp_path, monkeypatch) -> None:
    raw = _zip_bytes({"app.py": b"bounded source"})
    original = extract.zipfile.ZipFile.open
    observed = []

    class Reader:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wrapped.close()

        def read(self, size=-1):
            observed.append(size)
            assert size >= 0, "unbounded decompression"
            return self.wrapped.read(size)

    def open_member(self, *args, **kwargs):
        return Reader(original(self, *args, **kwargs))

    monkeypatch.setattr(extract.zipfile.ZipFile, "open", open_member)
    result = _observe(tmp_path / "work", raw)
    assert result.state is extract.CodingArchiveExtractObserveState.EXTRACTED
    assert observed


def test_extract_transaction_preserves_original_files_and_permissions_on_refusal(tmp_path) -> None:
    root = tmp_path / "work"
    root.mkdir(mode=0o750)
    target = root / "app.py"
    target.write_bytes(b"owner")
    target.chmod(0o400)
    result = _observe(root, _zip_bytes({"app.py": b"changed"}))
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert target.read_bytes() == b"owner"
    assert target.stat().st_mode & 0o777 == 0o400
    assert root.stat().st_mode & 0o777 == 0o750


def test_extract_transaction_interrupt_removes_only_new_files(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "owner.txt").write_bytes(b"keep")
    original = os.write
    calls = 0

    def interrupt(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(descriptor, data)

    monkeypatch.setattr(os, "write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _observe(root, _zip_bytes({"a.py": b"first", "nested/b.py": b"second"}))
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == ["owner.txt"]
    assert (root / "owner.txt").read_bytes() == b"keep"


def test_extract_transaction_handles_short_writes_and_keeps_workspace_identity(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    before = (root.stat().st_dev, root.stat().st_ino)
    original = os.write
    monkeypatch.setattr(os, "write", lambda descriptor, data: original(descriptor, data[:2]))
    result = _observe(root, _zip_bytes({"pkg/__init__.py": b"", "pkg/main.py": b"print(123456)\n"}))
    assert result.state is extract.CodingArchiveExtractObserveState.EXTRACTED
    assert (root.stat().st_dev, root.stat().st_ino) == before
    assert (root / "pkg/main.py").read_bytes() == b"print(123456)\n"
    assert (root / "pkg/__init__.py").read_bytes() == b""
    assert (root / "pkg/main.py").stat().st_mode & 0o777 == 0o600
    assert (root / "pkg").stat().st_mode & 0o777 == 0o700


def test_extract_transaction_sync_failure_cannot_leave_a_partial_result(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    original = os.fsync
    calls = 0

    def fail_sync(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated sync failure")
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_sync)
    result = _observe(root, _zip_bytes({"a.py": b"first", "b.py": b"second"}))
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert not list(root.rglob("*"))


def test_extract_rollback_does_not_remove_another_writers_replacement(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    original = os.write
    calls = 0

    def replace_then_fail(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "a.py").rename(root / "original-moved.py")
            (root / "a.py").write_bytes(b"concurrent replacement")
            raise OSError("abort our second member")
        return original(descriptor, data)

    monkeypatch.setattr(os, "write", replace_then_fail)
    result = _observe(root, _zip_bytes({"a.py": b"first", "b.py": b"second"}))
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert (root / "a.py").read_bytes() == b"concurrent replacement"
    assert (root / "original-moved.py").read_bytes() == b"first"
    assert not (root / "b.py").exists()


def test_extract_rechecks_parent_alias_on_the_actual_file_open(tmp_path, monkeypatch) -> None:
    root = tmp_path / "work"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = os.open
    raced = False

    def race(path, flags, *args, **kwargs):
        nonlocal raced
        if str(path) == "nested" and not raced and (root / "nested").is_dir():
            raced = True
            (root / "nested").rename(root / "old-nested")
            (root / "nested").symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, race})
    result = _observe(root, _zip_bytes({"nested/app.py": b"not outside"}))
    assert raced
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert not list(outside.iterdir())


@pytest.mark.parametrize("raw", [True, 123, "PKfake", [1], bytearray(b"PKfake")])
def test_extract_rejects_non_bytes_without_unhandled_exceptions(tmp_path, raw) -> None:
    assert _observe(tmp_path / "work", raw).state is extract.CodingArchiveExtractObserveState.BLOCKED


def test_extract_decompression_failure_happens_before_any_file_is_written(tmp_path, monkeypatch) -> None:
    raw = _zip_bytes({"a.py": b"first", "b.py": b"second"})
    original = extract.zipfile.ZipFile.open
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise extract.zipfile.BadZipFile("simulated invalid second member CRC")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(extract.zipfile.ZipFile, "open", fail_second)
    root = tmp_path / "work"
    result = _observe(root, raw)
    assert result.state is extract.CodingArchiveExtractObserveState.BLOCKED
    assert not list(root.rglob("*"))


@pytest.mark.parametrize("parts", [("..",), ("/tmp",), ("nested/other",), ("",), (".",)])
def test_coding_relative_directory_helper_cannot_escape_its_descriptor(tmp_path, parts) -> None:
    from friday.organs.coding.workspace_io import open_relative_directory

    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError), open_relative_directory(descriptor, parts):
            pytest.fail("invalid relative component reached a directory")
    finally:
        os.close(descriptor)
