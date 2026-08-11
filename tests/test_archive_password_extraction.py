"""Passwords unlock archives without becoming document data or process argv."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import py7zr
import pytest
import pyzipper
from docx import Document as WordDocument

from friday.documents import ArchiveLimitError, DocumentExtractor

_ZIP_PASSWORD = "zip-test-password"
_RAR_PASSWORD = "rar-test-password"
_ZIPCRYPTO = base64.b64decode(
    "UEsDBAoACQAAAOmMC12hSvXIMAAAACQAAAAMABwAemlwLW5vdGUudHh0VVQJAAMVNHtqFTR7anV4"
    "CwABBOgDAAAE6AMAAN3rlBatrT5P8hlcTnWX9C2CidSgGC/rk1Y9WgDinoGTI2q+JA/+4YF7yS6q"
    "gF6+mlBLBwihSvXIMAAAACQAAABQSwECHgMKAAkAAADpjAtdoUr1yDAAAAAkAAAADAAYAAAAAAABAA"
    "AAtIEAAAAAemlwLW5vdGUudHh0VVQFAAMVNHtqdXgLAAEE6AMAAAToAwAAUEsFBgAAAAABAAEAUgAA"
    "AIYAAAAAAA=="
)
_HEADER_ENCRYPTED_RAR = base64.b64decode(
    "UmFyIRoHAQBLJx5nIQQAAAEPwqbQ9jglpV/lD0bFoGnNBC45iy2mzQPz7kfymxxFGKi/Vax1rT8b"
    "ekMztd4/MFaC5U5dluBIYcuJG7G0k1a1uh3UjSGfLfC3NTIlRhgAlBCFHQj/DDB0ulxSCjyQ6AP0"
    "TxmiEtvKwmZZYh+BLBOyJXxyAD5KsiK9u1UfAteLnFQqXXcN4LZFc6WQol+jyAOBqiW+K/mWjOBK"
    "QRUwuQL6Me0rc9vB51s1dOvLo9001aQ7UGVbwViFnPiVPUJWhEdSwW5cQINMuuzn3BI1cVl5bk30"
    "/reAG3CjXDTPe8GjEEJIU72DvPWxzCS4tpjIqvefMu7Uu363bDiHC0IC"
)
# Created with Debian's 7-Zip 26.00 by first adding 24 plain files and then
# updating the same archive with an encrypted ``secret.txt`` folder.  This is
# intentionally a mixed-stream 7z: archive-level ``needs_password()`` is true,
# but the first 24 members do not exercise the password at all.
_MIXED_7Z_AFTER_PREVIEW_CAP = base64.b64decode(
    "N3q8ryccAAR3p6YjlwEAAAAAAAAiAAAAAAAAACIjb37gANcAOV0AOBsIR4KzA4kgtgXlntioublg"
    "PReyck3PY+ai6YSIGSWU57brIVOpSMSipV7NBDxH1AU/AxKjgmYAAOOrEbfAxrk8/tS8Z4WLi/0A"
    "B+f/trzwXBF0oy/w06PeAACBMweuMZwKNkwYi0byhH7QMzCYTHForD8mfqODDk184PUNTmjRSbRW"
    "l2NAPaECjbbimsI2ll2lkkoOpJ7iq4RAmFA74ZfLERSN/9hady1QvGWZSMmBUdtumnfWKXLzUKvi"
    "ztbMjayq7I+KV90JRCYeq/k626tkifdd24N22sgjIdGAJfOVfBW02jpiDy5nQDbwPq1YhZrVk/H8"
    "GMyw2dMxcB0m/yDjR/1hx7ixAR1ChdxOJ65Mr2x3r/RitTxUiBCT+4O/YtE3oY+2YhKeeXvNYFQQ"
    "n913grPQoujShx2ITOQ34u4vRTFPhzvS+DXd3iepo/Jn6Vo1G7gjlQf0n4fDjvZJsYyDVw9s0+Ee"
    "8lqw3WIPie9p9lwsyc7Wh19w8w1VukFrTx86VvUFzn0fOeHycAAAABcGYQEJgTYABwsBAAEjAwEB"
    "BV0AEAAADISSCgHBH+WtAAA="
)


def _extractor() -> DocumentExtractor:
    return DocumentExtractor(secret_values=(), parse_budget_sec=5)


def _assert_password_contract(payload: bytes, filename: str, password: str, marker: str) -> None:
    missing = _extractor().extract(payload, filename)
    assert missing.success is False
    assert missing.error == "archive_password_required"

    wrong = _extractor().extract(payload, filename, archive_password="definitely-wrong")
    assert wrong.success is False
    assert wrong.error == "archive_password_invalid"

    unlocked = _extractor().extract(payload, filename, archive_password=password)
    assert unlocked.success is True
    assert unlocked.error == ""
    assert marker in unlocked.text
    assert unlocked.metadata["encrypted"] is True
    assert unlocked.metadata["previewed_files"] == 1
    assert password not in json.dumps(unlocked.to_dict(), ensure_ascii=False, sort_keys=True)


def test_winzip_aes_and_legacy_zipcrypto_are_read_in_memory() -> None:
    aes = io.BytesIO()
    with pyzipper.AESZipFile(
        aes,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(_ZIP_PASSWORD.encode("utf-8"))
        archive.writestr("nested/aes-note.txt", "AES-ZIP-MARKER кириллица")

    _assert_password_contract(aes.getvalue(), "current.zip", _ZIP_PASSWORD, "AES-ZIP-MARKER")
    _assert_password_contract(_ZIPCRYPTO, "legacy.zip", _ZIP_PASSWORD, "ZIPCRYPTO-MARKER")


@pytest.mark.parametrize("header_encryption", [False, True])
def test_7z_password_and_member_contents_are_supported(header_encryption: bool) -> None:
    payload = io.BytesIO()
    with py7zr.SevenZipFile(
        payload,
        mode="w",
        password="seven-test-password",
        header_encryption=header_encryption,
    ) as archive:
        archive.writestr("SEVEN-ZIP-MARKER кириллица".encode(), "nested/note.txt")

    _assert_password_contract(
        payload.getvalue(),
        "current.7z",
        "seven-test-password",
        "SEVEN-ZIP-MARKER",
    )


def test_plain_7z_is_content_not_just_a_filename_listing() -> None:
    payload = io.BytesIO()
    with py7zr.SevenZipFile(payload, mode="w") as archive:
        archive.writestr(b"PLAIN-SEVEN-CONTENT", "note.txt")

    result = _extractor().extract(payload.getvalue(), "plain.7z")

    assert result.success is True
    assert "PLAIN-SEVEN-CONTENT" in result.text
    assert result.metadata["previewed_files"] == 1
    assert result.metadata.get("archive_budget_exhausted") is not True


@pytest.mark.parametrize("archive_format", ["zip", "7z"])
def test_oversized_encrypted_member_still_validates_password(archive_format: str) -> None:
    """The former 128 KiB skip must never turn a wrong password into success."""

    password = "oversized-test-password"
    member = hashlib.shake_256(b"friday-oversized-password-regression").digest(192 * 1024)
    assert len(member) > 128 * 1024
    payload = io.BytesIO()
    if archive_format == "zip":
        with pyzipper.AESZipFile(
            payload,
            mode="w",
            compression=pyzipper.ZIP_DEFLATED,
            encryption=pyzipper.WZ_AES,
        ) as archive:
            archive.setpassword(password.encode())
            archive.writestr("oversized.bin", member)
    else:
        with py7zr.SevenZipFile(payload, mode="w", password=password) as archive:
            archive.writestr(member, "oversized.bin")

    wrong = _extractor().extract(
        payload.getvalue(),
        f"oversized.{archive_format}",
        archive_password="wrong-password",
    )
    assert wrong.success is False
    assert wrong.error == "archive_password_invalid"

    unlocked = _extractor().extract(
        payload.getvalue(),
        f"oversized.{archive_format}",
        archive_password=password,
    )
    assert unlocked.success is True
    assert unlocked.metadata["previewed_files"] == 1


def test_mixed_7z_encrypted_member_after_preview_cap_is_mandatory() -> None:
    wrong = _extractor().extract(
        _MIXED_7Z_AFTER_PREVIEW_CAP,
        "mixed.7z",
        archive_password="wrong-password",
    )
    assert wrong.success is False
    assert wrong.error == "archive_password_invalid"

    unlocked = _extractor().extract(
        _MIXED_7Z_AFTER_PREVIEW_CAP,
        "mixed.7z",
        archive_password="seven-mixed-password",
    )
    assert unlocked.success is True
    assert unlocked.metadata["files"] == 25
    assert unlocked.metadata["previewed_files"] == 24
    assert unlocked.metadata["archive_budget_exhausted"] is True
    assert "SECRET-AFTER-CAP" in unlocked.text


def test_nested_docx_larger_than_legacy_preview_cap_is_read() -> None:
    document = WordDocument()
    body = base64.b64encode(hashlib.shake_256(b"friday-large-nested-docx").digest(180 * 1024)).decode("ascii")
    document.add_paragraph(f"NESTED-DOCX-MARKER {body}")
    nested = io.BytesIO()
    document.save(nested)
    nested_bytes = nested.getvalue()
    assert len(nested_bytes) > 128 * 1024

    outer = io.BytesIO()
    with zipfile.ZipFile(outer, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("nested/report.docx", nested_bytes)

    result = _extractor().extract(outer.getvalue(), "documents.zip")

    assert result.success is True
    assert result.metadata["previewed_files"] == 1
    assert "NESTED-DOCX-MARKER" in result.text
    assert result.metadata.get("archive_budget_exhausted") is not True


@pytest.mark.parametrize("filename", ["broken.zip", "broken.7z"])
def test_corrupt_zip_and_7z_fail_with_closed_error(filename: str) -> None:
    result = _extractor().extract(b"attacker-controlled parser diagnostic", filename)

    assert result.success is False
    assert result.error == "archive_extract_failed"


def test_7z_lzma_dictionary_is_rejected_before_decoder_allocation() -> None:
    oversized_lzma2 = SimpleNamespace(
        coders=[{"method": b"\x21", "properties": b"\x20"}],
    )

    with pytest.raises(ArchiveLimitError, match="dictionary"):
        _extractor()._validate_7z_coder_folders([oversized_lzma2])


def test_archive_member_paths_never_escape_even_for_memory_extraction() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, mode="w") as archive:
        archive.writestr("../escape.txt", b"must stay inert")

    result = _extractor().extract(payload.getvalue(), "unsafe.zip")

    assert result.success is False
    assert result.error == "archive_limit_exceeded"


def test_rar_password_is_stdin_only_and_members_never_touch_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "argv.json"
    fake = tmp_path / "unrar"
    fake.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

password = sys.stdin.buffer.readline().rstrip(b"\\n").decode("utf-8")
with open({str(trace)!r}, "w", encoding="utf-8") as handle:
    json.dump({{"argv": sys.argv[1:], "environment": sorted(os.environ)}}, handle)
if any(password and password in argument for argument in sys.argv[1:]):
    raise SystemExit(97)
if any(password and password in value for value in os.environ.values()):
    raise SystemExit(98)
if password != "rar-test-password":
    raise SystemExit(11)
sys.stdout.buffer.write("RAR-MARKER кириллица\\n".encode("utf-8"))
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    _assert_password_contract(
        _HEADER_ENCRYPTED_RAR,
        "protected.rar",
        _RAR_PASSWORD,
        "RAR-MARKER",
    )

    invocation = json.loads(trace.read_text(encoding="utf-8"))
    argv = invocation["argv"]
    assert "-p" in argv
    assert "-cfg-" in argv
    assert "-mdx128m" in argv
    assert all(_RAR_PASSWORD not in argument for argument in argv)
    assert set(invocation["environment"]) <= {"LANG", "LC_ALL"}
    assert not (tmp_path / "rar-note.txt").exists()


def test_rar_tool_stdin_preserves_whitespace_and_unicode(
    tmp_path: Path,
) -> None:
    password = "  Ra\u0301r-🔐  "
    trace = tmp_path / "stdin-ok.json"
    fake = tmp_path / "unrar"
    fake.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

password = sys.stdin.buffer.readline().rstrip(b"\\n").decode("utf-8")
with open({str(trace)!r}, "w", encoding="utf-8") as handle:
    json.dump({{"matched": password == {password!r}, "argv": sys.argv[1:]}}, handle)
if password != {password!r}:
    raise SystemExit(11)
sys.stdout.buffer.write(b"RAR-EXACT-STDIN-MARKER")
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    source = tmp_path / "opaque-encrypted.rar"
    source.write_bytes(b"encrypted-rar-bytes")

    extracted = _extractor()._read_rar_member_with_tool(
        str(source),
        "note.txt",
        tool=str(fake),
        password=password,
        limit=1024,
        deadline=time.monotonic() + 5,
    )

    invocation = json.loads(trace.read_text(encoding="utf-8"))
    assert invocation["matched"] is True
    assert extracted == b"RAR-EXACT-STDIN-MARKER"
    assert all(password not in argument for argument in invocation["argv"])


@pytest.mark.skipif(shutil.which("unrar") is None, reason="real UnRAR is not installed")
def test_real_unrar_opens_known_header_encrypted_fixture() -> None:
    _assert_password_contract(
        _HEADER_ENCRYPTED_RAR,
        "protected.rar",
        _RAR_PASSWORD,
        "RAR-MARKER",
    )
