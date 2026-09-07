"""Model-authored creation uses the same source and final-publisher boundary."""

from __future__ import annotations

import json

import pytest

from tests.test_coding_model_edit import (
    _load,
    _publish,
    _route,
)
from tests.test_coding_model_edit import (
    coding_boundary as coding_boundary,
)

SOURCES = {
    "main.py": "def add(a: int, b: int) -> int:\n    return a + b\n",
    "test_main.py": "import unittest\nfrom main import add\n\nclass TestAdd(unittest.TestCase):\n    def test_sum(self):\n        self.assertEqual(add(2, 3), 5)\n",
    "README.md": "# Sum\n\nA Python addition function with unittest coverage.\n",
}


class CreateModel:
    def __init__(self, *, effect=None):
        self.calls = []
        self.effect = effect
        self.response = {"content": json.dumps({"files": SOURCES}), "finish_reason": "stop", "tool_calls": []}

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.effect:
            await self.effect()
        return self.response


@pytest.mark.asyncio
async def test_create_task_publishes_model_sources_instead_of_static_scaffold(storage, coding_boundary):
    model = CreateModel()
    task = "создай python проект с функцией add(a, b), возвращающей сумму"
    response = await _route(storage, model, task, None)
    assert len(model.calls) == 1
    assert response["context"]["coding_model_create"] == "prepared"
    assert response["context"]["coding_create"] == "written"
    published = _publish(storage, response)
    assert dict(_load(storage, published).members) == {name: text.encode() for name, text in SOURCES.items()}
    assert response["verified"] is False
    assert response["context"]["coding_execution_attempted"] is False


