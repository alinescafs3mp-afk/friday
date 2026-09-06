"""End-to-end source-carrier regressions using only disposable local files."""

from __future__ import annotations

import base64
import gc
import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

import friday.organs.coding.result_archive as export
from friday.orchestration.operation_result_carrier import (
    OperationResultCarrierError,
    pack_operation_result_archive,
    plan_generated_file_documents,
)


def _workspace(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "work"
    root.mkdir(mode=0o700)
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def _pack(workspace: Path, output: Path):
    return export.observe_coding_result_archive(
        turn_id="turn.export", workspace=workspace, export_path=output, ready=True
    )


def _payload(result) -> bytes:
    assert len(result.files) == 1
    return base64.b64decode(result.files[0]["content_base64"], validate=True)


def test_filtered_internal_carrier_cannot_replace_the_selected_user_document() -> None:
    plan, documents = plan_generated_file_documents(
        [
            {"id": "private", "filename": "receipts/internal.json", "payload": b"PRIVATE MARKER"},
            {"id": "public", "filename": "report.txt", "payload": b"the requested report"},
        ]
    )
    assert [item.relative_path for item in plan.files] == ["report.txt"]
    assert len(documents) == 1
    assert documents[0].filename == "report.txt"
    assert documents[0].artifact_id == "public"
    assert documents[0].payload == b"the requested report"


def test_source_package_retains_empty_python_package_initializers() -> None:
    payload = pack_operation_result_archive(
        [("package/__init__.py", b""), ("package/main.py", b"def main(): return 42\n")]
    )
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["package/__init__.py", "package/main.py"]
        assert archive.read("package/__init__.py") == b""


def test_shared_archive_rejects_oversize_before_allocating_a_zip(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("oversized source bytes reached ZipFile")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(OperationResultCarrierError, match="size_limit"):
        pack_operation_result_archive([("a.txt", b"a" * 70), ("b.txt", b"b" * 70)], max_archive_bytes=100)


def test_archive_size_preflight_accounts_for_utf8_names_and_headers() -> None:
    members = [("первый.py", b"a"), ("второй.py", b"b")]
    raw = pack_operation_result_archive(members)
    assert pack_operation_result_archive(members, max_archive_bytes=len(raw)) == raw
    with pytest.raises(OperationResultCarrierError, match="size_limit"):
        pack_operation_result_archive(members, max_archive_bytes=len(raw) - 1)


def test_coding_archive_is_deterministic_and_uses_the_shared_packer(tmp_path, monkeypatch) -> None:
    members = {"main.py": b"print(42)\n", "README.md": b"Usage: python main.py\n"}
    root = _workspace(tmp_path, members)
    monkeypatch.setattr(zipfile.time, "localtime", lambda *args: (2026, 9, 6, 10, 0, 0, 6, 249, 0))
    first = _pack(root, tmp_path / "out-one")
    monkeypatch.setattr(zipfile.time, "localtime", lambda *args: (2027, 2, 7, 17, 20, 8, 6, 38, 0))
    second = _pack(root, tmp_path / "out-two")
    assert first.state is second.state is export.CodingResultArchiveObserveState.ARCHIVE
    assert _payload(first) == _payload(second) == pack_operation_result_archive(list(members.items()))


def test_source_export_excludes_runtime_caches_vcs_and_secret_paths(tmp_path) -> None:
    root = _workspace(
        tmp_path,
        {
            "main.py": b"print(42)\n",
            "__pycache__/main.cpython-314.pyc": b"compiled",
            ".git/config": b"private repository configuration",
            ".pytest_cache/README.md": b"cache",
            ".venv/bin/activate": b"local environment",
            "loose.pyc": b"compiled",
            ".env": b"not user source",
            "logs/worker.log": b"internal execution details",
        },
    )
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.FILE
    assert result.files[0]["filename"] == "main.py"
    assert _payload(result) == b"print(42)\n"


def test_snapshot_uses_the_same_bytes_for_manifest_archive_and_final_document(tmp_path) -> None:
    members = {"package/__init__.py": b"", "package/main.py": b"value = 42\n"}
    result = _pack(_workspace(tmp_path, members), tmp_path / "out")
    raw = _payload(result)
    assert (tmp_path / "out" / "friday-source.zip").read_bytes() == raw
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == members
    assert result.carrier.carrier.value == "archive"
    assert result.untrusted_execute is False
    assert stat.S_IMODE((tmp_path / "out" / "friday-source.zip").stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ("workspace", "ancestor", "member", "directory", "hardlink", "fifo"))
def test_untrusted_filesystem_aliases_and_special_members_are_not_exported(tmp_path, kind) -> None:
    root = _workspace(tmp_path, {"main.py": b"legitimate"})
    external = tmp_path / "outside"
    external.mkdir()
    (external / "marker.py").write_bytes(b"PRIVATE-OUTSIDE-MARKER")
    if kind == "workspace":
        root = tmp_path / "alias"
        root.symlink_to(external, target_is_directory=True)
    elif kind == "ancestor":
        (external / "child").mkdir()
        (external / "child/main.py").write_bytes(b"PRIVATE-OUTSIDE-MARKER")
        alias = tmp_path / "alias"
        alias.symlink_to(external, target_is_directory=True)
        root = alias / "child"
    elif kind == "member":
        (root / "alias.py").symlink_to(external / "marker.py")
    elif kind == "directory":
        (root / "package").symlink_to(external, target_is_directory=True)
    elif kind == "hardlink":
        os.link(external / "marker.py", root / "alias.py")
    else:
        os.mkfifo(root / "pipe")
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()
    assert (external / "marker.py").read_bytes() == b"PRIVATE-OUTSIDE-MARKER"


@pytest.mark.parametrize("relation", ("equal", "inside", "ancestor"))
def test_workspace_and_export_must_be_disjoint(tmp_path, relation) -> None:
    root = _workspace(tmp_path, {"main.py": b"source"})
    output = {"equal": root, "inside": root / "out", "ancestor": tmp_path}[relation]
    result = _pack(root, output)
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert (root / "main.py").read_bytes() == b"source"


def test_input_limit_is_enforced_before_export_mutation(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"large.py": b"x" * 65})
    monkeypatch.setattr(export, "MAX_RESULT_INPUT_BYTES", 64, raising=False)
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()
    assert not (tmp_path / "out/large.py").exists()


def test_mutation_during_snapshot_does_not_publish_mixed_revisions(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"a.py": b"first", "b.py": b"second"})
    original_read = os.read
    changed = False

    def race(fd, size):
        nonlocal changed
        chunk = original_read(fd, size)
        if chunk and not changed:
            changed = True
            (root / "a.py").write_bytes(b"new-a")
            (root / "b.py").write_bytes(b"new-bb")
        return chunk

    monkeypatch.setattr(os, "read", race)
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()


