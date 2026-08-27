"""Bounded, content-free structure for native DOCX/XLSX extraction.

The ordinary extracted text is the corpus contract.  This module deliberately
does not improve, reorder, or annotate that text.  It reproduces the legacy
rendering while recording half-open character spans into the exact result.
Persisted structure contains only offsets, ordinals, opaque deterministic IDs,
closed enums, counts, and the exact UTF-8 digest; literal document values are
always recovered from the bound text after validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import unicodedata
import zipfile
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

OFFICE_STRUCTURE_SCHEMA_VERSION = 1
OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES = 48 * 1024

_MAX_BLOCKS = 256
_MAX_ROWS = 768
_MAX_CELLS = 4_096
_MAX_RUNS = 4_096
_MAX_CANDIDATES = 1_024
_MAX_MERGE_RANGES = 2_048
_MAX_XLSX_VISITED_CELLS = 1_000_000
_MAX_VALID_COUNT = 2_147_483_647

_COVERAGE_REASON_ORDER = (
    "text_budget",
    "row_budget",
    "index_budget",
    "unsupported_body_content",
    "nested_table",
    "header_footer",
    "text_box",
    "hidden_layout",
    "run_alignment",
    "formula_without_cached_value",
    "formula_scan_unavailable",
    "formula_alignment",
    "merge_scan_unavailable",
    "merge_scan_budget",
    "merge_topology",
)
_COVERAGE_REASONS = frozenset(_COVERAGE_REASON_ORDER)
_STYLE_ROLES = frozenset({"body", "heading", "title", "list", "other"})
_ROW_ROLES = frozenset({"header", "record", "empty", "footer", "unknown"})
_VISIBILITIES = frozenset({"visible", "hidden", "very_hidden"})


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "format",
        "text_sha256",
        "complete",
        "coverage",
        "blocks",
        "record_sets",
        "candidate_refs",
    }
)
_COVERAGE_KEYS = frozenset(
    {
        "reasons",
        "blocks_seen",
        "blocks_indexed",
        "rows_seen",
        "rows_indexed",
        "cells_seen",
        "cells_indexed",
    }
)
_PARAGRAPH_KEYS = frozenset({"id", "kind", "source_order", "text_span", "style_role", "runs"})
_RUN_KEYS = frozenset({"id", "text_span", "bold", "italic", "underline"})
_TABLE_KEYS = frozenset({"id", "kind", "source_order", "text_span", "rows"})
_SHEET_KEYS = frozenset({"id", "kind", "source_order", "text_span", "title_span", "visibility", "rows"})
_ROW_KEYS = frozenset({"id", "source_row", "role", "text_span", "cells"})
_CELL_KEYS = frozenset({"id", "column", "coordinate", "text_span", "merge_anchor"})
_RECORD_SET_KEYS = frozenset(
    {
        "id",
        "block_id",
        "kind",
        "authoritative",
        "header_row_id",
        "record_ids",
        "records_total",
        "person_column",
    }
)
_CANDIDATE_KEYS = frozenset({"id", "type", "record_id", "cell_id", "text_span", "basis"})

_BLOCK_ID_RE = re.compile(r"(?:p|t|s)[0-9]{6}")
_PARAGRAPH_ID_RE = re.compile(r"p[0-9]{6}")
_TABLE_ID_RE = re.compile(r"t[0-9]{6}")
_SHEET_ID_RE = re.compile(r"s[0-9]{6}")
_RUN_ID_RE = re.compile(r"p[0-9]{6}:u[0-9]{6}")
_ROW_ID_RE = re.compile(r"(?:t|s)[0-9]{6}:r[0-9]{6}")
_CELL_ID_RE = re.compile(r"(?:t|s)[0-9]{6}:r[0-9]{6}:c[0-9]{6}")
_RECORD_SET_ID_RE = re.compile(r"rs[0-9]{6}")
_CANDIDATE_ID_RE = re.compile(r"cand[0-9]{6}")
_DOCX_COORDINATE_RE = re.compile(r"R[1-9][0-9]{0,6}C[1-9][0-9]{0,5}")
_XLSX_COORDINATE_RE = re.compile(r"[A-Z]{1,3}[1-9][0-9]{0,6}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WORDPROCESSINGML_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFFICE_RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)

_STRONG_PERSON_HEADER_VALUES = frozenset(
    {
        "фио",
        "ф и о",
        "фамилия имя отчество",
        "фамилия и о",
        "фамилия имя",
        "имя",
        "name",
        "full name",
        "employee name",
    }
)
_GENERIC_PERSON_HEADER_VALUES = frozenset(
    {
        "сотрудник",
        "работник",
        "персона",
        "человек",
        "person",
        "employee",
    }
)
_PERSON_HEADER_VALUES = _STRONG_PERSON_HEADER_VALUES | _GENERIC_PERSON_HEADER_VALUES
_SCHEMA_HEADER_VALUES = _PERSON_HEADER_VALUES | frozenset(
    {
        "номер",
        "no",
        "id",
        "item",
        "items",
        "роль",
        "role",
        "должность",
        "position",
        "job title",
        "title",
        "описание",
        "description",
        "подразделение",
        "department",
        "отдел",
        "unit",
        "команда",
        "team",
        "дата",
        "date",
        "статус",
        "status",
        "state",
        "телефон",
        "phone",
        "почта",
        "email",
    }
)
_FOOTER_PREFIXES = (
    "итого",
    "итог",
    "общий итог",
    "всего",
    "subtotal",
    "total",
    "totals",
    "grand total",
    "average",
    "среднее",
    "сумма",
    "общая сумма",
    "sum",
    "результат",
    "примечание",
    "примечания",
    "комментарий",
    "комментарии",
    "справочно",
    "note",
    "notes",
    "comment",
    "comments",
    "remark",
    "remarks",
    "источник",
    "источники",
    "source",
    "sources",
    "данные актуальны",
    "data as of",
    "обновлено",
    "updated",
    "last updated",
    "подготовил",
    "подготовила",
    "подготовлено",
    "prepared by",
    "дата формирования",
    "дата выгрузки",
    "сформировано",
    "сформирован",
    "проверено",
    "утверждено",
    "версия отчёта",
    "generated",
    "generated at",
    "approved",
    "approved by",
    "report version",
    "составил",
    "выгружено",
    "экспортировано",
    "created by",
    "prepared",
    "отчет сформирован",
    "отчёт сформирован",
    "дата отчета",
    "дата отчёта",
    "период отчета",
    "период отчёта",
    "страница",
)
_DOCX_FIELD_TAGS = frozenset({"fldSimple", "fldChar", "instrText", "delInstrText"})
_DOCX_VISIBLE_AUXILIARY_TAGS = frozenset(
    {
        "drawing",
        "pict",
        "object",
        "oleObject",
        "sym",
        "ptab",
        "footnoteReference",
        "endnoteReference",
        "commentReference",
    }
)
_DOCX_UNSUPPORTED_CONTAINERS = frozenset(
    {
        "sdt",
        "customXml",
        "smartTag",
        "altChunk",
        "ins",
        "del",
        "moveFrom",
        "moveTo",
        "oMath",
        "oMathPara",
        "dir",
        "bdo",
        "ruby",
        "AlternateContent",
        "Choice",
        "Fallback",
    }
)
_DOCX_EMPTY_PARAGRAPH_DIRECT_CHILDREN = frozenset(
    {
        "pPr",
        # Proofing and permission range markers do not render content by
        # themselves.  They are neutral only in a paragraph with no run or
        # relationship-bearing descendant; see
        # ``_docx_paragraph_is_structurally_empty``.
        "proofErr",
        "permStart",
        "permEnd",
    }
)
_DOCX_EMPTY_PARAGRAPH_ALLOWED_TAGS = _DOCX_EMPTY_PARAGRAPH_DIRECT_CHILDREN | frozenset(
    {
        "p",
        # CT_PPr paragraph properties.
        "pStyle",
        "keepNext",
        "keepLines",
        "pageBreakBefore",
        "framePr",
        "widowControl",
        "numPr",
        "suppressLineNumbers",
        "pBdr",
        "shd",
        "tabs",
        "suppressAutoHyphens",
        "kinsoku",
        "wordWrap",
        "overflowPunct",
        "topLinePunct",
        "autoSpaceDE",
        "autoSpaceDN",
        "bidi",
        "adjustRightInd",
        "snapToGrid",
        "spacing",
        "ind",
        "contextualSpacing",
        "mirrorIndents",
        "suppressOverlap",
        "jc",
        "textDirection",
        "textAlignment",
        "textboxTightWrap",
        "outlineLvl",
        "divId",
        "cnfStyle",
        "rPr",
        "sectPr",
        "pPrChange",
        # Nested numbering, border, tab, and section properties.
        "ilvl",
        "numId",
        "numberingChange",
        "top",
        "left",
        "bottom",
        "right",
        "between",
        "bar",
        "tab",
        "headerReference",
        "footerReference",
        "footnotePr",
        "endnotePr",
        "type",
        "pgSz",
        "pgMar",
        "paperSrc",
        "pgBorders",
        "lnNumType",
        "pgNumType",
        "cols",
        "formProt",
        "vAlign",
        "noEndnote",
        "titlePg",
        "rtlGutter",
        "docGrid",
        "printerSettings",
        "sectPrChange",
        # CT_RPr formatting for the paragraph mark.  These are properties,
        # never a content-bearing ``w:r``.
        "rStyle",
        "rFonts",
        "b",
        "bCs",
        "i",
        "iCs",
        "caps",
        "smallCaps",
        "strike",
        "dstrike",
        "outline",
        "shadow",
        "emboss",
        "imprint",
        "noProof",
        "vanish",
        "webHidden",
        "color",
        "w",
        "kern",
        "position",
        "sz",
        "szCs",
        "highlight",
        "u",
        "effect",
        "bdr",
        "fitText",
        "vertAlign",
        "rtl",
        "cs",
        "em",
        "lang",
        "eastAsianLayout",
        "specVanish",
        "rPrChange",
        # Property-only revision markers.  Their parent is checked below.
        "ins",
        "del",
        "moveFrom",
        "moveTo",
    }
)
_DOCX_EMPTY_PARAGRAPH_REVISION_MARKERS = frozenset({"ins", "del", "moveFrom", "moveTo"})
_DOCX_EMPTY_PARAGRAPH_REVISION_PARENTS = frozenset({"rPr", "numPr"})
_XLSX_HEADER_FOOTER_TAGS = frozenset(
    {
        "oddHeader",
        "oddFooter",
        "evenHeader",
        "evenFooter",
        "firstHeader",
        "firstFooter",
    }
)
_XLSX_VISIBLE_AUXILIARY_TAGS = frozenset(
    {
        "drawing",
        "legacyDrawing",
        "legacyDrawingHF",
        "picture",
        "oleObjects",
        "controls",
        "dataValidations",
        "hyperlinks",
        "rPh",
        "phoneticPr",
    }
)
_XLSX_ACTIVE_FILTER_TAGS = frozenset(
    {
        "filterColumn",
        "sortState",
        "customFilters",
        "filters",
        "top10",
        "dynamicFilter",
        "colorFilter",
        "iconFilter",
    }
)
_XLSX_AUXILIARY_PART_PREFIXES = (
    "xl/comments",
    "xl/threadedcomments/",
    "xl/drawings/",
    "xl/charts/",
    "xl/chartsheets/",
    "xl/dialogsheets/",
    "xl/macrosheets/",
    "xl/embeddings/",
    "xl/activex/",
    "xl/ctrlprops/",
    "xl/pivottables/",
    "xl/slicers/",
    "xl/timelines/",
    "xl/media/",
)


def _exact_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _local_name(tag: Any) -> str:
    value = str(tag or "")
    return value.rsplit("}", 1)[-1] if "}" in value else value.rsplit(":", 1)[-1]


def _xml_namespace(tag: Any) -> str:
    value = str(tag or "")
    return value[1:].split("}", 1)[0] if value.startswith("{") and "}" in value else ""


def _has_office_relationship_attribute(element: Any) -> bool:
    return any(
        _xml_namespace(name) in _OFFICE_RELATIONSHIP_NAMESPACES for name in getattr(element, "attrib", {})
    )


def _docx_paragraph_is_structurally_empty(element: Any) -> bool:
    """Prove that a body paragraph carries no content absent from ``.text``.

    A blank ``Paragraph.text`` is not enough: an empty-looking paragraph can
    still carry a drawing, a field, an embedded object, a break, a bookmark, or
    a relationship-backed hyperlink.  Only paragraph properties and inert
    proofing/permission markers are neutral.  Property descendants are allowed
    because Word stores revision metadata there, but the same payload and
    relationship checks apply recursively so malformed OOXML cannot hide an
    object inside ``pPr``.
    """

    if _local_name(getattr(element, "tag", "")) != "p":
        return False
    try:
        children = list(element.iterchildren())
        descendants = list(element.iter())
    except (AttributeError, TypeError):
        return False
    if any(
        _local_name(getattr(child, "tag", "")) not in _DOCX_EMPTY_PARAGRAPH_DIRECT_CHILDREN
        for child in children
    ):
        return False
    for node in descendants:
        local_name = _local_name(getattr(node, "tag", ""))
        parent = getattr(node, "getparent", lambda: None)()
        if (
            _xml_namespace(getattr(node, "tag", "")) != _WORDPROCESSINGML_NAMESPACE
            or local_name not in _DOCX_EMPTY_PARAGRAPH_ALLOWED_TAGS
            or (
                local_name in _DOCX_EMPTY_PARAGRAPH_REVISION_MARKERS
                and _local_name(getattr(parent, "tag", "")) not in _DOCX_EMPTY_PARAGRAPH_REVISION_PARENTS
            )
            or _has_office_relationship_attribute(node)
            or str(getattr(node, "text", "") or "").strip()
            or str(getattr(node, "tail", "") or "").strip()
        ):
            return False
    return True


def _docx_structurally_empty_paragraphs(body: Any) -> set[Any]:
    try:
        return {
            node
            for node in body.iter()
            if _local_name(getattr(node, "tag", "")) == "p" and _docx_paragraph_is_structurally_empty(node)
        }
    except (AttributeError, TypeError):
        return set()


def _closed_text(value: Any) -> str:
    return " ".join(
        re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).split()
    )


def _is_person_header(value: str) -> bool:
    return _closed_text(value) in _PERSON_HEADER_VALUES


def _is_strong_person_header(value: str) -> bool:
    return _closed_text(value) in _STRONG_PERSON_HEADER_VALUES


def _is_schema_header(value: str) -> bool:
    return _closed_text(value) in _SCHEMA_HEADER_VALUES


def _is_schema_header_like(value: str) -> bool:
    """Recognise a known header with a continuation/pagination qualifier."""

    normalized = _closed_text(value)
    return any(
        normalized == header or normalized.startswith(f"{header} ") for header in _SCHEMA_HEADER_VALUES
    )


def _is_footer_value(value: str) -> bool:
    normalized = _closed_text(value)
    return any(normalized == prefix or normalized.startswith(f"{prefix} ") for prefix in _FOOTER_PREFIXES)


def _is_numeric_aggregate(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().replace("\u00a0", " ")
    return bool(re.fullmatch(r"[+-]?(?:\d+(?:[ .,]\d{3})*|\d+)(?:[.,]\d+)?%?", normalized))


def _style_role(paragraph: Any) -> str:
    """Collapse potentially private custom style names to a closed role enum."""

    style = getattr(paragraph, "style", None)
    style_id = str(getattr(style, "style_id", "") or "").casefold()
    style_name = str(getattr(style, "name", "") or "").casefold()
    probe = f"{style_id} {style_name}"
    if "heading" in probe or "заголов" in probe:
        return "heading"
    if any(token in probe for token in ("title", "subtitle", "название")):
        return "title"
    if any(token in probe for token in ("list", "спис")):
        return "list"
    if not probe.strip() or any(token in probe for token in ("normal", "body", "обыч")):
        return "body"
    return "other"


@dataclass
class _LegacyTextBuilder:
    max_chars: int

    def __post_init__(self) -> None:
        self.parts: list[str] = []
        self.used = 0

    def append(self, value: str, *, separator: str = "\n") -> tuple[list[int] | None, bool]:
        """Exactly mirror ``DocumentExtractor._append_bounded`` and expose its span."""

        if not value:
            return None, False
        separator_size = len(separator) if self.parts else 0
        available = self.max_chars - self.used - separator_size
        if available <= 0:
            return None, True
        clipped = value[:available]
        start = self.used + separator_size
        self.parts.append(clipped)
        self.used = start + len(clipped)
        return [start, self.used], len(clipped) != len(value)

    def text(self, *, separator: str = "\n") -> str:
        return separator.join(self.parts)


def _ordered_reasons(reasons: set[str]) -> list[str]:
    return [reason for reason in _COVERAGE_REASON_ORDER if reason in reasons]


def _new_coverage() -> dict[str, Any]:
    return {
        "reasons": [],
        "blocks_seen": 0,
        "blocks_indexed": 0,
        "rows_seen": 0,
        "rows_indexed": 0,
        "cells_seen": 0,
        "cells_indexed": 0,
    }


def _visible_xml_text(element: Any) -> bool:
    try:
        for node in element.iter():
            if _local_name(getattr(node, "tag", "")) == "t" and str(getattr(node, "text", "") or "").strip():
                return True
    except (AttributeError, TypeError):
        return False
    return False


def _xml_attribute(element: Any, name: str) -> str:
    for key, value in getattr(element, "attrib", {}).items():
        if _local_name(key) == name:
            return str(value or "")
    return ""


def _ooxml_truthy(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "on", "yes"}


def _positive_ooxml_int(value: Any) -> bool:
    try:
        return int(str(value or "0")) > 0
    except (TypeError, ValueError, OverflowError):
        return bool(str(value or "").strip())


def _docx_initial_reasons(
    document: Any,
    structurally_empty_paragraphs: set[Any] | None = None,
    *,
    deadline: float | None = None,
) -> set[str]:
    reasons: set[str] = set()
    body = document.element.body
    neutral_paragraphs = _docx_structurally_empty_paragraphs(body)
    neutral_paragraphs.update(structurally_empty_paragraphs or set())
    neutral_nodes = {node for paragraph in neutral_paragraphs for node in paragraph.iter()}
    for child in body.iterchildren():
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            return reasons
        if child in neutral_paragraphs:
            continue
        if _local_name(child.tag) not in {"p", "tbl", "sectPr"}:
            reasons.add("unsupported_body_content")
    for node in body.iter():
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            return reasons
        if node in neutral_nodes:
            continue
        local_name = _local_name(getattr(node, "tag", ""))
        if (
            local_name in _DOCX_UNSUPPORTED_CONTAINERS
            or local_name in _DOCX_FIELD_TAGS
            or local_name in _DOCX_VISIBLE_AUXILIARY_TAGS
        ):
            # python-docx does not promise these containers in Paragraph.text /
            # Document.paragraphs. Their mere presence is enough to make native
            # coverage unknown; do not inspect or persist their literal value.
            reasons.add("unsupported_body_content")
        if local_name == "t":
            # Only a Word text node in a direct paragraph run (or a direct
            # hyperlink run) is guaranteed to participate in python-docx's
            # Paragraph.text/cell.text projection.  Math, ruby, bidi wrappers,
            # compatibility choices and DrawingML may all contain a visually
            # rendered `*:t` that the legacy reader silently omits.
            parent = getattr(node, "getparent", lambda: None)()
            container = getattr(parent, "getparent", lambda: None)()
            if _local_name(getattr(container, "tag", "")) == "hyperlink":
                container = getattr(container, "getparent", lambda: None)()
            if (
                _xml_namespace(getattr(node, "tag", "")) != _WORDPROCESSINGML_NAMESPACE
                or _local_name(getattr(parent, "tag", "")) != "r"
                or _local_name(getattr(container, "tag", "")) != "p"
            ):
                reasons.add("unsupported_body_content")
        if local_name == "txbxContent" and _visible_xml_text(node):
            reasons.add("text_box")

    seen_parts: set[Any] = set()
    for section in document.sections:
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            return reasons
        for name in (
            "header",
            "first_page_header",
            "even_page_header",
            "footer",
            "first_page_footer",
            "even_page_footer",
        ):
            container = getattr(section, name)
            element = getattr(container, "_element", None)
            if element is None or element in seen_parts:
                continue
            seen_parts.add(element)
            if _visible_xml_text(element):
                reasons.add("header_footer")
    return reasons


#: Сколько знаков разрешено забрать из служебных частей документа.
#: Колонтитул, сноска и примечание — это подписи и пояснения, а не второй том;
#: потолок бережёт и запрос к модели, и бюджет текста самого документа.
_DOCX_AUXILIARY_CHAR_BUDGET = 20_000
#: Метки, под которыми служебный текст становится видимым человеку и модели.
#: Без метки «Согласовано: Петров» из колонтитула читалось бы как фраза из текста.
_DOCX_AUXILIARY_LABELS = {
    "header_footer": "Колонтитул",
    "note": "Сноска",
    "comment": "Примечание",
    "textbox": "Надпись",
}


def _docx_part_visible_text(stream: Any, *, limit: int) -> str:
    """Видимый текст части OOXML — только настоящие `w:t`, без служебных полей."""
    pieces: list[str] = []
    used = 0
    for _, element in ElementTree.iterparse(stream, events=("end",)):
        if _local_name(element.tag) in {"t", "delText"}:
            value = str(element.text or "").strip()
            if value:
                pieces.append(value)
                used += len(value) + 1
                if used >= limit:
                    break
        element.clear()
    return " ".join(pieces)[:limit]


def _docx_textbox_text(stream: Any, *, limit: int) -> str:
    """Текст надписей: он лежит в `w:txbxContent` внутри тела документа.

    `python-docx` его не отдаёт: `Paragraph.text` собирает только прямые прогоны
    абзаца, а надпись — отдельный контейнер внутри рисунка. На бланках и схемах
    именно там стоят подписи, номера и фамилии.
    """
    pieces: list[str] = []
    used = 0
    inside = 0
    for event, element in ElementTree.iterparse(stream, events=("start", "end")):
        name = _local_name(element.tag)
        if name == "txbxContent":
            inside += 1 if event == "start" else -1
            if event == "end":
                element.clear()
            continue
        if event != "end":
            continue
        if inside > 0 and name in {"t", "delText"}:
            value = str(element.text or "").strip()
            if value:
                pieces.append(value)
                used += len(value) + 1
                if used >= limit:
                    break
    return " ".join(pieces)[:limit]


def _docx_auxiliary_parts(
    content: bytes,
    *,
    deadline: float | None = None,
) -> tuple[list[tuple[str, str]], set[str]]:
    """Служебный текст документа и то, что по-прежнему остаётся непрочитанным.

    Колонтитулы, сноски, примечания и надписи ПОМЕЧАЛИСЬ как утраченные и не
    извлекались: их наличие делало документ «неполным», а человек не получал ни
    строчки из них. На бланках там стоят «Согласовано», «Исполнитель», номер и
    дата — то есть ровно то, что ищут.

    Возвращает пары «метка — текст» в устойчивом порядке и множество причин,
    которые остаются в силе: диаграммы, встроенные объекты и картинки читать
    по-прежнему нечем, и молчать об этом нельзя.
    """
    chunks: list[tuple[str, str]] = []
    remaining: set[str] = set()
    budget = _DOCX_AUXILIARY_CHAR_BUDGET
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = sorted(archive.namelist())
            for name in names:
                if _deadline_expired(deadline):
                    remaining.add("text_budget")
                    break
                normalized = name.lstrip("/").casefold()
                if normalized.startswith(
                    (
                        "word/charts/",
                        "word/drawings/",
                        "word/diagrams/",
                        "word/embeddings/",
                        "word/activex/",
                        "word/media/",
                    )
                ):
                    remaining.add("unsupported_body_content")
                    continue
                if budget <= 0:
                    continue
                if re.fullmatch(r"word/(?:header|footer)[0-9]+\.xml", normalized):
                    kind = "header_footer"
                elif normalized in {"word/footnotes.xml", "word/endnotes.xml"}:
                    kind = "note"
                elif normalized.startswith("word/comments"):
                    kind = "comment"
                elif normalized == "word/document.xml":
                    with archive.open(name) as stream:
                        value = _docx_textbox_text(stream, limit=budget)
                    if value:
                        chunks.append((_DOCX_AUXILIARY_LABELS["textbox"], value))
                        budget -= len(value)
                    continue
                else:
                    continue
                with archive.open(name) as stream:
                    value = _docx_part_visible_text(stream, limit=budget)
                if value:
                    chunks.append((_DOCX_AUXILIARY_LABELS[kind], value))
                    budget -= len(value)
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        # Внешняя проверка ZIP владеет успехом разбора; здесь отказ закрытый:
        # не прочитали — значит документ неполон, и это сказано.
        return [], {"unsupported_body_content"}
    if budget <= 0:
        remaining.add("unsupported_body_content")
    return chunks, remaining


def _xlsx_package_reasons(
    content: bytes,
    sheet_paths: Sequence[str],
    *,
    deadline: float | None = None,
) -> tuple[set[str], dict[str, tuple[int, int]]]:
    """Fail closed on visible XLSX material absent from the cell-value text."""

    reasons: set[str] = set()
    actual_extents: dict[str, tuple[int, int]] = {}
    normalized_paths = [str(path).lstrip("/") for path in sheet_paths]
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for name in archive.namelist():
                if _deadline_expired(deadline):
                    reasons.add("text_budget")
                    return reasons, actual_extents
                normalized = name.lstrip("/").casefold()
                if normalized.startswith(_XLSX_AUXILIARY_PART_PREFIXES):
                    reasons.add("unsupported_body_content")
                    break
                if normalized == "xl/sharedstrings.xml":
                    with archive.open(name) as stream:
                        for event_index, (_, element) in enumerate(
                            ElementTree.iterparse(stream, events=("end",)),
                            start=1,
                        ):
                            if event_index % 256 == 0 and _deadline_expired(deadline):
                                reasons.add("text_budget")
                                return reasons, actual_extents
                            if _local_name(element.tag) in {"rPh", "phoneticPr"}:
                                reasons.add("unsupported_body_content")
                                break
                            element.clear()

            for path in normalized_paths:
                if _deadline_expired(deadline):
                    reasons.add("text_budget")
                    return reasons, actual_extents
                dimension_bounds: tuple[int, int, int, int] | None = None
                actual_bounds: list[int] | None = None
                active_row: int | None = None
                last_row = 0
                last_cell = (0, 0)
                with archive.open(path) as stream:
                    for event_index, (event, element) in enumerate(
                        ElementTree.iterparse(stream, events=("start", "end")),
                        start=1,
                    ):
                        if event_index % 256 == 0 and _deadline_expired(deadline):
                            reasons.add("text_budget")
                            return reasons, actual_extents
                        local_name = _local_name(element.tag)
                        if event == "start":
                            if local_name == "row":
                                row_value = _xml_attribute(element, "r")
                                try:
                                    parsed_row = int(row_value)
                                except (TypeError, ValueError, OverflowError):
                                    parsed_row = 0
                                if parsed_row <= last_row or parsed_row > 1_048_576:
                                    reasons.add("formula_alignment")
                                active_row = parsed_row
                                last_row = max(last_row, parsed_row)
                            elif local_name == "c":
                                coordinate = _parse_coordinate(_xml_attribute(element, "r"))
                                if (
                                    coordinate is None
                                    or coordinate[0] > 1_048_576
                                    or coordinate[1] > 16_384
                                    or active_row is None
                                    or coordinate[0] != active_row
                                    or coordinate <= last_cell
                                ):
                                    reasons.add("formula_alignment")
                                else:
                                    row_number, column_number = coordinate
                                    if actual_bounds is None:
                                        actual_bounds = [
                                            row_number,
                                            column_number,
                                            row_number,
                                            column_number,
                                        ]
                                    else:
                                        actual_bounds[0] = min(actual_bounds[0], row_number)
                                        actual_bounds[1] = min(actual_bounds[1], column_number)
                                        actual_bounds[2] = max(actual_bounds[2], row_number)
                                        actual_bounds[3] = max(actual_bounds[3], column_number)
                                    last_cell = coordinate
                            continue
                        if local_name == "dimension":
                            dimension_bounds = _parse_dimension_bounds(_xml_attribute(element, "ref"))
                            if dimension_bounds is None:
                                reasons.add("formula_alignment")
                        if local_name in _XLSX_HEADER_FOOTER_TAGS and "".join(element.itertext()).strip():
                            reasons.add("header_footer")
                        if local_name in _XLSX_VISIBLE_AUXILIARY_TAGS:
                            reasons.add("unsupported_body_content")
                        if local_name in _XLSX_ACTIVE_FILTER_TAGS:
                            reasons.add("hidden_layout")
                        if local_name in {"row", "col"} and (
                            _ooxml_truthy(_xml_attribute(element, "hidden"))
                            or _ooxml_truthy(_xml_attribute(element, "collapsed"))
                            or _positive_ooxml_int(_xml_attribute(element, "outlineLevel"))
                        ):
                            reasons.add("hidden_layout")
                        if local_name == "sheetFormatPr" and _ooxml_truthy(
                            _xml_attribute(element, "zeroHeight")
                        ):
                            reasons.add("hidden_layout")
                        if local_name == "sheetPr" and _ooxml_truthy(_xml_attribute(element, "filterMode")):
                            reasons.add("hidden_layout")
                        if local_name == "row":
                            active_row = None
                        element.clear()
                if actual_bounds is not None and (
                    dimension_bounds is not None
                    and (
                        actual_bounds[0] < dimension_bounds[0]
                        or actual_bounds[1] < dimension_bounds[1]
                        or actual_bounds[2] > dimension_bounds[2]
                        or actual_bounds[3] > dimension_bounds[3]
                    )
                ):
                    # openpyxl's read-only iterator trusts the dimension and can
                    # silently omit real cells outside it, including formulas.
                    # The package preflight sees the XML itself and revokes
                    # completeness before such a projection can claim totals.
                    reasons.add("formula_alignment")
                actual_extents[path] = (
                    max(last_row, actual_bounds[2] if actual_bounds is not None else 0),
                    actual_bounds[3] if actual_bounds is not None else 0,
                )
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        reasons.add("unsupported_body_content")
    return reasons, actual_extents


def _body_source_orders(document: Any) -> dict[Any, int]:
    # Keep the lxml element objects themselves as keys.  ``iterchildren()`` may
    # manufacture short-lived Python proxies; storing only ``id(child)`` lets a
    # later proxy reuse that address and gives two different blocks one order.
    result: dict[Any, int] = {}
    source_order = 0
    for child in document.element.body.iterchildren():
        if _local_name(child.tag) == "sectPr":
            continue
        result[child] = source_order
        source_order += 1
    return result


def _paragraph_runs(
    paragraph: Any, block_id: str, text_span: list[int], reasons: set[str]
) -> list[dict[str, Any]]:
    raw_text = str(paragraph.text or "")
    trimmed = raw_text.strip()
    if not trimmed:
        return []
    left = len(raw_text) - len(raw_text.lstrip())
    right = len(raw_text.rstrip())

    run_objects: list[Any] = []
    try:
        for item in paragraph.iter_inner_content():
            nested_runs = getattr(item, "runs", None)
            if nested_runs is not None:
                run_objects.extend(list(nested_runs))
            else:
                run_objects.append(item)
    except (AttributeError, TypeError):
        run_objects = list(getattr(paragraph, "runs", []))

    if "".join(str(getattr(run, "text", "") or "") for run in run_objects) != raw_text:
        reasons.add("run_alignment")
        return []

    result: list[dict[str, Any]] = []
    cursor = 0
    for ordinal, run in enumerate(run_objects, start=1):
        run_text = str(getattr(run, "text", "") or "")
        raw_start = cursor
        raw_end = cursor + len(run_text)
        cursor = raw_end
        clipped_start = max(raw_start, left)
        clipped_end = min(raw_end, right)
        if clipped_start >= clipped_end:
            continue
        if len(result) >= _MAX_RUNS:
            reasons.add("index_budget")
            break
        start = text_span[0] + clipped_start - left
        end = text_span[0] + clipped_end - left
        result.append(
            {
                "id": f"{block_id}:u{ordinal:06d}",
                "text_span": [start, end],
                "bold": getattr(run, "bold", None) is True,
                "italic": getattr(run, "italic", None) is True,
                "underline": getattr(run, "underline", None) is True,
            }
        )
    return result


def _span_text(text: str, span: Any) -> str:
    if not isinstance(span, list) or len(span) != 2:
        return ""
    start, end = span
    if not isinstance(start, int) or not isinstance(end, int):
        return ""
    if start < 0 or end < start or end > len(text):
        return ""
    return text[start:end]


def _row_nonempty_cells(row: Mapping[str, Any], text: str) -> list[Mapping[str, Any]]:
    return [
        cell
        for cell in row.get("cells", [])
        if isinstance(cell, Mapping) and _span_text(text, cell.get("text_span")).strip()
    ]


def _infer_person_records(
    blocks: list[dict[str, Any]],
    text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    record_sets: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("kind") not in {"table", "sheet"}:
            continue
        rows = block.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            row["role"] = "empty" if not _row_nonempty_cells(row, text) else "unknown"

        header_options: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            nonempty = _row_nonempty_cells(row, text)
            matching = [
                cell
                for cell in nonempty
                if isinstance(cell, dict) and _is_person_header(_span_text(text, cell.get("text_span")))
            ]
            # A data row such as ``ALICE | Employee`` is not a header merely
            # because one value belongs to a broad people lexicon.  A
            # single-column header needs a strong label (ФИО/full name/name).
            # In a multi-column row every visible neighbour must independently
            # be a closed schema label; ``Name | Engineer`` therefore remains
            # data/unknown rather than minting an authoritative person set.
            header_shape_proven = bool(
                matching
                and (
                    (
                        len(nonempty) == 1
                        and any(
                            _is_strong_person_header(_span_text(text, cell.get("text_span")))
                            for cell in matching
                        )
                    )
                    or (
                        len(nonempty) > 1
                        and all(
                            _is_schema_header(_span_text(text, cell.get("text_span"))) for cell in nonempty
                        )
                    )
                )
            )
            if header_shape_proven:
                header_options.append((row_index, row, matching))
        if len(header_options) != 1:
            continue
        header_index, header_row, matching_cells = header_options[0]
        header_row["role"] = "header"
        # V1 has no schema that can prove whether a non-empty row before the
        # declared header is a title/preamble or an omitted data record.  Calling
        # all of them headers produced a false whole-file 1/2 count for
        # ``ALICE | role; ФИО | Роль; BOB | role``.  Preserve the literal rows,
        # but do not mint an authoritative set from this ambiguous region.
        if any(_row_nonempty_cells(row, text) for row in rows[:header_index]):
            continue

        # A horizontally merged DOCX header appears once per grid column in the
        # immutable legacy text.  It does not declare one unambiguous person
        # column and must not multiply records/candidates.
        matching_anchors = {str(cell.get("merge_anchor") or "") for cell in matching_cells}
        if len(matching_cells) != 1 or len(matching_anchors) != 1:
            continue
        matching_anchor = next(iter(matching_anchors))
        anchor_occupants = [
            cell
            for cell in header_row.get("cells", [])
            if str(cell.get("merge_anchor") or "") == matching_anchor
        ]
        if len(anchor_occupants) != 1:
            continue
        person_column = int(matching_cells[0].get("column") or 0)
        if person_column <= 0:
            continue

        record_rows: list[dict[str, Any]] = []
        ambiguous = False
        region_closed = False
        data_region = rows[header_index + 1 :]
        for data_index, row in enumerate(data_region):
            nonempty = _row_nonempty_cells(row, text)
            if not nonempty:
                row["role"] = "empty"
                if record_rows:
                    region_closed = True
                continue
            person_cells = [cell for cell in row.get("cells", []) if cell.get("column") == person_column]
            person_cell = person_cells[0] if len(person_cells) == 1 else None
            person_value = _span_text(text, person_cell.get("text_span")) if person_cell else ""
            other_values = [
                _span_text(text, cell.get("text_span"))
                for cell in row.get("cells", [])
                if cell.get("column") != person_column
            ]
            terminal_numeric_total = bool(
                person_cell is not None
                and _is_numeric_aggregate(person_value)
                and not any(
                    _row_nonempty_cells(later_row, text) for later_row in data_region[data_index + 1 :]
                )
                and all(
                    not value.strip() or _is_footer_value(value) or _is_numeric_aggregate(value)
                    for value in other_values
                )
            )
            # Footer-like prose in another column may be a legitimate role
            # (for example "Total Quality Manager").  A footer marker in the
            # declared person column is still not proof that the row may be
            # silently excluded: make the whole record set ambiguous instead.
            if person_cell is not None and (_is_footer_value(person_value) or terminal_numeric_total):
                row["role"] = "footer"
                region_closed = True
                ambiguous = True
                continue
            if (
                region_closed
                or person_cell is None
                or not person_value.strip()
                or person_cell.get("merge_anchor") != person_cell.get("id")
            ):
                row["role"] = "unknown"
                ambiguous = True
                continue
            row["role"] = "record"
            record_rows.append(row)

        if ambiguous or not record_rows:
            continue
        record_set_id = f"rs{len(record_sets) + 1:06d}"
        record_sets.append(
            {
                "id": record_set_id,
                "block_id": block["id"],
                "kind": "person_rows",
                "authoritative": True,
                "header_row_id": header_row["id"],
                "record_ids": [row["id"] for row in record_rows],
                "records_total": len(record_rows),
                "person_column": person_column,
            }
        )
        for row in record_rows:
            person_cell = next(cell for cell in row["cells"] if cell["column"] == person_column)
            if len(candidates) >= _MAX_CANDIDATES:
                # Record count remains locally authoritative, but the root will
                # be incomplete after finalization and cannot promise all names.
                break
            candidates.append(
                {
                    "id": f"cand{len(candidates) + 1:06d}",
                    "type": "person",
                    "record_id": row["id"],
                    "cell_id": person_cell["id"],
                    "text_span": list(person_cell["text_span"]),
                    "basis": "declared_person_column",
                }
            )
    return _infer_generic_records(blocks, text, record_sets), candidates


def _infer_generic_records(
    blocks: list[dict[str, Any]],
    text: str,
    record_sets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add exact row sets for closed ordinary tables with a known schema header."""

    occupied_blocks = {str(item.get("block_id") or "") for item in record_sets}
    for block in blocks:
        if block.get("kind") not in {"table", "sheet"} or block.get("id") in occupied_blocks:
            continue
        rows = block.get("rows")
        if not isinstance(rows, list):
            continue

        # A failed people-table proof may not be downgraded into an ordinary
        # row set: doing so would let a malformed person column keep exact row
        # authority while losing the stronger candidate invariant.
        if any(
            _is_person_header(_span_text(text, cell.get("text_span")))
            for row in rows
            for cell in _row_nonempty_cells(row, text)
        ):
            continue
        for row in rows:
            row["role"] = "empty" if not _row_nonempty_cells(row, text) else "unknown"

        header_options: list[tuple[int, dict[str, Any]]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            cells = [cell for cell in row.get("cells", []) if isinstance(cell, dict)]
            nonempty = _row_nonempty_cells(row, text)
            if not nonempty or len(nonempty) != len(cells):
                continue
            anchors = [str(cell.get("merge_anchor") or "") for cell in cells]
            unmerged = bool(
                all(
                    anchor and anchor == str(cell.get("id") or "")
                    for anchor, cell in zip(anchors, cells, strict=True)
                )
                and len(anchors) == len(set(anchors))
            )
            if unmerged and all(
                _is_schema_header(_span_text(text, cell.get("text_span"))) for cell in nonempty
            ):
                header_options.append((row_index, row))
        if len(header_options) != 1:
            continue
        header_index, header_row = header_options[0]
        header_row["role"] = "header"
        if any(_row_nonempty_cells(row, text) for row in rows[:header_index]):
            continue

        header_columns = [
            int(cell.get("column") or 0) for cell in header_row.get("cells", []) if isinstance(cell, Mapping)
        ]
        if not header_columns or 0 in header_columns or len(header_columns) != len(set(header_columns)):
            continue

        record_rows: list[dict[str, Any]] = []
        ambiguous = False
        region_closed = False
        data_region = rows[header_index + 1 :]
        for data_index, row in enumerate(data_region):
            nonempty = _row_nonempty_cells(row, text)
            if not nonempty:
                row["role"] = "empty"
                if record_rows:
                    region_closed = True
                continue
            cells = [cell for cell in row.get("cells", []) if isinstance(cell, dict)]
            columns = [int(cell.get("column") or 0) for cell in cells]
            values = [_span_text(text, cell.get("text_span")) for cell in cells]
            unmerged = all(str(cell.get("merge_anchor") or "") == str(cell.get("id") or "") for cell in cells)
            terminal_row = not any(
                _row_nonempty_cells(later_row, text) for later_row in data_region[data_index + 1 :]
            )
            # A footer label is structural: terminal and in the leading field.
            # Words such as ``Total``, ``Source`` or ``Prepared`` are ordinary
            # role/status prose in any other cell and must not revoke an exact
            # record set merely by sharing a prefix with a report annotation.
            footer_like = bool(terminal_row and values and values[0].strip() and _is_footer_value(values[0]))
            terminal_numeric_total = bool(
                any(_is_numeric_aggregate(value) for value in values if value.strip())
                and terminal_row
                and all(
                    not value.strip() or _is_footer_value(value) or _is_numeric_aggregate(value)
                    for value in values
                )
            )
            if footer_like or terminal_numeric_total:
                row["role"] = "footer"
                region_closed = True
                ambiguous = True
                continue
            if (
                region_closed
                or not unmerged
                or columns != header_columns
                or len(columns) != len(set(columns))
                or all(_is_schema_header_like(value) for value in values if value.strip())
            ):
                row["role"] = "unknown"
                ambiguous = True
                continue
            row["role"] = "record"
            record_rows.append(row)

        if ambiguous or not record_rows:
            continue
        record_sets.append(
            {
                "id": f"rs{len(record_sets) + 1:06d}",
                "block_id": block["id"],
                "kind": "table_rows",
                "authoritative": True,
                "header_row_id": header_row["id"],
                "record_ids": [row["id"] for row in record_rows],
                "records_total": len(record_rows),
                "person_column": None,
            }
        )
    return record_sets


def _block_text_span(block: Mapping[str, Any]) -> list[int] | None:
    spans: list[list[int]] = []
    title_span = block.get("title_span")
    if isinstance(title_span, list):
        spans.append(title_span)
    for row in block.get("rows", []):
        span = row.get("text_span") if isinstance(row, Mapping) else None
        if isinstance(span, list):
            spans.append(span)
    if not spans:
        return None
    return [min(span[0] for span in spans), max(span[1] for span in spans)]


def _recount_indexed(index: dict[str, Any]) -> None:
    blocks = index["blocks"]
    rows = [row for block in blocks for row in block.get("rows", [])]
    cells = [cell for row in rows for cell in row.get("cells", [])]
    coverage = index["coverage"]
    coverage["blocks_indexed"] = len(blocks)
    coverage["rows_indexed"] = len(rows)
    coverage["cells_indexed"] = len(cells)


def _drop_invalid_references(index: dict[str, Any]) -> None:
    blocks = {block["id"]: block for block in index["blocks"]}
    rows = {row["id"]: (block["id"], row) for block in index["blocks"] for row in block.get("rows", [])}
    cells = {cell["id"]: (row["id"], cell) for _, row in rows.values() for cell in row.get("cells", [])}
    for _, cell in cells.values():
        if cell.get("merge_anchor") not in cells:
            cell["merge_anchor"] = cell["id"]

    valid_sets: list[dict[str, Any]] = []
    for item in index["record_sets"]:
        block = blocks.get(item.get("block_id"))
        header = rows.get(item.get("header_row_id"))
        records = [rows.get(record_id) for record_id in item.get("record_ids", [])]
        if (
            block is None
            or header is None
            or header[0] != block["id"]
            or any(record is None or record[0] != block["id"] for record in records)
        ):
            continue
        if len(records) != int(item.get("records_total") or -1):
            continue
        valid_sets.append(item)
    index["record_sets"] = valid_sets

    valid_record_ids = {record_id for item in valid_sets for record_id in item["record_ids"]}
    index["candidate_refs"] = [
        item
        for item in index["candidate_refs"]
        if item.get("record_id") in valid_record_ids
        and item.get("cell_id") in cells
        and cells[item["cell_id"]][0] == item["record_id"]
    ]


def _serialized_size(index: Mapping[str, Any]) -> int:
    return len(json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _fit_serialized_budget(index: dict[str, Any], reasons: set[str]) -> None:
    if _serialized_size(index) <= OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
        return
    reasons.add("index_budget")
    index["complete"] = False
    index["coverage"]["reasons"] = _ordered_reasons(reasons)

    # Formatting is useful but less important than row identity and candidate
    # evidence. Drop whole run objects first.
    for block in reversed(index["blocks"]):
        if block.get("kind") == "paragraph" and block.get("runs"):
            block["runs"] = []
            if _serialized_size(index) <= OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
                return

    # Candidate and record-set objects are indivisible. If their full inventory
    # does not fit, the root is already incomplete and must not advertise a
    # partial authoritative set.
    if index["candidate_refs"]:
        index["candidate_refs"] = []
        index["record_sets"] = []
        if _serialized_size(index) <= OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
            return

    # Remove only complete trailing rows; never cut a row/cell object. Keep the
    # sheet-title span when present and update enclosing spans afterwards.
    while _serialized_size(index) > OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
        owner = next(
            (
                block
                for block in reversed(index["blocks"])
                if block.get("kind") in {"table", "sheet"} and block.get("rows")
            ),
            None,
        )
        if owner is None:
            break
        owner["rows"].pop()
        owner["text_span"] = _block_text_span(owner)
        _drop_invalid_references(index)
        _recount_indexed(index)

    while _serialized_size(index) > OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES and index["blocks"]:
        index["blocks"].pop()
        _drop_invalid_references(index)
        _recount_indexed(index)


def _finalize_index(
    *,
    format_name: str,
    text: str,
    blocks: list[dict[str, Any]],
    coverage: dict[str, Any],
    reasons: set[str],
) -> dict[str, Any]:
    blocks.sort(key=lambda item: int(item["source_order"]))
    record_sets, candidates = _infer_person_records(blocks, text)
    if len(candidates) >= _MAX_CANDIDATES:
        reasons.add("index_budget")
    coverage["reasons"] = _ordered_reasons(reasons)
    index: dict[str, Any] = {
        "schema_version": OFFICE_STRUCTURE_SCHEMA_VERSION,
        "format": format_name,
        "text_sha256": _exact_text_sha256(text),
        "complete": not reasons,
        "coverage": coverage,
        "blocks": blocks,
        "record_sets": record_sets,
        "candidate_refs": candidates,
    }
    _recount_indexed(index)
    _fit_serialized_budget(index, reasons)
    coverage["reasons"] = _ordered_reasons(reasons)
    index["complete"] = not reasons
    _drop_invalid_references(index)
    _recount_indexed(index)
    return index


def build_docx_text_and_structure(
    document: Any,
    *,
    max_text_chars: int,
    content: bytes | None = None,
    deadline: float | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """Return the byte-identical legacy DOCX text plus its bounded v1 index."""

    builder = _LegacyTextBuilder(max(0, int(max_text_chars)))
    coverage = _new_coverage()
    reasons: set[str] = set()
    structurally_empty_paragraphs: set[Any] = set()
    source_orders = _body_source_orders(document)
    blocks: list[dict[str, Any]] = []
    text_stopped = False
    paragraph_ordinal = 0

    for paragraph in document.paragraphs:
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            text_stopped = True
            break
        paragraph_element = paragraph._element
        if _docx_paragraph_is_structurally_empty(paragraph_element):
            structurally_empty_paragraphs.add(paragraph_element)
            continue
        paragraph_ordinal += 1
        coverage["blocks_seen"] += 1
        value = str(paragraph.text or "").strip()
        if not value:
            # ``Paragraph.text`` can be empty while a run, field, object,
            # bookmark, break, or relationship-backed wrapper still exists.
            # Only the structural proof above may turn a blank projection into
            # a neutral body separator.
            reasons.add("unsupported_body_content")
        if "\n" in value or "\r" in value:
            reasons.add("unsupported_body_content")
        span: list[int] | None = None
        clipped = False
        if not text_stopped:
            span, clipped = builder.append(value)
            if clipped:
                reasons.add("text_budget")
                text_stopped = True
        if len(blocks) >= _MAX_BLOCKS:
            reasons.add("index_budget")
            continue
        block_id = f"p{paragraph_ordinal:06d}"
        full_span = span if span is not None and not clipped else None
        blocks.append(
            {
                "id": block_id,
                "kind": "paragraph",
                "source_order": source_orders.get(paragraph._element, paragraph_ordinal - 1),
                "text_span": full_span,
                "style_role": _style_role(paragraph),
                "runs": (
                    _paragraph_runs(paragraph, block_id, full_span, reasons) if full_span is not None else []
                ),
            }
        )
        if text_stopped:
            # Match the legacy bounded reader: once the visible prefix is
            # clipped, no later paragraph/table proxy is inspected.  Besides
            # bounding latency, this keeps malformed or exotic content in an
            # unread tail from changing a successful truncated extraction.
            break

    if not text_stopped and structurally_empty_paragraphs:
        neutral_orders = sorted(
            source_orders[element] for element in structurally_empty_paragraphs if element in source_orders
        )

        def semantic_source_order(raw_order: int) -> int:
            return raw_order - bisect_left(neutral_orders, raw_order)

        for element, raw_order in list(source_orders.items()):
            source_orders[element] = semantic_source_order(raw_order)
        for block in blocks:
            block["source_order"] = semantic_source_order(int(block["source_order"]))

    table_ordinal = 0
    held_xml_cells: list[Any] = []
    merge_anchors: dict[int, str] = {}
    indexed_rows = 0
    indexed_cells = 0
    tables = document.tables if not text_stopped else ()
    for table in tables:
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            text_stopped = True
            break
        table_ordinal += 1
        coverage["blocks_seen"] += 1
        block_id = f"t{table_ordinal:06d}"
        can_index_block = len(blocks) < _MAX_BLOCKS
        if not can_index_block:
            reasons.add("index_budget")
        rows_out: list[dict[str, Any]] = []
        table_spans: list[list[int]] = []
        for source_row, row in enumerate(table.rows, start=1):
            if _deadline_expired(deadline):
                reasons.add("text_budget")
                text_stopped = True
                break
            coverage["rows_seen"] += 1
            cells = list(row.cells)
            values = [str(cell.text or "").strip() for cell in cells]
            if any("\n" in value or "\r" in value for value in values):
                reasons.add("unsupported_body_content")
            coverage["cells_seen"] += len(cells)
            for cell in cells:
                if getattr(cell, "tables", None):
                    reasons.add("nested_table")
            row_span: list[int] | None = None
            clipped = False
            if any(values) and not text_stopped:
                row_span, clipped = builder.append(" | ".join(values))
                if clipped:
                    reasons.add("text_budget")
                    text_stopped = True
            full_row_span = row_span if row_span is not None and not clipped else None
            if not can_index_block or indexed_rows >= _MAX_ROWS or indexed_cells + len(cells) > _MAX_CELLS:
                reasons.add("index_budget")
                continue
            row_id = f"{block_id}:r{source_row:06d}"
            cells_out: list[dict[str, Any]] = []
            cursor = full_row_span[0] if full_row_span is not None else 0
            for column, (cell, value) in enumerate(zip(cells, values, strict=True), start=1):
                cell_id = f"{row_id}:c{column:06d}"
                xml_cell = cell._tc
                held_xml_cells.append(xml_cell)
                xml_key = id(xml_cell)
                anchor = merge_anchors.setdefault(xml_key, cell_id)
                cell_span = [cursor, cursor + len(value)] if full_row_span is not None else None
                cells_out.append(
                    {
                        "id": cell_id,
                        "column": column,
                        "coordinate": f"R{source_row}C{column}",
                        "text_span": cell_span,
                        "merge_anchor": anchor,
                    }
                )
                if full_row_span is not None:
                    cursor += len(value)
                    if column < len(cells):
                        cursor += 3
            rows_out.append(
                {
                    "id": row_id,
                    "source_row": source_row,
                    "role": "unknown",
                    "text_span": full_row_span,
                    "cells": cells_out,
                }
            )
            indexed_rows += 1
            indexed_cells += len(cells_out)
            if full_row_span is not None:
                table_spans.append(full_row_span)
            if text_stopped:
                break
        if can_index_block:
            blocks.append(
                {
                    "id": block_id,
                    "kind": "table",
                    "source_order": source_orders.get(
                        table._element,
                        paragraph_ordinal + table_ordinal - 1,
                    ),
                    "text_span": ([table_spans[0][0], table_spans[-1][1]] if table_spans else None),
                    "rows": rows_out,
                }
            )
        if text_stopped:
            break

    del held_xml_cells  # their lifetime protected identity-based merge anchors above
    if not text_stopped:
        # Completeness scans are intentionally after the legacy text projection.
        # They can only remove authority from a fully read document; they must
        # never force a bounded reader to traverse an otherwise ignored tail.
        reasons.update(
            _docx_initial_reasons(
                document,
                structurally_empty_paragraphs,
                deadline=deadline,
            )
        )
        if content is not None:
            # Колонтитулы, сноски, примечания и надписи ЧИТАЮТСЯ, а не только
            # помечаются утраченными: на бланках там стоят «Согласовано»,
            # «Исполнитель», номер и дата — то есть ровно то, что ищут. Текст
            # уходит в тот же построитель, поэтому отпечаток индекса считается по
            # ПОЛНОМУ тексту: индекс, посчитанный по куску, был бы отброшен
            # проверкой молча.
            auxiliary, remaining = _docx_auxiliary_parts(content, deadline=deadline)
            # Причина снимается ровно потому, что часть ПРОЧИТАНА. Если чтение
            # упёрлось в потолок, `remaining` вернёт `unsupported_body_content`, и
            # документ останется неполным — но уже по другой, честной причине.
            reasons.discard("header_footer")
            for label, value in auxiliary:
                paragraph_ordinal += 1
                coverage["blocks_seen"] += 1
                span, clipped = builder.append(f"[{label}] {value}")
                if clipped:
                    reasons.add("text_budget")
                    break
                if span is None:
                    continue
                if len(blocks) >= _MAX_BLOCKS:
                    reasons.add("index_budget")
                    continue
                # Кусок служебного текста — такой же абзац для индекса, как и
                # абзац тела. Без своей строки в индексе он оставил бы «дыру»
                # между отрезками, и проверка целостности отбросила бы весь
                # индекс МОЛЧА — вместе с точным путём по таблицам.
                blocks.append(
                    {
                        "id": f"p{paragraph_ordinal:06d}",
                        "kind": "paragraph",
                        "source_order": paragraph_ordinal + table_ordinal - 1,
                        "text_span": span,
                        "style_role": "other",
                        # Один прогон на весь кусок: полный индекс требует, чтобы
                        # прогоны покрывали отрезок абзаца встык. Начертание у
                        # служебного текста не снимается — оно ничего не значит
                        # для поиска и не стоит второго обхода XML.
                        "runs": [
                            {
                                "id": f"p{paragraph_ordinal:06d}:u000001",
                                "text_span": list(span),
                                "bold": False,
                                "italic": False,
                                "underline": False,
                            }
                        ],
                    }
                )
            reasons.update(remaining)
    text = builder.text()
    index = _finalize_index(
        format_name="docx",
        text=text,
        blocks=blocks,
        coverage=coverage,
        reasons=reasons,
    )
    return text, index, bool({"text_budget", "row_budget"} & reasons)


def _column_letters(column: int) -> str:
    letters = ""
    value = int(column)
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters or "A"


def _parse_coordinate(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", str(value or "").upper())
    if not match:
        return None
    column = 0
    for character in match.group(1):
        column = column * 26 + ord(character) - 64
    return int(match.group(2)), column


def _parse_dimension_bounds(value: str) -> tuple[int, int, int, int] | None:
    endpoints = str(value or "").replace("$", "").split(":", 1)
    start = _parse_coordinate(endpoints[0])
    end = _parse_coordinate(endpoints[-1])
    if start is None or end is None:
        return None
    min_row, max_row = sorted((start[0], end[0]))
    min_col, max_col = sorted((start[1], end[1]))
    if max_row > 1_048_576 or max_col > 16_384:
        return None
    return min_row, min_col, max_row, max_col


def _merge_rectangles(
    content: bytes,
    sheet_paths: Sequence[str],
    reasons: set[str],
    *,
    deadline: float | None = None,
) -> dict[str, list[tuple[int, int, int, int]]]:
    normalized_paths = [str(path).lstrip("/") for path in sheet_paths]
    result: dict[str, list[tuple[int, int, int, int]]] = {path: [] for path in normalized_paths}
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            for path in normalized_paths:
                if _deadline_expired(deadline):
                    reasons.add("text_budget")
                    return result
                rectangles = result[path]
                with archive.open(path) as stream:
                    for event_index, (_, element) in enumerate(
                        ElementTree.iterparse(stream, events=("end",)),
                        start=1,
                    ):
                        if event_index % 256 == 0 and _deadline_expired(deadline):
                            reasons.add("text_budget")
                            return result
                        if _local_name(element.tag) != "mergeCell":
                            element.clear()
                            continue
                        if len(rectangles) >= _MAX_MERGE_RANGES:
                            reasons.add("merge_scan_budget")
                            element.clear()
                            continue
                        reference = str(element.attrib.get("ref") or "")
                        endpoints = reference.split(":", 1)
                        start = _parse_coordinate(endpoints[0])
                        end = _parse_coordinate(endpoints[-1])
                        if (
                            len(endpoints) != 2
                            or start is None
                            or end is None
                            or start[0] > end[0]
                            or start[1] > end[1]
                            or start == end
                            or end[0] > 1_048_576
                            or end[1] > 16_384
                        ):
                            reasons.add("merge_topology")
                            element.clear()
                            continue
                        rectangle = (start[0], start[1], end[0], end[1])
                        if any(
                            rectangle[0] <= existing[2]
                            and existing[0] <= rectangle[2]
                            and rectangle[1] <= existing[3]
                            and existing[1] <= rectangle[3]
                            for existing in rectangles
                        ):
                            # Two merged regions may not own the same cell.  A
                            # first-match anchor would otherwise make the exact
                            # row inventory depend on archive order.
                            reasons.add("merge_topology")
                            element.clear()
                            continue
                        rectangles.append(rectangle)
                        element.clear()
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        reasons.add("merge_scan_unavailable")
    return result


def _merge_anchor_coordinate(
    row: int,
    column: int,
    rectangles: Sequence[tuple[int, int, int, int]],
) -> tuple[int, int]:
    for min_row, min_col, max_row, max_col in rectangles:
        if min_row <= row <= max_row and min_col <= column <= max_col:
            return min_row, min_col
    return row, column


def _sheet_visibility(value: Any) -> str:
    state = str(value or "visible")
    if state == "veryHidden":
        return "very_hidden"
    return state if state in {"visible", "hidden"} else "hidden"


def build_xlsx_text_and_structure(
    workbook: Any,
    formula_workbook: Any | None,
    *,
    content: bytes,
    max_text_chars: int,
    max_rows: int,
    deadline: float | None = None,
) -> tuple[str, dict[str, Any], bool, int]:
    """Return the byte-identical legacy XLSX text plus its bounded v1 index."""

    builder = _LegacyTextBuilder(max(0, int(max_text_chars)))
    coverage = _new_coverage()
    reasons: set[str] = set()
    blocks: list[dict[str, Any]] = []
    text_stopped = False
    legacy_rows_read = 0
    visited_cells = 0
    sheet_paths = [
        str(getattr(sheet, "_worksheet_path", "") or "").lstrip("/") for sheet in workbook.worksheets
    ]
    package_reasons, actual_extents = _xlsx_package_reasons(
        content,
        sheet_paths,
        deadline=deadline,
    )
    reasons.update(package_reasons)
    merge_ranges = _merge_rectangles(content, sheet_paths, reasons, deadline=deadline)
    formula_sheets = list(formula_workbook.worksheets) if formula_workbook is not None else []
    if formula_workbook is None or len(formula_sheets) != len(workbook.worksheets):
        reasons.add("formula_scan_unavailable")

    indexed_rows = 0
    indexed_cells = 0
    for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
        if _deadline_expired(deadline):
            reasons.add("text_budget")
            text_stopped = True
            break
        coverage["blocks_seen"] += 1
        block_id = f"s{sheet_index:06d}"
        header = f"--- Sheet: {sheet.title} ---"
        title_span: list[int] | None = None
        header_span: list[int] | None = None
        clipped = False
        if not text_stopped:
            header_span, clipped = builder.append(header)
            if header_span is not None:
                title_start = header_span[0] + len("--- Sheet: ")
                title_span = [title_start, min(title_start + len(str(sheet.title)), header_span[1])]
            if clipped:
                reasons.add("text_budget")
                text_stopped = True

        can_index_block = len(blocks) < _MAX_BLOCKS
        if not can_index_block:
            reasons.add("index_budget")
        rows_out: list[dict[str, Any]] = []
        block_spans: list[list[int]] = [header_span] if header_span is not None and not clipped else []
        formula_sheet = formula_sheets[sheet_index - 1] if sheet_index <= len(formula_sheets) else None
        if formula_sheet is not None and str(getattr(formula_sheet, "title", "")) != str(
            getattr(sheet, "title", "")
        ):
            reasons.add("formula_alignment")
        rectangles = merge_ranges.get(sheet_paths[sheet_index - 1], [])

        if text_stopped:
            if can_index_block:
                blocks.append(
                    {
                        "id": block_id,
                        "kind": "sheet",
                        "source_order": sheet_index - 1,
                        "text_span": None,
                        "title_span": None,
                        "visibility": _sheet_visibility(getattr(sheet, "sheet_state", "visible")),
                        "rows": [],
                    }
                )
            break

        try:
            xml_rows, xml_columns = actual_extents.get(sheet_paths[sheet_index - 1], (0, 0))
            declared_rows = max(0, int(getattr(sheet, "max_row", 0) or xml_rows))
            declared_columns = max(1, int(getattr(sheet, "max_column", 0) or xml_columns or 1))
        except (TypeError, ValueError, OverflowError):
            declared_rows = max_rows + 1
            declared_columns = _MAX_XLSX_VISITED_CELLS + 1
            reasons.add("formula_alignment")
        remaining_rows = max(0, max_rows - legacy_rows_read)
        remaining_cells = max(0, _MAX_XLSX_VISITED_CELLS - visited_cells)
        rows_by_cell_budget = remaining_cells // declared_columns
        rows_to_visit = min(declared_rows, remaining_rows, rows_by_cell_budget)
        data_rows = (
            sheet.iter_rows(
                min_row=1,
                max_row=rows_to_visit,
                max_col=declared_columns,
                values_only=True,
            )
            if rows_to_visit > 0
            else ()
        )
        formula_iterator = (
            iter(
                formula_sheet.iter_rows(
                    min_row=1,
                    max_row=rows_to_visit,
                    max_col=declared_columns,
                )
            )
            if formula_sheet is not None and rows_to_visit > 0
            else None
        )

        for source_row, data_row in enumerate(data_rows, start=1):
            if _deadline_expired(deadline):
                reasons.add("text_budget")
                text_stopped = True
                break
            legacy_rows_read += 1
            visited_cells += len(data_row)
            coverage["rows_seen"] += 1
            values = [str(value) if value is not None else "" for value in data_row]
            if any("\n" in value or "\r" in value for value in values):
                reasons.add("unsupported_body_content")
            coverage["cells_seen"] += len(values)
            formula_row: Sequence[Any] | None = None
            if formula_iterator is not None:
                try:
                    formula_row = next(formula_iterator)
                except StopIteration:
                    reasons.add("formula_alignment")
                    formula_iterator = None
                except Exception:  # noqa: BLE001 - auxiliary coverage must not break legacy text
                    reasons.add("formula_scan_unavailable")
                    formula_iterator = None
            if formula_row is not None and len(formula_row) != len(data_row):
                reasons.add("formula_alignment")
            for column, value in enumerate(data_row, start=1):
                formula_cell = (
                    formula_row[column - 1]
                    if formula_row is not None and column <= len(formula_row)
                    else None
                )
                if getattr(formula_cell, "data_type", None) == "f":
                    if str(getattr(formula_cell, "coordinate", "")).upper() != (
                        f"{_column_letters(column)}{source_row}"
                    ):
                        reasons.add("formula_alignment")
                    if value is None:
                        reasons.add("formula_without_cached_value")

            row_span: list[int] | None = None
            row_clipped = False
            if any(values) and not text_stopped:
                row_span, row_clipped = builder.append(" | ".join(values))
                if row_clipped:
                    reasons.add("text_budget")
                    text_stopped = True
            full_row_span = row_span if row_span is not None and not row_clipped else None
            if not can_index_block or indexed_rows >= _MAX_ROWS or indexed_cells + len(values) > _MAX_CELLS:
                reasons.add("index_budget")
                continue
            row_id = f"{block_id}:r{source_row:06d}"
            cells_out: list[dict[str, Any]] = []
            cursor = full_row_span[0] if full_row_span is not None else 0
            for column, value in enumerate(values, start=1):
                cell_id = f"{row_id}:c{column:06d}"
                anchor_row, anchor_col = _merge_anchor_coordinate(source_row, column, rectangles)
                anchor_id = f"{block_id}:r{anchor_row:06d}:c{anchor_col:06d}"
                cell_span = [cursor, cursor + len(value)] if full_row_span is not None else None
                cells_out.append(
                    {
                        "id": cell_id,
                        "column": column,
                        "coordinate": f"{_column_letters(column)}{source_row}",
                        "text_span": cell_span,
                        "merge_anchor": anchor_id,
                    }
                )
                if full_row_span is not None:
                    cursor += len(value)
                    if column < len(values):
                        cursor += 3
            rows_out.append(
                {
                    "id": row_id,
                    "source_row": source_row,
                    "role": "unknown",
                    "text_span": full_row_span,
                    "cells": cells_out,
                }
            )
            indexed_rows += 1
            indexed_cells += len(cells_out)
            if full_row_span is not None:
                block_spans.append(full_row_span)
            if text_stopped:
                # Legacy extraction stopped immediately after the clipped row.
                break
        if not text_stopped and rows_to_visit < declared_rows:
            # The iterator expands every coordinate in the declared rectangle,
            # including blank cells.  Bound that work before requesting the
            # next potentially 16,384-cell row; the formula view advances only
            # alongside rows admitted by this same budget.
            reasons.add("row_budget")
            text_stopped = True
        if can_index_block:
            blocks.append(
                {
                    "id": block_id,
                    "kind": "sheet",
                    "source_order": sheet_index - 1,
                    "text_span": ([block_spans[0][0], block_spans[-1][1]] if block_spans else None),
                    "title_span": title_span if header_span is not None and not clipped else None,
                    "visibility": _sheet_visibility(getattr(sheet, "sheet_state", "visible")),
                    "rows": rows_out,
                }
            )
        if text_stopped:
            # This is the legacy stop boundary: later rows/sheets were never read.
            break

    text = builder.text()
    index = _finalize_index(
        format_name="xlsx",
        text=text,
        blocks=blocks,
        coverage=coverage,
        reasons=reasons,
    )
    return text, index, bool({"text_budget", "row_budget"} & reasons), legacy_rows_read


def _is_plain_int(value: Any, *, minimum: int = 0, maximum: int = _MAX_VALID_COUNT) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _validated_span(value: Any, *, text_length: int, allow_none: bool) -> list[int] | None | object:
    invalid = object()
    if value is None:
        return None if allow_none else invalid
    if not isinstance(value, list) or len(value) != 2:
        return invalid
    start, end = value
    if not _is_plain_int(start, maximum=text_length) or not _is_plain_int(end, maximum=text_length):
        return invalid
    if start > end:
        return invalid
    return [start, end]


def _inside(inner: list[int], outer: list[int]) -> bool:
    return outer[0] <= inner[0] <= inner[1] <= outer[1]


def _canonical_row_text(row: Mapping[str, Any], text: str, *, complete: bool) -> bool:
    """Prove one row is exactly ``cell ( ' | ' cell )*`` with no hidden tail."""

    row_span = row.get("text_span")
    cells = row.get("cells")
    if not isinstance(cells, list):
        return False
    if row_span is None:
        return all(isinstance(cell, Mapping) and cell.get("text_span") is None for cell in cells)
    if not isinstance(row_span, list) or len(row_span) != 2 or not cells:
        return False
    cursor = row_span[0]
    for position, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            return False
        cell_span = cell.get("text_span")
        if not isinstance(cell_span, list) or len(cell_span) != 2 or cell_span[0] != cursor:
            return False
        literal = text[cell_span[0] : cell_span[1]]
        if complete and ("\n" in literal or "\r" in literal):
            return False
        cursor = cell_span[1]
        if position < len(cells) - 1:
            if text[cursor : cursor + 3] != " | ":
                return False
            cursor += 3
    return cursor == row_span[1]


def _xlsx_header_atom(block: Mapping[str, Any], text: str) -> list[int] | None:
    """Recover and prove the fixed wrapper around the content-free title span."""

    title_span = block.get("title_span")
    if not isinstance(title_span, list) or len(title_span) != 2:
        return None
    prefix = "--- Sheet: "
    suffix = " ---"
    header_start = title_span[0] - len(prefix)
    header_end = title_span[1] + len(suffix)
    if (
        header_start < 0
        or header_end > len(text)
        or text[header_start : title_span[0]] != prefix
        or text[title_span[1] : header_end] != suffix
        or "\n" in text[title_span[0] : title_span[1]]
        or "\r" in text[title_span[0] : title_span[1]]
    ):
        return None
    return [header_start, header_end]


def _canonical_atomic_coverage(
    format_name: str,
    blocks: Sequence[Mapping[str, Any]],
    text: str,
    *,
    complete: bool,
) -> bool:
    """Reject duplicate/expanded atoms and prove complete indexes tile raw text."""

    atoms: list[list[int]] = []
    for block in blocks:
        kind = block.get("kind")
        block_span = block.get("text_span")
        if kind == "paragraph":
            if block_span is None:
                continue
            if not isinstance(block_span, list) or (complete and "\n" in text[block_span[0] : block_span[1]]):
                return False
            atoms.append(block_span)
            continue

        rows = block.get("rows")
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) or not _canonical_row_text(row, text, complete=complete)
            for row in rows
        ):
            return False
        row_spans = [row["text_span"] for row in rows if isinstance(row.get("text_span"), list)]
        if format_name == "xlsx":
            header_atom = _xlsx_header_atom(block, text)
            if header_atom is None:
                if complete:
                    return False
            else:
                atoms.append(header_atom)
            if complete and isinstance(block_span, list):
                expected_end = row_spans[-1][1] if row_spans else header_atom[1] if header_atom else -1
                if header_atom is None or block_span != [header_atom[0], expected_end]:
                    return False
        elif complete:
            expected_span = [row_spans[0][0], row_spans[-1][1]] if row_spans else None
            if block_span != expected_span:
                return False
        atoms.extend(row_spans)

    atoms.sort(key=lambda span: (span[0], span[1]))
    for position, atom in enumerate(atoms):
        if atom[0] >= atom[1]:
            return False
        if position and atom[0] <= atoms[position - 1][1]:
            return False
    if not complete:
        return True
    if not atoms:
        return not text
    if atoms[0][0] != 0 or atoms[-1][1] != len(text):
        return False
    return all(
        current[0] == previous[1] + 1 and text[previous[1] : current[0]] == "\n"
        for previous, current in zip(atoms, atoms[1:], strict=False)
    )


def validate_office_structure_index(index: Mapping[str, Any], text: str) -> dict[str, Any] | None:
    """Return a detached canonical v1 index, or ``None`` on any mismatch.

    Strict allowlists are intentional.  The index crosses a durable metadata
    boundary and later controls what private source text reaches the local model;
    accepting an unknown string field would turn a content-free capability into
    an unnoticed second copy of document contents.
    """

    if not isinstance(index, Mapping) or not isinstance(text, str):
        return None
    try:
        if set(index) != _ROOT_KEYS or _serialized_size(index) > OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
            return None
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    if index.get("schema_version") != OFFICE_STRUCTURE_SCHEMA_VERSION:
        return None
    format_name = index.get("format")
    if format_name not in {"docx", "xlsx"}:
        return None
    digest = index.get("text_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        return None
    try:
        exact_digest = _exact_text_sha256(text)
    except UnicodeEncodeError:
        return None
    if digest != exact_digest:
        return None
    if not isinstance(index.get("complete"), bool):
        return None

    coverage_value = index.get("coverage")
    if not isinstance(coverage_value, Mapping) or set(coverage_value) != _COVERAGE_KEYS:
        return None
    reasons_value = coverage_value.get("reasons")
    if not isinstance(reasons_value, list) or any(
        not isinstance(reason, str) or reason not in _COVERAGE_REASONS for reason in reasons_value
    ):
        return None
    if reasons_value != _ordered_reasons(set(reasons_value)):
        return None
    coverage: dict[str, Any] = {"reasons": list(reasons_value)}
    for key in _COVERAGE_KEYS - {"reasons"}:
        value = coverage_value.get(key)
        if not _is_plain_int(value):
            return None
        coverage[key] = value
    if bool(index.get("complete")) != (not reasons_value):
        return None

    blocks_value = index.get("blocks")
    if not isinstance(blocks_value, list) or len(blocks_value) > _MAX_BLOCKS:
        return None
    blocks: list[dict[str, Any]] = []
    block_ids: set[str] = set()
    row_lookup: dict[str, tuple[str, dict[str, Any]]] = {}
    cell_lookup: dict[str, tuple[str, dict[str, Any]]] = {}
    block_kind_ordinals = {"paragraph": 0, "table": 0, "sheet": 0}
    last_source_order = -1
    total_runs = 0
    total_rows = 0
    total_cells = 0

    for block_value in blocks_value:
        if not isinstance(block_value, Mapping):
            return None
        kind = block_value.get("kind")
        if not isinstance(kind, str) or kind not in block_kind_ordinals:
            return None
        expected_keys = (
            _PARAGRAPH_KEYS
            if kind == "paragraph"
            else _TABLE_KEYS
            if kind == "table"
            else _SHEET_KEYS
            if kind == "sheet"
            else None
        )
        if expected_keys is None or set(block_value) != expected_keys:
            return None
        block_id = block_value.get("id")
        expected_id_re = (
            _PARAGRAPH_ID_RE if kind == "paragraph" else _TABLE_ID_RE if kind == "table" else _SHEET_ID_RE
        )
        block_kind_ordinals[kind] += 1
        expected_block_id = f"{kind[0]}{block_kind_ordinals[kind]:06d}"
        if (
            not isinstance(block_id, str)
            or not expected_id_re.fullmatch(block_id)
            or block_id != expected_block_id
            or block_id in block_ids
        ):
            return None
        source_order = block_value.get("source_order")
        if (
            not isinstance(source_order, int)
            or isinstance(source_order, bool)
            or not _is_plain_int(source_order)
        ):
            return None
        if source_order <= last_source_order:
            return None
        last_source_order = source_order
        block_ids.add(block_id)
        block_span = _validated_span(block_value.get("text_span"), text_length=len(text), allow_none=True)
        if not (block_span is None or isinstance(block_span, list)):
            return None

        if kind == "paragraph":
            style_role = block_value.get("style_role")
            runs_value = block_value.get("runs")
            if style_role not in _STYLE_ROLES or not isinstance(runs_value, list):
                return None
            runs: list[dict[str, Any]] = []
            run_ids: set[str] = set()
            last_run_ordinal = 0
            last_run_end: int | None = None
            for run_value in runs_value:
                total_runs += 1
                if (
                    total_runs > _MAX_RUNS
                    or not isinstance(run_value, Mapping)
                    or set(run_value) != _RUN_KEYS
                ):
                    return None
                run_id = run_value.get("id")
                run_span = _validated_span(
                    run_value.get("text_span"), text_length=len(text), allow_none=False
                )
                run_ordinal = (
                    int(run_id[-6:]) if isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id) else 0
                )
                if (
                    not isinstance(run_id, str)
                    or not _RUN_ID_RE.fullmatch(run_id)
                    or not run_id.startswith(f"{block_id}:")
                    or run_id in run_ids
                    or run_ordinal <= last_run_ordinal
                    or not isinstance(run_span, list)
                    or run_span[0] == run_span[1]
                    or (last_run_end is not None and run_span[0] < last_run_end)
                    or not isinstance(block_span, list)
                    or not _inside(run_span, block_span)
                    or any(
                        not isinstance(run_value.get(flag), bool) for flag in ("bold", "italic", "underline")
                    )
                ):
                    return None
                run_ids.add(run_id)
                last_run_ordinal = run_ordinal
                last_run_end = run_span[1]
                runs.append(
                    {
                        "id": run_id,
                        "text_span": run_span,
                        "bold": run_value["bold"],
                        "italic": run_value["italic"],
                        "underline": run_value["underline"],
                    }
                )
            if (
                index.get("complete")
                and isinstance(block_span, list)
                and (
                    not runs
                    or runs[0]["text_span"][0] != block_span[0]
                    or runs[-1]["text_span"][1] != block_span[1]
                    or any(
                        current["text_span"][0] != previous["text_span"][1]
                        for previous, current in zip(runs, runs[1:], strict=False)
                    )
                )
            ):
                return None
            blocks.append(
                {
                    "id": block_id,
                    "kind": "paragraph",
                    "source_order": source_order,
                    "text_span": block_span,
                    "style_role": style_role,
                    "runs": runs,
                }
            )
            continue

        rows_value = block_value.get("rows")
        if not isinstance(rows_value, list):
            return None
        rows: list[dict[str, Any]] = []
        block_cell_ids: set[str] = set()
        for row_position, row_value in enumerate(rows_value, start=1):
            total_rows += 1
            if total_rows > _MAX_ROWS or not isinstance(row_value, Mapping) or set(row_value) != _ROW_KEYS:
                return None
            row_id = row_value.get("id")
            source_row = row_value.get("source_row")
            role = row_value.get("role")
            row_span = _validated_span(row_value.get("text_span"), text_length=len(text), allow_none=True)
            if (
                not isinstance(row_id, str)
                or not _ROW_ID_RE.fullmatch(row_id)
                or row_id != f"{block_id}:r{row_position:06d}"
                or row_id in row_lookup
                or not _is_plain_int(source_row, minimum=1)
                or source_row != row_position
                or role not in _ROW_ROLES
                or not (row_span is None or isinstance(row_span, list))
                or (
                    isinstance(row_span, list)
                    and (not isinstance(block_span, list) or not _inside(row_span, block_span))
                )
            ):
                return None
            cells_value = row_value.get("cells")
            if not isinstance(cells_value, list):
                return None
            cells: list[dict[str, Any]] = []
            last_column = 0
            for cell_position, cell_value in enumerate(cells_value, start=1):
                total_cells += 1
                if (
                    total_cells > _MAX_CELLS
                    or not isinstance(cell_value, Mapping)
                    or set(cell_value) != _CELL_KEYS
                ):
                    return None
                cell_id = cell_value.get("id")
                column = cell_value.get("column")
                coordinate = cell_value.get("coordinate")
                cell_span = _validated_span(
                    cell_value.get("text_span"), text_length=len(text), allow_none=True
                )
                merge_anchor = cell_value.get("merge_anchor")
                coordinate_re = _DOCX_COORDINATE_RE if kind == "table" else _XLSX_COORDINATE_RE
                expected_coordinate = (
                    f"R{source_row}C{cell_position}"
                    if kind == "table"
                    else f"{_column_letters(cell_position)}{source_row}"
                )
                if (
                    not isinstance(cell_id, str)
                    or not _CELL_ID_RE.fullmatch(cell_id)
                    or cell_id != f"{row_id}:c{cell_position:06d}"
                    or cell_id in cell_lookup
                    or column != cell_position
                    or not isinstance(coordinate, str)
                    or not coordinate_re.fullmatch(coordinate)
                    or coordinate != expected_coordinate
                    or not (cell_span is None or isinstance(cell_span, list))
                    or (
                        isinstance(cell_span, list)
                        and (not isinstance(row_span, list) or not _inside(cell_span, row_span))
                    )
                    or not isinstance(merge_anchor, str)
                    or not _CELL_ID_RE.fullmatch(merge_anchor)
                    or not merge_anchor.startswith(f"{block_id}:")
                ):
                    return None
                if not isinstance(column, int) or isinstance(column, bool):
                    return None
                if column <= last_column:
                    return None
                last_column = column
                cell = {
                    "id": cell_id,
                    "column": column,
                    "coordinate": coordinate,
                    "text_span": cell_span,
                    "merge_anchor": merge_anchor,
                }
                cells.append(cell)
                block_cell_ids.add(cell_id)
                cell_lookup[cell_id] = (row_id, cell)
            row = {
                "id": row_id,
                "source_row": source_row,
                "role": role,
                "text_span": row_span,
                "cells": cells,
            }
            rows.append(row)
            row_lookup[row_id] = (block_id, row)
        for row in rows:
            if any(cell["merge_anchor"] not in block_cell_ids for cell in row["cells"]):
                return None
        block: dict[str, Any] = {
            "id": block_id,
            "kind": kind,
            "source_order": source_order,
            "text_span": block_span,
            "rows": rows,
        }
        if kind == "sheet":
            title_span = _validated_span(
                block_value.get("title_span"), text_length=len(text), allow_none=True
            )
            visibility = block_value.get("visibility")
            if (
                not (title_span is None or isinstance(title_span, list))
                or (
                    isinstance(title_span, list)
                    and (not isinstance(block_span, list) or not _inside(title_span, block_span))
                )
                or visibility not in _VISIBILITIES
            ):
                return None
            block["title_span"] = title_span
            block["visibility"] = visibility
            # Preserve canonical field order used by builders/renderers.
            block = {
                "id": block_id,
                "kind": kind,
                "source_order": source_order,
                "text_span": block_span,
                "title_span": title_span,
                "visibility": visibility,
                "rows": rows,
            }
        blocks.append(block)

    if index.get("complete") and [block["source_order"] for block in blocks] != list(range(len(blocks))):
        return None
    if not _canonical_atomic_coverage(
        format_name,
        blocks,
        text,
        complete=bool(index["complete"]),
    ):
        return None

    if coverage["blocks_indexed"] != len(blocks):
        return None
    if coverage["rows_indexed"] != total_rows or coverage["cells_indexed"] != total_cells:
        return None
    if (
        coverage["blocks_seen"] < len(blocks)
        or coverage["rows_seen"] < total_rows
        or coverage["cells_seen"] < total_cells
    ):
        return None
    if index.get("complete") and (
        coverage["blocks_seen"] != len(blocks)
        or coverage["rows_seen"] != total_rows
        or coverage["cells_seen"] != total_cells
    ):
        return None

    record_sets_value = index.get("record_sets")
    if not isinstance(record_sets_value, list):
        return None
    record_sets: list[dict[str, Any]] = []
    record_set_ids: set[str] = set()
    authoritative_record_ids: set[str] = set()
    expected_candidate_cells: dict[str, str] = {}
    for record_set_position, item_value in enumerate(record_sets_value, start=1):
        if not isinstance(item_value, Mapping) or set(item_value) != _RECORD_SET_KEYS:
            return None
        item_id = item_value.get("id")
        block_id = item_value.get("block_id")
        header_row_id = item_value.get("header_row_id")
        record_ids = item_value.get("record_ids")
        records_total = item_value.get("records_total")
        person_column = item_value.get("person_column")
        record_set_kind = item_value.get("kind")
        if (
            not isinstance(item_id, str)
            or not _RECORD_SET_ID_RE.fullmatch(item_id)
            or item_id != f"rs{record_set_position:06d}"
            or item_id in record_set_ids
            or block_id not in block_ids
            or record_set_kind not in {"person_rows", "table_rows"}
            or item_value.get("authoritative") is not True
            or header_row_id not in row_lookup
            or row_lookup[header_row_id][0] != block_id
            or row_lookup[header_row_id][1]["role"] != "header"
            or not isinstance(record_ids, list)
            or len(record_ids) != len(set(record_ids))
            or not _is_plain_int(records_total)
            or records_total != len(record_ids)
        ):
            return None
        if record_set_kind == "person_rows":
            if not _is_plain_int(person_column, minimum=1):
                return None
        elif person_column is not None:
            return None
        for record_id in record_ids:
            if (
                not isinstance(record_id, str)
                or record_id not in row_lookup
                or row_lookup[record_id][0] != block_id
                or row_lookup[record_id][1]["role"] != "record"
                or record_id in authoritative_record_ids
            ):
                return None
        header_cells = row_lookup[header_row_id][1]["cells"]
        header_columns = {cell["column"] for cell in header_cells}
        if record_set_kind == "person_rows":
            if person_column not in header_columns:
                return None
            header_person_cells = [
                cell
                for cell in header_cells
                if cell["column"] == person_column
                and _is_person_header(_span_text(text, cell["text_span"]))
                and cell["merge_anchor"] == cell["id"]
            ]
            if len(header_person_cells) != 1:
                return None
            for record_id in record_ids:
                person_cells = [
                    cell
                    for cell in row_lookup[record_id][1]["cells"]
                    if cell["column"] == person_column
                    and isinstance(cell["text_span"], list)
                    and cell["text_span"][0] < cell["text_span"][1]
                    and _span_text(text, cell["text_span"]).strip()
                    and cell["merge_anchor"] == cell["id"]
                ]
                if len(person_cells) != 1:
                    return None
                expected_candidate_cells[record_id] = person_cells[0]["id"]
        else:
            if (
                not header_cells
                or any(cell["merge_anchor"] != cell["id"] for cell in header_cells)
                or any(
                    not _span_text(text, cell["text_span"]).strip()
                    or not _is_schema_header(_span_text(text, cell["text_span"]))
                    or _is_person_header(_span_text(text, cell["text_span"]))
                    for cell in header_cells
                )
            ):
                return None
            ordered_header_columns = [cell["column"] for cell in header_cells]
            if len(ordered_header_columns) != len(set(ordered_header_columns)):
                return None
            for record_id in record_ids:
                record_cells = row_lookup[record_id][1]["cells"]
                if [cell["column"] for cell in record_cells] != ordered_header_columns or any(
                    cell["merge_anchor"] != cell["id"] for cell in record_cells
                ):
                    return None
        record_set_ids.add(item_id)
        authoritative_record_ids.update(record_ids)
        record_sets.append(
            {
                "id": item_id,
                "block_id": block_id,
                "kind": record_set_kind,
                "authoritative": True,
                "header_row_id": header_row_id,
                "record_ids": list(record_ids),
                "records_total": records_total,
                "person_column": person_column,
            }
        )

    candidates_value = index.get("candidate_refs")
    if not isinstance(candidates_value, list) or len(candidates_value) > _MAX_CANDIDATES:
        return None
    candidates: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    candidate_cells: set[str] = set()
    for candidate_position, item_value in enumerate(candidates_value, start=1):
        if not isinstance(item_value, Mapping) or set(item_value) != _CANDIDATE_KEYS:
            return None
        item_id = item_value.get("id")
        record_id = item_value.get("record_id")
        cell_id = item_value.get("cell_id")
        candidate_span = _validated_span(item_value.get("text_span"), text_length=len(text), allow_none=False)
        if (
            not isinstance(item_id, str)
            or not _CANDIDATE_ID_RE.fullmatch(item_id)
            or item_id != f"cand{candidate_position:06d}"
            or item_id in candidate_ids
            or item_value.get("type") != "person"
            or item_value.get("basis") != "declared_person_column"
            or record_id not in authoritative_record_ids
            or expected_candidate_cells.get(record_id) != cell_id
            or cell_id not in cell_lookup
            or cell_lookup[cell_id][0] != record_id
            or cell_id in candidate_cells
            or not isinstance(candidate_span, list)
            or candidate_span[0] == candidate_span[1]
            or candidate_span != cell_lookup[cell_id][1]["text_span"]
        ):
            return None
        candidate_ids.add(item_id)
        candidate_cells.add(cell_id)
        candidates.append(
            {
                "id": item_id,
                "type": "person",
                "record_id": record_id,
                "cell_id": cell_id,
                "text_span": candidate_span,
                "basis": "declared_person_column",
            }
        )

    # An authoritative set is exact only if every record contributes exactly
    # one candidate from its declared person column.  A syntactically valid
    # 8/16 subset is still incomplete evidence and must fail closed.
    if {item["record_id"] for item in candidates} != set(expected_candidate_cells):
        return None

    if index.get("complete"):
        # Re-derive semantic roles and the full authoritative inventory from the
        # bound text.  Local reference checks alone accept a coordinated 15/16
        # mutation (drop one record and candidate, lower the count, mark its row
        # unknown).  A complete index must equal the code-owned derivation,
        # including source-row order and one candidate per record.
        derived_blocks = copy.deepcopy(blocks)
        derived_record_sets, derived_candidates = _infer_person_records(derived_blocks, text)
        if derived_blocks != blocks or derived_record_sets != record_sets or derived_candidates != candidates:
            return None

    canonical = {
        "schema_version": OFFICE_STRUCTURE_SCHEMA_VERSION,
        "format": format_name,
        "text_sha256": digest,
        "complete": bool(index["complete"]),
        "coverage": coverage,
        "blocks": blocks,
        "record_sets": record_sets,
        "candidate_refs": candidates,
    }
    try:
        if _serialized_size(canonical) > OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES:
            return None
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return canonical


__all__ = [
    "OFFICE_STRUCTURE_MAX_SERIALIZED_BYTES",
    "OFFICE_STRUCTURE_SCHEMA_VERSION",
    "build_docx_text_and_structure",
    "build_xlsx_text_and_structure",
    "validate_office_structure_index",
]
