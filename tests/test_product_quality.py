from __future__ import annotations

import hashlib
import json

import pytest

from friday.agent_runtime import AgentRuntime
from friday.ingestion import IngestionPipeline, _extract_entities
from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import HybridSearcher
from friday.storage.models import (
    EntityType,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    RelationType,
    new_id,
)


def _store_knowledge(
    storage,
    user_id: str,
    content: str,
    *,
    title: str,
    quality: float = 0.5,
    promotion: float = 0.5,
    importance: float = 0.5,
    metadata: dict | None = None,
) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
        metadata_json=metadata or {},
        importance=importance,
        quality_score=quality,
        promotion_score=promotion,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


@pytest.mark.asyncio
async def test_moderate_promotion_has_three_explainable_outcomes(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)

    for index, text in enumerate(
        [
            "Привет!",
            "Как настроить PostgreSQL?",
            "/status",
            "Проверь сервер и перезапусти nginx",
        ]
    ):
        result = await pipeline.ingest_text(
            "alice",
            text,
            source_ref=f"transient:{index}",
        )
        assert result["action"] == "transient"
        assert result["promoted"] is False
        assert result["queued_for_review"] is False

    review = await pipeline.ingest_text(
        "alice",
        "Проект Orion возможно позже перенесём на новый сервер",
        source_ref="borderline:1",
    )
    assert review["action"] == "review"
    assert review["promoted"] is False
    assert review["queued_for_review"] is True
    assert review["suggestions"]["title"] != "Без названия"
    assert review["suggestions"]["summary"]
    assert review["suggestions"]["knowledge_kind"] == "project"
    assert storage.get_inbox_item(review["inbox_id"], "alice")["knowledge_object_id"] is None

    promoted = await pipeline.ingest_text(
        "alice",
        "Сервер Atlas работает на Ubuntu 24.04.",
        source_ref="fact:1",
    )
    assert promoted["action"] == "promote"
    assert promoted["promoted"] is True
    assert promoted["knowledge_object"]["knowledge_kind"] == "fact"
    assert promoted["knowledge_object"]["quality_score"] >= 0.5
    assert any(item["entity_name"] == "Atlas" for item in promoted["graph_links"])

    explicit_question = await pipeline.ingest_text(
        "alice",
        "Можешь запомнить, что проект Alpha использует PostgreSQL 16?",
        source_ref="explicit-save-question:1",
    )
    assert explicit_question["action"] == "promote"
    assert explicit_question["reason"] == "explicit save intent"
    assert not explicit_question["knowledge_object"]["title"].startswith("Можешь")

    assert storage.count_knowledge_objects("alice") == 2


