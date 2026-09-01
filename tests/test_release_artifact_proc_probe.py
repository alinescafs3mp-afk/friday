from __future__ import annotations

import base64
import copy
import errno
import hashlib
import json
import os
import select
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools import release_artifact_proc_probe as probe


def _index(tmp_path: Path, *objects: probe.ObjectKey) -> probe.TargetIndex:
    return probe.build_target_index(
        [probe.ProbeTarget("retired-release", (tmp_path / "release",), tuple(objects))]
    )


def _scope(seed: str = "scope") -> probe._ScopeIdentity:
    return probe._ScopeIdentity(hashlib.sha256(seed.encode()).hexdigest(), (1, 2), (3, 4), (5, 6))


def _observation(
    *,
    scope: probe._ScopeIdentity | None = None,
    reference_sha256: str | None = None,
    matches: tuple[probe._Match, ...] = (),
) -> probe._GlobalObservation:
    return probe._GlobalObservation(
        scope or _scope(),
        (
            probe._TaskObservation(
                tgid=101,
                tid=101,
                epoch_sha256=hashlib.sha256(b"epoch").hexdigest(),
                reference_count=len(matches),
                reference_sha256=reference_sha256 or hashlib.sha256(b"references").hexdigest(),
                shared_mm_proof_sha256=hashlib.sha256(b"shared-mm").hexdigest(),
                matches=matches,
            ),
        ),
    )


def _capture(value: probe._GlobalObservation):
    def capture(_target_index: probe.TargetIndex) -> probe._GlobalObservation:
        return value

    return capture


def _resign(receipt: dict[str, Any]) -> None:
    core = {name: value for name, value in receipt.items() if name != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(probe._canonical_json(core)).hexdigest()


def _match(object_key: probe.ObjectKey, *, entry: str = "9") -> probe._Match:
    return probe._Match(
        ("retired-release",),
        101,
        101,
        hashlib.sha256(b"epoch").hexdigest(),
        probe._Reference("fd", entry, object_key, 77, f"/alias/{entry}".encode()),
    )


def test_target_index_is_exact_canonical_and_rejects_a_forged_digest(tmp_path: Path) -> None:
    first = probe.ObjectKey(8, 22, stat.S_IFREG)
    second = probe.ObjectKey(8, 11, stat.S_IFDIR)
    left = probe.build_target_index(
        [
            probe.ProbeTarget(
                "b",
                (tmp_path / "z", tmp_path / "a"),
                (first, second, first),
            ),
            probe.ProbeTarget("a", (tmp_path / "r",), (second,)),
        ]
    )
    right = probe.build_target_index(
        [
            probe.ProbeTarget("a", (tmp_path / "r",), (second,)),
            probe.ProbeTarget("b", (tmp_path / "a", tmp_path / "z"), (second, first)),
        ]
    )

    assert left == right
    assert (
        left.sha256
        == hashlib.sha256(
            probe._canonical_json(
                {
                    "object_count": 3,
                    "root_count": 3,
                    "schema": probe.TARGET_INDEX_SCHEMA,
                    "target_count": 2,
                    "targets": [
                        {
                            "objects": [second.projection()],
                            "roots": [str(tmp_path / "r")],
                            "target_id": "a",
                        },
                        {
                            "objects": [second.projection(), first.projection()],
                            "roots": [str(tmp_path / "a"), str(tmp_path / "z")],
                            "target_id": "b",
                        },
                    ],
                }
            )
        ).hexdigest()
    )

    forged = probe.TargetIndex(left.targets, "0" * 64, left.object_count, left.root_count)
    with pytest.raises(probe.ProcProbeInputError, match="target_index_digest_invalid"):
        probe.probe_namespace_visible_proc_references(forged, _capture_pass=_capture(_observation()))


def test_target_roots_are_count_and_byte_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    object_key = probe.ObjectKey(8, 11, stat.S_IFREG)
    monkeypatch.setattr(probe, "MAX_TARGET_ROOTS", 1)
    with pytest.raises(probe.ProcProbeInputError, match="target_root_limit_exceeded"):
        probe.build_target_index(
            [probe.ProbeTarget("release", (tmp_path / "a", tmp_path / "b"), (object_key,))]
        )

    monkeypatch.setattr(probe, "MAX_TARGET_ROOTS", 2)
    monkeypatch.setattr(probe, "MAX_TARGET_ROOT_BYTES", len(os.fsencode(tmp_path)) + 2)
    with pytest.raises(probe.ProcProbeInputError, match="target_root_limit_exceeded"):
        probe.build_target_index([probe.ProbeTarget("release", (tmp_path / "long-name",), (object_key,))])


def test_clear_receipt_is_diagnostic_only_canonical_and_bounded(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFDIR))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation()),
    )

    assert receipt["status"] == "clear"
    assert receipt["diagnostic_complete"] is True
    assert "complete" not in receipt
    assert receipt["authority"] == "diagnostic_only"
    assert receipt["delete_authority"] is False
    assert receipt["open_inventory_complete_authority"] is False
    assert receipt["universal_absence_proof"] is False
    assert receipt["scope"] == "namespace_visible_proc_references"
    assert receipt["fixed_point_passes"] == 2
    assert receipt["target_index_sha256"] == index.sha256
    canonical = probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)
    assert canonical.endswith(b"\n")
    assert len(canonical) <= probe.MAX_RECEIPT_BYTES


