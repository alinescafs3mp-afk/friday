"""Real SQLite/file-store continuity, without a model or an untrusted process."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import zipfile

import pytest

from friday.generated_files import persist_generated_response_files
from friday.organs.coding import static_turn
from friday.organs.coding.create import observe_coding_create
from friday.organs.coding.result_archive import observe_coding_result_archive
from friday.organs.coding.revision import CodingRevisionUnavailable, load_coding_revision
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.organs.coding.workspace_io import publish_members, source_revision_sha256
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.storage import init_storage

OWNER = LEGACY_OWNER_USER_ID


def _actor() -> ActorContext:
    return ActorContext(OWNER, "owner", "telegram-bridge", identity_id="5001", telegram_chat_id="5001")


def _boundary(tmp_path):
    return default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home/data/state"),
        worker_root=str(tmp_path / "worker"),
    )


def _turn(
    storage, tmp_path, *, message="создай проект python example", conversation_id=None, attachments=None
):
    return static_turn.handle_coding_static_turn(
        storage=storage,
        user_id=OWNER,
        actor=_actor(),
        message=message,
        conversation_id=conversation_id,
        attachments=attachments,
        worker_boundary=_boundary(tmp_path),
    )


def _publish(storage, settings, response):
    return persist_generated_response_files(
        storage,
        settings.files_dir,
        response,
        tenant_id=OWNER,
        person_id=OWNER,
        max_bytes=settings.max_upload_bytes,
    )


def _load(storage, settings, response, **kwargs):
    args = dict(
        person_id=OWNER,
        tenant_id=OWNER,
        conversation_id=response["conversation_id"],
        message_id=response["message_id"],
        revision_sha256=response["context"]["coding_source_revision"]["revision_sha256"],
    )
    args.update(kwargs)
    return load_coding_revision(storage, settings.files_dir, **args)


def _members(response):
    payload = base64.b64decode(response["files"][0]["content_base64"], validate=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _metadata(storage, response):
    row = storage.get_message(response["message_id"], OWNER)
    return json.loads(row["metadata_json"])


def _set_metadata(storage, response, metadata):
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?", (json.dumps(metadata), response["message_id"])
        )


def test_source_card_and_revision_use_the_packed_snapshot(storage, tmp_path, monkeypatch):
    original = static_turn.observe_coding_result_archive

    def drift(**kwargs):
        result = original(**kwargs)
        (kwargs["workspace"] / "main.py").write_bytes(b"different after packing")
        return result

    monkeypatch.setattr(static_turn, "observe_coding_result_archive", drift)
    response = _turn(storage, tmp_path)
    source = response["context"]["coding_source_revision"]
    digests = {name: hashlib.sha256(body).hexdigest() for name, body in _members(response).items()}
    assert source["members"] == digests
    assert source["revision_sha256"] == source_revision_sha256(digests)
    assert _metadata(storage, response)["coding_source_revision"] == source


def test_created_sources_drifting_before_pack_cannot_be_a_success(tmp_path, monkeypatch):
    original = static_turn.observe_coding_result_archive

    def drift(**kwargs):
        (kwargs["workspace"] / "main.py").write_bytes(b"not the prepared source")
        return original(**kwargs)

    monkeypatch.setattr(static_turn, "observe_coding_result_archive", drift)
    response = _turn(None, tmp_path)
    assert response["files"] == []
    assert response["context"]["coding_result_archive"] == "blocked"
    assert "coding_source_revision" not in response["context"]
    assert "Выдача исходников заблокирована" in response["message"]


def test_revision_is_not_available_before_final_file_publication(storage, settings, tmp_path):
    response = _turn(storage, tmp_path)
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response)
    response = _publish(storage, settings, response)
    assert dict(_load(storage, settings, response).members) == _members(response)

    # A copied descriptor is not a publication by another message, even when
    # the person, chat and all source hashes happen to be identical.
    copied = storage.store_message(
        response["conversation_id"],
        OWNER,
        "assistant",
        "unpublished copy",
        metadata=_metadata(storage, response),
    )
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response, message_id=copied["id"])


def test_revision_survives_database_reopen_and_workspace_loss(storage, settings, tmp_path):
    response = _publish(storage, settings, _turn(storage, tmp_path))
    expected = _members(response)
    shutil.rmtree(tmp_path / "worker")
    storage.close()
    reopened = init_storage(settings)
    try:
        actual = _load(reopened, settings, response)
        assert dict(actual.members) == expected
        assert actual.project_id == response["context"]["coding_source_revision"]["project_id"]
    finally:
        reopened.close()


def test_owner_can_restore_an_exact_revision_through_the_coding_turn(
    storage, settings, tmp_path, monkeypatch
):
    first = _publish(storage, settings, _turn(storage, tmp_path))
    expected = _members(first)
    revision = first["context"]["coding_source_revision"]["revision_sha256"]
    shutil.rmtree(tmp_path / "worker")
    monkeypatch.setattr(
        static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("restore executed code")
    )
    response = _turn(
        storage,
        tmp_path,
        message=f"restore {first['message_id']} {revision}",
        conversation_id=first["conversation_id"],
    )
    assert response["context"]["coding_revision_restore"] == "restored"
    assert response["context"]["coding_execution_attempted"] is False
    assert response["context"]["coding_upload_applied"] is False
    assert response["context"]["coding_source_revision"] == first["context"]["coding_source_revision"]
    assert _members(response) == expected
    response = _publish(storage, settings, response)
    assert dict(_load(storage, settings, response).members) == expected


@pytest.mark.parametrize(
    "override",
    [
        {"person_id": "somebody-else"},
        {"tenant_id": "somebody-else"},
        {"conversation_id": "conv_0000000000000000"},
        {"message_id": "msg_0000000000000000"},
        {"revision_sha256": "0" * 64},
        {"revision_sha256": "latest"},
        {"revision_sha256": "HEAD"},
        {"revision_sha256": "A" * 64},
    ],
)
def test_exact_revision_refuses_scope_changes_and_floating_selectors(storage, settings, tmp_path, override):
    response = _publish(storage, settings, _turn(storage, tmp_path))
    with pytest.raises(CodingRevisionUnavailable, match="^revision unavailable$"):
        _load(storage, settings, response, **override)


def test_exact_revision_refuses_another_coding_chat_of_the_same_owner(storage, settings, tmp_path):
    response = _publish(storage, settings, _turn(storage, tmp_path))
    other = storage.create_conversation(OWNER, "other", mode="coding")
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response, conversation_id=other["id"])


@pytest.mark.parametrize(
    "damage", ["record", "member_hash", "carrier_hash", "descriptor", "duplicate_json", "oversize"]
)
def test_corrupt_revision_metadata_never_falls_back_to_a_workspace(storage, settings, tmp_path, damage):
    response = _publish(storage, settings, _turn(storage, tmp_path))
    metadata = _metadata(storage, response)
    if damage == "record":
        del metadata["coding_source_revision"]
    elif damage == "member_hash":
        metadata["coding_source_revision"]["members"]["main.py"] = "0" * 64
    elif damage == "carrier_hash":
        metadata["coding_source_revision"]["carrier_sha256"] = "0" * 64
    elif damage == "descriptor":
        metadata["generated_files"][0]["sha256"] = "0" * 64
    elif damage == "oversize":
        metadata["padding"] = "x" * (1024 * 1024)
    _set_metadata(storage, response, metadata)
    if damage == "duplicate_json":
        raw = json.dumps(metadata)
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE messages SET metadata_json=? WHERE id=?",
                ('{"interaction_mode":"other",' + raw[1:], response["message_id"]),
            )
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response)


@pytest.mark.parametrize("damage", ["missing", "changed", "deleted", "private"])
def test_missing_or_revoked_durable_bytes_do_not_use_inline_or_working_copies(
    storage, settings, tmp_path, damage
):
    response = _publish(storage, settings, _turn(storage, tmp_path))
    raw_id = response["files"][0]["id"]
    row = storage.get_raw_object(raw_id, OWNER)
    metadata = json.loads(row["metadata_json"])
    path = settings.files_dir / metadata["stored_path"]
    if damage == "missing":
        path.unlink()
    elif damage == "changed":
        path.write_bytes(b"changed")
    elif damage == "deleted":
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?", (raw_id,))
    else:
        metadata["generated_for"] = "someone-else"
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET metadata_json=? WHERE id=?", (json.dumps(metadata), raw_id))
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response)


@pytest.mark.parametrize(
    "message",
    [
        "restore latest",
        "restore",
        "восстанови HEAD",
        "restore msg_0000000000000000 " + "0" * 64 + " run",
        "restore ../../file " + "0" * 64,
    ],
)
def test_invalid_restore_request_never_writes_or_executes(storage, tmp_path, message):
    response = _turn(storage, tmp_path, message=message)
    assert response["files"] == []
    assert response["context"]["coding_revision_restore"] == "blocked"
    assert not (tmp_path / "worker").exists()


def test_restore_cannot_mix_a_stored_revision_with_new_uploads(storage, settings, tmp_path):
    first = _publish(storage, settings, _turn(storage, tmp_path))
    revision = first["context"]["coding_source_revision"]["revision_sha256"]
    response = _turn(
        storage,
        tmp_path,
        message=f"restore {first['message_id']} {revision}",
        conversation_id=first["conversation_id"],
        attachments=[{"filename": "extra.py", "size": 1}],
    )
    assert response["files"] == []
    assert response["context"]["coding_revision_restore"] == "blocked"


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink"])
def test_create_never_overwrites_preexisting_files_or_aliases(tmp_path, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    original = tmp_path / "original"
    original.write_bytes(b"owner file")
    target = root / "README.md"
    if kind == "regular":
        target.write_bytes(b"owner file")
    elif kind == "symlink":
        target.symlink_to(original)
    else:
        os.link(original, target)
    result = observe_coding_create(
        turn_id="turn.create",
        project_id="project.create",
        message="создай проект python",
        workspace=root,
        worker_admitted=True,
        has_members=False,
    )
    assert result.state.value == "blocked"
    assert target.read_bytes() == b"owner file"
    assert original.read_bytes() == b"owner file"
    assert not (root / "main.py").exists()


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "traversal"])
def test_revision_archive_checks_exact_members_before_writes(storage, settings, tmp_path, kind):
    response = _turn(storage, tmp_path)
    members = _members(response)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, body in members.items():
            info = zipfile.ZipInfo(name)
            if name == "main.py" and kind == "symlink":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if name == "main.py" and kind == "traversal":
                info.filename = "../main.py"
            archive.writestr(info, body)
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("main.py", members["main.py"])
    payload = stream.getvalue()
    response["files"][0]["content_base64"] = base64.b64encode(payload).decode()
    metadata = _metadata(storage, response)
    metadata["coding_source_revision"]["carrier_sha256"] = hashlib.sha256(payload).hexdigest()
    _set_metadata(storage, response, metadata)
    response = _publish(storage, settings, response)
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, settings, response)


def test_empty_package_member_survives_exact_revision_loading(storage, settings, tmp_path):
    root = tmp_path / "input"
    publish_members(root, [("pkg/__init__.py", b""), ("pkg/main.py", b"print(42)\n")])
    result = observe_coding_result_archive(
        turn_id="turn.export", workspace=root, export_path=tmp_path / "out", ready=True
    )
    from friday.organs.coding.revision import revision_record

    record = revision_record("project.example", result)
    conversation = storage.create_conversation(OWNER, "archive", mode="coding")
    assistant = storage.store_message(
        conversation["id"],
        OWNER,
        "assistant",
        "source",
        metadata={"interaction_mode": "coding", "coding_source_revision": record},
    )
    response = _publish(
        storage,
        settings,
        {
            "conversation_id": conversation["id"],
            "message_id": assistant["id"],
            "files": list(result.files),
            "context": {"coding_source_revision": record},
        },
    )
    assert dict(_load(storage, settings, response).members)["pkg/__init__.py"] == b""


@pytest.mark.parametrize("path", ["../outside", "a/../outside", "/outside", "a//b", "a\\b", "", "a\x00b"])
def test_shared_writer_rejects_non_admitted_paths_without_creating_a_root(tmp_path, path):
    root = tmp_path / "invalid"
    with pytest.raises(ValueError):
        publish_members(root, [(path, b"body")])
    assert not root.exists()


def test_single_nested_file_keeps_its_path_when_restored(storage, settings, tmp_path):
    from friday.organs.coding.revision import revision_record

    root = tmp_path / "single"
    publish_members(root, [("src/main.py", b"print(42)\n")])
    result = observe_coding_result_archive(
        turn_id="turn.single", workspace=root, export_path=tmp_path / "out", ready=True
    )
    assert result.state.value == "file"
    record = revision_record("project.single", result)
    conversation = storage.create_conversation(OWNER, "single", mode="coding")
    assistant = storage.store_message(
        conversation["id"],
        OWNER,
        "assistant",
        "source",
        metadata={"interaction_mode": "coding", "coding_source_revision": record},
    )
    response = _publish(
        storage,
        settings,
        {
            "conversation_id": conversation["id"],
            "message_id": assistant["id"],
            "files": list(result.files),
            "context": {"coding_source_revision": record},
        },
    )
    assert dict(_load(storage, settings, response).members) == {"src/main.py": b"print(42)\n"}