@pytest.mark.asyncio
async def test_named_relationships_dated_records_and_note_commands_promote_conservatively(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    company = await pipeline.ingest_text(
        "alice",
        "Компания Acme GmbH использует Kubernetes.",
        source_ref="fact:company-stack",
    )
    assert company["action"] == "promote"
    assert company["knowledge_object"]["knowledge_kind"] == "fact"
    assert {item["entity_name"] for item in company["graph_links"]} == {
        "Acme GmbH",
        "Kubernetes",
    }

    role = await pipeline.ingest_text(
        "alice",
        "Иван Петров — ведущий разработчик проекта Orion.",
        source_ref="fact:project-role",
    )
    assert role["action"] == "promote"
    assert role["knowledge_object"]["knowledge_kind"] == "fact"

    deadline = await pipeline.ingest_text(
        "alice",
        "Дедлайн проекта Orion — 31.07.2026.",
        source_ref="task:orion-deadline",
    )
    assert deadline["action"] == "promote"
    assert deadline["knowledge_object"]["knowledge_kind"] == "task"

    explicit_note = await pipeline.ingest_text(
        "alice",
        "Сделай заметку: сервер Atlas имеет IP 10.0.0.5",
        source_ref="note:atlas-ip",
    )
    assert explicit_note["action"] == "promote"
    assert explicit_note["knowledge_object"]["title"] == "сервер Atlas имеет IP 10.0.0.5"

    interpersonal = await pipeline.ingest_text(
        "alice",
        "Я люблю тебя",
        source_ref="chatter:affection",
    )
    assert interpersonal["action"] == "transient"
    assert interpersonal["promoted"] is False


@pytest.mark.asyncio
async def test_inbox_promotion_applies_human_corrections_and_provenance(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_text(
        "alice",
        "Идея: сделать дашборд состояния серверов",
        source_ref="idea:1",
    )
    assert result["queued_for_review"] is True
    assert result["promoted"] is False

    reviewed = pipeline.classify_inbox_item(
        "alice",
        result["inbox_id"],
        InboxStatus.CLASSIFIED,
        promote=True,
        title="Дашборд состояния инфраструктуры",
        summary="Собрать единый экран здоровья серверов.",
        knowledge_kind="idea",
        tags=["инфраструктура", "dashboard"],
        importance=0.72,
        reviewed_by="owner",
    )
    assert reviewed is not None
    assert reviewed["knowledge_object_id"]
    ko = storage.get_knowledge_object(reviewed["knowledge_object_id"], "alice")
    assert ko is not None
    assert ko["title"] == "Дашборд состояния инфраструктуры"
    assert ko["summary"] == "Собрать единый экран здоровья серверов."
    assert ko["promotion_score"] == 1.0
    assert storage.get_raw_object(ko["raw_object_id"], "alice") is not None


def test_entity_extraction_preserves_compact_identifiers_exactly():
    entities = _extract_entities(
        "BRK.A = 710000 USD, BRK.B = 473.25 USD; contract BRNQ26; ISIN US0378331005."
    )
    by_name = {item["name"]: item for item in entities}
    assert set(by_name) == {"BRK.A", "BRK.B", "BRNQ26", "US0378331005"}
    assert by_name["BRK.A"]["method"] == "identifier_syntax"
    assert by_name["BRK.B"]["method"] == "identifier_syntax"
    assert by_name["BRNQ26"]["method"] == "explicit_identifier"
    assert by_name["US0378331005"]["method"] == "identifier_syntax"


def test_a_bare_number_never_becomes_an_entity():
    """`id: 1609461599` is an identifier that leaked in, not a thing in the graph.

    Found on the live database: of 30 accepted entity links, 8 were bare Telegram
    numeric ids pulled out of a config listing by the `id`/`код`/`ticket` pattern.
    A letterless node cannot be resolved, merged or recognised later, and every
    document mentioning the same bare number collapses onto it. Identifiers worth
    naming keep their letters, which the assertions below hold on to.
    """
    entities = _extract_entities(
        "chat id 1609461599, код 6446814690; ticket PK-04-04, контракт ERC-20, лицензия GPL-3.0."
    )
    names = {item["name"] for item in entities}
    assert not {name for name in names if not any(ch.isalpha() for ch in name)}
    assert {"PK-04-04", "ERC-20", "GPL-3.0"} <= names


def test_entity_extraction_understands_named_infrastructure_without_person_noise():
    entities = _extract_entities("Сервер Atlas работает на Ubuntu 24.04.")
    assert entities == [
        {
            "name": "Ubuntu",
            "entity_type": "concept",
            "confidence": 0.92,
            "method": "explicit_technology_version",
            "version": "24.04",
            "matched_as": "Ubuntu 24.04",
        },
        {
            "name": "Atlas",
            "entity_type": "concept",
            "confidence": 0.89,
            "method": "explicit_infrastructure_marker",
        },
    ]


@pytest.mark.asyncio
async def test_local_model_advice_is_grounded_idempotent_and_never_promotes(settings, storage):
    class FakeLocalLLM:
        enabled = True
        model = "fake-local-model"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["priority"] == "background"
            assert kwargs["temperature"] == 0.0
            assert "недоверенные данные" in messages[0]["content"]
            return {
                "content": """```json
                {
                  "title": "Идея кеша Redis",
                  "summary": "Рассмотреть Redis как кеш для сервера.",
                  "knowledge_kind": "technical_note",
                  "importance": 0.61,
                  "tags": ["Redis", "кеш"],
                  "entities": [
                    {"name": "Redis", "entity_type": "concept", "confidence": 0.98,
                     "evidence": "Redis буквально указан в исходнике"},
                    {"name": "Kafka", "entity_type": "concept", "confidence": 0.99,
                     "evidence": "не существует в исходнике"}
                  ],
                  "recommended_action": "promote",
                  "confidence": 0.82,
                  "rationale": "Есть потенциально долговечная техническая идея, но её должен подтвердить пользователь."
                }
                ```"""
            }

    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    ingested = await pipeline.ingest_text(
        "alice",
        "Идея: когда-нибудь добавить Redis для кеша сервера",
        source_ref="advice:1",
    )
    assert ingested["action"] == "review"
    before = storage.get_inbox_item(ingested["inbox_id"], "alice")
    assert before is not None
    llm = FakeLocalLLM()

    result = await pipeline.advise_inbox_item(
        "alice",
        ingested["inbox_id"],
        llm=llm,
        requested_by="owner",
    )
    assert result["idempotent_replay"] is False
    assert result["model_advice"]["advisory_only"] is True
    assert result["model_advice"]["recommended_action"] == "promote"
    assert result["suggestions"]["title"] == "Идея кеша Redis"
    assert result["suggestions"]["knowledge_kind"] == "technical_note"
    model_entities = [
        item for item in result["suggestions"]["entities"] if item.get("method") == "local_model_advice"
    ]
    assert [item["name"] for item in model_entities] == ["Redis"]
    assert model_entities[0]["confidence"] == 0.79
    assert storage.find_entity_by_name("alice", "Redis") is None
    assert storage.find_entity_by_name("alice", "Kafka") is None

    after = storage.get_inbox_item(ingested["inbox_id"], "alice")
    assert after is not None
    assert after["status"] == "pending"
    assert after["reviewed_at"] is None
    assert after["reviewed_by"] is None
    assert after["knowledge_object_id"] is None
    assert after["promotion_score"] == before["promotion_score"]
    assert after["quality_score"] == before["quality_score"]
    assert storage.count_knowledge_objects("alice") == 0

    replay = await pipeline.advise_inbox_item(
        "alice",
        ingested["inbox_id"],
        llm=llm,
        requested_by="owner",
    )
    assert replay["idempotent_replay"] is True
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_retrieval_uses_graph_expansion_and_quality_ranking(storage):
    graph = KnowledgeGraph(storage)
    alpha = graph.create_entity("alice", "Alpha", EntityType.PROJECT)
    beta = graph.create_entity("alice", "Beta", EntityType.PROJECT)
    gamma = graph.create_entity("alice", "Gamma", EntityType.PROJECT)
    graph.create_relation(
        "alice",
        alpha["id"],
        beta["id"],
        RelationType.DEPENDS_ON,
        weight=1.0,
    )
    graph.create_relation(
        "alice",
        beta["id"],
        gamma["id"],
        RelationType.DEPENDS_ON,
        weight=1.0,
    )

    alpha_ko = _store_knowledge(
        storage,
        "alice",
        "Project Alpha migration plan and PostgreSQL configuration.",
        title="Alpha migration",
        quality=0.95,
        promotion=0.95,
        importance=0.8,
    )
    beta_ko = _store_knowledge(
        storage,
        "alice",
        "The blue deployment checklist is stored in the operations vault.",
        title="Deployment checklist",
        quality=0.9,
        promotion=0.9,
        importance=0.7,
    )
    gamma_ko = _store_knowledge(
        storage,
        "alice",
        "The contingency rollback procedure is approved for production.",
        title="Contingency rollback",
        quality=0.92,
        promotion=0.9,
        importance=0.72,
    )
    noisy = _store_knowledge(
        storage,
        "alice",
        "Что известно про Alpha PostgreSQL?",
        title="Что известно про Alpha PostgreSQL?",
        quality=0.05,
        promotion=0.05,
        importance=0.9,
        metadata={"promotion_assessment": {"category": "question", "action": "transient"}},
    )
    graph.link_knowledge_to_entity(alpha_ko["id"], alpha["id"], "alice")
    graph.link_knowledge_to_entity(beta_ko["id"], beta["id"], "alice")
    graph.link_knowledge_to_entity(gamma_ko["id"], gamma["id"], "alice")
    graph.link_knowledge_to_entity(noisy["id"], alpha["id"], "alice")

    result = await HybridSearcher(storage).search(
        "alice", "Alpha PostgreSQL", kg=graph, graph_expansion=True, limit=10
    )
    ids = [item["id"] for item in result["results"]]
    assert ids[0] == alpha_ko["id"]
    assert beta_ko["id"] in ids
    assert result["results"][ids.index(beta_ko["id"])]["_graph_score"] > 0
    assert ids.index(noisy["id"]) > ids.index(alpha_ko["id"])
    assert result["entity_matches"][0]["name"] == "Alpha"

    relational = await HybridSearcher(storage).search(
        "alice",
        "Что связано с Alpha через зависимости?",
        kg=graph,
        graph_expansion=True,
        limit=10,
    )
    relational_ids = [item["id"] for item in relational["results"]]
    assert gamma_ko["id"] in relational_ids
    gamma_result = relational["results"][relational_ids.index(gamma_ko["id"])]
    assert gamma_result["_graph_score"] >= 0.12
    assert gamma_result["_graph_evidence"]


@pytest.mark.asyncio
async def test_retrieval_respects_exact_identifiers_and_entity_only_graph_matches(storage):
    graph = KnowledgeGraph(storage)
    class_a = graph.create_entity("alice", "BRK.A", EntityType.OTHER)
    class_b = graph.create_entity("alice", "BRK.B", EntityType.OTHER)
    orion = graph.create_entity("alice", "Orion", EntityType.PROJECT)

    a_ko = _store_knowledge(
        storage,
        "alice",
        "Berkshire Hathaway Class A quote is 710000 USD.",
        title="BRK.A quote",
        quality=0.95,
        promotion=0.95,
        metadata={"source": "market-note"},
    )
    b_ko = _store_knowledge(
        storage,
        "alice",
        "Berkshire Hathaway Class B quote is 473.25 USD.",
        title="BRK.B quote",
        quality=0.95,
        promotion=0.95,
        metadata={"source": "market-note"},
    )
    orion_ko = _store_knowledge(
        storage,
        "alice",
        "The migration uses PostgreSQL 16 and finishes after the backup validation.",
        title="Database migration decision",
        quality=0.9,
        promotion=0.9,
    )
    graph.link_knowledge_to_entity(a_ko["id"], class_a["id"], "alice")
    graph.link_knowledge_to_entity(b_ko["id"], class_b["id"], "alice")
    graph.link_knowledge_to_entity(orion_ko["id"], orion["id"], "alice")

    searcher = HybridSearcher(storage)
    exact = await searcher.search("alice", "BRK.A quote", kg=graph, graph_expansion=True, limit=10)
    assert [item["id"] for item in exact["results"]] == [a_ko["id"]]
    assert exact["results"][0]["_score_components"]["identifier_coverage"] == 1.0

    graph_only = await searcher.search("alice", "Orion", kg=graph, graph_expansion=True, limit=10)
    assert graph_only["results"][0]["id"] == orion_ko["id"]
    assert graph_only["results"][0]["_field_matches"]["entities"] > 0
    assert graph_only["results"][0]["_graph_score"] > 0


@pytest.mark.asyncio
async def test_agent_context_distinguishes_personal_knowledge_and_followups(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    await pipeline.ingest_text(
        "alice",
        "Запомни: проект Orion использует PostgreSQL 16.",
        force_knowledge=True,
    )
    conversation = storage.create_conversation("alice", title="Orion")
    storage.store_message(conversation["id"], "alice", "user", "Что известно о проекте Orion?")
    history = storage.get_conversation_messages(conversation["id"], user_id="alice")

    runtime = AgentRuntime(settings, storage)
    context = await runtime._prepare_context(
        "alice",
        "А какая версия?",
        conversation["id"],
        prior_history=history,
        kg=graph,
        searcher=HybridSearcher(storage),
    )
    assert "Что известно о проекте Orion" in context.search_query
    assert context.answer_mode in {"personal_knowledge", "mixed"}
    assert context.knowledge_hits
    assert context.graph_context.get("entities")

    missing = await runtime._prepare_context(
        "bob",
        "Что ты помнишь обо мне?",
        storage.create_conversation("bob")["id"],
        prior_history=[],
        kg=graph,
        searcher=HybridSearcher(storage),
    )
    assert missing.answer_mode == "personal_knowledge_missing"
    assert missing.knowledge_hits == []


def test_legacy_cleanup_is_conservative_and_preserves_raw_and_versions(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    junk = _store_knowledge(
        storage,
        "alice",
        "Как настроить PostgreSQL?",
        title="Как настроить PostgreSQL?",
    )
    protected = _store_knowledge(
        storage,
        "alice",
        "Как настроить Redis?",
        title="Как настроить Redis?",
        metadata={"manually_promoted_from_inbox": "inbox_reviewed"},
    )

    candidates = pipeline.scan_legacy_quality("alice")
    ids = {item["knowledge_object"]["id"] for item in candidates}
    assert junk["id"] in ids
    assert protected["id"] not in ids
    assessment = next(item for item in candidates if item["knowledge_object"]["id"] == junk["id"])
    assert assessment["suspect"] is True
    assert assessment["recommended_action"] == "return_to_inbox"

    result = pipeline.return_knowledge_to_inbox(
        "alice",
        junk["id"],
        reviewed_by="owner",
        reason="question accidentally promoted by legacy classifier",
    )
    deleted = storage.get_knowledge_object(junk["id"], "alice")
    assert deleted is not None and deleted["deleted_at"]
    assert storage.get_raw_object(junk["raw_object_id"], "alice") is not None
    inbox = storage.get_inbox_item(result["inbox_id"], "alice")
    assert inbox is not None
    assert inbox["status"] == "pending"
    assert inbox["knowledge_object_id"] is None
    assert len(storage.list_knowledge_versions(junk["id"], "alice")) >= 3


@pytest.mark.asyncio
async def test_reenrichment_preview_is_non_destructive_and_apply_versions(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    item = _store_knowledge(
        storage,
        "alice",
        "Сервер Atlas работает на Ubuntu 24.04.",
        title="Legacy title",
        quality=0.2,
        promotion=0.2,
    )
    preview = pipeline.reenrich_knowledge("alice", item["id"], apply=False)
    assert preview["applied"] is False
    assert preview["suggestion"]["title"] != "Legacy title"
    assert storage.get_knowledge_object(item["id"], "alice")["version"] == 1

    applied = pipeline.reenrich_knowledge(
        "alice",
        item["id"],
        apply=True,
        reviewed_by="owner",
    )
    assert applied["applied"] is True
    assert applied["item"]["version"] == 2
    assert applied["item"]["quality_score"] >= 0.5
    assert applied["graph_links"]


def test_admin_quality_workflows_and_api_ingest_default(settings):
    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        transient = client.post(
            "/api/ingest",
            json={"content": "Как настроить PostgreSQL?", "source_ref": "api-question"},
            headers=headers,
        )
        assert transient.status_code == 200, transient.text
        assert transient.json()["action"] == "transient"
        assert transient.json()["promoted"] is False

        borderline = client.post(
            "/api/ingest",
            json={
                "content": "Проект Orion возможно позже перенесём на новый сервер",
                "source_ref": "api-borderline",
            },
            headers=headers,
        )
        assert borderline.status_code == 200, borderline.text
        pending = borderline.json()
        assert pending["queued_for_review"] is True

        inbox = client.get(
            f"/api/admin/inbox?user_id={LEGACY_OWNER_USER_ID}",
            headers=headers,
        )
        assert inbox.status_code == 200, inbox.text
        item = next(row for row in inbox.json()["items"] if row["id"] == pending["inbox_id"])
        assert item["raw_object"]["raw_content"].startswith("Проект Orion")
        assert item["suggestions"]["title"]

        promoted = client.post(
            f"/api/admin/inbox/{pending['inbox_id']}/classify",
            json={
                "user_id": LEGACY_OWNER_USER_ID,
                "status": "classified",
                "promote": True,
                "title": "Потенциальная миграция Orion",
                "summary": "Проверить целесообразность переноса Orion на новый сервер.",
                "knowledge_kind": "project",
                "importance": 0.65,
                "tags": ["orion", "migration"],
            },
            headers=headers,
        )
        assert promoted.status_code == 200, promoted.text
        knowledge_id = promoted.json()["item"]["knowledge_object_id"]

        inspection = client.get(
            f"/api/admin/knowledge/{knowledge_id}?user_id={LEGACY_OWNER_USER_ID}",
            headers=headers,
        )
        assert inspection.status_code == 200, inspection.text
        inspected = inspection.json()
        assert inspected["item"]["title"] == "Потенциальная миграция Orion"
        assert inspected["raw_object"]["source_ref"] == "api-borderline"
        assert inspected["versions"]

        preview = client.post(
            f"/api/admin/knowledge/{knowledge_id}/reenrich",
            json={"user_id": LEGACY_OWNER_USER_ID, "apply": False},
            headers=headers,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["applied"] is False
        assert preview.json()["suggestion"]["summary"]

        legacy = client.post(
            "/api/ingest",
            json={
                "content": "Почему сервер не работает?",
                "source_ref": "legacy-question",
                "force_knowledge": True,
            },
            headers=headers,
        )
        assert legacy.status_code == 200, legacy.text
        legacy_id = legacy.json()["knowledge_object"]["id"]
        import hmac

        legacy_body = "Почему сервер не работает?"
        second_body = "Почему это снова происходит каждый вечер без причины?"
        storage = app.state.storage
        safe_actions = ["return_to_inbox", "reclassify", "keep", "archive", "soft_delete"]
        missing_id = "ko_missing000000000000000000000000"
        unknown_user = "usr_cleanup_unknown_404"
        ordinary_secret = "cleanup-ordinary-token-lab001"
        foreign_user = "mallory"
        # Fix the relevant seed fields independently of enrichment defaults.
        storage.update_knowledge_fields(
            legacy_id,
            LEGACY_OWNER_USER_ID,
            title=legacy_body,
            knowledge_kind="note",
            quality_score=0.1,
            promotion_score=0.1,
        )
        protected = _store_knowledge(
            storage,
            LEGACY_OWNER_USER_ID,
            legacy_body,
            title=legacy_body,
            quality=0.1,
            promotion=0.1,
            metadata={"manually_promoted_from_inbox": "inbox_reviewed"},
        )
        second = _store_knowledge(
            storage,
            LEGACY_OWNER_USER_ID,
            second_body,
            title=second_body,
            quality=0.5,
            promotion=0.5,
        )
        storage.ensure_user(foreign_user, source="api-token", display_name="Mallory", preset_key="user")
        storage.create_api_token(
            foreign_user,
            hashlib.sha256(ordinary_secret.encode()).hexdigest(),
            label="ordinary",
            created_by="test",
        )
        foreign = _store_knowledge(
            storage,
            foreign_user,
            legacy_body,
            title=legacy_body,
            quality=0.1,
            promotion=0.1,
        )
        ordinary = {"Authorization": f"Bearer {ordinary_secret}"}
        protected_id, second_id, foreign_id = protected["id"], second["id"], foreign["id"]
        seed_ids = (legacy_id, protected_id, second_id, foreign_id, knowledge_id)
        private_text = (
            legacy_body,
            second_body,
            settings.api_token,
            ordinary_secret,
            "Проект Orion возможно позже перенесём на новый сервер",
            "Потенциальная миграция Orion",
        )

        def _business():
            # Full stored rows, including raw provenance and every version/inbox
            # for the two synthetic accounts; no response-derived membership.
            return {
                table: [
                    dict(row)
                    for row in storage.execute(
                        f"SELECT * FROM {table} WHERE user_id IN (?, ?) ORDER BY id",
                        (LEGACY_OWNER_USER_ID, foreign_user),
                    ).fetchall()
                ]
                for table in ("raw_objects", "knowledge_objects", "inbox", "knowledge_object_versions")
            }

        def _audit_rows():
            return [dict(row) for row in storage.execute("SELECT * FROM audit_log ORDER BY rowid").fetchall()]

        def _checked_request(method, url, *, status=200, audit=(), unchanged=True, **kwargs):
            business_before = _business()
            audit_before = _audit_rows()
            response = client.request(method, url, **kwargs)
            assert response.status_code == status, response.text
            audit_after = _audit_rows()
            assert audit_after[: len(audit_before)] == audit_before, "cleanup audit prefix"
            delta = audit_after[len(audit_before) :]
            assert [
                (row["action"], row["user_id"], row["target_type"], row["target_id"]) for row in delta
            ] == list(audit), "cleanup audit delta"
            dumped = json.dumps(delta, ensure_ascii=False)
            for needle in private_text:
                assert needle not in dumped
            assert "raw_content" not in dumped
            if unchanged:
                assert _business() == business_before, "cleanup business preservation"
            if status >= 400:
                for needle in (*seed_ids, *private_text):
                    assert needle not in response.text
            return response, delta

        # Literal fixture fields and results, with distinct risks 1.0 and 0.94:
        # neither timestamps nor randomly generated IDs decide the order.
        for kid, uid, body, score in (
            (legacy_id, LEGACY_OWNER_USER_ID, legacy_body, 0.1),
            (protected_id, LEGACY_OWNER_USER_ID, legacy_body, 0.1),
            (second_id, LEGACY_OWNER_USER_ID, second_body, 0.5),
            (foreign_id, foreign_user, legacy_body, 0.1),
        ):
            row = storage.get_knowledge_object(kid, uid)
            assert row is not None
            assert {
                key: row[key]
                for key in (
                    "user_id",
                    "content",
                    "title",
                    "knowledge_kind",
                    "quality_score",
                    "promotion_score",
                    "lifecycle_stage",
                    "deleted_at",
                )
            } == {
                "user_id": uid,
                "content": body,
                "title": body,
                "knowledge_kind": "note",
                "quality_score": score,
                "promotion_score": score,
                "lifecycle_stage": "active",
                "deleted_at": None,
            }
        assert json.loads(protected["metadata_json"]) == {"manually_promoted_from_inbox": "inbox_reviewed"}
        expected_views = [
            {
                "id": legacy_id,
                "suspect": True,
                "risk_score": 1.0,
                "reasons": [
                    "fresh_policy_question",
                    "question_like_content",
                    "question_title",
                    "very_short",
                    "low_stored_quality",
                    "low_stored_promotion",
                ],
                "protected": False,
                "protected_reasons": [],
                "recommended_action": "return_to_inbox",
            },
            {
                "id": second_id,
                "suspect": True,
                "risk_score": 0.94,
                "reasons": ["fresh_policy_question", "question_like_content", "question_title"],
                "protected": False,
                "protected_reasons": [],
                "recommended_action": "return_to_inbox",
            },
        ]

        def _item_view(row):
            return {
                "id": row["knowledge_object"]["id"],
                **{
                    key: row[key]
                    for key in (
                        "suspect",
                        "risk_score",
                        "reasons",
                        "protected",
                        "protected_reasons",
                        "recommended_action",
                    )
                },
            }

        preview_url = f"/api/admin/cleanup/legacy?user_id={LEGACY_OWNER_USER_ID}"
        apply_url = "/api/admin/cleanup/legacy/apply"
        apply_body = {
            "user_id": LEGACY_OWNER_USER_ID,
            "action": "return_to_inbox",
            "knowledge_ids": [legacy_id],
        }
        for method, url, kwargs in (
            ("GET", preview_url, {}),
            ("POST", apply_url, {"json": apply_body}),
        ):
            _checked_request(
                method,
                url,
                status=401,
                audit=[("auth.failed", "anonymous", "auth", "invalid_credentials")],
                **kwargs,
            )
            _checked_request(method, url, status=403, headers=ordinary, **kwargs)
        empty_ids, _ = _checked_request(
            "POST",
            apply_url,
            status=400,
            headers=headers,
            json={**apply_body, "knowledge_ids": []},
        )
        assert empty_ids.json() == {"detail": "knowledge_ids должен быть непустым списком"}
        bad_action, _ = _checked_request(
            "POST",
            apply_url,
            status=400,
            headers=headers,
            json={**apply_body, "action": "explode"},
        )
        assert bad_action.json() == {"detail": "Недопустимое действие очистки"}
        _checked_request("GET", preview_url + "&limit=0", status=422, headers=headers)
        # Independently bind the installation-keyed pseudonym for this exact
        # unknown user; no production sanitizer supplies the expected target.
        privacy_key = bytes.fromhex(
            storage.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()[0]
        )
        unknown_ref = (
            "user:ref:"
            + hmac.new(
                privacy_key,
                f"target:user\0{unknown_user}".encode(),
                hashlib.sha256,
            ).hexdigest()[:24]
        )
        unknown_get, unknown_delta = _checked_request(
            "GET",
            f"/api/admin/cleanup/legacy?user_id={unknown_user}",
            status=404,
            headers=headers,
            audit=[("admin.cleanup.read", LEGACY_OWNER_USER_ID, "user", unknown_ref)],
        )
        assert unknown_get.json() == {"detail": "Пользователь не найден"}
        assert unknown_delta[0]["before_json"] is None
        assert unknown_delta[0]["after_json"] is None
        assert unknown_user not in json.dumps(unknown_delta)
        unknown_post, _ = _checked_request(
            "POST",
            apply_url,
            status=404,
            headers=headers,
            json={**apply_body, "user_id": unknown_user},
        )
        assert unknown_post.json() == {"detail": "Пользователь не найден"}

        for limit, expected in ((1, expected_views[:1]), (500, expected_views)):
            preview_response, _ = _checked_request(
                "GET",
                preview_url + f"&limit={limit}&offset=0",
                headers=headers,
            )
            page_body = preview_response.json()
            assert set(page_body) == {"user_id", "items", "count", "total", "limit", "offset", "safe_actions"}
            assert {key: value for key, value in page_body.items() if key != "items"} == {
                "user_id": LEGACY_OWNER_USER_ID,
                "count": len(expected),
                "total": 2,
                "limit": limit,
                "offset": 0,
                "safe_actions": safe_actions,
            }
            assert [_item_view(row) for row in page_body["items"]] == expected, "cleanup page membership"
            assert not {protected_id, foreign_id, knowledge_id} & {
                row["knowledge_object"]["id"] for row in page_body["items"]
            }

        business_before_apply = _business()
        legacy_before = dict(storage.get_knowledge_object(legacy_id, LEGACY_OWNER_USER_ID))
        raw_before = dict(storage.get_raw_object(legacy_before["raw_object_id"], LEGACY_OWNER_USER_ID))
        versions_before = list(reversed(storage.list_knowledge_versions(legacy_id, LEGACY_OWNER_USER_ID)))
        request_body = {
            **apply_body,
            "knowledge_ids": [legacy_id, legacy_id, protected_id, missing_id],
            "require_suspect": True,
        }
        applied, delta = _checked_request(
            "POST",
            apply_url,
            headers=headers,
            json=request_body,
            unchanged=False,
            audit=[
                (
                    "admin.knowledge.cleanup.return_to_inbox",
                    LEGACY_OWNER_USER_ID,
                    "knowledge_object",
                    legacy_id,
                )
            ],
        )
        applied_body = applied.json()
        assert set(applied_body) == {"user_id", "action", "changed", "changed_count", "skipped"}
        assert applied_body["user_id"] == LEGACY_OWNER_USER_ID
        assert applied_body["action"] == "return_to_inbox"
        assert applied_body["changed_count"] == 1
        assert len(applied_body["changed"]) == 1
        changed = applied_body["changed"][0]
        assert set(changed) == {"knowledge_object_id", "status", "result"}
        assert changed["knowledge_object_id"] == legacy_id
        assert changed["status"] == "return_to_inbox"
        result = changed["result"]
        assert result["knowledge_object_id"] == legacy_id
        assert result["status"] == "returned_to_inbox"
        assert result["raw_object_id"] == raw_before["id"]
        inbox_id = result["inbox_id"]
        assert inbox_id
        assert applied_body["skipped"] == [
            {"id": protected_id, "reason": "not_flagged_by_quality_scan"},
            {"id": missing_id, "reason": "not_found"},
        ]
        stored = storage.get_knowledge_object(legacy_id, LEGACY_OWNER_USER_ID)
        assert stored is not None and stored["deleted_at"]
        assert stored["lifecycle_stage"] == "deleted"
        assert stored["raw_object_id"] == raw_before["id"]
        assert stored["content"] == legacy_body
        assert storage.get_raw_object(raw_before["id"], LEGACY_OWNER_USER_ID) == raw_before
        inbox_row = storage.get_inbox_item(inbox_id, LEGACY_OWNER_USER_ID)
        assert inbox_row is not None
        assert {
            key: inbox_row[key]
            for key in (
                "id",
                "user_id",
                "raw_object_id",
                "status",
                "knowledge_object_id",
                "suggested_action",
                "classification_notes",
            )
        } == {
            "id": inbox_id,
            "user_id": LEGACY_OWNER_USER_ID,
            "raw_object_id": raw_before["id"],
            "status": "pending",
            "knowledge_object_id": None,
            "suggested_action": "legacy_review",
            "classification_notes": "legacy quality cleanup",
        }
        reviewer = json.loads(stored["metadata_json"])["legacy_cleanup"]
        assert reviewer == {
            "reviewed": True,
            "action": "return_to_inbox",
            "reviewed_by": LEGACY_OWNER_USER_ID,
            "reason": "legacy quality cleanup",
        }
        versions_after = list(reversed(storage.list_knowledge_versions(legacy_id, LEGACY_OWNER_USER_ID)))
        assert versions_after[: len(versions_before)] == versions_before
        new_versions = versions_after[len(versions_before) :]
        assert [row["version"] for row in new_versions] == [
            legacy_before["version"] + 1,
            legacy_before["version"] + 2,
        ]
        assert stored["version"] == legacy_before["version"] + 2
        for version, lifecycle in zip(new_versions, ("active", "deleted"), strict=True):
            assert version["knowledge_object_id"] == legacy_id
            assert version["user_id"] == LEGACY_OWNER_USER_ID
            snapshot = json.loads(version["snapshot_json"])
            assert snapshot["id"] == legacy_id
            assert snapshot["raw_object_id"] == raw_before["id"]
            assert snapshot["content"] == legacy_body
            assert snapshot["version"] == version["version"]
            assert snapshot["lifecycle_stage"] == lifecycle
            assert json.loads(snapshot["metadata_json"])["legacy_cleanup"] == reviewer
        assert json.loads(new_versions[-1]["snapshot_json"])["deleted_at"] == stored["deleted_at"]
        business_after_apply = _business()
        assert business_after_apply["raw_objects"] == business_before_apply["raw_objects"]
        for table, key, target_id in (
            ("knowledge_objects", "id", legacy_id),
            ("knowledge_object_versions", "knowledge_object_id", legacy_id),
            ("inbox", "raw_object_id", raw_before["id"]),
        ):
            assert [row for row in business_after_apply[table] if row[key] != target_id] == [
                row for row in business_before_apply[table] if row[key] != target_id
            ]
        before_payload = json.loads(delta[0]["before_json"])
        content_digest = hashlib.sha256(legacy_body.encode()).hexdigest()
        content_ref = (
            "fpref_"
            + hmac.new(
                privacy_key,
                f"payload_fingerprint:content\0{content_digest}".encode(),
                hashlib.sha256,
            ).hexdigest()[:24]
        )
        assert before_payload == {
            "id": legacy_id,
            "title_chars": len(legacy_body),
            "knowledge_kind": "note",
            "lifecycle_stage": "active",
            "version": legacy_before["version"],
            "content_chars": len(legacy_body),
            "content_ref": content_ref,
            "created_at": legacy_before["created_at"],
            "updated_at": legacy_before["updated_at"],
        }
        after_payload = json.loads(delta[0]["after_json"])
        assert after_payload["knowledge_object_id"] == legacy_id
        assert "status" not in after_payload
        assert after_payload["status_chars"] == 15
        assert after_payload == {
            "knowledge_object_id": legacy_id,
            "status_chars": 15,
            "private_fields_count": 1,
            "private_items_count": 9,
        }
        assert content_digest not in json.dumps(delta)
        replay, replay_delta = _checked_request("POST", apply_url, headers=headers, json=request_body)
        assert replay.json() == {
            "user_id": LEGACY_OWNER_USER_ID,
            "action": "return_to_inbox",
            "changed_count": 0,
            "changed": [],
            "skipped": [
                {"id": legacy_id, "reason": "not_found"},
                {"id": protected_id, "reason": "not_flagged_by_quality_scan"},
                {"id": missing_id, "reason": "not_found"},
            ],
        }
        assert replay_delta == []
        assert _business() == business_after_apply, "cleanup replay complete business snapshot"


def test_legacy_cleanup_actions_are_explicit_versioned_and_provenance_safe(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    keep_item = _store_knowledge(
        storage,
        "alice",
        "Как настроить Redis?",
        title="Legacy Redis question",
        quality=0.1,
        promotion=0.1,
    )
    kept = pipeline.apply_legacy_cleanup(
        "alice",
        keep_item["id"],
        action="keep",
        reviewed_by="owner",
        reason="human confirmed this is useful context",
    )
    assert kept["deleted_at"] is None
    assert kept["quality_score"] >= 0.55
    assert kept["promotion_score"] >= 0.65
    assert json.loads(kept["metadata_json"])["legacy_cleanup"]["kept_as_knowledge"] is True

    archive_item = _store_knowledge(
        storage,
        "alice",
        "Почему старый сервер недоступен?",
        title="Legacy server question",
        quality=0.1,
        promotion=0.1,
    )
    archived = pipeline.apply_legacy_cleanup(
        "alice",
        archive_item["id"],
        action="archive",
        reviewed_by="owner",
    )
    assert archived["lifecycle_stage"] == "archived"
    assert archived["deleted_at"] is None
    assert storage.get_raw_object(archive_item["raw_object_id"], "alice") is not None

    delete_item = _store_knowledge(
        storage,
        "alice",
        "Привет, как дела?",
        title="Legacy chatter",
        quality=0.05,
        promotion=0.05,
    )
    deleted = pipeline.apply_legacy_cleanup(
        "alice",
        delete_item["id"],
        action="soft_delete",
        reviewed_by="owner",
    )
    assert deleted["status"] == "soft_deleted"
    deleted_row = storage.get_knowledge_object(delete_item["id"], "alice")
    assert deleted_row is not None and deleted_row["deleted_at"]
    assert storage.get_raw_object(delete_item["raw_object_id"], "alice") is not None

    reclassify_item = _store_knowledge(
        storage,
        "alice",
        "Идея: позже добавить Redis для кеша сервера Atlas.",
        title="Legacy idea",
        quality=0.1,
        promotion=0.1,
    )
    reclassified = pipeline.apply_legacy_cleanup(
        "alice",
        reclassify_item["id"],
        action="reclassify",
        reviewed_by="owner",
    )
    assert reclassified["version"] == 2
    assert reclassified["knowledge_kind"] in {"idea", "technical_note"}
    assert reclassified["quality_score"] > 0.1
    assert json.loads(reclassified["metadata_json"])["legacy_cleanup"]["action"] == "reclassify"

    for item in (keep_item, archive_item, delete_item, reclassify_item):
        assert len(storage.list_knowledge_versions(item["id"], "alice")) >= 2
