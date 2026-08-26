"""Code-owned Engineer Java compilation tool and current-intent boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from friday.execution_kernel import ExecutionKernel, mark_tool_physical_start
from friday.organs import ServiceContext
from friday.organs.engineer import ENGINEER_BUILD, compiler
from friday.organs.engineer import tools as engineer_tools
from friday.organs.engineer.targets import (
    artifact_compile_request_is_atomic,
    requested_artifact_compile_filename,
    requests_artifact_compile,
)
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService
from friday.source_identity import authorized_file_snapshot_token


def _class_bytes() -> bytes:
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x41bounded-main"


def _jar() -> bytes:
    payload = compiler._deterministic_jar([("Main.class", _class_bytes())])  # noqa: SLF001
    assert payload is not None
    return payload


def _worker_report(source: bytes, jar: bytes) -> dict[str, object]:
    return {
        "ok": True,
        "status": "completed",
        "schema": compiler.SCHEMA,
        "profile": compiler.PROFILE,
        "tool_name": compiler.TOOL_NAME,
        "tool_version": compiler.JDK_VERSION,
        "jdk_version": compiler.JDK_VERSION,
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "jar_sha256": hashlib.sha256(jar).hexdigest(),
        "source_size_bytes": len(source),
        "class_files": 1,
        "class_bytes": len(_class_bytes()),
        "jar_size_bytes": len(jar),
        "java_release": 21,
        "class_major_version": 65,
        "archive": "jar",
        "compression": "stored",
        "signed": False,
        "manifest": False,
        "runtime_validation": "not_performed",
        "sample_executed": False,
        "network": "none",
        "sandbox": {
            "ok": True,
            "boundary": "bubblewrap",
            "network": "none",
            "compile_pids_limit": 512,
            "compile_memory_limit_bytes": 12 * 1024**3,
        },
    }


def _stored_source(
    source: bytes,
    *,
    raw_id: str = "raw_0123456789abcdef",
    filename: str = "Main.java",
) -> SimpleNamespace:
    raw = {
        "id": raw_id,
        "source": "upload",
        "source_ref": "test:compile",
        "content_type": "file",
        "received_at": "2026-08-26T00:00:00+00:00",
        "content_hash": hashlib.sha256(source).hexdigest(),
        "_raw_content": "",
        "_raw_metadata": "{}",
    }
    snapshot_token = authorized_file_snapshot_token(
        raw,
        content_sha256=hashlib.sha256(source).hexdigest(),
    )
    assert snapshot_token is not None
    return SimpleNamespace(
        raw_id=raw_id,
        content=source,
        filename=filename,
        snapshot_token=snapshot_token,
    )


@pytest.mark.parametrize(
    "speech",
    (
        "Скомпилируй этот Java-файл",
        "Пожалуйста, собери Main.java в JAR",
        "Теперь скомпилируй приложенный исходник Java в JAR",
        "Compile this Java source into a JAR",
        "Compile this",
        "Build this file",
        "Compile the current source",
        "Please build the attached Main.java as a JAR",
        "Build Main.java",
        "Compile Main.java",
        "Сборка Main.java",
        "Компиляция Main.java",
        "Нужно скомпилировать Main.java",
        "Не могла бы ты скомпилировать Main.java?",
        "Выполни сборку Main.java в JAR",
        "Build profile java21_single_source_library_jar_v1",
        "Compile with profile java21_single_source_library_jar_v1",
        "Сборка по профилю java21_single_source_library_jar_v1",
        "Компиляция профилем java21_single_source_library_jar_v1",
        "Скомпилируй это",
        "Скомпилируй этот файл",
        "Собери этот исходник",
        "Build and send the JAR",
        "Compile and attach the binary",
        "Собери и пришли бинарник",
        "Скомпилируй и приложи JAR",
        "Compile Main.java and send me the JAR",
        "Build Main.java and attach the binary",
        "Скомпилируй Main.java и пришли JAR",
        "Собери Main.java и приложи бинарник",
        "Собери и пришли бинарник/JAR",
        "Build and attach the binary/JAR",
    ),
)
def test_direct_current_java_compile_requests_are_admitted(speech: str) -> None:
    assert requests_artifact_compile(speech) is True


def test_compile_target_filename_is_exact_and_code_owned() -> None:
    assert requested_artifact_compile_filename("Compile Main.java") == "Main.java"
    assert requested_artifact_compile_filename("Скомпилируй этот Java-файл") is None
    assert requested_artifact_compile_filename("Compile Main.java and Helper.java") is None
    assert requested_artifact_compile_filename("`Compile Main.java`") is None


@pytest.mark.parametrize(
    "speech",
    (
        "«Скомпилируй Main.java в JAR»",
        "`compile Main.java into a JAR`",
        "> Build the attached Main.java as a JAR",
        "Он сказал, скомпилируй Main.java в JAR",
        "Это пример команды: compile Main.java into a JAR",
        "Если тесты зелёные, собери Main.java в JAR",
        "Не компилируй этот Java-файл",
        "Compile Main.java into a JAR, but don't do it",
        "Ты умеешь компилировать Java?",
        "Did you compile Main.java?",
        "Собери проект",
        "«Сборка Main.java»",
        "`Compile profile java21_single_source_library_jar_v1`",
        "> Компиляция Main.java",
        "Он сказал. Сборка Main.java",
        "The report says, build Main.java",
        "Если тесты зелёные, сборка Main.java",
        "If tests pass.\nBuild Main.java",
        "Compile Main.java if tests pass",
        "Не нужна сборка Main.java",
        "Сборка Main.java не нужна",
        "Build Main.java is not requested",
        "Сборка Main.java завершена",
        "Build Main.java failed yesterday",
        "Ты умеешь делать сборку Main.java?",
        "Есть ли возможность компиляции по профилю java21_single_source_library_jar_v1?",
        "Do you support builds with profile java21_single_source_library_jar_v1?",
        "Are you able to compile Main.java?",
        "Сборка проекта",
        # A syntactically direct imperative cannot override an explicit source
        # exclusion, delayed condition, approval dependency or reported suffix.
        "Compile Main.java, not this file",
        "Compile Main.java, not this one",
        "Compile Main.java, but not the attached Java source",
        "Compile Main.java, except this one",
        "Compile Main.java, not Helper.java",
        "Compile Main.java, except Helper.java",
        "Compile this, not Helper.java",
        "Build this file, exclude Helper.java",
        "Compile Main.java and Helper.java",
        "Compile this and Helper.java",
        "Build Main.java and this file",
        "Build Main.java, then build Helper.java",
        "Скомпилируй Main.java, но не этот файл",
        "Собери Main.java, только не приложенный исходник",
        "Скомпилируй Main.java, кроме этого",
        "Скомпилируй Main.java, но не Helper.java",
        "Собери Main.java, кроме Helper.java",
        "Скомпилируй это, но не Helper.java",
        "Собери этот файл, исключи Helper.java",
        "Скомпилируй Main.java и Helper.java",
        "Скомпилируй это и Helper.java",
        "Собери Main.java и этот файл",
        "When tests pass, compile Main.java",
        "After I approve, please compile Main.java",
        "Compile Main.java when tests pass",
        "Compile Main.java after I approve",
        "Compile Main.java once I say yes",
        "Compile Main.java upon approval",
        "Compile Main.java provided that tests pass",
        "Compile Main.java assuming that review passes",
        "Compile Main.java subject to approval",
        "Compile Main.java pending approval",
        "Compile Main.java only when I approve",
        "Tomorrow, compile Main.java",
        "At 17:30, compile Main.java",
        "Compile Main.java tomorrow",
        "Compile Main.java in 10 minutes",
        "Compile Main.java with my approval",
        "Когда тесты пройдут, скомпилируй Main.java",
        "После моего подтверждения собери Main.java",
        "Скомпилируй Main.java когда тесты пройдут",
        "Собери Main.java после моего подтверждения",
        "Скомпилируй Main.java как только я разрешу",
        "Собери Main.java при условии успешных тестов",
        "Скомпилируй Main.java только с моего разрешения",
        "Завтра скомпилируй Main.java",
        "В 17:30 собери Main.java",
        "Скомпилируй Main.java завтра",
        "Собери Main.java через 10 минут",
        "Compile Main.java, not now",
        "Compile Main.java, hold off",
        "Compile Main.java, skip it",
        "Compile Main.java, later",
        "Compile Main.java, I changed my mind",
        "Compile Main.java, scratch that",
        "Compile Main.java, this is not a request",
        "Compile Main.java, the build was completed yesterday",
        "Compile Main.java — build failed yesterday",
        "Скомпилируй Main.java, не сейчас",
        "Собери Main.java, позже",
        "Скомпилируй Main.java, это не команда",
        "Скомпилируй Main.java — сборка уже завершена",
        "Compile Main.java, Bob said",
        "Compile Main.java, said Bob",
        "Compile Main.java, according to Bob",
        "Compile Main.java (quote from Bob)",
        "Compile Main.java — this was Bob's request",
        "Compile Main.java — Bob's request",
        "Compile Main.java — per Bob's request",
        "Bob requested, compile Main.java",
        "Скомпилируй Main.java, сказал Боб",
        "Скомпилируй Main.java — написал коллега",
        "Собери Main.java, по словам коллеги",
        "Скомпилируй Main.java — это просьба Боба",
        "Скомпилируй Main.java — просьба Боба",
        "Скомпилируй Main.java — по просьбе Боба",
        "Боб поручил, скомпилируй Main.java",
        # Deictic and compile+delivery wording remains inert under the same
        # quote, reporting, conditional, negation and meta-language boundary.
        "«Скомпилируй это»",
        "`Build and send the JAR`",
        "> Compile this file and attach the binary",
        "Боб сказал: скомпилируй это",
        "Bob asked, build and send the JAR",
        "Если файл верный, скомпилируй это",
        "When review passes, build and attach the JAR",
        "Не компилируй это",
        "Do not build and send the JAR",
        "Compile Main.java but don't send the JAR",
        "Скомпилируй Main.java, но не присылай JAR",
        "Пример команды: скомпилируй это",
        "Example command: build and send the JAR",
        "Build and send the JAR — Bob's request",
        "Собери и пришли JAR, не сейчас",
        "Compile this example text",
        "Скомпилируй это приложение мысленно",
    ),
)
def test_inert_or_ambiguous_compile_language_is_rejected(speech: str) -> None:
    assert requests_artifact_compile(speech) is False


def test_compile_atomicity_closes_only_the_single_compile_clause() -> None:
    assert artifact_compile_request_is_atomic("Скомпилируй Main.java в JAR.") is True
    assert artifact_compile_request_is_atomic("Compile this Java source into a JAR?") is True
    assert artifact_compile_request_is_atomic("Скомпилируй это") is True
    assert artifact_compile_request_is_atomic("Build and send the JAR") is True
    assert artifact_compile_request_is_atomic("Compile Main.java and attach the binary") is True
    assert artifact_compile_request_is_atomic("Собери Main.java и пришли JAR") is True
    assert artifact_compile_request_is_atomic("Сборка Main.java в JAR.") is True
    assert (
        artifact_compile_request_is_atomic("Compile with profile java21_single_source_library_jar_v1?")
        is True
    )
    assert artifact_compile_request_is_atomic("Скомпилируй Main.java и объясни код") is False
    assert artifact_compile_request_is_atomic("Компиляция Main.java и объясни код") is False


@pytest.mark.asyncio
async def test_compile_tool_is_hidden_and_separates_private_jar_attachment(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    source = b"public class Main {}\n"
    jar = _jar()
    observed: dict[str, object] = {}

    def fake_read_owned(ctx, actor, selected_raw_id):  # noqa: ANN001
        observed["read"] = (ctx, actor, selected_raw_id)
        return _stored_source(source, raw_id=selected_raw_id)

    def fake_compile(
        content: bytes,
        filename: str,
        *,
        deadline: float,
        workspace_root: Path,
        on_started,
    ) -> tuple[bytes, dict[str, object]]:
        observed["sandbox"] = (content, filename, deadline, workspace_root)
        on_started()
        return jar, _worker_report(source, jar)

    monkeypatch.setattr(engineer_tools, "_read_owned", fake_read_owned)
    monkeypatch.setattr(engineer_tools.sandbox, "compile_java_artifact", fake_compile)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]
    assert spec.security_id == "engineer.artifact.build"
    assert spec.risk == "mutate"
    assert spec.model_visible is False

    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_BUILD)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(spec)
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    assert "engineer_compile_java" not in kernel.get_tool_names(actor)

    result = await kernel.execute(
        "engineer_compile_java",
        {
            "raw_id": raw_id,
            "expected_filename": "Main.java",
            "expected_sha256": hashlib.sha256(source).hexdigest(),
        },
        actor=actor,
    )

    assert result.success is True
    assert observed["read"][2] == raw_id  # type: ignore[index]
    sandbox_call = observed["sandbox"]
    assert sandbox_call[0:2] == (source, "Main.java")  # type: ignore[index]
    assert sandbox_call[2] > time.monotonic()  # type: ignore[index]
    assert sandbox_call[3] == settings.state_dir / "engineer-tmp"  # type: ignore[index]
    assert result.data is not None
    assert result.data["summary"] == "Java 21 compilation completed; the bounded JAR is prepared."
    report = result.data["report"]
    assert report["jar_sha256"] == hashlib.sha256(jar).hexdigest()
    assert report["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert report["jar_prepared"] is True
    assert report["sample_executed"] is False
    assert report["network"] == "none"
    encoded_public = json.dumps(result.data, sort_keys=True)
    assert raw_id not in encoded_public
    assert "public class Main" not in encoded_public

    assert result.attachment is not None
    assert result.attachment["filename"] == "Main.compiled.jar"
    assert result.attachment["mime_type"] == "application/java-archive"
    assert base64.b64decode(result.attachment["content_base64"], validate=True) == jar
    audit_reasons = [
        json.loads(str(row["after_json"] or "{}"))["reason"]
        for row in reversed(storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=20))
        if row["target_id"] == "engineer_compile_java"
    ]
    assert sorted(audit_reasons) == ["ok", "started"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_started", "expected_error", "expected_reasons"),
    (
        (False, "Engineer tool refused: compiler_busy", ["failed"]),
        (
            True,
            "Engineer tool failed: compiler_timeout",
            ["started", "failed_after_start"],
        ),
    ),
)
async def test_compile_kernel_started_audit_follows_physical_worker_attestation(
    settings,
    storage,
    work_started: bool,
    expected_error: str,
    expected_reasons: list[str],
) -> None:
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]

    async def failed_handler(  # noqa: ANN001
        *, actor, raw_id: str, expected_filename: str, expected_sha256: str
    ):
        del actor, raw_id, expected_filename, expected_sha256
        if work_started:
            mark_tool_physical_start()
        return {
            "ok": False,
            "status": "failed" if work_started else "unavailable",
            "error": "compiler_timeout" if work_started else "compiler_busy",
            "_work_started": work_started,
        }

    spec.handler = failed_handler
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_BUILD)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(spec)

    result = await kernel.execute(
        "engineer_compile_java",
        {
            "raw_id": "raw_0123456789abcdef",
            "expected_filename": "Main.java",
            "expected_sha256": "0" * 64,
        },
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
    )

    assert result.success is False
    assert result.error == expected_error
    assert result.handler_entered is True
    assert result.work_started is work_started
    audit_reasons = [
        json.loads(str(row["after_json"] or "{}"))["reason"]
        for row in reversed(storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=20))
        if row["target_id"] == "engineer_compile_java"
    ]
    assert sorted(audit_reasons) == sorted(expected_reasons)


@pytest.mark.asyncio
async def test_compile_kernel_exception_before_worker_is_safe_to_retry(
    settings,
    storage,
) -> None:
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]

    async def broken_handler(  # noqa: ANN001
        *, actor, raw_id: str, expected_filename: str, expected_sha256: str
    ):
        del actor, raw_id, expected_filename, expected_sha256
        raise RuntimeError("private compiler failure")

    spec.handler = broken_handler
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_BUILD)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(spec)

    result = await kernel.execute(
        "engineer_compile_java",
        {
            "raw_id": "raw_0123456789abcdef",
            "expected_filename": "Main.java",
            "expected_sha256": "0" * 64,
        },
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
    )

    assert result.success is False
    assert result.handler_entered is True
    assert result.work_started is False
    assert "не дошёл до запуска" in str(result.error)
    audit_reasons = [
        json.loads(str(row["after_json"] or "{}"))["reason"]
        for row in storage.list_audit_log(LEGACY_OWNER_USER_ID, limit=20)
        if row["target_id"] == "engineer_compile_java"
    ]
    assert audit_reasons == ["failed"]


@pytest.mark.asyncio
async def test_compile_tool_rejects_forged_report_or_changed_jar(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"class Main {}"
    jar = _jar()
    report = _worker_report(source, jar)
    report["jar_sha256"] = "0" * 64
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: _stored_source(source),
    )
    monkeypatch.setattr(
        engineer_tools.sandbox,
        "compile_java_artifact",
        lambda *_args, **_kwargs: (jar + b"changed", report),
    )
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id="raw_0123456789abcdef",
        expected_filename="Main.java",
        expected_sha256=hashlib.sha256(source).hexdigest(),
    )

    assert result == {
        "ok": False,
        "status": "failed",
        "error": "compiler_report_invalid",
        "_work_started": True,
    }
    assert "changed" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_filename", "expected_sha256"),
    (("Other.java", None), ("Main.java", "0" * 64)),
)
async def test_compile_tool_rechecks_code_owned_source_identity_before_spawn(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    expected_filename: str,
    expected_sha256: str | None,
) -> None:
    source = b"class Main {}"
    monkeypatch.setattr(engineer_tools, "_read_owned", lambda *_args: _stored_source(source))

    def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("sandbox must not start for a changed source identity")

    monkeypatch.setattr(engineer_tools.sandbox, "compile_java_artifact", forbidden_spawn)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id="raw_0123456789abcdef",
        expected_filename=expected_filename,
        expected_sha256=expected_sha256 or hashlib.sha256(source).hexdigest(),
    )

    assert result == {
        "ok": False,
        "status": "unavailable",
        "error": "source_identity_changed",
        "_work_started": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "work_started", "status"),
    (
        ("compiler_busy", False, "unavailable"),
        ("compiler_pid_cgroup_unbounded", False, "unavailable"),
        ("compiler_memory_cgroup_unbounded", False, "unavailable"),
        ("compiler_launch_failed", False, "failed"),
        ("compiler_timeout", True, "failed"),
    ),
)
async def test_compile_failures_preserve_entry_truth(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    work_started: bool,
    status: str,
) -> None:
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: _stored_source(b"class Main {}"),
    )

    def failed(*_args, **_kwargs):
        raise engineer_tools.sandbox.EngineerSandboxError(error, work_started=work_started)

    monkeypatch.setattr(engineer_tools.sandbox, "compile_java_artifact", failed)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id="raw_0123456789abcdef",
        expected_filename="Main.java",
        expected_sha256=hashlib.sha256(b"class Main {}").hexdigest(),
    )

    assert result == {
        "ok": False,
        "status": status,
        "error": error,
        "_work_started": work_started,
    }


@pytest.mark.asyncio
async def test_compile_respects_configured_generated_file_cap(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"class Main {}"
    jar = _jar()
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: _stored_source(source),
    )
    monkeypatch.setattr(
        engineer_tools.sandbox,
        "compile_java_artifact",
        lambda *_args, **_kwargs: (jar, _worker_report(source, jar)),
    )
    ctx = ServiceContext(
        settings=replace(settings, max_upload_bytes=len(jar) - 1),
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}["engineer_compile_java"]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id="raw_0123456789abcdef",
        expected_filename="Main.java",
        expected_sha256=hashlib.sha256(source).hexdigest(),
    )

    assert result == {
        "ok": False,
        "status": "failed",
        "error": "compiler_output_exceeds_cap",
        "_work_started": True,
    }
