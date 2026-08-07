"""CSV из русского Excel — таблица, а не строка текста.

Русский Excel сохраняет CSV с ТОЧКОЙ С ЗАПЯТОЙ: это его локальное умолчание, и
такой файл — обычный рабочий документ, а не экзотика. Разделитель при этом был
зашит запятой по расширению, и каждая строка такого файла становилась ОДНОЙ
ячейкой: смета на сорок позиций уходила модели сплошным текстом, а вид
«ячейка | ячейка», на который рассчитан и точный путь по таблицам, не появлялся
вовсе.

Выбор делает не `csv.Sniffer`: тот эвристичен, бросает исключение на коротких
файлах и объяснить свой выбор человеку не может. Правило здесь простое и
проверяемое — побеждает знак, дающий одинаковое число колонок в наибольшем числе
первых строк; ничья остаётся за умолчанием расширения. Выбранный разделитель
записывается в метаданные: догадка, о которой не сказано, — это не догадка, а
утверждение.
"""

from __future__ import annotations

from friday.documents import DocumentExtractor

RUSSIAN = "ФИО;Роль;Оклад\nИванов И.И.;инженер;120000\nПетров П.П.;мастер;95000\n"
COMMA = "ФИО,Роль,Оклад\nИванов,инженер,120000\nПетров,мастер,95000\n"
TABBED = "ФИО\tРоль\nИванов\tинженер\nПетров\tмастер\n"


def _extract(text: str, name: str = "смета.csv"):
    return DocumentExtractor(secret_values=()).extract(text.encode(), name, "text/csv")


def test_a_semicolon_table_becomes_columns() -> None:
    result = _extract(RUSSIAN)

    assert result.metadata.get("delimiter") == ";", result.metadata
    assert "ФИО | Роль | Оклад" in result.text, result.text
    assert "Иванов И.И. | инженер | 120000" in result.text


def test_a_comma_table_still_works() -> None:
    """Вторая половина: прежнее поведение не должно сломаться."""
    result = _extract(COMMA)

    assert result.metadata.get("delimiter") == ",", result.metadata
    assert "ФИО | Роль | Оклад" in result.text


def test_a_tab_table_is_recognised_by_its_text() -> None:
    """Даже если расширение соврало: `.csv` с табуляцией внутри — тоже таблица."""
    result = _extract(TABBED, name="выгрузка.csv")

    assert result.metadata.get("delimiter") == "\t", result.metadata
    assert "ФИО | Роль" in result.text


def test_the_chosen_separator_is_written_down() -> None:
    """Догадка, о которой не сказано, — это не догадка, а утверждение."""
    assert "delimiter" in _extract(RUSSIAN).metadata


def test_a_ragged_file_falls_back_to_the_extension() -> None:
    """Не таблица — не выдумывать колонки: число разделителей скачет по строкам."""
    ragged = "просто строка\nещё одна; с точкой с запятой\nи третья\n"
    result = _extract(ragged)

    assert result.metadata.get("delimiter") == ",", result.metadata


def test_one_stray_line_does_not_decide_for_the_whole_table() -> None:
    """Над шапкой часто стоит название отчёта — и оно не должно решать за таблицу.

    Здесь у первой строки две точки с запятой, а СОГЛАСОВАННЫ по всем остальным
    строкам запятые. Без правила «решает самое частое значение, и согласных строк
    должно быть минимум две» побеждала бы случайная строка заголовка, и таблица
    снова расклеивалась бы в текст.
    """
    text = "Смета; на март; черновик\nИванов,инженер,120000\nПетров,мастер,95000\n"
    result = _extract(text)

    assert result.metadata.get("delimiter") == ",", result.metadata
    assert "Иванов | инженер | 120000" in result.text, result.text


def test_a_single_comma_in_prose_is_not_a_table() -> None:
    """Одна строка согласна сама с собой всегда — это не признак таблицы."""
    text = "Заметка про смету, и ничего больше\n"
    result = _extract(text, name="заметка.tsv")

    assert result.metadata.get("delimiter") == "\t", result.metadata
