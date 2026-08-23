"""Synthetic coverage for Office family aliases and bounded legacy conversion."""

from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from friday.documents import DocumentExtractor
from friday.documents._office_convert import convert_legacy_office, libreoffice_available


@pytest.fixture(autouse=True)
def _clean_operator_office_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FRIDAY_LIBREOFFICE_PATH",
        "JERICHO_LIBREOFFICE_PATH",
        "FRIDAY_LIBREOFFICE_LIBRARY_PATH",
        "JERICHO_LIBREOFFICE_LIBRARY_PATH",
        "LD_LIBRARY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def _docx(marker: str = "WORD FAMILY MARKER") -> bytes:
    document = Document()
    document.add_paragraph(marker)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _xlsx(marker: str = "SHEET FAMILY MARKER") -> bytes:
    workbook = Workbook()
    workbook.active["A1"] = marker
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _pptx(marker: str = "SLIDE FAMILY MARKER") -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = marker
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def _odf(marker: str = "ODF FAMILY MARKER") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("content.xml", f"<office><text>{marker}</text></office>")
    return output.getvalue()


def _with_main_content_type(payload: bytes, canonical: str, replacement: str) -> bytes:
    """Turn a generated OOXML file into a real alias container."""

    source = io.BytesIO(payload)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(output, "w") as rewritten:
        for member in archive.infolist():
            content = archive.read(member)
            if member.filename == "[Content_Types].xml":
                old = canonical.encode("utf-8")
                new = replacement.encode("utf-8")
                assert content.count(old) == 1
                content = content.replace(old, new)
            rewritten.writestr(member, content)
    return output.getvalue()


_WORD_CANONICAL = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_WORD_TYPES = {
    "docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    "dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}
_SHEET_CANONICAL = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_SHEET_TYPES = {
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml",
}
_PRESENTATION_CANONICAL = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
_PRESENTATION_TYPES = {
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
    "potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
    "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
}


@pytest.mark.parametrize("extension", tuple(_WORD_TYPES))
def test_word_ooxml_family_aliases_use_the_docx_parser(extension: str) -> None:
    payload = _with_main_content_type(
        _docx(),
        _WORD_CANONICAL,
        _WORD_TYPES[extension],
    )
    result = DocumentExtractor(secret_values=()).extract(
        payload,
        f"synthetic.{extension}",
        "application/zip",
    )

    assert result.success is True, result.error
    assert "WORD FAMILY MARKER" in result.text
    assert result.metadata["format"] == "docx"
    assert result.metadata["main_content_type_normalized"] is True
    assert result.office_structure_index is not None


@pytest.mark.parametrize("extension", tuple(_SHEET_TYPES))
def test_excel_ooxml_family_aliases_use_the_xlsx_parser(extension: str) -> None:
    payload = _with_main_content_type(
        _xlsx(),
        _SHEET_CANONICAL,
        _SHEET_TYPES[extension],
    )
    result = DocumentExtractor(secret_values=()).extract(
        payload,
        f"synthetic.{extension}",
        "application/zip",
    )

    assert result.success is True, result.error
    assert "SHEET FAMILY MARKER" in result.text
    assert result.metadata["format"] == "xlsx"
    assert result.office_structure_index is not None


@pytest.mark.parametrize("extension", tuple(_PRESENTATION_TYPES))
def test_powerpoint_ooxml_family_aliases_use_the_pptx_parser(extension: str) -> None:
    payload = _with_main_content_type(
        _pptx(),
        _PRESENTATION_CANONICAL,
        _PRESENTATION_TYPES[extension],
    )
    result = DocumentExtractor(secret_values=()).extract(
        payload,
        f"synthetic.{extension}",
        "application/zip",
    )

    assert result.success is True, result.error
    assert "SLIDE FAMILY MARKER" in result.text
    assert result.metadata["format"] == "pptx"
    if extension != "pptm":
        assert result.metadata["main_content_type_normalized"] is True


@pytest.mark.parametrize("extension", ["sldx", "sldm"])
def test_standalone_slide_containers_are_honestly_unsupported(extension: str) -> None:
    result = DocumentExtractor(secret_values=()).extract(
        _pptx(),
        f"synthetic.{extension}",
        "application/zip",
    )

    assert result.success is False
    assert result.error == "unsupported_document_format"