@pytest.mark.asyncio
async def test_full_task_and_inherited_budget_reach_the_existing_primary(storage, coding_boundary, tmp_path):
    import time

    from tests.test_coding_model_edit import OWNER

    task = "создай python проект\n" + "Требование к программе. " * 50 + "Окончательное требование: сумма."
    model = CreateModel()
    deadline = time.monotonic() + 30
    response = await _route(storage, model, task, None, turn_deadline=deadline)
    assert response["context"]["coding_create"] == "written"
    messages, kwargs = model.calls[0]
    assert len(messages) == 2 and json.loads(messages[1]["content"]) == {"task": task}
    assert OWNER not in messages[1]["content"] and str(tmp_path) not in messages[1]["content"]
    assert kwargs["absolute_deadline"] == deadline
    assert kwargs["tools"] == [] and kwargs["tool_choice"] == "none"
    assert kwargs["allow_retries"] is False and kwargs["require_full_context"] is True
    assert kwargs["enable_thinking"] is False and kwargs["max_tokens"] == 8192
    records = storage.get_conversation_messages(response["conversation_id"], user_id=OWNER)
    assert any(row["role"] == "user" and row["content"] == task for row in records)
    metadata = storage.get_message(response["message_id"], OWNER)["metadata_json"]
    assert SOURCES["main.py"] not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "truncated",
        "tool",
        "function",
        "no_finish",
        "not_mapping",
        "not_text",
        "prose",
        "oversized",
        "invalid_json",
        "empty",
        "empty_files",
        "files_list",
        "extra_fields",
        "duplicate",
        "traversal",
        "absolute",
        "case_collision",
        "ancestor",
        "too_many",
        "body_type",
        "invalid_unicode",
        "secret",
        "artifact",
    ],
)
async def test_invalid_creation_proposal_never_writes_or_falls_back(
    storage, coding_boundary, monkeypatch, fault
):
    from friday.organs.coding import create, static_turn
    from friday.organs.coding.model_edit import MAX_MODEL_OUTPUT_BYTES

    model = CreateModel()
    if fault == "truncated":
        model.response["finish_reason"] = "length"
    elif fault == "tool":
        model.response["tool_calls"] = [{"name": "execute"}]
    elif fault == "function":
        model.response["function_call"] = {"name": "execute"}
    elif fault == "no_finish":
        del model.response["finish_reason"]
    elif fault == "not_mapping":
        model.response = []
    elif fault == "not_text":
        model.response["content"] = []
    else:
        model.response["content"] = {
            "prose": "Here is your program: " + json.dumps({"files": SOURCES}),
            "oversized": json.dumps({"files": {"main.py": "x" * MAX_MODEL_OUTPUT_BYTES}}),
            "invalid_json": '{"files":',
            "empty": "{}",
            "empty_files": '{"files":{}}',
            "files_list": '{"files":[]}',
            "extra_fields": '{"files":{"main.py":"x"},"run":true}',
            "duplicate": '{"files":{"main.py":"x","main.py":"y"}}',
            "traversal": '{"files":{"../main.py":"x"}}',
            "absolute": '{"files":{"/tmp/main.py":"x"}}',
            "case_collision": '{"files":{"Main.py":"x","main.py":"y"}}',
            "ancestor": '{"files":{"src":"x","src/main.py":"y"}}',
            "too_many": json.dumps({"files": {f"part{i}.py": "x" for i in range(17)}}),
            "body_type": '{"files":{"main.py":123}}',
            "invalid_unicode": json.dumps({"files": {"main.py": "\ud800"}}),
            "secret": json.dumps({"files": {"main.py": 'PASSWORD = "not-a-real-test-credential-value"'}}),
            "artifact": '{"files":{".env":"x"}}',
        }[fault]
    monkeypatch.setattr(create, "publish_members", lambda *a, **kw: pytest.fail("rejected creation wrote"))
    monkeypatch.setattr(static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("creation executed"))
    result = await _route(storage, model, "создай python калькулятор", None)
    assert len(model.calls) == 1
    assert result["context"]["coding_model_create"] == "output_rejected"
    assert result["context"]["coding_create"] == "blocked"
    assert result["files"] == [] and "coding_source_revision" not in result["context"]
    assert result["context"]["coding_execution_attempted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["attachments", "missing_chat", "foreign_chat", "oversized", "secret", "unbounded"]
)
async def test_inadmissible_creation_never_calls_primary(storage, coding_boundary, fault):

    task = "создай python калькулятор"
    conversation, attachments = None, None
    if fault == "attachments":
        attachments = [{"filename": "existing.py", "content_b64": "eA=="}]
    elif fault == "missing_chat":
        conversation = "conv_0000000000000000"
    elif fault == "foreign_chat":
        storage.ensure_user("other")
        conversation = storage.create_conversation("other", "other", mode="coding")["id"]
    elif fault == "oversized":
        task += "я" * 16384
    elif fault == "secret":
        task += ' PASSWORD = "not-a-real-test-credential-value"'
    elif fault == "unbounded":
        task = "создай всё"
    model = CreateModel()
    result = await _route(storage, model, task, conversation, attachments=attachments)
    assert not model.calls
    assert result["context"]["coding_create"] == "blocked"
    assert result["files"] == [] and result["context"]["llm_failed"] is False
    assert result["context"]["coding_execution_attempted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0.0, -1, float("nan"), float("inf"), True])
async def test_invalid_creation_deadline_never_submits(storage, coding_boundary, deadline):
    model = CreateModel()
    result = await _route(storage, model, "создай python калькулятор", None, turn_deadline=deadline)
    assert not model.calls
    assert result["context"]["coding_model_create"] == "deadline"
    assert result["context"]["coding_create"] == "blocked" and result["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unavailable", "exception", "timeout", "late"])
