from __future__ import annotations

import pytest

from friday.orchestration.mixed_file_archive_web_query import (
    extract_authorized_archive_filename,
    extract_authorized_archive_raw_id,
    extract_mixed_file_archive_public_web_query,
    mixed_file_archive_web_cues_present,
    mixed_file_archive_web_turn_is_admitted,
)

_MIXED = "Сравни этот договор с архивом contract.txt и текущими публичными правилами в интернете."
_ATTACHMENTS = [{"filename": "current.txt", "mime_type": "text/plain"}]


@pytest.mark.parametrize(
    ("reference", "filename", "raw_id", "topic"),
    [
        ("@contract.txt и", "contract.txt", "", "текущими публичными правилами"),
        ("contract.txt!", "contract.txt", "", "Текущие публичные правила"),
        ("raw_0123456789abcdef.", "", "raw_0123456789abcdef", "Текущие публичные правила"),
    ],
)
def test_exact_archive_marker_and_sentence_punctuation_remain_supported(
    reference: str,
    filename: str,
    raw_id: str,
    topic: str,
) -> None:
    message = f"Сравни этот договор с архивом {reference} {topic} в интернете."
    assert extract_authorized_archive_filename(message) == filename
    assert extract_authorized_archive_raw_id(message) == raw_id
    assert extract_mixed_file_archive_public_web_query(message) == topic
    assert mixed_file_archive_web_turn_is_admitted(message, attachments=_ATTACHMENTS)


def test_representative_utterance_admits_one_archive_filename_and_sealed_web_topic() -> None:
    assert mixed_file_archive_web_cues_present(_MIXED) is True
    assert extract_authorized_archive_filename(_MIXED) == "contract.txt"
    query = extract_mixed_file_archive_public_web_query(_MIXED)
    assert query == "текущими публичными правилами"
    folded = query.casefold()
    assert "contract.txt" not in folded
    assert "/etc/" not in folded
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=_ATTACHMENTS) is True


def test_private_archive_path_is_fail_closed() -> None:
    message = "Сравни этот договор с архивом /etc/passwd и текущими публичными правилами в интернете."
    # The intent stays visible to the sealer so generic extraction cannot run.
    assert mixed_file_archive_web_cues_present(message) is True
    assert extract_authorized_archive_filename(message) == ""
    assert mixed_file_archive_web_turn_is_admitted(message, attachments=_ATTACHMENTS) is False


def test_file_and_web_without_archive_is_not_mixed() -> None:
    message = "Сравни этот договор с текущими публичными правилами в интернете."
    assert mixed_file_archive_web_turn_is_admitted(message, attachments=_ATTACHMENTS) is False


def test_archive_and_web_without_attachment_is_not_mixed() -> None:
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=None) is False
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=[]) is False
    assert mixed_file_archive_web_turn_is_admitted(_MIXED, attachments=["not-a-dict"]) is False


def test_archive_prefix_does_not_consume_the_basename_of_an_english_file_selector() -> None:
    message = "Compare this file with archive file.txt and current public fire safety rules on the web."
    assert extract_authorized_archive_filename(message) == "file.txt"
    query = extract_mixed_file_archive_public_web_query(message)
    assert query
    assert "file.txt" not in query and "archive" not in query
