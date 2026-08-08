"""«Не дай забыть про отчёт» ставит напоминание, а не обсуждает его.

Последнее место, где решение звать инструмент оставалось за моделью в
однозначной просьбе. Замерено 2026-08-03 на сквозном прогоне: вердикт арбитра
«действие» приходил три раза из трёх, инструмент `remind` был выдан — и в одном
прогоне из двух модель его всё равно не позвала. Человек сказал «не дай забыть
про отчёт в пятницу» и не получил ничего.

Та же измеренная величина, что и везде на этом проекте: решение звать инструмент
принимается примерно раз из шести. Файлы (1/12 до правки), голос, вопросы о людях
и сборка архива уже лечены выполнением ДО хода модели; напоминания оставались
последними.

Цена ошибки несимметрична, и это главный довод. Не поставленное напоминание
человек обнаруживает тогда, когда уже поздно; лишнее — видно сразу и снимается
одной фразой.

Что и когда разбирается ПОНИМАНИЕМ, отдельным дешёвым вызовом: «напомни завтра в
10 про совещание» надо развести на «что» и «когда», и шаблон этого не сделает.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime


class _Kernel:
    def __init__(self, *, success: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._success = success

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, params))
        outcome = self._success

        class _Result:
            success = outcome
            error = "" if outcome else "нет права"
            data = (
                {
                    "created": True,
                    "what": str(params.get("what") or "")[:120],
                    "on": "2026-08-14",
                    "at": "",
                    "requested_when": str(params.get("when") or ""),
                    "delivery_scheduled": True,
                }
                if outcome
                else {"created": False, "reason": "нет права"}
            )

            def to_llm_message(self) -> str:
                return "Напоминание поставлено."

        return _Result()


class _LLM:
    def __init__(self, payload: str) -> None:
        self.enabled = True
        self.payload = payload
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        return {"content": self.payload}


def _runtime(payload: str, *, success: bool = True) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel(success=success)
    runtime.llm = _LLM(payload)
    runtime.settings = None
    return runtime


def _ask(runtime: AgentRuntime, message: str, *, kind: str = "действие", tools=None):
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=(kind, None))
    said: list[dict] = []
    used: list[str] = []
    evidence: list[dict] = []
    offered = tools if tools is not None else [{"function": {"name": "remind"}}]
    bound = AgentRuntime._prefetch_a_reminder_if_asked.__get__(runtime, AgentRuntime)
    done = asyncio.run(bound(message, context, None, offered, said, used, evidence))
    return done, said, used, offered


YES = '{"напоминание": "да", "что": "отчёт", "когда": "в пятницу"}'
NO = '{"напоминание": "нет", "что": "", "когда": ""}'


def test_the_reminder_is_set_without_asking_the_model() -> None:
    """Мутация: убрать предварительную постановку — просьба снова зависит от модели."""
    runtime = _runtime(YES)

    done, said, used, _ = _ask(runtime, "не дай забыть про отчёт в пятницу")

    assert done is True
    assert runtime.kernel.calls == [("remind", {"what": "отчёт", "when": "в пятницу"})]
    assert used == ["remind"]
    assert "отчёт" in str(said[0]["content"]) and "пятницу" in str(said[0]["content"])


def test_the_notice_reads_as_an_answer_if_repeated_verbatim() -> None:
    """Служебную строку модель пересказывает дословно — класс чинился дважды."""
    runtime = _runtime(YES)

    _, said, _, _ = _ask(runtime, "напомни про отчёт в пятницу")

    text = str(said[0]["content"]).lower()
    assert "поставлено" in text, "обещание вместо факта"
    for imperative in ("скажи", "не обещай", "ответь", "переспроси"):
        assert imperative not in text, f"указание самой себе уедет человеку: {imperative}"


def test_the_tool_is_removed_so_nobody_is_woken_twice() -> None:
    """Иначе модель поставит ВТОРОЕ такое же напоминание — как было с двумя архивами."""
    runtime = _runtime(YES)

    _, _, _, offered = _ask(
        runtime,
        "напомни завтра про совещание",
        tools=[{"function": {"name": "remind"}}, {"function": {"name": "make_file"}}],
    )

    names = [str((tool.get("function") or {}).get("name")) for tool in offered]
    assert "remind" not in names, "модель может поставить напоминание второй раз"
    assert "make_file" in names, "остальные инструменты трогать не за чем"


def test_a_question_about_reminders_sets_nothing() -> None:
    """«Какие у меня напоминания?» — вопрос, а не поручение."""
    runtime = _runtime(NO)

    done, said, used, _ = _ask(runtime, "какие у меня напоминания?")

    assert done is False
    assert runtime.kernel.calls == []
    assert said == [] and used == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"напоминание": "да", "что": "", "когда": "в пятницу"}',
        '{"напоминание": "да", "что": "отчёт", "когда": ""}',
        "не json вовсе",
    ],
)
def test_half_a_reminder_is_left_to_the_model(payload: str) -> None:
    """«Напомнить неизвестно о чём» и «когда-нибудь» одинаково бесполезны.

    Тут правильнее переспросить, а переспрашивает модель — значит вмешиваться
    не надо.
    """
    runtime = _runtime(payload)

    done, _, used, _ = _ask(runtime, "напомни")

    assert done is False
    assert runtime.kernel.calls == []
    assert used == []


def test_no_right_no_call() -> None:
    """Предварительное выполнение прав не обходит."""
    runtime = _runtime(YES)

    done, _, _, _ = _ask(runtime, "напомни про отчёт", tools=[])

    assert done is False
    assert runtime.kernel.calls == []
    assert runtime.llm.calls == 0, "разбор запущен там, где ставить всё равно нечем"


def test_an_unrelated_turn_costs_nothing() -> None:
    """Лишний вызов модели на каждую реплику — плата, которой быть не должно."""
    runtime = _runtime(YES)

    done, _, _, _ = _ask(runtime, "какая погода в Москве", kind="интернет")

    assert done is False
    assert runtime.llm.calls == 0, "разбор напоминания зовётся на посторонних ходах"


def test_a_refusing_tool_does_not_pretend_it_worked() -> None:
    """Отказ инструмента не должен превращаться в «напоминание поставлено»."""
    runtime = _runtime(YES, success=False)

    done, said, used, _ = _ask(runtime, "напомни про отчёт в пятницу")

    assert done is False
    assert said == [] and used == []


def test_a_domain_level_reminder_failure_does_not_pretend_it_worked() -> None:
    """Successful dispatch is not the same as a created reminder."""

    runtime = _runtime(YES)

    async def not_created(tool: str, params: dict, actor=None):  # noqa: ANN001, ARG001
        class _Result:
            success = True
            error = ""
            data = {"created": False, "reason": "не разобрала дату"}

            def to_llm_message(self) -> str:
                return "Напоминание не создано."

        return _Result()

    runtime.kernel.execute = not_created
    done, said, used, _ = _ask(runtime, "напомни про отчёт в пятницу")

    assert done is False
    assert said == [] and used == []


def test_the_structural_notice_uses_only_the_persisted_bounded_subject() -> None:
    long_subject = "X" * 180
    runtime = _runtime(
        f'{{"напоминание": "да", "что": "{long_subject}", "когда": "в пятницу", "остаток": ""}}'
    )

    done, said, used, _ = _ask(runtime, "напомни про длинный синтетический предмет")

    assert done is True and used == ["remind"]
    notice = str(said[0]["content"])
    assert "X" * 120 in notice
    assert "X" * 121 not in notice


def test_an_undeliverable_reminder_is_reported_as_saved_not_promised() -> None:
    runtime = _runtime(YES)
    original = runtime.kernel.execute

    async def saved_only(tool: str, params: dict, actor=None):  # noqa: ANN001
        result = await original(tool, params, actor=actor)
        result.data["delivery_scheduled"] = False
        return result

    runtime.kernel.execute = saved_only
    done, said, used, _ = _ask(runtime, "напомни про отчёт в пятницу")

    assert done is True and used == ["remind"]
    notice = str(said[0]["content"]).casefold()
    assert "сохранено" in notice
    assert "доставка в чат сейчас недоступна" in notice
    assert "придёт" not in notice


def test_the_prefetch_is_wired_into_the_loop() -> None:
    """Механизм, который никто не зовёт, работой не является."""
    source = inspect.getsource(AgentRuntime._agentic_loop)
    assert "_prefetch_a_reminder_if_asked(" in source, "постановку напоминания никто не зовёт"