def test_receipt_cap_includes_digest_and_trailing_newline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFDIR))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation()),
    )
    canonical = probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)
    monkeypatch.setattr(probe, "MAX_RECEIPT_BYTES", len(canonical) - 1)

    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)


@pytest.mark.parametrize(
    ("source", "entry", "link_target"),
    [
        ("fd", "9", b"/outside/by-hardlink"),
        ("map_files", "1000-2000", b"/other-mount/by-bind-alias"),
    ],
)
def test_inode_identity_detects_hardlink_and_bind_aliases_independent_of_path(
    tmp_path: Path,
    source: str,
    entry: str,
    link_target: bytes,
) -> None:
    object_key = probe.ObjectKey(44, 55, stat.S_IFREG)
    index = _index(tmp_path, object_key)
    reference = probe._Reference(source, entry, object_key, 77 if source == "fd" else None, link_target)
    match = probe._Match(
        ("retired-release",),
        101,
        101,
        hashlib.sha256(b"epoch").hexdigest(),
        reference,
    )

    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation(matches=(match,))),
    )

    assert receipt["status"] == "referenced"
    assert receipt["diagnostic_complete"] is True
    assert receipt["matches"][0]["object"] == object_key.projection()
    assert base64.b64decode(receipt["matches"][0]["link_target_base64"]) == link_target
    probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)


def test_linux_surface_readers_cover_fd_cwd_root_exe_and_complete_map_files(tmp_path: Path) -> None:
    target_file = tmp_path / "payload.bin"
    target_file.write_bytes(b"payload")
    target_directory = tmp_path / "payload-dir"
    target_directory.mkdir()
    file_key = probe.ObjectKey.from_stat(target_file.stat())
    directory_key = probe.ObjectKey.from_stat(target_directory.stat())
    index = _index(tmp_path, file_key, directory_key)

    process = tmp_path / "proc" / "123"
    (process / "fd").mkdir(parents=True)
    (process / "fdinfo").mkdir()
    (process / "map_files").mkdir()
    (process / "fd" / "7").symlink_to(target_file)
    (process / "fdinfo" / "7").write_text(
        "pos:\t0\nflags:\t0100000\nmnt_id:\t81\n",
        encoding="ascii",
    )
    (process / "cwd").symlink_to(target_directory, target_is_directory=True)
    (process / "root").symlink_to(target_directory, target_is_directory=True)
    (process / "exe").symlink_to(target_file)
    address = "1000-2000"
    (process / "map_files" / address).symlink_to(target_file)
    status = target_file.stat()
    (process / "maps").write_text(
        f"00001000-00002000 r--p 00000000 {os.major(status.st_dev):x}:"
        f"{os.minor(status.st_dev):x} {status.st_ino} {target_file}\n",
        encoding="ascii",
    )

    scanner = probe._LinuxProcScanner(Path("/proc"), index)
    process_fd = os.open(process, os.O_RDONLY | os.O_DIRECTORY)
    try:
        references = scanner._fd_references(process_fd, 123)
        references.extend(
            reference
            for name in ("cwd", "root", "exe")
            if (reference := scanner._special_reference(process_fd, 123, name)) is not None
        )
        map_references, _maps = scanner._map_references(process_fd, 123)
        references.extend(map_references)
    finally:
        os.close(process_fd)

    assert {reference.source for reference in references} == {
        "fd",
        "cwd",
        "root",
        "exe",
        "map_files",
    }
    assert {reference.object_key for reference in references} == {file_key, directory_key}
    assert next(reference for reference in references if reference.source == "fd").mount_id == 81


