"""Сверка предложенной связи с документом, который её якобы объявляет.

Очередь кандидатов растёт быстрее, чем человек её разбирает: на архиве владельца
она дошла до 597 строк, и половина не переживает вопроса «а про кого эта строка».
Разбирать её правилом нельзя — замерено: правило «выдержка обязана называть
начало связи ИЛИ начало обязано быть субъектом документа» отсекает в очереди 344
строки, но на контроле из уже принятых связей убивает 14 из 64, включая настоящую
родню из анкеты. Поэтому судит арбитр, и судит по окну документа.

Ниже — границы этого прохода: чего он не делает молча и что оставляет человеку.
"""

from __future__ import annotations

import json

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id

_ROSTER = (
    "ШТАТНО-ДОЛЖНОСТНОЙ РАСЧЁТ\n"
    "УПРАВЛЕНИЕ\n"
    "Командир батальона | подполковник | Марухненко Иван Михайлович\n"
    "Водитель | рядовой | Котельников Олег Сергеевич\n"
    "Повар | рядовой | Ким Виктор Григорьевич\n"
)


class _Model:
    """Заглушка арбитра: отвечает по очереди и запоминает, что видела."""

    def __init__(self, *payloads: object) -> None:
        self.payloads = list(payloads)
        self.calls: list[list[dict[str, object]]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        if isinstance(payload, Exception):
            raise payload
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return {"content": content, "finish_reason": "stop"}


def _queue(storage, *, excerpt: str, text: str = _ROSTER) -> tuple[str, dict[str, str]]:
    """Документ, две сущности и один предложенный кандидат между ними."""

    graph = KnowledgeGraph(storage)
    people = {
        name: str(graph.create_entity("alice", name, EntityType.PERSON)["id"])
        for name in ("Марухненко Иван Михайлович", "Котельников Олег Сергеевич")
    }
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), text, "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content=text, title="Расчёт")
    storage.store_knowledge_object(document)
    for entity_id in people.values():
        graph.link_knowledge_to_entity(document.id, entity_id, "alice")
    candidate = storage.store_relation_candidate(
        "alice",
        people["Марухненко Иван Михайлович"],
        people["Котельников Олег Сергеевич"],
        "manages",
        confidence=0.9,
        evidence={
            "knowledge_object_id": document.id,
            "source_name": "Марухненко Иван Михайлович",
            "target_name": "Котельников Олег Сергеевич",
            "excerpt": excerpt,
            "method": "document_structure_arbiter",
        },
    )
    return str(candidate["id"]), people


def _status(storage, candidate_id: str) -> str:
    row = storage.get_relation_candidate("alice", candidate_id)
    return str(row["status"]) if row else "исчез"


@pytest.mark.asyncio
async def test_a_verdict_of_reject_never_becomes_a_relation(storage):
    candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")  # noqa: E501
    graph = KnowledgeGraph(storage)
    model = _Model(
        {
            "verdict": "отвергаю",
            "about": "Котельников Олег Сергеевич",
            "reason": "Строка ведомости объявляет должность самого Котельникова.",
        }
    )

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["reject"] == 1
    assert _status(storage, candidate_id) == "rejected"
    assert storage.get_entity_relations(_people["Марухненко Иван Михайлович"], "alice") == []


@pytest.mark.asyncio
async def test_a_verdict_of_confirm_creates_the_relation(storage):
    candidate_id, people = _queue(
        storage, excerpt="Командир батальона | подполковник | Марухненко Иван Михайлович"
    )
    graph = KnowledgeGraph(storage)
    model = _Model(
        {
            "verdict": "подтверждаю",
            "about": "Марухненко Иван Михайлович",
            "reason": "Он назван командиром этого подразделения.",
        }
    )

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["confirm"] == 1
    assert _status(storage, candidate_id) == "accepted"
    relations = storage.get_entity_relations(people["Марухненко Иван Михайлович"], "alice")
    assert [row["relation_type"] for row in relations] == ["manages"]


@pytest.mark.asyncio
async def test_an_arbiter_that_abstains_leaves_the_candidate_to_the_person(storage):
    """Воздержание — не отказ.

    Решение по кандидату терминально: отвергнутого не вернуть. Поэтому «не
    уверен» обязано оставлять строку в очереди, а не закрывать её тихо.
    """

    candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")
    graph = KnowledgeGraph(storage)
    model = _Model({"verdict": "не уверен", "about": "", "reason": "Документ не даёт решить."})

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["unsure"] == 1
    assert result["applied"] == 0
    assert _status(storage, candidate_id) == "suggested"


