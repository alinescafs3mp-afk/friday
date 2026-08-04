"""«Привет» не должен стоить четырёх с половиной тысяч токенов.

Замерено 2026-08-02 на живом наборе прав: 24 инструмента, 13 950 знаков их
описаний ≈ 4 650 токенов — и они уходят в КАЖДЫЙ вызов модели, а на один ход
человека вызовов несколько. Журнал вызовов подтвердил: промпт с инструментами
8 426 токенов против 700–2 600 без них.

На «доброе утро» и «спасибо» это чистая трата. Мы уже решили не искать по такой
реплике в архиве — тем более незачем предлагать по ней двадцать четыре действия.

Решает ЗАКРЫТЫЙ СПИСОК, а не вердикт арбитра, хотя признак болтовни знает оба.
Разница в цене ошибки: для поиска ошибка арбитра стоит переспрашивания, здесь —
несделанного дела. «Напомни завтра» арбитр вполне может счесть разговором.
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime, _is_small_talk


@pytest.mark.parametrize("greeting", ["привет", "Доброе утро", "спасибо", "Спасибо!", "пока"])
def test_a_greeting_is_recognised_by_the_closed_list(greeting: str) -> None:
    """Приветствие и благодарность продолжать нечего — дешёвый путь им положен."""
    assert _is_small_talk(greeting)


@pytest.mark.parametrize("consent", ["ок", "ясно", "ага", "хорошо", "принято"])
def test_a_consent_is_not_decided_by_the_closed_list(consent: str) -> None:
    """Слово согласия решает арбитр, а не шаблон.

    Замерено 2026-08-04: «ок» после «Могу поискать на OLX — сделать?» означает
    «делай», а список объявлял его болтовнёй без единого обращения к модели — и
    гасил весь блок понимания вместе с инструментами. Цена дешёвого пути здесь
    оказалась выше выигрыша.
    """
    assert not _is_small_talk(consent), f"«{consent}» снова решается списком"


@pytest.mark.parametrize(
    "request_",
    [
        "напомни мне завтра позвонить",
        "напомни завтра",
        "сделай отчёт",
        "привет, напомни завтра позвонить",
        "что там по поверке",
    ],
)
def test_a_request_never_counts_as_small_talk(request_: str) -> None:
    """Мутация: опереться на `context.small_talk` — арбитр начнёт глотать просьбы."""
    assert not _is_small_talk(request_), f"просьба принята за болтовню: {request_!r}"


def test_the_turn_hides_tools_on_small_talk_only_by_the_list() -> None:
    """Проверяется подключённое: в боевом ходе стоит список, а не вердикт."""
    source = inspect.getsource(AgentRuntime.chat)
    marker = "visible_tools = ("
    decision = source[source.index(marker) : source.index("if self.llm.enabled and visible_tools")]
    assert "_is_small_talk(clean_message)" in decision, "инструменты снова уходят на каждое «привет»"
    assert "context.small_talk" not in decision, (
        "решение опирается на вердикт арбитра — цена его ошибки здесь несделанное дело"
    )
