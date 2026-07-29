"""Пересчёт векторов не должен оставлять поиск без индекса.

Вектор, посчитанный по неверному тексту, в базе неотличим от честного: хэш пишется
от полного текста, а посчитан вектор мог быть по укороченному. Замерено на корпусе
владельца 2026-07-29: 32 из 50 проверенных векторов описывали другой текст —
лечением отказа сервиса по длине было укорачивание всех текстов пачки вдвое.

Значит нужен способ сказать «пересчитай всё». Очевидная его форма — удалить строки
и дать индексатору посчитать заново — на корпусе в полторы тысячи документов
означает двадцать минут без плотного поиска. Поэтому пометка: старый вектор
остаётся на месте и отвечает на запросы, пока не придёт правильный.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.dedup import pack_vector
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _make(storage, user_id: str, index: int) -> str:
    text = f"Документ номер {index} про поставки и сроки приёмки " * 20
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}-{index}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=f"Документ {index}",
        summary="сводка",
    )
    storage.store_knowledge_object(knowledge)
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": knowledge.id,
                "user_id": user_id,
                "model": "test-embed",
                "dim": 2,
                "source_version": knowledge.version,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "vector": pack_vector([0.5, 0.5]),
            }
        ]
    )
    return knowledge.id


def test_marking_stale_keeps_the_vector_answering_until_it_is_replaced(storage):
    """Главное свойство: помеченный вектор НЕ исчезает.

    Удаление было бы проще написать и хуже жить: на полутора тысячах документов
    плотный канал отключился бы на всё время пересчёта, а человек об этом узнал бы
    по ухудшившимся ответам, а не из сообщения.
    """
    storage.ensure_user("alice")
    ids = [_make(storage, "alice", index) for index in range(5)]

    before = storage.count_knowledge_embeddings("alice")
    marked = storage.mark_embeddings_stale(user_id="alice")

    assert marked == len(ids)
    assert storage.count_knowledge_embeddings("alice") == before, (
        "пометка удалила вектора — на время пересчёта корпус остался бы без плотного поиска"
    )
    # И при этом индексатор обязан увидеть их как требующие работы.
    assert storage.count_knowledge_missing_embedding("test-embed") == len(ids)


def test_marking_one_tenant_leaves_the_others_alone(storage):
    """Пересчёт одного арендатора не должен заставлять пересчитывать всех."""
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    [_make(storage, "alice", index) for index in range(3)]
    [_make(storage, "bob", index) for index in range(4)]

    assert storage.mark_embeddings_stale(user_id="alice") == 3
    assert storage.count_knowledge_missing_embedding("test-embed") == 3, (
        "помечены чужие вектора: команда с --user трогает только своего арендатора"
    )


def test_marking_everyone_covers_every_tenant(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    [_make(storage, "alice", index) for index in range(3)]
    [_make(storage, "bob", index) for index in range(4)]

    assert storage.mark_embeddings_stale() == 7
    assert storage.count_knowledge_missing_embedding("test-embed") == 7


def test_a_fresh_vector_written_after_the_mark_clears_it(storage):
    """Пометка снимается ТОЛЬКО записью нового вектора — иначе пересчёт был бы вечным."""
    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    storage.mark_embeddings_stale()
    assert storage.count_knowledge_missing_embedding("test-embed") == 1

    record = storage.get_knowledge_object(object_id)
    assert record is not None
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": object_id,
                "user_id": "alice",
                "model": "test-embed",
                "dim": 2,
                "source_version": record["version"],
                "content_hash": hashlib.sha256(b"whatever").hexdigest(),
                "vector": pack_vector([0.1, 0.9]),
            }
        ]
    )
    assert storage.count_knowledge_missing_embedding("test-embed") == 0


@pytest.mark.parametrize("confirmed", [False, True])
def test_the_command_asks_before_it_makes_the_gpu_work(tmp_path, monkeypatch, confirmed):
    """Без --yes команда обязана ничего не делать: она заказывает работу на часы."""
    import argparse

    from jericho.cli import _reindex_embeddings

    calls: list[str | None] = []

    class _Storage:
        def mark_embeddings_stale(self, *, user_id=None):
            calls.append(user_id)
            return 7

        def record_event(self, *args, **kwargs): ...
        def close(self): ...

    monkeypatch.setattr("jericho.config.load_settings", lambda: object())
    monkeypatch.setattr("jericho.config.ensure_runtime_dirs", lambda settings: None)
    monkeypatch.setattr("jericho.storage.init_storage", lambda settings: _Storage())

    code = _reindex_embeddings(argparse.Namespace(user=None, yes=confirmed))

    if confirmed:
        assert code == 0 and calls == [None]
    else:
        assert code == 2 and calls == [], "команда пересчитала корпус без подтверждения"


def test_the_mark_defeats_the_reuse_cache_or_it_recomputes_nothing(storage):
    """Пометка версии без стирания хэша — пересчёт, который ничего не пересчитывает.

    Замерено на живом пересчёте 2026-07-29: 1208 объектов из 1537 «обновились» за
    две минуты по нулю секунд на пачку. Переиспользование по хэшу текста вернуло те
    же самые негодные вектора — по своим правилам совершенно верно: хэш писался от
    полного текста, а вектор считался по укороченному, и различить их хранилище не
    может. Команда отчиталась об успехе, индекс остался прежним.

    Значит признак «этот вектор уже есть для такого текста» обязан исчезнуть вместе
    с пометкой, иначе вся затея — переписывание номера версии.
    """
    import hashlib as _hashlib

    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    record = storage.get_knowledge_object(object_id)
    assert record is not None
    text_hash = _hashlib.sha256(str(record["content"]).encode()).hexdigest()

    # До пометки текст узнаётся по хэшу — на этом и держится дешёвый переимпорт.
    assert storage.get_vectors_by_content_hash([text_hash], "test-embed")

    storage.mark_embeddings_stale()

    assert not storage.get_vectors_by_content_hash([text_hash], "test-embed"), (
        "хэш пережил пометку — индексатор возьмёт из кэша тот же негодный вектор"
    )
    assert not storage.get_reusable_vectors([object_id], "test-embed").get(object_id), (
        "объект по-прежнему предъявляет свой прежний вектор как годный к переиспользованию"
    )
    # И при этом вектор никуда не делся: поиск продолжает отвечать.
    assert storage.count_knowledge_embeddings("alice") == 1


def test_passage_vectors_are_marked_with_their_object(storage):
    """Чанки укорачивались в тех же пачках, значит и пересчитываться должны вместе."""
    import hashlib as _hashlib

    from jericho.dedup import pack_vector

    storage.ensure_user("alice")
    object_id = _make(storage, "alice", 0)
    chunk_text = "кусок документа про сроки приёмки"
    chunk_hash = _hashlib.sha256(chunk_text.encode()).hexdigest()
    # Пассажи пишутся отдельным отображением, а не в общий список: они живут в своей
    # таблице, и запись их как объектных векторов молча перезаписала бы объектный.
    storage.upsert_knowledge_vectors(
        [],
        {
            object_id: [
                {
                    "knowledge_object_id": object_id,
                    "user_id": "alice",
                    "model": "test-embed",
                    "dim": 2,
                    "source_version": 1,
                    "content_hash": chunk_hash,
                    "chunk_scheme": "v2",
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": len(chunk_text),
                    "vector": pack_vector([0.3, 0.7]),
                }
            ]
        },
    )
    assert storage.get_vectors_by_content_hash([chunk_hash], "test-embed")

    storage.mark_embeddings_stale()

    assert not storage.get_vectors_by_content_hash([chunk_hash], "test-embed"), (
        "вектор пассажа остался в кэше переиспользования и вернётся вместо пересчёта"
    )
    assert storage.count_knowledge_chunk_embeddings("alice") == 1, "пассаж удалён, а должен был остаться"


@pytest.mark.asyncio
async def test_a_per_tenant_reindex_is_not_served_from_another_tenants_vector(storage, settings):
    """`--user X` обязан ПЕРЕСЧИТАТЬ, а не взять тот же текст у соседа.

    Поиск по хэшу идёт по всей таблице без фильтра арендатора — в этом его польза
    при повторном импорте. Но пометка с `--user` стирает хэши только у своего
    арендатора, и тот же документ у другого остаётся с прежним вектором. Индексатор
    находил его, HTTP-вызова не делал и переписывал строку как свежую: пересчёт
    отчитывался об успехе, ничего не пересчитав, и вектор после этого выглядел
    честным. Найдено состязательным ревью, воспроизведено на одноразовой базе.

    Проверяется поведение индексатора, а не хранилища: именно он решает, спрашивать
    ли кэш.
    """
    import dataclasses

    from jericho.workers import WorkersManager

    class _CountingEmbeddings:
        def __init__(self, tuned) -> None:
            self.settings = tuned
            self.remote_enabled = True
            self.cooling_down = False
            self.calls = 0

        async def embed(self, texts, *, budget_sec=None):
            self.calls += 1
            return [[0.5, 0.5] for _ in texts]

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
        embeddings_chunk_chars=0,
    )
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    # Один и тот же текст у двух арендаторов — на настоящем архиве это обычное дело.
    alice_id = _make(storage, "alice", 0)
    bob_id = _make(storage, "bob", 0)
    record = storage.get_knowledge_object(alice_id)
    assert record is not None
    assert str(record["content"]) == str((storage.get_knowledge_object(bob_id) or {})["content"]), (
        "проба построена неверно: у арендаторов разный текст, и общего хэша не будет"
    )

    # Вектор соседа надо записать под ТЕМ хэшем, который считает индексатор: он
    # хэширует `knowledge_search_text(...)`, а не голое тело. Первая версия этой
    # пробы хэшировала тело, хэши не совпадали, кэш не срабатывал никогда — и тест
    # был зелёным на сломанном коде. Проверено подменой поведения.
    import hashlib as _hl

    from jericho.retrieval import knowledge_search_text
    from jericho.workers import _DOC_VECTOR_MAX_CHARS

    bob_row = storage.get_knowledge_object(bob_id)
    assert bob_row is not None
    doc_text = knowledge_search_text(bob_row)[:_DOC_VECTOR_MAX_CHARS]
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": bob_id,
                "user_id": "bob",
                "model": "test-embed",
                "dim": 2,
                "source_version": bob_row["version"],
                "content_hash": _hl.sha256(doc_text.encode("utf-8")).hexdigest(),
                "vector": pack_vector([0.9, 0.1]),
            }
        ]
    )

    storage.mark_embeddings_stale(user_id="alice")

    fake = _CountingEmbeddings(tuned)
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_pass()  # noqa: SLF001

    assert fake.calls > 0, (
        "индексатор не обратился к сервису — вектор взят у другого арендатора, "
        "и принудительный пересчёт ничего не пересчитал"
    )


@pytest.mark.asyncio
async def test_a_forced_object_is_not_served_by_a_neighbours_hash(storage, settings):
    """Защита от кэша обязана стоять на ПРИМЕНЕНИИ, а не только на запросе.

    Первая версия правки убирала дайджесты помеченных объектов из аргумента
    `get_vectors_by_content_hash` — и этого мало. Ответ приходит плоским словарём
    «дайджест → вектор», поэтому достаточно ОДНОГО непомеченного соседа по пачке с
    тем же текстом, чтобы нужный дайджест туда попал; дальше помеченный объект
    находит его и уходит без обращения к сервису. Пересчёт снова отчитывается об
    успехе, ничего не пересчитав.

    Найдено состязательным ревью, двумя независимыми срезами.
    """
    import dataclasses
    import hashlib as _hl

    from jericho.retrieval import knowledge_search_text
    from jericho.workers import _DOC_VECTOR_MAX_CHARS, WorkersManager

    class _CountingEmbeddings:
        def __init__(self, tuned) -> None:
            self.settings = tuned
            self.remote_enabled = True
            self.cooling_down = False
            self.calls = 0

        async def embed(self, texts, *, budget_sec=None):
            self.calls += 1
            return [[0.5, 0.5] for _ in texts]

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
        embeddings_chunk_chars=0,
    )
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    alice_id = _make(storage, "alice", 0)
    bob_id = _make(storage, "bob", 0)

    bob_row = storage.get_knowledge_object(bob_id)
    assert bob_row is not None
    doc_text = knowledge_search_text(bob_row)[:_DOC_VECTOR_MAX_CHARS]
    digest = _hl.sha256(doc_text.encode("utf-8")).hexdigest()

    # Сосед УСТАРЕЛ и потому сам попадёт в пачку — значит его дайджест уйдёт в запрос
    # на законных основаниях, и `shared` наполнится, как ни фильтруй аргумент.
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": bob_id,
                "user_id": "bob",
                "model": "test-embed",
                "dim": 2,
                "source_version": 999,  # не равна версии объекта -> устарел
                "content_hash": digest,
                "vector": pack_vector([0.9, 0.1]),
            }
        ]
    )
    storage.mark_embeddings_stale(user_id="alice")

    fake = _CountingEmbeddings(tuned)
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_pass()  # noqa: SLF001

    alice_vectors = storage.get_reusable_vectors([alice_id], "test-embed").get(alice_id, {})
    assert digest not in alice_vectors or fake.calls > 0, (
        "помеченный объект получил вектор соседа по хэшу — пересчёт снова оказался "
        "переписыванием номера версии"
    )
    assert fake.calls > 0, "к сервису не обратились вовсе: кэш перекрыл принудительный пересчёт"


@pytest.mark.asyncio
async def test_a_mark_arriving_mid_flight_is_not_erased_by_a_cached_write(storage, settings):
    """Пометка, пришедшая пока пачка в сервисе, не должна затираться КЭШИРОВАННОЙ записью.

    План строится ДО пометки, поэтому у него `forced` нулевой. Если его входы
    разрешены из кэша, запись ставит настоящий `content_hash` и текущую версию — то
    есть снимает пометку вектором, который никто не пересчитывал. Команда пересчёта
    рассчитана на работу при живом сервисе, значит это не редкость: сегодня так
    вышло трижды.

    Важно, ЧТО именно проверяется. Объект, честно посчитанный сервисом в этом же
    проходе, пометку снимать ВПРАВЕ — пересчёт ведь произошёл. Первая версия этого
    теста требовала обратного и падала на здоровом коде; исправлена по сути, а не
    подгонкой ожидания.
    """
    import dataclasses
    import hashlib as _hl

    from jericho.retrieval import knowledge_search_text
    from jericho.workers import _DOC_VECTOR_MAX_CHARS, WorkersManager

    class _MarkingEmbeddings:
        """Помечает объекты РОВНО в тот момент, когда пачка ушла в сервис."""

        def __init__(self, tuned, store) -> None:
            self.settings = tuned
            self.remote_enabled = True
            self.cooling_down = False
            self.calls = 0
            self._store = store

        async def embed(self, texts, *, budget_sec=None):
            self.calls += 1
            self._store.mark_embeddings_stale()
            return [[0.5, 0.5] for _ in texts]

    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9999/v1",
        embeddings_model="test-embed",
        embeddings_chunk_chars=0,
    )
    storage.ensure_user("alice")
    cached_id = _make(storage, "alice", 0)
    # Объект БЕЗ вектора — он и уводит пачку в сервис, давая пометке прийти. `_make`
    # для этого не годится: он сразу пишет вектор, и объект перестаёт быть missing.
    fresh_raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content="совсем другой документ про сроки и приёмку " * 20,
        content_type="text",
        content_hash=hashlib.sha256(b"fresh").hexdigest(),
    )
    storage.store_raw_object(fresh_raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=fresh_raw.id,
            content=fresh_raw.raw_content,
            content_type="text",
            title="Свежий",
            summary="сводка",
        )
    )

    # Объект устарел по ВЕРСИИ, но текст не менялся, поэтому его собственный вектор
    # переиспользуется целиком и к сервису за ним никто не пойдёт.
    row = storage.get_knowledge_object(cached_id)
    assert row is not None
    digest = _hl.sha256(knowledge_search_text(row)[:_DOC_VECTOR_MAX_CHARS].encode("utf-8")).hexdigest()
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": cached_id,
                "user_id": "alice",
                "model": "test-embed",
                "dim": 2,
                "source_version": 999,  # не равна версии объекта -> устарел
                "content_hash": digest,
                "vector": pack_vector([0.9, 0.1]),
            }
        ]
    )

    fake = _MarkingEmbeddings(tuned, storage)
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_pass()  # noqa: SLF001
    assert fake.calls > 0, "проба не сработала: сервис не звался, пометке неоткуда было прийти"

    forced_now = storage.list_forced_embedding_ids([cached_id])
    assert forced_now == [cached_id], (
        "кэшированная запись стёрла пришедшую пометку — принудительный пересчёт "
        "потерян молча, и вектор после этого выглядит честным"
    )
