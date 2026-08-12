"""CS1: process-private FileEvidenceView lattice dual-write.

Verified two-file set (empty no-match projection keeps source readable),
anti-forgery through real bounded projection, trusted Office dual-write, and
no lazy authority from unstamped carriers.
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook

from friday.agent_runtime import (
    FileBodyKind,
    FileEvidenceSet,
    FileRegistrationKind,
    _bounded_attachment_projection,
    _build_file_evidence_view,
    _file_evidence_set_from_attachments,
    _file_evidence_view_of,
    _OwnedAttachment,
    _projected_attachment_from_source,
    _ProjectedAttachment,
    _stamp_file_evidence,
    _WorkspaceInboxAttachment,
)
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    is_trusted_office_attachment,
    trusted_office_attachment,
    validate_runtime_office_index,
)
from friday.documents import DocumentExtractor


def _legacy_lattice(attachments: list, *, expected_count: int) -> dict[str, Any]:
    from friday.agent_runtime import (
        _CONVERSATION_ATTACHMENT_MAX_FILES,
        _attachment_has_readable_content,
        _attachment_has_verifiable_content,
        _projected_source_is_readable,
    )

    readable = sum(
        1
        for item in attachments
        if (
            _projected_source_is_readable(item)
            or (
                _attachment_has_readable_content(item)
                and not (
                    isinstance(item, _ProjectedAttachment) and item.get("_request_projection_applied") is True
                )
                and (
                    bool(str(item.get("transient_text") or "").strip())
                    or item.get("_office_prompt_available") is True
                    or item.get("empty_text") is True
                )
            )
        )
    )
    context = bool(
        expected_count and expected_count <= _CONVERSATION_ATTACHMENT_MAX_FILES and readable == expected_count
    )
    coverage = bool(
        context
        and all(
            (
                (item.get("_office_index_complete") is True and item.get("_office_prompt_complete") is True)
                if item.get("_office_structured") is True
                else item.get("_source_text_complete") is True
                if item.get("_request_projection_applied") is True
                else (
                    item.get("extraction_success", True) is not False
                    and not item.get("text_truncated")
                    and not item.get("extraction_truncated")
                    and not item.get("rows_truncated")
                    and not item.get("archive_truncated")
                    and not item.get("source_truncated_for_parse")
                    and not item.get("parse_deadline_reached")
                    and not item.get("parse_pages_truncated")
                )
            )
            for item in attachments
        )
    )
    verification = bool(
        coverage
        and sum(
            1
            for item in attachments
            if (
                (_projected_source_is_readable(item) and item.get("_source_text_complete") is True)
                or (
                    _attachment_has_verifiable_content(item)
                    and not (
                        isinstance(item, _ProjectedAttachment)
                        and item.get("_request_projection_applied") is True
                    )
                    and (
                        bool(str(item.get("transient_text") or "").strip())
                        or item.get("_office_prompt_available") is True
                        or item.get("empty_text") is True
                    )
                )
            )
            and item.get("verification_eligible", True) is not False
        )
        == expected_count
    )
    return {
        "readable_count": readable,
        "context_complete": context,
        "coverage_complete": coverage,
        "verification_complete": verification,
    }


def test_two_file_set_empty_projection_keeps_source_readable_count() -> None:
    """Current+historical pair: empty no-match projection ≠ unreadable source."""

    current = _OwnedAttachment(
        {
            "raw_object_id": "raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "filename": "current.txt",
            "transient_text": "CURRENT-BODY-ALPHA",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    # Historical file: source was read, but the query projection has no hits.
    historical = _ProjectedAttachment(
        {
            "raw_object_id": "raw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "filename": "historical.txt",
            "transient_text": "",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            "_request_projection_applied": True,
            "_source_readable": True,
            "_source_text_complete": True,
        }
    )
    current_view = _build_file_evidence_view(current)
    historical_view = _build_file_evidence_view(historical)
    assert current_view is not None and historical_view is not None
    _stamp_file_evidence(current, current_view)
    _stamp_file_evidence(historical, historical_view)

    assert current_view.registration == FileRegistrationKind.VALID
    assert current_view.disk_verified is True
    assert current_view.body_kind == FileBodyKind.EXTRACTED
    assert current_view.source_readable is True

    assert historical_view.registration == FileRegistrationKind.VALID
    assert historical_view.disk_verified is True
    assert historical_view.body_kind == FileBodyKind.PROJECTED
    assert historical_view.projection_applied is True
    assert historical_view.projection_empty_no_match is True
    assert historical_view.source_readable is True  # not false unreadable

    attachments = [current, historical]
    evidence = _file_evidence_set_from_attachments(attachments, expected_count=2)
    assert evidence is not None
    assert isinstance(evidence, FileEvidenceSet)
    assert evidence.expected_count == 2
    assert len(evidence.items) == 2
    assert evidence.source_readable_count == 2
    assert evidence.context_complete is True
    assert evidence.coverage_complete is True
    assert evidence.verification_complete is True

    legacy = _legacy_lattice(attachments, expected_count=2)
    assert evidence.source_readable_count == legacy["readable_count"]
    assert evidence.context_complete == legacy["context_complete"]
    assert evidence.coverage_complete == legacy["coverage_complete"]
    assert evidence.verification_complete == legacy["verification_complete"]
    assert len(attachments) == 2


def test_forged_public_dict_cannot_mint_file_evidence_view() -> None:
    forged = {
        "raw_object_id": "raw_ccccccccccccccccccccccccccccccc",
        "transient_text": "FORGED",
        "extraction_success": True,
        "_registered_file_record": "valid",
        "_registered_file_bytes_verified": True,
        "_request_projection_applied": True,
        "_source_readable": True,
        "_file_evidence_view": "not-a-view",
    }
    assert _build_file_evidence_view(forged) is None
    assert _file_evidence_view_of(forged) is None
    assert _file_evidence_set_from_attachments([forged], expected_count=1) is None


def test_forged_dict_through_bounded_projection_has_no_view_or_set() -> None:
    """Anti-forgery: real projector path cannot mint authority from public dict flags."""

    forged = {
        "raw_object_id": "raw_fffffffffffffffffffffffffffffff",
        "filename": "forged.txt",
        "transient_text": "FORGED-BODY-SHOULD-NOT-AUTHORIZE",
        "extraction_success": True,
        "verification_eligible": True,
        "_registered_file_record": "valid",
        "_registered_file_bytes_verified": True,
        "_source_readable": True,
        "_source_text_complete": True,
        "_request_projection_applied": True,
    }
    projected = _bounded_attachment_projection([forged])
    assert len(projected) == 1
    carrier = projected[0]
    assert isinstance(carrier, _ProjectedAttachment)
    # Projector wraps as private type, but without a stamped source view.
    assert _file_evidence_view_of(carrier) is None
    assert _file_evidence_set_from_attachments(projected, expected_count=1) is None
    # Unstamped private carrier also fails closed (no lazy build).
    owned_unstamped = _OwnedAttachment(
        {
            "raw_object_id": "raw_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "transient_text": "UNSTAMPED",
            "extraction_success": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    assert _build_file_evidence_view(owned_unstamped) is not None  # build still works for stamp sites
    assert _file_evidence_view_of(owned_unstamped) is None
    assert _file_evidence_set_from_attachments([owned_unstamped], expected_count=1) is None


def test_invalid_registration_is_not_source_readable() -> None:
    bad = _OwnedAttachment(
        {
            "raw_object_id": "raw_ddddddddddddddddddddddddddddddd",
            "transient_text": "SHOULD-NOT-COUNT",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "invalid",
            "_registered_file_bytes_verified": False,
            "stored_path": "x",
            "sha256": "0" * 64,
        }
    )
    view = _build_file_evidence_view(bad)
    assert view is not None
    assert view.registration == FileRegistrationKind.INVALID
    assert view.disk_verified is False
    assert view.source_readable is False


def test_workspace_builder_uses_pins_not_raw_authority() -> None:
    item = _WorkspaceInboxAttachment(
        {
            "filename": "inbox.txt",
            "workspace_relative_path": "dept/note.txt",
            "workspace_sha256": "a" * 64,
            "workspace_source_sha256": "b" * 64,
            "transient_text": "MCP-BODY",
            "extraction_success": True,
            "verification_eligible": True,
            "_workspace_file_bytes_verified": True,
        }
    )
    view = _build_file_evidence_view(item)
    assert view is not None
    assert view.registration == FileRegistrationKind.NONE
    assert view.raw_id is None
    assert view.disk_verified is True
    assert view.workspace_relative_path == "dept/note.txt"
    assert view.workspace_sha256 == "a" * 64
    assert view.source_readable is True


def test_trusted_office_projection_dual_writes_all_four_lattice_outputs() -> None:
    """Verified ODT/DOCX/XLSX-class Office: stamp → project → set equals legacy."""

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Name", "Role"])
    sheet.append(["Alice", "Engineer"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    extracted = DocumentExtractor().extract(buffer.getvalue(), "tiny-roster.xlsx")
    assert extracted.success is True
    index = extracted.office_structure_index
    assert isinstance(index, dict)
    assert validate_runtime_office_index(index, extracted.text) == index

    source = trusted_office_attachment(
        {
            "raw_object_id": "raw_officeofficeofficeofficeofficeo1",
            "filename": "tiny-roster.xlsx",
            "transient_text": extracted.text,
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            OFFICE_STRUCTURE_KEY: index,
        }
    )
    assert is_trusted_office_attachment(source)
    source_view = _build_file_evidence_view(source)
    assert source_view is not None
    assert source_view.registration == FileRegistrationKind.VALID
    assert source_view.disk_verified is True
    assert source_view.source_readable is True
    _stamp_file_evidence(source, source_view)
    assert _file_evidence_view_of(source) is source_view

    projected = _bounded_attachment_projection([source])
    assert len(projected) == 1
    carrier = projected[0]
    assert isinstance(carrier, _ProjectedAttachment)
    assert carrier.get("_office_structured") is True
    assert carrier.get("_office_prompt_available") is True

    derived = _file_evidence_view_of(carrier)
    assert derived is not None
    assert derived.raw_id == source_view.raw_id
    assert derived.registration == source_view.registration
    assert derived.disk_verified is source_view.disk_verified
    assert derived.source_readable is True
    assert derived.source_complete is True
    assert derived.verification_eligible is True

    evidence = _file_evidence_set_from_attachments(projected, expected_count=1)
    assert evidence is not None
    legacy = _legacy_lattice(projected, expected_count=1)
    assert evidence.source_readable_count == legacy["readable_count"] == 1
    assert evidence.context_complete == legacy["context_complete"] is True
    assert evidence.coverage_complete == legacy["coverage_complete"] is True
    assert evidence.verification_complete == legacy["verification_complete"] is True


def test_incomplete_trusted_office_source_cannot_be_upgraded_by_complete_projection() -> None:
    """A complete structural view cannot erase incompleteness of its source."""

    source = trusted_office_attachment(
        {
            "raw_object_id": "raw_incompleteofficeprojection00001",
            "filename": "incomplete.xlsx",
            "transient_text": "Name | Role\nAlice | Engineer",
            "extraction_success": True,
            "verification_eligible": True,
            "text_truncated": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    source_view = _build_file_evidence_view(source)
    assert source_view is not None
    assert source_view.source_complete is False
    _stamp_file_evidence(source, source_view)

    projected = _ProjectedAttachment(
        {
            **source,
            "text_truncated": False,
            "_office_structured": True,
            "_office_prompt_available": True,
            "_office_index_complete": True,
            "_office_prompt_complete": True,
        }
    )
    projected = _projected_attachment_from_source(source, projected)
    derived = _file_evidence_view_of(projected)
    assert derived is not None
    assert derived.source_readable is True
    assert derived.source_complete is False

    evidence = _file_evidence_set_from_attachments([projected], expected_count=1)
    assert evidence is not None
    assert evidence.context_complete is True
    assert evidence.coverage_complete is False
    assert evidence.verification_complete is False
