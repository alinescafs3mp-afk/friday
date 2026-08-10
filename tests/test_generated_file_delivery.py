"""Generated outputs survive refresh without becoming shared source documents."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from friday.api.projections import public_chat_ingestion
from friday.generated_files import GeneratedFilePersistenceError, persist_generated_response_files
from friday.permissions import LEGACY_OWNER_USER_ID


def test_generated_file_has_exact_durable_download_and_history_handle(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = b"PK\x03\x04synthetic-xlsx\x00exact-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    seen: dict[str, str] = {}

    async def generated(user_id, _message, *, actor, **_kwargs):
        conversation = app.state.storage.create_conversation(actor.own_id, "generated output")
        assistant = app.state.storage.store_message(
            conversation["id"],
            actor.own_id,
            "assistant",
            "Таблица готова.",
            metadata={"tools_used": ["make_file"]},
        )
        seen["conversation_id"] = conversation["id"]
        return {
            "conversation_id": conversation["id"],
            "message_id": assistant["id"],
            "message": "Таблица готова.",
            "files": [
                {
                    "kind": "document",
                    "filename": "Люди.xlsx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                }
            ],
        }

    with TestClient(app) as client:
        app.state.agent.chat = generated
        request = {"message": "Сделай таблицу", "source_ref": "synthetic-generated-file:1"}
        response = client.post("/api/chat", headers=headers, json=request)
        assert response.status_code == 200, response.text
        file = response.json()["files"][0]
        assert base64.b64decode(file["content_base64"], validate=True) == payload
        assert file["filename"] == "Люди.xlsx"
        assert file["size_bytes"] == len(payload)
        assert file["sha256"] == digest
        assert file["download_url"] == f"/api/files/{file['id']}"

        raw = app.state.storage.get_raw_object(file["id"], LEGACY_OWNER_USER_ID)
        assert raw is not None and raw["content_type"] == "generated_file"
        metadata = json.loads(raw["metadata_json"])
        assert metadata["generated_for"] == LEGACY_OWNER_USER_ID
        assert metadata["generated_tenant"] == LEGACY_OWNER_USER_ID
        assert metadata["size_bytes"] == len(payload)
        stored = settings.files_dir / metadata["stored_path"]
        assert stored.read_bytes() == payload
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600

        downloaded = client.get(file["download_url"], headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == payload
        assert downloaded.headers["content-type"] == file["mime_type"]
        assert "Люди.xlsx" in unquote(downloaded.headers["content-disposition"])

        # A page refresh reloads conversation messages. It gets the same stable
        # descriptor, not another multi-megabyte base64 copy.
        history = client.get(
            f"/api/conversations/{seen['conversation_id']}/messages",
            headers=headers,
        )
        assert history.status_code == 200, history.text
        history_file = history.json()["items"][0]["files"][0]
        assert history_file == {
            key: value for key, value in file.items() if key not in {"content_base64", "kind"}
        }

        replay = client.post("/api/chat", headers=headers, json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["files"][0] == file
        generated_rows = app.state.storage.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
        ).fetchone()[0]
        assert generated_rows == 1

        with app.state.storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET deleted_at='2026-08-10T00:00:00Z' WHERE id=?",
                (file["id"],),
            )
        revoked_replay = client.post("/api/chat", headers=headers, json=request)
        assert revoked_replay.status_code == 200, revoked_replay.text
        assert revoked_replay.json()["files"] == []


def test_generated_download_is_person_scoped_inside_one_tenant(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    other_person = "telegram:user:synthetic-other"
    payload = b"synthetic private generated bytes"

    with TestClient(app) as client:
        app.state.storage.ensure_user(other_person, source="telegram", preset_key="user")
        conversation = app.state.storage.create_conversation(other_person, "other person's chat")
        assistant = app.state.storage.store_message(conversation["id"], other_person, "assistant", "Готово.")
        result = persist_generated_response_files(
            app.state.storage,
            settings.files_dir,
            {
                "message_id": assistant["id"],
                "files": [
                    {
                        "filename": "private.pdf",
                        "mime_type": "application/pdf",
                        "content_base64": base64.b64encode(payload).decode("ascii"),
                    }
                ],
            },
            tenant_id=LEGACY_OWNER_USER_ID,
            person_id=other_person,
            max_bytes=settings.max_upload_bytes,
        )
        foreign_handle = result["files"][0]["id"]
        assert app.state.storage.get_raw_object(foreign_handle, other_person) is not None
        assert app.state.storage.get_raw_object(foreign_handle, LEGACY_OWNER_USER_ID) is None

        # Knowing another participant's opaque id is still insufficient.
        refused = client.get(f"/api/files/{foreign_handle}", headers=headers)
        assert refused.status_code == 404
        assert payload not in refused.content


def test_regenerate_persists_a_new_generated_file_before_caching(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = b"regenerated-pdf-exact-bytes"

    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "regenerate file")
        app.state.storage.store_message(
            conversation["id"], LEGACY_OWNER_USER_ID, "user", "Сделай PDF ещё раз"
        )

        async def generated(_user_id, _message, *, actor, **_kwargs):
            assistant = app.state.storage.store_message(
                conversation["id"], actor.own_id, "assistant", "PDF готов."
            )
            return {
                "conversation_id": conversation["id"],
                "message_id": assistant["id"],
                "message": "PDF готов.",
                "files": [
                    {
                        "filename": "Повтор.pdf",
                        "mime_type": "application/pdf",
                        "content_base64": base64.b64encode(payload).decode("ascii"),
                    }
                ],
            }

        app.state.agent.chat = generated
        first = client.post(
            "/api/me/regenerate",
            headers=headers,
            json={"conversation_id": conversation["id"]},
        )
        assert first.status_code == 200, first.text
        file = first.json()["files"][0]
        assert client.get(file["download_url"], headers=headers).content == payload

        replay = client.post(
            "/api/me/regenerate",
            headers=headers,
            json={"conversation_id": conversation["id"]},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["files"][0] == file


def test_a_generated_file_batch_is_all_or_nothing(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app):
        conversation = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "atomic files")
        assistant = app.state.storage.store_message(
            conversation["id"], LEGACY_OWNER_USER_ID, "assistant", "Файлы готовы."
        )

        with pytest.raises(GeneratedFilePersistenceError):
            persist_generated_response_files(
                app.state.storage,
                settings.files_dir,
                {
                    "message_id": assistant["id"],
                    "files": [
                        {
                            "filename": "valid.xlsx",
                            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            "content_base64": base64.b64encode(b"valid first payload").decode("ascii"),
                        },
                        {
                            "filename": "broken.pdf",
                            "mime_type": "application/pdf",
                            "content_base64": "not base64!",
                        },
                    ],
                },
                tenant_id=LEGACY_OWNER_USER_ID,
                person_id=LEGACY_OWNER_USER_ID,
                max_bytes=settings.max_upload_bytes,
            )

        count = app.state.storage.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
        ).fetchone()[0]
        assert count == 0
        assert not list(settings.files_dir.rglob("*.blob"))
        assert not list(settings.files_dir.rglob("generated"))
        stored = app.state.storage.execute(
            "SELECT metadata_json FROM messages WHERE id=?",
            (assistant["id"],),
        ).fetchone()
        assert json.loads(stored["metadata_json"] or "{}").get("generated_files") is None


def test_a_database_failure_rolls_back_every_generated_file(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app):
        conversation = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "atomic db failure")
        assistant = app.state.storage.store_message(
            conversation["id"], LEGACY_OWNER_USER_ID, "assistant", "Файлы готовы."
        )
        original = app.state.storage.store_raw_object
        calls = 0

        def fail_second(raw):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic second-row failure")
            return original(raw)

        monkeypatch.setattr(app.state.storage, "store_raw_object", fail_second)
        items = [
            {
                "filename": f"valid-{index}.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(f"payload-{index}".encode()).decode("ascii"),
            }
            for index in (1, 2)
        ]

        with pytest.raises(RuntimeError, match="second-row"):
            persist_generated_response_files(
                app.state.storage,
                settings.files_dir,
                {"message_id": assistant["id"], "files": items},
                tenant_id=LEGACY_OWNER_USER_ID,
                person_id=LEGACY_OWNER_USER_ID,
                max_bytes=settings.max_upload_bytes,
            )

        assert (
            app.state.storage.execute(
                "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
            ).fetchone()[0]
            == 0
        )
        assert not list(settings.files_dir.rglob("*.blob"))
        assert not list(settings.files_dir.rglob("generated"))


def test_a_filesystem_failure_rolls_back_every_generated_file(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.generated_files as generated_files
    from friday.server import create_app

    app = create_app(settings)
    original_fsync = generated_files._fsync_directory  # noqa: SLF001
    calls = 0

    def fail_second(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic second-file fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(generated_files, "_fsync_directory", fail_second)
    with TestClient(app):
        conversation = app.state.storage.create_conversation(LEGACY_OWNER_USER_ID, "atomic fs failure")
        assistant = app.state.storage.store_message(
            conversation["id"], LEGACY_OWNER_USER_ID, "assistant", "Файлы готовы."
        )
        items = [
            {
                "filename": f"valid-{index}.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(f"payload-{index}".encode()).decode("ascii"),
            }
            for index in (1, 2)
        ]

        with pytest.raises(OSError, match="second-file"):
            persist_generated_response_files(
                app.state.storage,
                settings.files_dir,
                {"message_id": assistant["id"], "files": items},
                tenant_id=LEGACY_OWNER_USER_ID,
                person_id=LEGACY_OWNER_USER_ID,
                max_bytes=settings.max_upload_bytes,
            )

        assert (
            app.state.storage.execute(
                "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
            ).fetchone()[0]
            == 0
        )
        assert not list(settings.files_dir.rglob("*.blob"))
        assert not list(settings.files_dir.rglob("generated"))


def test_a_legacy_inline_file_without_a_claimed_handle_remains_compatible(settings) -> None:
    from friday.server import create_app

    app = create_app(settings)
    payload = b"legacy inline generated bytes"
    with TestClient(app):
        projected = public_chat_ingestion(
            {
                "files": [
                    {
                        "filename": "legacy.pdf",
                        "mime_type": "application/pdf",
                        "content_base64": base64.b64encode(payload).decode("ascii"),
                    }
                ]
            },
            storage=app.state.storage,
            resource_user_id=LEGACY_OWNER_USER_ID,
            resource_owner_id=LEGACY_OWNER_USER_ID,
        )

    assert base64.b64decode(projected["files"][0]["content_base64"], validate=True) == payload
    assert "id" not in projected["files"][0]
