"""Importer organ — cold-start bulk intake, review-gated absolutely.

Covers the dependency-free parsers (ICS with line folding and TZID forms,
Netscape bookmarks), the force_review pipeline contract (imported items land as
pending Inbox suggestions, never canonical KOs), endpoint idempotency on
re-import, format rejection, and capability gating.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.organs import build_registry
from friday.organs.importer import detect_format, parse_bookmarks, parse_ics
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app

ICS_SAMPLE = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:evt-1@example.com",
        "DTSTART;TZID=Europe/Moscow:20260801T120000",
        "DTEND;TZID=Europe/Moscow:20260801T130000",
        "SUMMARY:Запуск проекта",
        " Orion",  # RFC 5545 folded continuation line
        "LOCATION:Москва\\, офис",
        "DESCRIPTION:Обсудить план\\nи бюджет",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:evt-2@example.com",
        "DTSTART:20261224",
        "SUMMARY:Отпуск",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "SUMMARY:Без даты — пропустить",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)

BOOKMARKS_SAMPLE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
    <DT><A HREF="https://example.com/article" ADD_DATE="1700000000">Статья про Orion</A>
    <DT><A HREF="https://example.com/article">Дубликат той же ссылки</A>
    <DT><A HREF="javascript:void(0)">Мусор</A>
    <DT><A HREF="https://docs.example.com/">Документация</A>
</DL><p>
"""


# --- parsers --------------------------------------------------------------


def test_parse_ics_handles_folding_tzid_and_allday():
    events = parse_ics(ICS_SAMPLE)
    assert len(events) == 2  # the dateless one is skipped
    first = events[0]
    assert first["summary"] == "Запуск проектаOrion"
    assert first["date"] == "2026-08-01"
    assert first["location"] == "Москва, офис"
    assert "и бюджет" in first["description"]
    assert events[1]["date"] == "2026-12-24"


def test_parse_bookmarks_extracts_http_links_only_once():
    items = parse_bookmarks(BOOKMARKS_SAMPLE)
    urls = [i["url"] for i in items]
    assert urls == ["https://example.com/article", "https://docs.example.com/"]
    assert items[0]["title"] == "Статья про Orion"
    assert items[0]["add_date"] == "1700000000"


def test_detect_format():
    assert detect_format(ICS_SAMPLE) == "ics"
    assert detect_format(BOOKMARKS_SAMPLE) == "bookmarks"
    assert detect_format("просто текст") == ""


# --- force_review pipeline contract --------------------------------------


@pytest.mark.asyncio
async def test_force_review_lands_in_inbox_without_ko(settings, storage):
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    result = await pipeline.ingest_text(
        "alice",
        "Событие: Запуск Orion — 2026-08-01. Обсудить план и бюджет проекта подробно.",
        source="import",
        source_ref="ics:test-1",
        force_knowledge=True,
        force_review=True,
    )
    assert result["promoted"] is False
    assert result["queued_for_review"] is True
    assert storage.get_knowledge_by_raw(result["raw_object_id"], "alice") is None
    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox["status"] == "pending"


# --- endpoint -------------------------------------------------------------


def _upload(client, headers, content: str, filename: str):
    return client.post(
        "/api/import",
        files={"file": (filename, content.encode("utf-8"), "application/octet-stream")},
        headers=headers,
    )


