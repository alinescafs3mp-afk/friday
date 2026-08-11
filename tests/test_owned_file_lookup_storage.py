"""Storage-level document lookup keeps scope predicates ahead of finite pages."""

from __future__ import annotations

import unicodedata

from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)


def _file(
    storage,
    *,
    tenant: str,
    uploader: str,
    filename: str,
    body: str,
    mime_type: str = "application/pdf",
    status: InboxStatus | None = InboxStatus.PENDING,
    deleted: bool = False,
) -> RawObject:
    storage.ensure_user(tenant)
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="synthetic-upload",
        source_ref=new_id("source"),
        raw_content=body,
        content_type="file",
        metadata_json={
            "filename": filename,
            "mime_type": mime_type,
            "uploaded_by": uploader,
        },
    )
    storage.store_raw_object(raw)
    if status is not None:
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=tenant,
                raw_object_id=raw.id,
                status=status,
            )
        )
    if deleted:
        with storage.transaction() as connection:
            connection.execute(
                "UPDATE raw_objects SET deleted_at=? WHERE id=?",
                ("2026-08-11T00:00:00+00:00", raw.id),
            )
    return raw


def _make_private(storage, raw: RawObject) -> None:
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=raw.user_id,
        raw_object_id=raw.id,
        content=raw.raw_content,
        content_type="text",
        title="private lookup dependency",
    )
    storage.store_knowledge_object(knowledge)
    entity = Entity(
        id=new_id("ent"),
        user_id=raw.user_id,
        name=f"Private lookup entity {raw.id}",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(entity)
    storage.link_knowledge_entity(
        raw.user_id,
        knowledge.id,
        entity.id,
        status="accepted",
    )
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (entity.id, "another-person", "2026-08-11T00:00:00+00:00"),
        )


def _excluded_files(storage, *, filename: str, body: str) -> list[RawObject]:
    ignored = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename=filename,
        body=body,
        status=InboxStatus.IGNORED,
    )
    deleted = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename=filename,
        body=body,
        deleted=True,
    )
    wrong_uploader = _file(
        storage,
        tenant="shared",
        uploader="bob",
        filename=filename,
        body=body,
    )
    foreign_tenant = _file(
        storage,
        tenant="foreign",
        uploader="alice",
        filename=filename,
        body=body,
    )
    audio = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename=filename,
        body=body,
        mime_type="audio/ogg",
    )
    private = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename=filename,
        body=body,
    )
    _make_private(storage, private)
    return [ignored, deleted, wrong_uploader, foreign_tenant, audio, private]


def test_exact_filename_lookup_proves_ambiguity_after_all_scope_filters(storage) -> None:
    first = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename="Отчёт.PDF",
        body="VISIBLE-FILENAME-FIRST",
    )
    second = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename=unicodedata.normalize("NFD", "ОТЧЁТ.pdf"),
        body="VISIBLE-FILENAME-SECOND",
    )
    excluded = _excluded_files(
        storage,
        filename="отчёт.pdf",
        body="EXCLUDED-FILENAME-CANARY",
    )

    rows = storage.find_owned_files_by_filename("shared", "alice", "отчёт.pdf")

    assert {row["id"] for row in rows} == {first.id, second.id}
    assert len(rows) == 2
    assert not ({raw.id for raw in excluded} & {row["id"] for row in rows})
    assert all(set(row) == {"id", "content_type", "received_at", "filename"} for row in rows)
    assert storage.find_owned_files_by_filename("shared", "", "отчёт.pdf") == []
    assert storage.find_owned_files_by_filename("shared", "alice", "x" * 261) == []


def test_owned_content_search_has_a_65th_sentinel_after_scope_filters(storage) -> None:
    visible = [
        _file(
            storage,
            tenant="shared",
            uploader="alice",
            filename=f"visible-{index:02d}.txt",
            body=f"OMEGASENTINEL visible row {index:02d}",
            mime_type="text/plain",
        )
        for index in range(65)
    ]
    unique = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename="unique.txt",
        body="UNIQUECONTENTMARKER only visible result",
        mime_type="text/plain",
    )
    excluded = _excluded_files(
        storage,
        filename="excluded.txt",
        body="OMEGASENTINEL UNIQUECONTENTMARKER excluded row",
    )

    saturated = storage.search_owned_file_content(
        "shared",
        "alice",
        "OMEGASENTINEL",
        limit=1_000,
    )

    visible_ids = {raw.id for raw in visible}
    saturated_ids = {row["id"] for row in saturated["results"]}
    assert saturated["complete"] is False
    assert saturated["available"] is True
    assert saturated["limit"] == 64
    assert saturated["matched_at_least"] == 65
    assert len(saturated_ids) == 64
    assert saturated_ids < visible_ids
    assert not ({raw.id for raw in excluded} & saturated_ids)
    assert all("raw_content" not in row and "metadata_json" not in row for row in saturated["results"])

    scoped = storage.search_owned_file_content(
        "shared",
        "alice",
        "UNIQUECONTENTMARKER",
        limit=64,
    )
    assert scoped["available"] is True
    assert scoped["complete"] is True
    assert scoped["matched_at_least"] == 1
    assert [row["id"] for row in scoped["results"]] == [unique.id]
    assert storage.search_owned_file_content("shared", "alice", "")["complete"] is False
    assert storage.search_owned_file_content("shared", "", "OMEGASENTINEL")["available"] is False
