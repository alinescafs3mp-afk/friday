"""Ответ, под которым нет ни одного источника, обязан сказать это ПЕРВЫМ.

Замерено на переписке владельца за 2026-07-30. Из 15 ответов ассистента:

    со ссылками [K#] ............ 10, забраковано 0
    только «вне выборки» ........  5, забраковано 5

Забраковано — это две оценки «минус» в `feedback` и реплика «это и предыдущее —
неверно, посмотри в штатке». Корреляция без единого исключения.

Такой ответ собран из прежних ходов диалога: при переносе в новый ход их ссылки
становятся непроверяемыми и заменяются пометкой. Выглядело это как досье на живого
человека — дата рождения, номер личного дела, состав семьи, — где ни одна строка не
подтверждена ничем.

Прежние средства не срабатывали по двум разным причинам, и обе проверяются ниже:
оговорка стояла ПОСЛЕ тела (под 1645 знаками её не читают) и молчала в четырёх
случаях из пяти, потому что смотрела на результаты поиска, а поиск в тот ход не
нашёл ничего и признака не поднял.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import (
    _CITATION_OUT_OF_VIEW,
    AgentRuntime,
    _citation_notice,
    _grounding_warning,
)
from friday.permissions import ActorContext
from friday.telegram_bridge import TelegramBridge


class _EmptySearcher:
    """Поиск, который в этот ход не нашёл ничего — так и было в тех пяти ответах."""

    async def search(self, user_id, query, **kwargs):
        del user_id, query, kwargs
        return {"results": [], "entity_matches": []}


class _StaticLLM:
    enabled = True
    model = "grounding-test"

    def __init__(self, answer: str):
        self._answer = answer

    async def chat(self, messages, **kwargs):
        del kwargs
        if any(
            "Проверь ответ" in str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        ):
            return {"content": '{"ok": true, "score": 1.0, "issues": []}'}
        return {"content": self._answer}


DOSSIER = (
    "По Макарову Кириллу Евгеньевичу в базе знаний найдена следующая информация:\n"
    f"1. ФИО: Макаров Кирилл Евгеньевич {_CITATION_OUT_OF_VIEW}.\n"
    f"2. Дата рождения: 1999 год {_CITATION_OUT_OF_VIEW}.\n"
    f"3. Личное дело: СА-396195 {_CITATION_OUT_OF_VIEW}."
)


def test_an_answer_made_only_of_out_of_view_marks_is_called_a_recap():
    """Ровно тот ответ, который владелец назвал неверным."""
    warning = _grounding_warning(DOSSIER, None)
    assert warning, "ответ без единого источника прошёл молча"
    assert "пересказ" in warning, warning
    # Признак берётся из текста: в том ходе поиск не нашёл ничего, и `answer_grounded`
    # был None — прежняя проверка на этом и молчала.


def test_the_warning_does_not_fire_when_the_answer_cites_the_archive():
    """Десять ответов со ссылками владелец не забраковал ни разу — их трогать нельзя."""
    cited = "По Ринату Ямалиеву есть данные [K1]. Должность указана в [K2]."
    assert _grounding_warning(cited, True) == ""
    # И даже если рядом с живыми ссылками попалась пометка: там она значит именно то,
    # что написано — конкретное утверждение без источника, а не весь ответ.
    mixed = f"Часть по [K1] подтверждена, а вот это {_CITATION_OUT_OF_VIEW}."
    assert _grounding_warning(mixed, True) == ""


def test_retrieval_found_records_but_the_answer_used_none_of_them():
    """Второй случай из тех пяти: поиск нашёл, ответ не сослался ни на что."""
    warning = _grounding_warning("Досье без единой метки.", False)
    assert warning and "не опирается" in warning, warning


def test_a_plain_answer_is_left_alone():
    assert _grounding_warning("Сохранил заметку.", None) == ""
    assert _grounding_warning("", None) == ""


def test_the_same_thing_is_not_said_twice():
    """Оговорка переехала наверх и обязана исчезнуть снизу.

    Иначе человек читает одно и то же предупреждение дважды — сверху как условие
    и снизу как легенду, — и оба раза верит ему меньше.
    """
    assert _citation_notice([], False) == ""
    assert _citation_notice([], None) == ""
    # Легенда источников на месте: она не предупреждение, и её место снизу.
    legend = _citation_notice([{"label": "K1", "title": "Штатное расписание", "date": ""}], True)
    assert legend.startswith("📎 Источники:")


def test_the_bridge_puts_the_warning_above_the_answer():
    """Место в сообщении — половина этой правки: под досье оговорку не читают."""
    body = TelegramBridge._format_response_message(  # noqa: SLF001
        {
            "message": DOSSIER,
            "grounding_warning": _grounding_warning(DOSSIER, None),
            "citation_notice": "",
        }
    )
    assert body.index("пересказ") < body.index("Макарову"), (
        "предупреждение оказалось не перед ответом:\n" + body
    )
    assert body.startswith("⚠️"), body


@pytest.mark.asyncio
async def test_the_runtime_actually_puts_the_warning_in_its_answer(settings, storage):
    """Проводка, а не только формулировка.

    Предыдущая версия этого файла проверяла сам текст предупреждения и его место в
    сообщении моста, но не то, что рантайм вообще кладёт поле в ответ. Мутация —
    занулить поле в рантайме — проходила мимо всех проверок. Здесь ответ модели
    подставной, поиск подставной, а путь настоящий.
    """
    storage.ensure_user("alice")
    llm = _StaticLLM(DOSSIER)
    runtime = AgentRuntime(settings, storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "давай про Макарова Кирилла инфу",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        enable_tools=False,
        hybrid_searcher=_EmptySearcher(),
    )

    assert result["grounding_warning"].startswith("⚠️"), result["grounding_warning"]
    assert "пересказ" in result["grounding_warning"]
    # И ровно этот ответ мост обязан показать предупреждением вверх.
    body = TelegramBridge._format_response_message(result)  # noqa: SLF001
    assert body.startswith("⚠️"), body


def test_the_legend_still_comes_after_the_answer():
    body = TelegramBridge._format_response_message(  # noqa: SLF001
        {"message": "Ответ по [K1].", "citation_notice": "📎 Источники: [K1] Штатка"}
    )
    assert body.index("Ответ по") < body.index("📎"), body
