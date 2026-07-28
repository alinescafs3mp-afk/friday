"""«Что и когда этот человек писал и загружал» — one account, in order.

Every other read in the storage layer answers "what does this account hold". This
one answers "what did this person do", which is a different shape: the spine is
`raw_objects`, because that is the row every arrival creates and it carries
`received_at` — when the person actually did it, not when a worker got round to
enriching it. What makes a row legible comes from elsewhere: the Knowledge Object
that came out of it, the Inbox item still waiting, the filename in the metadata.

Tenant isolation is the thing these tests care about most. An oversight query that
quietly includes somebody else's rows is worse than no oversight at all.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _arrival(storage, user_id: str, *, source: str, content: str, at: str, filename: str = "") -> str:
    metadata = {"filename": filename, "size_bytes": len(content)} if filename else {}
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source=source,
        source_ref=new_id("src"),
        raw_content=content,
        content_type="file" if filename else "text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (at, raw.id))
    storage.commit()
    return raw.id


@pytest.fixture
def populated(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    _arrival(
        storage, "alice", source="telegram", content="Записал мысль про склад", at="2026-07-01T09:00:00+00:00"
    )
    _arrival(
        storage,
        "alice",
        source="upload",
        content="Смета на ремонт",
        at="2026-07-05T12:00:00+00:00",
        filename="смета.pdf",
    )
    _arrival(
        storage,
        "bob",
        source="upload",
        content="Чужой файл",
        at="2026-07-05T12:30:00+00:00",
        filename="bob.pdf",
    )
    return storage


def test_the_timeline_is_one_accounts_own_arrivals(populated):
    rows = populated.user_activity("alice")
    assert len(rows) == 2, "the timeline did not return exactly this account's arrivals"
    assert all("bob" not in json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    assert [row["at"] for row in rows] == sorted((row["at"] for row in rows), reverse=True)


def test_writing_and_uploading_are_told_apart(populated):
    kinds = {row["activity"]: row for row in populated.user_activity("alice")}
    assert set(kinds) == {"wrote", "upload"}
    assert kinds["upload"]["filename"] == "смета.pdf"
    assert kinds["upload"]["size_bytes"] == len("Смета на ремонт")


def test_a_window_selects_by_when_it_happened(populated):
    only_july_first = populated.user_activity("alice", until="2026-07-02T00:00:00+00:00")
    assert len(only_july_first) == 1
    assert only_july_first[0]["activity"] == "wrote"

    from_july_second = populated.user_activity("alice", since="2026-07-02T00:00:00+00:00")
    assert len(from_july_second) == 1
    assert from_july_second[0]["activity"] == "upload"


def test_the_summary_counts_only_this_account(populated):
    summary = populated.user_activity_summary("alice")
    assert summary["arrivals"] == 2
    assert summary["first_at"] == "2026-07-01T09:00:00+00:00"
    assert summary["last_at"] == "2026-07-05T12:00:00+00:00"
    assert {item["source"] for item in summary["by_source"]} == {"telegram", "upload"}
    assert {item["day"] for item in summary["by_day"]} == {"2026-07-01", "2026-07-05"}

    other = populated.user_activity_summary("bob")
    assert other["arrivals"] == 1


def test_the_summary_carries_no_content(populated):
    blob = json.dumps(populated.user_activity_summary("alice"), ensure_ascii=False, default=str)
    assert "склад" not in blob and "Смета" not in blob


def test_a_promoted_object_is_shown_beside_its_arrival(storage):
    storage.ensure_user("alice")
    raw_id = _arrival(
        storage,
        "alice",
        source="telegram",
        content="Договор аренды до декабря",
        at="2026-07-10T10:00:00+00:00",
    )
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw_id,
        content="Договор аренды до декабря",
        content_type="text",
        title="Договор аренды",
    )
    storage.store_knowledge_object(ko)

    row = storage.user_activity("alice")[0]
    assert row["knowledge_object_id"] == ko.id
    assert row["title"] == "Договор аренды"


def test_an_empty_account_is_empty_rather_than_everyone(storage):
    storage.ensure_user("alice")
    storage.ensure_user("newcomer")
    _arrival(storage, "alice", source="telegram", content="что-то", at="2026-07-01T09:00:00+00:00")
    assert storage.user_activity("newcomer") == []
    assert storage.user_activity_summary("newcomer")["arrivals"] == 0


def test_a_preview_is_bounded(storage):
    from jericho.storage._oversight import _PREVIEW_CHARS

    storage.ensure_user("alice")
    _arrival(
        storage,
        "alice",
        source="upload",
        content="я" * 50_000,
        at="2026-07-01T09:00:00+00:00",
        filename="big.txt",
    )
    row = storage.user_activity("alice")[0]
    assert row["content_chars"] == 50_000
    assert len(row["preview"]) == _PREVIEW_CHARS, "one large document would size the whole response"
