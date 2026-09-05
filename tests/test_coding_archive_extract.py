from __future__ import annotations

import base64
import io
import stat
import zipfile
from pathlib import Path

from friday.organs.coding.extract import (
    CodingArchiveExtractObserveReason,
    CodingArchiveExtractObserveState,
    archive_bytes_from_attachment,
    first_archive_bytes,
    observe_coding_archive_extract,
)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _zip_info_bytes(info: zipfile.ZipInfo, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, payload)
    return buffer.getvalue()


def _observe(tmp_path: Path, raw: bytes | None):
    workspace = tmp_path / "workspace"
    return observe_coding_archive_extract(
        extract_id="extract.1",
        authenticated_turn_id="turn.1",
        workspace=workspace,
        raw=raw,
    ), workspace


def test_extract_module_never_extracts_or_executes() -> None:
    source = Path(observe_coding_archive_extract.__code__.co_filename).read_text(encoding="utf-8")
    assert "extractall" not in source
    assert "ZipFile.extract" not in source
    assert ".extract(" not in source
    assert "subprocess" not in source
    assert "import docker" not in source


def test_missing_bytes_and_non_zip_are_empty(tmp_path: Path) -> None:
    empty, workspace = _observe(tmp_path, None)
    assert empty.state is CodingArchiveExtractObserveState.EMPTY
    assert empty.reason is CodingArchiveExtractObserveReason.NO_ARCHIVE
    assert empty.untrusted_execute is False
    assert not workspace.exists()
    assert archive_bytes_from_attachment({"filename": "app.zip", "path": "/etc/passwd"}) is None
    assert first_archive_bytes([{"filename": "app.zip", "path": str(tmp_path / "missing.zip")}]) is None
    text, _ = _observe(tmp_path, b"print(1)\n")
    assert text.state is CodingArchiveExtractObserveState.EMPTY


def test_host_path_attachment_is_never_opened(tmp_path: Path) -> None:
    payload = _zip_bytes({"src/main.py": b"print(1)\n"})
    host = tmp_path / "on-disk.zip"
    host.write_bytes(payload)
    host.chmod(0)
    raw = archive_bytes_from_attachment({"filename": "on-disk.zip", "path": str(host), "size": len(payload)})
    assert raw is None
    result, workspace = _observe(tmp_path, raw)
    assert result.state is CodingArchiveExtractObserveState.EMPTY
    assert not workspace.exists()
    encoded = base64.standard_b64encode(payload).decode("ascii")
    assert archive_bytes_from_attachment({"path": str(host), "content_b64": encoded}) == payload


def test_admitted_regular_files_are_extracted_without_execute(tmp_path: Path) -> None:
    payload = _zip_bytes({"src/main.py": b"print(1)\n", "README.md": b"hi\n"})
    result, workspace = _observe(tmp_path, payload)
    assert result.state is CodingArchiveExtractObserveState.EXTRACTED
    assert result.extracted_count == 2
    assert result.untrusted_execute is False
    assert result.digest_state == "bound"
    assert result.overwrite_state == "clear"
    assert (workspace / "src" / "main.py").read_bytes() == b"print(1)\n"
    assert (workspace / "README.md").read_bytes() == b"hi\n"


def test_existing_destination_blocks_extract_without_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "main.py").write_bytes(b"old\n")
    result = observe_coding_archive_extract(
        extract_id="extract.1",
        authenticated_turn_id="turn.1",
        workspace=workspace,
        raw=_zip_bytes({"src/main.py": b"new\n"}),
    )
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.reason is CodingArchiveExtractObserveReason.OVERWRITE_COLLISION
    assert result.overwrite_state == "collision"
    assert (workspace / "src" / "main.py").read_bytes() == b"old\n"


def test_zip_slip_is_blocked_and_writes_nothing(tmp_path: Path) -> None:
    info = zipfile.ZipInfo("../escape.txt")
    result, workspace = _observe(tmp_path, _zip_info_bytes(info, b"nope"))
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.reason is CodingArchiveExtractObserveReason.ADMISSION_NOT_GRANTED
    assert result.extracted_count == 0
    assert not (tmp_path / "escape.txt").exists()
    assert not list(workspace.rglob("*")) if workspace.exists() else True


def test_symlink_member_is_blocked_without_write(tmp_path: Path) -> None:
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    result, workspace = _observe(tmp_path, _zip_info_bytes(info, b"/tmp/target"))
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.reason is CodingArchiveExtractObserveReason.ADMISSION_NOT_GRANTED
    assert not (workspace / "link").exists() if workspace.exists() else True


def test_mixed_safe_and_symlink_writes_nothing(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/main.py", b"print(1)\n")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"/tmp/target")
    result, workspace = _observe(tmp_path, buffer.getvalue())
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.untrusted_execute is False
    assert not (workspace / "src" / "main.py").exists() if workspace.exists() else True


def test_device_member_is_blocked(tmp_path: Path) -> None:
    info = zipfile.ZipInfo("dev/null")
    info.create_system = 3
    info.external_attr = (stat.S_IFCHR | 0o666) << 16
    result, workspace = _observe(tmp_path, _zip_info_bytes(info, b""))
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.reason is CodingArchiveExtractObserveReason.ADMISSION_NOT_GRANTED
    assert not list(workspace.rglob("*")) if workspace.exists() else True


def test_corrupt_zip_magic_is_blocked(tmp_path: Path) -> None:
    result, workspace = _observe(tmp_path, b"PK\x03\x04not-a-zip")
    assert result.state is CodingArchiveExtractObserveState.BLOCKED
    assert result.reason is CodingArchiveExtractObserveReason.INVALID_ARCHIVE
    assert not workspace.exists()


def test_zipfile_extract_is_never_called(tmp_path: Path, monkeypatch) -> None:
    def _boom(self, *args, **kwargs):  # noqa: ANN001
        raise AssertionError("ZipFile.extract was called")

    monkeypatch.setattr(zipfile.ZipFile, "extract", _boom)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", _boom)
    result, workspace = _observe(tmp_path, _zip_bytes({"src/main.py": b"print(1)\n"}))
    assert result.state is CodingArchiveExtractObserveState.EXTRACTED
    assert (workspace / "src" / "main.py").read_bytes() == b"print(1)\n"
    assert result.untrusted_execute is False