def test_import_endpoint_queues_reviews_and_is_idempotent(settings):
    import hashlib
    import hmac
    import json as json_mod
    import re
    from datetime import UTC, datetime

    from friday.storage.models import InboxItem, KnowledgeObject, RawObject, new_id
    from tests.test_api_tokens import _issue

    person = "scopedperson"
    secret = "jrc_scopedperson_text_import_secret"
    canary_content = "CANARY_PRIVATE_RAW_CONTENT_TEXT_IMPORT_001"
    canary_filename = "canary-secret-filename.ics"
    canary_token = "jrc_canary_token_value_do_not_leak"
    canary_actor = "canaryactor"
    canary_ref = "canary:source-ref-secret"
    calendar_evt1 = "Событие: Запуск проектаOrion — 2026-08-01.\nМесто: Москва, офис\nОбсудить план\nи бюджет"
    calendar_evt2 = "Событие: Отпуск — 2026-12-24."
    bookmark_article = "Закладка: Статья про Orion\nhttps://example.com/article"
    bookmark_docs = "Закладка: Документация\nhttps://docs.example.com/"
    expected_items = {
        "ics:evt-1@example.com": {
            "content": calendar_evt1,
            "import_kind": "calendar",
            "filename": "calendar.ics",
            "title": "Событие: Запуск проектаOrion — 2026-08-01",
            "summary": "Событие: Запуск проектаOrion — 2026-08-01. Место: Москва, офис Обсудить план и бюджет",
            "tags": [
                "event",
                "запуск",
                "место",
                "москва",
                "обсудить",
                "офис",
                "план",
                "проектаorion",
                "событие",
            ],
            "knowledge_kind": "event",
            "quality_score": 0.488,
            "importance": 0.22 + 0.488 * 0.28 + 0.12 + 0.08,
            "urls": [],
            "dates": ["2026-08-01"],
            "sentence_count": 4,
            "word_count": 12,
            "notes": "action=promote; category=knowledge; promotion=0.90; quality=0.49; reason=manual promotion",
        },
        "ics:evt-2@example.com": {
            "content": calendar_evt2,
            "import_kind": "calendar",
            "filename": "calendar.ics",
            "title": "Событие: Отпуск — 2026-12-24",
            "summary": "Событие: Отпуск — 2026-12-24.",
            "tags": ["event", "отпуск", "событие"],
            "knowledge_kind": "event",
            "quality_score": 0.368,
            "importance": 0.22 + 0.368 * 0.28 + 0.12 + 0.08,
            "urls": [],
            "dates": ["2026-12-24"],
            "sentence_count": 1,
            "word_count": 4,
            "notes": "action=promote; category=knowledge; promotion=0.90; quality=0.37; reason=manual promotion",
        },
        "bookmark:https://example.com/article": {
            "content": bookmark_article,
            "import_kind": "bookmarks",
            "filename": "bookmarks.html",
            "title": "Закладка: Статья про Orion",
            "summary": "Закладка: Статья про Orion https://example.com/article",
            "tags": ["article", "example.com", "https", "orion", "reference", "закладка", "статья"],
            "knowledge_kind": "reference",
            "quality_score": 0.268,
            "importance": 0.22 + 0.268 * 0.28 + 0.04,
            "urls": ["https://example.com/article"],
            "dates": [],
            "sentence_count": 2,
            "word_count": 5,
            "notes": "action=promote; category=knowledge; promotion=0.90; quality=0.27; reason=manual promotion",
        },
        "bookmark:https://docs.example.com/": {
            "content": bookmark_docs,
            "import_kind": "bookmarks",
            "filename": "bookmarks.html",
            "title": "Закладка: Документация",
            "summary": "Закладка: Документация https://docs.example.com/",
            "tags": ["docs.example.com", "https", "reference", "документация", "закладка"],
            "knowledge_kind": "reference",
            "quality_score": 0.14800000000000002,
            "importance": 0.22 + 0.14800000000000002 * 0.28 + 0.04,
            "urls": ["https://docs.example.com/"],
            "dates": [],
            "sentence_count": 2,
            "word_count": 3,
            "notes": "action=promote; category=knowledge; promotion=0.90; quality=0.15; reason=manual promotion",
        },
    }

    def rows(storage, sql):
        return [dict(row) for row in storage.execute(sql).fetchall()]

    def snapshot(storage):
        return {
            "raw_objects": rows(storage, "SELECT * FROM raw_objects ORDER BY rowid"),
            "inbox": rows(storage, "SELECT * FROM inbox ORDER BY rowid"),
            "knowledge_objects": rows(storage, "SELECT * FROM knowledge_objects ORDER BY rowid"),
            "audit_log": rows(storage, "SELECT * FROM audit_log ORDER BY rowid"),
        }

    def typed_equal(actual, expected):
        assert type(actual) is type(expected), (actual, expected)
        if isinstance(expected, dict):
            assert actual.keys() == expected.keys()
            for key, value in expected.items():
                typed_equal(actual[key], value)
        elif isinstance(expected, list):
            assert len(actual) == len(expected)
            for item, value in zip(actual, expected, strict=True):
                typed_equal(item, value)
        else:
            assert actual == expected

    def encoded(value):
        return json_mod.dumps(value, ensure_ascii=False, sort_keys=True)

    def opaque(value, prefix):
        assert type(value) is str
        assert re.fullmatch(rf"{prefix}_[0-9a-f]{{16}}", value)
        return value

    def timestamp(value, window, *, audit=False):
        assert type(value) is str
        parsed = datetime.fromisoformat(value)
        precision = "microseconds" if audit else "seconds"
        assert value == parsed.astimezone(UTC).isoformat(timespec=precision)
        assert window[0] <= parsed <= window[1]
        return value

    def import_target(key_hex, filename):
        digest = hmac.new(
            bytes.fromhex(key_hex),
            f"target:import\0{filename}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        return f"import:ref:{digest}"

    def closed_after(kind, parsed, queued, already):
        # kind is outside the audit enum; truncated is a bool-key so int 0 is hidden.
        return {
            "kind_chars": len(kind),
            "parsed": parsed,
            "queued_for_review": queued,
            "already_imported": already,
            "conflicts": 0,
            "failed": 0,
            "private_fields_count": 1,
        }

    def seed_canaries(storage):
        storage.ensure_user(
            LEGACY_OWNER_USER_ID, source="api-token", display_name="Owner", preset_key="owner"
        )
        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=LEGACY_OWNER_USER_ID,
                source="api",
                source_ref=canary_ref,
                raw_content=canary_content,
                content_type="text",
                content_hash=hashlib.sha256(canary_content.encode("utf-8")).hexdigest(),
                metadata_json={"uploaded_by": canary_actor, "filename": canary_filename},
            )
        )
        ko = storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
                content=canary_content,
                title="canary-ko",
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
                knowledge_object_id=ko.id,
                status="pending",
            )
        )

    def assert_import_item(raw_row, item, source_ref, window):
        expected = expected_items[source_ref]
        assessment = {
            "category": "knowledge",
            "confidence": 1.0,
            "action": "promote",
            "promotion_score": 0.9,
            "quality_score": expected["quality_score"],
            "knowledge_kind": expected["knowledge_kind"],
            "reason": "manual promotion",
            "signals": ["manual_promotion"],
            "penalties": [],
            "policy_version": "moderate-v6",
        }
        typed_equal(
            raw_row,
            {
                "id": opaque(raw_row["id"], "raw"),
                "user_id": LEGACY_OWNER_USER_ID,
                "source": "import",
                "source_ref": source_ref,
                "raw_content": expected["content"],
                "content_type": "text",
                "metadata_json": encoded(
                    {
                        "import_kind": expected["import_kind"],
                        "filename": expected["filename"],
                        "uploaded_by": person,
                        "promotion_assessment": assessment,
                        "classification": "knowledge",
                        "classification_confidence": 1.0,
                        "classification_reason": "manual promotion",
                    }
                ),
                "content_hash": hashlib.sha256(expected["content"].encode("utf-8")).hexdigest(),
                "version": 1,
                "received_at": timestamp(raw_row["received_at"], window),
                "created_at": timestamp(raw_row["created_at"], window),
                "deleted_at": None,
            },
        )
        suggestions = {
            "title": expected["title"],
            "summary": expected["summary"],
            "tags": expected["tags"],
            "importance": expected["importance"],
            "quality_score": expected["quality_score"],
            "knowledge_kind": expected["knowledge_kind"],
            "entities": [],
            "metadata": {
                "enrichment_version": "moderate-v6",
                "knowledge_kind": expected["knowledge_kind"],
                "urls": expected["urls"],
                "dates": expected["dates"],
                "action_items": [],
                "entity_suggestion_count": 0,
                "structure": {
                    "has_list": False,
                    "has_code": False,
                    "sentence_count": expected["sentence_count"],
                    "word_count": expected["word_count"],
                },
                "promotion_assessment": assessment,
            },
        }
        typed_equal(
            item,
            {
                "id": opaque(item["id"], "inbox"),
                "user_id": LEGACY_OWNER_USER_ID,
                "raw_object_id": raw_row["id"],
                "knowledge_object_id": None,
                "status": "pending",
                "suggested_entity_id": None,
                "suggested_tags_json": encoded(expected["tags"]),
                "suggestions_json": encoded(suggestions),
                "suggested_action": "promote",
                "promotion_score": 0.9,
                "quality_score": expected["quality_score"],
                "classification_notes": expected["notes"],
                "created_at": timestamp(item["created_at"], window),
                "reviewed_at": None,
                "reviewed_by": None,
            },
        )
        assert raw_row["received_at"] <= raw_row["created_at"] <= item["created_at"]

    def imported_by_ref(raw_rows, prefix_len, count):
        imported = raw_rows[prefix_len:]
        assert len(imported) == count
        assert len({row["source_ref"] for row in imported}) == count
        assert len({row["id"] for row in imported}) == count
        return {row["source_ref"]: row for row in imported}

    def assert_import_audit(row, response, target, payload, window):
        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{24}", request_id)
        typed_equal(
            row,
            {
                "id": opaque(row["id"], "audit"),
                "user_id": person,
                "action": "knowledge.import",
                "target_type": "import",
                "target_id": target,
                "before_json": None,
                "after_json": encoded(payload),
                "ip_address": "",
                "request_id": request_id,
                "created_at": timestamp(row["created_at"], window, audit=True),
            },
        )

    app = create_app(replace(settings, shared_archive=True))
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, person, "user", secret)
        headers = {"Authorization": f"Bearer {secret}"}
        seed_canaries(storage)
        before = snapshot(storage)
        key_hex = storage.execute(
            "SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'"
        ).fetchone()[0]
        calendar_target = import_target(key_hex, "calendar.ics")
        bookmark_target = import_target(key_hex, "bookmarks.html")
        assert calendar_target != bookmark_target

        me = client.get("/api/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["actor"]["user_id"] == person

        first_started = datetime.now(UTC).replace(microsecond=0)
        first = _upload(client, headers, ICS_SAMPLE, "calendar.ics")
        first_window = (first_started, datetime.now(UTC))
        assert first.status_code == 200, first.text
        first_body = first.json()
        typed_equal(
            first_body,
            {
                "kind": "calendar",
                "parsed": 2,
                "queued_for_review": 2,
                "already_imported": 0,
                "conflicts": 0,
                "failed": 0,
                "truncated": 0,
            },
        )
        after_first = snapshot(storage)
        by_ref = imported_by_ref(after_first["raw_objects"], len(before["raw_objects"]), 2)
        assert set(by_ref) == {"ics:evt-1@example.com", "ics:evt-2@example.com"}
        new_inbox = after_first["inbox"][len(before["inbox"]) :]
        assert len(new_inbox) == 2
        assert len({row["raw_object_id"] for row in new_inbox}) == 2
        inbox_by_raw = {row["raw_object_id"]: row for row in new_inbox}
        assert set(inbox_by_raw) == {row["id"] for row in by_ref.values()}
        for source_ref, raw_row in by_ref.items():
            item = inbox_by_raw[raw_row["id"]]
            assert_import_item(raw_row, item, source_ref, first_window)
        assert after_first["knowledge_objects"] == before["knowledge_objects"]
        assert after_first["raw_objects"][: len(before["raw_objects"])] == before["raw_objects"]
        assert after_first["inbox"][: len(before["inbox"])] == before["inbox"]
        first_audit = after_first["audit_log"][len(before["audit_log"]) :]
        assert len(first_audit) == 1
        assert_import_audit(
            first_audit[0],
            first,
            calendar_target,
            closed_after("calendar", 2, 2, 0),
            first_window,
        )
        assert after_first["audit_log"][: len(before["audit_log"])] == before["audit_log"]

        replay_started = datetime.now(UTC).replace(microsecond=0)
        again = _upload(client, headers, ICS_SAMPLE, "calendar.ics")
        replay_window = (replay_started, datetime.now(UTC))
        assert again.status_code == 200, again.text
        typed_equal(
            again.json(),
            {
                "kind": "calendar",
                "parsed": 2,
                "queued_for_review": 0,
                "already_imported": 2,
                "conflicts": 0,
                "failed": 0,
                "truncated": 0,
            },
        )
        after_replay = snapshot(storage)
        assert after_replay["raw_objects"] == after_first["raw_objects"]
        assert after_replay["inbox"] == after_first["inbox"]
        assert after_replay["knowledge_objects"] == after_first["knowledge_objects"]
        replay_audit = after_replay["audit_log"][len(after_first["audit_log"]) :]
        assert len(replay_audit) == 1
        assert_import_audit(
            replay_audit[0],
            again,
            calendar_target,
            closed_after("calendar", 2, 0, 2),
            replay_window,
        )
        assert after_replay["audit_log"][: len(after_first["audit_log"])] == after_first["audit_log"]

        marks_started = datetime.now(UTC).replace(microsecond=0)
        marks = _upload(client, headers, BOOKMARKS_SAMPLE, "bookmarks.html")
        marks_window = (marks_started, datetime.now(UTC))
        assert marks.status_code == 200, marks.text
        typed_equal(
            marks.json(),
            {
                "kind": "bookmarks",
                "parsed": 2,
                "queued_for_review": 2,
                "already_imported": 0,
                "conflicts": 0,
                "failed": 0,
                "truncated": 0,
            },
        )
        after_marks = snapshot(storage)
        by_ref = imported_by_ref(after_marks["raw_objects"], len(before["raw_objects"]), 4)
        assert set(by_ref) == {
            "ics:evt-1@example.com",
            "ics:evt-2@example.com",
            "bookmark:https://example.com/article",
            "bookmark:https://docs.example.com/",
        }
        assert after_marks["raw_objects"][: len(after_first["raw_objects"])] == after_first["raw_objects"]
        assert after_marks["inbox"][: len(after_first["inbox"])] == after_first["inbox"]
        assert after_marks["knowledge_objects"] == before["knowledge_objects"]
        imported_inbox = after_marks["inbox"][len(before["inbox"]) :]
        assert len(imported_inbox) == 4
        assert len({row["raw_object_id"] for row in imported_inbox}) == 4
        inbox_by_raw = {row["raw_object_id"]: row for row in imported_inbox}
        assert set(inbox_by_raw) == {row["id"] for row in by_ref.values()}
        for source_ref, raw_row in by_ref.items():
            item = inbox_by_raw[raw_row["id"]]
            window = first_window if expected_items[source_ref]["import_kind"] == "calendar" else marks_window
            assert_import_item(raw_row, item, source_ref, window)
        mark_audit = after_marks["audit_log"][len(after_replay["audit_log"]) :]
        assert len(mark_audit) == 1
        assert_import_audit(
            mark_audit[0],
            marks,
            bookmark_target,
            closed_after("bookmarks", 2, 2, 0),
            marks_window,
        )
        assert after_marks["audit_log"][: len(after_replay["audit_log"])] == after_replay["audit_log"]
        assert len(after_marks["audit_log"]) - len(before["audit_log"]) == 3
        assert len({row["id"] for row in (first_audit[0], replay_audit[0], mark_audit[0])}) == 3
        assert (
            len({first.headers["x-request-id"], again.headers["x-request-id"], marks.headers["x-request-id"]})
            == 3
        )
        leaked = (
            json_mod.dumps(first_body, ensure_ascii=False)
            + first.text
            + again.text
            + marks.text
            + json_mod.dumps(
                after_marks["audit_log"][len(before["audit_log"]) :], ensure_ascii=False, default=str
            )
        )
        for marker in (canary_content, canary_filename, canary_token, canary_actor, canary_ref, secret):
            assert marker not in leaked


def test_import_endpoint_rejects_unknown_format_and_requires_auth(settings):
    import hashlib
    import json as json_mod

    from friday.storage.models import InboxItem, KnowledgeObject, RawObject, new_id
    from tests.test_api_tokens import _issue

    person = "scopedperson"
    secret = "jrc_scopedperson_text_import_secret"
    guest = "scopedguest"
    guest_secret = "jrc_scopedguest_default_deny_secret"
    canary_content = "CANARY_PRIVATE_RAW_CONTENT_TEXT_IMPORT_001"
    canary_filename = "canary-secret-filename.ics"
    canary_token = "jrc_canary_token_value_do_not_leak"
    canary_actor = "canaryactor"
    canary_ref = "canary:source-ref-secret"

    def rows(storage, sql):
        return [dict(row) for row in storage.execute(sql).fetchall()]

    def snapshot(storage):
        return {
            "raw_objects": rows(storage, "SELECT * FROM raw_objects ORDER BY rowid"),
            "inbox": rows(storage, "SELECT * FROM inbox ORDER BY rowid"),
            "knowledge_objects": rows(storage, "SELECT * FROM knowledge_objects ORDER BY rowid"),
            "audit_log": rows(storage, "SELECT * FROM audit_log ORDER BY rowid"),
        }

    def as_json(value):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json_mod.loads(value)

    def seed_canaries(storage):
        storage.ensure_user(
            LEGACY_OWNER_USER_ID, source="api-token", display_name="Owner", preset_key="owner"
        )
        raw = storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=LEGACY_OWNER_USER_ID,
                source="api",
                source_ref=canary_ref,
                raw_content=canary_content,
                content_type="text",
                content_hash=hashlib.sha256(canary_content.encode("utf-8")).hexdigest(),
                metadata_json={"uploaded_by": canary_actor, "filename": canary_filename},
            )
        )
        ko = storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
                content=canary_content,
                title="canary-ko",
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=LEGACY_OWNER_USER_ID,
                raw_object_id=raw.id,
                knowledge_object_id=ko.id,
                status="pending",
            )
        )

    def leak_blob(response, extra_rows):
        return (
            response.text
            + json_mod.dumps(response.json(), ensure_ascii=False, default=str)
            + json_mod.dumps(extra_rows, ensure_ascii=False, default=str)
        )

    def assert_no_canaries(blob):
        for marker in (
            canary_content,
            canary_filename,
            canary_token,
            canary_actor,
            canary_ref,
            secret,
            guest_secret,
        ):
            assert marker not in blob

    app = create_app(replace(settings, shared_archive=True))
    with TestClient(app) as client:
        storage = app.state.storage
        _issue(storage, person, "user", secret)
        _issue(storage, guest, "guest", guest_secret)
        capable = {"Authorization": f"Bearer {secret}"}
        guest_headers = {"Authorization": f"Bearer {guest_secret}"}
        seed_canaries(storage)
        before = snapshot(storage)

        bad = _upload(client, capable, "просто текст без структуры", "notes.txt")
        assert bad.status_code == 400
        assert bad.json() == {
            "detail": (
                "Unsupported file: expected an ICS calendar, a bookmarks HTML export, "
                "an mbox mail archive, or a single .eml message"
            )
        }
        after_bad = snapshot(storage)
        assert after_bad["raw_objects"] == before["raw_objects"]
        assert after_bad["inbox"] == before["inbox"]
        assert after_bad["knowledge_objects"] == before["knowledge_objects"]
        assert after_bad["audit_log"] == before["audit_log"]
        assert_no_canaries(leak_blob(bad, []))

        denied = _upload(client, guest_headers, ICS_SAMPLE, "calendar.ics")
        assert denied.status_code == 403
        assert denied.json() == {"detail": "Access denied for import.run (default_deny)"}
        after_denied = snapshot(storage)
        assert after_denied["raw_objects"] == before["raw_objects"]
        assert after_denied["inbox"] == before["inbox"]
        assert after_denied["knowledge_objects"] == before["knowledge_objects"]
        assert after_denied["audit_log"] == before["audit_log"]
        assert_no_canaries(leak_blob(denied, []))

        anonymous = client.post(
            "/api/import",
            files={"file": ("a.ics", b"BEGIN:VCALENDAR", "text/calendar")},
        )
        assert anonymous.status_code == 401
        assert anonymous.json() == {"detail": "Missing authentication"}
        after_anon = snapshot(storage)
        assert after_anon["raw_objects"] == before["raw_objects"]
        assert after_anon["inbox"] == before["inbox"]
        assert after_anon["knowledge_objects"] == before["knowledge_objects"]
        assert after_anon["audit_log"][: len(before["audit_log"])] == before["audit_log"]
        new_audit = after_anon["audit_log"][len(before["audit_log"]) :]
        assert len(new_audit) == 1
        row = new_audit[0]
        assert row["user_id"] == "anonymous"
        assert row["action"] == "auth.failed"
        assert row["target_type"] == "auth"
        assert row["target_id"] == "invalid_credentials"
        assert as_json(row["before_json"]) is None
        assert as_json(row["after_json"]) == {
            "method_chars": 4,
            "path_chars": 11,
            "reason": "invalid_credentials",
            "status_present": True,
        }
        assert_no_canaries(leak_blob(anonymous, new_audit))


