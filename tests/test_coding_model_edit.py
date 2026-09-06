"""Primary model proposals through the real Coding route and durable publisher."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.generated_files import persist_generated_response_files
from friday.orchestration.router import OrchestrationRouter
from friday.organs.coding import static_turn
from friday.organs.coding.revision import load_coding_revision
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationError
from friday.storage import init_storage

OWNER = LEGACY_OWNER_USER_ID
ACTOR = ActorContext(OWNER, "owner", "telegram-bridge", identity_id="5001", telegram_chat_id="5001")
NEW_SOURCE = "def add(a: int, b: int) -> int:\n    return a + b\n"


class Model:
    def __init__(self, response=None, effect=None):
        self.calls = []
        self.response = (
            response
            if response is not None
            else {
                "content": json.dumps({"replace": {"main.py": NEW_SOURCE}}),
                "finish_reason": "stop",
                "tool_calls": [],
            }
        )
        self.effect = effect

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.effect:
            await self.effect()
        return self.response


@pytest.fixture
def coding_boundary(tmp_path, monkeypatch):
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home/data/state"),
        worker_root=str(tmp_path / "worker"),
    )
    monkeypatch.setattr(static_turn, "default_coding_worker_boundary", lambda **kwargs: boundary)
    return boundary


def _publish(storage, response):
    return persist_generated_response_files(
        storage,
        storage.settings.files_dir,
        response,
        tenant_id=OWNER,
        person_id=OWNER,
        max_bytes=storage.settings.max_upload_bytes,
    )


def _base(storage):
    return _publish(
        storage,
        static_turn.handle_coding_static_turn(
            storage=storage,
            user_id=OWNER,
            actor=ACTOR,
            message="создай проект python example",
            conversation_id=None,
            attachments=None,
        ),
    )


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


def _request(base, task="Реализуй функцию add(a, b), которая возвращает сумму."):
    return (
        f"revise {base['message_id']} {base['context']['coding_source_revision']['revision_sha256']}\n{task}"
    )


async def _route(storage, model, message, conversation_id, **kwargs):
    legacy = SimpleNamespace(storage=storage, llm=model)
    router = OrchestrationRouter(legacy, SimpleNamespace(), mode="legacy")
    return await router.chat(
        OWNER,
        message,
        actor=kwargs.pop("actor", ACTOR),
        conversation_id=conversation_id,
        mode="coding",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_primary_model_task_publishes_a_new_exact_revision(storage, coding_boundary, tmp_path):
    base = _base(storage)
    original = _load(storage, base)
    model = Model()
    task = _request(base)
    deadline = time.monotonic() + 20
    response = await _route(storage, model, task, base["conversation_id"], turn_deadline=deadline)
    assert response["context"].get("coding_revision_edit") == "applied"
    assert len(model.calls) == 1
    messages, kwargs = model.calls[0]
    assert kwargs["absolute_deadline"] == deadline
    assert kwargs["allow_retries"] is False
    assert kwargs["require_full_context"] is True
    assert kwargs["enable_thinking"] is False
    assert kwargs["tools"] == [] and kwargs["tool_choice"] == "none"
    assert kwargs["priority"] == "foreground"
    assert messages[0]["role"] == "system"
    data = json.loads(messages[1]["content"])
    assert data["task"] == task.split("\n", 1)[1]
    assert data["files"] == {p: b.decode() for p, b in original.members}
    assert OWNER not in messages[1]["content"]
    assert base["message_id"] not in messages[1]["content"]
    assert str(tmp_path) not in messages[1]["content"]
    response = _publish(storage, response)
    after = _load(storage, response)
    assert dict(after.members)["main.py"] == NEW_SOURCE.encode()
    assert dict(_load(storage, base).members) == dict(original.members)
    assert response["context"]["coding_execution_attempted"] is False
    assert response["verified"] is False
    assert response["context"]["coding_model_edit"] == "prepared"
    assert response["context"]["coding_model_calls"] == 1
    records = storage.get_conversation_messages(base["conversation_id"], user_id=OWNER, limit=20)
    assert any(row["role"] == "user" and row["content"] == task for row in records)
    metadata = storage.get_message(response["message_id"], OWNER)["metadata_json"]
    assert NEW_SOURCE not in metadata
    shutil.rmtree(tmp_path / "worker")
    settings = storage.settings
    storage.close()
    reopened = init_storage(settings)
    try:
        assert _load(reopened, response).members == after.members
        assert _load(reopened, base).members == original.members
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "length",
        "tools",
        "function",
        "missing_finish",
        "not_mapping",
        "not_text",
        "empty",
        "duplicate",
        "unsafe",
        "no_change",
        "prose",
        "too_large",
        "unicode",
        "extra_fields",
        "invalid_json",
        "credential",
    ],
)
async def test_rejected_model_output_cannot_write_or_execute(storage, coding_boundary, monkeypatch, fault):
    base = _base(storage)
    original = _load(storage, base)
    model = Model()
    response = model.response
    if fault == "length":
        response["finish_reason"] = "length"
    elif fault == "tools":
        response["tool_calls"] = [{"name": "execute", "arguments": "echo forbidden"}]
    elif fault == "function":
        response["function_call"] = {"name": "execute"}
    elif fault == "missing_finish":
        del response["finish_reason"]
    elif fault == "not_mapping":
        model.response = []
    elif fault == "not_text":
        response["content"] = []
    elif fault == "empty":
        response["content"] = "{}"
    elif fault == "duplicate":
        response["content"] = '{"replace":{"main.py":"a","main.py":"b"}}'
    elif fault == "unsafe":
        response["content"] = '{"add":{"../escape.py":"not written"}}'
    elif fault == "no_change":
        response["content"] = json.dumps({"replace": {"main.py": dict(original.members)["main.py"].decode()}})
    elif fault == "prose":
        response["content"] = "Here is the patch:\n" + response["content"]
    elif fault == "too_large":
        from friday.organs.coding.model_edit import MAX_MODEL_OUTPUT_BYTES

        response["content"] = json.dumps({"replace": {"main.py": "x" * MAX_MODEL_OUTPUT_BYTES}})
    elif fault == "unicode":
        response["content"] = json.dumps({"replace": {"main.py": "\ud800"}})
    elif fault == "extra_fields":
        response["content"] = '{"replace":{"main.py":"x"},"execute":true}'
    elif fault == "invalid_json":
        response["content"] = '{"replace":'
    elif fault == "credential":
        response["content"] = json.dumps(
            {"replace": {"main.py": 'PASSWORD = "not-a-real-test-credential-value"\n'}}
        )
    monkeypatch.setattr(static_turn, "publish_members", lambda *a, **kw: pytest.fail("rejected model wrote"))
    monkeypatch.setattr(static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("model executed"))
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert len(model.calls) == 1
    assert result["context"]["coding_model_edit"] == "output_rejected"
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["context"]["coding_execution_attempted"] is False
    assert result["files"] == []
    assert "coding_source_revision" not in result["context"]
    assert dict(_load(storage, base).members) == dict(original.members)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "attachments",
        "wrong_hash",
        "wrong_message",
        "wrong_chat",
        "missing_task",
        "oversized_task",
        "invalid_unicode",
    ],
)
async def test_invalid_selection_never_calls_model(storage, coding_boundary, fault):
    from friday.organs.coding.model_edit import MAX_MODEL_TASK_BYTES

    base = _base(storage)
    task, conversation, attachments = _request(base), base["conversation_id"], None
    if fault == "attachments":
        attachments = [{"filename": "other.py", "content_b64": "eA=="}]
    elif fault == "wrong_hash":
        task = task.replace(base["context"]["coding_source_revision"]["revision_sha256"], "0" * 64)
    elif fault == "wrong_message":
        task = task.replace(base["message_id"], "msg_" + "0" * 16)
    elif fault == "wrong_chat":
        conversation = storage.create_conversation(OWNER, "other", mode="coding")["id"]
    elif fault == "missing_task":
        task = task.split("\n")[0]
    elif fault == "oversized_task":
        task = _request(base, "я" * MAX_MODEL_TASK_BYTES)
    else:
        # Malformed transport text must fail before any database mutation.
        task = _request(base, "\ud800")
    model = Model()
    if fault == "invalid_unicode":
        with pytest.raises(ValueError, match="UTF-8"):
            await _route(storage, model, task, conversation, attachments=attachments)
        assert not model.calls
        return
    result = await _route(storage, model, task, conversation, attachments=attachments)
    assert not model.calls
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["nonowner", "public_chat", "api"])
async def test_unauthorized_model_edit_has_no_model_or_write_effect(storage, coding_boundary, fault):
    base = _base(storage)
    if fault == "nonowner":
        actor = replace(ACTOR, preset_key="guest")
    elif fault == "public_chat":
        actor = replace(ACTOR, telegram_chat_id="-5001")
    else:
        actor = replace(ACTOR, source="api")
    model = Model()
    with pytest.raises(AuthorizationError):
        await _route(storage, model, _request(base), base["conversation_id"], actor=actor)
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0.0, float("nan"), float("inf"), True])
async def test_expired_or_invalid_deadline_never_submits(storage, coding_boundary, deadline):
    base = _base(storage)
    model = Model()
    result = await _route(storage, model, _request(base), base["conversation_id"], turn_deadline=deadline)
    assert not model.calls
    assert result["context"]["coding_model_edit"] == "deadline"
    assert result["files"] == []


@pytest.mark.asyncio
async def test_timeout_cancels_the_pending_model_without_writing(storage, coding_boundary, monkeypatch):
    from friday.organs.coding import model_edit

    base = _base(storage)
    cancelled = asyncio.Event()

    async def effect():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(model_edit, "MODEL_BUDGET_SEC", 0.02)
    monkeypatch.setattr(static_turn, "publish_members", lambda *a, **kw: pytest.fail("timeout wrote"))
    model = Model(effect=effect)
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert cancelled.is_set()
    assert len(model.calls) == 1
    assert result["context"]["coding_model_edit"] == "deadline"
    assert result["files"] == []


@pytest.mark.asyncio
async def test_task_cancellation_propagates_without_persisting_a_turn(storage, coding_boundary, monkeypatch):
    base = _base(storage)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def effect():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    model = Model(effect=effect)
    before = storage.get_conversation_messages(base["conversation_id"], user_id=OWNER)
    monkeypatch.setattr(static_turn, "publish_members", lambda *a, **kw: pytest.fail("cancelled turn wrote"))
    task = asyncio.create_task(_route(storage, model, _request(base), base["conversation_id"]))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert storage.get_conversation_messages(base["conversation_id"], user_id=OWNER) == before


@pytest.mark.asyncio
async def test_parent_revoked_during_model_await_cannot_create_a_copy(storage, coding_boundary, monkeypatch):
    base = _base(storage)

    async def revoke():
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?",
                (base["files"][0]["id"],),
            )

    model = Model(effect=revoke)
    monkeypatch.setattr(static_turn, "publish_members", lambda *a, **kw: pytest.fail("revoked source wrote"))
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert len(model.calls) == 1
    assert result["context"]["coding_revision_edit"] == "blocked"
    assert result["files"] == []


@pytest.mark.asyncio
async def test_parent_revoked_after_model_edit_still_blocks_the_existing_publisher(storage, coding_boundary):
    from friday.generated_files import GeneratedFilePersistenceError

    base = _base(storage)
    result = await _route(storage, Model(), _request(base), base["conversation_id"])
    assert result["context"]["coding_revision_edit"] == "applied"
    with storage.transaction() as conn:
        count = conn.execute("SELECT count(*) FROM raw_objects").fetchone()[0]
        conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?", (base["files"][0]["id"],)
        )
    with pytest.raises(GeneratedFilePersistenceError):
        _publish(storage, result)
    with storage.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM raw_objects").fetchone()[0] == count


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unavailable", "exception"])
async def test_model_failure_has_no_fallback_or_private_error_leak(storage, coding_boundary, fault):
    base = _base(storage)

    async def fail():
        raise RuntimeError("private-provider-error-must-not-be-persisted")

    model = None if fault == "unavailable" else Model(effect=fail)
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert result["files"] == []
    assert result["context"]["llm_failed"] is True
    assert "private-provider-error" not in json.dumps(result)
    assert "private-provider-error" not in storage.get_message(result["message_id"], OWNER)["metadata_json"]
    if model:
        assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["inspect", "restore", "edit"])
async def test_existing_static_paths_never_invoke_model(storage, coding_boundary, command):
    base = _base(storage)
    selector = f"{base['message_id']} {base['context']['coding_source_revision']['revision_sha256']}"
    message = "осмотри" if command == "inspect" else f"{command} {selector}"
    if command == "edit":
        message += '\n{"replace":{"main.py":"answer = 42\\n"}}'
    model = Model()
    result = await _route(storage, model, message, base["conversation_id"])
    assert not model.calls
    assert result["context"]["coding_execution_attempted"] is False
    assert "coding_model_edit" not in result["context"]


@pytest.mark.asyncio
async def test_russian_request_and_exact_json_fence_use_same_edit_primitive(
    storage, coding_boundary, monkeypatch
):
    base = _base(storage)
    model = Model()
    model.response["content"] = "```json\n" + model.response["content"] + "\n```"
    monkeypatch.setattr(
        static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("source task executed")
    )
    result = await _route(
        storage,
        model,
        _request(base, "Исправь main.py; run make pytest это просто текст задания.").replace(
            "revise ", "доработай ", 1
        ),
        base["conversation_id"],
    )
    assert result["context"]["coding_revision_edit"] == "applied"
    assert result["context"]["coding_execution_attempted"] is False
    assert dict(_load(storage, _publish(storage, result)).members)["main.py"] == NEW_SOURCE.encode()


@pytest.mark.parametrize("fault", ["binary", "large", "serialized", "secret"])
def test_complete_input_is_rejected_instead_of_truncated(fault):
    import hashlib

    from friday.organs.coding.model_edit import MAX_MODEL_INPUT_BYTES, _messages
    from friday.organs.coding.revision import CodingSourceRevision
    from friday.organs.coding.workspace_io import source_revision_sha256

    body = {
        "binary": b"\xff\xfe",
        "large": b"x" * (MAX_MODEL_INPUT_BYTES + 1),
        "serialized": b'"' * (MAX_MODEL_INPUT_BYTES // 2),
        "secret": b'PASSWORD = "not-a-real-test-credential-value"',
    }[fault]
    source = CodingSourceRevision(
        "project.test",
        source_revision_sha256({"main.py": hashlib.sha256(body).hexdigest()}),
        (("main.py", body),),
    )
    with pytest.raises(ValueError):
        _messages(source, "Improve this program")


@pytest.mark.asyncio
async def test_late_model_response_is_not_applied_even_if_transport_suppresses_timeout(
    storage, coding_boundary, monkeypatch
):
    from friday.organs.coding import model_edit

    base = _base(storage)

    async def late():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:  # Deliberately misbehaving fixture transport.
            return

    monkeypatch.setattr(model_edit, "MODEL_BUDGET_SEC", 0.02)
    monkeypatch.setattr(static_turn, "publish_members", lambda *a, **kw: pytest.fail("late model wrote"))
    model = Model(effect=late)
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert result["context"]["coding_model_edit"] == "deadline"
    assert result["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["coding", None])
async def test_agent_explicit_and_persisted_coding_paths_use_the_primary_model(
    storage, coding_boundary, mode
):
    from friday.agent_runtime import AgentRuntime

    base = _base(storage)
    model = Model()
    runtime = AgentRuntime(storage.settings, storage, llm=model)
    result = await runtime.chat(
        OWNER, _request(base), actor=ACTOR, conversation_id=base["conversation_id"], mode=mode
    )
    assert len(model.calls) == 1
    assert result["context"]["coding_revision_edit"] == "applied"
    assert result["context"]["coding_execution_attempted"] is False


@pytest.mark.asyncio
async def test_input_secret_is_not_sent_or_misreported_as_a_model_failure(storage, coding_boundary):
    base = _base(storage)
    model = Model()
    task = _request(base, 'Use PASSWORD = "not-a-real-test-credential-value"')
    result = await _route(storage, model, task, base["conversation_id"])
    assert not model.calls
    assert result["context"]["coding_model_edit"] == "input_rejected"
    assert result["context"]["llm_failed"] is False
    assert result["files"] == []