def test_privileged_io_uring_conservatively_references_every_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = probe.ProbeTarget(
        "first",
        (tmp_path / "first",),
        (probe.ObjectKey(8, 11, stat.S_IFREG),),
    )
    second = probe.ProbeTarget(
        "second",
        (tmp_path / "second",),
        (probe.ObjectKey(8, 12, stat.S_IFREG),),
    )
    index = probe.build_target_index((first, second))
    process = tmp_path / "proc" / "123"
    (process / "fd").mkdir(parents=True)
    (process / "fdinfo").mkdir()
    (process / "fd" / "7").write_bytes(b"")
    (process / "fdinfo" / "7").write_bytes(b"mnt_id:\t81\n")
    ring = probe._Reference(  # noqa: SLF001
        "fd",
        "7",
        probe.ObjectKey(0, 99, stat.S_IFREG),
        81,
        b"anon_inode:[io_uring]",
    )

    conservative = probe._LinuxProcScanner(  # noqa: SLF001
        Path("/proc"),
        index,
        conservatively_retain_opaque_file_references=True,
    )
    assert conservative.opaque_file_reference_target_ids == ("first", "second")
    monkeypatch.setattr(conservative, "_reference", lambda *args, **kwargs: ring)
    process_fd = os.open(process, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert conservative._fd_references(process_fd, 123) == [ring]  # noqa: SLF001
    finally:
        os.close(process_fd)
    assert conservative.opaque_file_reference_target_ids == ("first", "second")

    namespace = probe._Reference(  # noqa: SLF001
        "fd",
        "7",
        probe.ObjectKey(0, 100, stat.S_IFREG),
        81,
        b"mnt:[4026531840]",
    )
    monkeypatch.setattr(conservative, "_reference", lambda *args, **kwargs: namespace)
    process_fd = os.open(process, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert conservative._fd_references(process_fd, 123) == [namespace]  # noqa: SLF001
    finally:
        os.close(process_fd)

    diagnostic = probe._LinuxProcScanner(Path("/proc"), index)  # noqa: SLF001
    monkeypatch.setattr(diagnostic, "_reference", lambda *args, **kwargs: ring)
    process_fd = os.open(process, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(probe._ProbeIssue, match="^proc_surface_unsupported$"):  # noqa: SLF001
            diagnostic._fd_references(process_fd, 123)  # noqa: SLF001
    finally:
        os.close(process_fd)
    assert diagnostic.opaque_file_reference_target_ids == ()

    observation = _observation()

    class OpaqueScanner:
        def __init__(
            self,
            proc_root: Path,
            target_index: probe.TargetIndex,
            *,
            conservatively_retain_opaque_file_references: bool,
        ) -> None:
            assert proc_root == Path("/proc")
            assert target_index == index
            assert conservatively_retain_opaque_file_references is True

        def __enter__(self) -> OpaqueScanner:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def capture(self) -> probe._GlobalObservation:  # noqa: SLF001
            return observation

        @property
        def opaque_file_reference_target_ids(self) -> tuple[str, ...]:
            return ("first", "second")

    monkeypatch.setattr(probe, "_LinuxProcScanner", OpaqueScanner)
    monkeypatch.setattr(
        probe,
        "_kernel_target_references",
        lambda target_index: ((), "a" * 64),
    )

    captured, referenced, kernel_epoch_sha256 = probe._capture_privileged_target_observation(  # noqa: SLF001
        index
    )

    assert captured == observation
    assert referenced == ("first", "second")
    assert len(kernel_epoch_sha256) == 64
    assert kernel_epoch_sha256 != "a" * 64


def test_maps_without_one_exact_map_files_object_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    index = _index(tmp_path, probe.ObjectKey.from_stat(target.stat()))
    process = tmp_path / "proc" / "123"
    (process / "map_files").mkdir(parents=True)
    status = target.stat()
    (process / "maps").write_text(
        f"1000-2000 r--p 00000000 {os.major(status.st_dev):x}:"
        f"{os.minor(status.st_dev):x} {status.st_ino} {target}\n",
        encoding="ascii",
    )
    scanner = probe._LinuxProcScanner(Path("/proc"), index)
    process_fd = os.open(process, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(probe._ProbeIssue, match="proc_map_files_incomplete"):
            scanner._map_references(process_fd, 123)
    finally:
        os.close(process_fd)


@pytest.mark.parametrize(
    "issue",
    [
        probe._ProbeIssue("proc_permission_denied", tgid=7, tid=8, source="map_files"),
        probe._ProbeIssue("proc_maps_invalid", tgid=7, tid=8, source="maps"),
        probe._ProbeIssue("proc_surface_unsupported", source="proc"),
        probe._ProbeIssue("proc_observation_raced", tgid=7, tid=8, source="fd"),
    ],
)
def test_permission_parse_unsupported_and_race_never_claim_completeness(
    tmp_path: Path,
    issue: probe._ProbeIssue,
) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))

    def fail(_target_index: probe.TargetIndex) -> probe._GlobalObservation:
        raise issue

    receipt = probe.probe_namespace_visible_proc_references(index, _capture_pass=fail)

    assert receipt["status"] == "ambiguous"
    assert receipt["diagnostic_complete"] is False
    assert receipt["delete_authority"] is False
    assert receipt["matches"] == []
    assert receipt["ambiguities"] == [
        {"code": issue.code, "source": issue.source, "tgid": issue.tgid, "tid": issue.tid}
    ]
    probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)


def test_eacces_and_eperm_have_one_closed_permission_code() -> None:
    for error_number in (errno.EACCES, errno.EPERM):
        issue = probe._issue_from_oserror(
            PermissionError(error_number, "denied"),
            pid=42,
            source="map_files",
        )
        assert (issue.code, issue.tgid, issue.tid, issue.source) == (
            "proc_permission_denied",
            42,
            42,
            "map_files",
        )


def test_fixed_point_change_is_ambiguous_even_when_passes_are_valid(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    observations = iter(
        (
            _observation(reference_sha256=hashlib.sha256(b"before").hexdigest()),
            _observation(reference_sha256=hashlib.sha256(b"after").hexdigest()),
        )
    )

    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=lambda _index: next(observations),
    )

    assert receipt["status"] == "ambiguous"
    assert receipt["diagnostic_complete"] is False
    assert receipt["ambiguities"][0]["code"] == "proc_fixed_point_changed"


def test_task_epoch_binds_boot_tgid_tid_starttime_and_proc_inode() -> None:
    boot = hashlib.sha256(b"boot").hexdigest()
    baseline = probe._task_epoch_sha256(boot, 123, 124, 456, (7, 8))

    assert (
        len(
            {
                baseline,
                probe._task_epoch_sha256(hashlib.sha256(b"other-boot").hexdigest(), 123, 124, 456, (7, 8)),
                probe._task_epoch_sha256(boot, 125, 124, 456, (7, 8)),
                probe._task_epoch_sha256(boot, 123, 125, 456, (7, 8)),
                probe._task_epoch_sha256(boot, 123, 124, 457, (7, 8)),
                probe._task_epoch_sha256(boot, 123, 124, 456, (7, 9)),
            }
        )
        == 6
    )


def test_validator_binds_expected_target_index_and_exact_counts(tmp_path: Path) -> None:
    first = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    second = probe.build_target_index(
        [
            probe.ProbeTarget(
                "retired-release",
                (tmp_path / "other",),
                (probe.ObjectKey(8, 12, stat.S_IFREG),),
            )
        ]
    )
    receipt = probe.probe_namespace_visible_proc_references(
        first,
        _capture_pass=_capture(_observation()),
    )

    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(receipt, expected_target_index=second)

    forged = copy.deepcopy(receipt)
    forged["target_object_count"] += 1
    _resign(forged)
    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(forged, expected_target_index=first)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("ambiguities", 0, "code"), "invented_issue"),
        (("ambiguities", 0, "source"), "invented_source"),
    ],
)
def test_validator_rejects_open_ended_issue_taxonomy(
    tmp_path: Path,
    path: tuple[str, int, str],
    value: str,
) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=lambda _index: (_ for _ in ()).throw(
            probe._ProbeIssue("proc_permission_denied", tgid=7, tid=8, source="fd")
        ),
    )
    forged = copy.deepcopy(receipt)
    forged[path[0]][path[1]][path[2]] = value
    _resign(forged)

    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(forged, expected_target_index=index)


