from __future__ import annotations

import base64
import errno
import hashlib
import os
import stat
from pathlib import Path

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
            probe._ProcessObservation(
                pid=101,
                epoch_sha256=hashlib.sha256(b"epoch").hexdigest(),
                reference_count=len(matches),
                reference_sha256=reference_sha256 or hashlib.sha256(b"references").hexdigest(),
                matches=matches,
            ),
        ),
    )


def _capture(value: probe._GlobalObservation):
    def capture(_target_index: probe.TargetIndex) -> probe._GlobalObservation:
        return value

    return capture


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
                    "schema": probe.TARGET_INDEX_SCHEMA,
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

    forged = probe.TargetIndex(left.targets, "0" * 64, left.object_count)
    with pytest.raises(probe.ProcProbeInputError, match="target_index_digest_invalid"):
        probe.probe_namespace_visible_proc_references(forged, _capture_pass=_capture(_observation()))


def test_clear_receipt_is_canonical_bounded_and_explicitly_not_universal(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFDIR))
    observation = _observation()

    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(observation),
    )

    assert receipt["status"] == "clear"
    assert receipt["complete"] is True
    assert receipt["scope"] == "namespace_visible_proc_references"
    assert receipt["universal_absence_proof"] is False
    assert receipt["fixed_point_passes"] == 2
    assert receipt["target_index_sha256"] == index.sha256
    canonical = probe.canonical_probe_receipt_bytes(receipt)
    assert canonical.endswith(b"\n")
    assert len(canonical) < probe.MAX_RECEIPT_BYTES


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
        hashlib.sha256(b"epoch").hexdigest(),
        reference,
    )

    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation(matches=(match,))),
    )

    assert receipt["status"] == "referenced"
    assert receipt["complete"] is True
    assert receipt["matches"][0]["object"] == object_key.projection()
    assert base64.b64decode(receipt["matches"][0]["link_target_base64"]) == link_target


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
    (process / "fdinfo" / "7").write_text("pos:\t0\nflags:\t0100000\nmnt_id:\t81\n", encoding="ascii")
    (process / "cwd").symlink_to(target_directory, target_is_directory=True)
    (process / "root").symlink_to(target_directory, target_is_directory=True)
    (process / "exe").symlink_to(target_file)
    address = "1000-2000"
    (process / "map_files" / address).symlink_to(target_file)
    status = target_file.stat()
    (process / "maps").write_text(
        f"00001000-00002000 r--p 00000000 {os.major(status.st_dev):x}:{os.minor(status.st_dev):x} "
        f"{status.st_ino} {target_file}\n",
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
        references.extend(scanner._map_references(process_fd, 123))
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


def test_maps_without_one_exact_map_files_object_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    index = _index(tmp_path, probe.ObjectKey.from_stat(target.stat()))
    process = tmp_path / "proc" / "123"
    (process / "map_files").mkdir(parents=True)
    status = target.stat()
    (process / "maps").write_text(
        f"1000-2000 r--p 00000000 {os.major(status.st_dev):x}:{os.minor(status.st_dev):x} "
        f"{status.st_ino} {target}\n",
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
        probe._ProbeIssue("proc_permission_denied", pid=7, source="map_files"),
        probe._ProbeIssue("proc_maps_invalid", pid=7, source="maps"),
        probe._ProbeIssue("proc_surface_unsupported", source="proc"),
        probe._ProbeIssue("proc_observation_raced", pid=7, source="fd"),
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
    assert receipt["complete"] is False
    assert receipt["matches"] == []
    assert receipt["ambiguities"] == [{"code": issue.code, "pid": issue.pid, "source": issue.source}]


def test_eacces_and_eperm_have_one_closed_permission_code() -> None:
    for error_number in (errno.EACCES, errno.EPERM):
        issue = probe._issue_from_oserror(
            PermissionError(error_number, "denied"),
            pid=42,
            source="map_files",
        )
        assert (issue.code, issue.pid, issue.source) == (
            "proc_permission_denied",
            42,
            "map_files",
        )


def test_fixed_point_change_is_ambiguous_even_when_both_passes_are_individually_valid(
    tmp_path: Path,
) -> None:
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
    assert receipt["complete"] is False
    assert receipt["ambiguities"][0]["code"] == "proc_fixed_point_changed"


def test_pid_epoch_binds_boot_pid_starttime_and_proc_inode() -> None:
    boot = hashlib.sha256(b"boot").hexdigest()
    baseline = probe._pid_epoch_sha256(boot, 123, 456, (7, 8))

    assert (
        len(
            {
                baseline,
                probe._pid_epoch_sha256(hashlib.sha256(b"other-boot").hexdigest(), 123, 456, (7, 8)),
                probe._pid_epoch_sha256(boot, 124, 456, (7, 8)),
                probe._pid_epoch_sha256(boot, 123, 457, (7, 8)),
                probe._pid_epoch_sha256(boot, 123, 456, (7, 9)),
            }
        )
        == 5
    )


def test_canonical_receipt_rejects_tampering_and_non_linux_proc_roots(tmp_path: Path) -> None:
    index = _index(tmp_path, probe.ObjectKey(8, 11, stat.S_IFREG))
    receipt = probe.probe_namespace_visible_proc_references(
        index,
        _capture_pass=_capture(_observation()),
    )
    receipt["status"] = "referenced"
    with pytest.raises(probe.ProcProbeInputError, match="probe_receipt_invalid"):
        probe.canonical_probe_receipt_bytes(receipt)

    unsupported = probe.probe_namespace_visible_proc_references(index, proc_root=tmp_path)
    assert unsupported["status"] == "ambiguous"
    assert unsupported["complete"] is False
    assert unsupported["ambiguities"][0]["code"] == "proc_surface_unsupported"
