"""Починка правила действует только вперёд — узлы прежней редакции остаются.

И не просто остаются: сопоставление упоминаний работает от СУЩЕСТВУЮЩИХ имён, поэтому
однажды заведённая ошибка продолжает собирать привязки. На живом архиве владельца узел
«Викторович» (отчество, принятое правилом за имя сервера) родился на ОДНОМ документе и
набрал 314 привязок в документах про разных людей — концентратор из ничего.

Проход `prune-entities` не знает никаких списков мусора и не судит имя. Он задаёт ровно
один вопрос: породили бы сегодняшние правила это имя хоть на одном документе архива?

Что здесь проверяется — не «удаляет ли он» (это тривиально), а три запрета, каждый из
которых стирал бы чужое решение молча.
"""

from __future__ import annotations

import argparse
import hashlib

from friday.storage.models import EntityType, KnowledgeObject, RawObject, new_id

BODY = "Приказом назначен Петров Иван Иванович. Уважаемая Вера Андреевна, ознакомьтесь."


def _store(storage, user_id: str, text: str, title: str = "Приказ") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _stale_node(
    storage,
    user_id: str,
    ko_id: str,
    name: str,
    *,
    created_by: str = "ingestion",
    method: str | None = "explicit_person_patronymic",
    entity_type: EntityType = EntityType.PERSON,
) -> str:
    """Узел, который сегодняшние правила уже не порождают, — как из прежней редакции."""
    from friday.knowledge_graph import KnowledgeGraph

    graph = KnowledgeGraph(storage)
    metadata: dict[str, object] = {"created_by": created_by}
    if method is not None:
        metadata["extraction_method"] = method
    entity = graph.create_entity(user_id, name, entity_type, metadata=metadata)
    graph.link_knowledge_to_entity(
        ko_id,
        str(entity["id"]),
        user_id,
        confidence=0.9,
        evidence={"method": "explicit_person_patronymic"},
        status="accepted",
    )
    return str(entity["id"])


def _run(**overrides) -> int:
    from friday.cli import _prune_entities

    args = argparse.Namespace(user=None, batch=50, limit=0, apply=True)
    for key, value in overrides.items():
        setattr(args, key, value)
    return _prune_entities(args)


def _alive(storage, user_id: str) -> set[str]:
    return {str(row["name"]) for row in storage.list_entities(user_id, limit=200)}


def test_a_node_no_rule_produces_any_more_is_removed(settings, storage):
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _stale_node(storage, "alice", ko_id, "Уважаемая Вера Андреевна")

    assert "Уважаемая Вера Андреевна" in _alive(storage, "alice")
    assert _run() == 0
    assert "Уважаемая Вера Андреевна" not in _alive(storage, "alice")


def test_a_node_the_rules_still_produce_stays(settings, storage):
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _stale_node(storage, "alice", ko_id, "Петров Иван Иванович")

    assert _run() == 0
    assert "Петров Иван Иванович" in _alive(storage, "alice")


def test_the_pass_writes_nothing_without_apply(settings, storage):
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _stale_node(storage, "alice", ko_id, "Уважаемая Вера Андреевна")

    assert _run(apply=False) == 0
    assert "Уважаемая Вера Андреевна" in _alive(storage, "alice")


def test_a_node_a_person_named_is_not_touched(settings, storage):
    """Названное человеком или выведенное арбитром правилами не проверяется.

    Мутация, которую тест обязан ловить: убрать проверку происхождения. Тогда проход
    снесёт всё, чего нет в тексте документов, — в том числе узлы, которые владелец
    завёл руками, и всё, что вывел арбитр из формы документа.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _stale_node(storage, "alice", ko_id, "Тайный Знакомый Иванович", created_by="human", method=None)

    assert _run() == 0
    assert "Тайный Знакомый Иванович" in _alive(storage, "alice")


def test_the_pass_that_created_the_node_may_have_been_renamed(settings, storage):
    """Происхождение узла спрашивается у МЕТОДА, а не у имени прохода.

    Найдено первым же прогоном по живому графу: 4349 узлов-людей заведены проходом
    под прежним именем `backfill_person_entities`, которого не было в списке «своих».
    Вся людская половина графа молча считалась чужой — а проход при этом отчитывался
    как отработавший, и по числам это выглядело правдоподобно.

    Мутация, которую тест обязан ловить: вернуть проверку на имя прохода.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _stale_node(
        storage,
        "alice",
        ko_id,
        "Уважаемая Вера Андреевна",
        created_by="backfill_person_entities",
    )

    assert _run() == 0
    assert "Уважаемая Вера Андреевна" not in _alive(storage, "alice")


def test_a_weak_guess_does_not_save_a_node_a_strong_rule_abandoned(settings, storage):
    """Сверять надо с ТЕМ ЖЕ правилом, что завело узел.

    «Курган Курганская» пережил первую редакцию прохода: правило про город больше
    не берёт хвост адреса, но слабое правило «два слова с заглавной» выдаёт ту же
    строку кандидатом в люди. Мусорное место спряталось за догадкой о человеке.

    Мутация, которую тест обязан ловить: сверять имя со всеми правилами разом.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", "Проживает в городе Курган Курганская область.")
    _stale_node(
        storage,
        "alice",
        ko_id,
        "Курган Курганская",
        method="explicit_location_marker",
        entity_type=EntityType.LOCATION,
    )

    assert _run() == 0
    # Проход только сносит и ничего не заводит: настоящий «Курган» появится
    # следующим приёмом или проходом `backfill-entities`, а не здесь.
    assert "Курган Курганская" not in _alive(storage, "alice")


def test_a_name_another_strong_rule_still_declares_is_kept(settings, storage):
    """Метод узла мог смениться на более уверенный — это не повод сносить."""
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", "Приказом назначен Петров Иван Иванович.")
    _stale_node(storage, "alice", ko_id, "Петров Иван Иванович", method="capitalized_person_name")

    assert _run() == 0
    assert "Петров Иван Иванович" in _alive(storage, "alice")


def test_a_link_the_owner_reviewed_saves_the_node(settings, storage):
    """Решение человека — вершина: разовый проход с ним не спорит.

    Мутация, которую тест обязан ловить: убрать `entity_links_touched_by_a_person`.
    Тогда узел, привязку которого владелец посмотрел глазами, исчезнет молча, а
    счётчики прохода останутся правдоподобными.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    entity_id = _stale_node(storage, "alice", ko_id, "Уважаемая Вера Андреевна")
    link = next(
        row
        for row in storage.list_knowledge_entity_links("alice", knowledge_object_id=ko_id)
        if str(row["entity_id"]) == entity_id
    )
    storage.set_knowledge_entity_link_status(str(link["id"]), "alice", "accepted", reviewed_by="alice")

    assert _run() == 0
    assert "Уважаемая Вера Андреевна" in _alive(storage, "alice")


def test_half_the_corpus_may_not_decide_the_whole_graph(settings, storage):
    """`--limit` даёт неполный список порождённых имён — удалять по нему нельзя.

    Иначе прогон «посмотреть на сотне документов» объявил бы мусором всё, что
    объявлено в оставшихся полутора тысячах.
    """
    storage.ensure_user("alice")
    ko_id = _store(storage, "alice", BODY)
    _store(storage, "alice", "Второй документ: Сидоров Пётр Никифорович.", title="Второй")
    _stale_node(storage, "alice", ko_id, "Уважаемая Вера Андреевна")

    assert _run(limit=1) == 2
    assert "Уважаемая Вера Андреевна" in _alive(storage, "alice")
