"""Importer organ — cold-start bulk intake, review-gated absolutely.

Covers the dependency-free parsers (ICS with line folding and TZID forms,
Netscape bookmarks), the force_review pipeline contract (imported items land as
pending Inbox suggestions, never canonical KOs), endpoint idempotency on
re-import, format rejection, and capability gating.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jericho.organs import build_registry
from jericho.organs.importer import detect_format, parse_bookmarks, parse_ics
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app

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
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph

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
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}

        first = _upload(client, owner, ICS_SAMPLE, "calendar.ics")
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["kind"] == "calendar"
        assert body["queued_for_review"] == 2
        assert body["already_imported"] == 0

        storage = app.state.storage
        pending = storage.list_inbox(LEGACY_OWNER_USER_ID, limit=50)
        assert len(pending) == 2
        # The review gate held: no knowledge object exists yet.
        assert storage.count_knowledge_objects(LEGACY_OWNER_USER_ID) == 0

        # Re-importing the same file is a no-op, not a duplication.
        again = _upload(client, owner, ICS_SAMPLE, "calendar.ics")
        assert again.status_code == 200
        assert again.json()["queued_for_review"] == 0
        assert again.json()["already_imported"] == 2
        assert len(storage.list_inbox(LEGACY_OWNER_USER_ID, limit=50)) == 2

        # Bookmarks import works through the same endpoint.
        marks = _upload(client, owner, BOOKMARKS_SAMPLE, "bookmarks.html")
        assert marks.status_code == 200
        assert marks.json()["kind"] == "bookmarks"
        assert marks.json()["queued_for_review"] == 2

        # The import itself is audited.
        actions = [row["action"] for row in storage.list_audit_log(None, limit=50)]
        assert "knowledge.import" in actions


def test_import_endpoint_rejects_unknown_format_and_requires_auth(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        bad = _upload(client, owner, "просто текст без структуры", "notes.txt")
        assert bad.status_code == 400
        assert (
            client.post(
                "/api/import",
                files={"file": ("a.ics", b"BEGIN:VCALENDAR", "text/calendar")},
            ).status_code
            == 401
        )


def test_registry_has_all_organs(settings):
    names = {o.name for o in build_registry(settings).organs}
    assert names == {"reminders", "reflection", "profile", "chronicle", "importer", "sentinel"}


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
    from jericho.organs.importer import parse_mbox

    items = parse_mbox(MBOX_SAMPLE)
    assert len(items) == 2
    assert items[0]["subject"] == "Отчёт по Orion"
    assert items[0]["message_id"] == "msg-1@example.com"
    assert "Обсудим проект Orion" in items[0]["body"]
    # The cp1251 body was decoded per its own Content-Type header.
    assert "Смета по кухне готова." in items[1]["body"]


def test_parse_eml_strips_html_to_text():
    from jericho.organs.importer import parse_eml

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
