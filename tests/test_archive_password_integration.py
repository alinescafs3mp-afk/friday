"""Archive passwords are ephemeral credentials, never durable chat content."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import pyzipper

from friday.agent_runtime import AgentRuntime
from friday.archive_formats import archive_dispatch_kind
from friday.documents import DocumentResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext
from friday.telegram_bridge import TelegramBridge, TelegramConfig

_PASSWORD = "only-in-this-request"


class _NoArchiveQuicklookLLM:
    enabled = True
    model = "archive-quicklook-closed-route"

    async def chat(self, messages, **kwargs):  # pragma: no cover - receipt owns this turn
        del messages, kwargs
        raise AssertionError("archive bare-upload quicklook called the model")


def _encrypted_zip() -> bytes:
    payload = io.BytesIO()
    with pyzipper.AESZipFile(
        payload,
        mode="w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as archive:
        archive.setpassword(_PASSWORD.encode())
        archive.writestr("nested/note.txt", "ARCHIVE-INTEGRATION-MARKER")
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
 <meta:print-date>2023-06-08T09:10:11Z</meta:print-date>
 <meta:document-statistic meta:page-count="4" meta:word-count="99"
     meta:paragraph-count="12" meta:non-whitespace-character-count="500"/>
 <meta:user-defined meta:name="Подразделение" meta:value-type="string">Отдел 7</meta:user-defined>
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
async def test_mime_only_archive_challenges_before_dedup_and_persists_exact_bytes(
    settings,
    storage,
    monkeypatch,
) -> None:
    assert archive_dispatch_kind("protected.bin", "application/zip") == ".zip"
    assert archive_dispatch_kind("report.docx", "application/zip") is None
    assert archive_dispatch_kind("document.odt", "application/zip") is None
    assert archive_dispatch_kind("book.epub", "application/zip") is None
    assert archive_dispatch_kind("bundle.7z", "application/zip") == ".7z"

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    payload = _encrypted_zip()
    dedup_calls = 0
    enrichment_calls = 0
    original_find = storage.find_file_by_content_hash
    original_enrich = pipeline._enrich  # noqa: SLF001

    def tracked_find(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal dedup_calls
        dedup_calls += 1
        return original_find(*args, **kwargs)

    def tracked_enrich(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal enrichment_calls
        enrichment_calls += 1
        return original_enrich(*args, **kwargs)

    monkeypatch.setattr(storage, "find_file_by_content_hash", tracked_find)
    monkeypatch.setattr(pipeline, "_enrich", tracked_enrich)

    missing = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="protected.bin",
        mime_type="application/zip",
        metadata={"uploaded_by": "alice"},
        source_ref="archive-password-test",
    )
    wrong = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="protected.bin",
        mime_type="application/zip",
        metadata={"uploaded_by": "alice"},
        source_ref="archive-password-test",
        archive_password="wrong-password",
    )

    assert missing["archive_password_required"] is True
    assert wrong["archive_password_invalid"] is True
    assert missing["persisted"] is False and wrong["persisted"] is False
    assert dedup_calls == 0 and enrichment_calls == 0
    assert storage.execute("SELECT COUNT(*) AS count FROM raw_objects").fetchone()["count"] == 0
    assert not any(settings.files_dir.rglob("*"))

    unlocked = await pipeline.ingest_file(
        "alice",
        None,
        payload,
        filename="protected.bin",
        mime_type="application/zip",
        metadata={"uploaded_by": "alice"},
        source_ref="archive-password-test",
        archive_password=_PASSWORD,
    )

    raw = storage.get_raw_object(unlocked["raw_object_id"], "alice")
    raw_metadata = json.loads(raw["metadata_json"])
    assert unlocked["extraction"]["success"] is True
    assert "ARCHIVE-INTEGRATION-MARKER" in raw["raw_content"]
    assert raw_metadata["filename"] == "protected.bin"
    assert raw_metadata["mime_type"] == "application/zip"
    assert raw_metadata["sha256"] == raw["content_hash"]
    assert raw_metadata["size_bytes"] == len(payload)
    assert dedup_calls == 1 and enrichment_calls == 1
    assert storage.execute("SELECT COUNT(*) AS count FROM raw_objects").fetchone()["count"] == 1
    assert Path(unlocked["stored_path"]).read_bytes() == payload

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NoArchiveQuicklookLLM(),
    )

    review_calls: list[list[dict]] = []

    async def full_review(context, message, attachments):  # noqa: ANN001
        del context, message
        snapshot = [dict(item) for item in attachments or []]
        review_calls.append(snapshot)
        body = "\n".join(str(item.get("transient_text") or "") for item in snapshot)
        assert "ARCHIVE-INTEGRATION-MARKER" in body
        return {
            "content": "**Подробное ревью**\n\nАрхив содержит ARCHIVE-INTEGRATION-MARKER.",
            "tools_used": [],
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_generate_response", full_review)
    receipt = await runtime.chat(
        "alice",
        "Загружен документ: protected.bin",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": str(unlocked["raw_object_id"])}],
        synthetic_document_notice=True,
        enable_tools=False,
    )
    assert len(review_calls) == 1
    assert "ARCHIVE-INTEGRATION-MARKER" in receipt["message"]
    assert receipt["message_format"] == "markdown"
    assert receipt["attachment_context_expected_count"] == 1
    assert receipt["attachment_context_readable_count"] == 1
    assert receipt["attachment_coverage_complete"] is True
    assert receipt["attachment_verification_complete"] is True
    assert receipt["tools_used"] == []


@pytest.mark.asyncio
async def test_password_validation_deadline_cannot_replay_or_persist_archive(
    settings,
    storage,
    monkeypatch,
) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    old_payload = _encrypted_zip()
    old_receipt = await pipeline.ingest_file(
        "alice",
        None,
        old_payload,
        filename="protected.zip",
        metadata={"uploaded_by": "alice"},
        source_ref="successful-before-deadline",
        archive_password=_PASSWORD,
    )
    old_raw_id = old_receipt["raw_object_id"]
    old_files = {path for path in settings.files_dir.rglob("*") if path.is_file()}
    old_counts = {
        table: storage.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
        for table in ("raw_objects", "inbox", "knowledge_objects")
    }
    assert old_files

    def validation_deadline(*_args, **_kwargs) -> DocumentResult:
        return DocumentResult(
            "",
            {
                "password_validation_incomplete": True,
                "parse_deadline_reached": True,
            },
            False,
            "password_validation_incomplete",
        )

    def forbidden_dedup(*_args, **_kwargs):
        raise AssertionError("password validation incomplete reached durable dedup")

    monkeypatch.setattr(pipeline._doc_extractor, "extract", validation_deadline)  # noqa: SLF001
    monkeypatch.setattr(storage, "find_raw_by_source_ref", forbidden_dedup)
    monkeypatch.setattr(storage, "find_file_by_content_hash", forbidden_dedup)

    replay_attempt = await pipeline.ingest_file(
        "alice",
        None,
        old_payload,
        filename="protected.zip",
        metadata={"uploaded_by": "alice"},
        source_ref="successful-before-deadline",
        archive_password=_PASSWORD,
    )
    new_attempt = await pipeline.ingest_file(
        "alice",
        None,
        _encrypted_zip(),
        filename="new-protected.zip",
        metadata={"uploaded_by": "alice"},
        source_ref="new-after-deadline",
        archive_password=_PASSWORD,
    )
    transient_attempt = await pipeline.inspect_file_transient(
        _encrypted_zip(),
        filename="transient-protected.zip",
        archive_password=_PASSWORD,
    )

    for result in (replay_attempt, new_attempt, transient_attempt):
        assert result["password_validation_incomplete"] is True
        assert result["persisted"] is False
        assert result.get("raw_object_id") is None
        assert result["extraction_success"] is False
    assert replay_attempt.get("idempotent_replay") is not True
    assert storage.get_raw_object(old_raw_id, "alice") is not None
    for table, old_count in old_counts.items():
        assert storage.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == old_count  # nosec B608
    assert {path for path in settings.files_dir.rglob("*") if path.is_file()} == old_files


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
    assert header["_document_metadata"]["print_date"] == "2023-06-08T09:10:11Z"
    assert header["_document_metadata"]["paragraph_count"] == 12
    assert header["_document_metadata"]["user_defined"] == [
        {"name": "Подразделение", "value_type": "string", "value": "Отдел 7"}
    ]
    assert "text_preview" not in header and "_runtime_source_text" not in header
    assert stored["title"] == "ODF-METADATA-TITLE"
    assert stored["document_date"] == "2023-06-07"
    assert stored["print_date"] == "2023-06-08T09:10:11Z"
    assert stored["paragraph_count"] == 12
    assert stored["non_whitespace_character_count"] == 500
    assert stored["user_defined"] == [{"name": "Подразделение", "value_type": "string", "value": "Отдел 7"}]


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


def test_password_validation_incomplete_has_no_generic_failure_or_pending_challenge(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    try:
        response = {
            "message": "Проверка пароля не завершилась; архив не сохранён.",
            "message_format": "plain",
            "password_validation_incomplete": True,
            "file_ingestion": {
                "password_validation_incomplete": True,
                "persisted": False,
                "extraction_success": False,
                "extraction": {"success": False},
            },
        }
        rendered = bridge._format_response_message(response)  # noqa: SLF001

        assert rendered == response["message"]
        assert "извлечь не удалось" not in rendered.casefold()
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
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
async def test_replied_archive_password_is_ephemeral_and_current_media_wins_pending(
    tmp_path,
    monkeypatch,
) -> None:
    bridge = _bridge(tmp_path)
    backend_payloads: list[dict] = []

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        if "document" not in message:
            return None
        descriptor = message["document"]
        return {
            "filename": descriptor["file_name"],
            "mime_type": descriptor["mime_type"],
            "content_base64": "TkVXLUZJTEU=",
            "source_ref": f"telegram-file:{descriptor['file_id']}",
            "media_kind": "document",
        }

    async def backend(_client, method, path, payload, _user, _chat):  # noqa: ANN001
        assert method == "POST" and path == "/api/chat"
        backend_payloads.append(dict(payload))
        return {"message": "ok", "message_format": "plain"}

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)

    archive = {
        "file_id": "old-pending-archive",
        "file_unique_id": "old-pending-unique",
        "file_name": "old-protected.zip",
        "mime_type": "application/zip",
        "file_size": 123,
    }
    try:
        # A password written in the caption of the exact archive being replied
        # to is credential data, not quoted chat content.  It reaches this one
        # request, while both the durable update and backend reply quote are
        # scrubbed before either can retain it.
        replied_archive = {
            "update_id": 4501,
            "message": {
                "message_id": 4501,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": "Что внутри этого архива?",
                "reply_to_message": {
                    "message_id": 4499,
                    "caption": f"пароль: {_PASSWORD}",
                    "caption_entities": [{"type": "bold", "offset": 0, "length": 6}],
                    "document": dict(archive),
                },
            },
        }
        safe_reply = bridge._sanitize_update_before_store(replied_archive)  # noqa: SLF001
        assert safe_reply["message"]["reply_to_message"]["caption"] == ""
        assert "caption_entities" not in safe_reply["message"]["reply_to_message"]
        assert bridge._inbox.store(safe_reply) is True  # noqa: SLF001
        await bridge._process_update(object(), object(), safe_reply, cached_response=None)  # noqa: SLF001
        replied_payload = backend_payloads.pop()
        assert replied_payload["archive_password"] == _PASSWORD
        assert replied_payload["reply_document_source_ref"] == "telegram-file:old-pending-archive"
        # The password is a separate request-local field, so exclude it before
        # checking the public/message-shaped portion of the payload.
        scrubbed_payload = dict(replied_payload)
        assert scrubbed_payload.pop("archive_password") == _PASSWORD
        assert _PASSWORD not in json.dumps(scrubbed_payload, ensure_ascii=False)
        assert _PASSWORD not in "\n".join(bridge._inbox._conn.iterdump())  # noqa: SLF001

        # An older pending archive must never replace a newly attached ordinary
        # document merely because that new document has a caption.
        bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
            5001,
            1001,
            archive,
            safe_query="Открой старый архив",
            original_message_id=4499,
        )
        new_document = {
            "update_id": 4502,
            "message": {
                "message_id": 4502,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "caption": "Кратко по новому файлу",
                "document": {
                    "file_id": "new-current-odt",
                    "file_unique_id": "new-current-unique",
                    "file_name": "new-current.odt",
                    "mime_type": "application/vnd.oasis.opendocument.text",
                    "file_size": 456,
                },
            },
        }
        safe_new = bridge._sanitize_update_before_store(new_document)  # noqa: SLF001
        assert "friday_archive_password_followup" not in safe_new
        await bridge._process_update(object(), object(), safe_new, cached_response=None)  # noqa: SLF001
        current_payload = backend_payloads.pop()
        assert current_payload["document"]["filename"] == "new-current.odt"
        assert current_payload["message"] == "Кратко по новому файлу"
        assert "archive_password" not in current_payload
        assert "reply_document_source_ref" not in current_payload
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001

        # The precedence must be structural, not an accident of a prose caption
        # being rejected by the standalone-password grammar. `report2` is a
        # valid legacy standalone credential shape; current media still wins.
        bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
            5001,
            1001,
            archive,
            safe_query="Открой старый архив",
            original_message_id=4499,
        )
        password_shaped_document = {
            "update_id": 4510,
            "message": {
                "message_id": 4510,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "caption": "report2",
                "document": {
                    "file_id": "new-current-password-shaped",
                    "file_unique_id": "new-current-password-shaped-unique",
                    "file_name": "new-current-password-shaped.odt",
                    "mime_type": "application/vnd.oasis.opendocument.text",
                    "file_size": 457,
                },
            },
        }
        safe_password_shaped = bridge._sanitize_update_before_store(  # noqa: SLF001
            password_shaped_document
        )
        assert "friday_archive_password_followup" not in safe_password_shaped
        await bridge._process_update(  # noqa: SLF001
            object(), object(), safe_password_shaped, cached_response=None
        )
        password_shaped_payload = backend_payloads.pop()
        assert password_shaped_payload["document"]["filename"] == "new-current-password-shaped.odt"
        assert password_shaped_payload["message"] == "report2"
        assert "archive_password" not in password_shaped_payload
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001

        # A wrong retry may refresh expiry/query, but never drifts the durable
        # origin from the archive request to the credential message itself.
        bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
            5001,
            1001,
            archive,
            safe_query="Открой старый архив",
            original_message_id=4499,
        )

        async def invalid_backend(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
            backend_payloads.append(dict(payload))
            return {
                "message": "Пароль не подошёл",
                "message_format": "plain",
                "archive_password_invalid": True,
            }

        monkeypatch.setattr(bridge, "_backend_json", invalid_backend)
        invalid_retry = {
            "update_id": 4511,
            "message": {
                "message_id": 4511,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": "wrong2",
            },
        }
        safe_invalid = bridge._sanitize_update_before_store(invalid_retry)  # noqa: SLF001
        assert safe_invalid["friday_archive_password_followup"] is True
        await bridge._process_update(object(), object(), safe_invalid, cached_response=None)  # noqa: SLF001
        invalid_payload = backend_payloads.pop()
        assert invalid_payload["archive_password"] == "wrong2"
        refreshed = bridge._inbox.archive_password_challenge(5001, 1001)  # noqa: SLF001
        assert refreshed is not None
        assert refreshed["original_message_id"] == 4499
        monkeypatch.setattr(bridge, "_backend_json", backend)

        # Ordinary prose after a challenge is an ordinary turn and cancels the
        # stale challenge; it is never retried as a guessed password.
        bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
            5001,
            1001,
            archive,
            safe_query="Открой старый архив",
            original_message_id=4499,
        )
        for offset, ordinary_text in enumerate(
            ("Почему архив не открылся?", "Почему?", "неверный!", "стоп."),
            start=3,
        ):
            if offset > 3:
                bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
                    5001,
                    1001,
                    archive,
                    safe_query="Открой старый архив",
                    original_message_id=4499,
                )
            ordinary = {
                "update_id": 4500 + offset,
                "message": {
                    "message_id": 4500 + offset,
                    "chat": {"id": 5001},
                    "from": {"id": 1001},
                    "text": ordinary_text,
                },
            }
            safe_ordinary = bridge._sanitize_update_before_store(ordinary)  # noqa: SLF001
            assert "friday_archive_password_followup" not in safe_ordinary
            assert safe_ordinary["message"]["text"] == ordinary_text
            await bridge._process_update(  # noqa: SLF001
                object(), object(), safe_ordinary, cached_response=None
            )
            ordinary_payload = backend_payloads.pop()
            assert ordinary_payload["message"] == ordinary_text
            assert "document" not in ordinary_payload
            assert "archive_password" not in ordinary_payload
            assert "reply_document_source_ref" not in ordinary_payload
            assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
    finally:
        bridge._archive_passwords.clear()  # noqa: SLF001
        bridge._inbox.close()  # noqa: SLF001


@pytest.mark.asyncio
async def test_password_only_replied_message_unlocks_exact_pending_archive(tmp_path, monkeypatch) -> None:
    bridge = _bridge(tmp_path)
    backend_payloads: list[dict] = []
    archive = {
        "file_id": "pending-password-reply-archive",
        "file_unique_id": "pending-password-reply-unique",
        "file_name": "pending-protected.zip",
        "mime_type": "application/zip",
        "file_size": 123,
    }

    async def prepare(_telegram, message, _update):  # noqa: ANN001
        descriptor = message["document"]
        return {
            "filename": descriptor["file_name"],
            "mime_type": descriptor["mime_type"],
            "content_base64": "UEs=",
            "source_ref": f"telegram-file:{descriptor['file_id']}",
            "media_kind": "document",
        }

    async def backend(_client, _method, _path, payload, _user, _chat):  # noqa: ANN001
        backend_payloads.append(dict(payload))
        return {"message": "Архив прочитан", "message_format": "plain"}

    async def send(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bridge, "_prepare_document", prepare)
    monkeypatch.setattr(bridge, "_backend_json", backend)
    monkeypatch.setattr(bridge, "_send_message", send)

    try:
        bridge._inbox.remember_archive_password_challenge(  # noqa: SLF001
            5001,
            1001,
            archive,
            safe_query="Открой архив",
            original_message_id=4600,
        )
        followup = {
            "update_id": 4602,
            "message": {
                "message_id": 4602,
                "chat": {"id": 5001},
                "from": {"id": 1001},
                "text": "Это пароль",
                "reply_to_message": {
                    "message_id": 4601,
                    "text": _PASSWORD,
                    "entities": [{"type": "code", "offset": 0, "length": len(_PASSWORD)}],
                },
            },
        }
        safe = bridge._sanitize_update_before_store(followup)  # noqa: SLF001
        assert safe["friday_archive_password_followup"] is True
        assert safe["message"]["reply_to_message"]["text"] == ""
        assert "entities" not in safe["message"]["reply_to_message"]
        assert bridge._inbox.store(safe) is True  # noqa: SLF001
        await bridge._process_update(object(), object(), safe, cached_response=None)  # noqa: SLF001

        assert len(backend_payloads) == 1
        assert backend_payloads[0]["archive_password"] == _PASSWORD
        assert backend_payloads[0]["document"]["filename"] == "pending-protected.zip"
        scrubbed_payload = dict(backend_payloads[0])
        assert scrubbed_payload.pop("archive_password") == _PASSWORD
        assert _PASSWORD not in json.dumps(scrubbed_payload, ensure_ascii=False)
        assert _PASSWORD not in "\n".join(bridge._inbox._conn.iterdump())  # noqa: SLF001
        assert bridge._inbox.archive_password_challenge(5001, 1001) is None  # noqa: SLF001
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
