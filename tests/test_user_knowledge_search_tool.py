"""Спросить по существу о том, что написал другой человек, не сливая корпуса.

`user_activity` отвечает про объём и темы — «что прислал, когда, сколько».
На вопрос «а что он писал про сроки поставки» она ответить не может, а обычный
поиск ограничен своим арендатором by design. Между ними была дыра: старший,
которому владелец разрешил видеть содержимое, читал чужое только глазами.

Изоляция при этом не снимается — арендатор остаётся ПАРАМЕТРОМ поиска. Меняется
одно: кто вправе назвать чужой арендатор этим параметром. Отсюда три правила,
которые здесь и закреплены: гейт на ВЕРХНЕМ праве (метаданного уровня у этого
инструмента не бывает — любой результат есть содержимое), запись в аудит на
ЦЕЛЕВОЙ аккаунт с необратимым отпечатком вопроса, и честный отказ вместо пустоты.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.retrieval import HybridSearcher
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer

_MISSING_UPLOADER = object()


def _knowledge(
    storage,
    user_id: str,
    text: str,
    title: str,
    *,
    uploaded_by: object = _MISSING_UPLOADER,
) -> str:
    metadata = {} if uploaded_by is _MISSING_UPLOADER else {"uploaded_by": uploaded_by}
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="telegram",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}{text}".encode()).hexdigest(),
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        summary=text[:120],
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


@pytest.fixture
async def shared_kernel(settings, storage):
    tenant = "shared-archive"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user("boss", preset_key="admin")
    storage.update_user("boss", preset_key="admin", display_name="Босс")
    storage.ensure_user("usr_ivan", preset_key="user")
    storage.update_user("usr_ivan", preset_key="user", display_name="Иван")
    storage.ensure_user("usr_anna", preset_key="user")
    storage.update_user("usr_anna", preset_key="user", display_name="Анна")

    term = "термоконтроль"
    for number in range(30):
        _knowledge(
            storage,
            tenant,
            "FOREIGN_SCOPE_SENTINEL " + " ".join([term] * 24),
            f"Чужой документ {number:02d}",
            uploaded_by="usr_anna",
        )
    _knowledge(
        storage,
        tenant,
        " ".join([term] * 24),
        "Документ без автора",
    )
    target_id = _knowledge(
        storage,
        tenant,
        f"{term} " + "обычный текст " * 80,
        "Документ Ивана",
        uploaded_by="usr_ivan",
    )

    auth = AuthorizationService(storage, shared_tenant=tenant)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    try:
        yield kernel, auth, storage, tenant, target_id, term
    finally:
        await web.close()


@pytest.fixture
async def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="admin")
    storage.update_user("boss", preset_key="admin", display_name="Босс")
    storage.ensure_user("usr_ivan", preset_key="user")
    storage.update_user("usr_ivan", preset_key="user", display_name="Иван")
    storage.ensure_user("usr_anna", preset_key="user")
    storage.update_user("usr_anna", preset_key="user", display_name="Анна")
    _knowledge(storage, "usr_ivan", "Срыв сроков поставки оборудования на склад в июле.", "Поставка")
    _knowledge(storage, "usr_ivan", "Рутинная заметка про обед и погоду.", "Обед")
    _knowledge(storage, "usr_anna", "Совсем другая тема: отпуск и билеты.", "Отпуск")

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    try:
        yield kernel, auth, storage
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_an_admin_gets_the_text_of_what_that_person_wrote(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Ивану", "query": "срыв сроков поставки"}, actor=boss
    )

    assert result.success is True, result.error
    assert result.data["resolved"]["user_id"] == "usr_ivan"
    titles = [item["title"] for item in result.data["results"]]
    assert "Поставка" in titles
    excerpt = next(item["excerpt"] for item in result.data["results"] if item["title"] == "Поставка")
    assert "срок" in excerpt.casefold(), "выдержка не показывает написанное"


@pytest.mark.asyncio
async def test_the_isolation_holds_the_other_way(kernel):
    """Корпус Анны не должен просачиваться в ответ про Ивана."""
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "отпуск билеты"}, actor=boss
    )

    assert result.success is True
    assert all(item["title"] != "Отпуск" for item in result.data["results"])


@pytest.mark.asyncio
async def test_an_ordinary_account_cannot_read_anyone(kernel):
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")

    result = await runtime.execute("user_knowledge_search", {"person": "Анна", "query": "отпуск"}, actor=ivan)

    assert result.success is False
    assert "denied" in result.error.casefold() or "not allowed" in result.error.casefold()


@pytest.mark.asyncio
async def test_the_tool_is_invisible_without_the_capability(kernel):
    """Гейт — не только на вызове: обычный аккаунт не должен даже видеть инструмент."""
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")
    boss = auth.actor_for_user("boss", source="test")

    assert "user_knowledge_search" not in set(runtime.get_tool_names(ivan))
    assert "user_knowledge_search" in set(runtime.get_tool_names(boss))


@pytest.mark.asyncio
async def test_reading_someone_is_recorded_against_them_with_a_question_fingerprint(kernel):
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")

    await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "срыв сроков поставки"}, actor=boss
    )

    rows = [
        row
        for row in storage.list_audit_log(limit=50)
        if str(row.get("action")) == "tool.user_knowledge_search"
    ]
    assert rows, "чтение чужого корпуса не записано"
    entry = rows[0]
    assert entry["target_id"] == "usr_ivan", "запись не на том, кого читали"
    after = entry.get("after") if isinstance(entry.get("after"), dict) else json.loads(entry["after_json"])
    query = "срыв сроков поставки"
    assert query not in str(entry.get("after_json") or ""), "в append-only журнале остался вопрос"
    assert after.get("query_chars") == len(query)
    assert str(after.get("query_ref")).startswith("fpref_")
    assert after.get("query_ref") != hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert "query_sha256" not in after


@pytest.mark.asyncio
async def test_an_ambiguous_name_answers_with_candidates_and_leaves_a_trace(kernel):
    runtime, auth, storage = kernel
    storage.ensure_user("usr_ivan2", preset_key="user")
    storage.update_user("usr_ivan2", preset_key="user", display_name="Иван")
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "поставка"}, actor=boss
    )

    assert result.success is True
    assert result.data["resolved"] is None and result.data["reason"] == "ambiguous"
    assert len(result.data["candidates"]) == 2
    assert any(
        str(row.get("action")) == "tool.user_knowledge_search.unresolved"
        for row in storage.list_audit_log(limit=50)
    ), "перебор неоднозначных имён не оставил следа"


@pytest.mark.asyncio
async def test_storage_fallback_clamps_limit_like_the_searcher_path(kernel, monkeypatch):
    """Without a HybridSearcher the bare FTS path used to honour limit=999.

    Schema says maximum 20, but execute() does not enforce schema bounds —
    the handler itself must clamp both branches the same way.
    """
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")
    assert runtime.searcher is None, "precondition: fixture leaves searcher unbound"

    seen: list[int] = []
    real = storage.search_knowledge

    def _spy(user_id, query, *, limit=20):
        seen.append(int(limit))
        return real(user_id, query, limit=limit)

    monkeypatch.setattr(storage, "search_knowledge", _spy)

    result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": "поставка", "limit": 999},
        actor=boss,
    )

    assert result.success is True, result.error
    assert seen == [20], f"storage got unbounded limit: {seen}"


@pytest.mark.asyncio
async def test_shared_search_scopes_by_uploader_before_the_candidate_cap(shared_kernel, monkeypatch):
    """A shared tenant is the storage location; the requested person is the scope.

    Thirty stronger foreign matches and an unattributed match deliberately fill the
    ordinary FTS cap before Ivan's record.  Filtering that result afterwards returns
    a false empty answer; filtering the Raw uploader in SQL before LIMIT finds the
    target and cannot expose the other contributors.
    """
    runtime, auth, storage, tenant, target_id, term = shared_kernel
    boss = auth.actor_for_user("boss", source="test")
    unscoped = storage.search_knowledge(tenant, term, limit=20)
    assert target_id not in {str(item["id"]) for item in unscoped}, "precondition: cap did not saturate"

    real_search = storage.search_knowledge

    scoped_calls: list[str | None] = []

    def _scoped_search(*args, **kwargs):
        if kwargs.get("uploaded_by") is not None:
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
        scoped_calls.append(kwargs.get("uploaded_by"))
        return real_search(*args, **kwargs)

    monkeypatch.setattr(storage, "search_knowledge", _scoped_search)
    runtime.searcher = HybridSearcher(storage, None)
    result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": term, "limit": 20},
        actor=boss,
    )

    assert result.success is True, result.error
    assert result.data["resolved"]["user_id"] == "usr_ivan"
    assert result.data["strategy"] == "scoped_hybrid"
    assert result.data["unattributed_excluded"] is True
    assert [item["id"] for item in result.data["results"]] == [target_id]
    assert result.data["results"][0]["title"] == "Документ Ивана"
    assert "FOREIGN_SCOPE_SENTINEL" not in json.dumps(result.data, ensure_ascii=False)
    assert scoped_calls and set(scoped_calls) == {"usr_ivan"}

    like_target = _knowledge(
        storage,
        tenant,
        "x",
        "Однобуквенный документ Ивана",
        uploaded_by="usr_ivan",
    )
    _knowledge(
        storage,
        tenant,
        "x",
        "Похожий идентификатор автора",
        uploaded_by="usr_ivan-copy",
    )
    _knowledge(storage, tenant, "x", "Ещё один документ без автора")
    _knowledge(storage, tenant, "x", "Автор явно null", uploaded_by=None)
    _knowledge(storage, tenant, "x", "Автор-число", uploaded_by=17)
    _knowledge(storage, tenant, "x", "Автор-массив", uploaded_by=["usr_ivan"])
    like_result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": "x", "limit": 20},
        actor=boss,
    )

    assert like_result.success is True, like_result.error
    assert [item["id"] for item in like_result.data["results"]] == [like_target]

    scoped_calls.clear()
    ivan_again, anna = await asyncio.gather(
        runtime.execute(
            "user_knowledge_search",
            {"person": "Иван", "query": term, "limit": 20},
            actor=boss,
        ),
        runtime.execute(
            "user_knowledge_search",
            {"person": "Анна", "query": term, "limit": 20},
            actor=boss,
        ),
    )
    assert ivan_again.success is True and anna.success is True
    assert [item["id"] for item in ivan_again.data["results"]] == [target_id]
    assert target_id not in {item["id"] for item in anna.data["results"]}
    assert all(item["title"].startswith("Чужой документ") for item in anna.data["results"])
    assert set(scoped_calls) == {"usr_ivan", "usr_anna"}


@pytest.mark.asyncio
async def test_personal_archive_keeps_using_the_hybrid_searcher(kernel):
    """The shared fail-closed lane must not downgrade an ordinary personal archive."""
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")
    calls: list[tuple[str, str, int]] = []

    class _PersonalSearcher:
        async def search(self, user_id: str, query: str, *, limit: int):
            calls.append((user_id, query, limit))
            return {"results": storage.search_knowledge(user_id, query, limit=limit)}

    runtime.searcher = _PersonalSearcher()
    result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": "срыв сроков поставки", "limit": 8},
        actor=boss,
    )

    assert result.success is True, result.error
    assert calls == [("usr_ivan", "срыв сроков поставки", 8)]
    assert any(item["title"] == "Поставка" for item in result.data["results"])


@pytest.mark.asyncio
async def test_shared_hybrid_quotes_the_passage_that_dense_recall_found(shared_kernel):
    runtime, auth, _, tenant, target_id, _ = shared_kernel
    boss = auth.actor_for_user("boss", source="test")
    header = "HEADER_SENTINEL " * 80
    evidence = "LATE_PASSAGE_EVIDENCE именно здесь находится ответ."
    content = header + evidence + (" хвост" * 80)
    start = len(header)

    class _PassageSearcher:
        async def search(self, user_id, query, **kwargs):
            assert user_id == tenant
            assert kwargs == {
                "limit": 8,
                "uploaded_by": "usr_ivan",
                "record_usage": False,
                "include_entities": False,
                "graph_expansion": False,
            }
            return {
                "query": "semantic rewritten query",
                "results": [
                    {
                        "id": target_id,
                        "title": "Поздний фрагмент",
                        "knowledge_kind": "note",
                        "content": content,
                        "_embedding_chunk_span": [start, start + len(evidence)],
                    }
                ],
                "strategy": {"uploader_scoped": True},
            }

    runtime.searcher = _PassageSearcher()
    result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": "вопрос без общих слов", "limit": 8},
        actor=boss,
    )

    assert result.success is True, result.error
    excerpt = result.data["results"][0]["excerpt"]
    assert "LATE_PASSAGE_EVIDENCE" in excerpt
    assert "HEADER_SENTINEL" not in excerpt