def test_known_suffix_wins_over_an_adversarial_conflicting_mime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_libreoffice(tmp_path / "soffice")
    monkeypatch.setenv("FRIDAY_LIBREOFFICE_PATH", str(executable))
    extractor = DocumentExtractor(secret_values=())

    legacy_sheet = extractor.extract(
        _xlsx("LEGACY SHEET"),
        "conflict.xls",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    direct_sheet = extractor.extract(
        _xlsx("DIRECT SHEET"),
        "conflict.xlsx",
        "application/msword",
    )
    odf = extractor.extract(
        _odf("ODF PRECEDENCE"),
        "conflict.odt",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert legacy_sheet.success is True, legacy_sheet.error
    assert legacy_sheet.metadata["format"] == "xls"
    assert "LEGACY SHEET" in legacy_sheet.text
    assert direct_sheet.success is True, direct_sheet.error
    assert direct_sheet.metadata["format"] == "xlsx"
    assert "DIRECT SHEET" in direct_sheet.text
    assert odf.success is True, odf.error
    assert odf.metadata["format"] == "odt"
    assert "ODF PRECEDENCE" in odf.text


def test_ooxml_mime_is_used_only_for_an_unknown_suffix() -> None:
    result = DocumentExtractor(secret_values=()).extract(
        _docx("MIME FALLBACK"),
        "neutral.bin",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result.success is True, result.error
    assert result.metadata["format"] == "docx"
    assert "MIME FALLBACK" in result.text


@pytest.mark.parametrize(
    ("extension", "expected_format"),
    [
        ("ott", "odt"),
        ("odm", "odt"),
        ("oth", "odt"),
        ("ots", "ods"),
        ("otp", "odp"),
        ("odg", "odg"),
        ("otg", "odg"),
    ],
)
def test_opendocument_family_aliases_use_the_bounded_xml_parser(
    extension: str,
    expected_format: str,
) -> None:
    result = DocumentExtractor(secret_values=()).extract(
        _odf(),
        f"synthetic.{extension}",
        "application/zip",
    )

    assert result.success is True, result.error
    assert "ODF FAMILY MARKER" in result.text
    assert result.metadata["format"] == expected_format


def _fake_libreoffice(path: Path, *, expected_library_path: str | None = None) -> Path:
    path.write_text(
        f"""#!{sys.executable}
import os
import pathlib
import sys
arguments = sys.argv[1:]
required = ['--headless', '--nologo', '--nodefault', '--nolockcheck', '--norestore']
if any(item not in arguments for item in required):
    raise SystemExit(2)
if os.environ.get('LD_LIBRARY_PATH') != {expected_library_path!r}:
    raise SystemExit(3)
source = pathlib.Path(arguments[-1])
target = {{'doc': 'docx', 'xls': 'xlsx', 'xlsb': 'xlsx', 'ppt': 'pptx'}}[source.suffix[1:]]
output = pathlib.Path(arguments[arguments.index('--outdir') + 1]) / ('source.' + target)
output.write_bytes(source.read_bytes())
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_libreoffice_action(path: Path, action: str) -> Path:
    path.write_text(
        f"""#!{sys.executable}
import pathlib
import subprocess
import sys
import time
arguments = sys.argv[1:]
source = pathlib.Path(arguments[-1])
target = pathlib.Path(arguments[arguments.index('--outdir') + 1]) / 'source.xlsx'
{action}
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("legacy_extension", "marker", "target_format"),
    [
        ("doc", "WORD FAMILY MARKER", "docx"),
        ("xls", "SHEET FAMILY MARKER", "xlsx"),
        ("xlsb", "SHEET FAMILY MARKER", "xlsx"),
        ("ppt", "SLIDE FAMILY MARKER", "pptx"),
    ],
)
def test_legacy_office_families_use_one_closed_libreoffice_fallback(
    legacy_extension: str,
    marker: str,
    target_format: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _fake_libreoffice(tmp_path / "soffice")
    monkeypatch.setenv("FRIDAY_LIBREOFFICE_PATH", str(executable))
    payload = {
        "doc": _docx,
        "xls": _xlsx,
        "xlsb": _xlsx,
        "ppt": _pptx,
    }[legacy_extension]()

    result = DocumentExtractor(secret_values=()).extract(
        payload,
        f"legacy.{legacy_extension}",
        "application/octet-stream",
    )

    assert result.success is True, result.error
    assert marker in result.text
    assert result.metadata["format"] == legacy_extension
    assert result.metadata["converted_format"] == target_format
    assert result.metadata["parser"] == "libreoffice"


def test_libreoffice_rootless_loader_path_is_explicit_and_ambient_state_is_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_dir = tmp_path / "private-runtime" / "lib"
    library_dir.mkdir(parents=True)
    executable = _fake_libreoffice(
        tmp_path / "soffice",
        expected_library_path=str(library_dir.resolve()),
    )
    monkeypatch.setenv("FRIDAY_LIBREOFFICE_PATH", str(executable))
    monkeypatch.setenv("FRIDAY_LIBREOFFICE_LIBRARY_PATH", str(library_dir))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/ambient/value/must/not/leak")

    converted = convert_legacy_office(_xlsx(), "xls")

    assert libreoffice_available() is True
    assert converted.success is True
    assert converted.target_format == "xlsx"


def test_relative_libreoffice_executable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRIDAY_LIBREOFFICE_PATH", "relative/soffice")
    monkeypatch.setenv("PATH", "")

    converted = convert_legacy_office(b"synthetic", "xls")

    assert libreoffice_available() is False
    assert converted.success is False
    assert converted.error == "libreoffice_unavailable"


def test_libreoffice_converter_accepts_only_the_declared_legacy_family() -> None:
    converted = convert_legacy_office(b"synthetic", "pub")

    assert converted.success is False
    assert converted.error == "legacy_office_conversion_unsupported"


def test_libreoffice_timeout_kills_the_whole_converter_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "escaped-child"
    child = (
        "import pathlib,time;time.sleep(0.4);"
        f"pathlib.Path({str(marker)!r}).write_text('escaped')"
    )
    executable = _fake_libreoffice_action(
        tmp_path / "soffice",
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(5)",
    )

    converted = convert_legacy_office(
        b"synthetic",
        "xls",
        executable=str(executable),
        deadline=time.monotonic() + 0.1,
    )
    time.sleep(0.6)

    assert converted.success is False
    assert converted.error == "libreoffice_deadline_reached"
    assert marker.exists() is False


def test_libreoffice_nonzero_exit_is_not_treated_as_conversion(tmp_path: Path) -> None:
    executable = _fake_libreoffice_action(tmp_path / "soffice", "raise SystemExit(7)")

    converted = convert_legacy_office(b"synthetic", "xls", executable=str(executable))

    assert converted.success is False
    assert converted.error == "libreoffice_conversion_failed"


def test_libreoffice_missing_output_is_reported_exactly(tmp_path: Path) -> None:
    executable = _fake_libreoffice_action(tmp_path / "soffice", "raise SystemExit(0)")

    converted = convert_legacy_office(b"synthetic", "xls", executable=str(executable))

    assert converted.success is False
    assert converted.error == "libreoffice_output_missing"


def test_libreoffice_oversize_output_is_rejected_before_read(tmp_path: Path) -> None:
    executable = _fake_libreoffice_action(
        tmp_path / "soffice",
        "target.write_bytes(b'x' * 257)",
    )

    converted = convert_legacy_office(
        b"synthetic",
        "xls",
        executable=str(executable),
        max_output_bytes=256,
    )

    assert converted.success is False
    assert converted.error == "libreoffice_output_too_large"


def test_libreoffice_symlink_output_is_rejected_without_following_it(tmp_path: Path) -> None:
    executable = _fake_libreoffice_action(
        tmp_path / "soffice",
        "target.symlink_to(source)",
    )

    converted = convert_legacy_office(b"synthetic", "xls", executable=str(executable))

    assert converted.success is False
    assert converted.error == "libreoffice_output_invalid"


def test_libreoffice_multiply_linked_output_is_rejected(tmp_path: Path) -> None:
    executable = _fake_libreoffice_action(
        tmp_path / "soffice",
        "target.hardlink_to(source)",
    )

    converted = convert_legacy_office(b"synthetic", "xls", executable=str(executable))

    assert converted.success is False
    assert converted.error == "libreoffice_output_invalid"
