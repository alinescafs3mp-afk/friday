"""Дело сделано до хода модели — о нём говорит структура, а не модель.

Разбор Grok (флешка, `grok.txt`, класс 4). Продолжение того же контракта, что уже
закрыл правила, поправки и отказы (`f4b3cc2`), только источник решённого другой:
не разбор реплики, а СОСТОЯВШИЙСЯ побочный эффект. Напоминание поставлено, архив
собран — исход известен точно, и обещать его будущим временем нельзя.

Сегодняшняя защита половинчата и признаёт это самим своим устройством: служебная
строка написана «фактами в прошедшем времени», чтобы её МОЖНО БЫЛО пересказать
дословно. То есть говорить по-прежнему будет модель, а мы лишь надеемся, что она
перескажет удачно. Замерено на этом проекте четырежды, что надежда — не механизм.

Цена ошибки здесь та же, что у отказа в правах, и такая же несимметричная. «Сейчас
поставлю напоминание» после того, как оно уже стоит, человек читает как обещание;
он ждёт — и либо получает напоминание, которого не ждал, либо ставит второе.
"""

from __future__ import annotations

import asyncio

from friday.agent_runtime import AgentRuntime
from friday.permissions import ActorContext

PROMISES_THE_FUTURE = "Хорошо, сейчас поставлю тебе напоминание про отчёт на пятницу."


