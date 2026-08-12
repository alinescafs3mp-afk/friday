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
from collections.abc import Callable

#: Блок кода с необязательным языком: ```python ... ```
_CODE_BLOCK = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.DOTALL)
#: Код в строке: `что-то`
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: Начало безопасной ссылки. Конец нельзя искать обычным ``[^)]``: скобки
#: допустимы в URL и часто встречаются в ссылках на wiki и в query string.
_LINK_START = re.compile(r"\[([^\]\n]+)\]\((https?://)")
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
# Some models occasionally flatten several Markdown bullets onto one physical
# line: ``* first. *   second``.  Telegram quite correctly treats only the
# first marker as a list item, leaving the later ``*`` visible.  Three or more
# spaces after the inline star is the shape emitted by that failure and keeps
# this distinct from multiplication or ordinary prose.
_INLINE_BULLET = re.compile(r"[ \t]+\*[ \t]{2,}(?=\S)")
_FLATTENED_LIST_LABEL = re.compile(
    r"^(?:"
    r"\*\*[^*\n]{1,80}[:;—–-]\*\*|\*\*[^*\n]{1,80}\*\*[ \t]*[:;—–-]|"
    r"__[^_\n]{1,80}[:;—–-]__|__[^_\n]{1,80}__[ \t]*[:;—–-]"
    r")(?:[ \t]+|$)"
)
# Markdown quote markers have to become Telegram's supported blockquote tag;
# leaving ``>`` as escaped prose preserves bytes but loses the requested shape.
_ESCAPED_QUOTE = re.compile(r"^&gt;[ \t]?(.*)$", re.MULTILINE)