def test_existing_different_export_is_not_overwritten(tmp_path) -> None:
    root = _workspace(tmp_path, {"main.py": b"new revision"})
    output = tmp_path / "out"
    output.mkdir(mode=0o700)
    destination = output / "main.py"
    destination.write_bytes(b"previous accepted revision")
    before = destination.stat()
    result = _pack(root, output)
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert destination.read_bytes() == b"previous accepted revision"
    assert destination.stat().st_ino == before.st_ino
    assert list(output.iterdir()) == [destination]


def test_repeated_same_snapshot_is_idempotent_and_leaves_no_temporary_files(tmp_path) -> None:
    root = _workspace(tmp_path, {"main.py": b"same revision"})
    output = tmp_path / "out"
    first = _pack(root, output)
    before = (output / "main.py").stat()
    second = _pack(root, output)
    assert first.state is second.state is export.CodingResultArchiveObserveState.FILE
    assert _payload(first) == _payload(second)
    assert (output / "main.py").stat().st_ino == before.st_ino
    assert [p.name for p in output.iterdir()] == ["main.py"]


def test_export_directory_symlink_cannot_redirect_writes(tmp_path) -> None:
    root = _workspace(tmp_path, {"main.py": b"source"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "out").symlink_to(outside, target_is_directory=True)
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert list(outside.iterdir()) == []


def test_interrupted_export_preserves_previous_files_and_removes_partial(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"main.py": b"source"})
    original_fsync = os.fsync

    def fail_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("synthetic full disk")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_file)
    output = tmp_path / "out"
    result = _pack(root, output)
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()
    assert not output.exists() or list(output.iterdir()) == []


