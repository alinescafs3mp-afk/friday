"""«О чём мы вчера говорили?» — ответ лежит в переписке, а не в документах.

НАЙДЕНО ВЛАДЕЛЬЦЕМ 2026-08-04. Вопрос «Какие сообщения были в самом начале
разговора с пользователем RF?» получил уверенный вымысел: первая реплика ЕГО
текущей беседы была выдана за начало разговора другого человека.

Замер по метаданным того хода: вид реплики определён верно («архив»), но
инструменты не звались вовсе, а поиск шёл по ДОКУМЕНТАМ — в следе видны файлы
вроде «Огневая подготовка_3.docx», все отброшенные переранжировщиком. Найдено
ноль, уверенность 0.0, предупреждение пустое, судья пропущен. На этом нуле и был
построен уверенный ответ.

ДВЕ ПРИЧИНЫ, и обе структурные.

Первая: у арбитра видов «собственная переписка» стояла в описании вида «архив»,
а вид «человек» описывался узко — «что писал, чем занимался». Замерено на живой
модели: из десяти формулировок о переписке в «человек» попадала ОДНА, девять
уходили в «архив», то есть в поиск по документам, где переписки нет вовсе. После
разведения видов по ИСТОЧНИКУ ОТВЕТА (разговоры — «человек», материалы —
«архив») стало 10 из 10 при целом контроле.

Вторая: вид «человек» без названного имени не вёл никуда. Предвыборка искала имя
среди учётных записей, не находила и выходила ни с чем — то есть ход заканчивался
ответом из окна контекста. А окно тут не помощник, а ловушка: история обрезается
на шестнадцати ходах, и начало длинного разговора модели физически недоступно.
Она достраивает его правдоподобным.

Инструмент `message_search` для этого и существует, и ищет по СВОИМ сообщениям
(`own_id`), а не по арендатору: общими владелец сделал документы и записи, не
разговоры. Зовёт его теперь структура — потому что решение звать инструмент,
оставленное модели, принимается редко, и это на системе измерено не раз.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from friday.agent_runtime import AgentContext, AgentRuntime


class _Kernel:
    def __init__(self, rendered: str = "", success: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._rendered = rendered
        self._success = success

    async def execute(  # noqa: ANN202
        self,
        tool: str,
        params: dict,
        actor=None,  # noqa: ANN001, ARG002
        *,
        execution_scope: str = "dialogue",
    ):
        assert execution_scope == "internal"
        self.calls.append((tool, params))
        rendered, success = self._rendered, self._success

        class _Result:
            def __init__(self) -> None:
                self.success = success
                self.data = {
                    "results": (
                        [
                            {
                                "role": "user",
                                "excerpt": rendered,
                                "created_at": "2026-08-01T09:10:00+00:00",
                            }
                        ]
                        if success and rendered
                        else []
                    ),
                    "count": 1 if success and rendered else 0,
                }

            def to_llm_message(self) -> str:
                return rendered

        return _Result()


class _Storage:
    """Учёток нет вовсе: значит имя в вопросе опознать не удастся."""

    def execute(self, sql: str, params: tuple[int, ...] = ()):  # noqa: ANN001
        assert "FROM users" in sql
        assert params == (5001,)

        class _Rows:
            @staticmethod
            def fetchall() -> list[dict]:
                return []

        return _Rows()


def _runtime(rendered: str = "", success: bool = True):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel(rendered, success)
    runtime.storage = _Storage()
    runtime.settings = SimpleNamespace(local_timezone="Europe/Moscow")
    return runtime


def _ask_about_own(runtime, message: str) -> tuple[list[dict], list[str]]:
    messages: list[dict] = []
    used: list[str] = []
    bound = AgentRuntime._prefetch_own_messages.__get__(runtime, AgentRuntime)  # noqa: SLF001
    context = AgentContext(
        conversation_id="conv_fixture",
        user_id="owner",
        person_id="owner",
        source_search_lineage_user_message_id="msg_0000000000000001",
    )
    asyncio.run(
        bound(
            message,
            None,
            [{"function": {"name": "message_search"}}],
            messages,
            used,
            [],
            context=context,
        )
    )
    if context.structural_answer and not messages:
        messages.append({"role": "assistant", "content": context.structural_answer})
    return messages, used


def test_the_search_runs_before_the_model_gets_the_turn() -> None:
    """Мутация: убрать предварительный вызов — тест краснеет."""
    runtime = _runtime("2026-08-03 14:02 вы: про поверку манометра")

    messages, used = _ask_about_own(runtime, "что я писал про поверку?")

    assert runtime.kernel.calls, "поиск по переписке не вызван — модель снова решает сама"
    assert runtime.kernel.calls[0][0] == "message_search"
    assert "message_search" in used
    assert "поверку манометра" in messages[0]["content"]


def test_the_answer_is_bound_to_what_was_found() -> None:
    """Найденное подаётся как ЕДИНСТВЕННОЕ основание ответа.

    Иначе модель смешает найденные строки с содержимым окна и выдаст смесь за
    выписку из переписки — ровно то, что произошло у владельца.
    """
    runtime = _runtime("2026-08-01 09:10 вы: когда поверка?")

    messages, _ = _ask_about_own(runtime, "что я тебе писал про поверку?")

    body = messages[0]["content"]
    assert "По точной теме" in body
    assert "переписке" not in body.casefold() or "найдено сообщений" in body


def test_an_empty_search_says_so_instead_of_filling_the_gap() -> None:
    """Пустая выдача — это ОТВЕТ, а не повод достроить его из окна.

    Самый опасный случай: поиск отработал и не нашёл ничего. Модель, увидев
    пустоту, склонна заполнить её последними ходами диалога — и выдать их за
    историю переписки. Поэтому пустота проговаривается явно, вместе с запретом
    пересказывать текущий диалог как найденное.
    """
    runtime = _runtime("")

    messages, used = _ask_about_own(runtime, "что я писал про квантовыйананас")

    assert "message_search" in used, "ход не помечен сработавшим — след потерян"
    body = messages[0]["content"]
    assert "найдено сообщений: 0" in body


def test_a_failed_search_does_not_pretend_to_have_data() -> None:
    """Инструмент отказал — ветка та же, что у пустоты, а не «вот что нашлось»."""
    runtime = _runtime("что-то", success=False)

    messages, _ = _ask_about_own(runtime, "что я писал про квантование?")

    assert "не нашёл" in messages[0]["content"]


def test_without_the_tool_nothing_happens() -> None:
    """Права человека не обходим: нет инструмента — нет и предвыборки."""
    runtime = _runtime("что-то")
    messages: list[dict] = []
    used: list[str] = []
    bound = AgentRuntime._prefetch_own_messages.__get__(runtime, AgentRuntime)  # noqa: SLF001
    done = asyncio.run(bound("о чём мы говорили?", None, [], messages, used, []))

    assert done is False
    assert not runtime.kernel.calls and not messages


def test_missing_current_message_boundary_fails_closed_without_search() -> None:
    runtime = _runtime("FORBIDDEN-OLD-HISTORY")
    context = AgentContext(conversation_id="conv_fixture", user_id="owner", person_id="owner")
    bound = AgentRuntime._prefetch_own_messages.__get__(runtime, AgentRuntime)  # noqa: SLF001

    done = asyncio.run(
        bound(
            "что я писал про сроки?",
            None,
            [{"function": {"name": "message_search"}}],
            [],
            [],
            [],
            context=context,
        )
    )

    assert done is True
    assert runtime.kernel.calls == []
    assert "границу текущего сообщения" in context.structural_answer
    assert "FORBIDDEN-OLD-HISTORY" not in context.structural_answer


def test_a_named_stranger_does_not_become_my_own_messages() -> None:
    """«Что писал Иванов» про человека из ДОКУМЕНТОВ — это не моя переписка.

    Обратная сторона новой ветки, и без неё правка была бы регрессом: имя названо,
    учётки с ним нет, а поиск по своим сообщениям вернул бы случайные совпадения
    слов и выдал их за ответ про Иванова. Раньше такой вопрос просто уходил
    обычным поиском по архиву — так и должно остаться.
    """
    runtime = _runtime("что-то нашлось")
    messages: list[dict] = []
    used: list[str] = []

    class _Ctx:
        outward_verdict = ("человек", "Иванов")

    bound = AgentRuntime._prefetch_person_activity.__get__(runtime, AgentRuntime)  # noqa: SLF001
    done = asyncio.run(
        bound(
            "что писал Иванов",
            None,
            [{"function": {"name": "user_activity"}}, {"function": {"name": "message_search"}}],
            messages,
            used,
            [],
            _Ctx(),
        )
    )

    assert done is False
    assert "message_search" not in used, "вопрос про чужого человека ушёл в мою переписку"
    assert not messages


def test_the_person_branch_falls_through_to_own_messages() -> None:
    """Вид «человек» без названного имени обязан вести к своей переписке.

    Проверяется именно СТЫК, и проверяется ПОВЕДЕНИЕМ: первая редакция этого
    теста мерила расстояние между строками в исходнике и покраснела от
    добавленного рядом комментария — то есть проверяла форму кода, а не то, что
    он делает.
    """
    runtime = _runtime("2026-08-02 11:00 вы: обсуждали сроки")
    messages: list[dict] = []
    used: list[str] = []

    class _Ctx:
        outward_verdict = ("человек", None)
        source_search_lineage_user_message_id = "msg_0000000000000001"
        structural_answer = ""

    context = _Ctx()
    bound = AgentRuntime._prefetch_person_activity.__get__(runtime, AgentRuntime)  # noqa: SLF001
    done = asyncio.run(
        bound(
            "что я писал про сроки?",
            None,
            [{"function": {"name": "user_activity"}}, {"function": {"name": "message_search"}}],
            messages,
            used,
            [],
            context,
        )
    )

    assert done is True, "вопрос о разговоре без имени снова не ведёт никуда"
    assert used == ["message_search"]
    assert "обсуждали сроки" in context.structural_answer


def test_the_arbiter_sends_conversations_to_the_person_kind() -> None:
    """Разведение видов держится на ИСТОЧНИКЕ ответа, а не на вежливости фразы.

    Замерено на живой модели: 1 из 10 до правки, 10 из 10 после. Здесь
    проверяется, что описание видов не вернулось к прежнему противоречию, где
    «собственная переписка» стояла и в «архиве», и в «человеке» одновременно.
    """
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)  # noqa: SLF001
    human = source[source.index('"человек — ') :][:1200]
    archive = source[source.index('"архив — ') :][:700]

    assert "ПЕРЕПИСКУ" in human
    assert "о чём был разговор" in human or "о чём мы вчера говорили" in human
    assert "собственной переписке" not in archive, "переписка снова заявлена и в «архиве»"
    assert "МАТЕРИАЛАХ" in archive
    # Упрёк о повторяющемся поведении — это ПРАВИЛО, а не вопрос о переписке.
    # Замерено 2026-08-04: пока «сколько раз я просил» стояло примером у вида
    # «человек», две формулировки из восьми уходили в поиск по сообщениям, где
    # ответа нет, и правило не записывалось вовсе. После разведения — 11 из 11
    # на правилах при целом наборе про переписку.
    assert "сколько раз я просил" not in human.casefold(), "упрёк снова заявлен как вопрос о переписке"
    rules = source[source.index('"правило — ') : source.index('"поправка — ')]
    assert "ВОПРОСОМ или упрёком" in rules
    assert "ПОВТОРЯЮЩИМСЯ поведением" in rules
