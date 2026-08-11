"""Unpacking limits belong to the UPLOAD, not to each archive inside it.

The ceilings used to be per level: a nested member started again with a full
allowance of 24 previews and `max_archive_uncompressed_bytes`, so they
multiplied instead of dividing. Measured on the shipped defaults: a 3.0 MB zip
whose 24 members are each a `.tar.gz` of 440 x 250 KiB expanded to ~2.7 GB and
held the event loop for 107 seconds — returning `success=True`, raising nothing,
and reporting `previewed_files: 24` as though everything were normal. The
operator's "250 MB per upload" was in fact 250 MB per nesting level.

The outer members are STORED, so the ZIP compression-ratio guard sees a ratio of
1 and passes them: the expansion happens one level down, inside a `.tar.gz` that
has no ratio guard at all. That asymmetry is what made the nesting profitable.
"""

from __future__ import annotations

import io
import tarfile
import time
import zipfile

import pytest

from friday.documents import DocumentExtractor

_MEMBER_BYTES = 250 * 1024
_MEMBERS_PER_TAR = 200
_OUTER_MEMBERS = 24


def _inner_tar_gz() -> bytes:
    """One small `.tar.gz` that unpacks to ~50 MB of zero-filled members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for index in range(_MEMBERS_PER_TAR):
            info = tarfile.TarInfo(f"member-{index}.bin")
            info.size = _MEMBER_BYTES
            archive.addfile(info, io.BytesIO(bytes(_MEMBER_BYTES)))
    return buffer.getvalue()


@pytest.fixture(scope="module")
def nested_bomb() -> bytes:
    inner = _inner_tar_gz()
    assert len(inner) < 512 * 1024, f"inner archive grew to {len(inner)} bytes"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(_OUTER_MEMBERS):
            archive.writestr(f"payload-{index}.tar.gz", inner)
    return buffer.getvalue()


def test_nested_archives_share_one_expansion_budget(nested_bomb):
    """Total expansion stays inside the configured ceiling, not 24x it."""
    ceiling = 8 * 1024 * 1024
    extractor = DocumentExtractor(max_archive_uncompressed_bytes=ceiling)

    started = time.monotonic()
    result = extractor.extract(nested_bomb, "bomb.zip")
    elapsed = time.monotonic() - started

    # Would-be expansion if every level got its own allowance:
    unbounded = _OUTER_MEMBERS * _MEMBERS_PER_TAR * _MEMBER_BYTES
    assert unbounded > ceiling * 20  # the input really is a multiplying archive

    assert result.success  # a partial listing, not an error — the upload is legal
    # Bounded work is the contract; the wall-clock assertion is deliberately loose
    # (CI machines vary), it only has to fail the 100-second version.
    assert elapsed < 20.0, f"unpacking took {elapsed:.1f}s"


def test_preview_allowance_is_not_renewed_per_level(nested_bomb):
    """24 previews per upload, not 24 per archive at every depth."""
    extractor = DocumentExtractor(max_archive_uncompressed_bytes=64 * 1024 * 1024)
    result = extractor.extract(nested_bomb, "bomb.zip")

    previews = result.text.count("\n--- ")
    assert previews <= 24, f"{previews} member previews across the upload"


def test_a_plain_archive_still_previews_its_members(tmp_path):
    """The budget must not cost an ordinary upload anything."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(5):
            archive.writestr(f"note-{index}.txt", f"Заметка номер {index} про дежурства.")
    result = DocumentExtractor().extract(buffer.getvalue(), "notes.zip")

    assert result.success
    assert result.metadata["previewed_files"] == 5
    assert result.text.count("\n--- ") == 5
    assert "Заметка номер 4" in result.text


def test_one_readable_zip_member_is_not_silently_cut_at_20k() -> None:
    tail = "ZIP-MEMBER-TAIL-SENTINEL"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("only.txt", "ZIP-HEAD\n" + "x" * 25_000 + "\n" + tail)

    result = DocumentExtractor().extract(buffer.getvalue(), "one-long-note.zip")

    assert result.success is True
    assert tail in result.text
    assert result.metadata.get("archive_budget_exhausted") is not True
    assert result.metadata.get("text_truncated") is not True


def test_an_oversized_tar_member_is_reported_as_unread() -> None:
    buffer = io.BytesIO()
    payload = b"x" * (2 * 1024 * 1024)
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("oversized.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    result = DocumentExtractor(max_input_bytes=512 * 1024).extract(
        buffer.getvalue(),
        "one-oversized-member.tar.gz",
    )

    assert result.success is True
    assert result.metadata["archive_budget_exhausted"] is True
    assert result.metadata["previewed_files"] == 0
