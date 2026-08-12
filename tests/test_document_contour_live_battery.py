"""Offline contract tests for the isolated document live battery runner."""

from __future__ import annotations

import asyncio
import base64
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


def test_d03_prompt_authorizes_approximate_filename_navigation_without_becoming_exact() -> None:
    from friday.agent_runtime import (
        _attachment_filename_mentions,
        _attachment_reference_kind,
        _descriptive_filename_selector,
        _filename_clue_ids,
    )

    runner = _module()
    target_id = "raw_" + "a" * 32
    decoy_id = "raw_" + "b" * 32
    catalog = [
        {
            "raw_object_id": target_id,
            "filename": "Список комендатур Луганской Народной Республики 2026.odt",
        },
        {"raw_object_id": decoy_id, "filename": "СУВ 5_222.xlsx"},
    ]

    assert _attachment_reference_kind(runner._D03_PROMPT) == "explicit"
    assert _descriptive_filename_selector(runner._D03_PROMPT) is True
    assert _attachment_filename_mentions(runner._D03_PROMPT) == ()
    assert _filename_clue_ids(runner._D03_PROMPT, catalog) == ([target_id], 1)


def test_offline_self_test_never_imports_server_or_uses_production_database(monkeypatch) -> None:
    runner = _module()
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", "/sentinel/production.sqlite3")
    sys.modules.pop("friday.server", None)

    report = runner.offline_self_test()

    assert report["self_test"] == "passed"
    assert report["runs"] == 2
    assert report["cases_per_run"] == 10
    assert report["identity_count"] == 40
    assert report["identity_disjoint"] is True
    assert report["prompt_variants"] == 2
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


