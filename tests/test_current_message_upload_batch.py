"""Current Telegram uploads are reauthorized as one exact byte batch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from friday.file_delivery import (
    FileRecordUnavailable,
    authorize_current_message_upload_batch,
    read_current_message_upload_file,
    reauthorize_current_message_upload_batch,
)
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _actor() -> ActorContext:
    return ActorContext(
        user_id="alice",
        preset_key="admin",
        source="telegram-bridge",
        identity_id="42",
    )


def _store_upload(
    settings,
    storage,
    *,
    body: bytes,
    filename: str,
    uploaded_by: str = "alice",
) -> tuple[str, Path]:
    digest = hashlib.sha256(body).hexdigest()
    raw_id = new_id("raw")
    relative = f"alice/current-message/{raw_id}.blob"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id="alice",
            source="upload",
            source_ref=filename,
            raw_content=f"extracted {filename}",
            content_type="file",
            content_hash=digest,
            metadata_json={
                "filename": filename,
                "mime_type": "application/octet-stream",
                "sha256": digest,
                "size_bytes": len(body),
                "stored_path": relative,
                "uploaded_by": uploaded_by,
            },
        )
    )
    return raw_id, target


def _source_message(
    storage,
    *,
    uploaded_raw_ids: list[str] | None,
    attachment_raw_ids: list[str] | None = None,
    update_id: str = "77001",
    attachment_origin: str = "upload",
) -> tuple[str, str]:
    storage.ensure_user("alice", preset_key="admin")
    conversation = storage.create_conversation("alice")
    metadata: dict[str, object] = {
        "telegram_update_id": update_id,
        "attachment_origin": attachment_origin,
    }
    if uploaded_raw_ids is not None:
        metadata["conversation_uploaded_raw_ids"] = uploaded_raw_ids
    if attachment_raw_ids is not None:
        metadata["conversation_attachment_raw_ids"] = attachment_raw_ids
    source = storage.store_message(
        str(conversation["id"]),
        "alice",
        "user",
        "process these files",
        metadata=metadata,
    )
    return str(conversation["id"]), str(source["id"])


def _capture(
    settings,
    storage,
    *,
    conversation_id: str,
    source_message_id: str,
    raw_ids: list[str],
    update_id: str = "77001",
):
    return authorize_current_message_upload_batch(
        storage,
        Path(settings.files_dir),
        AuthorizationService(storage),
        _actor(),
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        telegram_update_id=update_id,
        uploaded_raw_ids=raw_ids,
    )


def test_batch_preserves_upload_order_and_never_admits_ambient_pointer(settings, storage) -> None:
    first, first_path = _store_upload(
        settings,
        storage,
        body=b"FIRST",
        filename="first.bin",
    )
    second, _ = _store_upload(
        settings,
        storage,
        body=b"SECOND",
        filename="second.bin",
    )
    ambient, _ = _store_upload(
        settings,
        storage,
        body=b"AMBIENT-MUST-NOT-ENTER",
        filename="ambient.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[second, first],
        attachment_raw_ids=[ambient, second, first],
    )

    captured = _capture(
        settings,
        storage,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        raw_ids=[second, first],
    )
    repeated = reauthorize_current_message_upload_batch(
        storage,
        Path(settings.files_dir),
        AuthorizationService(storage),
        _actor(),
        expected=captured.identity,
    )

    assert captured.identity.uploaded_raw_ids == (second, first)
    assert [item.content for item in captured.files] == [b"SECOND", b"FIRST"]
    assert repeated == captured
    assert ambient not in repr(captured)
    assert str(first_path.relative_to(settings.files_dir)) not in repr(captured)
    with pytest.raises(FileRecordUnavailable):
        _capture(
            settings,
            storage,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            raw_ids=[ambient, second, first],
        )


def test_shared_tenant_message_is_owned_by_person_while_raw_stays_tenant_scoped(
    settings,
    storage,
) -> None:
    person_id = "shared-owner-person"
    tenant_id = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant_id, preset_key="owner")
    storage.ensure_user(person_id, preset_key="admin")
    authorization = AuthorizationService(storage, shared_tenant=tenant_id)
    actor = authorization.actor_for_user(person_id, source="telegram-bridge", identity_id="5001")

    body = b"SHARED-TENANT-CURRENT-UPLOAD"
    digest = hashlib.sha256(body).hexdigest()
    raw_id = new_id("raw")
    relative = f"shared/current-message/{raw_id}.bin"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=tenant_id,
            source="upload",
            source_ref="telegram-update:77101",
            raw_content="[engineer-input: opaque current upload]",
            content_type="file",
            content_hash=digest,
            metadata_json={
                "filename": "shared.bin",
                "mime_type": "application/octet-stream",
                "sha256": digest,
                "size_bytes": len(body),
                "stored_path": relative,
                "uploaded_by": person_id,
            },
        )
    )
    conversation = storage.create_conversation(person_id, mode="engineer")
    source = storage.store_message(
        str(conversation["id"]),
        person_id,
        "user",
        "process current upload",
        metadata={
            "telegram_update_id": "77101",
            "conversation_uploaded_raw_ids": [raw_id],
        },
    )

    captured = authorize_current_message_upload_batch(
        storage,
        Path(settings.files_dir),
        authorization,
        actor,
        conversation_id=str(conversation["id"]),
        source_message_id=str(source["id"]),
        telegram_update_id="77101",
        uploaded_raw_ids=[raw_id],
    )

    assert captured.identity.uploaded_raw_ids == (raw_id,)
    assert [item.content for item in captured.files] == [body]


@pytest.mark.parametrize("origin", ("reply_reference", "restored", "replay"))
def test_attachment_pointer_without_current_upload_never_authorizes(
    settings,
    storage,
    origin: str,
) -> None:
    raw_id, _ = _store_upload(
        settings,
        storage,
        body=b"POINTER-ONLY",
        filename="pointer.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=None,
        attachment_raw_ids=[raw_id],
        attachment_origin=origin,
    )

    with pytest.raises(FileRecordUnavailable):
        _capture(
            settings,
            storage,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            raw_ids=[raw_id],
        )


def test_duplicate_or_duplicate_key_upload_list_fails_closed(settings, storage) -> None:
    raw_id, _ = _store_upload(
        settings,
        storage,
        body=b"DUPLICATE",
        filename="duplicate.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[raw_id, raw_id],
    )
    with pytest.raises(FileRecordUnavailable):
        _capture(
            settings,
            storage,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            raw_ids=[raw_id],
        )

    storage.execute(
        "UPDATE messages SET metadata_json=? WHERE id=?",
        (
            (
                '{"telegram_update_id":"77001",'
                f'"conversation_uploaded_raw_ids":["{raw_id}"],'
                f'"conversation_uploaded_raw_ids":["{raw_id}"]}}'
            ),
            source_message_id,
        ),
    )
    storage.commit()
    with pytest.raises(FileRecordUnavailable):
        _capture(
            settings,
            storage,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            raw_ids=[raw_id],
        )


@pytest.mark.parametrize("failure", ("missing", "deleted", "unowned", "quarantined"))
def test_missing_deleted_unowned_or_quarantined_raw_fails_closed(
    settings,
    storage,
    failure: str,
) -> None:
    storage.ensure_user("alice", preset_key="admin")
    if failure == "missing":
        raw_id = "raw_missing_current_upload"
    else:
        raw_id, _ = _store_upload(
            settings,
            storage,
            body=failure.encode("ascii"),
            filename=f"{failure}.bin",
            uploaded_by="bob" if failure == "unowned" else "alice",
        )
    if failure == "deleted":
        storage.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-27T00:00:00Z' WHERE id=?",
            (raw_id,),
        )
        storage.commit()
    elif failure == "quarantined":
        entity = Entity(
            id=new_id("ent"),
            user_id="alice",
            name="private upload dependency",
            entity_type=EntityType.EVENT,
        )
        storage.create_entity(entity)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw_id,
            content="private upload dependency",
            content_type="text",
            title="private",
        )
        storage.store_knowledge_object(knowledge)
        storage.link_knowledge_entity(
            "alice",
            knowledge.id,
            entity.id,
            status="accepted",
        )
        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO private_entity_owners(
                           entity_id, person_id, privacy_kind, created_at)
                       VALUES(?, 'bob', 'reminder', '2026-08-27T00:00:00Z')""",
                (entity.id,),
            )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[raw_id],
    )

    with pytest.raises(FileRecordUnavailable):
        _capture(
            settings,
            storage,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            raw_ids=[raw_id],
        )


