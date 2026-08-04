"""«Давай» — согласие на предложенное, а не пустая реплика.

Найдено в живой переписке владельца 2026-08-04 и воспроизведено по журналу.

    ход 1: «А можешь узнать где в Донецке в наличии есть RPI5?»
            → web_research отработал (09:50:19 старт, 09:50:46 успех);
    ход 2: «давай»
            → НИ ОДНОГО вызова инструмента, ноль записей в audit_log;
            → ответ: «Вот что удалось найти по Донецску: OLX.ua — продавец
              магазин «IT-Store» — статус: В НАЛИЧИИ — цена 2 800–3 200 грн».

Магазин, наличие и цена выдуманы целиком. Тяжелее самой выдумки фраза «Вот что
удалось найти»: она утверждает, что поиск был.

Механика, установленная замером на стенде: слово «давай» короткое, поэтому его
судит арбитр болтовни; арбитру передавалась ТОЛЬКО текущая реплика, а в его
правилах «короткое подтверждение» прямо названо признаком РАЗГОВОРА. Вердикт
«разговор» гасит весь блок понимания — арбитр видов не запускается, веб-поиск не
предлагается, — и модель, оставшись без данных, дописывает результат по памяти.

Вклад половин правки разделён замером на живой модели (продолжения / болтовня):

    старое правило, без предыстории (как было)   1/4   4/4
    старое правило, С предысторией                1/4   4/4
    новое правило, без предыстории                3/4   3/4
    новое правило, С предысторией (как стало)     4/4   3/4

То есть предыстория сама по себе не даёт ничего: решает формулировка, а
предыстория добавляет последний случай. Работает только связка — и это ровно тот
случай, когда «нейтральная в одиночку правка оказывается условием для решающей».

Потеря честная и названа: «ага» в ответ на «Привет! Чем помочь?» теперь уходит в
запрос. Цена — несколько секунд лишнего поиска; цена обратной ошибки — ответ,
выдуманный целиком и поданный как найденный.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import AgentRuntime, _last_exchange


def test_the_previous_exchange_carries_both_sides():
    """Продолжают не свой вопрос, а ПРЕДЛОЖЕНИЕ из ответа.

    Мутация: собирать только реплики человека — тест краснеет, и «давай»
    перестаёт иметь предмет.
    """
    exchange = _last_exchange(
        [
            {"role": "user", "content": "старое"},
            {"role": "assistant", "content": "старый ответ"},
            {"role": "user", "content": "где купить RPI5?"},
            {"role": "assistant", "content": "Могу поискать объявления на OLX — сделать?"},
        ]
    )

    assert "где купить RPI5?" in exchange
    assert "поискать объявления" in exchange, "предложение Пятницы потеряно"
    assert "старый ответ" not in exchange, "взят не последний ход"


def test_an_empty_history_gives_nothing():
    """Первая реплика разговора продолжать не может — и вида не портит."""
    assert _last_exchange([]) == ""
    assert _last_exchange(None) == ""


@pytest.mark.asyncio
async def test_the_arbiter_is_shown_what_is_being_continued(settings, storage):
    """Потребитель — АРБИТР: проверяется, что он получил.

    Мутация: вернуть `message[:200]` без предыстории — тест краснеет, и решение
    снова принимается по слову, вырванному из разговора.
    """
    seen: list[str] = []

    class _Watching:
        enabled = True

        async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
            seen.append(str(messages[-1].get("content") or ""))
            return {"content": "ЗАПРОС"}

    runtime = AgentRuntime(settings, storage, llm=_Watching())
    previous = _last_exchange(
        [
            {"role": "user", "content": "где купить RPI5?"},
            {"role": "assistant", "content": "Могу поискать на OLX — сделать?"},
        ]
    )

    verdict = await runtime._is_small_talk_by_arbiter("давай", previous_turn=previous)  # noqa: SLF001

    assert seen, "арбитр не звался"
    assert "OLX" in seen[0], f"предыдущий ход не доехал до арбитра: {seen[0]!r}"
    assert "давай" in seen[0], "текущая реплика потерялась"
    assert verdict is False


@pytest.mark.asyncio
async def test_a_first_greeting_still_goes_without_history(settings, storage):
    """Ошибка в другую сторону: на первой реплике предыстории нет, и это норма."""
    seen: list[str] = []

    class _Watching:
        enabled = True

        async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
            seen.append(str(messages[-1].get("content") or ""))
            return {"content": "РАЗГОВОР"}

    runtime = AgentRuntime(settings, storage, llm=_Watching())

    assert await runtime._is_small_talk_by_arbiter("привет") is True  # noqa: SLF001
    assert seen[0] == "привет", "пустая предыстория попала в запрос лишним текстом"


@pytest.mark.asyncio
async def test_the_rules_no_longer_call_a_consent_small_talk(settings, storage):
    """Формулировка — половина правки, и замер приписал ей главный вклад.

    Пока «короткое подтверждение» стояло в перечне признаков РАЗГОВОРА,
    предыстория не помогала вовсе: 1/4 и с ней, и без неё.

    Проверяются ПРАВИЛА, дошедшие до модели, а не текст исходника: тест по
    исходнику краснеет от комментария, который объясняет сам дефект, — на этом
    проекте так случалось трижды, и последний раз ровно на этой правке.
    """
    seen: list[str] = []

    class _Watching:
        enabled = True

        async def chat(self, messages, **kwargs):  # noqa: ANN003, ARG002
            seen.append(str(messages[0].get("content") or ""))
            return {"content": "ЗАПРОС"}

    runtime = AgentRuntime(settings, storage, llm=_Watching())
    await runtime._is_small_talk_by_arbiter("давай", previous_turn="человек: найди\nПятница: искать?")  # noqa: SLF001

    rules = seen[0]
    assert "короткое подтверждение" not in rules, "согласие снова объявлено разговором"
    assert "Согласие" in rules, "правило про согласие не доехало до модели"
    assert "продолжать нечего" in rules, "обратная сторона правила потеряна"
    # Отказ — не согласие, и без этого правила первое ломает второе.
    assert "ОТКАЗ" in rules, "просьба остановиться снова читается как согласие"
    assert "просит НЕ делать" in rules


def test_words_of_consent_reach_the_arbiter_at_all():
    """Закрытый список не должен решать за согласие сам.

    Замерено 2026-08-04: «ага», «ок», «хорошо», «ясно», «принято» и ещё пять слов
    ловились шаблоном ДО арбитра, `small_talk` ставился без единого обращения к
    модели, и починка арбитра их не касалась вовсе — то есть дыра оставалась
    открытой для большинства форм согласия.

    Приветствие и благодарность остаются в списке: там продолжать нечего, и
    платить вызовом модели не за что.

    Мутация: вернуть «ок|ага|хорошо» в шаблон — тест краснеет.
    """
    from friday.agent_runtime import _is_small_talk, _might_be_small_talk

    for word in ("ага", "ок", "окей", "угу", "хорошо", "ясно", "понятно", "принято"):
        assert not _is_small_talk(word), f"«{word}» решается списком, минуя арбитра"
        assert _might_be_small_talk(word), f"«{word}» вообще не дойдёт до арбитра"

    for word in ("привет", "спасибо", "пока", "проверка связи", "как дела"):
        assert _is_small_talk(word), f"«{word}» потерял дешёвый путь"
