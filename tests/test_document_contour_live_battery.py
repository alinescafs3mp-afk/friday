"""Offline contract tests for the isolated document live battery runner."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "document_contour_live_battery.py"


def _module():
    spec = importlib.util.spec_from_file_location("document_contour_live_battery", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_is_exactly_ten_unique_document_scenarios() -> None:
    runner = _module()

    assert runner.RUNS == 2
    assert runner.CASES == 10
    assert [item.case_id for item in runner.SCENARIOS] == [f"D{index:02d}" for index in range(1, 11)]
    assert len(runner._CASE_RUNNERS) == 10
    assert all(item.contract for item in runner.SCENARIOS)


def test_offline_self_test_never_imports_server_or_uses_production_database(monkeypatch) -> None:
    runner = _module()
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", "/sentinel/production.sqlite3")
    sys.modules.pop("friday.server", None)

    report = runner.offline_self_test()

    assert report["self_test"] == "passed"
    assert report["runs"] == 2
    assert report["cases_per_run"] == 10
    assert "friday.server" not in sys.modules
    assert os.environ["FRIDAY_DATABASE_PATH"] == "/sentinel/production.sqlite3"


def test_worker_environment_is_closed_and_every_mutable_path_is_under_run_root(tmp_path) -> None:
    runner = _module()
    run_dir = tmp_path / "isolated"
    run_dir.mkdir(mode=0o700)
    chats = tuple(9911000 + index for index in range(1, 12))

    environment = runner.build_worker_environment(run_dir, owner_chats=chats)

    assert environment["FRIDAY_DATABASE_PATH"] != os.environ.get("FRIDAY_DATABASE_PATH")
    assert Path(environment["FRIDAY_DATABASE_PATH"]).is_relative_to(run_dir)
    assert Path(environment["FRIDAY_ENV_FILE"]).is_relative_to(run_dir)
    assert environment["FRIDAY_WORKERS_ENABLED"] == "0"
    assert environment["FRIDAY_CODE_EXECUTION_ENABLED"] == "0"
    assert environment["FRIDAY_WEB_DAILY_QUOTA"] == "0"
    assert environment["FRIDAY_MCP_ENABLED"] == "1"
    assert environment["FRIDAY_TELEGRAM_OWNER_CHAT_IDS"].split(",") == [str(value) for value in chats[:-1]]
    assert environment["FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS"].split(",") == [str(value) for value in chats]


def test_every_case_has_a_distinct_database_and_private_state_root(tmp_path) -> None:
    runner = _module()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)

    states = [runner.case_state_paths(run_dir, item.case_id) for item in runner.SCENARIOS]

    assert len({item["database"] for item in states}) == 10
    assert len({item["root"] for item in states}) == 10
    assert all(item["database"].is_relative_to(item["root"]) for item in states)
    assert all(item["evidence"].is_relative_to(item["root"]) for item in states)


def test_explicit_source_env_forwards_only_sidecar_allowlist_and_never_its_path(tmp_path) -> None:
    runner = _module()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    source_env = tmp_path / "operator.env"
    source_env.write_text(
        "\n".join(
            (
                "FRIDAY_LLM_BASE_URL=http://127.0.0.1:8101/v1",
                "FRIDAY_LLM_API_KEY=synthetic-sidecar-key",
                "FRIDAY_EMBEDDINGS_MODEL=embedding-model",
                "FRIDAY_DATABASE_PATH=/sentinel/production.sqlite3",
                "FRIDAY_CODE_EXECUTION_ENABLED=1",
            )
        ),
        encoding="utf-8",
    )

    environment = runner.build_worker_environment(
        run_dir,
        owner_chats=tuple(9911000 + index for index in range(1, 12)),
        source_env_file=source_env,
    )

    assert environment["FRIDAY_LLM_BASE_URL"] == "http://127.0.0.1:8101/v1"
    assert environment["FRIDAY_LLM_API_KEY"] == "synthetic-sidecar-key"
    assert environment["FRIDAY_EMBEDDINGS_MODEL"] == "embedding-model"
    assert environment["FRIDAY_DATABASE_PATH"] != "/sentinel/production.sqlite3"
    assert environment["FRIDAY_CODE_EXECUTION_ENABLED"] == "0"
    assert environment["FRIDAY_ENV_FILE"] != str(source_env)
    assert str(source_env) not in environment.values()


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://api.example.test/v1",
        "http://203.0.113.7:8101/v1",
        "http://user:password@127.0.0.1:8101/v1",
    ),
)
def test_worker_environment_refuses_nonlocal_or_credentialed_sidecars(
    tmp_path,
    endpoint,
) -> None:
    runner = _module()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    source_env = tmp_path / "operator.env"
    source_env.write_text(f"FRIDAY_LLM_BASE_URL={endpoint}\n", encoding="utf-8")

    with pytest.raises(runner.BatteryFailure, match="friday_llm_base_url_not_local"):
        runner.build_worker_environment(
            run_dir,
            owner_chats=tuple(9911000 + index for index in range(1, 12)),
            source_env_file=source_env,
        )


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://localhost:8101/v1",
        "http://127.0.0.1:8101/v1",
        "http://10.10.0.2:8101/v1",
        "http://[::1]:8101/v1",
    ),
)
def test_worker_environment_accepts_explicit_local_sidecars(tmp_path, endpoint) -> None:
    runner = _module()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    source_env = tmp_path / "operator.env"
    source_env.write_text(f"FRIDAY_LLM_BASE_URL={endpoint}\n", encoding="utf-8")

    environment = runner.build_worker_environment(
        run_dir,
        owner_chats=tuple(9911000 + index for index in range(1, 12)),
        source_env_file=source_env,
    )

    assert environment["FRIDAY_LLM_BASE_URL"] == endpoint


def test_controller_source_env_defaults_to_its_env_without_forwarding_it(tmp_path, monkeypatch) -> None:
    runner = _module()
    source_env = tmp_path / "operator.env"
    source_env.write_text("FRIDAY_LLM_MODEL=local-model\n", encoding="utf-8")
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(source_env))

    selected = runner._controller_source_env_file("")

    assert selected == source_env.resolve()
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    environment = runner.build_worker_environment(
        run_dir,
        owner_chats=tuple(9911000 + index for index in range(1, 12)),
        source_env_file=selected,
    )
    assert environment["FRIDAY_LLM_MODEL"] == "local-model"
    assert environment["FRIDAY_ENV_FILE"] != str(source_env.resolve())
    assert str(source_env.resolve()) not in environment.values()


def test_live_probes_fail_closed_on_any_ordinary_web_tool_attempt() -> None:
    runner = _module()

    async def embed(texts, **kwargs):
        del kwargs
        return [[1.0] for _text in texts]

    async def execute(_name, _arguments, **kwargs):
        del kwargs
        return SimpleNamespace(success=True, data={})

    async def hierarchy(*args, **kwargs):
        del args, kwargs
        return {}

    app = SimpleNamespace(
        state=SimpleNamespace(
            embeddings=SimpleNamespace(embed=embed),
            hybrid_searcher=SimpleNamespace(_reranker=None),
            kernel=SimpleNamespace(execute=execute),
            agent=SimpleNamespace(_hierarchical_attachment_response=hierarchy),
            settings=SimpleNamespace(
                embeddings_base_url="http://127.0.0.1:8102/v1",
                rerank_base_url="http://127.0.0.1:8103",
            ),
            mcp=None,
        )
    )
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        with pytest.raises(runner.BatteryFailure, match="external_web_tool_attempted"):
            asyncio.run(app.state.kernel.execute("web_fetch", {"url": "https://example.test"}))
        assert probes.counts["forbidden_web_calls"] == 1
    finally:
        probes.close()


@pytest.mark.parametrize(
    ("commit", "bridge_stopped", "code"),
    (("", True, "freeze_commit_required"), ("0" * 40, False, "bridge_stop_assertion_required")),
)
def test_live_gate_fails_before_any_worker_or_model_call(commit, bridge_stopped, code) -> None:
    runner = _module()

    with pytest.raises(runner.BatteryFailure, match=code):
        runner._validate_live_gate(commit, bridge_stopped)


def test_live_gate_refuses_a_dirty_tree_even_when_commit_matches(monkeypatch) -> None:
    runner = _module()
    commit = "a" * 40

    def git_output(*args):
        if args[0] == "status":
            return " M friday/server.py"
        return commit

    monkeypatch.setattr(runner, "_git_output", git_output)

    with pytest.raises(runner.BatteryFailure, match="release_worktree_is_dirty"):
        runner._validate_live_gate(commit, True)


@pytest.mark.parametrize(
    ("worker_statuses", "expected_calls", "expected_status"),
    ((["failed", "passed"], 1, "failed"), (["passed", "passed"], 2, "passed")),
)
def test_controller_stops_a_failed_streak_but_runs_two_clean_workers(
    tmp_path,
    monkeypatch,
    worker_statuses,
    expected_calls,
    expected_status,
) -> None:
    runner = _module()
    statuses = iter(worker_statuses)
    calls = 0

    def run_worker(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        status = next(statuses)
        payload = {
            "schema": runner.WORKER_SCHEMA,
            "run_index": calls,
            "status": status,
            "failure_codes": [] if status == "passed" else ["synthetic_failure"],
            "cases": [],
        }
        return SimpleNamespace(stdout=json.dumps(payload).encode("utf-8"), returncode=0)

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner.subprocess, "run", run_worker)
    report_path = tmp_path / "closed-report.json"
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        keep_private_run_dir=False,
        report=str(report_path),
    )

    report = runner.run_controller(args)

    assert calls == expected_calls
    assert report["runs_completed"] == expected_calls
    assert report["status"] == expected_status
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == expected_status


def test_d02_offline_oracle_closes_newer_deleted_and_foreign_controls() -> None:
    runner = _module()

    class FakeStorage:
        def __init__(self) -> None:
            self.deleted: set[str] = set()

        def execute(self, _sql, parameters):  # noqa: ANN001
            self.deleted.add(str(parameters[1]))

        def commit(self) -> None:
            return None

    class FakeHarness:
        run_index = 1
        owner_id = "owner"
        jbl_id = "jbl"
        owner_chats = {"D02": 9911002}

        def __init__(self) -> None:
            self.storage = FakeStorage()
            self.refs: dict[str, tuple[str, str]] = {}
            self.reply_metadata: dict[str, object] = {}

        def document(self, filename, mime_type, content, source_ref):  # noqa: ANN001
            del filename, mime_type, content
            return {"source_ref": source_ref}

        def ingest(self, _case_id, _content, _filename, *, uploader="", source_ref=""):  # noqa: ANN001
            raw_id = "raw-foreign" if uploader == self.jbl_id else "raw-deleted"
            self.refs[source_ref] = (raw_id, uploader or self.owner_id)
            return {"raw_object_id": raw_id}

        def chat(self, _case_id, message, *, document=None, reply_source_message_id="", **_kwargs):  # noqa: ANN001
            if document is not None:
                source_ref = str(document["source_ref"])
                if "LINEAGE-T-" in source_ref:
                    self.refs[source_ref] = ("raw-target", self.owner_id)
                    return {
                        "message_id": "assistant-target",
                        "message": "LINEAGE-TARGET-1",
                        "metadata_json": {
                            "attachment_context_used": True,
                            "conversation_attachment_raw_ids": ["raw-target"],
                        },
                    }
                self.refs[source_ref] = ("raw-newer", self.owner_id)
                return {"message_id": "assistant-newer", "message": "accepted"}
            assert reply_source_message_id == "assistant-target"
            assert "процитированного ответа" in message
            self.reply_metadata = {
                "attachment_origin": "reply_assistant",
                "conversation_attachment_raw_ids": ["raw-target"],
            }
            return {"message": "LINEAGE-TARGET-1"}

        def resolve_ref(self, source_ref, *, uploader=""):  # noqa: ANN001
            row = self.refs.get(source_ref)
            if row is None or row[0] in self.storage.deleted:
                return ""
            expected_uploader = uploader or self.owner_id
            return row[0] if row[1] == expected_uploader else ""

        @staticmethod
        def message_row(result):  # noqa: ANN001
            return result

        def last_user_metadata(self, _result):  # noqa: ANN001
            return self.reply_metadata

        @staticmethod
        def case_result(case_id, _started, checks, *, counters=None):  # noqa: ANN001
            del counters
            failed = [name for name, value in checks.items() if not value]
            return {
                "case_id": case_id,
                "status": "failed" if failed else "passed",
                "failure_codes": failed,
                "checks": checks,
            }

    result = runner._case_02(FakeHarness())

    assert result["status"] == "passed"
    assert result["checks"]["controls_distinct"] is True
    assert result["checks"]["deleted_control_closed"] is True
    assert result["checks"]["foreign_control_scoped"] is True
    assert result["checks"]["reply_raw_exact"] is True
    assert result["checks"]["answer_no_decoy"] is True
    assert result["checks"]["answer_no_deleted"] is True
    assert result["checks"]["answer_no_foreign"] is True


def test_cli_self_test_is_closed_json_and_does_not_start_live_worker() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), "--self-test"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    report = json.loads(completed.stdout)
    assert report["self_test"] == "passed"
    assert report["scenario_ids"] == [f"D{index:02d}" for index in range(1, 11)]
    assert completed.stderr == ""


def test_release_runner_pins_and_attests_friday_import_origin(tmp_path) -> None:
    release_root = tmp_path / "release"
    release_tools = release_root / "tools"
    release_package = release_root / "friday"
    decoy_root = tmp_path / "dirty-editable"
    decoy_package = decoy_root / "friday"
    release_tools.mkdir(parents=True)
    release_package.mkdir()
    decoy_package.mkdir(parents=True)
    copied_runner = release_tools / RUNNER.name
    shutil.copy2(RUNNER, copied_runner)
    (release_package / "__init__.py").write_text("ORIGIN = 'release'\n", encoding="utf-8")
    (decoy_package / "__init__.py").write_text("ORIGIN = 'dirty'\n", encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(decoy_root)}

    direct = subprocess.run(
        [sys.executable, str(copied_runner), "--self-test"],
        cwd=release_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert direct.returncode == 0
    assert json.loads(direct.stdout)["self_test"] == "passed"

    preload = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy, sys; sys.path.insert(0, sys.argv[2]); "
                "import friday; runpy.run_path(sys.argv[1], run_name='frozen_probe')"
            ),
            str(copied_runner),
            str(decoy_root),
        ],
        cwd=release_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert preload.returncode != 0
    assert "package origin is outside the frozen release root" in preload.stderr
