"""Source-text search, and the verdict it must not overturn.

`raw_objects` holds the original ingested characters; the Knowledge Object holds a
normalised, often summarised version. Measured on the owner's database, **93% of
ingested characters** lived only in the former and no index covered them — an exact
phrase from a PDF was unfindable once review had condensed it.

The complication is the reason this file exists. On that same database the Inbox
breakdown is 65 ignored / 1 classified: nearly all of that unreachable text is
material the owner EXPLICITLY REJECTED. DATA_LIFECYCLE §3 makes "игнорировать" a
verdict, and this project has already shipped three separate paths that resurrected
rejected material. Making raw text searchable without honouring the verdict would
repeat that at the largest scale yet.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel, _source_anchor_context_projection
from friday.permissions import AuthorizationService
from friday.server import create_app
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)

PHRASE = "autovacuum_vacuum_scale_factor"


def _ingest(storage, user_id: str, text: str, *, status: InboxStatus | None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    if status is not None:
        storage.store_inbox_item(
            InboxItem(id=new_id("inbox"), user_id=user_id, raw_object_id=raw.id, status=status)
        )
    return raw.id


def test_source_text_is_searchable_and_the_verdict_is_obeyed(storage):
    storage.ensure_user("owner")
    pending = _ingest(storage, "owner", f"черновик {PHRASE} на проверке", status=InboxStatus.PENDING)
    classified = _ingest(storage, "owner", f"принято {PHRASE} в работу", status=InboxStatus.CLASSIFIED)
    archived = _ingest(storage, "owner", f"убрано из inbox {PHRASE}", status=InboxStatus.ARCHIVED)
    rejected = _ingest(storage, "owner", f"отвергнуто {PHRASE} совсем", status=InboxStatus.IGNORED)
    orphan = _ingest(storage, "owner", f"без inbox-строки {PHRASE}", status=None)

    found = {item["id"] for item in storage.search_raw_objects("owner", PHRASE, limit=50)}

    # Awaiting a decision, approved, and Inbox-tidied material is reachable.
    assert pending in found
    assert classified in found
    assert archived in found
    assert orphan in found
    # The verdict stands.
    assert rejected not in found, "search resurrected material the reviewer rejected"


@pytest.mark.asyncio
async def test_explicit_agent_source_search_reads_pending_owned_file_but_not_rejected(
    settings,
    storage,
):
    owner = "source-tool-owner"
    neighbour = "source-tool-neighbour"
    storage.ensure_user(owner, preset_key="owner")
    storage.ensure_user(neighbour, preset_key="owner")
    target = "Иванов — ведущий инженер по эксплуатации"
    kept = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-kept",
        raw_content=("служебное вступление\n" * 80) + target,
        content_type="file",
        metadata_json={"filename": "штатное расписание.docx"},
    )
    rejected = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-rejected",
        raw_content=f"отклонённая копия: {target}",
        content_type="file",
        metadata_json={"filename": "отклонено.docx"},
    )
    foreign = RawObject(
        id=new_id("raw"),
        user_id=neighbour,
        source="upload",
        source_ref="opaque-foreign",
        raw_content=f"чужой материал: {target}",
        content_type="file",
        metadata_json={"filename": "чужое.docx"},
    )
    for raw in (kept, rejected, foreign):
        storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=kept.id, status=InboxStatus.PENDING)
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=owner,
            raw_object_id=rejected.id,
            status=InboxStatus.IGNORED,
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=neighbour,
            raw_object_id=foreign.id,
            status=InboxStatus.PENDING,
        )
    )

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "Иванов должность", "limit": 20},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["shown"] == 1
    assert result.data["coverage"] == {
        "complete": True,
        "limit": 20,
        "candidates_scanned": 1,
        "candidate_cap": 100,
        "focus_conjunctive": False,
        "focus_match_found": False,
        "focus_fallback_contextual": False,
        "ignored_excluded": True,
    }
    [item] = result.data["results"]
    assert item["raw_object_id"] == kept.id
    assert item["title"] == "штатное расписание.docx"
    assert item["review_status"] == "pending"
    assert item["promoted"] is False
    assert target in item["excerpt"]
    assert rejected.id not in str(result.data)
    assert foreign.id not in str(result.data)


@pytest.mark.asyncio
async def test_source_search_requires_knowledge_read(settings, storage):
    storage.ensure_user("source-guest", preset_key="guest")
    authorization = AuthorizationService(storage)
    authorization.deny_permission("source-guest", "knowledge.read")
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": PHRASE},
        actor=authorization.actor_for_user("source-guest", source="test"),
    )

    assert result.success is False


def test_source_search_is_detailed_for_file_work_and_withheld_from_small_talk(settings, storage):
    storage.ensure_user("source-routing-owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    actor = authorization.actor_for_user("source-routing-owner", source="test")

    file_tools = {
        str((item.get("function") or {}).get("name") or "")
        for item in kernel.get_tool_definitions(actor, topic="файл")
    }
    household_tools = {
        str((item.get("function") or {}).get("name") or "")
        for item in kernel.get_tool_definitions(actor, topic="быт")
    }

    assert "source_search" in file_tools
    assert "source_search" not in household_tools


@pytest.mark.asyncio
async def test_source_search_page_reaches_the_model_without_tail_truncation(settings, storage):
    owner = "source-page-owner"
    storage.ensure_user(owner, preset_key="owner")
    for index in range(20):
        raw = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"source-{index}",
            raw_content=(f"PAGE-SOURCE-{index:02d} {PHRASE} " + "длинное окружение " * 120),
            content_type="file",
            metadata_json={"filename": f"Материал {index:02d}.docx"},
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=raw.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": PHRASE, "limit": 20},
        actor=authorization.actor_for_user(owner, source="test"),
    )
    rendered = result.to_llm_message()

    assert result.success is True
    assert result.data["shown"] == 20
    assert result.data["coverage"]["complete"] is False
    assert len(rendered) < 12_000
    assert "Материал 00.docx" in rendered
    assert "Материал 19.docx" in rendered
    assert result.truncated is False


@pytest.mark.asyncio
async def test_source_search_uses_a_separate_focus_without_broadening_retrieval(settings, storage):
    owner = "source-focus-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-focus-target",
        raw_content=("Иванов\n" * 1_000) + "Иванов — ведущий инженер по эксплуатации\n",
        content_type="file",
        metadata_json={"filename": "synthetic-focus-target.docx"},
    )
    predicate_noise = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-focus-noise",
        raw_content="Должность: посторонний предикат без искомой фамилии",
        content_type="file",
        metadata_json={"filename": "synthetic-focus-noise.docx"},
    )
    anchor_noise = [
        RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"opaque-anchor-noise-{index}",
            raw_content="Иванов\n" * 200,
            content_type="file",
            metadata_json={"filename": f"synthetic-anchor-noise-{index:02d}.docx"},
        )
        for index in range(30)
    ]
    for raw in (target, predicate_noise, *anchor_noise):
        storage.store_raw_object(raw)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=raw.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["query"] == "Иванов"
    assert result.data["focus"] == "Иванов должность"
    assert result.data["shown"] == 1
    assert result.data["coverage"]["focus_match_found"] is False
    assert result.data["coverage"]["focus_fallback_contextual"] is True
    [item] = result.data["results"]
    assert item["raw_object_id"] == target.id
    assert "Иванов — ведущий инженер по эксплуатации" in item["excerpt"]
    assert item["focus_match_kind"] == "anchor_context"
    assert predicate_noise.id not in str(result.data)
    assert all(noise.id not in str(result.data) for noise in anchor_noise)


@pytest.mark.asyncio
async def test_source_search_never_cross_joins_a_far_predicate_in_the_same_document(settings, storage):
    owner = "source-same-window-owner"
    storage.ensure_user(owner, preset_key="owner")
    source = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-same-window",
        raw_content=("Иванов\n" * 1_000)
        + ("нейтральный раздел без кадровых сведений\n" * 100)
        + "Петров\nДолжность: генеральный директор\n",
        content_type="file",
        metadata_json={"filename": "synthetic-same-window.docx"},
    )
    storage.store_raw_object(source)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=source.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["shown"] == 0
    assert result.data["results"] == []
    assert "Петров" not in str(result.data)
    assert "генеральный директор" not in str(result.data)
    assert result.data["coverage"]["focus_match_found"] is False


@pytest.mark.asyncio
async def test_source_search_context_boilerplate_cannot_page_out_an_implicit_value(settings, storage):
    owner = "source-context-rank-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-context-target",
        raw_content="Иванов — ведущий инженер по эксплуатации",
        content_type="file",
        metadata_json={"filename": "target.docx"},
    )
    storage.store_raw_object(target)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=target.id))
    for index in range(30):
        noise = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"opaque-context-noise-{index}",
            raw_content=(
                "Список сотрудников организации: Иванов. "
                "Дополнительные сведения об обязанностях отсутствуют полностью."
            ),
            content_type="file",
            metadata_json={"filename": f"noise-{index:02d}.docx"},
        )
        storage.store_raw_object(noise)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=noise.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert any(item["raw_object_id"] == target.id for item in result.data["results"])
    assert "ведущий инженер по эксплуатации" in str(result.data)


@pytest.mark.asyncio
async def test_source_search_maximum_metadata_page_remains_valid_untruncated_json(
    settings,
    storage,
    monkeypatch,
):
    owner = "source-envelope-owner"
    storage.ensure_user(owner, preset_key="owner")
    rows = [
        {
            "id": f"raw-{index:02d}-" + ("r" * 70),
            "content_type": "application/synthetic-" + ("x" * 58),
            "received_at": "2026-08-10T00:00:00.000000+00:00-extra",
            "inbox_status": "pending-review-state-" + ("s" * 19),
            "knowledge_object_id": None,
            "_raw_metadata": {"filename": f"Материал-{index:02d}-" + ("т" * 248)},
            "_raw_content": ("Иванов " + ("контекст " * 42) + f"Должность: ведущий инженер {index:02d}"),
        }
        for index in range(10)
    ]

    def fake_search_raw_objects(user_id, query, *, limit, include_content):
        assert user_id == owner
        assert query == "Иванов"
        assert limit == 100
        assert include_content is True
        return rows

    monkeypatch.setattr(storage, "search_raw_objects", fake_search_raw_objects)
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )
    rendered = result.to_llm_message()

    assert result.success is True
    assert result.truncated is False
    assert len(rendered.removeprefix("Результат source_search:\n")) < 12_000
    parsed = json.loads(rendered.removeprefix("Результат source_search:\n"))
    assert parsed["shown"] == 10
    assert len(parsed["results"]) == 10
    assert "Должность: ведущий инженер 09" in parsed["results"][-1]["excerpt"]
    assert parsed["results"][-1]["focus_match_kind"] == "full"


@pytest.mark.parametrize(
    ("source_name", "query", "expected"),
    [
        ("Иванов", "иванов", True),
        ("Иванова", "иванов", True),
        ("Иванову", "иванов", True),
        ("Ивановым", "иванов", True),
        ("Ивановский", "иванов", False),
        ("Иванович", "иванов", False),
        ("Петровский", "петровск", True),
        ("Петровского", "петровск", True),
        ("Петровскому", "петровск", True),
        ("Петровским", "петровск", True),
    ],
)
def test_source_anchor_uses_closed_surname_forms(source_name, query, expected):
    excerpt, matched, context = _source_anchor_context_projection(
        query,
        f"{query} должност",
        f"{source_name}\nДолжность: ведущий инженер",
        max_chars=600,
    )

    assert bool(excerpt) is expected
    if expected:
        assert source_name in excerpt
        assert matched == 2
        assert context >= 2


@pytest.mark.parametrize(
    ("focus", "text"),
    [
        ("иванов рол", "Иванов\nПароль: PRIVATE-VALUE"),
        ("иванов рол", "Иванов\nКонтроль: PRIVATE-VALUE"),
        ("иванов позици", "Иванов\nПозиционирование продукта"),
        ("иванов должност", "Иванов\nДолжностная инструкция"),
    ],
)
def test_source_focus_does_not_match_unrelated_token_substrings(focus, text):
    excerpt, matched, _context = _source_anchor_context_projection(
        "иванов",
        focus,
        text,
        max_chars=600,
    )

    assert "Иванов" in excerpt
    assert matched == 1


def test_source_projection_preserves_original_offsets_and_table_record_boundaries():
    unicode_text = (
        ("ﬁ" * 500) + ("before " * 30) + "\nИванов\nДолжность: ведущий инженер\n" + ("after " * 1_000)
    )
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        unicode_text,
        max_chars=600,
    )
    assert "Иванов\nДолжность: ведущий инженер" in excerpt
    assert matched == 2
    assert context >= 2

    table = "\n".join(
        ["Фамилия | Примечание | Должность"]
        + [f"Петров-{index:02d} | заметка | генеральный директор" for index in range(20)]
        + ["Иванов | " + ("длинное примечание " * 80) + " | Должность: ведущий инженер"]
        + [f"Сидоров-{index:02d} | заметка | начальник отдела" for index in range(20)]
    )
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        table,
        max_chars=480,
    )
    assert excerpt == "Фамилия | Должность\nИванов | Должность: ведущий инженер"
    assert matched == 2
    assert context >= 2
    assert "Петров" not in excerpt
    assert "Сидоров" not in excerpt

    for implicit_table in (
        "Иванов | ведущий инженер",
        "ФИО | Штатная единица\nИванов | ведущий инженер",
    ):
        excerpt, matched, context = _source_anchor_context_projection(
            "иванов",
            "иванов должност",
            implicit_table,
            max_chars=480,
        )
        assert "Иванов" in excerpt
        assert "ведущий инженер" in excerpt
        assert matched == 1
        assert context >= 2


def test_source_projection_accepts_a_safe_preceding_field_but_not_a_neighbour_record():
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Должность: ведущий инженер\nИванов",
        max_chars=600,
    )
    assert excerpt == "Должность: ведущий инженер\nИванов"
    assert matched == 2
    assert context >= 2

    hostile, hostile_matched, hostile_context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Петров\nДолжность: генеральный директор\nИванов",
        max_chars=600,
    )
    assert hostile == "Иванов"
    assert hostile_matched == 1
    assert hostile_context == 0


def test_source_projection_rejects_a_field_label_without_a_value():
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Иванов\nДолжность:",
        max_chars=600,
    )
    assert excerpt == "Иванов\nДолжность:"
    assert matched == 2
    assert context == 0


def test_a_soft_deleted_source_is_not_reachable(storage):
    storage.ensure_user("owner")
    raw_id = _ingest(storage, "owner", f"будет удалено {PHRASE}", status=InboxStatus.PENDING)
    assert any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))

    # No public soft-delete for a Raw Object (purge removes it outright), so mark
    # it the way the column is meant to be used and check the query honours it.
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at=? WHERE id=?", ("2026-07-27T00:00:00Z", raw_id))
    assert not any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))


def test_source_search_is_tenant_scoped(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    _ingest(storage, "alice", f"личное {PHRASE} алисы", status=InboxStatus.PENDING)
    assert storage.search_raw_objects("alice", PHRASE)
    assert storage.search_raw_objects("bob", PHRASE) == []


def test_a_foreign_inbox_row_cannot_hide_or_relabel_an_owned_source(storage):
    """Every correlated child row must prove the same tenant as its Raw parent."""

    storage.ensure_user("owner")
    storage.ensure_user("foreign")
    raw_id = _ingest(storage, "owner", f"tenant correlation {PHRASE}", status=None)
    foreign_rows = [
        InboxItem(
            id=new_id("inbox"),
            user_id="foreign",
            raw_object_id=raw_id,
            status=status,
            created_at=created_at,
        ).to_row()
        for status, created_at in (
            (InboxStatus.IGNORED, "2026-08-08T00:00:00+00:00"),
            (InboxStatus.CLASSIFIED, "2026-08-08T00:00:01+00:00"),
        )
    ]
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
               VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
            foreign_rows,
        )

    found = storage.search_raw_objects("owner", PHRASE)
    owned = next(item for item in found if item["id"] == raw_id)
    assert owned["inbox_status"] is None
    for row in foreign_rows:
        assert storage.get_inbox_item(row["id"], "foreign") is None
    assert storage.get_inbox_by_raw(raw_id, "foreign") is None
    assert storage.find_inbox_by_raw(raw_id, "foreign") is None
    assert storage.count_inbox("foreign") == 0
    assert storage.list_inbox("foreign") == []
    assert storage.list_inbox_detailed("foreign") == []
    assert storage.group_pending_inbox("foreign")["items_total"] == 0


def test_raw_replay_keys_cannot_reopen_a_quarantined_source(storage):
    """Source-ref/hash/text-hash replay readers share the full raw dependency guard."""

    user_id = "alice"
    sentinel = "PRIVATE RAW REPLAY SENTINEL"
    content_hash = "a" * 64
    text_hash = "b" * 64
    storage.ensure_user(user_id)
    raw = RawObject(
        id="raw-private-replay",
        user_id=user_id,
        source="agent_tool",
        source_ref="private-replay-ref",
        raw_content=sentinel,
        content_type="file",
        content_hash=content_hash,
        metadata_json={
            "text_sha256": text_hash,
            "candidate_type": "memory_save",
            "requested_by": "alice",
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id="inbox-private-replay",
            user_id=user_id,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )
    knowledge = KnowledgeObject(
        id="ko-private-replay",
        user_id=user_id,
        raw_object_id=raw.id,
        content=sentinel,
        content_type="text",
        title=sentinel,
    )
    storage.store_knowledge_object(knowledge)
    hidden = Entity(
        id="ent-private-replay",
        user_id=user_id,
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(hidden)
    storage.link_knowledge_entity(user_id, knowledge.id, hidden.id, status="accepted")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_raw_object(raw.id, user_id) is None
    assert storage.find_raw_by_source_ref(user_id, raw.source, raw.source_ref) is None
    assert storage.find_file_by_content_hash(user_id, content_hash) is None
    assert storage.find_file_by_extracted_text(user_id, text_hash) is None
    assert (
        storage.find_fresh_agent_candidate(
            user_id,
            raw.source,
            "memory_save",
            content_hash,
            requested_by="alice",
            since="2000-01-01T00:00:00Z",
        )
        is None
    )


def test_the_index_is_only_ever_read_through_the_filtered_helper():
    """Structural, because a forgotten filter is exactly how the previous three went.

    `raw_fts` holds terms derived from EVERY raw object, rejected ones included — a
    deliberate choice, so that returning an ignored item to pending makes it
    reachable again without an index rebuild. The price is that a second query
    against `raw_fts` without the verdict filter would expose rejected material, so
    there must not be one.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "raw_fts" not in source:
            continue
        # The schema declares it; storage/_intake.py is the one reader.
        if path.name in {"_base.py", "_core.py", "_intake.py"}:
            continue
        offenders.append(str(path.relative_to(root)))
    assert not offenders, f"raw_fts is queried outside the filtered helper: {offenders}"

    intake = (root / "storage" / "_intake.py").read_text(encoding="utf-8")
    tree = ast.parse(intake)
    readers = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "raw_fts" in ast.get_source_segment(intake, node)
    ]
    assert readers == ["search_raw_objects"], f"a second reader of raw_fts appeared: {readers}"


