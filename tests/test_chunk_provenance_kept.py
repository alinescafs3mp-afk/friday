"""Цитата в ответ должна браться из совпавшего пассажа, а не из шапки документа.

Происхождение лучшего пассажа выбрасывалось, если корроборированный агрегат по
чанкам не обогнал вектор всего документа. Два изъяна сразу.

Агрегат алгебраически не больше лучшего чанка и может быть на 17% меньше, поэтому
пассаж мог честно обогнать вектор документа и всё равно потерять происхождение.
А сам вектор документа строится по ПЕРВЫМ 20 000 символов: на этом архиве 44
объекта из 342 длиннее, вектор покрывает медианно 35% текста и 2.6% у самого
длинного.

Дальше всё детерминировано: нет происхождения — нет `_embedding_chunk`, нет
запроса спана, `_matched_region` возвращает всё тело, а `best_snippet` по телу без
слов запроса отдаёт его первые 520 символов. Модели и верификатору показывали
шапку документа, совпавшего где-то в середине.
"""

from __future__ import annotations

import math

import pytest

from jericho.retrieval import HybridSearcher
from jericho.storage.models import KnowledgeObject, RawObject, new_id

# Оси задают «смысл» для поддельных векторов. Слова запроса и слова документа
# лежат на ОДНОЙ оси, но не совпадают буквально: так объект находится плотным
# отбором и не находится лексическим — единственный случай, в котором спан пассажа
# вообще запрашивается (совпавший лексически документ выдержку не сужает, иначе
# из неё может выпасть искомая фраза).
_AXES = (
    ("рекс", "корм", "миска", "питомец", "щенок", "лапы"),
    ("налог", "вычет"),
    ("устав", "приложение"),
)


def _vec(text: str) -> list[float]:
    lowered = text.casefold()
    raw = [float(sum(lowered.count(word) for word in axis)) for axis in _AXES]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw] if norm else [0.0] * len(_AXES)


class _Backend:
    remote_enabled = True

    def __init__(self, settings):
        self.settings = settings

    async def embed(self, texts):
        return [_vec(text) for text in texts]


def _store(storage, user_id: str, title: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"), user_id=user_id, raw_object_id=raw.id, content=text, title=title, summary=title
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.fixture()
def dense_settings(settings):
    from dataclasses import replace

    return replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model="test-embed",
        embeddings_chunk_chars=400,
        embeddings_chunk_overlap_chars=40,
    )


@pytest.mark.asyncio
async def test_the_matching_passage_is_reported_even_when_the_document_vector_wins(dense_settings, storage):
    from jericho.workers import WorkersManager

    storage.ensure_user("alice")
    # Шапка на «уставной» оси, совпадение с запросом — глубоко в теле.
    head = "Устав и приложение к уставу. " * 40
    tail = "Щенок и питомец, лапы. " * 6
    document = _store(storage, "alice", "Документ", head + tail + ("Прочий текст. " * 40))

    backend = _Backend(dense_settings)
    await WorkersManager(dense_settings, storage, None, None, embeddings=backend)._embeddings_index_all()  # noqa: SLF001

    searcher = HybridSearcher(storage, backend, record_usage=False)
    payload = await searcher.search("alice", "рекс корм миска", limit=5, explain=True)

    entry = next(item for item in payload["trace"] if item["id"] == document)
    assert entry["components"]["embedding_chunk"] >= 0, "происхождение пассажа потеряно"

    hit = next(item for item in payload["results"] if item["id"] == document)
    span = hit.get("_embedding_chunk_span")
    assert span, "спан пассажа не запрошен — выдержка возьмётся из начала тела"
    start, end = span
    assert document and 0 <= start < end
    assert "Щенок и питомец" in str(hit["content"])[start:end], "спан указывает не на совпавший пассаж"