async def test_failed_creation_has_no_scaffold_fallback(storage, coding_boundary, monkeypatch, fault):
    import asyncio

    from friday.organs.coding import model_edit
    from tests.test_coding_model_edit import OWNER

    async def effect():
        if fault == "exception":
            raise RuntimeError("private-provider-error-do-not-persist")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if fault != "late":
                raise

    if fault in {"timeout", "late"}:
        monkeypatch.setattr(model_edit, "MODEL_BUDGET_SEC", 0.02)
    model = None if fault == "unavailable" else CreateModel(effect=effect)
    result = await _route(storage, model, "создай python калькулятор", None)
    assert result["context"]["coding_create"] == "blocked" and result["files"] == []
    assert result["context"]["llm_failed"] is True
    assert "private-provider-error" not in json.dumps(result)
    assert "private-provider-error" not in storage.get_message(result["message_id"], OWNER)["metadata_json"]
    if model:
        assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_creation_cannot_persist_a_new_turn(storage, coding_boundary, monkeypatch):
    import asyncio

    from friday.organs.coding import create
    from tests.test_coding_model_edit import OWNER

    started = asyncio.Event()
    cancelled = asyncio.Event()
    conversation = storage.create_conversation(OWNER, "coding", mode="coding")["id"]

    async def effect():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(create, "publish_members", lambda *a, **kw: pytest.fail("cancelled creation wrote"))
    task = asyncio.create_task(
        _route(storage, CreateModel(effect=effect), "создай python калькулятор", conversation)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not storage.get_conversation_messages(conversation, user_id=OWNER)


@pytest.mark.asyncio
async def test_creation_code_and_task_do_not_grant_execution(storage, coding_boundary, monkeypatch):
    from friday.organs.coding import static_turn

    monkeypatch.setattr(static_turn, "spawn_coding_worker", lambda *a, **kw: pytest.fail("creation executed"))
    model = CreateModel()
    model.response["content"] = "```json\n" + model.response["content"] + "\n```"
    response = await _route(storage, model, "создай python калькулятор и запусти pytest", None)
    assert response["context"]["coding_create"] == "written"
    assert response["context"]["coding_execution_attempted"] is False
    assert response["context"]["coding_worker_spawned"] is False
    assert "тесты не запускались" in response["message"]


@pytest.mark.asyncio
async def test_creation_survives_reopen_and_can_be_revised(storage, coding_boundary, tmp_path):
    import shutil

    from friday.storage import init_storage
    from tests.test_coding_model_edit import Model, _request

    base = _publish(storage, await _route(storage, CreateModel(), "создай python калькулятор", None))
    original = _load(storage, base)
    model = Model(
        response={
            "content": json.dumps({"add": {"difference.py": "def subtract(a, b):\n    return a - b\n"}}),
            "finish_reason": "stop",
        }
    )
    edited = _publish(
        storage, await _route(storage, model, _request(base, "Добавь вычитание"), base["conversation_id"])
    )
    revision = _load(storage, edited)
    assert revision.project_id == original.project_id and revision.revision_sha256 != original.revision_sha256
    assert dict(_load(storage, base).members) == {name: text.encode() for name, text in SOURCES.items()}
    settings = storage.settings
    storage.close()
    shutil.rmtree(tmp_path / "worker")
    reopened = init_storage(settings)
    try:
        assert _load(reopened, base) == original
        assert _load(reopened, edited) == revision
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["coding", None])
async def test_both_agent_entrypoints_use_model_creation(storage, coding_boundary, mode):
    from friday.agent_runtime import AgentRuntime
    from tests.test_coding_model_edit import ACTOR, OWNER

    conversation = storage.create_conversation(OWNER, "coding", mode="coding")["id"]
    model = CreateModel()
    runtime = AgentRuntime(storage.settings, storage, llm=model)
    result = await runtime.chat(
        OWNER, "создай python калькулятор", actor=ACTOR, conversation_id=conversation, mode=mode
    )
    assert len(model.calls) == 1 and result["context"]["coding_create"] == "written"
    assert result["context"]["coding_execution_attempted"] is False


@pytest.mark.asyncio
async def test_explicit_scaffold_keeps_its_model_free_contract(storage, coding_boundary):
    model = CreateModel()
    result = await _route(storage, model, "scaffold python example", None)
    assert not model.calls
    assert result["context"]["coding_create"] == "written"
    assert "coding_model_create" not in result["context"]
    assert b"return None" in dict(_load(storage, _publish(storage, result)).members)["main.py"]


