"""G10 / #39: вопрос про человека больше не уходит в болтовню сам по себе.

Ночной разбор (2026-07-30): 4/5 вопросов владельца про архив получили
`answer_mode=general_conversation` и `knowledge_hits=0`. Подозревали классификатор.

Условие сменилось вечером 31 июля: `_prepare_context` ходит в поиск с
`graph_expansion=False`, recall на золотом наборе вырос. Классификатор решает по
`knowledge_hits` / `retrieval_confidence` — то есть зависит от того же поиска.

**Критерий объявлен ДО правки классификатора (правки не было):**
на формах из переписки/фикстур, при наличии в корпусе досье на человека,
доля `personal_knowledge|mixed` с `hits>0` должна быть полной; настоящая болтовня
обязана остаться в `general_conversation` при пустых hits.

Замер 2026-07-31 на сегодняшнем коде (синтетический корпус + формы из
переписки/регрессий, не удобные выдумки):

    person standalone   8/9   (промах — опечатка «Кирила», hits=0, это поиск)
    person follow-up    3/3
    chitchat            8/8   general_conversation

Итог: болезнь ушла вместе с поиском. Регулярку `personal_cue` и порог 0.35 не
трогаем (отрицательный результат по классификатору). Этот тест — сторож, чтобы
регрессия снова не отдавала архивный вопрос в general_conversation при живых hits.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import AgentRuntime
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import HybridSearcher

# Формы из переписки / существующих регрессий (см. test_answer_without_sources…,
# test_people_get_into_the_graph). Не «удобные» выдумки.
PERSON_QUERIES = (
    "давай про Макарова Кирилла инфу",
    "что ты знаешь про Макарова Кирилла Евгеньевича",
    "найди мне человека с фамилией Нестеренко",
    "что известно о Нестеренко Ольге",
    "про Макарова Кирилла",
    "Макаров Кирилл Евгеньевич",
    "расскажи про Нестеренко",
    "а Макарова Кирилла?",
)

FOLLOWUPS = (
    "а его брат?",
    "а его должность?",
    "её телефон?",
)

CHITCHAT = (
    "привет",
    "как дела?",
    "что думаешь о погоде?",
    "расскажи анекдот",
    "кто такой Наполеон?",
    "сколько будет 2+2",
    "спасибо",
    "ок",
)


async def _seed_person_corpus(settings, storage) -> KnowledgeGraph:
    graph = KnowledgeGraph(storage)
    pipe = IngestionPipeline(settings, storage, graph)
    await pipe.ingest_text(
        "alice",
        (
            "Личное дело. Макаров Кирилл Евгеньевич, дата рождения 1999, "
            "должность инженер, личное дело СА-396195. "
            "Брат — Макаров Андрей Евгеньевич."
        ),
        force_knowledge=True,
        source_ref="g10-ld-makarov",
    )
    await pipe.ingest_text(
        "alice",
        "Штатное расписание. Нестеренко Ольга Петровна, отдел кадров, телефон 100-200.",
        force_knowledge=True,
        source_ref="g10-staff-nesterenko",
    )
    # Концентраторы со-встречаемости — раньше расширение по графу через них
    # вытесняло нужное; на сегодняшнем пути (expansion off) они не должны
    # обнулять hits у прямого запроса по фамилии.
    for i in range(5):
        await pipe.ingest_text(
            "alice",
            (
                f"Общий приказ N{i}: перечислить присутствующих. "
                "Макаров Кирилл Евгеньевич, Иванов Иван Иванович, "
                "Петров Пётр Петрович, Сидоров С.С., Козлов А.А."
            ),
            force_knowledge=True,
            source_ref=f"g10-order-{i}",
        )
    return graph


@pytest.mark.asyncio
async def test_person_archive_questions_leave_general_conversation(settings, storage):
    """Критерий: 8/8 standalone-форм → personal_knowledge|mixed и hits>0."""
    graph = await _seed_person_corpus(settings, storage)
    runtime = AgentRuntime(settings, storage)
    searcher = HybridSearcher(storage)
    conv = storage.create_conversation("alice", title="g10-person")

    modes: list[str] = []
    for query in PERSON_QUERIES:
        ctx = await runtime._prepare_context(
            "alice",
            query,
            conv["id"],
            prior_history=[],
            kg=graph,
            searcher=searcher,
        )
        modes.append(ctx.answer_mode)
        assert ctx.knowledge_hits, f"hits=0 на архивном вопросе: {query!r}"
        assert ctx.answer_mode in {"personal_knowledge", "mixed"}, (
            f"архивный вопрос ушёл в {ctx.answer_mode}: {query!r} "
            f"hits={len(ctx.knowledge_hits)} conf={ctx.retrieval_confidence}"
        )

    assert modes.count("general_conversation") == 0
    # Хотя бы часть должна быть personal_knowledge (не всё «слабый mixed»).
    assert any(m == "personal_knowledge" for m in modes), modes


@pytest.mark.asyncio
async def test_person_followups_keep_archive_mode(settings, storage):
    graph = await _seed_person_corpus(settings, storage)
    runtime = AgentRuntime(settings, storage)
    searcher = HybridSearcher(storage)
    conv = storage.create_conversation("alice", title="g10-fu")
    storage.store_message(
        conv["id"],
        "alice",
        "user",
        "что ты знаешь про Макарова Кирилла Евгеньевича",
    )
    history = storage.get_conversation_messages(conv["id"], user_id="alice")

    for query in FOLLOWUPS:
        ctx = await runtime._prepare_context(
            "alice",
            query,
            conv["id"],
            prior_history=history,
            kg=graph,
            searcher=searcher,
        )
        assert ctx.knowledge_hits, f"follow-up без hits: {query!r} q={ctx.search_query!r}"
        assert ctx.answer_mode in {"personal_knowledge", "mixed"}, (
            f"follow-up ушёл в {ctx.answer_mode}: {query!r}"
        )


@pytest.mark.asyncio
async def test_true_chitchat_stays_general_conversation(settings, storage):
    """Второе обязательное плечо критерия: болтовню нельзя утащить в архив."""
    graph = await _seed_person_corpus(settings, storage)
    runtime = AgentRuntime(settings, storage)
    searcher = HybridSearcher(storage)
    conv = storage.create_conversation("alice", title="g10-chat")

    for query in CHITCHAT:
        ctx = await runtime._prepare_context(
            "alice",
            query,
            conv["id"],
            prior_history=[],
            kg=graph,
            searcher=searcher,
        )
        assert ctx.answer_mode == "general_conversation", (
            f"болтовня ушла в {ctx.answer_mode}: {query!r} hits={len(ctx.knowledge_hits)}"
        )
        assert not ctx.knowledge_hits, f"неожиданные hits на болтовне: {query!r}"


@pytest.mark.asyncio
async def test_answer_mode_without_hits_is_not_personal_knowledge(settings, storage):
    """Мутационный якорь: пустой поиск не должен краситься в personal_knowledge.

    Если кто-то «починит» G10, заставив personal_cue/режим всегда personal_knowledge,
    болтовня и пустой архив станут ложными досье — этот тест обязан упасть.
    """
    runtime = AgentRuntime(settings, storage)

    class _Empty:
        async def search(self, user_id, query, **kwargs):
            del user_id, query, kwargs
            return {"results": [], "entity_matches": []}

    conv = storage.create_conversation("alice", title="g10-empty")
    ctx = await runtime._prepare_context(
        "alice",
        "давай про Макарова Кирилла инфу",
        conv["id"],
        prior_history=[],
        kg=None,
        searcher=_Empty(),
    )
    assert ctx.knowledge_hits == []
    assert ctx.answer_mode == "general_conversation", ctx.answer_mode

    missing = await runtime._prepare_context(
        "alice",
        "что ты помнишь обо мне?",
        conv["id"],
        prior_history=[],
        kg=None,
        searcher=_Empty(),
    )
    assert missing.answer_mode == "personal_knowledge_missing"
