from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from friday.organs.engineer.command.contracts import CommandError
from friday.organs.engineer.command.inputs import (
    EMPTY_INPUT_MANIFEST,
    EMPTY_INPUT_MANIFEST_SHA256,
    INPUT_MANIFEST_SCHEMA,
    MAX_INPUT_FILE_BYTES,
    MAX_INPUT_FILES,
    MAX_INPUT_TOTAL_BYTES,
    CommandInputDescriptor,
    CommandInputManifest,
    canonical_input_filename,
    command_input_descriptor,
    command_input_manifest,
    sanitize_input_filename,
)


def _descriptor(
    position: int,
    identity: int,
    *,
    size_bytes: int = 10,
    filename: str | None = None,
    mime_type: str = "text/plain",
) -> CommandInputDescriptor:
    return command_input_descriptor(
        position=position,
        raw_id=f"raw_{identity:016x}",
        source_identity_sha256=f"{identity % 16:x}" * 64,
        content_sha256=f"{(identity + 1) % 16:x}" * 64,
        size_bytes=size_bytes,
        original_filename=filename or f"source-{identity}.txt",
        mime_type=mime_type,
    )


def _assert_body_safe(value: object) -> None:
    if isinstance(value, dict):
        assert all(type(key) is str for key in value)
        for item in value.values():
            _assert_body_safe(item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_body_safe(item)
        return
    assert type(value) in {str, int}


def test_empty_manifest_has_one_exact_canonical_identity() -> None:
    expected = b'{"files":[],"schema":"friday.engineer.command-input-manifest.v1","total_size_bytes":0}'

    assert CommandInputManifest() == EMPTY_INPUT_MANIFEST
    assert EMPTY_INPUT_MANIFEST.canonical_bytes() == expected
    assert EMPTY_INPUT_MANIFEST.canonical_sha256() == EMPTY_INPUT_MANIFEST_SHA256
    assert CommandInputManifest.from_payload(EMPTY_INPUT_MANIFEST.to_payload()) == EMPTY_INPUT_MANIFEST


def test_manifest_preserves_order_and_binds_stable_unique_paths() -> None:
    first = command_input_manifest((_descriptor(1, 1), _descriptor(2, 2)))
    reordered = command_input_manifest((_descriptor(1, 2), _descriptor(2, 1)))

    assert [item.raw_id for item in first.files] == ["raw_0000000000000001", "raw_0000000000000002"]
    assert [item.sandbox_path for item in first.files] == [
        "/job/input/01-source-1.txt",
        "/job/input/02-source-2.txt",
    ]
    assert first.canonical_sha256() != reordered.canonical_sha256()
    assert CommandInputManifest.from_payload(first.to_payload()) == first


def test_descriptors_are_immutable_and_serialize_metadata_only() -> None:
    descriptor = _descriptor(
        1,
        3,
        filename="/home/owner/private/report.txt",
        mime_type="Text/Plain; charset=UTF-8",
    )
    manifest = command_input_manifest((descriptor,))
    payload = manifest.to_payload()

    assert descriptor.original_filename == "report.txt"
    assert descriptor.mime_type == "text/plain"
    with pytest.raises(FrozenInstanceError):
        descriptor.raw_id = "raw_ffffffffffffffff"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        manifest.files = ()  # type: ignore[misc]
    _assert_body_safe(payload)
    rendered = json.dumps(payload, sort_keys=True)
    assert "/home/owner" not in rendered
    assert "host_path" not in rendered
    assert "content" not in payload["files"][0]


@pytest.mark.parametrize(
    ("filename", "canonical", "component"),
    [
        ("../../etc/passwd", "passwd", "passwd"),
        (r"C:\\Users\\Dest\\NUL", "_NUL", "_NUL"),
        ("..\x00/CON?.txt", "CON_.txt", "CON_.txt"),
        ("\u202e report\n.txt ", "_ report_.txt", "_-report_.txt"),
        (".", "input.bin", "input.bin"),
    ],
)
def test_filename_canonicalization_blocks_traversal_control_and_reserved_names(
    filename: str,
    canonical: str,
    component: str,
) -> None:
    assert canonical_input_filename(filename) == canonical
    assert sanitize_input_filename(filename) == component
    assert sanitize_input_filename(filename) == sanitize_input_filename(filename)
    assert "/" not in component and "\\" not in component and component not in {".", ".."}


def test_filename_bounds_count_unicode_by_encoded_bytes() -> None:
    value = "ж" * 200 + ".txt"
    canonical = canonical_input_filename(value)
    component = sanitize_input_filename(value)

    assert len(canonical.encode("utf-8")) <= 180
    assert len(component.encode("utf-8")) <= 120


def test_manifest_enforces_per_file_total_and_count_limits() -> None:
    assert _descriptor(1, 1, size_bytes=MAX_INPUT_FILE_BYTES).size_bytes == MAX_INPUT_FILE_BYTES
    with pytest.raises(CommandError, match="input_file_size_invalid"):
        _descriptor(1, 1, size_bytes=MAX_INPUT_FILE_BYTES + 1)

    over_total = tuple(
        _descriptor(position, position, size_bytes=MAX_INPUT_FILE_BYTES) for position in range(1, 4)
    )
    with pytest.raises(CommandError, match="input_manifest_size_invalid"):
        command_input_manifest(over_total)

    with pytest.raises(CommandError, match="input_manifest_count_invalid"):
        CommandInputManifest(files=(_descriptor(1, 1),) * (MAX_INPUT_FILES + 1))
    assert MAX_INPUT_TOTAL_BYTES == 2 * MAX_INPUT_FILE_BYTES


def test_duplicate_raw_ids_and_sandbox_paths_fail_closed() -> None:
    first = _descriptor(1, 1)
    with pytest.raises(CommandError, match="input_raw_id_duplicate"):
        command_input_manifest((first, replace(_descriptor(2, 2), raw_id=first.raw_id)))

    same_path = _descriptor(1, 2, filename=first.original_filename)
    with pytest.raises(CommandError, match="input_sandbox_path_duplicate"):
        command_input_manifest((first, same_path))


def test_descriptor_and_manifest_parsers_reject_noncanonical_bodies() -> None:
    descriptor = _descriptor(1, 1)
    descriptor_payload = descriptor.to_payload()
    manifest_payload = command_input_manifest((descriptor,)).to_payload()

    with pytest.raises(CommandError, match="input_descriptor_shape_invalid"):
        CommandInputDescriptor.from_payload({**descriptor_payload, "host_path": "/tmp/source"})
    with pytest.raises(CommandError, match="input_mime_type_invalid"):
        CommandInputDescriptor.from_payload({**descriptor_payload, "mime_type": "Text/Plain"})
    with pytest.raises(CommandError, match="input_sandbox_path_invalid"):
        CommandInputDescriptor.from_payload(
            {**descriptor_payload, "sandbox_path": "/job/input/01-../../escape"}
        )
    with pytest.raises(CommandError, match="input_manifest_shape_invalid"):
        CommandInputManifest.from_payload({**manifest_payload, "schema": "wrong"})
    with pytest.raises(CommandError, match="input_manifest_noncanonical"):
        CommandInputManifest.from_payload({**manifest_payload, "total_size_bytes": True})
    with pytest.raises(CommandError, match="input_manifest_noncanonical"):
        CommandInputManifest.from_payload({**manifest_payload, "total_size_bytes": 999})


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_id": "raw_bad"},
        {"source_identity_sha256": "A" * 64},
        {"content_sha256": "0" * 63},
        {"size_bytes": True},
        {"original_filename": "../unsafe.txt"},
        {"sandbox_path": "/job/input/01-other.txt"},
    ],
)
def test_descriptor_strictly_rejects_noncanonical_identity(changes: dict[str, object]) -> None:
    descriptor = _descriptor(1, 1)
    with pytest.raises(CommandError):
        replace(descriptor, **changes)


def test_manifest_requires_exact_descriptor_type_and_position_order() -> None:
    with pytest.raises(CommandError, match="input_descriptor_invalid"):
        CommandInputManifest(files=({"raw_id": "raw_0000000000000001"},))  # type: ignore[arg-type]
    with pytest.raises(CommandError, match="input_manifest_order_invalid"):
        command_input_manifest((_descriptor(2, 1),))


def test_twelve_files_are_admitted_with_closed_two_digit_paths() -> None:
    files = tuple(_descriptor(position, position, size_bytes=0) for position in range(1, 13))
    manifest = command_input_manifest(files)

    assert len(manifest.files) == MAX_INPUT_FILES
    assert manifest.files[-1].sandbox_path == "/job/input/12-source-12.txt"
    assert manifest.total_size_bytes == 0
    assert manifest.to_payload()["schema"] == INPUT_MANIFEST_SCHEMA