@pytest.mark.asyncio
async def test_removed_conversation_after_model_await_blocks_creation(storage, coding_boundary, monkeypatch):
    from friday.organs.coding import create
    from tests.test_coding_model_edit import OWNER

    conversation = storage.create_conversation(OWNER, "coding", mode="coding")["id"]

    async def effect():
        storage.delete_conversation(conversation, OWNER)

    monkeypatch.setattr(create, "publish_members", lambda *a, **kw: pytest.fail("deleted conversation wrote"))
    result = await _route(storage, CreateModel(effect=effect), "создай python калькулятор", conversation)
    assert result["context"]["coding_create"] == "blocked"
    assert result["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["создай", "create", "новый проект"])
async def test_creation_without_a_task_never_invents_a_project(storage, coding_boundary, task):
    model = CreateModel()
    result = await _route(storage, model, task, None)
    assert not model.calls
    assert result["context"]["coding_model_create"] == "invalid_request"
    assert result["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["write", "changed_source"])
async def test_failed_creation_write_or_snapshot_cannot_publish_sources(
    storage, coding_boundary, monkeypatch, fault
):
    from friday.organs.coding import create, static_turn

    if fault == "write":

        def fail(*args, **kwargs):
            raise OSError("private-filesystem-error-do-not-persist")

        monkeypatch.setattr(create, "publish_members", fail)
    else:
        archive = static_turn.observe_coding_result_archive

        def change(**kwargs):
            (kwargs["workspace"] / "main.py").write_text("corrupted = True\n")
            return archive(**kwargs)

        monkeypatch.setattr(static_turn, "observe_coding_result_archive", change)
    result = await _route(storage, CreateModel(), "создай python калькулятор", None)
    assert not result["files"] and "coding_source_revision" not in result["context"]
    assert "заблокировано" in result["message"]
    assert "private-filesystem-error" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["nonowner", "public_chat", "api"])
async def test_unauthorized_creation_has_no_primary_or_file_effect(storage, coding_boundary, fault):
    from dataclasses import replace

    from friday.permissions import AuthorizationError
    from tests.test_coding_model_edit import ACTOR

    actor = {
        "nonowner": replace(ACTOR, preset_key="guest"),
        "public_chat": replace(ACTOR, telegram_chat_id="-5001"),
        "api": replace(ACTOR, source="api"),
    }[fault]
    model = CreateModel()
    with pytest.raises(AuthorizationError):
        await _route(storage, model, "создай python калькулятор", None, actor=actor)
    assert not model.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("during", [False, True])
async def test_archival_cancels_new_model_edit_without_removing_saved_revision(
    storage, coding_boundary, during
):
    from tests.test_coding_model_edit import OWNER, Model, _base, _request

    base = _base(storage)
    original = _load(storage, base)

    async def archive():
        storage.delete_conversation(base["conversation_id"], OWNER)

    model = Model(effect=archive if during else None)
    if not during:
        await archive()
    result = await _route(storage, model, _request(base), base["conversation_id"])
    assert len(model.calls) == int(during)
    assert result["context"]["coding_revision_edit"] == "blocked" and result["files"] == []
    assert _load(storage, base) == original


CSV_TASK = "создай python cli который читает csv из stdin и печатает сводку rows и sum колонки amount"
CSV_SOURCES = {
    "main.py": (
        "import csv, io, sys\n"
        "def main() -> None:\n"
        "    text = sys.stdin.read()\n"
        "    if text == '':\n"
        "        raise SystemExit(2)\n"
        "    reader = csv.DictReader(io.StringIO(text))\n"
        "    if reader.fieldnames is None:\n"
        "        raise SystemExit(2)\n"
        "    total = 0\n"
        "    count = 0\n"
        "    for row in reader:\n"
        "        count += 1\n"
        "        total += int(row['amount'])\n"
        "    sys.stdout.write(f'rows={count}\\nsum={total}\\n')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    ),
    "README.md": "# CSV summary\n\nRead CSV from stdin and print rows/sum.\n",
}
WRONG_CSV_SOURCES = {
    "main.py": (
        "def main() -> None:\n"
        "    print('rows=0')\n"
        "    print('sum=0')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    ),
    "test_main.py": (
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self) -> None:\n        self.assertTrue(True)\n"
    ),
    "README.md": "# wrong\n",
}


class ScriptedCreateModel:
    def __init__(self, payloads: list[str]):
        self.calls = []
        self.payloads = list(payloads)

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.payloads.pop(0), "finish_reason": "stop", "tool_calls": []}


@pytest.mark.asyncio
async def test_csv_summary_create_is_verified_by_independent_oracle(storage, coding_boundary):
    model = CreateModel()
    model.response = {
        "content": json.dumps({"files": CSV_SOURCES}),
        "finish_reason": "stop",
        "tool_calls": [],
    }
    response = await _route(storage, model, CSV_TASK, None)
    assert len(model.calls) == 1
    assert json.loads(model.calls[0][0][1]["content"]) == {"task": CSV_TASK}
    assert "amount column" in model.calls[0][0][0]["content"]
    assert response["verified"] is True
    assert response["context"]["coding_create"] == "written"
    assert response["context"]["coding_behavior_oracle"] == "csv_summary_v1"
    assert response["context"]["coding_behavior_verification"] == "verified"
    assert response["context"]["coding_execution_attempted"] is True
    assert response["context"]["coding_loop"] == "empty"
    published = _publish(storage, response)
    assert dict(_load(storage, published).members)["main.py"] == CSV_SOURCES["main.py"].encode()


@pytest.mark.asyncio
async def test_wrong_csv_summary_is_repaired_then_verified(storage, coding_boundary):
    model = ScriptedCreateModel(
        [json.dumps({"files": WRONG_CSV_SOURCES}), json.dumps({"files": CSV_SOURCES})]
    )
    response = await _route(storage, model, CSV_TASK, None)
    assert len(model.calls) == 2
    repair = json.loads(model.calls[1][0][1]["content"])
    assert repair["task"] == CSV_TASK and repair["repair"] is True
    assert repair["diagnostics"]["failed_case"] == "two_rows"
    assert response["verified"] is True
    assert response["context"]["coding_model_calls"] == 2
    assert response["context"]["coding_behavior_verification"] == "verified"
    published = _publish(storage, response)
    assert dict(_load(storage, published).members)["main.py"] == CSV_SOURCES["main.py"].encode()


@pytest.mark.asyncio
async def test_exhausted_csv_summary_repair_keeps_the_failing_artifact(storage, coding_boundary):
    wrong = json.dumps({"files": WRONG_CSV_SOURCES})
    model = ScriptedCreateModel([wrong, wrong, wrong])
    response = await _route(storage, model, CSV_TASK, None)
    assert len(model.calls) == 3
    assert response["verified"] is False
    assert response["context"]["coding_create"] == "written"
    assert response["context"]["coding_behavior_verification"] == "blocked"
    assert response["context"]["coding_behavior_verification_reason"] == "oracle_failed"
    published = _publish(storage, response)
    assert dict(_load(storage, published).members)["main.py"] == WRONG_CSV_SOURCES["main.py"].encode()
    assert "заготовка вместо программы не подставлялась" in response["message"].casefold()


@pytest.mark.asyncio
async def test_csv_summary_user_run_words_do_not_grant_execute(storage, coding_boundary, monkeypatch):
    from friday.organs.coding import static_turn

    loop = static_turn.observe_coding_isolated_loop

    def refuse_execute(*args, **kwargs):
        operation = kwargs.get("operation")
        if getattr(operation, "value", operation) in {"execute", "run", "test"}:
            raise AssertionError("user execute claim reached the isolated loop")
        return loop(*args, **kwargs)

    monkeypatch.setattr(static_turn, "observe_coding_isolated_loop", refuse_execute)
    model = CreateModel()
    model.response = {
        "content": json.dumps({"files": CSV_SOURCES}),
        "finish_reason": "stop",
        "tool_calls": [],
    }
    response = await _route(storage, model, CSV_TASK + " и запусти программу", None)
    assert response["verified"] is True
    assert response["context"]["coding_loop"] == "empty"
    assert response["context"]["coding_behavior_verification"] == "verified"


@pytest.mark.asyncio
async def test_verified_csv_summary_survives_workspace_loss(storage, coding_boundary, tmp_path):
    import shutil

    from friday.storage import init_storage
    from tests.test_coding_model_edit import Model, _request

    model = CreateModel()
    model.response = {
        "content": json.dumps({"files": CSV_SOURCES}),
        "finish_reason": "stop",
        "tool_calls": [],
    }
    created = await _route(storage, model, CSV_TASK, None)
    base = _publish(storage, created)
    original = _load(storage, base)
    assert created["verified"] is True
    helper = {"add": {"helper.py": "def version() -> int:\n    return 1\n"}}
    edited_response = await _route(
        storage,
        Model(response={"content": json.dumps(helper), "finish_reason": "stop"}),
        _request(base, "Добавь helper.version без изменения CLI"),
        base["conversation_id"],
    )
    assert edited_response["verified"] is True
    assert edited_response["context"]["coding_behavior_verification"] == "verified"
    edited = _publish(storage, edited_response)
    revision = _load(storage, edited)
    assert revision.project_id == original.project_id
    assert revision.revision_sha256 != original.revision_sha256
    assert dict(_load(storage, base).members)["main.py"] == CSV_SOURCES["main.py"].encode()
    settings = storage.settings
    storage.close()
    shutil.rmtree(tmp_path / "worker")
    reopened = init_storage(settings)
    try:
        assert _load(reopened, base) == original
        assert _load(reopened, edited) == revision
        restored = await _route(
            reopened,
            None,
            f"restore {base['message_id']} {original.revision_sha256}",
            base["conversation_id"],
        )
        assert restored["context"]["coding_revision_restore"] == "restored"
        assert restored["verified"] is False
    finally:
        reopened.close()


async def _verified_csv(storage):
    model = CreateModel()
    model.response = {
        "content": json.dumps({"files": CSV_SOURCES}),
        "finish_reason": "stop",
        "tool_calls": [],
    }
    created = await _route(storage, model, CSV_TASK, None)
    return _publish(storage, created)


@pytest.mark.asyncio
async def test_wrong_csv_summary_edit_is_repaired_then_verified(storage, coding_boundary):
    from tests.test_coding_model_edit import _request

    repaired_main = "# repaired\n" + CSV_SOURCES["main.py"]
    base = await _verified_csv(storage)
    original = _load(storage, base)
    model = ScriptedCreateModel(
        [
            json.dumps({"replace": {"main.py": WRONG_CSV_SOURCES["main.py"]}}),
            json.dumps({"replace": {"main.py": repaired_main}}),
        ]
    )
    response = await _route(
        storage,
        model,
        _request(base, "Поправь обработку пустого ввода, не меняя CLI"),
        base["conversation_id"],
    )
    assert len(model.calls) == 2
    repair = json.loads(model.calls[1][0][1]["content"])
    assert repair["repair"] is True
    assert repair["diagnostics"]["failed_case"] == "two_rows"
    assert response["verified"] is True
    assert response["context"]["coding_revision_edit"] == "applied"
    assert dict(_load(storage, base).members) == dict(original.members)
    published = _publish(storage, response)
    assert dict(_load(storage, published).members)["main.py"] == repaired_main.encode()


@pytest.mark.asyncio
async def test_reply_selects_the_exact_published_revision(storage, coding_boundary):
    from tests.test_coding_model_edit import Model

    base = await _verified_csv(storage)
    original = _load(storage, base)
    helper = {"add": {"helper.py": "def version() -> int:\n    return 1\n"}}
    response = await _route(
        storage,
        Model(response={"content": json.dumps(helper), "finish_reason": "stop"}),
        "доработай этот проект\nДобавь helper.version без изменения CLI",
        base["conversation_id"],
        reply_assistant_message_id=base["message_id"],
    )
    assert response["verified"] is True
    assert response["context"]["coding_revision_edit"] == "applied"
    published = _publish(storage, response)
    after = _load(storage, published)
    assert "helper.py" in dict(after.members)
    assert dict(_load(storage, base).members) == dict(original.members)


@pytest.mark.asyncio
async def test_natural_edit_without_reply_does_not_select_latest(storage, coding_boundary):
    from tests.test_coding_model_edit import Model

    base = await _verified_csv(storage)
    original = _load(storage, base)
    response = await _route(
        storage,
        Model(),
        "доработай этот проект\nДобавь helper.version без изменения CLI",
        base["conversation_id"],
    )
    assert response["context"]["coding_revision_edit"] == "blocked"
    assert response["verified"] is False
    assert dict(_load(storage, base).members) == dict(original.members)


@pytest.mark.asyncio
async def test_restore_via_reply_reopens_the_exact_parent(storage, coding_boundary):
    base = await _verified_csv(storage)
    original = _load(storage, base)
    restored = await _route(
        storage,
        None,
        "восстанови этот проект",
        base["conversation_id"],
        reply_assistant_message_id=base["message_id"],
    )
    assert restored["context"]["coding_revision_restore"] == "restored"
    assert restored["verified"] is False
    assert dict(_load(storage, base).members) == dict(original.members)
