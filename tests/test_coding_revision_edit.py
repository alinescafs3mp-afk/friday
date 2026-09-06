"""Exact source edits through the real Coding turn and durable final publisher."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import zipfile

import pytest

from friday.generated_files import persist_generated_response_files
from friday.organs.coding import static_turn
from friday.organs.coding.revision import CodingRevisionUnavailable, load_coding_revision
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.storage import init_storage

OWNER = LEGACY_OWNER_USER_ID


def _turn(storage, tmp_path, message, conversation_id=None, attachments=None, boundary=None):
    boundary = boundary or default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home/data/state"),
        worker_root=str(tmp_path / "worker"),
    )
    return static_turn.handle_coding_static_turn(
        storage=storage,
        user_id=OWNER,
        actor=ActorContext(OWNER, "owner", "telegram-bridge", identity_id="5001", telegram_chat_id="5001"),
        message=message,
        conversation_id=conversation_id,
        attachments=attachments,
        worker_boundary=boundary,
    )


def _publish(storage, response):
    return persist_generated_response_files(
        storage,
        storage.settings.files_dir,
        response,
        tenant_id=OWNER,
        person_id=OWNER,
        max_bytes=storage.settings.max_upload_bytes,
    )


def _base(storage, tmp_path):
    return _publish(storage, _turn(storage, tmp_path, "создай проект python example"))


def _load(storage, response):
    return load_coding_revision(
        storage,
        storage.settings.files_dir,
        person_id=OWNER,
        tenant_id=OWNER,
        conversation_id=response["conversation_id"],
        message_id=response["message_id"],
        revision_sha256=response["context"]["coding_source_revision"]["revision_sha256"],
    )


def _request(response, edits):
    return (
        f"edit {response['message_id']} "
        f"{response['context']['coding_source_revision']['revision_sha256']}\n"
        + (edits if isinstance(edits, str) else json.dumps(edits, ensure_ascii=False))
    )


def test_edit_creates_a_real_new_revision_and_preserves_its_parent(storage, tmp_path, monkeypatch):
    first = _base(storage, tmp_path)
    original = _load(storage, first)
    before = dict(original.members)
    request = {
        "replace": {"main.py": "def add(a, b):\n    return a + b\n"},
        "add": {"notes/change.txt": "Сложение реализовано.\n"},
        "delete": ["README.md"],
    }
    monkeypatch.setattr(
        static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("edit executed code")
    )
    result = _turn(storage, tmp_path, _request(first, request), first["conversation_id"])
    assert result["context"].get("coding_revision_edit") == "applied"
    assert result["context"]["coding_upload_applied"] is True
    assert result["context"]["coding_execution_attempted"] is False
    assert result["context"]["coding_plan_gate"] == "modify"
    record = result["context"]["coding_source_revision"]
    assert record["project_id"] == original.project_id
    assert record["revision_sha256"] != original.revision_sha256
    assert result["context"]["coding_source_parent"] == {
        "message_id": first["message_id"],
        "revision_sha256": original.revision_sha256,
    }
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, result)
    result = _publish(storage, result)
    after = dict(_load(storage, result).members)
    assert after == {
        **{p: b for p, b in before.items() if p != "README.md"},
        "main.py": request["replace"]["main.py"].encode(),
        "notes/change.txt": request["add"]["notes/change.txt"].encode(),
    }
    assert dict(_load(storage, first).members) == before
    payload = base64.b64decode(result["files"][0]["content_base64"], validate=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert {p: archive.read(p) for p in archive.namelist()} == after
    metadata = json.loads(storage.get_message(result["message_id"], OWNER)["metadata_json"])
    assert metadata["coding_source_parent"] == result["context"]["coding_source_parent"]
    assert "def add" not in json.dumps(metadata)
    shutil.rmtree(tmp_path / "worker")
    settings = storage.settings
    storage.close()
    reopened = init_storage(settings)
    try:
        assert dict(_load(reopened, result).members) == after
        assert dict(_load(reopened, first).members) == before
        restore = f"restore {result['message_id']} {record['revision_sha256']}"
        restored = _publish(reopened, _turn(reopened, tmp_path, restore, first["conversation_id"]))
        assert restored["context"]["coding_revision_restore"] == "restored"
        assert dict(_load(reopened, restored).members) == after
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "edits",
    [
        {},
        {"replace": {}},
        [],
        {"unknown": {}},
        {"replace": []},
        {"add": []},
        {"delete": {}},
        {"delete": [123]},
        {"delete": [{}]},
        {"replace": {"main.py": None}},
        {"replace": {"main.py": 123}},
        {"replace": {"missing.py": "x"}},
        {"delete": ["missing.py"]},
        {"add": {"main.py": "x"}},
        {"replace": {"main.py": "x"}, "delete": ["main.py"]},
        {"delete": ["main.py", "main.py"]},
        {"add": {"../escape.py": "x"}},
        {"add": {"/escape.py": "x"}},
        {"add": {"C:\\escape.py": "x"}},
        {"add": {".env": "x"}},
        {"add": {".git/config": "x"}},
        {"add": {"__pycache__/entry.py": "x"}},
        {"add": {"cache.py": "x"}},
        {"add": {"Main.py": "x"}},
        {"add": {"main.py/child.py": "x"}},
        {"delete": ["main.py", "README.md", "test_main.py"]},
        {"add": {f"file_{i}.py": "x" for i in range(17)}},
        {"replace": {"main.py": "\ud800"}},
        '{"replace":{"main.py":"x","main.py":"y"}}',
        '{"replace":{"main.py":"x"},"replace":{"main.py":"y"}}',
        '{"replace":{"main.py":NaN}}',
        '{"replace":{"main.py":"x"}} trailing',
        "[]",
        "{",
    ],
)
def test_invalid_edit_is_not_partially_applied(storage, tmp_path, edits, monkeypatch):
    first = _base(storage, tmp_path)
    original = _load(storage, first)
    monkeypatch.setattr(
        static_turn, "publish_members", lambda *a, **kw: pytest.fail("invalid edit wrote files")
    )
    # Escaped surrogate is deliberately valid JSON text but not valid UTF-8 source.
    payload = edits if isinstance(edits, str) else json.dumps(edits)
    result = _turn(storage, tmp_path, _request(first, payload), first["conversation_id"])
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["context"]["coding_upload_applied"] is False
    assert result["files"] == []
    assert "coding_source_revision" not in result["context"]
    assert dict(_load(storage, first).members) == dict(original.members)


def test_unchanged_or_oversized_edit_does_not_write(storage, tmp_path, monkeypatch):
    from friday.organs.coding.modify import MAX_REVISION_EDIT_BYTES

    first = _base(storage, tmp_path)
    original = dict(_load(storage, first).members)
    monkeypatch.setattr(
        static_turn, "publish_members", lambda *a, **kw: pytest.fail("invalid edit wrote files")
    )
    for text in (
        original["main.py"].decode(),
        "x" * MAX_REVISION_EDIT_BYTES,
        "я" * (MAX_REVISION_EDIT_BYTES // 2),
    ):
        result = _turn(
            storage, tmp_path, _request(first, {"replace": {"main.py": text}}), first["conversation_id"]
        )
        assert result["context"]["coding_revision_edit"] == "blocked"
        assert result["files"] == []


@pytest.mark.parametrize(
    "fault", ["wrong_hash", "wrong_message", "wrong_chat", "attachments", "missing_payload"]
)
def test_edit_selection_never_falls_back_to_another_source(storage, tmp_path, fault, monkeypatch):
    first = _base(storage, tmp_path)
    message = _request(first, {"replace": {"main.py": "answer = 42\n"}})
    chat, attachments = first["conversation_id"], None
    if fault == "wrong_hash":
        message = message.replace(first["context"]["coding_source_revision"]["revision_sha256"], "0" * 64)
    elif fault == "wrong_message":
        message = message.replace(first["message_id"], "msg_0000000000000000")
    elif fault == "wrong_chat":
        chat = storage.create_conversation(OWNER, title="other", mode="coding")["id"]
    elif fault == "attachments":
        attachments = [{"filename": "elsewhere.py", "content_base64": "eA=="}]
    else:
        message = message.partition("\n")[0]
    monkeypatch.setattr(
        static_turn, "publish_members", lambda *a, **kw: pytest.fail("bad selection wrote files")
    )
    result = _turn(storage, tmp_path, message, chat, attachments)
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["files"] == []
    assert result["context"]["coding_execution_attempted"] is False


def test_output_drift_blocks_edit_publication_without_modifying_parent(storage, tmp_path, monkeypatch):
    first = _base(storage, tmp_path)
    parent = dict(_load(storage, first).members)
    observe = static_turn.observe_coding_result_archive

    def drift(**kwargs):
        (kwargs["workspace"] / "main.py").write_bytes(b"raced")
        return observe(**kwargs)

    monkeypatch.setattr(static_turn, "observe_coding_result_archive", drift)
    result = _turn(
        storage,
        tmp_path,
        _request(first, {"replace": {"main.py": "answer = 42\n"}}),
        first["conversation_id"],
    )
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["files"] == []
    assert "coding_source_revision" not in result["context"]
    assert dict(_load(storage, first).members) == parent


@pytest.mark.parametrize("operation", ["edit", "restore"])
def test_revocation_before_final_publish_rolls_back_new_artifact(storage, tmp_path, operation):
    from friday.generated_files import GeneratedFilePersistenceError

    first = _base(storage, tmp_path)
    message = _request(first, {"replace": {"main.py": "answer = 42\n"}})
    if operation == "restore":
        message = message.partition("\n")[0].replace("edit", "restore", 1)
    response = _turn(storage, tmp_path, message, first["conversation_id"])
    assert response["files"]
    with storage.transaction() as conn:
        prior = conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0]
        conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?", (first["files"][0]["id"],)
        )
    with pytest.raises(GeneratedFilePersistenceError, match="no longer authorized"):
        _publish(storage, response)
    with storage.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == prior
    metadata = json.loads(storage.get_message(response["message_id"], OWNER)["metadata_json"])
    assert "generated_files" not in metadata
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, response)


def test_edit_retries_are_deterministic_and_keep_empty_unchanged_members(storage, tmp_path):
    first = _base(storage, tmp_path)
    payload = {"add": {"lib/__init__.py": ""}, "replace": {"main.py": "answer = 42\n"}}
    one = _publish(storage, _turn(storage, tmp_path, _request(first, payload), first["conversation_id"]))
    two = _publish(storage, _turn(storage, tmp_path, _request(first, payload), first["conversation_id"]))
    assert one["message_id"] != two["message_id"]
    assert one["context"]["coding_source_revision"] == two["context"]["coding_source_revision"]
    assert dict(_load(storage, one).members)["lib/__init__.py"] == b""
    assert (
        hashlib.sha256(base64.b64decode(one["files"][0]["content_base64"])).hexdigest()
        == one["files"][0]["sha256"]
    )


@pytest.mark.parametrize("failure", ["write", "cancel", "denied_worker"])
def test_edit_failures_preserve_parent_and_do_not_publish_a_candidate(
    storage, tmp_path, monkeypatch, failure
):
    import os
    from dataclasses import replace

    from friday.organs.coding import workspace_io

    first = _base(storage, tmp_path)
    original = dict(_load(storage, first).members)
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home/data/state"),
        worker_root=str(tmp_path / "worker"),
    )
    message = _request(first, {"replace": {"main.py": "answer = 42\n"}})
    before_workspaces = set((tmp_path / "worker/work").iterdir())
    if failure == "denied_worker":
        boundary = replace(boundary, visible_paths=("/",))
        monkeypatch.setattr(
            static_turn, "publish_members", lambda *a, **kw: pytest.fail("denied worker wrote files")
        )
    else:
        original_write = os.write
        calls = 0

        def fail_midway(descriptor, body):
            nonlocal calls
            calls += 1
            if calls == 2:
                if failure == "cancel":
                    raise KeyboardInterrupt()
                raise OSError("synthetic disk failure")
            return original_write(descriptor, body)

        monkeypatch.setattr(workspace_io.os, "write", fail_midway)
    if failure == "cancel":
        with pytest.raises(KeyboardInterrupt):
            _turn(storage, tmp_path, message, first["conversation_id"], boundary=boundary)
    else:
        result = _turn(storage, tmp_path, message, first["conversation_id"], boundary=boundary)
        assert result["context"]["coding_revision_edit"] == "blocked"
        assert result["files"] == []
    assert dict(_load(storage, first).members) == original
    for folder in set((tmp_path / "worker/work").iterdir()) - before_workspaces:
        assert not list(folder.iterdir())


@pytest.mark.parametrize("damage", ["self", "wrong_project", "foreign_message", "extra_field", "not_mapping"])
def test_stored_parent_binding_cannot_be_substituted_at_publication(storage, tmp_path, damage):
    from friday.generated_files import GeneratedFilePersistenceError

    first = _base(storage, tmp_path)
    response = _turn(
        storage,
        tmp_path,
        _request(first, {"replace": {"main.py": "answer = 42\n"}}),
        first["conversation_id"],
    )
    metadata = json.loads(storage.get_message(response["message_id"], OWNER)["metadata_json"])
    if damage == "self":
        metadata["coding_source_parent"]["message_id"] = response["message_id"]
    elif damage == "wrong_project":
        metadata["coding_source_revision"]["project_id"] = "other-project"
    elif damage == "foreign_message":
        other = _base(storage, tmp_path)
        metadata["coding_source_parent"]["message_id"] = other["message_id"]
    elif damage == "extra_field":
        metadata["coding_source_parent"]["allow"] = True
    else:
        metadata["coding_source_parent"] = []
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?", (json.dumps(metadata), response["message_id"])
        )
    with pytest.raises(GeneratedFilePersistenceError):
        _publish(storage, response)
    with pytest.raises(CodingRevisionUnavailable):
        _load(storage, response)


def test_edit_body_is_never_interpreted_as_a_command(storage, tmp_path, monkeypatch):
    first = _base(storage, tmp_path)
    program = "# run npm make cargo pytest execute\nraise RuntimeError('never executed')\n"
    monkeypatch.setattr(
        static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("code body became a command")
    )
    response = _publish(
        storage,
        _turn(
            storage, tmp_path, _request(first, {"replace": {"main.py": program}}), first["conversation_id"]
        ),
    )
    assert response["context"]["coding_revision_edit"] == "applied"
    assert dict(_load(storage, response).members)["main.py"] == program.encode()
    assert response["context"]["coding_execution_attempted"] is False
