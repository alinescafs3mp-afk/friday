from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage import JerichoStorage, UnsupportedSchemaVersionError
from jericho.storage.models import EntityType, KnowledgeObject, RawObject, RelationType, new_id, utc_now
from jericho.web_surfer import (
    UnsafeURLError,
    WebSurfer,
    _PinnedPublicNetworkBackend,
    validate_public_url,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "Не запоминай: сервер Astra использует порт 9001 и PostgreSQL 16.",
        "Do not remember: Project Orion uses PostgreSQL 16 on port 5432.",
    ],
)
async def test_explicit_no_save_overrides_force_and_creates_no_knowledge_trace(settings, storage, content):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)

    result = await pipeline.ingest_text(
        "alice",
        content,
        source="api",
        source_ref="private:1",
        force_knowledge=True,
    )

    assert result["action"] == "transient"
    assert result["category"] == "private_transient"
    assert result["persisted"] is False
    assert result["raw_object_id"] is None
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
    assert storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    assert storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0
    assert storage.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


def test_ensure_user_merges_channel_metadata_without_erasing_admin_fields(storage):
    storage.ensure_user(
        "telegram:telegram:42",
        source="admin",
        metadata={"admin_note": "VIP", "chat_id": "old"},
    )
    user = storage.ensure_user(
        "telegram:telegram:42",
        source="telegram",
        metadata={"chat_id": "new", "language_code": "ru"},
    )

    metadata = json.loads(user["metadata_json"])
    assert metadata == {"admin_note": "VIP", "chat_id": "new", "language_code": "ru"}