def test_validator_rejects_invalid_object_source_order_and_duplicates(tmp_path: Path) -> None:
    object_key = probe.ObjectKey(8, 11, stat.S_IFREG)
    index = _index(tmp_path, object_key)
    matches = tuple(sorted((_match(object_key, entry="8"), _match(object_key, entry="9"))))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation(matches=matches)),
    )
    probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)

    mutations: list[dict[str, Any]] = []
    invalid_object = copy.deepcopy(receipt)
    invalid_object["matches"][0]["object"][1] = 0
    mutations.append(invalid_object)
    invalid_source = copy.deepcopy(receipt)
    invalid_source["matches"][0]["source"] = "other"
    mutations.append(invalid_source)
    reversed_matches = copy.deepcopy(receipt)
    reversed_matches["matches"].reverse()
    mutations.append(reversed_matches)
    duplicate = copy.deepcopy(receipt)
    duplicate["matches"][1] = copy.deepcopy(duplicate["matches"][0])
    mutations.append(duplicate)

    for forged in mutations:
        _resign(forged)
        with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
            probe.canonical_probe_receipt_bytes(forged, expected_target_index=index)


def test_self_authored_receipt_cannot_promote_itself_to_effect_authority(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation()),
    )
    forged = copy.deepcopy(receipt)
    forged["authority"] = "delete"
    forged["delete_authority"] = True
    forged["open_inventory_complete_authority"] = True
    forged["universal_absence_proof"] = True
    _resign(forged)

    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(forged, expected_target_index=index)