@pytest.mark.asyncio
async def test_an_unreadable_answer_is_not_a_rejection(storage):
    """Нечитаемый ответ модели говорит о сверке, а не о связи."""

    candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")
    graph = KnowledgeGraph(storage)
    model = _Model("не знаю, тут какая-то ерунда")

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["unsure"] == 1
    assert _status(storage, candidate_id) == "suggested"


@pytest.mark.asyncio
async def test_a_quote_absent_from_the_document_is_rejected_without_the_model(storage):
    """Выдумannая выдержка не стоит вызова модели — и отказ по ней не её мнение."""

    candidate_id, _people = _queue(storage, excerpt="Начальник штаба | майор | Кузнецов Евгений")
    graph = KnowledgeGraph(storage)
    model = _Model({"verdict": "подтверждаю", "about": "", "reason": "не должно быть спрошено"})

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["reject"] == 1
    assert model.calls == []
    assert _status(storage, candidate_id) == "rejected"
    assert result["verdicts"][0]["checked_by"] == "structure"


@pytest.mark.asyncio
async def test_a_showing_pass_changes_nothing(storage):
    """Показ здесь настоящий: без `--apply` статусы не трогаются вовсе."""

    candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")
    graph = KnowledgeGraph(storage)
    model = _Model({"verdict": "отвергаю", "about": "", "reason": "Строка про самого Котельникова."})

    result = await graph.review_relation_candidates("alice", llm=model, apply=False)

    assert result["reject"] == 1
    assert result["applied"] == 0
    assert _status(storage, candidate_id) == "suggested"


@pytest.mark.asyncio
async def test_a_silent_model_is_counted_and_not_mistaken_for_a_clean_queue(storage):
    """Молчащая модель даёт «просмотрено N, отвергнуто 0» — это надо назвать числом."""

    candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")
    graph = KnowledgeGraph(storage)
    model = _Model(RuntimeError("эндпоинт недоступен"))

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["model_errors"] == 1
    assert result["confirm"] == result["reject"] == 0
    assert _status(storage, candidate_id) == "suggested"


@pytest.mark.asyncio
async def test_the_judge_sees_the_window_the_answerer_saw(storage):
    """Судье нужно то же, что было у отвечающего.

    По одной выдержке «Водитель | рядовой | Котельников» форму документа не
    узнать: она одинакова и в ведомости, и в приказе о назначении. Решает
    соседняя строка — поэтому в промпт идёт окно документа целиком.
    """

    _candidate_id, _people = _queue(storage, excerpt="Водитель | рядовой | Котельников Олег Сергеевич")
    graph = KnowledgeGraph(storage)
    model = _Model({"verdict": "отвергаю", "about": "", "reason": "Строка про самого Котельникова."})

    await graph.review_relation_candidates("alice", llm=model, apply=False)

    prompt = str(model.calls[0][1]["content"])
    assert "ШТАТНО-ДОЛЖНОСТНОЙ РАСЧЁТ" in prompt
    assert "Повар | рядовой | Ким Виктор Григорьевич" in prompt
    assert "недоверенные ДАННЫЕ" in prompt


@pytest.mark.asyncio
async def test_an_impossible_relation_is_not_worth_asking_about(storage):
    """Связь, невозможную по видам концов, модель не спрашивают вовсе.

    Ответ документа тут ничего не решает: «человек состоит в человеке» неверно
    при любом тексте. Замер на живой очереди — 67 таких кандидатов из 597.
    """

    graph = KnowledgeGraph(storage)
    people = {
        name: str(graph.create_entity("alice", name, EntityType.PERSON)["id"])
        for name in ("Марухненко Иван Михайлович", "Котельников Олег Сергеевич")
    }
    raw = RawObject(new_id("raw"), "alice", "test", new_id("ref"), _ROSTER, "text")
    storage.store_raw_object(raw)
    document = KnowledgeObject(new_id("ko"), "alice", raw.id, content=_ROSTER, title="Расчёт")
    storage.store_knowledge_object(document)
    for entity_id in people.values():
        graph.link_knowledge_to_entity(document.id, entity_id, "alice")
    storage.store_relation_candidate(
        "alice",
        people["Марухненко Иван Михайлович"],
        people["Котельников Олег Сергеевич"],
        "member_of",
        confidence=0.9,
        evidence={
            "knowledge_object_id": document.id,
            "excerpt": "Водитель | рядовой | Котельников Олег Сергеевич",
            "method": "document_structure_arbiter",
        },
    )
    model = _Model({"verdict": "подтверждаю", "about": "", "reason": "не должно быть спрошено"})

    result = await graph.review_relation_candidates("alice", llm=model, apply=True)

    assert result["reject"] == 1
    assert model.calls == []
    assert result["verdicts"][0]["checked_by"] == "structure"