@pytest.mark.parametrize("names", (("café.py", "cafe\u0301.py"), ("main.py", "MAIN.py")))
def test_portably_colliding_source_names_are_not_packed(names) -> None:
    with pytest.raises(OperationResultCarrierError, match="duplicate"):
        pack_operation_result_archive([(name, b"source") for name in names])


def test_non_utf8_path_is_rejected_as_a_closed_carrier_error() -> None:
    with pytest.raises(OperationResultCarrierError, match="path_invalid"):
        pack_operation_result_archive([("bad\udcff.py", b"a"), ("good.py", b"b")])


def test_invalid_local_filename_does_not_escape_as_an_unhandled_exception(tmp_path) -> None:
    root = _workspace(tmp_path, {"main.py": b"source", "bad\udcff.py": b"bad name"})
    result = _pack(root, tmp_path / "out")
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()


@pytest.mark.parametrize("mime", ("text/plain\r\nInjected: value", "text/plain\x7f"))
def test_document_mime_control_bytes_are_rejected(mime) -> None:
    with pytest.raises(OperationResultCarrierError, match="mime_type_invalid"):
        plan_generated_file_documents([{"filename": "report.txt", "payload": b"text", "mime_type": mime}])


def test_generated_document_decoder_checks_size_before_base64_allocation(monkeypatch) -> None:
    import friday.orchestration.operation_result_carrier as carrier

    monkeypatch.setattr(carrier, "MAX_OPERATION_RESULT_ARCHIVE_BYTES", 4)
    monkeypatch.setattr(base64, "b64decode", lambda *args, **kwargs: pytest.fail("oversized decode"))
    with pytest.raises(OperationResultCarrierError, match="size_limit"):
        plan_generated_file_documents([{"filename": "report.txt", "content_base64": "AAAA" * 100}])


def test_generated_document_count_is_checked_before_decoding(monkeypatch) -> None:
    monkeypatch.setattr(base64, "b64decode", lambda *args, **kwargs: pytest.fail("unbounded decode"))
    with pytest.raises(OperationResultCarrierError, match="count_limit"):
        plan_generated_file_documents(
            [{"filename": f"report-{n}.txt", "content_base64": "eA=="} for n in range(33)]
        )


def test_generated_document_total_budget_is_not_per_file(monkeypatch) -> None:
    import friday.orchestration.operation_result_carrier as carrier

    monkeypatch.setattr(carrier, "MAX_OPERATION_RESULT_ARCHIVE_BYTES", 6)
    with pytest.raises(OperationResultCarrierError, match="size_limit"):
        plan_generated_file_documents(
            [{"filename": "a.txt", "payload": b"1234"}, {"filename": "b.txt", "payload": b"5678"}]
        )


def test_concurrent_export_creation_does_not_overwrite_the_winning_revision(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"main.py": b"requested source"})
    output = tmp_path / "out"
    original_link = os.link
    raced = False

    def race(source, destination, **kwargs):
        nonlocal raced
        raced = True
        (output / "main.py").write_bytes(b"concurrent accepted revision")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", race)
    result = _pack(root, output)
    assert raced
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert (output / "main.py").read_bytes() == b"concurrent accepted revision"
    assert [p.name for p in output.iterdir()] == ["main.py"]


