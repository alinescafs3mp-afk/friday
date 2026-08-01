"""Первый кусок спеки v3 (OPUS_INTEGRATED_SYSTEM_AUDIT_PROMPT.md §6): «вид объекта».

Открыл сущность — видишь связанные документы, теги по ним, честный диапазон
дат (даты ДОКУМЕНТОВ, не загрузки) и сколько связей ждёт проверки человеком,
не только уже подтверждённые. Расширяет существующий инструмент agent'а
entity_lookup — не новый параллельный инструмент, продолжение того же шва.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer


def _document(
    storage,
    user_id: str,
    text: str,
    *,
    tags: list[str],
    document_date: str | None,
    importance: float = 0.5,
) -> str:
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
        tags_json=tags,
        importance=importance,
        metadata_json=({"document_date": document_date} if document_date else {}),
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _linked_entity(
    storage, kg, user_id: str, name: str, *, entity_type: EntityType = EntityType.PROJECT
) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type=entity_type)
    storage.create_entity(entity)
    return entity.id


@pytest.fixture
def kernel(settings, storage):
    storage.ensure_user("alice")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    built = ExecutionKernel(auth, settings)
    built.bind_services(storage, graph, web, ingestion)
    return built, auth, storage, graph


@pytest.mark.asyncio
async def test_entity_profile_aggregates_tags_and_date_range(kernel):
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(
        storage,
        "alice",
        "Отчёт по проекту Атлас за январь.",
        tags=["отчёт", "квартал"],
        document_date="2026-01-15",
    )
    ko2 = _document(storage, "alice", "Смета по проекту Атлас.", tags=["смета"], document_date="2026-03-02")
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko2, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert result.success is True
    profile = result.data["profile"]
    assert profile["tags"] == ["квартал", "отчёт", "смета"]
    assert profile["document_date_range"] == {"earliest": "2026-01-15", "latest": "2026-03-02"}
    assert profile["documents_without_own_date"] == 0


@pytest.mark.asyncio
async def test_entity_profile_counts_documents_without_their_own_date_separately(kernel):
    """Найдено собственным правилом проекта: дата документа и дата загрузки —
    разные факты, и путать их запрещено с давних пор (см. историю про
    document_date). Документ без document_date не должен молча выпадать из
    диапазона ИЛИ подменяться датой загрузки — он должен быть честно посчитан
    отдельно.

    Мутация: убрать ветку `else: undated_count += 1` — тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(storage, "alice", "Документ с датой.", tags=[], document_date="2026-01-15")
    ko2 = _document(storage, "alice", "Документ без даты.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko2, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    profile = result.data["profile"]
    assert profile["document_date_range"] == {"earliest": "2026-01-15", "latest": "2026-01-15"}
    assert profile["documents_without_own_date"] == 1


@pytest.mark.asyncio
async def test_entity_profile_reports_no_date_range_when_nothing_is_dated(kernel):
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko1 = _document(storage, "alice", "Без даты первый.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko1, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    profile = result.data["profile"]
    assert profile["document_date_range"] is None
    assert profile["documents_without_own_date"] == 1


@pytest.mark.asyncio
async def test_entity_profile_summary_covers_every_document_not_just_the_shown_page(kernel):
    """Сводка карточки считается по ВСЕМ документам сущности, а показанный
    список — страница (сегодня 10 штук, отсортированных по важности).

    Пока сводку выводили из этой самой страницы, карточка утверждала про сущность
    то, что верно лишь для её десяти самых важных документов. Замерено на копии
    боевой базы, а не предположено: из 200 сущностей с наибольшим числом
    документов у 93 диапазон дат был неверным (худший край — мимо на 13 лет),
    у всех 200 занижено число документов, объединение тегов теряло медианно 9
    тегов (максимум 329).

    Стенд повторяет ровно этот случай: крайние по датам документы наименее важны,
    поэтому в страницу они не попадают.

    Мутация: считать сводку от `knowledge_objects` (как было) — тест обязан
    покраснеть на диапазоне, тегах и на `knowledge_objects_total`.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    # Десять «важных» документов середины диапазона — ровно страница.
    for index in range(10):
        ko = _document(
            storage,
            "alice",
            f"Важный документ {index}.",
            tags=["середина"],
            document_date=f"2026-06-{index + 1:02d}",
            importance=0.9,
        )
        graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")
    # Края диапазона и уникальные теги — на документах с низкой важностью.
    oldest = _document(
        storage, "alice", "Самый ранний.", tags=["архив"], document_date="2011-02-03", importance=0.1
    )
    newest = _document(
        storage, "alice", "Самый поздний.", tags=["свежее"], document_date="2026-12-31", importance=0.1
    )
    undated = _document(storage, "alice", "Без даты.", tags=[], document_date=None, importance=0.1)
    for ko_id in (oldest, newest, undated):
        graph.link_knowledge_to_entity(ko_id, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    profile = result.data["profile"]
    assert profile["document_date_range"] == {"earliest": "2011-02-03", "latest": "2026-12-31"}
    assert "архив" in profile["tags"] and "свежее" in profile["tags"]
    assert profile["documents_without_own_date"] == 1
    assert result.data["knowledge_objects_total"] == 13
    assert len(result.data["knowledge_objects"]) == 10, "показанный список остаётся страницей"


@pytest.mark.asyncio
async def test_entity_profile_lists_documents_without_carrying_their_bodies(kernel):
    """Карточка перечисляет документы, а не показывает их текст.

    Пока список брался как `k.*`, в ответ попадало полное содержимое каждого
    документа: замерено 2.4–4.9 МБ на одну карточку боевого корпуса, из которых
    читателю нужны заголовки. Та же тяжесть уходила в контекст модели через
    `entity_lookup` и там всё равно обрезалась на 11 900 знаках — байты не
    покупали ничего, но вытесняли начало списка.

    Мутация: вернуть `get_entity_knowledge` (то есть `k.*`) — тест обязан
    покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    body = "Секретное тело документа. " * 200
    ko = _document(storage, "alice", body, tags=["отчёт"], document_date="2026-01-15")
    graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    shown = result.data["knowledge_objects"]
    assert len(shown) == 1
    assert "content" not in shown[0], "тело документа в карточке не нужно и не должно уезжать модели"
    assert shown[0]["title"] == "Документ", "заголовок — то, ради чего список существует"
    assert "raw_content" not in shown[0]
    assert body not in json.dumps(result.data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_entity_profile_counts_pending_relations_separately_from_confirmed(kernel):
    """Подтверждённые связи (`relations`) и ожидающие проверки (`pending_relations_count`)
    — разные вещи. Карточка сущности, показывающая только подтверждённые,
    молчала бы про очередь ревью, которая касается именно этой сущности.

    Вторая пара сущностей со своим кандидатом стоит здесь не для полноты:
    без неё счёт «по этой сущности» и счёт «по всему пользователю» численно
    совпадают, и предикат `(source=? OR target=?)` тест не держит вовсе —
    проверено снятием предиката, тест оставался зелёным. С двумя парами
    карточка «Атласа» обязана показать 1 из 2, а не всю очередь владельца.

    Мутация: вернуть 0 вместо настоящего счётчика в `count_pending_relations`
    ИЛИ снять entity-предикат в `count_relation_candidates_for_entity` — тест
    обязан покраснеть в обоих случаях.
    """
    built, auth, storage, graph = kernel
    left_id = _linked_entity(storage, graph, "alice", "Атлас")
    right_id = _linked_entity(storage, graph, "alice", "Полярис")
    ko = _document(storage, "alice", "Атлас использует Полярис.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko, left_id, "alice", status="accepted")
    graph.link_knowledge_to_entity(ko, right_id, "alice", status="accepted")
    storage.store_relation_candidate(
        "alice", left_id, right_id, "uses", confidence=0.8, evidence={"method": "test"}
    )
    # Чужая пара того же владельца: в очереди она есть, к «Атласу» отношения не имеет.
    other_left = _linked_entity(storage, graph, "alice", "Веста")
    other_right = _linked_entity(storage, graph, "alice", "Гелиос")
    storage.store_relation_candidate(
        "alice", other_left, other_right, "uses", confidence=0.8, evidence={"method": "test"}
    )
    assert storage.count_relation_candidates("alice") == 2, "в очереди владельца два кандидата"

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert result.data["relations"] == [], "связь ещё не подтверждена — не должна быть в relations"
    assert result.data["pending_relations_count"] == 1


@pytest.mark.asyncio
async def test_entity_profile_tags_do_not_leak_across_tenants(kernel):
    built, auth, storage, graph = kernel
    storage.ensure_user("bob", preset_key="user")
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko = _document(storage, "alice", "Секретный проект.", tags=["секрет"], document_date="2026-01-01")
    graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    bob = auth.actor_for_user("bob", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=bob)

    assert result.data["found"] is False, "чужая сущность не должна находиться по имени"


@pytest.mark.asyncio
async def test_entity_profile_shows_when_an_event_occurred(kernel):
    """Спека v3 §4: три разных временных факта не должны путаться. `event_time`
    (когда событие ПРОИЗОШЛО) — отдельно от `profile.document_date_range`
    (когда написаны ДОКУМЕНТЫ о нём) и от `created_at` документа (когда
    Friday об этом УЗНАЛА).

    Мутация: убрать `"event_time": self.get_event_time(...)` из entity_profile
    — тест обязан покраснеть на отсутствующем ключе.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Совещание", entity_type=EntityType.EVENT)
    graph.set_event_time("alice", entity_id, "2026-05-10", occurred_end="2026-05-11")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Совещание"}, actor=actor)

    event_time = result.data["event_time"]
    assert event_time is not None
    assert event_time["occurred_at"] == "2026-05-10"
    assert event_time["occurred_end"] == "2026-05-11"


@pytest.mark.asyncio
async def test_entity_profile_event_time_is_none_for_non_event_entities(kernel):
    """Проект/человек/организация не имеют occurred_at — поле должно быть None,
    а не отсутствовать или падать."""
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас", entity_type=EntityType.PROJECT)
    ko = _document(storage, "alice", "Документ.", tags=[], document_date=None)
    graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert result.data["event_time"] is None


def test_http_entity_profile_by_name_matches_the_agent_tool_shape(settings):
    """GET /api/kg/entity-profile?name=... — та же композиция (`kg.entity_profile`),
    что и агентский инструмент entity_lookup, но по HTTP и без участия модели.
    Основа для детерминированной команды /profile в Telegram, которая не зависит
    от того, решит ли модель позвать инструмент (см. TASKS.md #72)."""
    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage = app.state.storage
        kg = app.state.kg
        entity_id = _linked_entity(storage, kg, LEGACY_OWNER_USER_ID, "Атлас")
        ko = _document(
            storage, LEGACY_OWNER_USER_ID, "Отчёт по Атласу.", tags=["отчёт"], document_date="2026-02-01"
        )
        kg.link_knowledge_to_entity(ko, entity_id, LEGACY_OWNER_USER_ID, status="accepted")

        response = client.get("/api/kg/entity-profile", params={"name": "Атлас"}, headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert body["entity"]["id"] == entity_id
        assert body["profile"]["tags"] == ["отчёт"]
        assert body["profile"]["document_date_range"] == {"earliest": "2026-02-01", "latest": "2026-02-01"}
        assert body["pending_relations_count"] == 0

        missing = client.get("/api/kg/entity-profile", params={"name": "Нет такой сущности"}, headers=owner)
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_derived_values_say_that_they_are_derived(kernel):
    """Спека v3 §2: производное значение не выдаётся за свойство объекта.

    Теги и диапазон дат НЕ записаны на сущности — они вычислены из её документов
    прямо сейчас. Без пометки и человек, и модель читают их как факт об объекте:
    «теги Иванова такие-то», хотя честно — «в документах, где он упомянут,
    встречаются такие-то». Отсюда же берётся свежесть: значение верно на момент
    вычисления, а не навсегда.

    Мутация: убрать `profile_provenance` — тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    ko = _document(storage, "alice", "Отчёт", tags=["отчёт"], document_date="2026-01-15")
    graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    provenance = result.data["profile_provenance"]
    assert provenance["derived"] is True
    assert provenance["source_count"] == 1, "не сказано, из скольких документов выведено"
    assert provenance["computed_at"], "не сказано, когда посчитано — значит выглядит вечным"
    assert provenance["calculation"], "нет версии расчёта: сравнить два ответа будет нечем"


@pytest.mark.asyncio
async def test_the_derived_marker_counts_all_sources_not_the_shown_page(kernel):
    """`source_count` — число ВСЕХ документов, из которых выведена сводка.

    Ревью показало, что это утверждение не проверялось ничем: подмена на длину
    показанной страницы оставляла тесты зелёными. А расхождение читается прямо в
    карточке — «По его документам (10)» и через три строки «Связанных документов:
    314»: пометка о производности врала бы именно про то, ИЗ ЧЕГО значение
    выведено.

    Мутация: `source_count: len(knowledge_objects)` — тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    for index in range(13):  # страница карточки — 10
        ko = _document(
            storage, "alice", f"Документ {index}", tags=[f"тег{index}"], document_date="2026-01-15"
        )
        graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)

    assert len(result.data["knowledge_objects"]) == 10, "стенд не воспроизводит: страница не полна"
    assert result.data["profile_provenance"]["source_count"] == 13, (
        "пометка о производности считает страницу, а не все документы"
    )
    assert result.data["knowledge_objects_total"] == 13


@pytest.mark.asyncio
async def test_the_summary_survives_the_tool_budget_not_just_the_dict(kernel):
    """Сводка должна дойти ДО МОДЕЛИ, а не просто лежать в словаре.

    Ответ инструмента режется на 12 000 знаках, и список документов — самая
    длинная часть. Пока сводка стояла после него, у трети сущностей корпуса
    (замерено: 34%, а среди 200 самых широких — у всех 200) модель не получала
    ни тегов, ни диапазона дат, ни числа документов, ни пометки о производности.
    Существующие тесты этого не видели: они смотрят `result.data`, то есть
    НЕОБРЕЗАННЫЙ словарь — ровно «проверять механизм, а не то, что он подключён».

    Мутация: вернуть `knowledge_objects` в начало словаря (или вернуть сырой
    `metadata_json` в проекцию карточки) — тест обязан покраснеть.
    """
    built, auth, storage, graph = kernel
    entity_id = _linked_entity(storage, graph, "alice", "Атлас")
    # Десять документов с тяжёлыми метаданными — форма живого корпуса.
    for index in range(10):
        ko = _document(
            storage,
            "alice",
            f"Документ {index}. " + ("текст " * 200),
            tags=[f"тег{index}", "общий"],
            document_date=f"2026-0{index % 9 + 1}-15",
        )
        storage.update_knowledge_fields(
            ko,
            "alice",
            metadata_json={
                "document_date": f"2026-0{index % 9 + 1}-15",
                "stored_path": f"alice/файл-{index}.docx",
                "provenance": {"note": "служебные метаданные " * 60},
            },
        )
        graph.link_knowledge_to_entity(ko, entity_id, "alice", status="accepted")

    actor = auth.actor_for_user("alice", source="test")
    result = await built.execute("entity_lookup", {"name": "Атлас"}, actor=actor)
    message = result.to_llm_message()

    assert "profile_provenance" in message, "пометка о производности не дошла до модели"
    assert "document_date_range" in message, "диапазон дат не дошёл до модели"
    assert "knowledge_objects_total" in message, "число документов не дошло до модели"

    # И вторая половина той же защиты: сам ответ не должен упираться в потолок.
    # Порядок ключей спасает факты о сущности, но если карточка снова начнёт
    # таскать сырые метаданные документов, модель получит десяток обрезанных
    # блобов вместо списка — замерено: один `metadata_json` на живом корпусе даёт
    # медиану 13 253 знака на десять карточек при лимите 12 000.
    #
    # Мутация: вернуть `k.metadata_json` в `_ENTITY_CARD_COLUMNS` — этот ассерт
    # обязан покраснеть.
    import json as _json

    payload = len(_json.dumps(result.data, ensure_ascii=False, indent=2))
    assert payload < 12_000, (
        f"ответ инструмента {payload} знаков при лимите 12 000 — список документов будет обрезан целиком"
    )