@pytest.mark.asyncio
async def test_the_trace_still_says_which_signal_carried_the_retrieval(dense_settings, storage):
    """Честность объяснения не приносится в жертву: это отдельное поле, а не молчание."""
    from jericho.workers import WorkersManager

    storage.ensure_user("alice")
    document = _store(storage, "alice", "Заметка", "Щенок и питомец, лапы. " * 40)
    backend = _Backend(dense_settings)
    await WorkersManager(dense_settings, storage, None, None, embeddings=backend)._embeddings_index_all()  # noqa: SLF001

    searcher = HybridSearcher(storage, backend, record_usage=False)
    payload = await searcher.search("alice", "рекс корм миска", limit=5, explain=True)
    entry = next(item for item in payload["trace"] if item["id"] == document)
    assert entry["embedding_source"] in {"chunk", "document"}


@pytest.mark.asyncio
async def test_an_object_without_passage_vectors_says_so(dense_settings, storage):
    from dataclasses import replace

    tuned = replace(dense_settings, embeddings_enabled=False)
    storage.ensure_user("alice")
    document = _store(storage, "alice", "Заметка", "Рекс ест корм из миски.")
    searcher = HybridSearcher(storage, None, record_usage=False)
    payload = await searcher.search("alice", "рекс корм", limit=5, explain=True)
    entry = next(item for item in payload["trace"] if item["id"] == document)
    assert entry["embedding_source"] == "none"
    del tuned


@pytest.mark.asyncio
async def test_provenance_survives_the_band_where_the_document_vector_wins(dense_settings, storage):
    """Полоса, в которой происхождение терялось, задана числами напрямую.

    Агрегат по чанкам это `0.75*best + 0.25*mean(top-3)`, то есть не больше лучшего
    чанка и не меньше 0.8333 от него. Вектор документа может лежать между ними —
    и тогда старое условие `агрегат >= вектор документа` выбрасывало индекс лучшего
    пассажа, хотя пассаж был найден и известен.

    Здесь: вектор документа 0.94, чанки 1.00 / 1.00 / 0.00, агрегат
    0.75 + 0.25·(2/3) = 0.9167 < 0.94. Подобрать такую пару обычным текстом почти
    невозможно — полоса узкая, — поэтому векторы записаны прямо в базу.
    """
    from jericho.dedup import pack_vector

    storage.ensure_user("alice")
    document = _store(storage, "alice", "Документ", "тело " * 400)
    query_vector = [1.0, 0.0]

    def unit(cosine: float) -> list[float]:
        return [cosine, math.sqrt(max(0.0, 1.0 - cosine * cosine))]

    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": document,
                "user_id": "alice",
                "model": dense_settings.embeddings_model,
                "dim": 2,
                "source_version": 1,
                "content_hash": "doc",
                "vector": pack_vector(unit(0.94)),
                "chunk_scheme": "v1:1200:200:64",
            }
        ],
        chunks={
            document: [
                {
                    "knowledge_object_id": document,
                    "chunk_index": index,
                    "user_id": "alice",
                    "model": dense_settings.embeddings_model,
                    "dim": 2,
                    "source_version": 1,
                    "content_hash": f"chunk{index}",
                    "vector": pack_vector(unit(cosine)),
                    "chunk_scheme": "v1:1200:200:64",
                    "start_char": index * 100,
                    "end_char": index * 100 + 100,
                }
                for index, cosine in enumerate((1.0, 1.0, 0.0))
            ]
        },
    )

    class _Fixed:
        remote_enabled = True

        def __init__(self, settings):
            self.settings = settings

        async def embed(self, texts):
            return [query_vector for _ in texts]

    searcher = HybridSearcher(storage, _Fixed(dense_settings), record_usage=False)
    meta: dict = {}
    await searcher._dense_recall(  # noqa: SLF001
        "alice", "вопрос", {document: storage.get_knowledge_object(document, "alice")}, meta=meta
    )

    assert document in (meta.get("chunk_provenance") or {}), "происхождение пассажа выброшено"
    assert meta["chunk_provenance"][document][0] == 0, "указан не лучший пассаж"
    # И объяснение остаётся честным: выдачу вытянул вектор документа, а не пассаж.
    assert document not in (meta.get("chunk_carried") or set())
