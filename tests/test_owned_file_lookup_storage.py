"""Storage-level document lookup keeps scope predicates ahead of finite pages."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import replace

import pytest

from friday.storage import FridayStorage, UnsupportedSchemaVersionError
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
    storage.ensure_user(uploader)
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
    folded_rows = storage.find_owned_files_by_filename("shared", "alice", "отчет.pdf")

    assert {row["id"] for row in rows} == {first.id, second.id}
    assert {row["id"] for row in folded_rows} == {first.id, second.id}
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


def test_filename_and_content_union_keeps_every_filename_lane(storage) -> None:
    first = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename="ШТАТКА ПОЛНАЯ.xlsx",
        body="body without the requested word",
    )
    second = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename="штатка резерв.xlsx",
        body="another unrelated body",
    )
    body_only = _file(
        storage,
        tenant="shared",
        uploader="alice",
        filename="notes.txt",
        body="внутри упоминается штатка",
        mime_type="text/plain",
    )

    page = storage.search_owned_files_by_term("shared", "alice", "штатка")

    by_id = {str(row["id"]): row for row in page["results"]}
    assert {first.id, second.id, body_only.id} <= set(by_id)
    assert "filename" in by_id[first.id]["match_kinds"]
    assert "filename" in by_id[second.id]["match_kinds"]
    assert "content" in by_id[body_only.id]["match_kinds"]
    assert page["complete"] is True
    assert page["total"] == 3
    assert all("raw_content" not in row and "metadata_json" not in row for row in page["results"])


def test_deduplicated_upload_names_are_durable_and_never_rewrite_raw(storage) -> None:
    storage.ensure_user("shared")
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="shared",
        source="upload",
        source_ref="uploader:alice:telegram-file:original",
        raw_content="canonical replay body",
        content_type="file",
        metadata_json={
            "filename": "7849.odt",
            "mime_type": "application/vnd.oasis.opendocument.text",
            "uploaded_by": "alice",
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id="shared",
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )

    assert storage.bind_owned_file_source_ref_alias(
        "shared", "alice", "telegram-file:replay-one", raw.id, "666.odt"
    )
    assert storage.bind_owned_file_source_ref_alias(
        "shared", "alice", "telegram-file:replay-two", raw.id, "latest-name.odt"
    )
    assert storage.bind_owned_file_source_ref_alias(
        "shared", "alice", "telegram-file:replay-one", raw.id, "changed.odt"
    )

    exact = storage.find_owned_files_by_filename("shared", "alice", "666.odt")
    union = storage.search_owned_files_by_term("shared", "alice", "666")
    persisted = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref=?",
        ("telegram-file:replay-one",),
    ).fetchone()
    canonical = storage.get_raw_object(raw.id, "shared")

    assert len(exact) == 1 and exact[0]["id"] == raw.id and exact[0]["filename"] == "666.odt"
    assert len(union["results"]) == 1
    assert union["results"][0]["filename"] == "666.odt"
    assert union["results"][0]["match_kinds"] == ["filename_alias"]
    assert persisted["supplied_filename"] == "666.odt"
    assert canonical is not None
    assert json.loads(canonical["metadata_json"])["filename"] == "7849.odt"
    assert storage.find_owned_files_by_filename("shared", "bob", "666.odt") == []
    assert storage.find_owned_files_by_filename("foreign", "alice", "666.odt") == []
    with storage.transaction() as conn:
        conn.execute("UPDATE inbox SET status='ignored' WHERE raw_object_id=?", (raw.id,))
    assert storage.find_owned_files_by_filename("shared", "alice", "666.odt") == []
    assert storage.search_owned_files_by_term("shared", "alice", "666")["results"] == []


@pytest.mark.parametrize(
    "bad_name",
    ["bad/name.odt", "bad\\name.odt", "nul\x00name.odt", "line\nname.odt", "line\rname.odt"],
)
def test_alias_filename_sql_guards_match_the_public_invariant(storage, bad_name: str) -> None:
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref="original",
        raw_content="body",
        content_type="file",
        metadata_json={"filename": "canonical.odt", "uploaded_by": "alice"},
    )
    storage.store_raw_object(raw)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id, uploaded_by, source_ref, raw_object_id, supplied_filename, created_at
               ) VALUES(?, ?, ?, ?, ?, ?)""",
            (
                "alice",
                "alice",
                "telegram-file:direct-sql",
                raw.id,
                bad_name,
                "2026-08-20T00:00:00+00:00",
            ),
        )


def test_alias_filename_is_immutable_and_only_allowed_on_file_carriers(storage) -> None:
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref="original",
        raw_content="body",
        content_type="file",
        metadata_json={"filename": "canonical.odt", "uploaded_by": "alice"},
    )
    storage.store_raw_object(raw)
    assert storage.bind_owned_file_source_ref_alias(
        "alice", "alice", "telegram-file:valid", raw.id, "first.odt"
    )
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            "UPDATE file_source_aliases SET supplied_filename='second.odt' WHERE source_ref=?",
            ("telegram-file:valid",),
        )
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id, uploaded_by, source_ref, raw_object_id, supplied_filename, created_at
               ) VALUES('alice','alice','telegram-message:not-a-file',?,
                        'name.odt','2026-08-20T00:00:00+00:00')""",
            (raw.id,),
        )
    assert (
        storage.bind_owned_file_source_ref_alias(
            "alice", "alice", "telegram-file:too-long", raw.id, "x" * 261
        )
        is False
    )


def test_current_schema_missing_alias_guard_fails_closed(settings, tmp_path) -> None:
    database = tmp_path / "tampered-alias-schema.sqlite3"
    tuned = replace(settings, database_path=database)
    initial = FridayStorage(tuned)
    initial.execute("SELECT 1")
    initial.close(final=True)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TRIGGER file_source_alias_filename_insert_guard")

    tampered = FridayStorage(tuned)
    try:
        with pytest.raises(UnsupportedSchemaVersionError, match="guards"):
            tampered.execute("SELECT 1")
    finally:
        tampered.close(final=True)
