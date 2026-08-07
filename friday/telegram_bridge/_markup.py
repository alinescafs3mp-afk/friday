"""Разметка модели → разметка Telegram.

Заказ владельца 2026-08-02: «добавь markdown разметку в телеграмме, чтобы код
вылазил как код, чтобы форматирование текста модели смотрелось как задумано».
До этого `sendMessage` уходил без `parse_mode` вовсе, и человек читал сырые
звёздочки: `**срок**` приходило звёздочками, блок кода — сплошной простынёй без
моноширинного шрифта.

Выбран HTML, а не MarkdownV2. У Telegram в MarkdownV2 экранирования требуют
восемнадцать символов, включая точку, минус и скобку, — то есть почти любой
обычный русский текст со списком или ссылкой ломает разбор, и сообщение
отвергается целиком (400). В HTML спецсимволов ровно три: `&`, `<`, `>`.

Порядок работы важен: сначала из текста ВЫНИМАЮТСЯ куски кода (они не
размечаются), потом экранируется HTML, потом накладывается разметка, потом код
возвращается на место уже как `<pre>`/`<code>`. Иначе звёздочка внутри кода стала
бы жирным шрифтом, а `<` в коде — сломанным тегом.
"""

from __future__ import annotations

import html
import re

#: Блок кода с необязательным языком: ```python ... ```
_CODE_BLOCK = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.DOTALL)
#: Код в строке: `что-то`
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: Ссылка: [текст](адрес)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
#: Жирный: **текст** или __текст__
_BOLD = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__", re.DOTALL)
#: Курсив: *текст* или _текст_. Проверяются границы, иначе умножение и
#: подчёркивания в именах файлов превращались бы в разметку.
_ITALIC = re.compile(
    r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])"
)
#: Зачёркнутый: ~~текст~~
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~", re.DOTALL)
#: Заголовок: ## Текст. У Telegram заголовков нет — становится жирной строкой.
# Хвост описан как `[ \t]*`, а не `\s*`: `\s` включает перевод строки, и жадный
# хвост съедал пустую строку под заголовком — абзац слипался с ним.
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
#: Горизонтальная черта: --- или ***
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
#: Маркер списка в начале строки: «- », «* », «+ ». Telegram списков не знает,
#: поэтому маркер приводится к одному виду, а не выбрасывается.
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?=\S)", re.MULTILINE)

#: Строка таблицы Markdown: `| a | b |`. Разделитель шапки (`|---|---|`) отдельно.
_TABLE_ROW = re.compile(r"^[ \t]{0,3}\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(r"^[ \t]{0,3}\|[\s:|-]+\|[ \t]*$")

_PLACEHOLDER = "\x00code{}\x00"


def _table_to_monospace(text: str) -> str:
    """Таблицу Markdown — в моноширинный блок с выровненными колонками.

    У Telegram таблиц нет вовсе, и до этого они доезжали сырыми палками: строка
    `| срок | ответственный |` приходила ровно так, как написана. Читать это
    нельзя, а модель пишет таблицы сама, когда просят сравнить.

    Ответ — не выбросить разметку, а превратить её в то единственное, что
    мессенджер умеет ровно: блок `<pre>` с колонками, выровненными пробелами.
    Ширина колонки считается по самой длинной ячейке, поэтому столбцы стоят
    столбцами и на телефоне.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        if not _TABLE_ROW.match(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and _TABLE_ROW.match(lines[index]):
            block.append(lines[index])
            index += 1
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in block
            if not _TABLE_RULE.match(line)
        ]
        # Одна строка — это не таблица, а предложение с палками. Не трогаем.
        if len(rows) < 2:
            out.extend(block)
            continue
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        sizes = [max(len(row[column]) for row in rows) for column in range(width)]
        rendered = [
            "  ".join(cell.ljust(sizes[column]) for column, cell in enumerate(row)).rstrip() for row in rows
        ]
        out.append("```")
        out.extend(rendered)
        out.append("```")
    return "\n".join(out)


def to_telegram_html(text: str) -> str:
    """Разметить ответ так, как его покажет Telegram при `parse_mode=HTML`.

    Возвращает готовый к отправке HTML. Текст без разметки проходит насквозь,
    изменяясь только экранированием трёх спецсимволов.
    """
    source = str(text or "")
    if not source.strip():
        return ""

    # 0. Таблицы — ДО вынимания кода: они превращаются в блок кода, и дальше по
    # тексту это уже обычный блок, который вынется вместе с остальными.
    source = _table_to_monospace(source)

    # 1. Вынимаем код: внутри него разметки нет и быть не должно.
    stash: list[str] = []

    def _stash_block(match: re.Match[str]) -> str:
        # Перевод строки вынесен в переменную: обратный слэш внутри f-строки
        # появился только в Python 3.12, а проект держит совместимость с 3.11.
        body = match.group(2).strip("\n")
        stash.append(f"<pre><code>{html.escape(body)}</code></pre>")
        return _PLACEHOLDER.format(len(stash) - 1)

    def _stash_span(match: re.Match[str]) -> str:
        stash.append(f"<code>{html.escape(match.group(1))}</code>")
        return _PLACEHOLDER.format(len(stash) - 1)

    source = _CODE_BLOCK.sub(_stash_block, source)
    source = _CODE_SPAN.sub(_stash_span, source)

    # 2. Экранируем HTML — до наложения тегов, иначе экранируются они сами.
    source = html.escape(source, quote=False)

    # 3. Накладываем разметку. Ссылки первыми: их текст может быть жирным.
    source = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', source)
    source = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", source)
    source = _RULE.sub("—" * 12, source)
    source = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", source)
    source = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", source)
    source = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", source)
    source = _BULLET.sub(r"\1• ", source)

    # 4. Возвращаем код на место.
    for index, snippet in enumerate(stash):
        source = source.replace(_PLACEHOLDER.format(index), snippet)
    return source
