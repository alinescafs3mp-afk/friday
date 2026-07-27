"""Two ways an answer disappeared on its way to the user.

**A page with a sidebar could not be read at all.** `_extract_text_from_html`
removed unwanted elements while iterating a list of tags taken beforehand, and
`decompose()` empties the tag AND every descendant — so the next descendant in
that list had `attrs is None` and `tag.get(...)` raised. Any element matching the
drop list with a child inside it triggered it, which is every real page carrying
`<div class="sidebar">`. The exception surfaced as «страницу не удалось
прочитать», so a whole class of ordinary sites was simply unfetchable.

**An answer that mentioned JSON was destroyed as a protocol violation.** The
guard fired on any `{` followed anywhere by `"name":`, so a reply explaining a
config, quoting an API response, or citing the owner's own notes was classified
as a tool-protocol error, discarded whole and replaced with «Не удалось
безопасно завершить вызов инструмента» — losing the answer and the model rounds
that produced it, for writing about JSON.
"""

from __future__ import annotations

import pytest

from jericho.agent_runtime.tool_protocol import classify_tool_turn
from jericho.web_surfer import WebSurfer

PAGE = """<html><head><title>Инструкция</title></head><body>
<div class="sidebar"><ul><li><a href="#">меню</a></li><li>ещё пункт</li></ul></div>
<nav id="main-nav"><span>навигация</span><a href="/x">ссылка</a></nav>
<main><p>Настоящий текст страницы.</p><p>Второй абзац с деталями.</p></main>
<div class="cookie-consent"><button><b>Принять</b></button></div>
<footer id="page-footer"><p>© 2026</p></footer></body></html>"""


def test_a_page_with_a_sidebar_is_still_readable():
    text, title = WebSurfer._extract_text_from_html(PAGE)  # noqa: SLF001
    assert title == "Инструкция"
    assert "Настоящий текст страницы." in text
    assert "Второй абзац с деталями." in text
    # …and the chrome really was removed, so the fix is not "stop dropping".
    assert "меню" not in text
    assert "Принять" not in text


@pytest.mark.parametrize(
    "answer",
    [
        'Конфигурация выглядит так: {"name": "atlas", "port": 8080} — порт можно менять.',
        'В ответе API приходит {"arguments": ["-v"], "input": "file.txt"}, это нормально.',
        'Вот пример из ваших заметок:\n```json\n{"name": "proxy", "parameters": {"tls": true}}\n```\nОн валиден.',
        "Позвоните им: Call: +7 495 123-45-67 — там ответят.",
    ],
)
def test_prose_about_json_is_an_answer(answer):
    turn = classify_tool_turn(answer)
    assert turn.kind == "answer", f"discarded as {turn.kind}"
    assert turn.text == answer.strip()


@pytest.mark.parametrize(
    "payload",
    [
        '{"name": "memory_search", "arguments": {"query": "дежурства"}}',
        '{"tool_calls": [{"function": {"name": "kg_stats", "arguments": "{}"}}]}',
        '```json\n{"tool": "web_fetch", "arguments": {"url": "https://example.org"}}\n```',
    ],
)
def test_a_real_tool_envelope_is_still_recognised(payload):
    """The guard exists for a reason and must keep working."""
    turn = classify_tool_turn(payload)
    assert turn.kind in {"tool", "protocol_error"}, "a control payload reached the user as an answer"
    assert turn.text == ""


def test_a_bare_call_line_is_still_a_protocol_error():
    turn = classify_tool_turn("Call: memory_search")
    assert turn.kind == "protocol_error"
