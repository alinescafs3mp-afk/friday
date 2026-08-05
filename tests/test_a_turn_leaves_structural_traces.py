"""Ход оставляет признаки, по которым его видно без чтения переписки.

Первый шаг ночного компактора (`artifacts/compactor_design.md`). Компакт за сутки
собирается из этого блока, и главное его свойство — в нём НЕТ СТРОК, выведенных
из переписки: только булевы признаки и вид вердикта из закрытого списка.

Почему это решается здесь, а не разбором готового ответа. Половина признаков в
тексте уже неразличима: был ли ответ собран структурой или моделью, знали ли
остаток реплики, отклонили ли указание о правах — по итоговому тексту не понять
никак. Признак надо ставить там, где решение принимается.

Почему список полей у компактора разрешительный. В тех же самых метаданных лежат
`search_query` — сырая реплика человека — и `retrieval_trace` с именами его
документов. Ворота на одной дороге не охраняют ничего: это замерено 2026-08-04
дважды за сутки, и здесь та же развилка.
"""

from __future__ import annotations

import asyncio
import json

from friday.agent_runtime import AgentRuntime
from friday.permissions import ActorContext

# Всё, что человек написал, и всё, что модель ответила. Ни одна подстрока этих
# двух строк не имеет права оказаться в структурном блоке.
HE_WROTE = "запомни: не здоровайся со мной, майор Нестеренко просил"
SHE_SAID = "Привет! Конечно, буду здороваться с вами и дальше."


class _LLM:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self, *, kind: str = "правило") -> None:
        self.kind = kind

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(m.get("content") or "") for m in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {
                "content": (
                    f'{{"вид": "{self.kind}", "правило": "не здороваться", '
                    '"запрос": "", "кто": "", "дни": []}'
                )
            }
        if '"запомнить|забыть|ничего"' in asked:
            return {
                "content": '{"действие": "запомнить", "правило": "не здороваться",'
                ' "прежнее": 0, "остаток": ""}'
            }
        return {"content": SHE_SAID}


def _structural(settings, storage, message: str = HE_WROTE, *, kind: str = "правило") -> dict:
    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage)
    agent.llm = _LLM(kind=kind)
    actor = ActorContext(user_id="alice", preset_key="user", source="test")

    result = asyncio.run(agent.chat("alice", message, actor=actor, enable_tools=False))

    row = storage.execute("SELECT metadata_json FROM messages WHERE id=?", (result["message_id"],)).fetchone()
    meta = json.loads(str(row["metadata_json"] or "{}"))
    return dict(meta.get("structural") or {})


def test_the_turn_records_that_the_structure_spoke(settings, storage) -> None:
    """Мутация: убрать блок — компактор слепнет, и это не видно ниоткуда.

    «Ответ собрала структура» — главный признак всей семьи правок за эти сутки.
    По готовому тексту он неразличим: там просто ответ.
    """
    marks = _structural(settings, storage)

    assert marks.get("answer_present") is True, "структурное утверждение не отмечено"
    assert marks.get("model_spoke") is False, "ход модели не отмечен"
    assert marks.get("rule_learned") is True
    assert marks.get("verdict_kind") == "правило"


def test_no_word_of_the_conversation_leaks_into_the_marks(settings, storage) -> None:
    """Ради этого блок и делается булевым.

    Проверяются ОБЕ стороны разговора: и то, что написал человек, и то, что
    ответила модель. Достаточно одного слова длиной от четырёх букв, чтобы
    компакт перестал быть обезличенным.
    """
    marks = _structural(settings, storage)

    printed = json.dumps(marks, ensure_ascii=False).casefold()
    for said in (HE_WROTE, SHE_SAID):
        for word in said.split():
            word = word.strip(".,:;!?«»").casefold()
            if len(word) >= 4:
                assert word not in printed, f"слово переписки в признаках: {word!r}"


def test_the_marks_are_only_flags_and_a_closed_verdict(settings, storage) -> None:
    """Структурная гарантия, а не обещание фильтра.

    Строка разрешена ровно одна — вид вердикта, и он из закрытого списка. Всё
    остальное булево. Тогда утечке некуда попасть, и проверять её нечем.
    """
    marks = _structural(settings, storage)

    known_kinds = {
        "",
        "интернет",
        "знание",
        "архив",
        "человек",
        "файл",
        "действие",
        "быт",
        "правило",
        "поправка",
        "материал",
        "другое",
    }
    for name, value in marks.items():
        if name == "verdict_kind":
            assert value in known_kinds, f"вид вне закрытого списка: {value!r}"
            continue
        assert isinstance(value, bool), f"{name} — не флаг, а {type(value).__name__}"


def test_an_ordinary_turn_is_marked_as_the_models(settings, storage) -> None:
    """Обратная сторона: обычный вопрос отмечается как ход МОДЕЛИ.

    Без этого счётчик «сколько ответов собрала структура» показывал бы сто
    процентов и не значил бы ничего.
    """
    marks = _structural(settings, storage, "какая столица у Франции?", kind="знание")

    assert marks.get("model_spoke") is True
    assert marks.get("answer_present") is False
    assert marks.get("rule_learned") is False
