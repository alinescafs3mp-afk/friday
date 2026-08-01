"""«Сделай отчёт» заканчивается файлом, а не текстом в чате.

Требование владельца 2026-08-01: Пятница должна уметь составлять Word, Excel, PDF
и картинки — «сделай мне отчёт с выводом по тем-то документам» и подобное.

Проверяется не «функция вернула байты», а то, что файл ОТКРЫВАЕТСЯ штатной
библиотекой и русский текст в нём читается: PDF со встроенными шрифтами
reportlab молча превращает кириллицу в чёрные квадраты, и такой отчёт выглядит
готовым ровно до момента, когда его откроют.
"""

from __future__ import annotations

import io

import pytest

from friday.reports import SUPPORTED_KINDS, render, spec_from_payload

BLOCKS = [
    {"kind": "heading", "text": "Основные выводы"},
    {"kind": "text", "text": "По документам архива за июль 2026 года видно следующее."},
    {"kind": "bullets", "items": ["Штатное расписание обновлено 30 июля", "Приказ № 214 от 3 мая"]},
    {
        "kind": "table",
        "rows": [["Фамилия", "Месяц", "Начислено"], ["Бутко", "октябрь 2025", "87 450"]],
    },
]


def _spec():
    return spec_from_payload("Отчёт по документам", "Подготовлено Пятницей", BLOCKS)


@pytest.mark.parametrize("kind", sorted(SUPPORTED_KINDS))
def test_every_format_produces_a_file(kind):
    payload = render(kind, _spec())
    assert len(payload) > 1000, f"{kind}: файл подозрительно мал"


def test_the_word_file_opens_and_keeps_the_russian_text():
    import docx

    document = docx.Document(io.BytesIO(render("docx", _spec())))
    text = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    assert "Отчёт по документам" in text
    assert "Основные выводы" in text
    assert any("Штатное расписание" in line for line in text), "список не попал в документ"
    assert [cell.text for cell in document.tables[0].rows[0].cells] == [
        "Фамилия",
        "Месяц",
        "Начислено",
    ]


def test_the_excel_file_opens_and_keeps_the_table():
    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(render("xlsx", _spec())))
    sheet = book.active
    values = [str(sheet.cell(row=row, column=1).value) for row in range(1, sheet.max_row + 1)]
    assert "Отчёт по документам" in values
    assert "Фамилия" in values
    assert any("Бутко" in value for value in values)


def test_the_pdf_keeps_cyrillic_readable():
    """Мутация: убрать регистрацию DejaVu (вернуть Helvetica) — тест краснеет.

    Встроенные шрифты reportlab кириллицу не знают: текст верстается, файл
    открывается, а вместо букв — квадраты. Проверяется извлечённый текст, а не
    факт создания файла.
    """
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(render("pdf", _spec())))
    text = " ".join(reader.pages[0].extract_text().split())
    assert "Отчёт по документам" in text
    assert "Основные выводы" in text
    assert "Штатное расписание обновлено 30 июля" in text


def test_the_picture_is_a_real_image_with_room_for_the_text():
    from PIL import Image

    image = Image.open(io.BytesIO(render("png", _spec())))
    assert image.format == "PNG"
    assert image.width >= 800
    # Высота считается по содержимому: карточка, обрезанная на середине списка,
    # выглядит как сбой вёрстки.
    assert image.height > 300


def test_an_unknown_format_is_refused_by_name():
    with pytest.raises(ValueError, match="Неизвестный формат"):
        render("djvu", _spec())


def test_a_broken_block_does_not_break_the_report():
    """Модель ошибётся в форме рано или поздно.

    Падать из-за лишнего ключа посреди отчёта хуже, чем пропустить непонятный
    кусок: человек просил документ, а не сообщение об ошибке разбора.
    """
    spec = spec_from_payload(
        "Отчёт",
        "",
        [
            {"kind": "text", "text": "Первый абзац."},
            "строка вместо объекта",
            {"kind": "невиданное", "text": "Станет обычным абзацем."},
            {"kind": "table", "rows": "не список"},
            {"kind": "bullets", "items": []},
            {"kind": "text", "text": "Последний абзац."},
        ],
    )
    assert [block.kind for block in spec.blocks] == ["text", "text", "text"]
    assert len(render("docx", spec)) > 1000


def test_the_filename_cannot_escape_its_folder():
    """Заголовок пишет модель, а он становится именем файла."""
    from friday.execution_kernel import _safe_filename

    assert _safe_filename("../../etc/passwd", "pdf") == "etc passwd.pdf"
    assert _safe_filename("Отчёт: июль/август", "docx") == "Отчёт июль август.docx"
    assert _safe_filename("", "xlsx") == "Отчёт.xlsx"
    assert "/" not in _safe_filename("a/b/c", "png")


@pytest.mark.anyio
async def test_the_tool_returns_the_file_as_an_attachment(settings, storage):
    """Мутация: убрать `_attachment` из ответа — тест краснеет.

    Файл, который никуда не уходит, — это не выполненная просьба.
    """
    from friday.execution_kernel import ExecutionKernel
    from friday.permissions import ActorContext, AuthorizationService

    storage.ensure_user("alice", preset_key="admin")
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, None, None, None)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute(
        "make_file",
        {"kind": "docx", "title": "Отчёт по документам", "blocks": BLOCKS},
        actor=actor,
    )

    assert result.success, result.error
    assert result.data["created"] is True
    assert result.data["filename"] == "Отчёт по документам.docx"
    assert result.attachment, "файл собран, но человеку не отправляется"
    assert result.attachment["kind"] == "document"
    assert result.attachment["mime_type"].endswith("wordprocessingml.document")
    assert result.attachment["content_base64"]


@pytest.mark.anyio
async def test_an_empty_report_is_refused_rather_than_sent_blank(settings, storage):
    from friday.execution_kernel import ExecutionKernel
    from friday.permissions import ActorContext, AuthorizationService

    storage.ensure_user("alice", preset_key="admin")
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, None, None, None)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute(
        "make_file", {"kind": "pdf", "title": "Пустой", "blocks": []}, actor=actor
    )
    assert result.data["created"] is False
    assert result.attachment is None