def test_future_schema_is_rejected_without_modifying_database(settings, tmp_path: Path):
    database = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO schema_meta VALUES('schema_version', '999', 'future');
        CREATE TABLE future_sentinel(value TEXT NOT NULL);
        INSERT INTO future_sentinel VALUES('do-not-touch');
        """
    )
    conn.close()

    instance = JerichoStorage(replace(settings, database_path=database))
    with pytest.raises(UnsupportedSchemaVersionError, match="999"):
        _ = instance.conn

    verify = sqlite3.connect(database)
    try:
        assert (
            verify.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "999"
        )
        assert verify.execute("SELECT value FROM future_sentinel").fetchone()[0] == "do-not-touch"
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_concurrent_chat_retry_is_claimed_before_side_effects(settings):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Запомни: Project Alpha launches in September.",
        "force_knowledge": True,
        "source_ref": "concurrent:telegram-update:1",
    }

    async with app.router.lifespan_context(app):
        original_chat = app.state.agent.chat
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def delayed_chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return await original_chat(*args, **kwargs)

        app.state.agent.chat = delayed_chat
        transport = httpx.ASGITransport(app=app, client=("198.51.100.7", 4312))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_task = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await client.post("/api/chat", json=payload, headers=headers)
            assert second.status_code == 409
            assert second.headers["Retry-After"] == "2"

            release.set()
            first = await first_task
            assert first.status_code == 200, first.text
            replay = await client.post("/api/chat", json=payload, headers=headers)
            assert replay.status_code == 200
            assert replay.json()["idempotent_replay"] is True

        assert calls == 1
        assert app.state.storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1
        assert app.state.storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 1
        assert app.state.storage.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert app.state.storage.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_proxy_headers_are_ignored_from_untrusted_peer(settings):
    from jericho.server import create_app

    proxy_settings = replace(
        settings,
        api_host="0.0.0.0",
        api_require_token_on_loopback=False,
        trust_proxy_headers=True,
        trusted_proxy_networks=["127.0.0.1/32"],
    )
    app = create_app(proxy_settings)
    async with app.router.lifespan_context(app):
        remote_transport = httpx.ASGITransport(app=app, client=("198.51.100.77", 9000))
        async with httpx.AsyncClient(transport=remote_transport, base_url="http://test") as remote:
            spoofed = await remote.get("/api/me", headers={"X-Forwarded-For": "127.0.0.1"})
            assert spoofed.status_code == 401

        proxy_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        # The loopback CSRF guard requires a loopback Host header, so the
        # trusted-peer request must present one to reach the owner bypass.
        async with httpx.AsyncClient(transport=proxy_transport, base_url="http://127.0.0.1:8000") as proxy:
            trusted = await proxy.get("/api/me", headers={"X-Forwarded-For": "127.0.0.1"})
            assert trusted.status_code == 200
            assert trusted.json()["actor"]["source"] == "loopback"


@pytest.mark.asyncio
async def test_code_runner_stops_process_when_output_budget_is_exceeded(settings, storage):
    executable_settings = replace(
        settings,
        code_execution_enabled=True,
        code_execution_timeout_sec=5,
        code_execution_max_output_bytes=4096,
    )
    storage.ensure_user("operator", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(executable_settings, storage, graph)
    web = WebSurfer(executable_settings)
    kernel = ExecutionKernel(auth, executable_settings)
    kernel.bind_services(storage, graph, web, ingestion)
    actor = auth.actor_for_user("operator", source="test")
    try:
        result = await kernel.execute(
            "code_run",
            {"code": "import os\nwhile True:\n    os.write(1, b'x' * 8192)\n"},
            actor=actor,
        )
        assert result.success is True
        assert result.data["output_truncated"] is True
        assert result.data["terminated_for_output_limit"] is True
        captured = len(result.data["stdout"].encode()) + len(result.data["stderr"].encode())
        assert captured <= executable_settings.code_execution_max_output_bytes
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_dns_rebinding_is_blocked_again_at_tcp_connect(monkeypatch):
    answers = iter(
        [
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ]
    )
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: next(answers))
    assert validate_public_url("http://rebind.example/") == "http://rebind.example/"

    class NeverConnect:
        calls = 0

        async def connect_tcp(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace()

        async def sleep(self, seconds):
            await asyncio.sleep(seconds)

    backend = _PinnedPublicNetworkBackend(allow_private_networks=False)
    delegate = NeverConnect()
    backend._delegate = delegate
    with pytest.raises(UnsafeURLError, match="connect time"):
        await backend.connect_tcp("rebind.example", 80)
    assert delegate.calls == 0


def test_backup_without_manifest_is_not_reported_as_verified(storage):
    result = storage.create_backup(label="manifest-required")
    Path(result["manifest_path"]).unlink()
    verification = storage.verify_backup(result["database"])
    assert verification["integrity_check"] == "ok"
    assert verification["manifest_present"] is False
    assert verification["ok"] is False
    assert "missing" in verification["manifest_error"].casefold()


def test_soft_deleted_file_is_hidden_from_user_routes(settings):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/files",
            files={"file": ("secret.txt", b"durable file", "text/plain")},
            headers=headers,
        )
        assert uploaded.status_code == 200, uploaded.text
        raw_id = uploaded.json()["raw_object_id"]
        app.state.storage.execute(
            "UPDATE raw_objects SET deleted_at=? WHERE id=?",
            (utc_now(), raw_id),
        )
        app.state.storage.commit()

        listed = client.get("/api/files", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"] == []
        assert client.get(f"/api/files/{raw_id}", headers=headers).status_code == 404


def test_user_export_includes_versions_permissions_and_sessions(storage):
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref="export:1",
        raw_content="Project Alpha uses PostgreSQL 16.",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=raw.raw_content,
        title="Alpha",
    )
    storage.store_knowledge_object(ko)
    storage.update_knowledge_fields(ko.id, "alice", title="Alpha database")
    storage.set_permission_override("alice", "web.search", "deny")
    conversation = storage.create_conversation("alice", "Exported conversation")
    storage.set_channel_conversation("alice", "telegram", "42", conversation["id"])

    export = storage.export_user("alice")
    payload = json.loads(Path(export["path"]).read_text(encoding="utf-8"))
    assert payload["format"] == "jericho-user-export-v3"
    assert len(payload["knowledge_object_versions"]) == 2
    assert payload["user_permission_overrides"][0]["security_id"] == "web.search"
    assert payload["channel_sessions"][0]["channel_id"] == "42"
    assert "feedback_state" in payload
    assert "knowledge_usage" in payload
    assert "relation_candidates" in payload
    assert "knowledge_conflicts" in payload
    # Слежения человек завёл сам — в архиве аккаунта они обязаны быть. Новая
    # таблица, забытая в экспорте, теряется молча: архив выглядит полным.
    storage.create_monitor("alice", "поверка весов")
    reexported = json.loads(Path(storage.export_user("alice")["path"]).read_text(encoding="utf-8"))
    assert [row["query"] for row in reexported["monitors"]] == ["поверка весов"]


@pytest.mark.asyncio
async def test_text_source_ref_reuse_with_different_content_is_rejected(settings, storage):
    from jericho.ingestion import IdempotencyConflictError

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_text(
        "alice",
        "Запомни: Project Alpha uses PostgreSQL 16.",
        source="api",
        source_ref="immutable:text:1",
        force_knowledge=True,
    )

    with pytest.raises(IdempotencyConflictError, match="different text"):
        await pipeline.ingest_text(
            "alice",
            "Запомни: Project Beta uses SQLite.",
            source="api",
            source_ref="immutable:text:1",
            force_knowledge=True,
        )

    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1
    assert storage.get_raw_object(first["raw_object_id"], "alice")["raw_content"].endswith("PostgreSQL 16.")


@pytest.mark.asyncio
async def test_raw_audio_is_stored_with_provenance_and_queued_for_review(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        "telegram:telegram:1001",
        None,
        b"\x00\x01OpusHead-fake-voice-bytes",
        filename="telegram-voice-7.ogg",
        mime_type="audio/ogg",
        media_kind="voice",
        metadata={
            "channel": "telegram-bridge",
            "media_kind": "voice",
            "forward": {"from_user": {"id": 42, "username": "bob"}},
        },
        source_ref="telegram-file:99:voice-7",
    )

    # Non-transcribable media is captured (not dropped) and waits for review.
    assert result["queued_for_review"] is True
    raw = storage.get_raw_object(result["raw_object_id"], "telegram:telegram:1001")
    meta = json.loads(raw["metadata_json"])
    assert meta["extraction_success"] is False
    assert meta["media_kind"] == "voice"
    assert meta["forward"]["from_user"]["id"] == 42
    # The placeholder body reflects the media kind rather than a generic "File".
    assert raw["raw_content"].startswith("[voice: telegram-voice-7.ogg")


@pytest.mark.asyncio
async def test_file_source_ref_reuse_with_different_bytes_is_rejected(settings, storage):
    from jericho.ingestion import IdempotencyConflictError

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_file(
        "alice",
        None,
        b"first file payload",
        filename="note.txt",
        mime_type="text/plain",
        source_ref="immutable:file:1",
    )

    with pytest.raises(IdempotencyConflictError, match="different file"):
        await pipeline.ingest_file(
            "alice",
            None,
            b"second file payload",
            filename="note.txt",
            mime_type="text/plain",
            source_ref="immutable:file:1",
        )

    raw = storage.get_raw_object(first["raw_object_id"], "alice")
    assert json.loads(raw["metadata_json"])["sha256"] == hashlib.sha256(b"first file payload").hexdigest()
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_file_is_removed_when_outer_ingestion_transaction_rolls_back(settings, storage, monkeypatch):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    storage.ensure_user("alice")
    original_transaction = storage.transaction

    @contextmanager
    def fail_before_outer_commit():
        was_nested = storage.conn.in_transaction
        with original_transaction() as conn:
            yield conn
            if not was_nested:
                row = conn.execute(
                    "SELECT 1 FROM raw_objects WHERE source_ref='commit-failure:file:1'"
                ).fetchone()
                if row is not None:
                    raise RuntimeError("injected outer transaction failure")

    monkeypatch.setattr(storage, "transaction", fail_before_outer_commit)
    with pytest.raises(RuntimeError, match="outer transaction failure"):
        await pipeline.ingest_file(
            "alice",
            None,
            b"file that must not become orphaned",
            filename="orphan.txt",
            mime_type="text/plain",
            source_ref="commit-failure:file:1",
        )

    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
    assert not any(
        settings.files_dir.rglob(
            "*" + hashlib.sha256(b"file that must not become orphaned").hexdigest() + "*"
        )
    )


def test_chat_source_ref_is_bound_to_request_payload(settings):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    first_payload = {
        "message": "Запомни: Project Alpha uses PostgreSQL 16.",
        "force_knowledge": True,
        "source_ref": "immutable:chat:1",
    }
    conflicting_payload = {
        "message": "Запомни: Project Beta uses SQLite.",
        "force_knowledge": True,
        "source_ref": "immutable:chat:1",
    }

    with TestClient(app) as client:
        first = client.post("/api/chat", json=first_payload, headers=headers)
        assert first.status_code == 200, first.text

        exact_replay = client.post("/api/chat", json=first_payload, headers=headers)
        assert exact_replay.status_code == 200
        assert exact_replay.json()["idempotent_replay"] is True

        conflict = client.post("/api/chat", json=conflicting_payload, headers=headers)
        assert conflict.status_code == 409
        assert "different request" in conflict.json()["detail"]

        rows = app.state.storage.execute(
            "SELECT raw_content FROM raw_objects WHERE source_ref='immutable:chat:1'"
        ).fetchall()
        assert [row["raw_content"] for row in rows] == [first_payload["message"]]
        assert app.state.storage.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_file_storage_keeps_colliding_tenant_slugs_in_separate_directories(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_file(
        "a:b",
        None,
        b"same bytes",
        filename="shared.txt",
        mime_type="text/plain",
        source_ref="tenant-file:1",
    )
    second = await pipeline.ingest_file(
        "a_b",
        None,
        b"same bytes",
        filename="shared.txt",
        mime_type="text/plain",
        source_ref="tenant-file:1",
    )

    first_raw = storage.get_raw_object(first["raw_object_id"], "a:b")
    second_raw = storage.get_raw_object(second["raw_object_id"], "a_b")
    # Путь теперь ОТНОСИТЕЛЬНЫЙ корню хранилища — абсолютный привязывал архив к
    # машине. Проверка о разделении арендаторов от этого не меняется: она про
    # РАЗНЫЕ каталоги, а не про форму записи.
    first_path = settings.files_dir / json.loads(first_raw["metadata_json"])["stored_path"]
    second_path = settings.files_dir / json.loads(second_raw["metadata_json"])["stored_path"]
    assert first_path != second_path
    assert first_path.parents[1] != second_path.parents[1]
    assert first_path.read_bytes() == second_path.read_bytes() == b"same bytes"


def test_exports_for_colliding_user_slugs_never_overwrite_each_other(storage):
    storage.ensure_user("a:b", display_name="Colon user")
    storage.ensure_user("a_b", display_name="Underscore user")

    first = storage.export_user("a:b")
    second = storage.export_user("a_b")

    assert first["filename"] != second["filename"]
    assert json.loads(Path(first["path"]).read_text(encoding="utf-8"))["user"]["id"] == "a:b"
    assert json.loads(Path(second["path"]).read_text(encoding="utf-8"))["user"]["id"] == "a_b"


@pytest.mark.parametrize(
    ("field", "replacement", "result_field"),
    [
        ("database", "other.sqlite3", "manifest_database_matches"),
        ("size_bytes", -1, "manifest_size_matches"),
        ("schema_version", 999, "manifest_schema_supported"),
    ],
)
def test_backup_manifest_identity_fields_are_verified(storage, field, replacement, result_field):
    created = storage.create_backup(label=f"tamper-{field}")
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = replacement
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = storage.verify_backup(created["database"])
    assert verification["integrity_check"] == "ok"
    assert verification["hash_matches_manifest"] is True
    assert verification[result_field] is False
    assert verification["ok"] is False


def test_backup_listing_ignores_manifest_path_escape(storage):
    malicious = storage.settings.backups_dir / "malicious.manifest.json"
    malicious.write_text(
        json.dumps({"database": "../outside.sqlite3", "sha256": "0" * 64}),
        encoding="utf-8",
    )
    assert all(item.get("manifest_path") != str(malicious) for item in storage.list_backups())


def test_backup_manifest_schema_must_match_database_schema(storage):
    created = storage.create_backup(label="schema-match")
    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = max(0, int(created["schema_version"]) - 1)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = storage.verify_backup(created["database"])
    assert verification["manifest_schema_supported"] is True
    assert verification["manifest_schema_matches_database"] is False
    assert verification["database_schema_version"] == created["schema_version"]
    assert verification["ok"] is False


def test_backup_verification_detects_foreign_key_corruption_even_with_fresh_hash(storage):
    storage.ensure_user("backup-user")
    raw = RawObject(
        id=new_id("raw"),
        user_id="backup-user",
        source="test",
        source_ref="backup-fk:1",
        raw_content="Knowledge with provenance",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="backup-user",
            raw_object_id=raw.id,
            content=raw.raw_content,
            title="Backup FK",
        )
    )
    created = storage.create_backup(label="foreign-key")
    backup_path = Path(created["path"])

    conn = sqlite3.connect(backup_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM users WHERE id='backup-user'")
        conn.commit()
    finally:
        conn.close()

    manifest_path = Path(created["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["size_bytes"] = backup_path.stat().st_size
    manifest["sha256"] = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verification = storage.verify_backup(created["database"])
    assert verification["integrity_check"] == "ok"
    assert verification["hash_matches_manifest"] is True
    assert verification["foreign_key_violations"] > 0
    assert verification["ok"] is False


def test_backup_manifest_symlink_is_rejected(storage):
    created = storage.create_backup(label="manifest-symlink")
    manifest_path = Path(created["manifest_path"])
    real_manifest = manifest_path.with_name(f"real-{manifest_path.name}")
    manifest_path.rename(real_manifest)
    try:
        manifest_path.symlink_to(real_manifest.name)
    except OSError:
        pytest.skip("symlinks are not available in this environment")

    verification = storage.verify_backup(created["database"])
    assert verification["manifest_present"] is False
    assert "symlink" in verification["manifest_error"].casefold()
    assert verification["ok"] is False
    assert all(item.get("manifest_path") != str(manifest_path) for item in storage.list_backups())


def test_graph_rejects_non_finite_weights_and_unknown_link_states(storage):
    storage.ensure_user("graph-user")
    graph = KnowledgeGraph(storage)
    first = graph.create_entity("graph-user", "Alpha", EntityType.PROJECT)
    second = graph.create_entity("graph-user", "Beta", EntityType.PROJECT)

    with pytest.raises(ValueError, match="finite number"):
        graph.create_relation(
            "graph-user",
            first["id"],
            second["id"],
            RelationType.RELATED_TO,
            weight=float("nan"),
        )

    raw = RawObject(
        id=new_id("raw"),
        user_id="graph-user",
        source="test",
        source_ref="graph-validation:1",
        raw_content="Alpha uses Beta.",
        content_type="text",
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="graph-user",
        raw_object_id=raw.id,
        content=raw.raw_content,
        title="Graph validation",
    )
    storage.store_knowledge_object(knowledge)

    with pytest.raises(ValueError, match="suggested, accepted, or rejected"):
        graph.link_knowledge_to_entity(
            knowledge.id,
            first["id"],
            "graph-user",
            status="unreviewed",
        )
    with pytest.raises(ValueError, match="finite number"):
        graph.link_knowledge_to_entity(
            knowledge.id,
            first["id"],
            "graph-user",
            confidence=float("nan"),
        )


def test_explicit_no_save_document_is_transient_and_never_written(settings):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    captured: dict[str, object] = {}

    with TestClient(app) as client:

        async def capture_chat(*args, **kwargs):
            captured["attachments"] = kwargs.get("attachments")
            return {"conversation_id": "transient-conversation", "content": "Проверено без сохранения."}

        app.state.agent.chat = capture_chat
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Не запоминай этот документ, только посмотри его.",
                "source_ref": "private-document:1",
                "document": {
                    "filename": "private.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"private transient payload").decode(),
                },
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ingestion"]["persisted"] is False
        assert payload["file_ingestion"]["persisted"] is False
        assert payload["file_ingestion"]["raw_object_id"] is None
        assert app.state.storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
        assert app.state.storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0
        assert not any(settings.files_dir.rglob("private.txt"))

    attachments = captured["attachments"]
    assert isinstance(attachments, list)
    assert attachments[0]["transient"] is True
    assert attachments[0]["transient_text"] == "private transient payload"


def test_invalid_oversized_caption_cannot_persist_document_before_413(settings):
    from jericho.server import create_app

    limited = replace(settings, max_extracted_text_chars=16)
    app = create_app(limited)
    headers = {"Authorization": f"Bearer {limited.api_token}"}
    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "x" * 17,
                "source_ref": "invalid-caption:1",
                "document": {
                    "filename": "must-not-exist.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"must not persist").decode(),
                },
            },
        )
        assert response.status_code == 413
        assert app.state.storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
        assert not any(limited.files_dir.rglob("must-not-exist.txt"))


@pytest.mark.asyncio
async def test_physical_upload_path_is_bounded_for_extreme_windows_filename(settings, storage):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    original_name = ("очень-длинное-имя-" * 40) + ".txt"
    result = await pipeline.ingest_file(
        "telegram:realm:123456789",
        None,
        b"bounded path",
        filename=original_name,
        mime_type="text/plain",
        source_ref="long-name:1",
    )
    raw = storage.get_raw_object(result["raw_object_id"], "telegram:realm:123456789")
    # Путь теперь ОТНОСИТЕЛЬНЫЙ корню хранилища: абсолютный привязывал архив к
    # машине, и после переезда каждый из 1671 файла отдавал 404.
    stored = settings.files_dir / json.loads(raw["metadata_json"])["stored_path"]
    assert stored.is_file()
    assert len(stored.name) <= 81
    assert stored.name.endswith(".txt")


def test_direct_text_ingestion_is_atomic_across_independent_connections(settings, storage):
    """Two API workers must never expose a half-promoted Raw Object."""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    second_storage = JerichoStorage(settings)
    first_pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    second_pipeline = IngestionPipeline(settings, second_storage, KnowledgeGraph(second_storage))
    barrier = Barrier(2)

    def synchronize_initial_source_lookup(instance):
        original = instance.find_raw_by_source_ref
        calls = 0

        def wrapped(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                # Both workers must observe the source_ref as absent before either
                # enters the serialized ingestion transaction.
                barrier.wait(timeout=5)
            return result

        instance.find_raw_by_source_ref = wrapped

    synchronize_initial_source_lookup(storage)
    synchronize_initial_source_lookup(second_storage)

    async def run(pipeline):
        return await pipeline.ingest_text(
            "alice",
            "Запомни: Project Atomic uses PostgreSQL 16 in production.",
            source="api",
            source_ref="atomic-direct:1",
            force_knowledge=True,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(asyncio.run, run(first_pipeline)),
                executor.submit(asyncio.run, run(second_pipeline)),
            ]
            results = [future.result(timeout=10) for future in futures]
    finally:
        second_storage.close()

    assert sum(bool(result.get("idempotent_replay")) for result in results) == 1
    assert all(result["promoted"] is True for result in results)
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1
    assert storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 1
    assert storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_losing_file_source_ref_race_leaves_no_final_or_staged_orphan(
    settings, storage, monkeypatch: pytest.MonkeyPatch
):
    from jericho.ingestion import IdempotencyConflictError

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    await pipeline.ingest_file(
        "alice",
        None,
        b"winner bytes",
        filename="note.txt",
        mime_type="text/plain",
        source_ref="racy-file:1",
    )

    original_find = storage.find_raw_by_source_ref
    calls = 0

    def miss_initial_check(user_id: str, source: str, source_ref: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_find(user_id, source, source_ref)

    monkeypatch.setattr(storage, "find_raw_by_source_ref", miss_initial_check)
    losing_bytes = b"losing conflicting bytes"
    losing_digest = hashlib.sha256(losing_bytes).hexdigest()
    losing_target = pipeline._file_target("alice", losing_digest, "note.txt")

    with pytest.raises(IdempotencyConflictError, match="different file"):
        await pipeline.ingest_file(
            "alice",
            None,
            losing_bytes,
            filename="note.txt",
            mime_type="text/plain",
            source_ref="racy-file:1",
        )

    assert not losing_target.exists()
    assert not list(losing_target.parent.glob(f".{losing_digest}.*.tmp"))
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1
    assert storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_failed_file_promotion_rolls_back_database_and_new_file(
    settings, storage, monkeypatch: pytest.MonkeyPatch
):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    payload = b"promotion must roll back"
    digest = hashlib.sha256(payload).hexdigest()
    target = pipeline._file_target("alice", digest, "rollback.txt")

    def fail_promotion(**kwargs):
        raise RuntimeError("injected promotion failure")

    monkeypatch.setattr(pipeline, "_promote_raw", fail_promotion)
    with pytest.raises(RuntimeError, match="injected promotion failure"):
        await pipeline.ingest_file(
            "alice",
            None,
            payload,
            filename="rollback.txt",
            mime_type="text/plain",
            source_ref="rollback-file:1",
        )

    assert not target.exists()
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
    assert storage.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"api_port": 65536}, "JERICHO_API_PORT"),
        ({"trust_proxy_headers": True, "trusted_proxy_networks": ["0.0.0.0/0"]}, "Unrestricted"),
        ({"trust_proxy_headers": True, "trusted_proxy_networks": ["::/0"]}, "Unrestricted"),
        ({"cors_origins": ["http://localhost:8000/admin"]}, "Invalid CORS origin"),
        ({"cors_origins": ["http://user:pass@localhost:8000"]}, "Invalid CORS origin"),
        ({"cors_origins": ["http://localhost:8000?token=x"]}, "Invalid CORS origin"),
    ],
)
def test_security_sensitive_settings_fail_closed(settings, changes, expected):
    from jericho.config import validate_settings

    problems = validate_settings(replace(settings, **changes))
    assert any(expected in problem for problem in problems)


def test_api_and_bridge_credentials_must_be_separate(settings):
    from jericho.config import validate_settings

    shared = "S" * 48
    problems = validate_settings(replace(settings, api_token=shared, telegram_bridge_secret=shared))
    assert any("must be different" in problem for problem in problems)


def test_configured_bridge_without_allowlist_fails_closed_in_production(settings):
    from jericho.config import validate_settings

    open_bot = replace(settings, telegram_allowed_chat_ids=[], telegram_owner_chat_ids=[])
    # Production (non-loopback) treats an open bot as a fatal error.
    problems = validate_settings(open_bot, production=True)
    assert any("must be set when the Telegram bridge is configured" in problem for problem in problems)
    assert any(
        not problem.startswith("warning:") and "Telegram bridge is configured" in problem
        for problem in problems
    )
    # On a loopback dev bind it degrades to an advisory warning.
    dev_problems = validate_settings(open_bot, production=False)
    assert any(problem.startswith("warning:") and "Telegram bridge" in problem for problem in dev_problems)


def test_owner_chat_ids_satisfy_the_allowlist_requirement(settings):
    from jericho.config import validate_settings

    with_owner = replace(settings, telegram_allowed_chat_ids=[], telegram_owner_chat_ids=[5001])
    problems = validate_settings(with_owner, production=True)
    assert not any("Telegram bridge is configured" in problem for problem in problems)
    assert with_owner.telegram_effective_allowed_chat_ids == [5001]


def test_oversized_content_length_is_rejected_before_authentication(settings):
    from jericho.server import _max_request_body_bytes, create_app

    limited = replace(settings, max_upload_bytes=1024, max_extracted_text_chars=1000)
    app = create_app(limited)
    limit = _max_request_body_bytes(limited)
    with TestClient(app) as client:
        response = client.post(
            "/api/ingest",
            content=b"x",
            headers={"Content-Length": str(limit + 1)},
        )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


@pytest.mark.asyncio
async def test_chunked_request_body_is_counted_without_content_length(settings):
    from jericho.server import _max_request_body_bytes, create_app

    limited = replace(settings, max_upload_bytes=1024, max_extracted_text_chars=1000)
    app = create_app(limited)
    limit = _max_request_body_bytes(limited)

    async def oversized_stream():
        yield b'{"content":"'
        remaining = limit + 1
        chunk = b"x" * 65536
        while remaining > 0:
            piece = chunk[:remaining]
            remaining -= len(piece)
            yield piece

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/ingest",
                content=oversized_stream(),
                headers={"Authorization": f"Bearer {limited.api_token}"},
            )

    assert response.status_code == 413
    assert response.json()["detail"] == "Request body is too large"


def test_admin_api_rejects_malformed_and_non_object_json_with_400(settings):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}", "Content-Type": "application/json"}
    with TestClient(app) as client:
        malformed = client.post("/api/admin/lifecycle/deprecate", content=b"{", headers=headers)
        assert malformed.status_code == 400
        assert malformed.json()["detail"] == "Тело запроса должно быть корректным JSON"

        non_object = client.post("/api/admin/lifecycle/deprecate", json=[], headers=headers)
        assert non_object.status_code == 400
        assert non_object.json()["detail"] == "JSON-тело должно быть объектом"


@pytest.mark.parametrize(
    ("path", "payload", "detail"),
    [
        (
            "/api/admin/lifecycle/deprecate",
            # `ids` is required now; without it the route rejects on that first and
            # this case would stop testing scalar validation at all.
            {"user_id": "owner", "ids": ["ko_x"], "days_threshold": "not-a-number"},
            "days_threshold: нужно целое число",
        ),
        (
            "/api/admin/inbox/missing/classify",
            {"user_id": "owner", "importance": "NaN"},
            "importance: нужно конечное число",
        ),
        (
            "/api/admin/inbox/missing/classify",
            {"user_id": "owner", "importance": 1.5},
            "importance: значение от 0 до 1",
        ),
        (
            "/api/admin/inbox/missing/classify",
            {"user_id": "owner", "promote": "false"},
            "promote: нужно логическое значение",
        ),
        (
            "/api/admin/knowledge/missing/entity-links",
            {"user_id": "owner", "entity_id": "missing", "confidence": "NaN"},
            "confidence: нужно конечное число",
        ),
        (
            "/api/admin/knowledge/missing/entity-links",
            {"user_id": "owner", "entity_id": "missing", "status": "invented"},
            "status должен быть suggested, accepted или rejected",
        ),
    ],
)
def test_admin_api_rejects_invalid_scalar_types_with_400(settings, path, payload, detail):
    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == detail


def test_public_api_rejects_ambiguous_booleans_and_nonfinite_or_out_of_range_numbers(settings):
    """Control flags and ranking signals must not be coerced from hostile JSON values."""

    from jericho.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        invalid_chat = client.post(
            "/api/chat",
            json={"message": "hello", "force_knowledge": "false", "enable_tools": True},
            headers=headers,
        )
        assert invalid_chat.status_code == 400
        assert invalid_chat.json()["detail"] == "force_knowledge: нужно логическое значение"

        invalid_tools = client.post(
            "/api/chat",
            json={"message": "hello", "force_knowledge": False, "enable_tools": 0},
            headers=headers,
        )
        assert invalid_tools.status_code == 400
        assert invalid_tools.json()["detail"] == "enable_tools: нужно логическое значение"

        invalid_ingest = client.post(
            "/api/ingest",
            json={"content": "Project Alpha uses PostgreSQL 16.", "force_knowledge": "false"},
            headers=headers,
        )
        assert invalid_ingest.status_code == 400

        nan_feedback = client.post(
            "/api/feedback",
            content=b'{"target_id":"answer-1","feedback_type":"answer_usefulness","score":NaN}',
            headers={**headers, "Content-Type": "application/json"},
        )
        assert nan_feedback.status_code == 400
        assert nan_feedback.json()["detail"] == "score: нужно конечное число"

        out_of_range_feedback = client.post(
            "/api/feedback",
            json={"target_id": "answer-1", "feedback_type": "answer_usefulness", "score": 2},
            headers=headers,
        )
        assert out_of_range_feedback.status_code == 400

        invalid_weight = client.post(
            "/api/kg/relations",
            content=(
                b'{"source_entity_id":"source","target_entity_id":"target",'
                b'"relation_type":"related_to","weight":Infinity}'
            ),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert invalid_weight.status_code == 400

        invalid_confidence = client.post(
            "/api/kg/link",
            json={"knowledge_object_id": "ko", "entity_id": "entity", "confidence": -0.1},
            headers=headers,
        )
        assert invalid_confidence.status_code == 400

        assert app.state.storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
        assert app.state.storage.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


# --- small defects found by the 2026-07-27 audit ---------------------------


def test_lifecycle_stage_cannot_be_set_to_something_that_is_not_a_stage(storage):
    """A typo in a PATCH removed an object from governance without removing it from search.

    The DDL CHECK-constrains importance, quality_score and promotion_score but not
    this column, and `update_knowledge_fields` passed whatever arrived straight
    through. Both "Active" (wrong case) and "totally-bogus" persisted:
    `get_lifecycle_stats` then reported a stage nobody defined, and the object
    matched no lifecycle filter — so it fell out of every governance scan while
    still answering searches.
    """
    import pytest

    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="owner",
        source="test",
        source_ref=new_id("src"),
        raw_content="заметка",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="owner",
        raw_object_id=raw.id,
        content="заметка",
        title="Заметка",
        summary="заметка",
    )
    storage.store_knowledge_object(ko)

    for bogus in ("totally-bogus", "", "deleted_at"):
        with pytest.raises(ValueError, match="lifecycle_stage"):
            storage.update_knowledge_fields(ko.id, "owner", lifecycle_stage=bogus)

    # Case is normalised rather than rejected — "Active" is an obvious intent.
    updated = storage.update_knowledge_fields(ko.id, "owner", lifecycle_stage="Archived")
    assert updated is not None and updated["lifecycle_stage"] == "archived"
    assert storage.get_lifecycle_stats("owner").get("archived") == 1


def test_rescoring_a_pair_keeps_the_duplicate_heap_a_heap():
    """Filtering a heap with a comprehension is not a heap operation.

    `_PairCollector` rebuilt `_heap` as a plain list to drop a re-scored pair, then
    pushed onto it as if the invariant still held. `_heap[0]` stops being the
    minimum, so `heappushpop` evicts a pair that is not the weakest — strong
    near-duplicate candidates were discarded in favour of weaker ones.
    """
    import heapq

    from jericho.dedup import _PairCollector

    collector = _PairCollector({})
    for index in range(200):
        collector.add(f"a{index:03d}", f"b{index:03d}", 0.50 + index / 1000)
    # Re-score a batch: this is the path that used to corrupt the ordering.
    for index in range(0, 200, 7):
        collector.add(f"a{index:03d}", f"b{index:03d}", 0.50 + index / 1000 + 1e-6)

    heap = collector._heap  # noqa: SLF001 - the invariant is the whole point
    assert heap[0][0] == min(item[0] for item in heap)
    copy = list(heap)
    heapq.heapify(copy)
    assert copy[0] == heap[0]

    ranked = collector.ranked()
    assert ranked == sorted(ranked, key=lambda item: (-item[2], item[0], item[1]))


def test_precision_at_k_divides_by_k():
    """One hit in two results and one hit in ten are not 0.50 and 0.10 of the same thing.

    `min(k, len(retrieved[:k]))` collapses to the returned count, so the denominator
    was however many results came back. `compare_chunk_recall` compares two arms
    that legitimately return different numbers of results, so the metric moved for
    a reason unrelated to quality.
    """
    from jericho.eval import precision_at_k

    assert precision_at_k(["a", "b"], {"a"}, 10) == 0.1
    assert precision_at_k(["a"] + [f"x{i}" for i in range(9)], {"a"}, 10) == 0.1
    assert precision_at_k(["a", "b", "c"], {"a", "b", "c"}, 3) == 1.0
    assert precision_at_k([], {"a"}, 10) == 0.0


def test_a_forwarded_header_cannot_claim_to_be_loopback(settings):
    """`X-Forwarded-For` is client-supplied; it must not decide authentication.

    `_client_ip` walked the chain right-to-left for the first untrusted hop and,
    when EVERY hop was trusted, fell back to `chain[0]` — the leftmost entry, the
    one the client writes itself. That value gated the credential-less loopback
    owner path, so behind a trusted reverse proxy a remote request carrying
    `X-Forwarded-For: 127.0.0.1` became the owner without credentials.

    Two changes, deliberately redundant: the all-trusted fallback returns the
    observed peer instead of the asserted leftmost hop, AND the loopback decision
    reads the TCP peer directly. Mutation-checked one at a time — reverting either
    alone still passes, because the other still refuses; only reverting both
    reproduces the bypass. That is the point of defence in depth, and it is why
    this test asserts the property rather than one mechanism.
    """
    from dataclasses import replace as _replace

    from fastapi.testclient import TestClient

    from jericho.server import create_app

    trusting = _replace(
        settings,
        api_require_token_on_loopback=False,
        # Both, or the forwarded chain is never consulted and this test proves
        # nothing: `_client_ip` returns the peer immediately when the first is off.
        trust_proxy_headers=True,
        trusted_proxy_networks=["127.0.0.0/8", "10.0.0.0/8"],
    )
    app = create_app(trusting)
    # Sequential, not nested: both clients enter the lifespan, and the backend
    # process lease permits exactly one holder at a time.
    with TestClient(app, client=("10.0.0.9", 5555)) as remote:
        spoofed = remote.get("/api/me", headers={"X-Forwarded-For": "127.0.0.1"})
        assert spoofed.status_code == 401, "a forwarded header bought owner access"

    # A genuine loopback peer still gets the documented bypass.
    # A loopback Host too: `_guard_loopback_browser_request` refuses the
    # credential-less path for a request that arrived under any other name.
    with TestClient(app, client=("127.0.0.1", 5555), base_url="http://127.0.0.1:8000") as local:
        assert local.get("/api/me").status_code == 200


def test_tls_pair_is_validated_as_a_pair(settings, tmp_path):
    """Половина пары не служит; несуществующий файл уронил бы uvicorn на старте
    невнятным исключением — говорим это на языке конфигурации."""
    import dataclasses

    from jericho.config import validate_settings

    half = dataclasses.replace(settings, ssl_certfile="/x/cert.pem", ssl_keyfile="")
    assert any("must be set together" in item for item in validate_settings(half))

    missing = dataclasses.replace(
        settings, ssl_certfile=str(tmp_path / "c.pem"), ssl_keyfile=str(tmp_path / "k.pem")
    )
    assert any("missing file" in item for item in validate_settings(missing))

    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    valid = dataclasses.replace(settings, ssl_certfile=str(cert), ssl_keyfile=str(key))
    assert not [item for item in validate_settings(valid) if "SSL" in item]


def test_a_bare_http_bind_beyond_loopback_is_a_warning_not_an_error(settings):
    """Owner-токен и вся база ходят открытым текстом через проброшенный порт —
    об этом надо говорить. Но ошибкой это быть НЕ может: живой экземпляр
    владельца сегодня слушает 0.0.0.0 без TLS, и ошибка не дала бы ему встать."""
    import dataclasses

    from jericho.config import validate_settings

    exposed = dataclasses.replace(settings, api_host="0.0.0.0", api_token="T" * 40)
    problems = validate_settings(exposed)
    warning = next((item for item in problems if "cleartext" in item), None)
    assert warning is not None, "голый HTTP наружу остался незамеченным"
    assert warning.startswith("warning:"), "это предупреждение обязано не блокировать старт"
