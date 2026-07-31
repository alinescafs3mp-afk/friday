"""Таблица `relations` была пуста ВСЕГДА, и код при этом был рабочий.

Путь от текста до строки в `relations` цел целиком: извлечение → предложение →
подтверждение человеком. Пусто было по двум причинам, обе — про условия, а не про
поломку.

**Окно в 160 символов.** Замер на 400 настоящих документах владельца (кандидаты
извлекателя вместо связей, медиана 8 на документ):

    окно  вхождения  связей  документов
     160  первое          2           1     <- как было
     400  все            26           8     <- стало
    1000  все            58          20     <- отвергнуто: это страница, не абзац

Фраза-связка встречается хотя бы раз в 141 документе из 400 — словарь ни при чём,
связывало именно окно.

**Утверждение связи человеком никуда не возвращалось.** Предложения считались один
раз, при рождении объекта, по связям, которые автомат принял сам. Всё, что
подтвердил владелец при разборе, для поиска связей не существовало никогда.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.knowledge_graph import _RELATION_SPAN_CHARS, KnowledgeGraph
from jericho.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id

# Два упоминания на расстоянии, типичном для абзаца рабочего документа: между ними
# помещается связка, но они не стоят вплотную.
FILLER = "и далее по тексту приводятся уточнения к порядку работ, "


def _document(storage, user_id: str, text: str) -> str:
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
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title="Документ",
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _linked_entity(storage, kg, user_id: str, ko_id: str, name: str, *, status: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=EntityType.CONCEPT)
    storage.create_entity(entity)
    # `status` передаётся ЯВНО: по умолчанию `link_knowledge_to_entity` создаёт
    # связь уже утверждённой, и стенд, полагающийся на дефолт, проверял бы не то,
    # что написано в его названии.
    kg.link_knowledge_to_entity(ko_id, entity.id, user_id, confidence=0.9, evidence={}, status=status)
    return entity.id


@pytest.fixture
def graph(storage):
    storage.ensure_user("alice")
    return KnowledgeGraph(storage)


def test_a_relation_across_a_paragraph_is_found(storage, graph):
    """Раньше эта же пара давала ноль: между упоминаниями больше 160 символов."""
    text = f"Сервис Атлас {FILLER * 3} использует {FILLER * 2} базу Полярис для хранения."
    assert 160 < text.index("Полярис") - text.index("Атлас") < _RELATION_SPAN_CHARS

    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "связь через абзац по-прежнему не находится"


def test_a_relation_across_a_page_is_still_refused(storage, graph):
    """1000 символов удвоили бы выдачу и НЕ взяты: это страница, а не абзац.

    Каждое предложение стоит человеку решения, а очередь разбора и так велика.
    """
    text = f"Сервис Атлас {FILLER * 12} использует {FILLER * 12} базу Полярис."
    assert text.index("Полярис") - text.index("Атлас") > _RELATION_SPAN_CHARS

    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_every_occurrence_counts_not_only_the_first(storage, graph):
    """Какое упоминание оказалось первым — случайность записи, а не факт о связи."""
    text = (
        "Полярис упоминается в начале документа. "
        + FILLER * 8
        + "Сервис Атлас использует базу Полярис для хранения."
    )
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, (
        "по первому вхождению «Полярис» стоит в начале, далеко от «Атлас» — "
        "и пара терялась, хотя рядом во втором предложении она есть"
    )


def test_evidence_phrase_is_the_relation_verb_not_a_stray_entity_name(storage, graph):
    """Найдено состязательным ревью: `evidence["phrase"]` хранил `match.group(0)`, а
    `match` — не результат `phrase.search(between)` (эта переменная не привязана к
    результату вовсе), а последнее значение из БОЛЕЕ РАННЕГО цикла сбора упоминаний
    (`for match in pattern.finditer(text)`). Ревьюер видел имя сущности вместо глагола,
    оправдавшего связь.

    Мутация: убрать привязку `phrase_match = phrase.search(between)` и вернуть чтение
    из `match` — тест обязан покраснеть.
    """
    text = f"Сервис Атлас {FILLER * 2} использует {FILLER * 2} базу Полярис для хранения."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "проба проверяет не тот сценарий — связь не нашлась вовсе"
    evidence = json.loads(suggestions[0]["evidence_json"])
    phrase = evidence["phrase"]
    assert phrase.casefold() == "использует", (
        f"evidence.phrase должен быть глаголом связи, а не именем сущности: получено {phrase!r}"
    )


def test_a_phrase_without_two_linked_entities_proposes_nothing(storage, graph):
    """Совпадение фразы само по себе связью не является."""
    text = "Сервис Атлас использует внешнее хранилище неизвестного производителя."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_only_accepted_links_take_part(storage, graph):
    """Предложенная связь — ещё не факт о сущности; строить на ней связь нельзя."""
    text = "Сервис Атлас использует базу Полярис."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Полярис", status="suggested")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_accepting_a_link_reconsiders_the_relations(settings):
    """Самый качественный сигнал в системе — и единственный, что не возвращался.

    Предложения считались один раз, при рождении объекта, по связям, которые
    автомат принял сам. Подтверждённое человеком не участвовало никогда.
    """
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]

        text = "Сервис Атлас использует базу Полярис для хранения смет."
        ko_id = _document(storage, user_id, text)
        _linked_entity(storage, kg, user_id, ko_id, "Атлас", status="accepted")
        pending = _linked_entity(storage, kg, user_id, ko_id, "Полярис", status="suggested")

        assert kg.suggest_relations_for_knowledge(user_id, ko_id) == []
        link_id = next(
            row["id"]
            for row in storage.list_knowledge_entity_links(
                user_id, knowledge_object_id=ko_id, status=None, limit=50
            )
            if row["entity_id"] == pending
        )

        response = client.patch(
            f"/api/admin/entity-links/{link_id}",
            json={"user_id": user_id, "status": "accepted"},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        assert response.json()["relation_candidates"], (
            "после подтверждения связи человеком предложения не пересчитались"
        )
        assert storage.count_relation_candidates(user_id) > 0


def test_rejecting_a_link_proposes_nothing(settings):
    """Отклонение — не повод искать связи; и оно не должно стоить прохода по тексту."""
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        kg = app.state.kg
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]

        ko_id = _document(storage, user_id, "Сервис Атлас использует базу Полярис.")
        _linked_entity(storage, kg, user_id, ko_id, "Атлас", status="accepted")
        pending = _linked_entity(storage, kg, user_id, ko_id, "Полярис", status="suggested")
        link_id = next(
            row["id"]
            for row in storage.list_knowledge_entity_links(
                user_id, knowledge_object_id=ko_id, status=None, limit=50
            )
            if row["entity_id"] == pending
        )

        response = client.patch(
            f"/api/admin/entity-links/{link_id}",
            json={"user_id": user_id, "status": "rejected"},
            headers=owner,
        )
        assert response.status_code == 200
        assert response.json()["relation_candidates"] == []


def test_one_entity_mentioned_twice_is_not_a_relation_with_itself(storage, graph):
    """Стало возможным ровно тогда, когда я разрешил считать все вхождения.

    При одном вхождении пара из двух упоминаний всегда была двумя разными
    сущностями. Со всеми вхождениями «Атлас … использует … Атлас» даёт пару из
    одной и той же сущности, хранилище отвечает `Self-relation candidates are not
    allowed`, и разбор ВСЕГО документа падает пятисоткой — документ не продвигается
    вовсе. Найдено на массовом продвижении: одно падение на сотню документов.
    """
    text = "Сервис Атлас настроен. " + FILLER + " Поэтому Атлас использует Атлас для кэша."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Атлас", status="accepted")

    assert graph.suggest_relations_for_knowledge("alice", ko_id) == []


def test_hierarchy_phrase_reverses_direction_so_the_manager_is_the_source(storage, graph):
    """Найдено состязательным ревью перед демо (сценарий «4 начальника + 3
    подчинённых»): «X подчиняется Y» упоминает подчинённого ПЕРВЫМ, но связь
    MANAGES по смыслу должна начинаться с руководителя. Без разворота
    начальник записывался бы подчинённым собственного подчинённого.

    Мутация: убрать `phrase_reversed` (всегда `source, target = left, right`) —
    тест обязан покраснеть на перепутанном source/target.
    """
    # Mention finding in suggest_relations_for_knowledge matches the entity's
    # stored name as an exact substring (no declension handling — a separate,
    # already-tracked gap), so the linked name is given in the form it appears
    # in the text, same convention as the file's other tests.
    text = f"Иванов {FILLER} подчиняется {FILLER} Смирновой в вопросах отчётности."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Иванов", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Смирновой", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «подчиняется» не дала ни одного кандидата"
    candidate = suggestions[0]
    assert candidate["relation_type"] == "manages"
    evidence = json.loads(candidate["evidence_json"])
    assert evidence["source_name"] == "Смирновой", (
        "руководитель должен быть source: «Иванов подчиняется Смирновой» значит "
        "Смирнова управляет Ивановым, а не наоборот"
    )
    assert evidence["target_name"] == "Иванов"


def test_coordination_phrase_is_found_and_not_reversed(storage, graph):
    """Симметричная связь («сотрудничает с») — направление упоминания в тексте не
    меняет смысл, разворот не нужен. Проверяет, что фраза вообще матчится (была
    полностью отсутствующей до этого фикса)."""
    text = f"Отдел продаж {FILLER} координирует с {FILLER} отделом логистики поставки."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Отдел продаж", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "отделом логистики", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «координирует с» не дала ни одного кандидата"
    assert suggestions[0]["relation_type"] == "related_to"


@pytest.mark.parametrize(
    "text,left_name,right_name",
    [
        pytest.param(
            "Сервис Атлас интегрируется с платёжным шлюзом Стрела через REST API.",
            "Атлас",
            "Стрела",
            id="integrates_with",
        ),
        pytest.param(
            "Модуль авторизации взаимодействует с базой данных Полярис при каждом запросе.",
            "авторизации",
            "Полярис",
            id="interacts_with",
        ),
        pytest.param(
            "Компания Ромашка подписала контракт с поставщиком Вектор на поставку оборудования.",
            "Ромашка",
            "Вектор",
            id="signed_a_contract",
        ),
        pytest.param(
            "Vendor Northwind Traders supplies raw materials to Fabrikam Manufacturing.",
            "Northwind",
            "Fabrikam",
            id="supplies_to",
        ),
        pytest.param(
            "Секретариат направил письмо в бухгалтерию с просьбой согласовать смету.",
            "Секретариат",
            "бухгалтерию",
            id="forwarded_to",
        ),
        pytest.param(
            "The compliance office notified the finance department about the new regulation.",
            "compliance",
            "finance",
            id="notified",
        ),
        pytest.param(
            "Врач Соколова диагностировала пациента Волкова с гипертонией на плановом осмотре.",
            "Соколова",
            "Волкова",
            id="diagnosed",
        ),
        pytest.param(
            "Dr. Alvarez consults with Dr. Chen on complex oncology cases every Thursday.",
            "Alvarez",
            "Chen",
            id="consults_with",
        ),
        pytest.param(
            "The book club at Elmwood Library meets weekly with the local historian Ms. Grant.",
            "Elmwood",
            "Grant",
            id="meets_weekly_with",
        ),
    ],
)
def test_cross_domain_business_phrases_are_no_longer_missed(storage, graph, text, left_name, right_name):
    """Найдено состязательным ревью перед демо: тема содержимого команды заранее
    непредсказуема («разной тематики»), а словарь до этого фикса знал только
    шесть технических глаголов (использует/управляет/работает над/зависит от/
    часть/член) — на синтетике из деловой, административной и медицинской
    переписки 33 из 36 предложений с явной связью не давали НИ ОДНОГО
    кандидата. Каждый параметр здесь — предложение из той находки.

    Мутация: убрать межотраслевой блок `_RELATION_PHRASES` целиком — каждый
    из девяти случаев обязан покраснеть на пустом списке.
    """
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, left_name, status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, right_name, status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, f"не дало ни одного кандидата: {text!r}"
    assert suggestions[0]["relation_type"] == "related_to"


def test_coach_phrase_maps_to_manages_not_related_to(storage, graph):
    """«Тренирует»/«coaches» — не общий межотраслевой глагол, а тот же
    направленный смысл, что «руководит»/«leads»: тренер управляет командой.
    Проверяет, что фраза попала именно в существующую запись MANAGES, а не
    случайно совпала с более общим межотраслевым RELATED_TO-блоком."""
    text = f"Тренер Волков {FILLER} тренирует {FILLER} сборную клуба Метеор перед финалом."
    ko_id = _document(storage, "alice", text)
    _linked_entity(storage, graph, "alice", ko_id, "Волков", status="accepted")
    _linked_entity(storage, graph, "alice", ko_id, "Метеор", status="accepted")

    suggestions = graph.suggest_relations_for_knowledge("alice", ko_id)
    assert suggestions, "фраза «тренирует» не дала ни одного кандидата"
    assert suggestions[0]["relation_type"] == "manages"
