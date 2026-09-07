from __future__ import annotations

from friday.orchestration.mixed_file_archive_web_query import (
    extract_authorized_archive_filename,
    extract_mixed_file_archive_public_web_query,
    mixed_file_archive_web_cues_present,
    mixed_file_archive_web_turn_is_admitted,
)

_MIXED = "Сравни этот договор с архивом contract.txt и текущими публичными правилами в интернете."
_ATTACHMENTS = [{"filename": "current.txt", "mime_type": "text/plain"}]


def test_representative_utterance_admits_one_archive_filename_and_sealed_web_topic() -> None:
    assert mixed_file_archive_web_cues_present(_MIXED) is True
    assert extract_authorized_archive_filename(_MIXED) == "contract.txt"
    query = extract_mixed_file_archive_public_web_query(_MIXED)
    assert query
    folded = query.casefold()
    assert "contract.txt" not in folded
    assert "/etc/" not in folded
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=_ATTACHMENTS) is True


def test_private_archive_path_is_fail_closed() -> None:
    message = "Сравни этот договор с архивом /etc/passwd и текущими публичными правилами в интернете."
    assert mixed_file_archive_web_cues_present(message) is False
    assert extract_authorized_archive_filename(message) == ""
    assert mixed_file_archive_web_turn_is_admitted(message, attachments=_ATTACHMENTS) is False


def test_file_and_web_without_archive_is_not_mixed() -> None:
    message = "Сравни этот договор с текущими публичными правилами в интернете."
    assert mixed_file_archive_web_turn_is_admitted(message, attachments=_ATTACHMENTS) is False


def test_archive_and_web_without_attachment_is_not_mixed() -> None:
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=None) is False
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=[]) is False
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=["not-a-dict"]) is False