def test_registry_has_all_organs(settings):
    names = {o.name for o in build_registry(settings).organs}
    assert names == {
        "reminders",
        "reflection",
        "profile",
        "chronicle",
        "importer",
        "monitors",
        "sentinel",
        # Ночная сводка о поведении системы за сутки. Заказ владельца
        # 2026-08-04; обезличивание делает структура, модель корпуса не видит.
        "compactor",
    }
    enabled_names = {
        organ.name for organ in build_registry(replace(settings, engineer_mode_enabled=True)).organs
    }
    assert enabled_names == {*names, "engineer"}


# --- mail (mbox / eml) ----------------------------------------------------

MBOX_SAMPLE = (
    (
        "From alice@example.com Thu Jul 23 10:00:00 2026\n"
        "From: Alice <alice@example.com>\n"
        "Subject: =?utf-8?b?0J7RgtGH0ZHRgiDQv9C+IE9yaW9u?=\n"
        "Date: Thu, 23 Jul 2026 10:00:00 +0300\n"
        "Message-ID: <msg-1@example.com>\n"
        "Content-Type: text/plain; charset=utf-8\n"
        "Content-Transfer-Encoding: 8bit\n"
        "\n"
        "Привет! Обсудим проект Orion в понедельник.\n"
        "\n"
    ).encode()
    + b"From bob@example.com Thu Jul 23 11:00:00 2026\n"
    + b"From: Bob <bob@example.com>\n"
    + b"Subject: Smeta\n"
    + b"Message-ID: <msg-2@example.com>\n"
    + b"Content-Type: text/plain; charset=windows-1251\n"
    + b"Content-Transfer-Encoding: 8bit\n"
    + b"\n"
    + "Смета по кухне готова.".encode("cp1251")
    + b"\n\n"
)