def test_source_text_only_reaches_the_agent_through_the_explicit_filtered_tool():
    """Source text never becomes ambient recall; one explicit tool may read it.

    Pending uploads must be searchable when the person asks about an uploaded
    source, but they must not silently enter HybridSearcher or every prompt.  The
    execution kernel is the single capability-gated bridge and still calls the
    verdict-aware helper which excludes ignored/private material.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    for relative in ("retrieval/__init__.py", "agent_runtime/__init__.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "search_raw_objects" not in source, f"{relative} reached into source text"
    kernel = (root / "execution_kernel" / "__init__.py").read_text(encoding="utf-8")
    assert kernel.count("storage.search_raw_objects") == 1
    assert '"source_search"' in kernel


def test_source_search_over_http_excludes_rejected_material(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        from friday.permissions import LEGACY_OWNER_USER_ID

        storage.ensure_user(LEGACY_OWNER_USER_ID)
        kept = _ingest(storage, LEGACY_OWNER_USER_ID, f"оставлено {PHRASE}", status=InboxStatus.PENDING)
        _ingest(storage, LEGACY_OWNER_USER_ID, f"отклонено {PHRASE}", status=InboxStatus.IGNORED)

        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.get("/api/knowledge/sources", params={"q": PHRASE}, headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [kept]
        assert body["excludes"] == "ignored"
        assert "_raw_content" not in str(body)
        assert "_raw_metadata" not in str(body)

        # Unauthenticated callers get nothing.
        assert client.get("/api/knowledge/sources", params={"q": PHRASE}).status_code == 401


def test_one_rejection_hides_the_source_even_among_several_inbox_rows(storage):
    """A Raw Object can carry SEVERAL Inbox rows, and a join let it through.

    `ingest_text` returns the existing raw object on an idempotent replay while
    still creating a review row, so `raw_object_id` is not unique in `inbox`. The
    first version of this query joined on the row and admitted the object whenever
    any single row was not the rejection — reproduced, and it returned rejected
    text. The test is `NOT EXISTS ... status='ignored'`: any rejection hides it.
    """
    storage.ensure_user("owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="owner",
        source="upload",
        source_ref=new_id("src"),
        raw_content=f"две строки inbox {PHRASE}",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.IGNORED)
    )
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.PENDING)
    )
    assert (
        storage.execute("SELECT COUNT(*) AS c FROM inbox WHERE raw_object_id=?", (raw.id,)).fetchone()["c"]
        == 2
    )

    assert storage.search_raw_objects("owner", PHRASE) == []


def test_the_index_is_rebuilt_over_rows_that_predate_it(settings, tmp_path, simulate_legacy_schema):
    """An external-content FTS table created over existing rows starts EMPTY.

    The rebuild is guarded on "did this table already exist", and probing that
    AFTER running the DDL always answers yes — so the guard skipped the rebuild and
    left an index that reports rows and matches nothing. Caught only by searching a
    copy of the owner's real database, where every query returned zero.
    """
    from dataclasses import replace

    from friday.storage import FridayStorage

    database = tmp_path / "predates.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        first.ensure_user("owner")
        _ingest(first, "owner", f"записано до индекса {PHRASE}", status=InboxStatus.PENDING)
        # Drop the index and its triggers: the state a schema-16 database is in.
        with first.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS raw_fts")
            for name in ("raw_objects_ai", "raw_objects_ad", "raw_objects_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    finally:
        first.close(final=True)

    with sqlite3.connect(database) as legacy:
        simulate_legacy_schema(legacy, 16)

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.search_raw_objects("owner", PHRASE), "the index was not rebuilt over existing rows"
    finally:
        migrated.close(final=True)