def test_unshared_worker_fd_and_cwd_are_seen_when_leader_holds_neither(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "worker-only.bin"
    target.write_bytes(b"worker-only")
    worker_directory = tmp_path / "worker-cwd"
    worker_directory.mkdir()
    file_key = probe.ObjectKey.from_stat(target.stat())
    directory_key = probe.ObjectKey.from_stat(worker_directory.stat())
    index = _index(tmp_path, file_key, directory_key)
    helper = r"""
import ctypes
import os
import sys
import threading

ready = threading.Event()
release = threading.Event()
result = {}
libc = ctypes.CDLL(None, use_errno=True)

def worker():
    if libc.unshare(0x00000200 | 0x00000400) != 0:
        result["errno"] = ctypes.get_errno()
        ready.set()
        return
    os.chdir(sys.argv[2])
    descriptor = os.open(sys.argv[1], os.O_RDONLY)
    try:
        result["tid"] = threading.get_native_id()
        ready.set()
        release.wait()
    finally:
        os.close(descriptor)

thread = threading.Thread(target=worker)
thread.start()
ready.wait()
if "errno" in result:
    print(f"ERR {result['errno']}", flush=True)
else:
    print(f"OK {os.getpid()} {result['tid']}", flush=True)
sys.stdin.buffer.read(1)
release.set()
thread.join()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", helper, str(target), str(worker_directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    try:
        assert child.stdout is not None
        readable, _, _ = select.select([child.stdout], [], [], 5)
        assert readable, "unshare helper did not become ready"
        fields = child.stdout.readline().decode("ascii").strip().split()
        assert fields and fields[0] == "OK", (
            os.strerror(int(fields[1])) if fields and fields[0] == "ERR" else fields
        )
        tgid, worker_tid = int(fields[1]), int(fields[2])
        assert worker_tid != tgid
        assert Path(os.readlink(f"/proc/{tgid}/task/{tgid}/cwd")) != worker_directory

        scanner = probe._LinuxProcScanner(Path("/proc"), index)
        with scanner:
            scope = scanner._scope_identity()
            tgid_fd = scanner._open_pid(tgid)
            try:
                task_directory = scanner._open_directory_at(
                    tgid_fd,
                    "task",
                    pid=tgid,
                    source="task",
                )
                try:
                    tids = scanner._task_names(task_directory, tgid)
                    expected = {}
                    for tid in tids:
                        task_fd = scanner._open_task(task_directory, tgid, tid)
                        try:
                            expected[tid] = scanner._task_epoch_from_fd(
                                task_fd,
                                boot_id_sha256=scope.boot_id_sha256,
                                tgid=tgid,
                                tid=tid,
                            )
                        finally:
                            scanner._close_owned(task_fd)
                finally:
                    scanner._close_owned(task_directory)
            finally:
                scanner._close_owned(tgid_fd)
            assert worker_tid in expected

            # Some test hosts deny even self /proc/*/map_files. This regression
            # isolates TGID/TID closure and preserves the exact maps projection
            # used by the shared-mm proof while bypassing only that host policy.
            def maps_without_privileged_links(
                pid_fd: int,
                pid: int,
            ) -> tuple[list[probe._Reference], tuple[probe._MapRecord, ...]]:
                raw = probe._read_bounded_at(
                    pid_fd,
                    "maps",
                    maximum=probe.MAX_PROC_FILE_BYTES,
                    pid=pid,
                    source="maps",
                )
                return [], probe._parse_maps(raw, pid=pid)

            monkeypatch.setattr(scanner, "_map_references", maps_without_privileged_links)
            # This regression exercises per-TID FS/files closure.  Covered mounts
            # on some CI hosts intentionally make the independent mount proof
            # fail closed, so isolate that separately-tested surface here.
            monkeypatch.setattr(scanner, "_mount_references", lambda _fd, _pid: ([], "6" * 64))
            observations = scanner._scan_tgid(
                tgid,
                expected,
                boot_id_sha256=scope.boot_id_sha256,
            )

        worker_matches = {
            match.reference.source
            for observation in observations
            if observation.tid == worker_tid
            for match in observation.matches
        }
        leader_objects = {
            match.reference.object_key
            for observation in observations
            if observation.tid == tgid
            for match in observation.matches
        }
        assert {"fd", "cwd"} <= worker_matches
        assert file_key not in leader_objects
        assert directory_key not in leader_objects
    finally:
        if child.stdin is not None:
            child.stdin.write(b"x")
            child.stdin.flush()
        try:
            _stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.terminate()
            _stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, stderr.decode(errors="replace")


def test_canonical_receipt_rejects_tampering_and_non_linux_proc_roots(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation()),
    )
    receipt["status"] = "referenced"
    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(receipt, expected_target_index=index)

    unsupported = probe.probe_namespace_visible_proc_references(index, proc_root=tmp_path)
    assert unsupported["status"] == "ambiguous"
    assert unsupported["diagnostic_complete"] is False
    assert unsupported["ambiguities"][0]["code"] == "proc_surface_unsupported"
    probe.canonical_probe_receipt_bytes(unsupported, expected_target_index=index)


def test_same_euid_snapshot_uses_stable_process_identity_not_volatile_cpu_counters(
    tmp_path: Path,
) -> None:
    held = tmp_path / "held.bin"
    held.write_bytes(b"held")
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import os,sys\n"
                "f=open(sys.argv[1],'rb')\n"
                "print('ready',flush=True)\n"
                "value=0\n"
                "while True: value=(value+1)%1000003\n"
            ),
            str(held),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        proc_path = Path("/proc") / str(child.pid)
        expected = os.stat(proc_path, follow_symlinks=False)
        first = probe._same_euid_process_snapshot(  # noqa: SLF001
            Path("/proc"),
            pid=child.pid,
            expected=expected,
        )
        second = probe._same_euid_process_snapshot(  # noqa: SLF001
            Path("/proc"),
            pid=child.pid,
            expected=expected,
        )
        held_status = held.stat()

        assert first[0:2] == second[0:2]
        assert (held_status.st_dev, held_status.st_ino) in first[3]
    finally:
        child.terminate()
        child.communicate(timeout=5)


def test_complete_open_snapshot_requires_an_exact_two_pass_fixed_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stable = ((101, "epoch-a", ("/exact",), ((8, 9),)),)
    monkeypatch.setattr(probe, "_same_euid_open_pass", lambda _root: stable)

    snapshot = probe.snapshot_same_euid_open_files()

    assert snapshot.paths == (Path("/exact"),)
    assert snapshot.identities == ((8, 9),)
    assert snapshot.process_count == 1

    passes = iter((stable, ((101, "epoch-a", ("/changed",), ((8, 9),)),)))
    monkeypatch.setattr(probe, "_same_euid_open_pass", lambda _root: next(passes))
    with pytest.raises(probe.ProcProbeInputError, match="open_inventory_proc_changed"):
        probe.snapshot_same_euid_open_files()


def test_privileged_target_index_round_trip_and_body_free_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = probe.build_target_index(
        (
            probe.ProbeTarget(
                "retired-release-a",
                (tmp_path / "release-a",),
                (probe.ObjectKey(8, 11, stat.S_IFREG),),
            ),
            probe.ProbeTarget(
                "retired-release-b",
                (tmp_path / "release-b",),
                (probe.ObjectKey(8, 12, stat.S_IFREG),),
            ),
        )
    )
    raw = probe.canonical_target_index_bytes(index)
    assert probe.parse_target_index_bytes(raw) == index

    monkeypatch.setattr(probe.os, "geteuid", lambda: 0)
    scope = _scope()
    scope_sha256 = "a" * 64
    monkeypatch.setattr(
        probe,
        "_host_scope_authority",
        lambda: (
            {
                "required_capabilities": ["CAP_SYS_ADMIN", "CAP_SYS_PTRACE"],
                "schema": probe.HOST_SCOPE_AUTHORITY_SCHEMA,
                "scope": "initial_pid_namespace_and_proc_v1",
            },
            scope_sha256,
        ),
    )
    monkeypatch.setattr(probe, "_capture_privileged_no_delete_scope", lambda _index: scope)
    monkeypatch.setattr(
        probe,
        "_capture_privileged_target_observation",
        lambda _index: pytest.fail("the no-delete contour must not scan global processes"),
    )
    receipt = probe.privileged_target_reference_receipt(index)
    canonical = probe.canonical_privileged_receipt_bytes(
        receipt,
        expected_target_index=index,
        expected_implementation_sha256=probe._implementation_sha256(),  # noqa: SLF001
        expected_host_scope_authority_sha256=scope_sha256,
    )

    assert receipt["status"] == "referenced"
    assert receipt["authority"] == probe.PRIVILEGED_NO_DELETE_AUTHORITY
    assert receipt["referenced_target_ids"] == ["retired-release-a", "retired-release-b"]
    assert receipt["task_count"] == receipt["tgid_count"] == 0
    assert str(tmp_path).encode() not in canonical
    assert b"link_target" not in canonical
    assert b'"matches"' not in canonical

    expected_referenced = list(receipt["referenced_target_ids"])
    for forged_referenced in (
        expected_referenced[:-1],
        [*expected_referenced, "unindexed-target"],
        [*expected_referenced, expected_referenced[0]],
        list(reversed(expected_referenced)),
    ):
        forged = copy.deepcopy(receipt)
        forged["referenced_target_ids"] = forged_referenced
        _resign(forged)
        with pytest.raises(probe.ProcProbeInputError, match="privileged_probe_receipt_invalid"):
            probe.canonical_privileged_receipt_bytes(
                forged,
                expected_target_index=index,
                expected_implementation_sha256=probe._implementation_sha256(),  # noqa: SLF001
                expected_host_scope_authority_sha256=scope_sha256,
            )

    for field, value in (
        ("status", "clear"),
        ("task_count", 1),
        ("observer_euid", False),
        ("target_count", True),
        ("task_count", False),
        ("tgid_count", False),
        ("observation_sha256", "f" * 64),
    ):
        forged = copy.deepcopy(receipt)
        forged[field] = value
        _resign(forged)
        with pytest.raises(probe.ProcProbeInputError, match="privileged_probe_receipt_invalid"):
            probe.canonical_privileged_receipt_bytes(
                forged,
                expected_target_index=index,
                expected_implementation_sha256=probe._implementation_sha256(),  # noqa: SLF001
                expected_host_scope_authority_sha256=scope_sha256,
            )


def test_privileged_no_delete_scope_never_enumerates_global_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    scope = _scope()
    calls: list[probe._ScopeIdentity] = []

    class ScopeOnlyScanner:
        def __init__(self, proc_root: Path, target_index: probe.TargetIndex) -> None:
            assert proc_root == Path("/proc")
            assert target_index == index

        def __enter__(self) -> ScopeOnlyScanner:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def _scope_identity(self) -> probe._ScopeIdentity:
            return scope

        def capture(self) -> probe._GlobalObservation:
            pytest.fail("global process capture is forbidden in the no-delete contour")

    monkeypatch.setattr(probe, "_LinuxProcScanner", ScopeOnlyScanner)
    monkeypatch.setattr(probe, "_require_initial_host_scope", calls.append)

    assert probe._capture_privileged_no_delete_scope(index) == scope  # noqa: SLF001
    assert calls == [scope]


def test_unprivileged_complete_snapshot_fails_closed_on_nondumpable_same_uid(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can inspect nondumpable processes")
    held = tmp_path / "held.bin"
    held.write_bytes(b"held")
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import ctypes,sys,time\n"
                "f=open(sys.argv[1],'rb')\n"
                "assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0\n"
                "print('ready',flush=True)\n"
                "time.sleep(30)\n"
            ),
            str(held),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        with pytest.raises(probe.ProcProbeInputError, match="open_inventory_proc_incomplete"):
            probe.snapshot_same_euid_open_files()
    finally:
        child.terminate()
        child.communicate(timeout=5)


def test_privileged_helper_cli_is_import_safe_and_body_free_outside_repo(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    tool = Path(probe.__file__).resolve()
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(tool), "privileged-target-probe"],
        input=probe.canonical_target_index_bytes(index),
        capture_output=True,
        check=False,
        cwd=tmp_path,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert json.loads(result.stderr) == {
        "failure_code": "privileged_probe_authority_invalid",
        "schema": probe.PRIVILEGED_RECEIPT_SCHEMA,
        "source": "proc",
        "status": "failed_closed",
    }
    assert b"Traceback" not in result.stderr
    assert str(tmp_path).encode() not in result.stderr

    outer = probe.ProcProbeInputError("privileged_probe_incomplete")
    outer.__cause__ = probe._ProbeIssue(  # noqa: SLF001
        "proc_surface_unsupported",
        pid=123,
        source="fdinfo",
    )
    assert probe._privileged_failure_projection(outer) == (  # noqa: SLF001
        "proc_surface_unsupported",
        "fdinfo",
    )
    assert probe._privileged_failure_projection(ValueError(str(tmp_path))) == (  # noqa: SLF001
        "privileged_probe_failed_closed",
        "proc",
    )


def test_repeated_mount_cache_hits_do_not_leak_task_root_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = _index(tmp_path, probe.ObjectKey.from_stat(tmp_path.stat()))
    scanner = probe._LinuxProcScanner(Path("/proc"), index)
    with scanner:
        pid = os.getpid()
        pid_fd = scanner._open_pid(pid)
        try:
            namespace = os.stat("ns/mnt", dir_fd=pid_fd, follow_symlinks=True)
            raw = probe._read_bounded_at(  # noqa: SLF001
                pid_fd,
                "mountinfo",
                maximum=probe.MAX_PROC_FILE_BYTES,
                pid=pid,
                source="mountinfo",
            )
            root_fd = os.open(
                "root",
                getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=pid_fd,
            )
            try:
                root = probe.ObjectKey.from_stat(os.fstat(root_fd))
                root_mount = probe._descriptor_unique_mount_id(root_fd)  # noqa: SLF001
            finally:
                os.close(root_fd)
            key = (
                int(namespace.st_dev),
                int(namespace.st_ino),
                hashlib.sha256(raw).hexdigest(),
                root.device,
                root.inode,
                root.file_type,
                root_mount,
            )
            scanner._mount_cache[key] = (  # noqa: SLF001
                (),
                "6" * 64,
                pid,
                raw,
                root,
                root_mount,
            )
            owned_before = set(scanner._owned_fds)  # noqa: SLF001

            class TrackedProbeOS:
                def __init__(self) -> None:
                    self.live: dict[int, tuple[object, object]] = {}
                    self.open_count = 0
                    self.close_count = 0

                def __getattr__(self, name: str) -> Any:
                    return getattr(os, name)

                def open(self, path: object, *args: object, **kwargs: object) -> int:
                    descriptor = os.open(path, *args, **kwargs)  # type: ignore[arg-type]
                    assert descriptor not in self.live
                    self.live[descriptor] = (path, kwargs.get("dir_fd"))
                    self.open_count += 1
                    return descriptor

                def close(self, descriptor: int) -> None:
                    if descriptor in self.live:
                        self.live.pop(descriptor)
                        self.close_count += 1
                    os.close(descriptor)

            tracked_os = TrackedProbeOS()
            monkeypatch.setattr(probe, "os", tracked_os)
            for _ in range(2_048):
                assert scanner._mount_references(pid_fd, pid) == ([], "6" * 64)  # noqa: SLF001
                assert tracked_os.live == {}
            assert scanner._owned_fds == owned_before  # noqa: SLF001
            assert tracked_os.open_count > 0
            assert tracked_os.close_count == tracked_os.open_count
        finally:
            scanner._close_owned(pid_fd)  # noqa: SLF001
