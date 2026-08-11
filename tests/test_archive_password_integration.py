"""Archive passwords are ephemeral credentials, never durable chat content."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import pyzipper

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.telegram_bridge import TelegramBridge, TelegramConfig

_PASSWORD = "only-in-this-request"


def _encrypted_zip() -> bytes:
    payload = io.BytesIO()
    with pyzipper.AESZipFile(
        payload,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(_PASSWORD.encode())
        archive.writestr("note.txt", "ARCHIVE-INTEGRATION-MARKER")
    return payload.getvalue()


def _metadata_odt() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office><text>ODF-BODY-MARKER</text></office>")
        archive.writestr(
            "meta.xml",
            """<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <office:meta><dc:title>ODF-METADATA-TITLE</dc:title>
 <meta:creation-date>2023-06-07T08:09:10Z</meta:creation-date>
 <meta:document-statistic meta:page-count="4" meta:word-count="99"/>
 </office:meta></office:document-meta>""",
        )
    return payload.getvalue()


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _archive_update(update_id: int, *, caption: str = "") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": 5001},
            "from": {"id": 1001},
            "caption": caption,
            "document": {
                "file_id": "telegram-stable-archive-id",
                "file_unique_id": "telegram-unique-archive-id",
                "file_name": "protected.zip",
                "mime_type": "application/zip",
                "file_size": 123,
            },
        },
    }


@pytest.mark.asyncio
async def test_ingestion_password_challenge_creates_no_raw_or_file(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    payload = _encrypted_zip()

    missing = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="protected.zip",
        source_ref="archive-password-test",
    )
    wrong = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="protected.zip",
        source_ref="archive-password-test",
        archive_password="wrong-password",
    )

    assert missing["archive_password_required"] is True
    assert wrong["archive_password_invalid"] is True
    assert missing["persisted"] is False and wrong["persisted"] is False
    assert storage.execute("SELECT COUNT(*) AS count FROM raw_objects").fetchone()["count"] == 0
    assert not any(settings.files_dir.rglob("*"))


@pytest.mark.asyncio
async def test_odf_metadata_is_persisted_and_has_header_only_transient_path(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    payload = _metadata_odt()

    header = await pipeline.inspect_file_transient(
        payload,
        filename="legacy.odt",
        metadata_only=True,
    )
    result = await pipeline.ingest_file("alice", None, payload, filename="current.odt")
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    stored = json.loads(raw["metadata_json"])

    assert header["_document_metadata"]["title"] == "ODF-METADATA-TITLE"
    assert header["_document_metadata"]["page_count"] == 4
    assert "text_preview" not in header and "_runtime_source_text" not in header
    assert stored["title"] == "ODF-METADATA-TITLE"
    assert stored["document_date"] == "2023-06-07"


def test_bridge_strips_same_caption_password_before_sqlite(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    try:
        raw = _archive_update(4101, caption=f"Проверь архив, пароль: {_PASSWORD}")
        safe = bridge._sanitize_update_before_store(raw)  # noqa: SLF001
        assert bridge._inbox.store(safe) is True  # noqa: SLF001

        stored = bridge._inbox._conn.execute(  # noqa: SLF001
            "SELECT payload_json FROM updates WHERE update_id=4101"
        ).fetchone()["payload_json"]
        assert _PASSWORD not in stored
        assert "Проверь архив" in stored
        assert bridge._archive_passwords[4101] == _PASSWORD  # noqa: SLF001
        # Intake has not yet proved that the archive is encrypted, so there is
        # no durable pending challenge to hijack the next ordinary message.
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_plain_archive_then_ordinary_text_is_not_misclassified(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    try:
        bridge._sanitize_update_before_store(_archive_update(4201))  # noqa: SLF001
        ordinary = {
            "update_id": 4202,
            "message": {
                "message_id": 4202,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": "Как дела?",
            },
        }
        safe = bridge._sanitize_update_before_store(ordinary)  # noqa: SLF001

        assert safe["message"]["text"] == "Как дела?"
        assert "friday_archive_password_followup" not in safe
        assert 4202 not in bridge._archive_passwords  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001


def test_password_challenge_has_no_generic_extraction_failure_companion(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    try:
        response = {
            "message": "Архив защищён паролем. Пришлите пароль следующим сообщением.",
            "message_format": "plain",
            "archive_password_required": True,
            "file_ingestion": {
                "archive_password_required": True,
                "extraction_success": False,
                "extraction": {"success": False},
            },
        }
        rendered = bridge._format_response_message(response)  # noqa: SLF001
        assert rendered == response["message"]
        assert "извлечь не удалось" not in rendered.casefold()
    finally:
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_bridge_followup_redownloads_without_ever_queuing_password(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    backend_payloads: list[dict] = []
    backend_responses = [
        {
            "message": "Нужен пароль",
            "archive_password_required": True,
            "file_ingestion": {"archive_password_required": True, "persisted": False},
        },
        {"message": "Архив прочитан"},
    ]

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        descriptor = message["document"]
        return {
            "filename": descriptor["file_name"],
            "mime_type": descriptor["mime_type"],
            "content_base64": "UEs=",
            "source_ref": f"telegram-file:{descriptor['file_id']}",
            "media_kind": "document",
        }

    async def backend(_client, method, path, payload, _user, _chat):  # noqa: ANN001
        assert method == "POST" and path == "/api/chat"
        backend_payloads.append(dict(payload))
        return backend_responses.pop(0)

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)

    try:
        first = _archive_update(4301, caption="Посчитай записи")
        await bridge._process_update(object(), object(), first, cached_response=None)  # noqa: SLF001
        assert bridge._inbox.archive_password_challenge(5001, 1001) is not None  # noqa: SLF001

        followup = {
            "update_id": 4302,
            "message": {
                "message_id": 4302,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": _PASSWORD,
            },
        }
        safe_followup = bridge._sanitize_update_before_store(followup)  # noqa: SLF001
        assert bridge._inbox.store(safe_followup) is True  # noqa: SLF001
        await bridge._process_update(  # noqa: SLF001
            object(),
            object(),
            safe_followup,
            cached_response=None,
        )

        assert backend_payloads[0].get("archive_password") is None
        assert backend_payloads[1]["archive_password"] == _PASSWORD
        assert backend_payloads[1]["message"] == "Посчитай записи"
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
        database_dump = "\n".join(bridge._inbox._conn.iterdump())  # noqa: SLF001
        assert _PASSWORD not in database_dump
    finally:
        bridge._archive_passwords.clear()  # noqa: SLF001
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_password_never_enters_failure_row_or_log(tmp_path, monkeypatch, caplog) -> None:
    bridge = _bridge(tmp_path)
    safe = bridge._sanitize_update_before_store(  # noqa: SLF001
        _archive_update(4401, caption=f"Проверь, пароль: {_PASSWORD}")
    )

    async def explode(*_args, **_kwargs):
        raise RuntimeError(_PASSWORD)

    try:
        assert bridge._inbox.store(safe) is True  # noqa: SLF001
        row = bridge._inbox.pending()[0]  # noqa: SLF001
        monkeypatch.setattr(bridge, "_process_update", explode)
        await bridge._run_update(object(), object(), row)  # noqa: SLF001

        database_dump = "\n".join(bridge._inbox._conn.iterdump())  # noqa: SLF001
        assert _PASSWORD not in database_dump
        assert _PASSWORD not in caplog.text
        assert "RuntimeError" in database_dump
    finally:
        bridge._inbox.close()  # noqa: SLF001
