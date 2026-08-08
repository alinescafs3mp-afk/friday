"""Готовые файлы: Word, Excel, PDF, картинка.

Требование владельца (2026-08-01): «сделай мне отчёт с выводом по тем-то
документам» и подобные просьбы должны заканчиваться настоящим файлом, а не
текстом в чате.

Документ описывается СТРУКТУРОЙ, а не разметкой: заголовки, абзацы, списки,
таблицы. Одна и та же структура кладётся в любой из форматов — иначе для каждого
пришлось бы учить модель отдельному языку разметки, и три формата разошлись бы по
возможностям в первый же день.

Здесь нет ни базы, ни модели: на вход — что писать, на выход — байты. Откуда
взялось содержимое и правда ли оно, решают выше.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Block",
    "ReportSpec",
    "SUPPORTED_KINDS",
    "render",
    "sheet_title_from_report_title",
]

#: Что умеем отдать. Расширение — часть договора: по нему Telegram выбирает,
#: как показать файл, а человек — чем открыть.
SUPPORTED_KINDS: dict[str, tuple[str, str]] = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pdf": ("application/pdf", "pdf"),
    "png": ("image/png", "png"),
}

#: Кириллица в PDF: встроенные шрифты reportlab её не знают, и текст молча
#: превращается в чёрные квадраты. DejaVu лежит в системе и покрывает кириллицу.
_PDF_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)
#: Потолок высоты картинки. Примерно 130 строк — столько человек ещё
#: разглядывает; дальше формат перестаёт быть картинкой и становится обузой.
_PNG_MAX_HEIGHT = 4000
_PDF_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


@dataclass(frozen=True)
class Block:
    """Кусок документа: заголовок, абзац, список или таблица.

    `kind` — одно из `heading`, `text`, `bullets`, `table`. Для таблицы `rows` —
    строки, первая считается шапкой.
    """

    kind: str
    text: str = ""
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class ReportSpec:
    title: str
    blocks: list[Block]
    subtitle: str = ""


def _as_blocks(raw: Any) -> list[Block]:
    """Структура от модели — в блоки, молча отбрасывая мусор.

    Модель ошибётся в форме рано или поздно; падать из-за лишнего ключа посреди
    отчёта хуже, чем пропустить непонятный кусок.
    """
    blocks: list[Block] = []
    if not isinstance(raw, list):
        return blocks
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "text").strip().casefold()
        if kind in {"heading", "header", "title"}:
            blocks.append(Block("heading", text=str(item.get("text") or "")))
        elif kind in {"bullets", "list", "items"}:
            source = item.get("items") or item.get("rows") or []
            items = [str(entry) for entry in source if str(entry).strip()] if isinstance(source, list) else []
            if items:
                blocks.append(Block("bullets", items=items))
        elif kind == "table":
            rows_raw = item.get("rows")
            rows = _table_rows(rows_raw)
            if rows:
                blocks.append(Block("table", rows=rows))
        else:
            text = str(item.get("text") or "")
            if text.strip():
                blocks.append(Block("text", text=text))
    return blocks


def _table_rows(raw: Any) -> list[list[str]]:
    """Строки таблицы, как бы модель их ни записала.

    Прежде брались только списки, а строки-словари ({"фамилия": …, "сумма": …})
    отбрасывались ВСЕ до одной: блок таблицы тихо исчезал, соседние оставались,
    и человек получал отчёт без главного — без самой таблицы, без единого следа
    в ответе. Словари — естественная форма для модели, и терять их нельзя.

    У словарей первая строка становится шапкой из ключей; порядок ключей берётся
    от первой строки, чтобы колонки не разъезжались.
    """
    if not isinstance(raw, list) or not raw:
        return []
    rows: list[list[str]] = []
    header: list[str] = []
    for item in raw:
        if isinstance(item, list):
            rows.append([str(cell) for cell in item])
        elif isinstance(item, dict):
            if not header:
                header = [str(key) for key in item]
                rows.append(header)
            rows.append([str(item.get(key, "")) for key in header])
        elif item is not None and str(item).strip():
            rows.append([str(item)])
    return rows


def spec_from_payload(title: str, subtitle: str, blocks: Any) -> ReportSpec:
    return ReportSpec(title=str(title or "Отчёт"), subtitle=str(subtitle or ""), blocks=_as_blocks(blocks))


def render(kind: str, spec: ReportSpec) -> bytes:
    kind = str(kind or "").strip().casefold()
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"Неизвестный формат: {kind!r}. Доступны: {', '.join(sorted(SUPPORTED_KINDS))}")
    if kind == "docx":
        return _render_docx(spec)
    if kind == "xlsx":
        return _render_xlsx(spec)
    if kind == "pdf":
        return _render_pdf(spec)
    return _render_png(spec)


def _render_docx(spec: ReportSpec) -> bytes:
    from docx import Document  # type: ignore[import-untyped]

    document = Document()
    document.add_heading(spec.title, level=0)
    if spec.subtitle:
        document.add_paragraph(spec.subtitle)
    for block in spec.blocks:
        if block.kind == "heading":
            document.add_heading(block.text, level=1)
        elif block.kind == "bullets":
            for item in block.items:
                document.add_paragraph(item, style="List Bullet")
        elif block.kind == "table" and block.rows:
            columns = max(len(row) for row in block.rows)
            table = document.add_table(rows=0, cols=columns)
            table.style = "Table Grid"
            for index, row in enumerate(block.rows):
                cells = table.add_row().cells
                for column, value in enumerate(row[:columns]):
                    cells[column].text = value
                    if index == 0:
                        for run in cells[column].paragraphs[0].runs:
                            run.bold = True
        else:
            document.add_paragraph(block.text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _render_xlsx(spec: ReportSpec) -> bytes:
    from openpyxl import Workbook  # type: ignore[import-untyped]
    from openpyxl.styles import Font  # type: ignore[import-untyped]

    book = Workbook()
    sheet = book.active
    # Имя листа пишет модель через заголовок отчёта, а Excel запрещает в нём
    # `\ / * ? : [ ]` — openpyxl на таком заголовке ПАДАЕТ, и человек вместо
    # файла получает сообщение об ошибке. «Отчёт: июль/август» — совершенно
    # обычная просьба.
    sheet.title = sheet_title_from_report_title(spec.title)
    _append_xlsx_literal_row(sheet, [spec.title])
    sheet["A1"].font = Font(bold=True, size=14)
    if spec.subtitle:
        _append_xlsx_literal_row(sheet, [spec.subtitle])
    sheet.append([])
    widest = len(spec.title)
    for block in spec.blocks:
        if block.kind == "heading":
            _append_xlsx_literal_row(sheet, [block.text])
            sheet.cell(row=sheet.max_row, column=1).font = Font(bold=True, size=12)
            widest = max(widest, len(block.text))
        elif block.kind == "bullets":
            for item in block.items:
                _append_xlsx_literal_row(sheet, [f"• {item}"])
                widest = max(widest, len(item) + 2)
        elif block.kind == "table":
            for index, row in enumerate(block.rows):
                _append_xlsx_literal_row(sheet, row)
                if index == 0:
                    for column in range(1, len(row) + 1):
                        sheet.cell(row=sheet.max_row, column=column).font = Font(bold=True)
                widest = max(widest, *(len(str(cell)) for cell in row)) if row else widest
        else:
            _append_xlsx_literal_row(sheet, [block.text])
            widest = max(widest, min(len(block.text), 120))
        sheet.append([])
    # Ширина по содержимому: колонка по умолчанию режет текст, и таблица выглядит
    # сломанной ещё до того, как её прочитали.
    sheet.column_dimensions["A"].width = min(max(widest + 2, 20), 120)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


#: Знаки, запрещённые Excel в имени листа.
_SHEET_FORBIDDEN = str.maketrans({char: " " for char in "\\/*?:[]"})


def _append_xlsx_literal_row(sheet: Any, values: list[str]) -> None:
    """Write report payload as literal cells, never as executable formulas.

    ``openpyxl`` interprets a string beginning with ``=`` as a formula.  Report
    content is model/data output, not workbook code; a formula such as
    ``WEBSERVICE`` could otherwise perform an outbound request when the person
    opens the attachment.  Setting the cell type after assignment preserves the
    visible text (without an apostrophe prefix) while serialising it as an
    inline string.  All strings are forced literal, so ``+``, ``-`` and ``@``
    are safe as well if the workbook is later converted to CSV.
    """

    sheet.append(list(values))
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def sheet_title_from_report_title(title: str) -> str:
    """Заголовок отчёта — в допустимое имя листа (не длиннее 31 знака)."""
    cleaned = " ".join(str(title or "").translate(_SHEET_FORBIDDEN).split())
    return cleaned[:31].strip() or "Отчёт"


def _pdf_font() -> tuple[str, str]:
    """Имена зарегистрированных шрифтов: обычный и полужирный."""
    from pathlib import Path

    from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
    from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]

    regular = next((path for path in _PDF_FONT_CANDIDATES if Path(path).exists()), "")
    bold = next((path for path in _PDF_BOLD_CANDIDATES if Path(path).exists()), regular)
    if not regular:
        # Ни одного подходящего шрифта — лучше честная латиница Helvetica, чем
        # чёрные квадраты вместо русского текста.
        return "Helvetica", "Helvetica-Bold"
    if "FridayText" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("FridayText", regular))
        pdfmetrics.registerFont(TTFont("FridayText-Bold", bold or regular))
    return "FridayText", "FridayText-Bold"


def _render_pdf(spec: ReportSpec) -> bytes:
    from reportlab.lib.enums import TA_LEFT  # type: ignore[import-untyped]
    from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
    from reportlab.lib.styles import ParagraphStyle  # type: ignore[import-untyped]
    from reportlab.lib.units import mm  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font, bold_font = _pdf_font()
    body = ParagraphStyle("body", fontName=font, fontSize=10.5, leading=15, alignment=TA_LEFT)
    heading = ParagraphStyle("heading", fontName=bold_font, fontSize=13, leading=18, spaceBefore=8)
    title_style = ParagraphStyle("title", fontName=bold_font, fontSize=18, leading=24, spaceAfter=6)

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=spec.title,
    )
    story: list[Any] = [Paragraph(_escape(spec.title), title_style)]
    if spec.subtitle:
        story.append(Paragraph(_escape(spec.subtitle), body))
    story.append(Spacer(1, 6))
    for block in spec.blocks:
        if block.kind == "heading":
            story.append(Paragraph(_escape(block.text), heading))
        elif block.kind == "bullets":
            story.append(
                ListFlowable(
                    [ListItem(Paragraph(_escape(item), body)) for item in block.items],
                    bulletType="bullet",
                    start="•",
                )
            )
        elif block.kind == "table" and block.rows:
            data = [[Paragraph(_escape(cell), body) for cell in row] for row in block.rows]
            table = Table(data, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("FONTNAME", (0, 0), (-1, 0), bold_font),
                        ("GRID", (0, 0), (-1, -1), 0.4, (0.6, 0.6, 0.6)),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("BACKGROUND", (0, 0), (-1, 0), (0.93, 0.93, 0.93)),
                    ]
                )
            )
            story.append(table)
        else:
            story.append(Paragraph(_escape(block.text), body))
        story.append(Spacer(1, 6))
    document.build(story)
    return buffer.getvalue()


def _escape(text: str) -> str:
    """`<`, `&` — разметка для reportlab, а в тексте это обычные знаки."""
    return (
        str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    )


@dataclass(frozen=True)
class _Line:
    """Строка картинки: текст либо ячейки таблицы с координатами колонок.

    Колонки нужны потому, что шрифт пропорциональный: склейка пробелами
    превращала таблицу в лесенку, по которой не прочитать, где чьё значение.
    """

    text: str | list[str]
    font: Any
    step: int
    columns: list[float] | None = None


def _render_png(spec: ReportSpec) -> bytes:
    """Картинка — тот же документ, нарисованный на холсте.

    Без внешних библиотек рисования: Pillow есть, а matplotlib нет, и тянуть его
    ради текстовой карточки не за что.
    """
    from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-untyped]

    def _font(size: int, bold: bool = False) -> Any:
        candidates = _PDF_BOLD_CANDIDATES if bold else _PDF_FONT_CANDIDATES
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    width, margin = 1200, 48
    lines: list[_Line] = [_Line(spec.title, _font(34, bold=True), 46)]
    if spec.subtitle:
        lines.append(_Line(spec.subtitle, _font(20), 30))
    lines.append(_Line("", _font(10), 12))
    body_font, head_font = _font(20), _font(24, bold=True)
    for block in spec.blocks:
        # Перенос нужен КАЖДОЙ ветке, а не только абзацам: длинный заголовок,
        # длинный пункт списка и широкая строка таблицы одинаково уезжали за
        # правый край картинки и обрывались на середине слова.
        if block.kind == "heading":
            lines.extend(_Line(chunk, head_font, 34) for chunk in _wrap(block.text, 64))
        elif block.kind == "bullets":
            for item in block.items:
                wrapped = _wrap(item, 74)
                lines.append(_Line(f"•  {wrapped[0]}", body_font, 28))
                lines.extend(_Line(f"   {chunk}", body_font, 28) for chunk in wrapped[1:])
        elif block.kind == "table":
            # Колонки — по координатам, а не через склейку пробелами: шрифт
            # пропорциональный, и «Тип Штук / Рапорты 42» превращалось в лесенку,
            # по которой не прочитать, где чьё значение. Ширина колонки — самая
            # широкая ячейка в ней, с отступом.
            rows = [[str(cell) for cell in row] for row in block.rows if row]
            if not rows:
                continue
            column_count = max(len(row) for row in rows)
            # Шапка меряется ЖИРНЫМ шрифтом, которым и рисуется: измерение
            # обычным давало колонку уже реального заголовка, и «Тип документа»
            # налезал на «Штук».
            widths = [
                max(
                    (
                        (head_font if row_index == 0 else body_font).getlength(row[index])
                        if index < len(row)
                        else 0.0
                    )
                    for row_index, row in enumerate(rows)
                )
                for index in range(column_count)
            ]
            offsets: list[float] = []
            running = 0.0
            for width_value in widths:
                offsets.append(running)
                running += width_value + 28
            for index, row in enumerate(rows):
                cells = [(row[column] if column < len(row) else "") for column in range(column_count)]
                lines.append(_Line(cells, head_font if index == 0 else body_font, 30, offsets))
        else:
            lines.extend(_Line(chunk, body_font, 28) for chunk in _wrap(block.text, 78))
        lines.append(_Line("", body_font, 10))

    # Картинка не может расти бесконечно. Замерено: 500 строк таблицы дают
    # 15164 пикселя и 1.1 МБ, 2000 строк — 60164 пикселя и 4.7 МБ. Telegram
    # такую не покажет, а человек просил «картинку со сводкой», а не файл,
    # который не открывается. Обрезаем — и ГОВОРИМ об этом прямо на картинке:
    # молчаливый обрез это ровно тот случай, который система уже чинила в
    # голосе и в разборе документов.
    # Место под саму оговорку резервируется заранее: иначе она выталкивает
    # картинку за собственный потолок.
    budget = _PNG_MAX_HEIGHT - margin * 2 - 40
    kept: list[_Line] = []
    used = 0
    for line in lines:
        if used + line.step > budget:
            break
        kept.append(line)
        used += line.step
    if len(kept) < len(lines):
        dropped = len(lines) - len(kept)
        kept.append(_Line("", body_font, 8))
        kept.append(
            _Line(
                f"…показано не всё: не поместилось строк — {dropped}. "
                "Попросите тот же отчёт в Word или Excel.",
                _font(18, bold=True),
                30,
            )
        )
        lines = kept
    else:
        lines = kept
    height = margin * 2 + sum(line.step for line in lines)
    image = Image.new("RGB", (width, max(height, 200)), "white")
    draw = ImageDraw.Draw(image)
    offset = margin
    for line in lines:
        if line.columns is not None and isinstance(line.text, list):
            for cell, column_offset in zip(line.text, line.columns, strict=False):
                if cell:
                    draw.text((margin + column_offset, offset), cell, font=line.font, fill=(24, 24, 24))
        elif isinstance(line.text, str) and line.text:
            draw.text((margin, offset), line.text, font=line.font, fill=(24, 24, 24))
        offset += line.step
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
