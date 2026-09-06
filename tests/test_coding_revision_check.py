"""Exact source checks: real compiler bytes, durable reports, no code execution.

Most turn tests use the real trusted compiler without certifying host isolation.
The native case separately exercises the production Bubblewrap command.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from friday.generated_files import GeneratedFilePersistenceError
from friday.organs.coding import check, model_edit, static_turn
from friday.organs.coding.revision import CodingRevisionUnavailable
from friday.permissions import ActorContext, AuthorizationError
from friday.storage import init_storage
from tests.test_coding_revision_edit import OWNER, _base, _load, _publish, _request, _turn


def _payload(*sources):
    return json.dumps([base64.b64encode(body).decode() for body in sources], separators=(",", ":")).encode()


def _compiler(payload):
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", check._CHECK_PROGRAM],
        input=payload,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return result.stdout


@pytest.fixture
def compiler(monkeypatch):
    calls = []

    async def run(payload, deadline):
        calls.append((payload, deadline))
        return _compiler(payload)

    monkeypatch.setattr(check, "run_source_compiler", run)
    return calls


def _command(base):
    return f"check {base['message_id']} {base['context']['coding_source_revision']['revision_sha256']}"


async def _check(storage, base, **kwargs):
    parameters = dict(
        storage=storage,
        user_id=OWNER,
        actor=ActorContext(OWNER, "owner", "telegram-bridge", identity_id="5001", telegram_chat_id="5001"),
        message=_command(base),
        conversation_id=base["conversation_id"],
        attachments=None,
        model=object(),
    )
    parameters.update(kwargs)
    return await model_edit.handle_coding_turn(**parameters)


def _report(response):
    return json.loads(base64.b64decode(response["files"][0]["content_base64"]))


def test_trusted_compiler_never_imports_or_executes_project_code(tmp_path):
    sentinel = tmp_path / "not-executed"
    source = f"open({str(sentinel)!r}, 'w').write('executed')\nraise RuntimeError('not imported')\n".encode()
    payload = _payload(source, b"def broken(:\n    pass\n")
    report = check._checked_report(_compiler(payload), payload, ("main.py", "bad.py"))
    assert report["state"] == "syntax_failed" and report["checked_files"] == 2
    assert report["errors"] == [{"path": "bad.py", "line": 1, "column": 12}]
    assert report["error_count"] == 1 and report["behavior_tested"] is False
    assert not sentinel.exists()
    assert "not imported" not in json.dumps(report) and "def broken" not in json.dumps(report)


@pytest.mark.parametrize(
    "source", [b"", b"raise RuntimeError('compiles, does not run')", b"# coding: latin1\nname='\xe9'\n"]
)
def test_empty_or_non_utf8_pep263_source_is_compiled_as_exact_bytes(source):
    payload = _payload(source)
    report = check._checked_report(_compiler(payload), payload, ("main.py",))
    assert report["state"] == "syntax_passed" and report["checked_files"] == 1


def test_many_syntax_errors_are_counted_but_bounded_without_source_excerpts():
    payload = _payload(*([b"def SECRET_BODY(:\n"] * 40))
    raw = _compiler(payload)
    report = check._checked_report(raw, payload, tuple(f"f{i}.py" for i in range(40)))
    assert report["error_count"] == 40 and len(report["errors"]) == check.MAX_CHECK_ERRORS
    assert report["diagnostics_truncated"] is True
    assert b"SECRET_BODY" not in raw and len(raw) < check.MAX_CHECK_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_check_uses_durable_bytes_after_restart_without_recreating_workspace(
    storage, tmp_path, compiler
):
    base = _base(storage, tmp_path)
    before = dict(_load(storage, base).members)
    settings = storage.settings
    storage.close()
    shutil.rmtree(tmp_path / "worker")
    reopened = init_storage(settings)
    try:
        result = await _check(reopened, base)
        assert result["context"]["coding_revision_check"] == "syntax_passed"
        assert result["verified"] is False and result["context"]["coding_execution_attempted"] is False
        report = _report(result)
        assert report["source_message_id"] == base["message_id"]
        assert report["revision_sha256"] == base["context"]["coding_source_revision"]["revision_sha256"]
        assert report["behavior_tested"] is False
        assert len(compiler) == 1
        assert not (tmp_path / "worker").exists()
        published = _publish(reopened, result)
        assert dict(_load(reopened, base).members) == before
        metadata = json.loads(reopened.get_message(published["message_id"], OWNER)["metadata_json"])
        assert metadata["coding_checked_source"] == result["context"]["coding_checked_source"]
        assert metadata["generated_files"][0]["sha256"] == published["files"][0]["sha256"]
        assert "coding_source_revision" not in metadata
        # A check report is not a new source revision, even though it is a file.
        with pytest.raises(CodingRevisionUnavailable):
            check.load_coding_revision(
                reopened,
                settings.files_dir,
                person_id=OWNER,
                tenant_id=OWNER,
                conversation_id=base["conversation_id"],
                message_id=result["message_id"],
                revision_sha256=report["revision_sha256"],
            )
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_check_reports_syntax_error_on_selected_revision_not_the_newest(storage, tmp_path, compiler):
    base = _base(storage, tmp_path)
    bad = _publish(
        storage,
        _turn(
            storage,
            tmp_path,
            _request(base, {"replace": {"main.py": "def broken(:\n"}}),
            base["conversation_id"],
        ),
    )
    good = _publish(
        storage,
        _turn(
            storage,
            tmp_path,
            _request(bad, {"replace": {"main.py": "answer = 42\n"}}),
            base["conversation_id"],
        ),
    )
    result = await _check(storage, bad)
    assert result["context"]["coding_revision_check"] == "syntax_failed"
    assert _report(result)["errors"][0]["path"] == "main.py"
    assert _report(result)["revision_sha256"] != good["context"]["coding_source_revision"]["revision_sha256"]
    assert (await _check(storage, good))["context"]["coding_revision_check"] == "syntax_passed"
    assert len(compiler) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["digest", "message", "chat", "archived", "upload", "malformed", "extra_line"]
)
async def test_invalid_selection_does_not_call_model_compiler_or_write_workspace(
    storage, tmp_path, monkeypatch, fault
):
    base = _base(storage, tmp_path)
    message = _command(base)
    chat = base["conversation_id"]
    attachments = None
    if fault == "digest":
        message = message.rsplit(" ", 1)[0] + " " + "0" * 64
    elif fault == "message":
        message = message.replace(base["message_id"], "msg_0000000000000000")
    elif fault == "chat":
        chat = storage.create_conversation(OWNER, title="other", mode="coding")["id"]
    elif fault == "archived":
        storage.archive_conversation(chat, OWNER)
    elif fault == "upload":
        attachments = [{"filename": "main.py"}]
    elif fault == "malformed":
        message = "check latest"
    else:
        message += "\nrun whatever"
    monkeypatch.setattr(check, "run_source_compiler", lambda *a: pytest.fail("invalid check compiled"))
    monkeypatch.setattr(static_turn, "publish_members", lambda *a: pytest.fail("check wrote workspace"))
    result = await _check(storage, base, message=message, conversation_id=chat, attachments=attachments)
    assert result["files"] == []
    assert result["context"]["coding_revision_check"] in {"invalid_request", "source_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, -1.0])
async def test_invalid_or_expired_deadline_never_starts_compiler(storage, tmp_path, monkeypatch, deadline):
    base = _base(storage, tmp_path)
    monkeypatch.setattr(check, "run_source_compiler", lambda *a: pytest.fail("expired check compiled"))
    result = await _check(storage, base, turn_deadline=deadline)
    assert result["context"]["coding_revision_check"] == "deadline" and not result["files"]


@pytest.mark.asyncio
async def test_inherited_deadline_not_reset_and_payload_contains_no_path_or_identity(
    storage, tmp_path, compiler
):
    base = _base(storage, tmp_path)
    end = time.monotonic() + 5
    await _check(storage, base, turn_deadline=end)
    payload, deadline = compiler[0]
    assert deadline == end
    assert base["message_id"].encode() not in payload and OWNER.encode() not in payload
    assert str(tmp_path).encode() not in payload
    assert all(type(value) is str for value in json.loads(payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revoked", "archived"])
async def test_revocation_during_compile_discards_the_report(storage, tmp_path, monkeypatch, fault):
    base = _base(storage, tmp_path)

    async def run(payload, deadline):
        if fault == "archived":
            storage.archive_conversation(base["conversation_id"], OWNER)
        else:
            with storage.transaction() as conn:
                conn.execute(
                    "UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?",
                    (base["files"][0]["id"],),
                )
        return _compiler(payload)

    monkeypatch.setattr(check, "run_source_compiler", run)
    result = await _check(storage, base)
    assert result["context"]["coding_revision_check"] == "source_unavailable"
    assert result["files"] == [] and "coding_checked_source" not in result["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["revoked", "archived", "binding_shape"])
async def test_final_publisher_reauthorizes_stored_binding_not_response_context(
    storage, tmp_path, compiler, fault
):
    base = _base(storage, tmp_path)
    result = await _check(storage, base)
    with storage.transaction() as conn:
        count = conn.execute("SELECT count(*) FROM raw_objects").fetchone()[0]
        if fault == "revoked":
            conn.execute(
                "UPDATE raw_objects SET deleted_at='2026-09-06T00:00:00Z' WHERE id=?",
                (base["files"][0]["id"],),
            )
        elif fault == "binding_shape":
            metadata = json.loads(storage.get_message(result["message_id"], OWNER)["metadata_json"])
            metadata["coding_checked_source"]["extra"] = "forbidden"
            conn.execute(
                "UPDATE messages SET metadata_json=? WHERE id=?", (json.dumps(metadata), result["message_id"])
            )
        else:
            storage.archive_conversation(base["conversation_id"], OWNER)
    result["context"].pop("coding_checked_source")
    with pytest.raises(GeneratedFilePersistenceError, match="no longer authorized"):
        _publish(storage, result)
    with storage.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM raw_objects").fetchone()[0] == count
    assert "generated_files" not in json.loads(
        storage.get_message(result["message_id"], OWNER)["metadata_json"]
    )


@pytest.mark.asyncio
async def test_cancel_during_compile_does_not_store_a_new_turn(storage, tmp_path, monkeypatch):
    base = _base(storage, tmp_path)
    before = storage.get_conversation_messages(base["conversation_id"], user_id=OWNER)

    async def cancel(*a):
        raise asyncio.CancelledError

    monkeypatch.setattr(check, "run_source_compiler", cancel)
    with pytest.raises(asyncio.CancelledError):
        await _check(storage, base)
    assert storage.get_conversation_messages(base["conversation_id"], user_id=OWNER) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "oversized",
        "corrupt",
        "wrong_input",
        "bool_count",
        "unexpected_key",
        "duplicate_key",
        "missing_errors",
        "wrong_index",
    ],
)
async def test_absent_or_malformed_compiler_result_never_becomes_success(
    storage, tmp_path, monkeypatch, fault
):
    base = _base(storage, tmp_path)

    async def run(payload, deadline):
        if fault == "missing":
            return None
        if fault == "oversized":
            return b"x" * (check.MAX_CHECK_OUTPUT_BYTES + 1)
        if fault == "corrupt":
            return b"{"
        data = json.loads(_compiler(payload))
        if fault == "wrong_input":
            data["input_sha256"] = "0" * 64
        elif fault == "bool_count":
            data["checked_files"] = True
        elif fault == "unexpected_key":
            data["tools"] = []
        elif fault == "duplicate_key":
            return (json.dumps(data)[:-1] + ',"checked_files":2}').encode()
        elif fault == "missing_errors":
            data["error_count"] = 1
        elif fault == "wrong_index":
            data["error_count"] = 1
            data["errors"] = [{"index": 999, "line": 1, "column": 1}]
        return json.dumps(data).encode()

    monkeypatch.setattr(check, "run_source_compiler", run)
    result = await _check(storage, base)
    assert result["context"]["coding_revision_check"] in {"compiler_unavailable", "report_rejected"}
    assert not result["files"] and result["verified"] is False


@pytest.mark.asyncio
async def test_non_owner_is_denied_before_revision_lookup_or_process():
    actor = ActorContext("other", "user", "telegram-bridge", identity_id="5002", telegram_chat_id="5002")
    with pytest.raises(AuthorizationError):
        await model_edit.handle_coding_turn(
            storage=None,
            user_id="other",
            actor=actor,
            message="check latest",
            conversation_id=None,
            attachments=None,
        )


def test_compiler_argv_is_fixed_isolated_and_contains_no_source_mount():
    argv = check._check_argv()
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in argv and "--disable-userns" in argv
    assert "--new-session" in argv and "--die-with-parent" in argv
    assert "--bind" not in argv and "--share-net" not in argv
    assert check.PRLIMIT_EXECUTABLE in argv and "--fsize=0:0" in argv
    assert argv[-5:] == ("-I", "-B", "-S", "-c", check._CHECK_PROGRAM)
    assert str(check.CHECK_MEMORY_BYTES) in next(value for value in argv if value.startswith("--as="))


@pytest.mark.asyncio
async def test_native_compiler_checks_exact_bytes_without_running_them(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    payload = _payload(f"open({str(sentinel)!r}, 'w').write('never')\n".encode(), b"def invalid(:\n")
    raw = await check.run_source_compiler(payload, time.monotonic() + 20)
    assert raw is not None, "requires the declared native Bubblewrap/prlimit prerequisite"
    report = check._checked_report(raw, payload, ("main.py", "test_bad.py"))
    assert report["state"] == "syntax_failed" and report["error_count"] == 1
    assert not sentinel.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["files", "bytes", "no_python"])
async def test_source_bounds_or_absent_python_do_not_start_compiler(storage, tmp_path, monkeypatch, fault):
    base = _base(storage, tmp_path)
    if fault == "files":
        monkeypatch.setattr(check, "MAX_CHECK_FILES", 1)
    elif fault == "bytes":
        monkeypatch.setattr(check, "MAX_CHECK_SOURCE_BYTES", 1)
    else:
        base = _publish(
            storage,
            _turn(
                storage,
                tmp_path,
                _request(base, {"delete": ["main.py", "test_main.py"]}),
                base["conversation_id"],
            ),
        )
    monkeypatch.setattr(check, "run_source_compiler", lambda *a: pytest.fail("unsupported input compiled"))
    result = await _check(storage, base)
    assert result["context"]["coding_revision_check"] in {"input_rejected", "no_python_sources"}
    assert not result["files"]


@pytest.mark.asyncio
async def test_static_entrance_cannot_claim_a_compilation_that_never_ran(storage, tmp_path):
    base = _base(storage, tmp_path)
    result = _turn(storage, tmp_path, _command(base), base["conversation_id"])
    assert result["context"]["coding_revision_check"] == "invalid_request"
    assert not result["files"] and result["verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "cancel", "overflow", "nonzero"])
async def test_compiler_process_is_reaped_on_timeout_cancel_overflow_and_failure(monkeypatch, fault):
    # Lifecycle-only launch substitution. Never a fallback in the product.
    script = {
        "timeout": "import time; time.sleep(60)",
        "cancel": "import time; time.sleep(60)",
        "overflow": "import os,time; os.write(1,b'x'*65536); time.sleep(60)",
        "nonzero": "raise SystemExit(3)",
    }[fault]
    monkeypatch.setattr(check, "_check_argv", lambda: (sys.executable, "-I", "-c", script))
    original = asyncio.create_subprocess_exec
    launched = asyncio.Event()
    pids = []

    async def spawn(*args, **kwargs):
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
        process = await original(*args, **kwargs)
        pids.append(process.pid)
        launched.set()
        return process

    monkeypatch.setattr(check.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        check.run_source_compiler(
            _payload(b"x = 1\n"), time.monotonic() + (0.5 if fault == "timeout" else 10)
        )
    )
    await asyncio.wait_for(launched.wait(), 5)
    if fault == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    else:
        assert await asyncio.wait_for(task, 10) is None
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cancellation_while_launching_cannot_orphan_the_new_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    pids = []

    async def delayed_spawn(*args, **kwargs):
        process = await original(sys.executable, "-I", "-c", "import time; time.sleep(60)", **kwargs)
        pids.append(process.pid)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(check.asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(check.run_source_compiler(_payload(b"x=1"), time.monotonic() + 10))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


@pytest.mark.asyncio
async def test_fragmented_stdout_is_read_completely_and_success_is_reaped(monkeypatch):
    script = "import os;[os.write(1,bytes([c])) for c in b'{\"ok\":true}']"
    monkeypatch.setattr(check, "_check_argv", lambda: (sys.executable, "-I", "-c", script))
    assert await check.run_source_compiler(b"[]", time.monotonic() + 10) == b'{"ok":true}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,deadline",
    [(b"", 10), (b"x", 0), (b"x", True), (b"x", float("nan")), (b"x", float("inf")), ("not-bytes", 10)],
)
async def test_bad_runner_inputs_never_launch_a_process(monkeypatch, payload, deadline):
    monkeypatch.setattr(
        check.asyncio, "create_subprocess_exec", lambda *a, **kw: pytest.fail("invalid runner input launched")
    )
    assert await check.run_source_compiler(payload, deadline) is None


@pytest.mark.asyncio
async def test_missing_bubblewrap_never_falls_back_to_a_host_compiler(monkeypatch):
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(args)
        raise FileNotFoundError

    monkeypatch.setattr(check.asyncio, "create_subprocess_exec", unavailable)
    assert await check.run_source_compiler(_payload(b"x=1"), time.monotonic() + 10) is None
    assert len(calls) == 1 and calls[0][0] == "/usr/bin/bwrap"


@pytest.mark.asyncio
async def test_repeated_cancellation_during_launch_still_reaps_the_child(monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        process = await original(sys.executable, "-I", "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(check.asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(check.run_source_compiler(_payload(b"x=1"), time.monotonic() + 10))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        with pytest.raises(ProcessLookupError):
            os.kill(processes[0].pid, 0)
    finally:
        # Preserve test isolation even when measuring the unfixed cancellation bug.
        if processes[0].returncode is None:
            processes[0].kill()
            await processes[0].communicate()


def test_expanded_diagnostic_paths_keep_the_final_report_byte_bounded():
    sources = [b"def broken(:\n"] * 20
    payload = _payload(*sources)
    names = tuple("\U0001f9ed" * 4000 + f"/{index}.py" for index in range(20))
    report = check._checked_report(_compiler(payload), payload, names)
    assert report["error_count"] == 20
    assert report["diagnostics_truncated"] is True
    assert 0 < len(report["errors"]) < 16
    assert len(json.dumps(report, ensure_ascii=False).encode()) < check.MAX_CHECK_OUTPUT_BYTES - 512
    assert all(error["path"] in names for error in report["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["check", "CHECK", "Проверь"])
async def test_check_aliases_share_the_same_revision_and_no_model_path(storage, tmp_path, compiler, verb):
    base = _base(storage, tmp_path)
    message = _command(base).replace("check", verb, 1)
    response = await _check(storage, base, message=message)
    assert response["context"]["coding_revision_check"] == "syntax_passed"
    assert _report(response)["source_message_id"] == base["message_id"]
    assert len(compiler) == 1 and response["tools_used"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["check this upload", "проверь main.py", "check", "проверь"])
@pytest.mark.parametrize("entrance", ["async", "static"])
async def test_upload_inspection_is_not_stolen_by_saved_revision_check(monkeypatch, message, entrance):
    # A saved-revision command must not replace the established upload inspector.
    monkeypatch.setattr(check, "run_source_compiler", lambda *a: pytest.fail("upload compiled"))
    monkeypatch.setattr(static_turn, "publish_members", lambda *a: pytest.fail("upload written"))
    args = dict(
        storage=None,
        user_id=OWNER,
        actor=ActorContext(OWNER, "owner", "telegram-bridge", identity_id="5001", telegram_chat_id="5001"),
        message=message,
        conversation_id=None,
        attachments=[{"filename": "main.py", "size": 12}],
    )
    result = (
        await model_edit.handle_coding_turn(**args, model=object())
        if entrance == "async"
        else static_turn.handle_coding_static_turn(**args)
    )
    assert result["context"]["coding_inspect_report"] == "inspected"
    assert result["context"]["coding_member_count"] == 1
    assert "coding_revision_check" not in result["context"]
    assert result["context"]["coding_execution_attempted"] is False