def test_alias_swap_between_inventory_and_open_does_not_read_outside(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"main.py": b"legitimate"})
    outside = tmp_path / "private-marker"
    outside.write_bytes(b"PRIVATE OUTSIDE")
    original_open = os.open
    raced = False

    def race(path, flags, *args, **kwargs):
        nonlocal raced
        if path == "main.py" and not raced:
            raced = True
            (root / "main.py").unlink()
            (root / "main.py").symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, race})
    result = _pack(root, tmp_path / "out")
    assert raced
    assert result.state is export.CodingResultArchiveObserveState.BLOCKED
    assert result.files == ()
    assert outside.read_bytes() == b"PRIVATE OUTSIDE"


def test_source_snapshot_closes_all_descriptors_on_repeated_rejection(tmp_path) -> None:
    root = _workspace(tmp_path, {f"file-{n}.py": b"x" for n in range(33)})
    # Prior API tests may leave collectable SQLite/socket wrappers. Reclaim
    # those before the baseline and keep GC from changing unrelated FD counts
    # mid-probe. Exact equality still proves our explicit rejection cleanup.
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(10):
            assert _pack(root, tmp_path / "out").state is export.CodingResultArchiveObserveState.BLOCKED
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        if was_enabled:
            gc.enable()


def test_directory_inventory_is_bounded_before_collecting_unlimited_entries(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"main.py": b"source"})
    for n in range(5):
        (root / f"dir{n}").mkdir()
    monkeypatch.setattr(export, "MAX_ARCHIVE_MEMBER_COUNT", 4)
    assert _pack(root, tmp_path / "out").state is export.CodingResultArchiveObserveState.BLOCKED


def test_directory_depth_is_bounded(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path, {"a/b/c/main.py": b"source"})
    monkeypatch.setattr(export, "MAX_ARCHIVE_NESTING_DEPTH", 2)
    assert _pack(root, tmp_path / "out").state is export.CodingResultArchiveObserveState.BLOCKED


def test_manifest_and_carrier_keep_the_captured_bytes_after_later_workspace_changes(
    tmp_path, monkeypatch
) -> None:
    import hashlib

    members = {"a.py": b"revision A", "b.py": b"revision B"}
    root = _workspace(tmp_path, members)
    original = export.build_coding_result_archive_manifest
    seen = []

    def observe(manifest_id, turn_id, digests):
        seen.append(digests)
        (root / "a.py").write_bytes(b"a later workspace revision")
        return original(manifest_id, turn_id, digests)

    monkeypatch.setattr(export, "build_coding_result_archive_manifest", observe)
    result = _pack(root, tmp_path / "out")
    assert seen == [{name: hashlib.sha256(body).hexdigest() for name, body in members.items()}]
    with zipfile.ZipFile(io.BytesIO(_payload(result))) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == members


def test_explicit_internal_metadata_cannot_be_published_under_an_ordinary_name() -> None:
    plan, documents = plan_generated_file_documents(
        [
            {"filename": "debug.txt", "internal": True, "payload": b"PRIVATE INTERNAL MARKER"},
            {"filename": "report.txt", "internal": False, "payload": b"owner result"},
        ]
    )
    assert [item.relative_path for item in plan.files] == ["report.txt"]
    assert len(documents) == 1 and documents[0].payload == b"owner result"


@pytest.mark.parametrize("flag", ["false", 0, None])
def test_ambiguous_internal_flags_fail_closed(flag) -> None:
    from friday.orchestration.engineer_result_carrier import (
        EngineerResultFile,
        select_user_result_files,
    )

    with pytest.raises(OperationResultCarrierError, match="internal_flag_invalid"):
        plan_generated_file_documents([{"filename": "report.txt", "internal": flag, "payload": b"x"}])
    with pytest.raises(OperationResultCarrierError, match="internal_flag_invalid"):
        select_user_result_files(EngineerResultFile("report.txt", internal=flag))
