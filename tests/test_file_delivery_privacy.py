"""File egress revalidates quarantine at the byte-read boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _store_linked_file(
    settings,
    storage,
    user_id: str,
    *,
    filename: str,
    body: bytes,
    received_at: str = "2026-08-05T10:00:00+00:00",
) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{user_id}/{digest[:2]}/{digest}.bin"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=filename,
        raw_content="synthetic extracted text",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "stored_path": relative,
            "mime_type": "application/octet-stream",
            "size_bytes": len(body),
            "sha256": digest,
        },
        received_at=received_at,
        created_at=received_at,
    )
    storage.store_raw_object(raw)
    entity = Entity(
        id=new_id("ent"),
        user_id=user_id,
        name=f"Private dependency for {filename}",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(entity)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content="synthetic extracted text",
        content_type="text",
        title="Synthetic file",
    )
    storage.store_knowledge_object(knowledge)
    storage.link_knowledge_entity(user_id, knowledge.id, entity.id, status="accepted")
    return raw.id, entity.id


def _quarantine(storage, entity_id: str) -> None:
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, 'bob', 'reminder', '2026-08-06T00:00:00Z')""",
            (entity_id,),
        )


def _kernel(settings, storage) -> ExecutionKernel:
    from friday.ingestion import IngestionPipeline

    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), IngestionPipeline(settings, storage, graph))
    return kernel


@pytest.mark.anyio
async def test_archive_revalidates_stale_rows_before_reading_bytes(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A file quarantined after the day listing cannot enter the returned ZIP."""

    import friday.execution_kernel as kernel_module

    storage.ensure_user("alice", preset_key="admin")
    private_body = b"ARCHIVE-TOCTOU-PRIVATE-BYTES"
    private_name = "ARCHIVE-TOCTOU-PRIVATE-NAME.bin"
    raw_id, entity_id = _store_linked_file(
        settings,
        storage,
        "alice",
        filename=private_name,
        body=private_body,
    )
    original = kernel_module._pack_authorized_archive

    def quarantine_after_listing(*args, **kwargs):
        _quarantine(storage, entity_id)
        assert storage.get_raw_object(raw_id, "alice") is None
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel_module, "_pack_authorized_archive", quarantine_after_listing)
    result = await _kernel(settings, storage).execute(
        "collect_files",
        {"days": ["2026-08-05"]},
        actor=ActorContext(user_id="alice", preset_key="admin", source="test"),
    )

    encoded = json.dumps(result.data, ensure_ascii=False)
    assert result.success is True
    assert result.attachment is None
    assert result.data == {
        "collected": False,
        "reason": "не удалось собрать архив",
        "days": ["2026-08-05"],
        "found": 0,
    }
    assert private_name not in encoded
    assert private_body.decode() not in encoded


def test_user_download_revalidates_immediately_before_atomic_read(settings, monkeypatch) -> None:
    """A quarantine committed after route entry makes the download look missing."""

    import friday.api.files as files_api
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        raw_id, entity_id = _store_linked_file(
            settings,
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            filename="USER-PRIVATE-NAME.bin",
            body=b"USER-PRIVATE-BYTES",
        )
        listed = client.get("/api/files", headers=headers)
        assert raw_id in {item["id"] for item in listed.json()["items"]}
        original = files_api.read_authorized_file

        def quarantine_then_read(*args, **kwargs):
            _quarantine(app.state.storage, entity_id)
            return original(*args, **kwargs)

        monkeypatch.setattr(files_api, "read_authorized_file", quarantine_then_read)
        response = client.get(f"/api/files/{raw_id}", headers=headers)

        assert response.status_code == 404
        assert b"USER-PRIVATE-BYTES" not in response.content
        assert "USER-PRIVATE-NAME" not in response.text


def test_admin_download_revalidates_immediately_before_atomic_read(settings, monkeypatch) -> None:
    """The privileged route has the same byte boundary and no deferred path."""

    import friday.admin_api._files as files_admin
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        raw_id, entity_id = _store_linked_file(
            settings,
            app.state.storage,
            LEGACY_OWNER_USER_ID,
            filename="ADMIN-PRIVATE-NAME.bin",
            body=b"ADMIN-PRIVATE-BYTES",
        )
        listed = client.get(
            "/api/admin/files",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )
        assert raw_id in {item["id"] for item in listed.json()["items"]}
        original = files_admin.read_authorized_file

        def quarantine_then_read(*args, **kwargs):
            _quarantine(app.state.storage, entity_id)
            return original(*args, **kwargs)

        monkeypatch.setattr(files_admin, "read_authorized_file", quarantine_then_read)
        response = client.get(
            f"/api/admin/files/{raw_id}/download",
            params={"user_id": LEGACY_OWNER_USER_ID},
            headers=headers,
        )

        assert response.status_code == 404
        assert b"ADMIN-PRIVATE-BYTES" not in response.content
        assert "ADMIN-PRIVATE-NAME" not in response.text