@pytest.mark.parametrize("drift", ("source", "update", "list", "content"))
def test_reauthorization_rejects_source_update_list_or_content_identity_drift(
    settings,
    storage,
    drift: str,
) -> None:
    first, first_path = _store_upload(
        settings,
        storage,
        body=b"ORIGINAL-FIRST",
        filename="first.bin",
    )
    second, _ = _store_upload(
        settings,
        storage,
        body=b"ORIGINAL-SECOND",
        filename="second.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[first, second],
        attachment_raw_ids=[first, second],
    )
    captured = _capture(
        settings,
        storage,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        raw_ids=[first, second],
    )

    if drift in {"source", "update", "list"}:
        row = storage.execute(
            "SELECT metadata_json FROM messages WHERE id=?",
            (source_message_id,),
        ).fetchone()
        metadata = json.loads(str(row["metadata_json"]))
        if drift == "source":
            metadata["unrelated_source_marker"] = "changed"
        elif drift == "update":
            metadata["telegram_update_id"] = "77002"
        else:
            metadata["conversation_uploaded_raw_ids"] = [second, first]
        storage.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), source_message_id),
        )
    else:
        changed = b"CHANGED-FIRST"
        digest = hashlib.sha256(changed).hexdigest()
        first_path.write_bytes(changed)
        row = storage.execute(
            "SELECT metadata_json FROM raw_objects WHERE id=?",
            (first,),
        ).fetchone()
        metadata = json.loads(str(row["metadata_json"]))
        metadata.update({"sha256": digest, "size_bytes": len(changed)})
        storage.execute(
            "UPDATE raw_objects SET content_hash=?, metadata_json=? WHERE id=?",
            (digest, json.dumps(metadata, sort_keys=True), first),
        )
    storage.commit()

    with pytest.raises(FileRecordUnavailable):
        reauthorize_current_message_upload_batch(
            storage,
            Path(settings.files_dir),
            AuthorizationService(storage),
            _actor(),
            expected=captured.identity,
        )