#: Строка таблицы Markdown: `| a | b |`. Разделитель шапки (`|---|---|`) отдельно.
_TABLE_ROW = re.compile(r"^[ \t]{0,3}\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(r"^[ \t]{0,3}\|[\s:|-]+\|[ \t]*$")

_PLACEHOLDER = "\x00code{}\x00"
_INLINE_PLACEHOLDER = "\x00inline{}\x00"
_LINK_PLACEHOLDER = "\x00link{}\x00"
_STASHED_CODE = re.compile(r"\x00code[0-9]+\x00")


def _restore_flattened_bullets(source: str) -> str:
    """Put flattened sibling bullets back on separate Markdown lines.

    Lines which already begin with a real Markdown bullet are unambiguous.  A
    prose preamble is repaired only when repeated siblings also look like list
    prose/labels.  This is deliberately narrower than replacing every
    `` *   `` sequence: code has already been stashed, but arithmetic and
    literal prose must still remain byte-for-byte text.
    """

    restored: list[str] = []
    for line in source.split("\n"):
        marker = _BULLET.match(line)
        body = line[marker.end() :] if marker is not None else ""
        if marker is not None and _INLINE_BULLET.search(body) is not None:
            indent = marker.group(1)
            # The leading marker itself has the same whitespace shape. Split
            # only the body after it, otherwise the first "sibling" is an
            # empty string and becomes a spurious leading blank line.
            siblings = _INLINE_BULLET.split(body)
            restored.append(f"{line[: marker.end()]}{siblings[0]}")
            restored.extend(f"{indent}* {sibling}" for sibling in siblings[1:])
            continue

        inline_markers = list(_INLINE_BULLET.finditer(line))
        if len(inline_markers) < 2:
            restored.append(line)
            continue

        # Models also flatten a list after a prose/Markdown preamble:
        # ``**Сводка:** *   one *   two``.  Repair only a repeated list whose
        # preamble visibly closes with punctuation or balanced emphasis.  The
        # repeat threshold keeps arithmetic such as ``5 *   3`` literal.
        preamble = line[: inline_markers[0].start()].rstrip()
        stripped = preamble.strip()
        punctuation_closed = stripped.endswith((".", ":", ";", "!", "?", "…"))
        emphasis_closed = bool(
            stripped.endswith("**")
            and stripped.count("**") >= 2
            and stripped.count("**") % 2 == 0
            or stripped.endswith("__")
            and stripped.count("__") >= 2
            and stripped.count("__") % 2 == 0
        )
        if not stripped or not (punctuation_closed or emphasis_closed):
            restored.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        siblings = _INLINE_BULLET.split(line)
        item_bodies = [sibling.strip() for sibling in siblings[1:]]
        has_markdown_label = any(_FLATTENED_LIST_LABEL.match(body) is not None for body in item_bodies)
        has_prose_item = any(
            len(re.findall(r"[^\W\d_]+", body, flags=re.UNICODE)) >= 2
            and body.rstrip().endswith((".", ":", ";", "!", "?", "…"))
            for body in item_bodies
        )
        # Balanced emphasis by itself is not list evidence: ``**5** * 3 * 2``
        # is an ordinary product.  A punctuation-led preamble may introduce
        # ordinary sentence items; an emphasis-led one needs an explicit
        # Markdown label unless its items are themselves closed prose.
        if not has_markdown_label and not (punctuation_closed and has_prose_item):
            restored.append(line)
            continue
        restored.append(siblings[0].rstrip())
        restored.extend(f"{indent}* {sibling}" for sibling in siblings[1:])
    return "\n".join(restored)


def _replace_balanced_links(source: str, renderer: Callable[[str, str], str]) -> str:
    """Replace safe Markdown links while balancing parentheses in the URL.

    Malformed, whitespace-containing and unclosed destinations remain literal.
    That fail-closed result is safer than emitting an ``href`` which points to
    only a truncated prefix of the address.
    """

    rendered: list[str] = []
    emitted_until = 0
    search_from = 0
    while match := _LINK_START.search(source, search_from):
        depth = 0
        closing: int | None = None
        invalid = False
        for index in range(match.end(2), len(source)):
            character = source[index]
            if character.isspace():
                invalid = True
                break
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    closing = index
                    break
                depth -= 1
        if invalid or closing is None or closing == match.end(2):
            search_from = match.start() + 1
            continue
        rendered.append(source[emitted_until : match.start()])
        rendered.append(renderer(match.group(1), source[match.start(2) : closing]))
        emitted_until = closing + 1
        search_from = emitted_until
    rendered.append(source[emitted_until:])
    return "".join(rendered)


def _table_to_monospace(text: str, *, inline_literals: tuple[str, ...] = ()) -> str:
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
        rows = []
        for line in block:
            if _TABLE_RULE.match(line):
                continue
            row = [cell.strip() for cell in line.strip().strip("|").split("|")]
            for column, cell in enumerate(row):
                for literal_index, literal in enumerate(inline_literals):
                    cell = cell.replace(_INLINE_PLACEHOLDER.format(literal_index), literal)
                row[column] = cell
            rows.append(row)
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

    # 0. Сначала вынимаем исходный код: похожая на таблицу строка внутри fenced
    # или inline code остаётся буквальным кодом и не проходит table renderer.
    stash: list[str] = []
    inline_stash: list[str] = []

    def _stash_block(match: re.Match[str]) -> str:
        # Перевод строки вынесен в переменную: обратный слэш внутри f-строки
        # появился только в Python 3.12, а проект держит совместимость с 3.11.
        body = match.group(2).strip("\n")
        for index, literal in enumerate(inline_stash):
            body = body.replace(_INLINE_PLACEHOLDER.format(index), literal)
        stash.append(f"<pre><code>{html.escape(body)}</code></pre>")
        return _PLACEHOLDER.format(len(stash) - 1)

    def _stash_span(match: re.Match[str]) -> str:
        inline_stash.append(match.group(1))
        return _INLINE_PLACEHOLDER.format(len(inline_stash) - 1)

    source = _CODE_BLOCK.sub(_stash_block, source)
    source = _CODE_SPAN.sub(_stash_span, source)

    # A flattened model list is still Markdown intent. Restore its physical
    # lines before the ordinary bullet renderer turns the markers into ``•``.
    source = _restore_flattened_bullets(source)

    # 1. Таблицы вне исходного кода превращаются в fenced blocks. Вынимаем и
    # эти новые блоки перед HTML/Markdown-преобразованиями ниже.
    source = _table_to_monospace(source, inline_literals=tuple(inline_stash))
    source = _CODE_BLOCK.sub(_stash_block, source)
    for index, literal in enumerate(inline_stash):
        placeholder = _INLINE_PLACEHOLDER.format(index)
        if placeholder not in source:
            continue
        stash.append(f"<code>{html.escape(literal)}</code>")
        source = source.replace(placeholder, _PLACEHOLDER.format(len(stash) - 1))

    # 2. Экранируем HTML — до наложения тегов, иначе экранируются они сами.
    source = html.escape(source, quote=False)

    # 3. Накладываем разметку. Ссылку вынимаем целиком до остальных regex:
    # Markdown-символы допустимы в URL как обычные байты и не должны превращать
    # часть ``href`` в ``<b>``/``<i>``/``<s>``. Подпись при этом сохраняет
    # поддерживаемую inline-разметку.
    links: list[str] = []

    def _stash_link(label: str, destination: str) -> str:
        label = _BOLD.sub(lambda item: f"<b>{item.group(1) or item.group(2)}</b>", label)
        label = _STRIKE.sub(lambda item: f"<s>{item.group(1)}</s>", label)
        label = _ITALIC.sub(lambda item: f"<i>{item.group(1) or item.group(2)}</i>", label)
        # Inline/fenced code is stashed before links are parsed.  A placeholder
        # inside a destination would otherwise be copied into ``href`` and then
        # expanded into a real ``<code>``/``<pre>`` tag during the final restore.
        # Keep that entire Markdown link literal instead.  Code in its label is
        # still restored normally, but no generated HTML can ever enter an
        # attribute value through a code placeholder.
        if _STASHED_CODE.search(destination):
            return f"[{label}]({destination})"
        href = html.escape(html.unescape(destination), quote=True)
        links.append(f'<a href="{href}">{label}</a>')
        return _LINK_PLACEHOLDER.format(len(links) - 1)

    source = _replace_balanced_links(source, _stash_link)
    source = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", source)
    source = _RULE.sub("—" * 12, source)
    source = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", source)
    source = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", source)
    source = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", source)
    source = _BULLET.sub(r"\1• ", source)
    source = _ESCAPED_QUOTE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", source)

    # 4. Сначала возвращаем ссылки, затем код: code-placeholder может находиться
    # внутри подписи ссылки и должен раскрыться уже после её возврата.
    for index, snippet in enumerate(links):
        source = source.replace(_LINK_PLACEHOLDER.format(index), snippet)
    for index, snippet in enumerate(stash):
        source = source.replace(_PLACEHOLDER.format(index), snippet)
    return source
