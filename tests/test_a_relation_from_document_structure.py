"""Связь, объявленная формой документа, и проверки, без которых ей нельзя верить.

Фразовый извлекатель связывает объявляющее слово с СОСЕДНИМ именем, а форма
служебного документа объявляет отношения СУБЪЕКТА: пункта списка, поля анкеты,
строки ведомости. Замерено на архиве владельца: в рапорте из восьми предложенных
пар верны три, и те случайно — субъект оказался ближайшим слева.

Арбитр читает форму, но за арбитром надо проверять: он ссылается на номера из
переданного списка и обязан привести дословную выдержку. Ниже — что происходит,
когда он ошибается или выдумывает.
"""

from __future__ import annotations

import json

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id

_REPORT = (
    "Командиру в/ч 30926\n"
    "Рапорт\n"
    "1. Прапорщику Кублику Александру Юрьевичу, Э-465806.\n"
    "Контактные телефоны:\n"
    "Супруга: Варламова Ольга Васильевна: +79992759780\n"
    "Брат: Макаров Кирилл Евгеньевич: +79182352277\n"
)


class _Model:
    """Заглушка модели: отдаёт заранее заданный ответ на каждое окно."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[list[dict[str, object]]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {"content": content, "finish_reason": "stop"}


def _document(storage, user_id: str, text: str) -> KnowledgeObject:
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), text, "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content=text, title="Рапорт")
    storage.store_knowledge_object(ko)
    return ko


def _linked_report(storage) -> tuple[KnowledgeObject, dict[str, str]]:
    graph = KnowledgeGraph(storage)
    people = {}
    for name in (
        "Кублик Александр Юрьевич",
        "Варламова Ольга Васильевна",
        "Макаров Кирилл Евгеньевич",
    ):
        people[name] = str(graph.create_entity("alice", name, EntityType.PERSON)["id"])
    unit = str(graph.create_entity("alice", "в/ч 30926", EntityType.ORGANIZATION)["id"])
    people["в/ч 30926"] = unit
    document = _document(storage, "alice", _REPORT)
    for entity_id in people.values():
        graph.link_knowledge_to_entity(document.id, entity_id, "alice")
    return document, people


def _index_of(model_messages: list[dict[str, object]], name: str) -> int:
    """Номер сущности в списке, который арбитр реально увидел."""

    listing = str(model_messages[1]["content"])
    for line in listing.splitlines():
        if name in line:
            return int(line.split(".", 1)[0])
    raise AssertionError(f"{name} не попал в промпт арбитра")


@pytest.mark.asyncio
async def test_the_relation_binds_the_subject_of_the_clause(storage):
    document, people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    # Первый прогон нужен, чтобы узнать нумерацию, которую увидит арбитр.
    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    subject = _index_of(probe.calls[0], "Кублик Александр Юрьевич")
    brother = _index_of(probe.calls[0], "Макаров Кирилл Евгеньевич")

    model = _Model(
        {
            "subject": "Кублик Александр Юрьевич",
            "relations": [
                {
                    "source": subject,
                    "target": brother,
                    "type": "family_of",
                    "quote": "Брат: Макаров Кирилл Евгеньевич",
                    "confidence": 0.8,
                }
            ],
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)

    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["status"] == "suggested"
    assert candidate["source_entity_id"] == people["Кублик Александр Юрьевич"]
    assert candidate["target_entity_id"] == people["Макаров Кирилл Евгеньевич"]
    evidence = json.loads(candidate["evidence_json"])
    assert evidence["method"] == "document_structure_arbiter"
    assert evidence["excerpt"] == "Брат: Макаров Кирилл Евгеньевич"


@pytest.mark.asyncio
async def test_an_invented_quote_is_rejected(storage):
    document, _people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    subject = _index_of(probe.calls[0], "Кублик Александр Юрьевич")
    spouse = _index_of(probe.calls[0], "Варламова Ольга Васильевна")

    model = _Model(
        {
            "relations": [
                {
                    "source": subject,
                    "target": spouse,
                    "type": "family_of",
                    # Правдоподобно и звучит как документ, но в тексте этого нет.
                    "quote": "Состоит в браке с Варламовой Ольгой Васильевной",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)

    assert result["candidates"] == []
    assert result["rejected"] == 1


@pytest.mark.asyncio
async def test_a_number_outside_the_listing_is_rejected(storage):
    document, _people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    model = _Model(
        {
            "relations": [
                {
                    "source": 1,
                    "target": 99,
                    "type": "member_of",
                    "quote": "Командиру в/ч 30926",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []
    assert result["rejected"] == 1


@pytest.mark.asyncio
async def test_an_unlisted_relation_type_is_rejected(storage):
    document, _people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    subject = _index_of(probe.calls[0], "Кублик Александр Юрьевич")
    spouse = _index_of(probe.calls[0], "Варламова Ольга Васильевна")

    # `related_to` не входит в разрешённые намеренно: «как-то связаны» не несёт
    # сведений сверх того, что оба названы в одном документе, а решение человека
    # стоит столько же, сколько решение по содержательной связи.
    model = _Model(
        {
            "relations": [
                {
                    "source": subject,
                    "target": spouse,
                    "type": "related_to",
                    "quote": "Супруга: Варламова Ольга Васильевна",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_kinship_is_refused_between_a_person_and_a_place(storage):
    document, _people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    subject = _index_of(probe.calls[0], "Кублик Александр Юрьевич")
    unit = _index_of(probe.calls[0], "в/ч 30926")

    model = _Model(
        {
            "relations": [
                {
                    "source": subject,
                    "target": unit,
                    "type": "family_of",
                    "quote": "Командиру в/ч 30926",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_a_truncated_pass_says_how_much_it_did_not_read(storage):
    # Молча разобранная часть документа читается как «в нём больше ничего нет».
    graph = KnowledgeGraph(storage)
    first = str(graph.create_entity("alice", "Кублик Александр Юрьевич", EntityType.PERSON)["id"])
    second = str(graph.create_entity("alice", "Варламова Ольга Васильевна", EntityType.PERSON)["id"])
    long_text = (_REPORT + "\n") * 400
    document = _document(storage, "alice", long_text)
    graph.link_knowledge_to_entity(document.id, first, "alice")
    graph.link_knowledge_to_entity(document.id, second, "alice")

    result = await graph.suggest_relations_from_structure(
        "alice", document.id, llm=_Model({"relations": []})
    )
    assert result["windows"] == 8
    assert result["windows_skipped"] > 0


@pytest.mark.asyncio
async def test_a_blank_field_heading_does_not_justify_a_relation(storage):
    """Заголовок поля анкеты стоит между любыми двумя именами и ничего не значит.

    «22. Родители (ФИО, дата рождения, где проживает…)» — это подпись графы
    бланка. Она честно встречается в тексте, поэтому буквальную сверку выдержки
    проходит безупречно, а подтверждает ровно ничего. На живом архиве такими
    заголовками были обоснованы одиннадцать из первых 64 принятых связей.
    """

    graph = KnowledgeGraph(storage)
    text = (
        "1. Фамилия, имя, отчество | Храмов Иван Александрович\n"
        "22. Родители (ФИО, дата рождения, где проживает, где работает, телефон)\n"
        "Чурилов Александр Вячеславович 12.03.1970\n"
    )
    subject = str(graph.create_entity("alice", "Храмов Иван Александрович", EntityType.PERSON)["id"])
    other = str(graph.create_entity("alice", "Чурилов Александр Вячеславович", EntityType.PERSON)["id"])
    document = _document(storage, "alice", text)
    graph.link_knowledge_to_entity(document.id, subject, "alice")
    graph.link_knowledge_to_entity(document.id, other, "alice")

    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    first = _index_of(probe.calls[0], "Храмов Иван Александрович")
    second = _index_of(probe.calls[0], "Чурилов Александр Вячеславович")

    model = _Model(
        {
            "relations": [
                {
                    "source": first,
                    "target": second,
                    "type": "family_of",
                    "quote": "22. Родители (ФИО, дата рождения, где проживает, где работает, телефон)",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []
    assert result["rejected"] == 1


@pytest.mark.asyncio
async def test_a_quote_about_a_third_person_does_not_justify_the_pair(storage):
    # «Комогоров Дмитрий → Комогорова Екатерина» при выдержке «Отец Комогоров
    # Виктор Леонидович»: выдержка настоящая, но она про третьего человека.
    graph = KnowledgeGraph(storage)
    text = (
        "1. Фамилия, имя, отчество | Комогоров Дмитрий Викторович\n"
        "Мать Комогорова Екатерина Михайловна\n"
        "Отец Комогоров Виктор Леонидович\n"
    )
    subject = str(graph.create_entity("alice", "Комогоров Дмитрий Викторович", EntityType.PERSON)["id"])
    mother = str(graph.create_entity("alice", "Комогорова Екатерина Михайловна", EntityType.PERSON)["id"])
    father = str(graph.create_entity("alice", "Комогоров Виктор Леонидович", EntityType.PERSON)["id"])
    document = _document(storage, "alice", text)
    for entity_id in (subject, mother, father):
        graph.link_knowledge_to_entity(document.id, entity_id, "alice")

    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    son = _index_of(probe.calls[0], "Комогоров Дмитрий Викторович")
    wrong_target = _index_of(probe.calls[0], "Комогорова Екатерина Михайловна")

    model = _Model(
        {
            "relations": [
                {
                    "source": son,
                    "target": wrong_target,
                    "type": "family_of",
                    "quote": "Отец Комогоров Виктор Леонидович",
                    "confidence": 0.9,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_a_dry_pass_writes_nothing(storage):
    """Разбор, объявленный сухим, обязан быть сухим.

    Иначе последующая сверка находит СВОИ же предложения и принимает их как
    независимое подтверждение. Поймано на живом архиве: сверка показала
    «принять 22», а записала 64.
    """

    document, _people = _linked_report(storage)
    graph = KnowledgeGraph(storage)
    probe = _Model({"relations": []})
    await graph.suggest_relations_from_structure("alice", document.id, llm=probe)
    subject = _index_of(probe.calls[0], "Кублик Александр Юрьевич")
    brother = _index_of(probe.calls[0], "Макаров Кирилл Евгеньевич")

    model = _Model(
        {
            "relations": [
                {
                    "source": subject,
                    "target": brother,
                    "type": "family_of",
                    "quote": "Брат: Макаров Кирилл Евгеньевич",
                    "confidence": 0.8,
                }
            ]
        }
    )
    result = await graph.suggest_relations_from_structure(
        "alice", document.id, llm=model, store=False
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["status"] == "not_stored"
    assert storage.count_relation_candidates("alice", status="suggested") == 0


@pytest.mark.asyncio
async def test_a_document_with_one_entity_is_not_sent_to_the_model(storage):
    graph = KnowledgeGraph(storage)
    only = str(graph.create_entity("alice", "Кублик Александр Юрьевич", EntityType.PERSON)["id"])
    document = _document(storage, "alice", _REPORT)
    graph.link_knowledge_to_entity(document.id, only, "alice")

    model = _Model({"relations": []})
    result = await graph.suggest_relations_from_structure("alice", document.id, llm=model)
    assert result["candidates"] == []
    assert model.calls == []