@pytest.mark.parametrize("revocation", ("files.read", "inactive", "not_telegram"))
def test_fresh_actor_and_files_read_are_required_on_every_batch(
    settings,
    storage,
    revocation: str,
) -> None:
    raw_id, _ = _store_upload(
        settings,
        storage,
        body=b"AUTHORIZED-ONCE",
        filename="once.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[raw_id],
    )
    captured = _capture(
        settings,
        storage,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        raw_ids=[raw_id],
    )
    authorization = AuthorizationService(storage)
    actor = _actor()
    if revocation == "files.read":
        storage.set_permission_override("alice", "files.read", "deny")
    elif revocation == "inactive":
        storage.update_user("alice", status="disabled")
    else:
        actor = ActorContext(user_id="alice", preset_key="admin", source="api-token")

    with pytest.raises(FileRecordUnavailable):
        reauthorize_current_message_upload_batch(
            storage,
            Path(settings.files_dir),
            authorization,
            actor,
            expected=captured.identity,
        )


def test_single_file_restart_reader_remains_compatible(settings, storage) -> None:
    raw_id, _ = _store_upload(
        settings,
        storage,
        body=b"SINGLE-COMPATIBILITY",
        filename="single.bin",
    )
    conversation_id, source_message_id = _source_message(
        storage,
        uploaded_raw_ids=[raw_id],
    )

    stored = read_current_message_upload_file(
        storage,
        Path(settings.files_dir),
        raw_id,
        "alice",
        person_id="alice",
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )

    assert stored.raw_id == raw_id
    assert stored.content == b"SINGLE-COMPATIBILITY"
