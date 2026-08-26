"""«что писал Yato?» → «а Пегас?» — второй вопрос тот же, что и первый.

Найдено владельцем 2026-08-03, уже ПОСЛЕ того, как предварительный вызов начал
работать по шаблону: «я сначала спросил — что писал Yato? Ответила. Следом
отправил — а Пегас? И Пятница зафакапилась. В итоге получается, что малейшее
отхождение от задуманной фразы и всё — косяк».

Он прав по сути, и вывод из этого не «дописать в шаблон ещё пару форм». Человек
во второй раз глагол не повторяет — остаётся одно имя, и таких сокращений
бесконечно много. Отдельная просьба владельца в тот же день: «не затыкай всё
эвристиками и шаблонами, старайся работать именно с пониманием. Всех дыр мы не
заткнём».

Поэтому вопрос узнаётся ДВУМЯ путями: быстрым шаблоном (бесплатно, ловит прямую
форму) и арбитром намерения, который видит реплику ВМЕСТЕ с предыдущими и потому
понимает «а Пегас?» как продолжение. Арбитр уже считается параллельно поиску —
своих секунд он не добавляет.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import (
    _ASKS_WHAT_A_PERSON_WROTE,
    _OUTBOUND_TOOL_NAMES,
    AgentContext,
    AgentRuntime,
    _is_direct_file_request,
)
from tests.test_asking_about_a_person_reaches_the_right_tool import PEOPLE, _runtime


@pytest.mark.parametrize("message", ["а Пегас?", "а JBL", "а он что?", "Yato?", "And Pegasus?"])
def test_the_pattern_alone_does_not_see_the_follow_up(message: str) -> None:
    """Половина условия, ради которой добавлена вторая: шаблон тут бессилен.

    Тест закрепляет не желаемое, а измеренное: в короткой форме нет ни одного
    слова, за которое шаблон мог бы зацепиться, — и не будет, сколько форм ни
    перечисляй.
    """
    assert not _ASKS_WHAT_A_PERSON_WROTE.search(message), message


def _ask(runtime, message: str, verdict: tuple[str, str | None] | None) -> list[dict]:
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=verdict)
    messages: list[dict] = []
    bound = AgentRuntime._prefetch_person_activity.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            message,
            None,
            [{"function": {"name": "user_activity"}}],
            messages,
            [],
            [],
            context,
        )
    )
    return messages


def test_understanding_carries_the_question_the_pattern_missed() -> None:
    """Мутация: убрать ветку по вердикту — «а Пегас?» снова уходит в архив."""
    runtime = _runtime(PEOPLE)

    messages = _ask(runtime, "а Пегас?", ("человек", "Пегас"))

    assert runtime.kernel.calls, "понимание не сработало — остался один шаблон"
    assert runtime.kernel.calls[0][0] == "user_activity"
    assert runtime.kernel.calls[0][1]["person"] == "Пегас"
    assert "Пегас" in str(messages[0]["content"])


def test_english_and_pegasus_continues_the_person_activity_question() -> None:
    runtime = _runtime(
        [
            *PEOPLE,
            {
                "id": "telegram:telegram:7777777777",
                "display_name": "Pegasus",
                "username": "pegasus",
                "status": "active",
            },
        ]
    )

    messages = _ask(runtime, "And Pegasus?", ("человек", "Pegasus"))

    assert runtime.kernel.calls[0] == ("user_activity", {"person": "Pegasus"})
    assert "Pegasus" in str(messages[0]["content"])


def test_a_name_that_is_not_in_the_words_at_all() -> None:
    """«а он что?» — имени в реплике нет, оно живёт в предыдущем ходе.

    Первая редакция этого теста брала «...а что там вообще Пегас...» и была
    пустой: мутация «не пробовать имя из вердикта» её НЕ уронила — перебор слов
    и так доходил до «Пегас», а склонения резолвер разбирает сам («Пегасу» →
    pegas, 0.95). Проверять надо там, где второй путь единственный.
    """
    runtime = _runtime(PEOPLE)
    _ask(runtime, "а он что?", ("человек", "Пегас"))
    assert runtime.kernel.calls, "без имени из вердикта спросить не о ком"
    assert runtime.kernel.calls[0][1]["person"] == "Пегас"


def test_when_two_people_are_named_the_verdict_picks_the_asked_one() -> None:
    """«Yato вчера писал, а JBL что?» — спрашивают про второго, не про первого.

    Перебор слов берёт первое опознанное имя и ошибается ровно в том месте, где
    человеку кажется, что он выразился ясно.
    """
    runtime = _runtime(PEOPLE)
    _ask(runtime, "Пегас вчера отписался, а JBL что там", ("человек", "JBL"))
    assert runtime.kernel.calls[0][1]["person"] == "JBL"


def test_a_verdict_without_a_name_still_falls_back_to_the_words() -> None:
    """Арбитр мог не выделить имя — тогда работает прежний перебор слов."""
    runtime = _runtime(PEOPLE)
    _ask(runtime, "а JBL", ("человек", None))
    assert runtime.kernel.calls[0][1]["person"] == "JBL"


def test_an_unknown_name_is_still_left_alone() -> None:
    """Понимание прав не даёт: имя обязано быть среди УЧЁТОК.

    Иначе «а Иванов?» про человека из документов увёл бы вопрос от архива, где
    ответ как раз и лежит.
    """
    runtime = _runtime(PEOPLE)
    messages = _ask(runtime, "а Иванов?", ("человек", "Иванов"))
    assert runtime.kernel.calls == []
    assert messages == []


def test_another_verdict_does_not_trigger_the_person_tool() -> None:
    """«интернет» — не «человек»: чужой вердикт ничего не запускает."""
    runtime = _runtime(PEOPLE)
    messages = _ask(runtime, "а сколько стоит 5090?", ("интернет", "цена 5090"))
    assert runtime.kernel.calls == []
    assert messages == []


def test_a_short_name_question_never_reaches_a_search_engine() -> None:
    """Замерено на живом экземпляре, СРАЗУ после того как короткая форма ожила.

    «А Пегас?» подняла инструмент надзора — и в том же ходе `web_research`. Имя
    участника ушло в чужой поисковик. Прежняя защита стояла на шаблоне, а эту
    форму шаблон не ловит: починили одну половину развилки, вторая осталась на
    старом условии. Дефект «Пегас Туристик» вернулся через новую дверь.

    Владелец просил режим полной приватности прямым текстом.
    """
    runtime = _runtime(PEOPLE)
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("человек", "Пегас"))
    tools_used: list[str] = []
    bound = AgentRuntime._prefetch_the_web_if_asked.__get__(runtime, AgentRuntime)
    asyncio.run(
        bound(
            "а Пегас?",
            None,
            [{"function": {"name": "web_research"}}],
            [],
            tools_used,
            [],
            [],
            context,
        )
    )
    assert runtime.kernel.calls == [], "имя участника снова уходит в поисковик"
    assert tools_used == []


def test_the_model_is_not_even_offered_the_web_for_a_person_question() -> None:
    """Веб-инструменты УБИРАЮТСЯ, а не отговариваются.

    Запрет на предварительный вызов такое не ловит: `web_research` в живом
    замере позвала САМА модель. Проверено дважды на других поверхностях (голос,
    поиск) — уговорами это не лечится, только тем, чего нет в списке.
    """
    source = inspect.getsource(AgentRuntime.chat)
    assert 'topic.startswith("человек")' in source
    at = source.index("if outbound_blocked")
    guard = source[at : at + 1800]
    assert "_OUTBOUND_TOOL_NAMES" in guard, "модель снова получила закрытые инструменты"
    assert {"web_research", "web_search", "web_fetch"} <= _OUTBOUND_TOOL_NAMES


def test_the_archive_hint_is_dropped_when_understanding_says_person() -> None:
    """Половина, без которой первая бесполезна.

    Инструмент отработает, но рядом в контекст уедет подсказка «похожее в архиве
    есть, но не по делу», и модель перескажет её. Именно это владелец увидел:
    «Пятница слово в слово прошлое сообщение повторила».
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    verdict_at = source.index("context.outward_verdict = await arbiter")
    tail = source[verdict_at:]
    assert 'startswith("человек")' in tail, "вердикт о человеке больше не чистит архив"
    assert "context.knowledge_hits = []" in tail, "подсказка про архив снова едет к модели"


def test_a_document_request_is_understood_too() -> None:
    """Явная просьба о документе узнаётся без доверия широкому вердикту.

    Арбитр полезен для понимания темы, но его ``файл`` слишком широк: так он
    помечает и просьбы отформатировать сообщение для Telegram. Полномочие на
    поздний ``make_file`` поэтому даёт только видимая просьба создать документ;
    измеренная владельцем формулировка обязана входить в эту закрытую границу.
    """

    assert _is_direct_file_request("Собери справку по поверке и сделай из неё документ Word")
    assert not _is_direct_file_request("Ответь списком для Telegram")


def test_understanding_knows_both_kinds() -> None:
    """Арбитра спрашивают о том, что от него ждут: виды должны быть в промпте."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "человек|файл" in source, "вид не объявлен — модель его не вернёт"
    assert '"кто"' in source, "имя человека некуда положить"