class _Kernel:
    """Ядро, у которого `remind` срабатывает, — как на живой системе."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [
            {"type": "function", "function": {"name": "remind", "description": "напомнить"}},
            {"type": "function", "function": {"name": "memory_search", "description": "искать"}},
        ]

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, params))

        class _Result:
            success = True
            error = ""
            attachment = None

            def to_llm_message(self) -> str:
                return "Напоминание поставлено."

        return _Result()


class _Hostile:
    """Арбитрам отвечает верно, человеку — обещает уже сделанное."""

    enabled = True
    total_budget_sec = 5.0

    def __init__(self, *, rest: str | None = "", final: str = PROMISES_THE_FUTURE) -> None:
        self.rest = rest
        self.final = final
        self.final_calls = 0
        self.final_prompts: list[str] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {
                "content": '{"вид": "действие", "правило": "", "запрос": "", "кто": "", "дни": []}'
            }
        if '"напоминание"' in asked:
            rest = "" if self.rest is None else f', "остаток": "{self.rest}"'
            return {
                "content": (
                    '{"напоминание": "да", "что": "отчёт", "когда": "в пятницу"' + rest + "}"
                )
            }
        self.final_calls += 1
        self.final_prompts.append(asked)
        return {"content": self.final}


def _answer(settings, storage, llm: _Hostile, message: str) -> tuple[str, _Kernel]:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _Kernel()
    agent = AgentRuntime(settings, storage, kernel=kernel)
    agent.llm = llm
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    result = asyncio.run(agent.chat("alice", message, actor=actor))
    return str(result.get("message") or ""), kernel


def test_the_reminder_is_reported_as_done(settings, storage) -> None:
    """Мутация: отдать текст модели — «сейчас поставлю» возвращается.

    Напоминание уже стоит. Обещание поставить его — неправда о собственном
    состоянии, и человек по ней действует: ждёт или ставит второе.
    """
    llm = _Hostile()

    said, kernel = _answer(settings, storage, llm, "напомни про отчёт в пятницу")

    assert kernel.calls and kernel.calls[0][0] == "remind", "проверять нечего: напоминание не ставилось"
    assert "сейчас поставлю" not in said.casefold(), f"обещание вместо факта: {said!r}"
    assert "отчёт" in said.casefold(), "человек не узнал, о чём напомнят"
    assert "пятниц" in said.casefold(), "человек не узнал, когда"


def test_the_model_is_not_asked_when_the_deed_is_the_whole_turn(settings, storage) -> None:
    """«Напомни про отчёт в пятницу» — просьба целиком, отвечать больше не на что."""
    llm = _Hostile()

    _answer(settings, storage, llm, "напомни про отчёт в пятницу")

    assert llm.final_calls == 0, "модели дали слово там, где всё уже сделано"


def test_a_question_beside_the_deed_is_still_answered(settings, storage) -> None:
    """Половина реплики — поручение, половина — вопрос. Вторую нельзя терять."""
    llm = _Hostile(rest="как там дела с проектом")

    said, _ = _answer(
        settings, storage, llm, "напомни про отчёт в пятницу, и как там дела с проектом"
    )

    assert "поставлено" in said.casefold(), "факт о сделанном пропал"
    assert llm.final_calls == 1, "вопрос человека потерян МОЛЧА"


def test_a_reminder_beside_an_archive_is_not_swallowed(settings, storage) -> None:
    """Собранный архив не отменяет просьбу напомнить.

    ДЕФЕКТ СОБСТВЕННОЙ ПРАВКИ, показанный мутацией. Сборка архива объявляла ход
    своим целиком (`open_remainder = ""`), потому что дни ей даёт общий арбитр
    видов, у которого поля «остаток» нет. Следующий предварительный вызов получал
    пустую строку — и «собери документы за 26 число и напомни про отчёт в
    пятницу» теряло напоминание МОЛЧА.

    Мутация ломала связку вызовов и тем самым делала поведение ЛУЧШЕ — верный
    признак, что неправа была связка. Различие «остатка нет» и «остаток не
    считали» появилось здесь.
    """

    class _Both(_Hostile):
        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            asked = " ".join(str(m.get("content") or "") for m in messages)
            if '"вид": "интернет' in asked:
                return {
                    "content": '{"вид": "файл", "правило": "", "запрос": "", "кто": "",'
                    ' "дни": ["26"]}'
                }
            return await super().chat(messages, tools=tools, **kwargs)

    class _Packs(_Kernel):
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            self.calls.append((tool, params))

            class _Result:
                success = True
                error = ""
                data = {"filename": "Документы.zip", "files_in_archive": 3, "days": ["2026-07-26"]}
                attachment = {"filename": "Документы.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Готово."

            return _Result()

    storage.ensure_user("alice", preset_key="owner")
    kernel = _Packs()
    agent = AgentRuntime(settings, storage, kernel=kernel)
    agent.llm = _Both()
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = asyncio.run(
        agent.chat(
            "alice",
            "собери документы за 26 число и напомни про отчёт в пятницу",
            actor=actor,
        )
    )

    called = [tool for tool, _ in kernel.calls]
    assert "collect_files" in called, "проверять нечего: архив не собирался"
    assert "remind" in called, "напоминание потеряно молча"
    said = str(result.get("message") or "")
    assert "Архив собран" in said and "Напоминание поставлено" in said, said


def test_a_question_beside_an_archive_still_reaches_the_model(settings, storage) -> None:
    """Сборка архива не отменяет заданный рядом вопрос.

    Сборка знает, что сделала, но остатка НЕ считает: дни ей даёт общий арбитр
    видов, у которого поля «остаток» нет. Объявить ход своим целиком она поэтому
    не вправе — «собери документы за 26 число, и что там по проекту» осталось бы
    без ответа, и человек не узнал бы об этом ниоткуда.

    Отсюда и различие «остатка нет» / «остаток не считали»: ход отнимается только
    в первом случае. Мутация «архив снова объявляет ход своим» краснит здесь.
    """

    class _Files(_Hostile):
        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            asked = " ".join(str(m.get("content") or "") for m in messages)
            if '"вид": "интернет' in asked:
                return {
                    "content": '{"вид": "файл", "правило": "", "запрос": "", "кто": "",'
                    ' "дни": ["26"]}'
                }
            return await super().chat(messages, tools=tools, **kwargs)

    class _Packs(_Kernel):
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            self.calls.append((tool, params))

            class _Result:
                success = True
                error = ""
                data = {"filename": "Документы.zip", "files_in_archive": 3, "days": ["2026-07-26"]}
                attachment = {"filename": "Документы.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Готово."

            return _Result()

    storage.ensure_user("alice", preset_key="owner")
    agent = AgentRuntime(settings, storage, kernel=_Packs())
    llm = _Files(final="По проекту всё идёт по плану.")
    agent.llm = llm
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = asyncio.run(
        agent.chat(
            "alice", "собери документы за 26 число, и что там по проекту", actor=actor
        )
    )

    said = str(result.get("message") or "")
    assert "Архив собран" in said, "факт о собранном архиве пропал"
    assert llm.final_calls == 1, "вопрос человека потерян МОЛЧА"
    assert "по плану" in said, "ответ модели не доехал до человека"


def test_the_judge_is_not_asked_about_the_structure(settings, storage) -> None:
    """Судят слова МОДЕЛИ, а не отчёт системы о собственном действии.

    Найдено разбором своей же правки, гейт этого не показал бы. «Напоминание
    поставлено: „отчёт“, срок — в пятницу» — не утверждение о мире, и судья,
    сверяющий утверждения с записями, обязан сказать «не подтверждается вашими
    данными». Предупреждение не по делу обесценивает те, что по делу, — а здесь
    оно стояло бы под фактом, который система знает ТОЧНО, потому что сама его и
    совершила.

    Мутация: судить склейку вместо слов модели — тест краснеет.
    """
    llm = _Hostile()
    judged: list[str] = []
    original = AgentRuntime._verify_response

    async def _spy(self, question, answer, context, **kwargs):  # noqa: ANN001, ANN002
        judged.append(str(answer))
        return await original(self, question, answer, context, **kwargs)

    AgentRuntime._verify_response = _spy
    try:
        said, _ = _answer(settings, storage, llm, "напомни про отчёт в пятницу")
    finally:
        AgentRuntime._verify_response = original

    assert "поставлено" in said.casefold(), "проверять нечего: факт не собран"
    assert not judged, f"структурный факт отдан судье: {judged!r}"


def test_the_model_never_sees_the_settled_deed(settings, storage) -> None:
    """Переспорить нельзя то, чего тебе не показали.

    Реплика заменяется остатком ЦЕЛИКОМ, а не только в последнем сообщении: та же
    просьба едет вторым путём — полем `search_query` в конверте контекста. На
    правилах эта дорога уже находилась замером; здесь проверяется сразу.
    """
    llm = _Hostile(rest="как там дела с проектом")

    _answer(settings, storage, llm, "напомни про отчёт в пятницу, и как там дела с проектом")

    assert llm.final_prompts, "модель не звалась — проверять нечего"
    asked = llm.final_prompts[0]
    assert "напомни про отчёт" not in asked, "сделанное всё ещё лежит перед моделью"
    assert "дела с проектом" in asked, "остаток до модели не доехал"


def test_the_judge_hears_only_the_model(settings, storage) -> None:
    """На смешанном ходу судья получает слова модели, а не склейку с фактом.

    Иначе он бракует ответ из-за строки, которую модель не писала и исправить не
    может, — и человек читает «не подтверждается вашими данными» под фактом,
    который система совершила сама.
    """
    # Длина не для красоты: судья не зовётся на коротких ответах, и первая
    # редакция теста молча проверяла ход, где судьи не было вовсе.
    long_enough = "Проект идёт по плану. " * 40
    llm = _Hostile(rest="как там дела с проектом", final=long_enough)
    judged: list[str] = []
    original = AgentRuntime._verify_response

    async def _spy(self, question, answer, context, **kwargs):  # noqa: ANN001, ANN002, ARG001
        judged.append(str(answer))
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    AgentRuntime._verify_response = _spy
    try:
        _answer(settings, storage, llm, "напомни про отчёт в пятницу, и как там дела с проектом")
    finally:
        AgentRuntime._verify_response = original

    assert judged, "судья не звался — проверять нечего"
    assert "Напоминание поставлено" not in judged[0], f"судье отдали структурный факт: {judged[0][:120]!r}"


def test_a_silent_arbiter_keeps_the_turn_for_the_model(settings, storage) -> None:
    """Арбитр не назвал остаток — ход остаётся у модели.

    Здесь запасной вариант тот же, что у подтверждений, а не перевёрнутый, как у
    отказов: лишняя фраза модели поверх факта безобидна, а потерянный вопрос —
    нет. Дело УЖЕ сделано, и переспорить этот факт модель не может.
    """
    llm = _Hostile(rest=None)

    said, _ = _answer(settings, storage, llm, "напомни про отчёт в пятницу, и что там по проекту")

    assert llm.final_calls == 1, "неизвестность съела вопрос человека"
    assert "поставлено" in said.casefold(), "факт о сделанном пропал"
