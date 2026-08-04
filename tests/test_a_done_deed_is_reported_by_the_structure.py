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

    def __init__(
        self,
        *,
        rest: str | None = "",
        final: str = PROMISES_THE_FUTURE,
        garbled: bool = False,
    ) -> None:
        self.rest = rest
        self.final = final
        # Арбитр ответил не JSON — то есть разобрать НЕ УДАЛОСЬ. Это не то же
        # самое, что «остатка нет», и различие проверяется отдельно: свести их
        # значило бы молча съедать вопрос всякий раз, когда модель ошиблась
        # форматом.
        self.garbled = garbled
        self.final_calls = 0
        self.final_prompts: list[str] = []
        #: Что видел арбитр напоминания. Нужно, чтобы проверить, ЧТО ему дали, а
        #: не только что он ответил: заглушка, отвечающая одинаково на любой
        #: вход, пропустила бы подмену остатка исходной репликой.
        self.reminder_asked: list[str] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {
                "content": '{"вид": "действие", "правило": "", "запрос": "", "кто": "", "дни": []}'
            }
        if '"остаток"' in asked and "уже решена" in asked:
            # Общий разбор остатка: «часть решена, что осталось?». Без этой ветки
            # он падал бы в ветку финального ответа — и `final_calls` считал бы
            # служебный вызов за слово, сказанное человеку.
            if self.garbled:
                return {"content": "разобрать не смог"}
            return {"content": '{"остаток": "%s"}' % ("" if self.rest is None else self.rest)}
        if '"напоминание"' in asked:
            # Записывается РЕПЛИКА, которую дали арбитру: она идёт последним
            # сообщением. Заглушка, отвечающая одинаково на любой вход, не
            # заметила бы подмены остатка исходной репликой.
            self.reminder_asked.append(str(messages[-1].get("content") or ""))
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
    # Остаток после сборки архива — именно просьба напомнить: так ответил бы и
    # настоящий арбитр. Пустая строка здесь означала бы «человек больше ничего не
    # просил», и тест проверял бы не тот случай.
    agent.llm = _Both(rest="напомни про отчёт в пятницу")
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
    # Арбитру напоминания дали ОСТАТОК, а не исходную реплику. Иначе он честно
    # назвал бы остатком просьбу об архиве — уже решённую половину, — и она
    # поехала бы к модели вторым путём.
    assert agent.llm.reminder_asked, "арбитр напоминания не звался"
    assert "собери документы" not in agent.llm.reminder_asked[0], (
        f"решённая половина ушла в разбор напоминания: {agent.llm.reminder_asked[0]!r}"
    )


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
    llm = _Files(rest="что там по проекту", final="По проекту всё идёт по плану.")
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


def test_a_pure_archive_request_needs_no_model(settings, storage) -> None:
    """«Собери документы за 26 число» — просьба целиком, отвечать больше не на что.

    Вторая половина класса 4, доделанная общим разбором остатка. Раньше сборка
    остатка не считала — дни ей даёт арбитр видов, а поля «остаток» у него нет, —
    и ход у модели не отнимался: к верному факту она могла добавить «сейчас
    соберу». Добавлять поле в самый нагруженный классификатор системы ради этого
    не понадобилось: `_remainder_after` зовётся только там, где структура уже что-
    то решила.
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
    llm = _Files(rest="", final="Сейчас соберу документы за 26 число.")
    agent.llm = llm
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = asyncio.run(agent.chat("alice", "собери документы за 26 число", actor=actor))

    said = str(result.get("message") or "")
    assert "Архив собран" in said, "факт о собранном архиве пропал"
    assert llm.final_calls == 0, "модели дали слово там, где всё уже сделано"
    assert "сейчас соберу" not in said.casefold(), f"обещание собрать уже собранное: {said!r}"


def test_an_unparsed_remainder_keeps_the_turn_for_the_model(settings, storage) -> None:
    """Арбитр ответил не JSON — это «не разобрали», а не «остатка нет».

    Свести эти два исхода значило бы молча съедать вопрос всякий раз, когда
    модель ошиблась форматом, — а ошибается она форматом регулярно. Здесь цена
    ошибки та же, что и везде в этой семье: несделанное дело человек обнаружит
    поздно, лишнюю фразу модели — сразу.
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
    llm = _Files(garbled=True, final="По проекту всё идёт по плану.")
    agent.llm = llm
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    result = asyncio.run(
        agent.chat("alice", "собери документы за 26 число, и что там по проекту", actor=actor)
    )

    said = str(result.get("message") or "")
    assert "Архив собран" in said, "факт о собранном архиве пропал"
    assert llm.final_calls == 1, "неразобранный остаток съел вопрос человека"
    assert "по плану" in said
    # Ход модели сам по себе ничего не доказывает: заглушка отвечает одинаково на
    # любой вход, и тест был зелёным, когда модели уезжала ПУСТАЯ строка вместо
    # реплики. Проверяется то, что она получила. Указано внешним разбором (Сол,
    # 2026-08-04) и подтверждено замером на боевой сборке.
    assert llm.final_prompts, "модель не звалась — проверять нечего"
    assert "что там по проекту" in llm.final_prompts[0], "вопрос человека не доехал до модели"


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