def test_two_invocations_and_both_runs_have_disjoint_fixture_identities(tmp_path) -> None:
    runner = _module()
    run_ids = ("01" * 32, "02" * 32)
    manifests = [runner._scenario_manifest() for _run_id in run_ids]
    identities = [
        runner._case_identity(run_id, run_index, scenario.case_id)
        for run_id in run_ids
        for run_index in range(1, runner.RUNS + 1)
        for scenario in runner.SCENARIOS
    ]

    assert manifests[0] == manifests[1]
    assert len(identities) == 40
    assert len({identity.cache_prefix for identity in identities}) == 40
    assert len({identity.marker("FACT") for identity in identities}) == 40
    assert len({identity.source_ref("SOURCE") for identity in identities}) == 40
    assert len({identity.filename("fixture", "odt") for identity in identities}) == 40
    assert len({f"document-live:{identity.token('chat-ref:1')}" for identity in identities}) == 40
    assert len({int(identity.token("message:1", length=15), 16) for identity in identities}) == 40
    assert len({runner.case_state_paths(tmp_path, item.case_id, item)["root"] for item in identities}) == 40
    prompts = {
        runner._scoped_prompt(SimpleNamespace(identity=identity), "natural", "Обобщи документ.")
        for identity in identities
    }
    assert prompts.issubset({"Обобщи документ.", "Пожалуйста.\nОбобщи документ."})
    assert all("Контекст проверки" not in prompt for prompt in prompts)
    assert {identity.prompt_variant("natural", 2) for identity in identities}.issubset({0, 1})
    assert runner._scoped_prompt(SimpleNamespace(identity=identities[0]), "bare", "") == ""
    all_chats = {
        chat
        for run_id in run_ids
        for run_index in range(1, runner.RUNS + 1)
        for chat in runner._run_owner_chats(run_id, run_index)
    }
    assert len(all_chats) == 44
    assert all(run_id not in json.dumps(sorted(prompts)) for run_id in run_ids)
    full_conversation_prompts = {
        "\n".join(
            (
                identity.filename("fixture", "odt"),
                identity.marker("FACT"),
                runner._scoped_prompt(SimpleNamespace(identity=identity), "natural", "Обобщи документ."),
            )
        )
        for identity in identities
    }
    assert len(full_conversation_prompts) == 40


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
        return ({"bounded": True}, True)

    async def late_make_file(*args, **kwargs):
        del args, kwargs
        return None

    app = SimpleNamespace(
        state=SimpleNamespace(
            embeddings=SimpleNamespace(embed=embed),
            hybrid_searcher=SimpleNamespace(_reranker=None),
            kernel=SimpleNamespace(execute=execute),
            agent=SimpleNamespace(
                _build_attachment_hierarchy_bundle=hierarchy,
                _file_for_a_request_that_wanted_one=late_make_file,
            ),
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
        built = asyncio.run(app.state.agent._build_attachment_hierarchy_bundle())
        assert built == ({"bounded": True}, True)
        assert probes.counts["hierarchy_calls"] == 1
        assert probes.counts["hierarchy_complete"] == 1
        assert probes.counts["llm_chat_attempts"] == 0
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
    worker_run_ids: list[str] = []

    def run_worker(*args, **kwargs):
        nonlocal calls
        del args
        calls += 1
        run_id = kwargs["env"][runner._RUN_ID_ENV]
        worker_run_ids.append(run_id)
        status = next(statuses)
        payload = {
            "schema": runner.WORKER_SCHEMA,
            "run_index": calls,
            "run_id_hash": runner._run_id_hash(run_id),
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
    assert len(set(worker_run_ids)) == 1
    assert report["runs_completed"] == expected_calls
    assert report["status"] == expected_status
    assert report["run_id_hash"] == runner._run_id_hash(worker_run_ids[0])
    assert worker_run_ids[0] not in json.dumps(report)
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


def test_d05_fixture_never_crosses_an_open_direct_transaction_with_ingestion() -> None:
    runner = _module()

    class FakeStorage:
        def __init__(self) -> None:
            self.pending = False
            self.updates: list[tuple[str, str]] = []

        def execute(self, _sql, parameters):  # noqa: ANN001
            self.pending = True
            self.updates.append((str(parameters[0]), str(parameters[1])))

        def commit(self) -> None:
            self.pending = False

        @staticmethod
        def get_searchable_file_sources(
            _tenant,
            raw_ids,
            *,
            uploaded_by,
            include_content,
            limit,
        ):  # noqa: ANN001
            assert uploaded_by == "jbl"
            assert include_content is False
            assert limit == 3
            return [{"id": raw_id} for raw_id in raw_ids]

    class FakeHarness:
        run_index = 1
        owner_id = "owner"
        jbl_id = "jbl"
        jbl_chat = 9911011

        def __init__(self) -> None:
            self.storage = FakeStorage()
            self.selected = ["raw-jbl-3", "raw-jbl-2", "raw-jbl-1"]
            self.refs: dict[str, str] = {}

        @staticmethod
        def document(filename, mime_type, content, source_ref):  # noqa: ANN001
            del filename, mime_type, content
            return {"source_ref": source_ref}

        def chat(self, _case_id, _message, *, document=None, **_kwargs):  # noqa: ANN001
            assert self.storage.pending is False, "HTTP turn crossed an open fixture transaction"
            if document is not None:
                index = str(document["source_ref"]).rsplit("-", 1)[-1]
                self.refs[str(document["source_ref"])] = f"raw-jbl-{index}"
                # The public API intentionally omits the private Raw id.
                return {"file_ingestion": {"persisted": True}}
            return {"message": "JBL-FIRST-1 JBL-SECOND-1 JBL-THIRD-1"}

        def resolve_ref(self, source_ref, *, uploader=None):  # noqa: ANN001
            assert uploader == self.jbl_id
            return self.refs.get(str(source_ref), "")

        def ingest(self, *_args, **_kwargs):  # noqa: ANN001
            assert self.storage.pending is False, "ingestion crossed an open fixture transaction"
            return {"raw_object_id": "raw-foreign"}

        def last_user_metadata(self, _result):  # noqa: ANN001
            return {"conversation_attachment_raw_ids": self.selected}

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

    harness = FakeHarness()
    result = runner._case_05(harness)

    assert result["status"] == "passed"
    assert harness.storage.updates[:3] == [
        ("2026-08-07T09:00:00+00:00", "raw-jbl-1"),
        ("2026-08-09T09:00:00+00:00", "raw-jbl-2"),
        ("2026-08-11T09:00:00+00:00", "raw-jbl-3"),
    ]
    assert result["checks"] == {
        "all_expected_ids": True,
        "uploader_reauthorized": True,
        "foreign_excluded": True,
        "answer_all_markers": True,
        "answer_no_foreign": True,
    }


def test_d09_oracle_counts_only_files_and_reauthorizes_public_persistence(tmp_path) -> None:
    runner = _module()

    class CountRow(dict):
        def fetchone(self):
            return self

    class FakeStorage:
        file_count = 0
        text_count = 0

        def execute(self, sql, _parameters):  # noqa: ANN001
            assert "content_type='file'" in sql
            return CountRow(count=self.file_count)

    class FakeHarness:
        run_index = 1
        owner_id = "owner"

        def __init__(self, *, hidden_missing_persistence: bool = False) -> None:
            self.storage = FakeStorage()
            self.run_dir = tmp_path
            self.raw_evidence: list[dict[str, object]] = []
            self.persisted = False
            self.hidden_missing_persistence = hidden_missing_persistence
            self.missing_persisted = False

        @staticmethod
        def document(filename, mime_type, content, source_ref):  # noqa: ANN001
            del filename, mime_type, content
            return {"source_ref": source_ref}

        def chat(self, _case_id, _message, *, document=None, archive_password=None, **_kwargs):  # noqa: ANN001
            assert document is not None
            request = {"archive_password": archive_password} if archive_password is not None else {}
            self.raw_evidence.append({"case_id": "D09", "request": request})
            if archive_password is None:
                if self.hidden_missing_persistence:
                    self.storage.file_count += 1
                    self.missing_persisted = True
                return {
                    "archive_password_required": True,
                    "file_ingestion": {"persisted": False},
                }
            self.persisted = True
            self.storage.file_count += 1
            # Ordinary chat provenance may create non-file Raw rows. The D09
            # contract is exactly one archive file, not one row of every kind.
            self.storage.text_count += 1
            return {
                "message": "ARCHIVE-NESTED-1",
                # Public chat intentionally does not expose raw_object_id.
                "file_ingestion": {"persisted": True},
            }

        def resolve_ref(self, source_ref, **_kwargs):  # noqa: ANN001
            assert source_ref == "telegram-file:ENCRYPTED-1"
            if self.persisted:
                return "raw-archive"
            return "raw-hidden" if self.missing_persisted else ""

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

    harness = FakeHarness()
    result = runner._case_09(harness)

    assert result["status"] == "passed"
    assert result["checks"]["missing_not_persisted"] is True
    assert result["checks"]["success_persisted_once"] is True
    assert harness.storage.file_count == 1
    assert harness.storage.text_count == 1
    assert all("archive_password" not in record["request"] for record in harness.raw_evidence)

    hidden = FakeHarness(hidden_missing_persistence=True)
    hidden_result = runner._case_09(hidden)
    assert hidden_result["status"] == "failed"
    assert hidden_result["checks"]["challenge_required"] is True
    assert hidden_result["checks"]["missing_not_persisted"] is False
    assert hidden_result["checks"]["success_persisted_once"] is False
    assert hidden.storage.file_count == 2


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        ("", ""),
        ("wrong-filename", "regular_file_delivered"),
        ("missing-signatory", "regular_file_grounded"),
        ("missing-regular-line", "regular_file_exact_four_lines"),
        ("extra-regular-line", "regular_file_exact_four_lines"),
        ("one-line-regular", "regular_file_exact_four_lines"),
        ("swapped-regular-lines", "regular_file_exact_four_lines"),
        ("extra-mcp-line", "mcp_exact_content"),
        ("duplicate-inline", "mcp_no_duplicate_chat_file"),
    ],
)
def test_d10_oracles_require_exact_delivery_requisites_and_mcp_shape(
    tmp_path,
    mutation: str,
    expected_failure: str,
) -> None:
    runner = _module()
    from friday.reports import render, spec_from_payload

    number = "17-ДСП/1"
    marker = "META-EXPORT-1"
    body_date = "10 августа 2026 года"
    required_lines = [
        "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
        f"ПРИКАЗ № {number}",
        f"Дата документа: {body_date}",
        "Подписант: начальник отдела Иван Иванович Иванов",
    ]
    if mutation == "missing-signatory":
        required_lines.pop()
    if mutation == "missing-regular-line":
        required_lines.pop(2)
    if mutation == "extra-regular-line":
        required_lines.append("Лишняя строка")
    if mutation == "swapped-regular-lines":
        required_lines[0], required_lines[1] = required_lines[1], required_lines[0]
    report_lines = [" ".join(required_lines)] if mutation == "one-line-regular" else required_lines
    report = render(
        "docx",
        spec_from_payload(
            "Синтетический экспорт",
            "",
            [{"kind": "text", "text": line} for line in report_lines],
        ),
    )

    class FakeProbes:
        @staticmethod
        def snapshot():
            return {"workspace_create_kernel": 0, "workspace_create_mcp": 0}

        @staticmethod
        def delta(_before):  # noqa: ANN001
            return {"workspace_create_kernel": 1, "workspace_create_mcp": 1}

    class FakeKernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001
            del actor
            assert name == "workspace_create"
            assert arguments["filename"] == "mcp-metadata.txt"
            return SimpleNamespace(success=False)

    class FakePortal:
        @staticmethod
        def call(function):  # noqa: ANN001
            return asyncio.run(function())

    class FakeHarness:
        run_index = 1
        owner_id = "owner"

        def __init__(self) -> None:
            outbox = tmp_path / mutation / "outbox"
            outbox.mkdir(parents=True)
            self.settings = SimpleNamespace(mcp_workspace_outbox_dir=outbox)
            self.probes = FakeProbes()
            self.app = SimpleNamespace(state=SimpleNamespace(kernel=FakeKernel()))
            self.client = SimpleNamespace(portal=FakePortal())
            self.calls = 0

        @staticmethod
        def document(filename, mime_type, content, source_ref):  # noqa: ANN001
            del filename, mime_type, content, source_ref
            return {}

        @staticmethod
        def resolve_ref(_source_ref):  # noqa: ANN001
            return "raw_authorized"

        def chat(self, _case_id, _message, **_kwargs):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return {
                    "message": "\n".join(
                        (
                            "Заголовок: Технический заголовок контейнера",
                            "Автор: Редактор Контейнера",
                            "Дата создания в свойствах контейнера: 2022-02-03T04:05:06+00:00",
                            "Дата изменения в свойствах контейнера: 2022-02-04T05:06:07+00:00",
                            "Циклы редактирования: 7",
                            "Страницы: 3",
                            "Абзацы: 8",
                            "Слова: 44",
                            *required_lines,
                            f"Контрольный маркер: {marker}",
                        )
                    )
                }
            if self.calls == 2:
                filename = "wrong.docx" if mutation == "wrong-filename" else "metadata-export.docx"
                return {
                    "files": [
                        {
                            "filename": filename,
                            "mime_type": (
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                            ),
                            "content_base64": base64.b64encode(report).decode("ascii"),
                        }
                    ]
                }
            outbox = Path(str(self.settings.mcp_workspace_outbox_dir)) / "mcp-metadata.txt"
            lines = [number, marker]
            if mutation == "extra-mcp-line":
                lines.append("unrequested")
            outbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
            outbox.chmod(0o600)
            return {
                "tools_used": ["workspace_create"],
                "files": ([{"filename": "duplicate.docx"}] if mutation == "duplicate-inline" else []),
            }

        @staticmethod
        def case_result(case_id, _started, checks, counters=None):  # noqa: ANN001
            del counters
            failed = [name for name, value in checks.items() if not value]
            return {
                "case_id": case_id,
                "status": "failed" if failed else "passed",
                "failure_codes": failed,
                "checks": checks,
            }

    result = runner._case_10(FakeHarness())

    if expected_failure:
        assert result["status"] == "failed"
        assert result["checks"][expected_failure] is False
    else:
        assert result["status"] == "passed", result
        assert all(result["checks"].values())
        subturns = result["diagnostics"]["subturns"]
        assert tuple(subturns) == ("metadata", "regular", "mcp")
        assert subturns["regular"]["reply_ref_bound_before"] is True
        assert subturns["mcp"]["reply_ref_bound_before"] is True


def test_d10_subturn_diagnostic_is_closed_and_content_free(monkeypatch) -> None:
    runner = _module()
    secret = "PRIVATE-SYNTHETIC-CONTENT"
    monkeypatch.setattr(runner.time, "monotonic", lambda: 10.125)

    diagnostic = runner._closed_d10_subturn(
        {
            "message": secret,
            "files": [{"filename": secret}],
            "tools_used": ["workspace_create"],
            "context": {"llm_failed": True, "private": secret},
        },
        10.0,
        {
            "llm_chat_attempts": 2,
            "late_make_file_attempts": 1,
            "workspace_create_kernel_attempts": 1,
            "workspace_create_mcp_attempts": 1,
        },
        reply_ref_bound=True,
    )

    assert diagnostic == {
        "duration_ms": 125,
        "http_returned": True,
        "llm_failed": True,
        "files_count": 1,
        "tools_count": 1,
        "attempts": {
            "llm_chat_attempts": 2,
            "late_make_file_attempts": 1,
            "workspace_create_kernel_attempts": 1,
            "workspace_create_mcp_attempts": 1,
        },
        "reply_ref_bound_before": True,
    }
    assert secret not in json.dumps(diagnostic, sort_keys=True)


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
