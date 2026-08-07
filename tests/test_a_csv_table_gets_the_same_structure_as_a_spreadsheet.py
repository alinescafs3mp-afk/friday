"""У CSV та же структура таблицы, что у `.xlsx` — и тот же точный путь по ней.

Структура доезжала до модели только у `.docx` и `.xlsx`: CSV оставался текстом, и
точный путь по таблице — «сколько всего позиций», «перечисли всех» — для него не
работал вовсе. При этом CSV это ровно таблица, и терять её структуру только
из-за расширения файла нечестно.

Строится ТЕМ ЖЕ построителем, что у `.xlsx`, на синтетической книге. Не своим
облегчённым разбором: второй построитель индекса разошёлся бы с первым на первой
же правке, а индекс проверяется валидатором, который отбрасывает несогласованное
МОЛЧА — вместе с точным путём, ради которого он и заведён.

Проба сравнивает CSV с ТЕМ ЖЕ содержимым в `.xlsx`. Это не украшение: первая
редакция правки строила индекс из уже склеенного текста, получала одну колонку на
строку — и индекс выходил валидным, полным и совершенно бесполезным: ни одной
записи, ни одного кандидата. Сравнение с настоящей книгой поймало это сразу,
проверка «индекс есть» не поймала бы никогда.
"""

from __future__ import annotations

import io

import pytest

from friday.documents import DocumentExtractor, validate_office_structure_index

Workbook = pytest.importorskip("openpyxl").Workbook

ROWS = [
    ["ФИО", "Роль"],
    ["Иванов Пётр", "инженер"],
    ["Петров Сергей", "мастер"],
    ["Сидоров Илья", "техник"],
]


def _extractor() -> DocumentExtractor:
    return DocumentExtractor(secret_values=())


def _as_xlsx(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Лист"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _as_csv(rows: list[list[str]]) -> bytes:
    return "\n".join(";".join(row) for row in rows).encode()


def test_a_csv_gets_a_valid_structure_index() -> None:
    result = _extractor().extract(_as_csv(ROWS), "штат.csv", "text/csv")

    assert result.office_structure_index is not None, "у CSV по-прежнему нет структуры"
    assert validate_office_structure_index(result.office_structure_index, result.text) is not None
    assert result.office_structure_index["complete"] is True


def test_the_same_table_gives_the_same_records_in_both_formats() -> None:
    """Главная проба: не «индекс есть», а «индекс тот же».

    Валидный, полный и пустой индекс — ровно тот случай, который прошёл бы
    проверку «структура построилась» и не дал бы ни одного точного ответа.
    """
    extractor = _extractor()
    from_csv = extractor.extract(_as_csv(ROWS), "штат.csv", "text/csv").office_structure_index or {}
    from_xlsx = extractor.extract(_as_xlsx(ROWS), "штат.xlsx", "").office_structure_index or {}

    assert len(from_csv.get("record_sets") or []) == len(from_xlsx.get("record_sets") or [])
    assert len(from_csv.get("candidate_refs") or []) == len(from_xlsx.get("candidate_refs") or [])
    assert len(from_csv["record_sets"]) == 1, from_csv["record_sets"]
    assert len(from_csv["candidate_refs"]) == 3, from_csv["candidate_refs"]

    csv_roles = [row["role"] for row in from_csv["blocks"][0]["rows"]]
    xlsx_roles = [row["role"] for row in from_xlsx["blocks"][0]["rows"]]
    assert csv_roles == xlsx_roles == ["header", "record", "record", "record"]


def test_the_columns_are_columns_and_not_one_glued_cell() -> None:
    """Опора предыдущей пробы: ячеек должно быть по числу колонок.

    Именно здесь ломалась первая редакция — восемь ячеек превращались в четыре.
    """
    index = _extractor().extract(_as_csv(ROWS), "штат.csv", "text/csv").office_structure_index or {}

    assert index["coverage"]["cells_seen"] == len(ROWS) * 2, index["coverage"]


def test_a_huge_table_says_it_skipped_the_structure() -> None:
    """Потолок есть, и молчать о нём нельзя.

    Индекс строится на настоящей книге, и её сборка стоит времени и памяти
    линейно по строкам. Выше потолка CSV остаётся ровным текстом — а человек,
    спросивший «сколько всего», должен знать, что ответ будет обычный, а не
    точный.
    """
    extractor = _extractor()
    rows = [["ФИО", "Роль"]] + [[f"Человек {index}", "роль"] for index in range(6000)]
    result = extractor.extract(_as_csv(rows), "большой.csv", "text/csv")

    assert result.office_structure_index is None
    assert result.metadata.get("office_structure_skipped") == "too_many_rows", result.metadata


def test_a_truncated_table_gets_no_index_at_all() -> None:
    """Индекс по обрезанной таблице утверждал бы о ней то, чего в ней нет."""
    extractor = DocumentExtractor(secret_values=(), max_text_chars=10_000)
    rows = [["ФИО", "Роль"]] + [[f"Человек {index}", "роль" * 40] for index in range(400)]
    result = extractor.extract(_as_csv(rows), "длинный.csv", "text/csv")

    assert result.metadata.get("rows_truncated") is True, result.metadata
    assert result.office_structure_index is None
