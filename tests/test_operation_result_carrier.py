from __future__ import annotations

import base64
import zipfile
from io import BytesIO

import pytest

from friday.orchestration.operation_result_carrier import (
    OPERATION_RESULT_ARCHIVE_FILENAME,
    OperationResultCarrierError,
    OperationResultCarrierKind,
    pack_operation_result_archive,
    plan_generated_file_documents,
    select_operation_result_carrier,
)


def test_no_files_selects_text_carrier() -> None:
    plan = select_operation_result_carrier(())
    assert plan.carrier is OperationResultCarrierKind.TEXT
    assert plan.files == ()


def test_one_ordinary_file_selects_file_not_archive() -> None:
    plan = select_operation_result_carrier(["report.txt"])
    assert plan.carrier is OperationResultCarrierKind.FILE
    assert [item.relative_path for item in plan.files] == ["report.txt"]


def test_two_files_select_one_archive() -> None:
    plan = select_operation_result_carrier(["a.txt", "b.txt"])
    assert plan.carrier is OperationResultCarrierKind.ARCHIVE
    assert [item.relative_path for item in plan.files] == ["a.txt", "b.txt"]


def test_single_file_cannot_be_packed_as_archive() -> None:
    with pytest.raises(OperationResultCarrierError):
        pack_operation_result_archive([("only.txt", b"hello")])


def test_pack_archive_is_zip_stored_and_contains_both_members() -> None:
    payload = pack_operation_result_archive([("b.txt", b"bee"), ("a.txt", b"aye")])
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert archive.namelist() == ["a.txt", "b.txt"]
        assert archive.read("a.txt") == b"aye"
        assert archive.read("b.txt") == b"bee"
        assert archive.comment == b""
        assert archive.getinfo("a.txt").compress_type == zipfile.ZIP_STORED


def test_generated_files_none_or_empty_are_text() -> None:
    plan, documents = plan_generated_file_documents(None)
    assert plan.carrier is OperationResultCarrierKind.TEXT
    assert documents == ()
    plan, documents = plan_generated_file_documents([])
    assert plan.carrier is OperationResultCarrierKind.TEXT
    assert documents == ()


def test_one_generated_file_keeps_original_bytes() -> None:
    raw = b"PK\x03\x04xlsx"
    plan, documents = plan_generated_file_documents(
        [
            {
                "id": "raw_1",
                "filename": "People.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "content_base64": base64.b64encode(raw).decode("ascii"),
            }
        ]
    )
    assert plan.carrier is OperationResultCarrierKind.FILE
    assert len(documents) == 1
    assert documents[0].filename == "People.xlsx"
    assert documents[0].payload == raw
    assert documents[0].artifact_id == "raw_1"


def test_two_generated_files_become_one_zip_document() -> None:
    plan, documents = plan_generated_file_documents(
        [
            {
                "id": "one",
                "filename": "a.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"aaa").decode("ascii"),
            },
            {
                "id": "two",
                "filename": "b.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"bbb").decode("ascii"),
            },
        ]
    )
    assert plan.carrier is OperationResultCarrierKind.ARCHIVE
    assert len(documents) == 1
    assert documents[0].filename == OPERATION_RESULT_ARCHIVE_FILENAME
    assert documents[0].mime_type == "application/zip"
    with zipfile.ZipFile(BytesIO(documents[0].payload)) as archive:
        assert set(archive.namelist()) == {"a.txt", "b.txt"}
        assert archive.read("a.txt") == b"aaa"


def test_internal_receipt_is_not_a_user_file_carrier() -> None:
    plan = select_operation_result_carrier(["receipts/ok.json", "report.txt"])
    assert plan.carrier is OperationResultCarrierKind.FILE
    assert [item.relative_path for item in plan.files] == ["report.txt"]
