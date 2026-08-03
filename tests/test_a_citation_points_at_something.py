"""Метка `[K1]` законна, только если источник №1 действительно подан.

Найдено на живом диалоге с новым человеком 2026-08-03. В одном из ответов стояли
`[K1]`, `[K2]`, `[K3]` — при НУЛЕ найденных документов и нуле поданных
источников. Режим ответа был «общий разговор»: Пятница отвечала из головы и
сослалась на три несуществующие записи архива.

Прежняя защита вырезала только НЕномерные метки (`[K_source]`, `[K-ref]`) — по
шаблону `(?![0-9])`, — а номерную считала настоящей по одному её виду.

Это хуже служебного мусора. `[K_source]` человек прочтёт как сбой системы, а
`[K1]` выглядит как ссылка на его собственный документ: ответ из головы подаётся
как ответ по архиву, и проверить это человек может, только пойдя искать документ,
которого нет.

Проверено на том же диалоге: правка вычищает три метки из одного ответа и не
трогает законные ссылки в трёх остальных.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import _strip_invented_citations


def test_a_label_without_a_source_is_removed() -> None:
    """Мутация: не смотреть на список источников — метка снова доедет."""
    assert "[K1]" not in _strip_invented_citations("Факт [K1] и всё.", {})
    assert "[K2]" not in _strip_invented_citations("Факт [K2].", set())


def test_a_label_with_a_source_stays() -> None:
    """Настоящую ссылку вырезать нельзя: человек по ней открывает документ."""
    kept = _strip_invented_citations("Из [K1] следует, из [K2] тоже.", {"1", "2"})
    assert "[K1]" in kept and "[K2]" in kept


def test_only_the_dangling_one_goes() -> None:
    """Смешанный случай — ровно тот, что был в живом ответе."""
    cleaned = _strip_invented_citations("Из [K1] известно, а [K7] нет.", {"1"})
    assert "[K1]" in cleaned
    assert "[K7]" not in cleaned


def test_without_a_list_numbered_labels_are_left_alone() -> None:
    """Не зная списка, вырезать законные ссылки хуже, чем оставить лишнюю.

    Старые вызовы без второго параметра сохраняют прежнее поведение.
    """
    assert "[K1]" in _strip_invented_citations("Факт [K1].")


def test_the_non_numbered_junk_still_goes() -> None:
    """Прежний дефект не должен вернуться: `[K_source]` уходит в любом случае."""
    assert "K_source" not in _strip_invented_citations("Факт [K_source].")
    assert "K_source" not in _strip_invented_citations("Факт [K_source].", {"1"})


@pytest.mark.parametrize("text", ["[k1]", "[ K1 ]", "[K 1]"])
def test_spacing_and_case_do_not_hide_it(text: str) -> None:
    """Модель пишет метку по-разному; защита не должна зависеть от пробела."""
    assert "1]" not in _strip_invented_citations(f"Факт {text} и всё.", {})


def test_the_sentence_survives_the_removal() -> None:
    """После вырезания не должно оставаться рваной пунктуации и двойных пробелов."""
    cleaned = _strip_invented_citations("Ставка снижена [K1] , это важно.", {})
    assert "  " not in cleaned
    assert " ," not in cleaned


def test_the_source_list_reaches_the_guard() -> None:
    """Проверяется подключённое: боевой путь передаёт список, а не зовёт вслепую."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime.chat)
    at = source.index("_strip_invented_citations(")
    call = source[at : at + 160]
    assert "knowledge_citations" in call, "список источников снова не передаётся"
