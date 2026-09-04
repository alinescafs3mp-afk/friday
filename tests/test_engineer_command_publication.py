from __future__ import annotations

import base64
import hashlib
import io
import json
import stat
import zipfile
from dataclasses import replace
from typing import Any

import pytest

from friday.orchestration.engineer_result_carrier import select_engineer_result_carrier
from friday.organs.engineer.command.contracts import (
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    CommandLane,
    CommandOrigin,
    CommandReceipt,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
)
from friday.organs.engineer.command.publication import (
    COMMAND_OUTPUT_MANIFEST_SCHEMA,
    COMMAND_OUTPUT_MIME_TYPE,
    COMMAND_OUTPUT_RECEIPT_SCHEMA,
    USER_RESULT_FILE_MIME_TYPE,
    CommandOutputPublicationError,
    build_command_output_archive,
    build_user_result_carrier,
)


def _descriptor(path: str, payload: bytes, *, mode: int = 0o755) -> GeneratedFile:
    return GeneratedFile(
        relative_path=path,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        mode=mode,
    )


def _receipt(
    files: tuple[GeneratedFile, ...],
    *,
    job_id: str = "1" * 32,
    status: CommandStatus = CommandStatus.COMPLETED,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> CommandReceipt:
    return CommandReceipt(
        job_id=job_id,
        status=status,
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        command_digest="2" * 64,
        argv_sha256="3" * 64,
        source_hash="4" * 64,
        exit_code=0,
        signal=None,
        timed_out=False,
        cancelled=False,
        truncated_stdout=False,
        truncated_stderr=False,
        started_at=10.0,
        finished_at=11.0,
        executable=None,
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        stdout=stdout,
        stderr=stderr,
        generated_files=files,
        error_code="",
        effect_boundary_crossed=True,
        receipt_mac="5" * 64,
    )


def _json_member(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    value = json.loads(archive.read(name))
    assert isinstance(value, dict)
    return value


def test_command_output_archive_is_byte_identical_sorted_and_fixed() -> None:
    alpha = b"alpha\n"
    nested = b"nested payload\x00"
    alpha_descriptor = _descriptor("a.txt", alpha, mode=0o600)
    nested_descriptor = _descriptor("reports/z.bin", nested, mode=0o755)
    receipt = _receipt((nested_descriptor, alpha_descriptor))

    first = build_command_output_archive(
        receipt,
        ((nested_descriptor, nested), (alpha_descriptor, alpha)),
    )
    second = build_command_output_archive(
        receipt,
        ((alpha_descriptor, alpha), (nested_descriptor, nested)),
    )

    assert first.payload == second.payload
    assert first.sha256 == hashlib.sha256(first.payload).hexdigest() == second.sha256
    assert first.filename == f"engineer-command-{receipt.job_id}.zip"
    assert first.mime_type == COMMAND_OUTPUT_MIME_TYPE
    assert base64.b64decode(first.attachment()["content_base64"], validate=True) == first.payload

    with zipfile.ZipFile(io.BytesIO(first.payload)) as archive:
        assert archive.namelist() == [
            "MANIFEST.json",
            "RECEIPT.json",
            "outputs/a.txt",
            "outputs/reports/z.bin",
        ]
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert stat.S_IMODE(info.external_attr >> 16) == 0o644
            assert info.extra == b""
            assert info.comment == b""
        assert archive.read("outputs/a.txt") == alpha
        assert archive.read("outputs/reports/z.bin") == nested
        manifest = _json_member(archive, "MANIFEST.json")
        delivery_receipt = _json_member(archive, "RECEIPT.json")

    assert manifest["schema"] == COMMAND_OUTPUT_MANIFEST_SCHEMA
    assert manifest["job_id"] == receipt.job_id
    assert manifest["command_digest"] == receipt.command_digest
    assert [row["relative_path"] for row in manifest["outputs"]] == ["a.txt", "reports/z.bin"]
    assert delivery_receipt["schema"] == COMMAND_OUTPUT_RECEIPT_SCHEMA
    assert delivery_receipt["job_id"] == receipt.job_id
    assert delivery_receipt["command_digest"] == receipt.command_digest
    assert delivery_receipt["command_receipt"]["receipt_mac"] == receipt.receipt_mac
    assert delivery_receipt["command_receipt"]["generated_file_count"] == 2


def test_zero_output_archive_carries_bounded_stdout_and_receipt() -> None:
    receipt = _receipt((), stdout=b"console result\n")
    result = build_command_output_archive(receipt, ())

    with zipfile.ZipFile(io.BytesIO(result.payload)) as archive:
        assert archive.namelist() == ["MANIFEST.json", "RECEIPT.json", "stdout.bin"]
        assert archive.read("stdout.bin") == b"console result\n"
        manifest = _json_member(archive, "MANIFEST.json")
    assert manifest["output_count"] == 0
    assert manifest["evidence"] == [
        {
            "archive_mode": "0644",
            "archive_path": "stdout.bin",
            "sha256": hashlib.sha256(b"console result\n").hexdigest(),
            "size_bytes": 15,
            "truncated": False,
        }
    ]


def test_archive_refuses_evidence_bytes_that_do_not_match_receipt_hashes() -> None:
    receipt = _receipt((), stdout=b"trusted")
    changed = replace(receipt, stdout=b"altered")
    with pytest.raises(CommandOutputPublicationError, match="command_output_receipt_invalid"):
        build_command_output_archive(changed, ())


@pytest.mark.parametrize(
    "path",
    [
        "../escape.txt",
        "/absolute.txt",
        "dir/../escape.txt",
        "dir\\windows.txt",
        "./relative.txt",
        "dir//empty.txt",
        "CON.txt",
        "trailing. ",
        "line\nbreak.txt",
        "fullwidth-Ａ.txt",
        "report\u202etxt.exe",
        "zero\u200bwidth.txt",
    ],
)
def test_command_output_archive_rejects_unsafe_or_noncanonical_paths(path: str) -> None:
    payload = b"x"
    descriptor = _descriptor(path, payload)

    with pytest.raises(CommandOutputPublicationError, match="command_output_path_invalid"):
        build_command_output_archive(_receipt((descriptor,)), ((descriptor, payload),))


def test_command_output_archive_rejects_portable_path_collisions() -> None:
    upper_payload = b"upper"
    lower_payload = b"lower"
    upper = _descriptor("Report.txt", upper_payload)
    lower = _descriptor("report.txt", lower_payload)

    with pytest.raises(CommandOutputPublicationError, match="command_output_inventory_mismatch"):
        build_command_output_archive(
            _receipt((upper, lower)),
            ((upper, upper_payload), (lower, lower_payload)),
        )


def test_command_output_archive_rejects_changed_bytes_or_inventory() -> None:
    payload = b"trusted"
    descriptor = _descriptor("result.bin", payload)
    receipt = _receipt((descriptor,))

    with pytest.raises(CommandOutputPublicationError, match="command_output_digest_mismatch"):
        build_command_output_archive(receipt, ((descriptor, b"altered"),))

    replacement_payload = b"replacement"
    replacement = _descriptor("other.bin", replacement_payload)
    with pytest.raises(CommandOutputPublicationError, match="command_output_inventory_mismatch"):
        build_command_output_archive(receipt, ((replacement, replacement_payload),))


def test_command_output_archive_enforces_inventory_and_archive_caps() -> None:
    too_many = tuple(
        GeneratedFile(
            relative_path=f"item-{index:02d}.bin",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            mode=0o600,
        )
        for index in range(MAX_OUTPUT_FILES + 1)
    )
    with pytest.raises(CommandOutputPublicationError, match="command_output_count_invalid"):
        build_command_output_archive(_receipt(too_many), tuple((item, b"") for item in too_many))

    oversized_file = GeneratedFile(
        relative_path="oversized.bin",
        size_bytes=MAX_OUTPUT_FILE_BYTES + 1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        mode=0o600,
    )
    with pytest.raises(CommandOutputPublicationError, match="command_output_inventory_invalid"):
        build_command_output_archive(_receipt((oversized_file,)), ((oversized_file, b"x"),))

    tree_overflow = tuple(
        GeneratedFile(
            relative_path=f"large-{index}.bin",
            size_bytes=MAX_OUTPUT_TREE_BYTES // 3 + 1,
            sha256=hashlib.sha256(bytes([index])).hexdigest(),
            mode=0o600,
        )
        for index in range(3)
    )
    with pytest.raises(CommandOutputPublicationError, match="command_output_size_limit"):
        build_command_output_archive(
            _receipt(tree_overflow),
            tuple((item, bytes([index])) for index, item in enumerate(tree_overflow)),
        )

    payload = b"bounded"
    descriptor = _descriptor("bounded.bin", payload)
    with pytest.raises(CommandOutputPublicationError, match="command_output_archive_size_limit"):
        build_command_output_archive(
            _receipt((descriptor,)),
            ((descriptor, payload),),
            max_archive_bytes=64,
        )


@pytest.mark.parametrize(
    ("job_id", "status"),
    [
        ("not-a-job", CommandStatus.COMPLETED),
        ("1" * 32, CommandStatus.RUNNING),
        ("1" * 32, CommandStatus.UNKNOWN),
    ],
)
def test_command_output_archive_requires_an_exact_terminal_receipt(
    job_id: str,
    status: CommandStatus,
) -> None:
    payload = b"terminal"
    descriptor = _descriptor("terminal.txt", payload)

    with pytest.raises(CommandOutputPublicationError, match="command_output_receipt_invalid"):
        build_command_output_archive(
            _receipt((descriptor,), job_id=job_id, status=status),
            ((descriptor, payload),),
        )


def test_user_file_carrier_sends_basename_bytes_not_a_zip() -> None:
    payload = b"exact output bytes\n"
    descriptor = _descriptor("reports/result.txt", payload)
    receipt = _receipt((descriptor,))
    plan = select_engineer_result_carrier(["reports/result.txt"])

    carrier = build_user_result_carrier(receipt, ((descriptor, payload),), plan)

    assert carrier.kind == "file"
    assert carrier.filename == "result.txt"
    assert carrier.mime_type == USER_RESULT_FILE_MIME_TYPE
    assert carrier.payload == payload
    assert not carrier.payload.startswith(b"PK")
    assert base64.b64decode(carrier.attachment()["content_base64"], validate=True) == payload


def test_user_archive_carrier_is_user_paths_without_receipt() -> None:
    alpha = b"alpha\n"
    nested = b"nested payload\x00"
    alpha_descriptor = _descriptor("a.txt", alpha)
    nested_descriptor = _descriptor("reports/z.bin", nested)
    receipt = _receipt((nested_descriptor, alpha_descriptor))
    plan = select_engineer_result_carrier(["reports/z.bin", "a.txt"])

    carrier = build_user_result_carrier(
        receipt,
        ((nested_descriptor, nested), (alpha_descriptor, alpha)),
        plan,
    )

    assert carrier.kind == "archive"
    assert carrier.filename == f"engineer-command-{receipt.job_id}.zip"
    assert carrier.mime_type == COMMAND_OUTPUT_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(carrier.payload)) as archive:
        assert archive.namelist() == ["a.txt", "reports/z.bin"]
        assert archive.read("a.txt") == alpha
        assert archive.read("reports/z.bin") == nested
        assert "RECEIPT.json" not in archive.namelist()
        assert "MANIFEST.json" not in archive.namelist()
        assert "stdout.bin" not in archive.namelist()


def test_empty_user_file_is_refused() -> None:
    descriptor = GeneratedFile(
        relative_path="empty.bin",
        size_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        mode=0o600,
    )
    receipt = _receipt((descriptor,))
    plan = select_engineer_result_carrier(["empty.bin"])
    with pytest.raises(CommandOutputPublicationError, match="command_output_user_file_empty"):
        build_user_result_carrier(receipt, ((descriptor, b""),), plan)
