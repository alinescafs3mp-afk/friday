"""Родство извлекается только там, где оно ОБЪЯВЛЕНО словом рядом с именем.

Задача #68: в графе 4610 сущностей и ноль связей. Обратный проход по всем 1533
документам архива дал НОЛЬ кандидатов, и замер объяснил почему:

    объявляющее слово (любое из прежних)      52 док.   3%
    «назначить на должность»                   8 док.   0%
    «состоит в должности»                      0 док.   0%
    «зачислить в списки»                      32 док.   2%
    «супруга/сын/дочь/отец/мать/брат/…»      186 док.  12%   <- единственное живое

Корпус — анкеты, приказы и списки личного состава. Служебные отношения в нём
словами не утверждаются; родственные — утверждаются, и чаще всего прочего.

Дальше три проверки, и каждая появилась ПОСЛЕ того, как замер показал мусор:

  * 509 кандидатов без фильтра по типу, и в выборке из двадцати восемь были вида
    «Изобильный -> Москва | слово: Брат» — два города в родстве;
  * 243 кандидата с фильтром по типу, и половина сцепляла людей из РАЗНЫХ анкет:
    родственные слова стоят в заголовке поля бланка («22. Родители (ФИО, дата
    рождения, где проживает…)»), а заголовок попадает между чужими друг другу
    именами;
  * 98 кандидатов с требованием близости, из них четыре — от слова «внук», и все
    четыре пришли из одного списка позывных: «Рядовой Нечипоренко Алексей Юрьевич
    (АВ-689922) Внук, Рядовой Азанов Иван Борисович (АБ-745975) Горный».

Итог: 94 кандидата на 56 документах, около 85% верных на глаз. Это очередь на
подтверждение человеком, а не готовые связи.
"""

from __future__ import annotations

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, KnowledgeObject, RawObject, RelationType


@pytest.fixture
def graph(storage):
    storage.ensure_user("alice", preset_key="admin")
    return KnowledgeGraph(storage)


def _document(storage, text: str) -> str:
    raw = RawObject(
        id="raw-1",
        user_id="alice",
        source="upload",
        source_ref="анкета",
        raw_content=text,
        content_type="file",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id="ko-1",
        user_id="alice",
        raw_object_id=raw.id,
        content=text,
        title="Анкета",
        knowledge_kind="document",
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


def _entity(storage, name: str, entity_type: str = "person") -> str:
    entity = Entity(
        id=f"ent-{abs(hash(name)) % 10**8}",
        user_id="alice",
        name=name,
        entity_type=entity_type,
    )
    storage.create_entity(entity)
    return entity.id


def _link(storage, ko_id: str, entity_id: str) -> None:
    storage.link_knowledge_entity("alice", ko_id, entity_id, status="accepted")


def test_a_declared_relative_becomes_a_candidate(graph, storage) -> None:
    """«Отец Горбунов Иван Алексеевич» — слово стоит прямо перед именем."""
    text = "Мать Горбунова Ирина Вячеславовна 30.05.1974. Отец Горбунов Иван Алексеевич 27.09.1974."
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Горбунова Ирина Вячеславовна"))
    _link(storage, ko_id, _entity(storage, "Горбунов Иван Алексеевич"))

    made = graph.suggest_relations_for_knowledge("alice", ko_id)

    assert made, "объявленное родство не найдено"
    assert made[0]["relation_type"] == RelationType.FAMILY_OF.value


def test_two_cities_are_never_relatives(graph, storage) -> None:
    """Мутация: убрать проверку типа — «Изобильный -> Москва | Брат» вернётся.

    Город не может быть ничьим братом, как бы ни был написан документ. Улика
    структурная и потому надёжнее любого уточнения словаря.
    """
    text = "Изобильный Ставропольский край. 23. Брат, сестра, близкие (ФИО) Москва"
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Изобильный", "location"))
    _link(storage, ko_id, _entity(storage, "Москва", "location"))

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_a_person_and_a_city_are_not_relatives(graph, storage) -> None:
    """Половина пары тоже должна быть человеком: «Глухова -> Кемерово | Супруга»."""
    text = "Глухова Нина Евгеньевна 89832158116. 20. Супруга (ФИО) Кемерово"
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Глухова Нина Евгеньевна"))
    _link(storage, ko_id, _entity(storage, "Кемерово", "location"))

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_a_form_heading_does_not_join_strangers(graph, storage) -> None:
    """Мутация: снять требование близости — чужие люди снова окажутся роднёй.

    В бланке «22. Родители (ФИО, дата рождения, где проживает, где работает,
    абонентский номер телефона)» слово стоит в ЗАГОЛОВКЕ поля, а следующее имя
    принадлежит уже другой анкете.
    """
    # Заголовок взят из архива дословно — в нём есть и «Брат», и «сестра», то
    # есть слова из словаря. Первая редакция теста брала заголовок «22. Родители
    # (…)», где ни одного словарного слова нет, и мутацию «снять требование
    # близости» НЕ ловила: совпадения не было вовсе, и тест был пустым.
    text = (
        "Титова Дарья Сергеевна 22.06.1991. "
        "23. Брат, сестра, близкие (ФИО, дата рождения, где проживает, "
        "где работает, абонентский номер телефона) НЕТ. "
        "Долженков Роман Сергеевич +79259001634"
    )
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Титова Дарья Сергеевна"))
    _link(storage, ko_id, _entity(storage, "Долженков Роман Сергеевич"))

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_the_word_right_before_the_name_still_counts(graph, storage) -> None:
    """Обратная сторона той же проверки: близкое слово принимается.

    Иначе требование близости можно было бы «выполнить», перестав находить
    вообще что-либо, — и тест выше остался бы зелёным.
    """
    text = "Мать: Комогорова Екатерина Михайловна 09.07.1954. Отец: Комогоров Виктор Леонидович"
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Комогорова Екатерина Михайловна"))
    _link(storage, ko_id, _entity(storage, "Комогоров Виктор Леонидович"))

    made = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert made, "слово стоит вплотную к имени, а связь не найдена"


def test_a_call_sign_is_not_a_grandson(graph, storage) -> None:
    """«Внук» в списке личного состава — позывной, и слова этого в словаре нет."""
    from friday.knowledge_graph import _RELATION_PHRASES

    for phrase, relation_type, _, _ in _RELATION_PHRASES:
        if relation_type is RelationType.FAMILY_OF:
            assert not phrase.search("Внук"), "дальняя родня вернулась в словарь"
            assert not phrase.search("племянник")
            assert phrase.search("Супруга"), "словарь потерял то, ради чего заведён"


def test_the_words_that_matter_are_all_there(graph, storage) -> None:
    """Ровно те поля, что спрашивает бланк: супруга, дети, родители, брат с сестрой."""
    from friday.knowledge_graph import _RELATION_PHRASES

    family = [p for p, t, _, _ in _RELATION_PHRASES if t is RelationType.FAMILY_OF]
    assert family, "родственные связи больше не извлекаются"
    for word in ("Супруга", "Жена", "Муж", "Сын", "Дочь", "Отец", "Мать", "Мама", "Брат", "Сестра"):
        assert any(p.search(word) for p in family), f"«{word}» выпало из словаря"


def test_the_relation_still_needs_two_different_people(graph, storage) -> None:
    """Одно имя, названное дважды, роднёй себе не приходится."""
    text = "Супруга Иванова Ольга Петровна. Супруга Иванова Ольга Петровна снова."
    ko_id = _document(storage, text)
    _link(storage, ko_id, _entity(storage, "Иванова Ольга Петровна"))

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []
