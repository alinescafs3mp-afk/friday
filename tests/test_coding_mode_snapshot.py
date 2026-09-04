from __future__ import annotations

from friday.orchestration.coding_mode_snapshot import (
    CodingModeSnapshotReason,
    CodingModeSnapshotState,
    build_coding_mode_snapshot,
)


SHA256 = "a" * 64


def test_empty_and_digest_bound_snapshot() -> None:
    assert build_coding_mode_snapshot("snapshot-1", "turn-1").state is CodingModeSnapshotState.EMPTY
    result = build_coding_mode_snapshot("snapshot-1", "turn-1", {"src/main.py": SHA256})
    assert result.state is CodingModeSnapshotState.SNAPSHOT
    assert result.names == ("src/main.py",)
    assert result.digests["src/main.py"] == SHA256


def test_unsafe_names_and_secret_names_fail_closed_without_names() -> None:
    for name, reason in (
        ("../escape.py", CodingModeSnapshotReason.PATH_TRAVERSAL),
        ("/tmp/file.py", CodingModeSnapshotReason.ABSOLUTE_PATH),
        (".env", CodingModeSnapshotReason.SECRET_NAME),
        ("Readme.md", CodingModeSnapshotReason.CASEFOLD_COLLISION),
    ):
        values = [(name, SHA256)]
        if reason is CodingModeSnapshotReason.CASEFOLD_COLLISION:
            values.append(("README.md", SHA256))
        result = build_coding_mode_snapshot("snapshot-1", "turn-1", values)
        assert result.state is CodingModeSnapshotState.BLOCKED
        assert result.reason is reason
        assert result.names == ()


def test_bad_digest_and_mapping_roundtrip() -> None:
    invalid = build_coding_mode_snapshot("snapshot-1", "turn-1", {"main.py": "bad"})
    assert invalid.state is CodingModeSnapshotState.BLOCKED
    result = build_coding_mode_snapshot("snapshot-1", "turn-1", {"main.py": SHA256})
    assert build_coding_mode_snapshot(result.to_mapping()) == result
