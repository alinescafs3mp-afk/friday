"""Вложение «не запоминай» говорит, чего в нём не увидели.

Осмотр такого файла (`inspect_file_transient`) вычисляет полноту разбора — успех,
ошибку, обрыв по времени, обрезку текста, число прочитанных страниц — и до
2026-08-04 отдавал наружу четыре ключа из десяти. Модель получала выдержку и
ничего о её полноте: первая страница четырёхсотстраничного тома выглядела как
весь том.

Здесь это дороже, чем в обычном приёме. Материал не сохраняется по прямой просьбе
человека: ни в Raw Objects, ни в Inbox, ни в графе его не будет. Переспросить по
нему потом нечего — другого случая сказать правду не представится.

Худший случай — не обрезка, а провал разбора. Пустая выдержка не попадала в
промпт вовсе (`if not excerpt: continue`), то есть вложение исчезало молча, и
модель отвечала так, будто файла не присылали. Человек при этом видел, что
отправил его, и получал ответ «по документу», написанный без документа.

Формулировки — факты в прошедшем времени. Служебная строка, написанная как
поручение самой себе, однажды уехала владельцу целиком: модель не отличает данные
от инструкции.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _what_is_missing_from_this_attachment


def test_a_failed_parse_is_named():
    """Мутация: вернуть `return ""` при провале разбора — тест краснеет."""
    line = _what_is_missing_from_this_attachment(
        {"extraction_success": False, "extraction_error": "PdfReadError: EOF marker not found"}
    )
    assert "не удалось" in line
    assert "EOF" in line, "причина потеряна, а она и есть полезная часть"


def test_a_thick_volume_reports_both_numbers():
    """Одно число без второго не отвечает на вопрос «много ли потеряно»."""
    line = _what_is_missing_from_this_attachment(
        {
            "extraction_success": True,
            "parse_pages_truncated": True,
            "parse_pages_read": 250,
            "parse_total_pages": 400,
        }
    )
    assert "250" in line and "400" in line


def test_three_different_cuts_are_not_confused():
    """Обрыв по времени, обрезка текста и потолок страниц — разные вещи."""
    line = _what_is_missing_from_this_attachment(
        {
            "extraction_success": True,
            "parse_deadline_reached": True,
            "text_truncated": True,
        }
    )
    assert "по времени" in line
    assert "начало текста" in line


def test_a_whole_document_says_nothing():
    """Предупреждение не по делу обесценивает те, что по делу."""
    assert (
        _what_is_missing_from_this_attachment(
            {"extraction_success": True, "parse_pages_read": 3, "parse_total_pages": 3}
        )
        == ""
    )


def test_the_line_is_a_fact_not_an_instruction():
    """Служебная строка не должна читаться как поручение модели."""
    line = _what_is_missing_from_this_attachment(
        {"extraction_success": False, "extraction_error": "сломан"}
    )
    for imperative in ("скажи", "сообщи", "не обещай", "предупреди"):
        assert imperative not in line.lower(), f"строка написана как приказ: {line!r}"


@pytest.mark.asyncio
async def test_an_unreadable_attachment_still_reaches_the_model(settings, storage):
    """Потребитель — МОДЕЛЬ: проверяется собранный промпт, а не словарь.

    Мутация: вернуть `if not excerpt: continue` в `agent_runtime` — тест
    краснеет, потому что о вложении в промпте не останется ни слова.
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    runtime = AgentRuntime(settings, storage)
    storage.ensure_user("alice")
    context = AgentContext(conversation_id="conv-1", user_id="alice")

    messages = runtime._build_initial_messages(  # noqa: SLF001
        context,
        "что в файле?",
        [
            {
                "filename": "акт.pdf",
                "transient": True,
                "transient_text": "",
                "extraction_success": False,
                "extraction_error": "PdfReadError",
            }
        ],
        tool_enabled=False,
    )

    whole = "\n".join(str(item.get("content") or "") for item in messages)
    assert "акт.pdf" in whole, "вложение исчезло из разговора молча"
    assert "не удалось" in whole