EML_HTML_SAMPLE = (
    b"From: Carol <carol@example.com>\n"
    b"Subject: Plany\n"
    b"Message-ID: <msg-3@example.com>\n"
    b"Content-Type: text/html; charset=utf-8\n"
    b"\n" + "<html><body><p>План на <b>август</b>: запуск.</p></body></html>".encode()
)


def test_parse_mbox_decodes_rfc2047_and_declared_charsets():
    from friday.organs.importer import parse_mbox

    items = parse_mbox(MBOX_SAMPLE)
    assert len(items) == 2
    assert items[0]["subject"] == "Отчёт по Orion"
    assert items[0]["message_id"] == "msg-1@example.com"
    assert "Обсудим проект Orion" in items[0]["body"]
    # The cp1251 body was decoded per its own Content-Type header.
    assert "Смета по кухне готова." in items[1]["body"]


def test_parse_eml_strips_html_to_text():
    from friday.organs.importer import parse_eml

    items = parse_eml(EML_HTML_SAMPLE)
    assert len(items) == 1
    assert items[0]["subject"] == "Plany"
    assert "План на август" in items[0]["body"]
    assert "<b>" not in items[0]["body"]


def test_detect_format_mail_variants():
    assert detect_format(MBOX_SAMPLE.decode("utf-8", errors="replace")) == "mbox"
    assert detect_format(EML_HTML_SAMPLE.decode("utf-8")) == "eml"
    # An HTML email with anchors is NOT mistaken for a bookmarks export.
    with_link = EML_HTML_SAMPLE.decode("utf-8").replace("запуск.", '<a href="https://x.example">x</a>')
    assert detect_format(with_link) == "eml"
    # Extension is the fallback hint when content heuristics are inconclusive.
    assert detect_format("непонятное содержимое", "backup.mbox") == "mbox"


def test_import_endpoint_accepts_mbox_and_eml(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        first = client.post(
            "/api/import",
            files={"file": ("mail.mbox", MBOX_SAMPLE, "application/mbox")},
            headers=owner,
        )
        assert first.status_code == 200, first.text
        assert first.json()["kind"] == "email"
        assert first.json()["queued_for_review"] == 2

        storage = app.state.storage
        assert len(storage.list_inbox(LEGACY_OWNER_USER_ID, limit=50)) == 2
        assert storage.count_knowledge_objects(LEGACY_OWNER_USER_ID) == 0

        # Re-import: Message-ID-stable refs make it a no-op.
        again = client.post(
            "/api/import",
            files={"file": ("mail.mbox", MBOX_SAMPLE, "application/mbox")},
            headers=owner,
        )
        assert again.json()["already_imported"] == 2
        assert again.json()["queued_for_review"] == 0

        single = client.post(
            "/api/import",
            files={"file": ("letter.eml", EML_HTML_SAMPLE, "message/rfc822")},
            headers=owner,
        )
        assert single.status_code == 200
        assert single.json()["kind"] == "email"
        assert single.json()["queued_for_review"] == 1
