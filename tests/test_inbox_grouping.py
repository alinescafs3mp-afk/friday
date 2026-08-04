"""Reviewing an import means recognising the noise, not reading five thousand files.

`jericho import` made a large pending queue the normal state. The Inbox could already
select and bulk-dismiss, but it could not *express* a selection: there was no way to say
"all the .py files" or "everything under Загрузки". Sorting did not help either —
measured on a real import of 187 files, ``promotion_score`` had p25 = median = p75 =
0.90 and separated nothing, while (extension x suggested_action) collapsed the queue
into 16 groups whose largest held 154 items.

Two constraints shaped the design, both verified in the code rather than assumed:

* **Grouping is read-only.** It hands back the ids it grouped, and the caller passes
  them to ``/inbox/bulk``, which already refuses to canonize anything. A grouping with
  its own mutation path would be a second door into the review gate.
* **No new table.** ``purge`` hard-deletes inbox rows with ``PRAGMA foreign_keys=ON``,
  so anything ``REFERENCES inbox(id)`` without a cascade would break purge — and
  therefore backups, which run purge's sibling paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import RawObject, new_id


def _pending(storage, *, source: str, path: str | None, content_type: str = "text/plain") -> str:
    """One pending Inbox item with the provenance the grouping reads."""
    metadata = {"import_source_path": path} if path else {}
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source=source,
            source_ref=f"sha256:{new_id('x')}",
            raw_content="содержимое",
            content_type=content_type,
            content_hash=new_id("h") * 2,
            metadata_json=metadata,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    inbox_id = new_id("inbox")
    storage.execute(
        "INSERT INTO inbox (id, user_id, raw_object_id, status, suggested_action, "
        "promotion_score, quality_score, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (inbox_id, "alice", raw.id, "pending", "promote", 0.9, 0.9, datetime.now(UTC).isoformat()),
    )
    storage.conn.commit()
    return inbox_id


@pytest.fixture
def seeded(storage):
    storage.ensure_user("alice", source="upload")
    for index in range(5):
        _pending(storage, source="upload", path=f"/home/u/Проекты/код/mod{index}.py")
    for index in range(2):
        _pending(storage, source="upload", path=f"/home/u/Документы/note{index}.md")
    _pending(storage, source="telegram", path=None)
    return storage


def test_grouping_by_extension_collapses_the_queue(seeded):
    groups = seeded.group_pending_inbox("alice", by="extension")["groups"]

    by_key = {group["key"]: group for group in groups}
    assert by_key[".py"]["total"] == 5
    assert by_key[".md"]["total"] == 2
    # A Telegram message has no file to take an extension from; it groups by its type
    # rather than vanishing from the view.
    assert "text/plain" in by_key
    assert sum(group["total"] for group in groups) == 8
    # Largest first: the biggest pile is the one worth one decision.
    assert [group["total"] for group in groups] == sorted((group["total"] for group in groups), reverse=True)


def test_grouping_by_directory_uses_the_immediate_parent(seeded):
    groups = {group["key"]: group["total"] for group in seeded.group_pending_inbox("alice", by="directory")["groups"]}

    assert groups["/home/u/Проекты/код"] == 5
    assert groups["/home/u/Документы"] == 2
    # Not everything comes from an import, and those items must stay visible.
    assert groups["(не из импорта)"] == 1


def test_grouping_by_source_separates_import_from_chat(seeded):
    groups = {group["key"]: group["total"] for group in seeded.group_pending_inbox("alice", by="source")["groups"]}
    assert groups == {"upload": 7, "telegram": 1}


def test_a_group_carries_its_members_and_says_when_it_is_truncated(storage):
    storage.ensure_user("alice", source="upload")
    for index in range(250):
        _pending(storage, source="upload", path=f"/home/u/Загрузки/f{index}.bin")

    group = storage.group_pending_inbox("alice", by="extension", limit_ids=200)["groups"][0]

    assert group["total"] == 250
    assert len(group["inbox_ids"]) == 200
    assert group["truncated"] is True, "a caller acting on 200 of 250 must be told"


def test_only_pending_items_are_grouped(seeded):
    """Reviewed material is not review work."""
    before = sum(group["total"] for group in seeded.group_pending_inbox("alice", by="extension")["groups"])
    seeded.execute("UPDATE inbox SET status='ignored' WHERE id IN (SELECT id FROM inbox LIMIT 3)")
    seeded.conn.commit()

    after = sum(group["total"] for group in seeded.group_pending_inbox("alice", by="extension")["groups"])
    assert after == before - 3


def test_an_unknown_axis_is_refused(seeded):
    with pytest.raises(ValueError, match="Unknown grouping axis"):
        seeded.group_pending_inbox("alice", by="promotion_score")["groups"]


# --- the endpoint and what it may do --------------------------------------


def test_the_endpoint_returns_groups_and_the_available_axes(settings, seeded):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/admin/inbox/groups?user_id=alice&by=directory",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["axis"] == "directory"
    # `quality` добавлена осознанно: на живом импорте это единственный признак,
    # который разделил материал (0.13 нечитаемое / 0.198 дампы / 0.9+ тексты),
    # тогда как совет классификатора стоял `promote` во всех группах.
    assert set(body["axes"]) == {"extension", "directory", "source", "quality"}
    assert body["grouped"] == 8


def test_a_group_can_be_dismissed_but_not_promoted(settings, seeded):
    """The whole point: one decision covers a pile, and it can only ever be a refusal."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(create_app(settings)) as client:
        groups = client.get("/api/admin/inbox/groups?user_id=alice", headers=headers).json()["groups"]
        python_group = next(group for group in groups if group["key"] == ".py")

        promoting = client.post(
            "/api/admin/inbox/bulk",
            json={
                "user_id": "alice",
                "inbox_ids": python_group["inbox_ids"],
                "status": "classified",
            },
            headers=headers,
        )
        assert promoting.status_code == 400, "a group must not become knowledge wholesale"

        dismissing = client.post(
            "/api/admin/inbox/bulk",
            json={
                "user_id": "alice",
                "inbox_ids": python_group["inbox_ids"],
                "status": "ignored",
                "promote": False,
            },
            headers=headers,
        )
        assert dismissing.status_code == 200, dismissing.text
        assert len(dismissing.json()["changed"]) == 5

    assert seeded.list_knowledge_objects("alice") == []
    remaining = {group["key"] for group in seeded.group_pending_inbox("alice", by="extension")["groups"]}
    assert ".py" not in remaining


def test_grouping_survives_a_purge(settings, storage):
    """No table references inbox(id): purge hard-deletes those rows with foreign keys on.

    This is the constraint that kept grouping computed rather than persisted, so it is
    worth an actual purge rather than a promise.
    """
    storage.ensure_user("alice", source="upload")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    result = asyncio.run(
        pipeline.ingest_file("alice", None, b"# note\n\nsome content", filename="a.md", force_review=True)
    )
    from friday.storage.models import InboxStatus

    pipeline.classify_inbox_item(
        "alice", result["inbox_id"], InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice"
    )
    knowledge_id = storage.list_knowledge_objects("alice")[0]["id"]
    storage.soft_delete_knowledge_object(knowledge_id, "alice")

    from friday.purge import purge_knowledge

    purge_knowledge(storage, settings, None, knowledge_id, "alice")

    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []
    assert storage.group_pending_inbox("alice", by="extension")["groups"] == []
