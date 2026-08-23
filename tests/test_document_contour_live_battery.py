"""Offline contract tests for the isolated document live battery runner."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


def _signal_current_thread(runner, selected_signal) -> None:  # noqa: ANN001
    runner.signal.pthread_kill(threading.get_ident(), selected_signal)


def test_manifest_is_exactly_ten_unique_document_scenarios() -> None:
    runner = _module()

    assert runner.RUNS == 2
    assert runner.CASES == 10
    assert [item.case_id for item in runner.SCENARIOS] == [f"D{index:02d}" for index in range(1, 11)]
    assert len(runner._CASE_RUNNERS) == 10
    assert all(item.contract for item in runner.SCENARIOS)
    assert runner.LIVE_CASE_IDS == ("D06", "D07", "D08")
    assert runner.LIVE_CASES == 3
    assert [item.case_id for item in runner.LIVE_SCENARIOS] == list(runner.LIVE_CASE_IDS)
    assert len(runner._LIVE_CASE_RUNNERS) == 3


def test_d07_scan_fixture_roundtrip_keeps_the_complete_secret_inside_page() -> None:
    import pypdf
    from PIL import Image, ImageDraw, ImageOps

    runner = _module()
    marker = "SCAN-PAGE-FIVE-" + "F" * 12
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    available_width = (
        runner._SCAN_FIXTURE_WIDTH - runner._SCAN_FIXTURE_TEXT_X - runner._SCAN_FIXTURE_RIGHT_MARGIN
    )
    marker_font = runner._scan_fixture_font(marker, font_path, max_width=available_width)
    left, _top, right, _bottom = marker_font.getbbox(marker)
    assert right - left <= available_width

    reader = pypdf.PdfReader(io.BytesIO(runner._scan_pdf(marker)))
    assert len(reader.pages) == 5
    images = list(reader.pages[-1].images)
    assert len(images) == 1
    page = images[0].image.convert("L")
    # The complete five-page raster fixture fits into two production batches,
    # both admissible in one concurrency wave.  This keeps the live canary
    # semantic (page-five OCR) instead of repeating five oversized prompts.
    assert page.width == runner._SCAN_FIXTURE_WIDTH
    assert page.height == runner._SCAN_FIXTURE_HEIGHT
    render_width = math.ceil(runner._SCAN_PDF_WIDTH * 2.5)
    render_height = math.ceil(runner._SCAN_PDF_HEIGHT * 2.5)
    assert render_width * render_height * 3 <= 1_048_576
    assert render_width * render_height * 4 > 1_048_576
    marker_band = page.crop(
        (
            0,
            runner._SCAN_SECRET_VALUE_Y,
            page.width,
            runner._SCAN_SECRET_VALUE_Y + runner._SCAN_FIXTURE_FONT_SIZE + 32,
        )
    )
    ink = ImageOps.invert(marker_band).getbbox()
    expected_page = Image.new("L", page.size, "white")
    ImageDraw.Draw(expected_page).text(
        (runner._SCAN_FIXTURE_TEXT_X, runner._SCAN_SECRET_VALUE_Y),
        marker,
        fill="black",
        font=marker_font,
    )
    expected_ink = ImageOps.invert(
        expected_page.crop(
            (
                0,
                runner._SCAN_SECRET_VALUE_Y,
                page.width,
                runner._SCAN_SECRET_VALUE_Y + runner._SCAN_FIXTURE_FONT_SIZE + 32,
            )
        )
    ).getbbox()
    assert ink is not None
    assert ink == expected_ink
    assert ink[0] >= runner._SCAN_FIXTURE_TEXT_X
    assert ink[2] <= page.width - runner._SCAN_FIXTURE_RIGHT_MARGIN


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


def test_d04_answer_oracle_checks_the_requested_identity_not_the_private_nonce() -> None:
    runner = _module()

    assert runner._d04_answer_has_requested_identity(
        "В подразделении РЭБ командиром взвода числится капитан Орлов."
    )
    assert not runner._d04_answer_has_requested_identity(
        "В подразделении РЭБ командиром взвода числится капитан Соколов."
    )
    assert not runner._d04_answer_has_requested_identity("Командиром указан капитан Орлов.")
    assert not runner._d04_answer_has_requested_identity("Капитан Орлов не является командиром взвода РЭБ.")
    assert not runner._d04_answer_has_requested_identity("Командиром взвода РЭБ числится не капитан Орлов.")


def test_offline_self_test_never_imports_server_or_uses_production_database(monkeypatch) -> None:
    runner = _module()
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", "/sentinel/production.sqlite3")
    sys.modules.pop("friday.server", None)

    report = runner.offline_self_test()

    assert report["self_test"] == "passed"
    assert report["runs"] == 2
    assert report["cases_per_run"] == 10
    assert report["live_cases_per_run"] == 3
    assert report["live_scenario_ids"] == ["D06", "D07", "D08"]
    assert report["identity_count"] == 40
    assert report["identity_disjoint"] is True
    assert report["prompt_variants"] == 2
    assert "friday.server" not in sys.modules
    assert os.environ["FRIDAY_DATABASE_PATH"] == "/sentinel/production.sqlite3"


def test_worker_environment_is_closed_and_every_mutable_path_is_under_run_root(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    run_dir = tmp_path / "isolated"
    run_dir.mkdir(mode=0o700)
    chats = tuple(9911000 + index for index in range(1, 12))
    monkeypatch.setenv("PATH", "/private/substituted-bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private/substituted-loader")

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
    assert "PATH" not in environment
    assert "LD_LIBRARY_PATH" not in environment


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


def _complete_operator_model_environment(runner) -> dict[str, str]:
    values = {key: f"operator-{index}" for index, key in enumerate(sorted(runner._MODEL_ENV_ALLOWLIST))}
    values.update(
        {
            "FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8101/v1",
            "FRIDAY_EMBEDDINGS_BASE_URL": "http://127.0.0.1:8102/v1",
            "FRIDAY_RERANK_BASE_URL": "http://127.0.0.1:8103/v1",
        }
    )
    return values


def test_operator_model_env_only_uses_complete_inherited_allowlist_without_file_probe(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    inherited = _complete_operator_model_environment(runner)
    for key in runner._MODEL_ENV_ALLOWLIST:
        monkeypatch.delenv(key, raising=False)
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    sentinel = tmp_path / "must-not-be-read.env"
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(sentinel))
    monkeypatch.setattr(
        runner,
        "_load_env_file_values",
        lambda _path: pytest.fail("operator mode must not read an env file"),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)

    environment = runner.build_worker_environment(
        run_dir,
        owner_chats=tuple(9911000 + index for index in range(1, 12)),
        operator_model_env_only=True,
    )

    assert {key: environment[key] for key in runner._MODEL_ENV_ALLOWLIST} == inherited
    assert str(sentinel) not in environment.values()


def test_operator_model_env_only_fails_closed_on_missing_key_without_file_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    inherited = _complete_operator_model_environment(runner)
    missing = sorted(runner._MODEL_ENV_ALLOWLIST)[0]
    for key in runner._MODEL_ENV_ALLOWLIST:
        monkeypatch.delenv(key, raising=False)
    for key, value in inherited.items():
        if key != missing:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        runner,
        "_load_env_file_values",
        lambda _path: pytest.fail("missing inherited state must not fall back to a file"),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)

    with pytest.raises(runner.BatteryFailure, match="operator_model_env_only_incomplete"):
        runner.build_worker_environment(
            run_dir,
            owner_chats=tuple(9911000 + index for index in range(1, 12)),
            operator_model_env_only=True,
        )


def test_operator_model_env_only_rejects_a_source_path_without_probing_it(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    monkeypatch.setattr(
        runner,
        "_load_env_file_values",
        lambda _path: pytest.fail("conflicting source path must not be probed"),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)

    with pytest.raises(
        runner.BatteryFailure,
        match="operator_model_env_only_source_env_file_conflict",
    ):
        runner.build_worker_environment(
            run_dir,
            owner_chats=tuple(9911000 + index for index in range(1, 12)),
            source_env_file=tmp_path / "operator.env",
            operator_model_env_only=True,
        )


def test_operator_model_env_only_controller_bypasses_all_source_env_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    monkeypatch.setattr(runner, "_validate_live_gate", lambda _commit, _stopped: "a" * 40)
    monkeypatch.setattr(
        runner,
        "_controller_source_env_file",
        lambda _value: pytest.fail("operator mode must bypass source env resolution"),
    )
    monkeypatch.setenv("FRIDAY_ENV_FILE", str(tmp_path / "ambient.env"))
    base = {
        "freeze_commit": "a" * 40,
        "bridge_stopped": True,
        "operator_model_env_only": True,
        "inter_run_barrier_dir": "",
    }

    with pytest.raises(
        runner.BatteryFailure,
        match="operator_model_env_only_source_env_file_conflict",
    ):
        runner.run_controller(SimpleNamespace(**base, source_env_file="explicit.env"))
    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_required"):
        runner.run_controller(SimpleNamespace(**base, source_env_file=""))

    parser = runner.build_parser()
    assert parser.parse_args([]).operator_model_env_only is False
    assert parser.parse_args(["--operator-model-env-only"]).operator_model_env_only is True


class _ProbeLLM:
    enabled = True

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay

    async def chat(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(self.delay)
        return {"content": "ok"}


class _ProbeAgent:
    def __init__(self, llm: _ProbeLLM, *, planned: int = 0, overlap: bool = False) -> None:
        self.llm = llm
        self.planned = planned
        self.overlap = overlap

    async def _attachment_primary_chat(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return await self.llm.chat([])

    async def _reduce_attachment_map_records(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return ([], True)

    async def _build_attachment_hierarchy_bundle(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        calls = [self.llm.chat([]) for _index in range(self.planned)]
        if self.overlap:
            await asyncio.gather(*calls)
        else:
            for call in calls:
                await call
        return (SimpleNamespace(chunks_planned=self.planned), True)

    async def _hierarchical_attachment_response(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return await self._attachment_primary_chat()

    async def _verify_response(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return await self.llm.chat([])

    @staticmethod
    async def _file_for_a_request_that_wanted_one(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


def _probe_app(*, planned: int = 0, overlap: bool = False, delay: float = 0.0):
    async def embed(texts, **kwargs):  # noqa: ANN001
        del kwargs
        return [[1.0] for _text in texts]

    async def execute(_name, _arguments, **kwargs):  # noqa: ANN001
        del kwargs
        return SimpleNamespace(success=True, data={})

    llm = _ProbeLLM(delay=delay)
    return SimpleNamespace(
        state=SimpleNamespace(
            embeddings=SimpleNamespace(embed=embed),
            hybrid_searcher=SimpleNamespace(_reranker=None),
            kernel=SimpleNamespace(execute=execute),
            agent=_ProbeAgent(llm, planned=planned, overlap=overlap),
            settings=SimpleNamespace(
                embeddings_base_url="http://127.0.0.1:8102/v1",
                rerank_base_url="http://127.0.0.1:8103",
            ),
            mcp=None,
        )
    )


def _closed_generation_counts(
    runner,
    *,
    direct: int,
    hierarchy: int,
    map_calls: int,
    reduce: int,
    final: int,
    verifier: int,
):
    counts = {key: 0 for key in runner._GENERATION_TELEMETRY_KEYS}
    counts.update(
        {
            "hierarchy_calls": hierarchy,
            "hierarchy_complete": hierarchy,
            "map_planned": map_calls,
            "map_peak_active": int(map_calls > 0),
        }
    )
    for stage, value in (
        ("direct_synthesis", direct),
        ("map", map_calls),
        ("reduce", reduce),
        ("final_synthesis", final),
        ("verifier", verifier),
    ):
        counts[f"{stage}_started"] = value
        counts[f"{stage}_completed"] = value
    counts["llm_chat_attempts"] = direct + map_calls + reduce + final + verifier
    return counts


def test_live_probes_fail_closed_on_any_ordinary_web_tool_attempt() -> None:
    runner = _module()
    app = _probe_app()
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        with pytest.raises(runner.BatteryFailure, match="external_web_tool_attempted"):
            asyncio.run(app.state.kernel.execute("web_fetch", {"url": "https://example.test"}))
        assert probes.counts["forbidden_web_calls"] == 1
        built = asyncio.run(app.state.agent._build_attachment_hierarchy_bundle())
        assert built[1] is True
        assert built[0].chunks_planned == 0
        assert probes.counts["hierarchy_calls"] == 1
        assert probes.counts["hierarchy_complete"] == 1
        assert probes.counts["map_planned"] == 0
        assert probes.counts["llm_chat_attempts"] == 0
    finally:
        probes.close()


def test_live_probes_fail_closed_when_an_authoritative_stage_boundary_is_missing() -> None:
    runner = _module()
    app = _probe_app()
    app.state.agent._verify_response = None

    with pytest.raises(runner.BatteryFailure, match="generation_stage_telemetry_unavailable"):
        runner.LiveProbes(app).install()


def test_sequential_map_telemetry_has_one_active_leaf_and_satisfies_dynamic_plan() -> None:
    runner = _module()
    app = _probe_app(planned=3, overlap=False)
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        before = probes.snapshot()
        asyncio.run(app.state.agent._build_attachment_hierarchy_bundle())
        delta = probes.delta(before)
        delta["final_synthesis_started"] = 1
        delta["final_synthesis_completed"] = 1
        delta["llm_chat_attempts"] += 1
        checks = runner._generation_integrity_checks(delta, hierarchy_required=True)

        assert delta["map_planned"] == 3
        assert delta["map_started"] == delta["map_completed"] == 3
        assert delta["map_peak_active"] == 1
        assert all(checks.values()), checks
    finally:
        probes.close()


def test_overlapping_map_calls_report_peak_two_and_fail_the_release_budget() -> None:
    runner = _module()
    app = _probe_app(planned=2, overlap=True, delay=0.001)
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        before = probes.snapshot()
        asyncio.run(app.state.agent._build_attachment_hierarchy_bundle())
        delta = probes.delta(before)
        delta["final_synthesis_started"] = 1
        delta["final_synthesis_completed"] = 1
        delta["llm_chat_attempts"] += 1
        checks = runner._generation_integrity_checks(delta, hierarchy_required=True)

        assert delta["map_started"] == delta["map_completed"] == 2
        assert delta["map_peak_active"] == 2
        assert checks["hierarchy_route_complete"] is True
        assert checks["map_concurrency_within_limit"] is False
    finally:
        probes.close()


def test_direct_final_and_verifier_are_classified_by_runtime_boundaries() -> None:
    runner = _module()
    app = _probe_app()
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        asyncio.run(app.state.agent._attachment_primary_chat())
        asyncio.run(app.state.agent._hierarchical_attachment_response())
        asyncio.run(app.state.agent._verify_response())

        assert probes.counts["direct_synthesis_started"] == 1
        assert probes.counts["final_synthesis_started"] == 1
        assert probes.counts["verifier_started"] == 1
        assert probes.counts["unclassified_started"] == 0
    finally:
        probes.close()


@pytest.mark.parametrize("phase", ("admission", "submitted"))
def test_live_probes_publish_only_the_content_free_deadline_phase(phase: str) -> None:
    from friday.agent_runtime.llm import LLMDeadlineError

    runner = _module()
    app = _probe_app()

    async def deadline_failure(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise LLMDeadlineError(phase)

    app.state.agent.llm.chat = deadline_failure
    probes = runner.LiveProbes(app)
    probes.install()
    try:
        with pytest.raises(LLMDeadlineError) as raised:
            asyncio.run(app.state.agent._attachment_primary_chat())
        assert raised.value.phase == phase
        assert probes.counts[f"generation_{phase}_timeouts"] == 1
        assert probes.counts["direct_synthesis_started"] == 1
        assert probes.counts["direct_synthesis_completed"] == 0
        assert probes.counts["direct_synthesis_failures"] == 1
        assert probes.counts["direct_synthesis_cancellations"] == 0
    finally:
        probes.close()


@pytest.mark.parametrize(
    ("route", "mutation", "expected_failed_check"),
    (
        ("D06", "direct_missing", "direct_route_complete"),
        ("D06", "hierarchy_unexpected", "direct_route_complete"),
        ("D08", "final_missing", "hierarchy_route_complete"),
        ("D08", "hierarchy_incomplete", "hierarchy_route_complete"),
        ("D08", "planned_clipped", "hierarchy_route_complete"),
        ("D08", "counter_missing", "generation_telemetry_complete"),
        ("D08", "map_failure", "generation_failures_zero"),
        ("D08", "map_cancellation", "generation_cancellations_zero"),
        ("D08", "unclassified_extra", "unclassified_generations_zero"),
        ("D08", "total_excess", "generation_attempts_accounted"),
        ("D08", "peak_excess", "map_concurrency_within_limit"),
    ),
)
def test_generation_integrity_mutations_fail_closed(route, mutation, expected_failed_check) -> None:
    runner = _module()
    d06 = route == "D06"
    counts = _closed_generation_counts(
        runner,
        direct=1 if d06 else 0,
        hierarchy=0 if d06 else 1,
        map_calls=0 if d06 else 2,
        reduce=0,
        final=0 if d06 else 1,
        verifier=0 if d06 else 1,
    )
    stage_mutations = {
        "direct_missing": ("direct_synthesis", 0),
        "final_missing": ("final_synthesis", 0),
    }
    if mutation in stage_mutations:
        stage, value = stage_mutations[mutation]
        old = counts[f"{stage}_started"]
        counts[f"{stage}_started"] = value
        counts[f"{stage}_completed"] = value
        counts["llm_chat_attempts"] += value - old
    elif mutation == "hierarchy_unexpected":
        counts["hierarchy_calls"] = 1
        counts["hierarchy_complete"] = 1
    elif mutation == "hierarchy_incomplete":
        counts["hierarchy_complete"] = 0
    elif mutation == "planned_clipped":
        counts["map_planned"] = 1
    elif mutation == "counter_missing":
        counts.pop("map_completed")
    elif mutation == "map_failure":
        counts["map_completed"] -= 1
        counts["map_failures"] = 1
    elif mutation == "map_cancellation":
        counts["map_completed"] -= 1
        counts["map_cancellations"] = 1
    elif mutation == "unclassified_extra":
        counts["unclassified_started"] = 1
        counts["unclassified_completed"] = 1
        counts["llm_chat_attempts"] += 1
    elif mutation == "total_excess":
        counts["llm_chat_attempts"] += 1
    elif mutation == "peak_excess":
        counts["map_peak_active"] = 2
    checks = runner._generation_integrity_checks(counts, hierarchy_required=not d06)

    assert checks[expected_failed_check] is False
    assert not all(checks.values())


def test_d06_and_d08_apply_semantic_generation_integrity_and_d08_fixture_is_not_rle() -> None:
    runner = _module()

    class FakeProbes:
        def __init__(self, counts) -> None:  # noqa: ANN001
            self.counts = counts

        @staticmethod
        def snapshot():
            return {}

        def delta(self, _before):  # noqa: ANN001
            return dict(self.counts)

    class FakeHarness:
        run_index = 1

        def __init__(self, case_id: str, counts) -> None:  # noqa: ANN001
            self.case_id = case_id
            self.probes = FakeProbes(counts)
            self.settings = SimpleNamespace(profile=SimpleNamespace(max_model_len=8_192))
            self.payload = b""
            self.prompt = ""

        @staticmethod
        def document(filename, mime_type, content, source_ref):  # noqa: ANN001
            return {
                "filename": filename,
                "mime_type": mime_type,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "source_ref": source_ref,
            }

        def chat(self, case_id, message, *, document, **_kwargs):  # noqa: ANN001
            self.payload = base64.b64decode(document["content_base64"])
            self.prompt = message
            markers = (
                ("SMALL-ALPHA-1", "SMALL-BETA-1", "SMALL-GAMMA-1")
                if case_id == "D06"
                else ("SCAN-PAGE-FIVE-1",)
                if case_id == "D07"
                else ("LONG-HEAD-1", "LONG-MIDDLE-1", "LONG-TAIL-1")
            )
            result = {
                "message": " ".join(markers),
                "file_ingestion": {"extraction": {}},
            }
            if case_id == "D07":
                result["file_ingestion"] = {
                    "extraction": {
                        "vision_pages_read": 5,
                        "vision_pages_total": 5,
                        "parse_pages_truncated": False,
                    }
                }
                result["grounding_warning"] = "OCR требует сверки с оригиналом."
            return result

        @staticmethod
        def message_row(_answer):  # noqa: ANN001
            return {
                "metadata_json": {
                    "attachment_context_used": True,
                    "fabricated_outside_deed_request": False,
                }
            }

        @staticmethod
        def case_result(case_id, _started, checks, counters=None):  # noqa: ANN001
            failed = [name for name, value in checks.items() if value is not True]
            return {
                "case_id": case_id,
                "status": "failed" if failed else "passed",
                "failure_codes": failed,
                "checks": dict(checks),
                "counters": dict(counters or {}),
            }

    d06_counts = _closed_generation_counts(
        runner,
        direct=1,
        hierarchy=0,
        map_calls=0,
        reduce=0,
        final=0,
        verifier=1,
    )
    d06 = FakeHarness("D06", d06_counts)
    d06_result = runner._case_06(d06)
    assert d06_result["status"] == "passed", d06_result
    assert "дословно перечисли" in d06.prompt
    assert "не пропускай ни одно" in d06.prompt

    d07 = FakeHarness("D07", {})
    d07_result = runner._case_07(d07)
    assert d07_result["status"] == "passed", d07_result
    assert "все пять страниц" in d07.prompt
    assert "дословно перепиши полное значение" in d07.prompt

    d08_counts = _closed_generation_counts(
        runner,
        direct=0,
        hierarchy=1,
        map_calls=4,
        reduce=1,
        final=1,
        verifier=2,
    )
    d08 = FakeHarness("D08", d08_counts)
    d08_result = runner._case_08(d08)
    assert d08_result["status"] == "passed", d08_result
    source = d08.payload.decode("utf-8")
    lines = source.splitlines()
    filler_lines = [line for line in lines if line.startswith("Абзац ")]
    assert len(filler_lines) == len(set(filler_lines)) == 216
    assert len({line.split(". ", 1)[1] for line in filler_lines}) == 216
    assert len(source) > 8_192 * 4
    assert source.index("LONG-HEAD-1") < source.index("LONG-MIDDLE-1") < source.index("LONG-TAIL-1")
    assert source.index("LONG-MIDDLE-1") > len(source) // 3
    assert source.index("LONG-MIDDLE-1") < len(source) * 2 // 3
    from friday.agent_runtime import (
        _attachment_hierarchy_map_chunk_chars,
        _attachment_lossless_unit_rle_bundle,
        _attachment_whole_source_plan,
        _OwnedAttachment,
    )

    source_attachment = _OwnedAttachment(
        {
            "filename": "large.txt",
            "transient_text": source,
            "extraction_success": True,
        }
    )
    chunk_chars = _attachment_hierarchy_map_chunk_chars(
        [source_attachment],
        max_model_len=8_192,
        request_chars=len(d08.prompt),
        parallelism=1,
    )
    chunks, *_rest = _attachment_whole_source_plan([source_attachment], chunk_chars=chunk_chars)
    assert len(chunks) == 4
    assert (
        _attachment_lossless_unit_rle_bundle(
            [source_attachment],
            message=d08.prompt,
            task_kind="summary",
            max_model_len=8_192,
        )
        is None
    )


def test_d08_live_case_uses_isolated_small_context_without_changing_release_profile(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    from friday.config import load_settings

    monkeypatch.setenv("FRIDAY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(tmp_path / "scratch.sqlite3"))
    base = load_settings(runner._RELEASE_PROFILE)

    d08, _case_dir, _evidence = runner._settings_for_case(base, tmp_path / "run", "D08")
    d06, _case_dir, _evidence = runner._settings_for_case(base, tmp_path / "run", "D06")

    assert base.profile.max_model_len == 40_960
    assert d06.profile is base.profile
    assert d08.profile.max_model_len == 8_192
    assert d08.profile.name == base.profile.name
    assert d08.profile.document_map_max_concurrency == base.profile.document_map_max_concurrency


@pytest.mark.parametrize("cap", (2, 3))
def test_worker_settings_refuse_document_map_fanout_above_one(tmp_path, monkeypatch, cap) -> None:
    runner = _module()
    from friday.config import load_settings

    monkeypatch.setenv("FRIDAY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(tmp_path / "scratch.sqlite3"))
    base = load_settings(runner._RELEASE_PROFILE)
    settings, case_dir, _evidence = runner._settings_for_case(base, tmp_path / "run", "D06")
    settings = replace(
        settings,
        profile=replace(settings.profile, document_map_max_concurrency=cap),
        workers_enabled=False,
        code_execution_enabled=False,
        web_daily_quota=0,
        llm_enabled=True,
        embeddings_enabled=True,
        embeddings_model="embedding-model",
        rerank_top=1,
        rerank_base_url="http://127.0.0.1:8103",
        rerank_model="reranker-model",
        mcp_enabled=True,
    )

    with pytest.raises(runner.BatteryFailure, match="document_map_concurrency_not_one"):
        runner._assert_worker_settings(settings, case_dir, require_mcp=True)


def test_worker_settings_refuse_any_other_named_profile(tmp_path, monkeypatch) -> None:
    runner = _module()
    from friday.config import load_settings

    monkeypatch.setenv("FRIDAY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("FRIDAY_DATABASE_PATH", str(tmp_path / "scratch.sqlite3"))
    base = load_settings(runner._RELEASE_PROFILE)
    settings, case_dir, _evidence = runner._settings_for_case(base, tmp_path / "run", "D06")
    settings = replace(settings, profile=replace(settings.profile, name="qwen36-vl"))

    with pytest.raises(runner.BatteryFailure, match="release_profile_mismatch"):
        runner._assert_worker_settings(settings, case_dir, require_mcp=True)


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


def test_runner_git_uses_absolute_binary_and_drops_ambient_loader_and_repository_controls(
    monkeypatch,
) -> None:
    runner = _module()
    monkeypatch.setenv("PATH", "/private/substituted-bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private/substituted-loader")
    monkeypatch.setenv("GIT_DIR", "/private/alternate.git")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid")
    observed: dict[str, Any] = {}

    def run(command, **kwargs):
        observed["command"] = list(command)
        observed["environment"] = dict(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="a" * 40 + "\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)

    assert runner._git_output("rev-parse", "HEAD") == "a" * 40
    assert observed["command"][:3] == [
        runner._GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
    ]
    environment = observed["environment"]
    for forbidden in ("PATH", "LD_LIBRARY_PATH", "GIT_DIR", "HTTPS_PROXY"):
        assert forbidden not in environment
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def _closed_worker_outcome(runner, run_id: str, run_index: int, *, status: str = "passed", **changes):
    payload = {
        "schema": runner.WORKER_SCHEMA,
        "run_index": run_index,
        "run_id_hash": runner._run_id_hash(run_id),
        "status": status,
        "failure_codes": [] if status == "passed" else ["synthetic_failure"],
        "lifecycle_teardown_clear": True,
        "lifecycle_failure_codes": [],
        "cases": [],
    }
    payload_changes = changes.pop("payload", {})
    for key, value in payload_changes.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    defaults = {
        "stdout": json.dumps(payload).encode("utf-8"),
        "returncode": 0,
        "worker_reaped": True,
        "process_group_clear_initial": True,
        "process_group_clear": True,
        "timed_out": False,
        "cleanup_failure_codes": (),
    }
    defaults.update(changes)
    return runner.WorkerProcessOutcome(**defaults)


def _clear_observer_projection(runner, request):
    response = {
        "schema": runner.OBSERVER_RESPONSE_SCHEMA,
        "commit": request["commit"],
        "run_id_hash": request["run_id_hash"],
        "run_index": request["run_index"],
        "run_receipt_sha256": request["run_receipt_sha256"],
        "worker_report_sha256": request["worker_report_sha256"],
        "challenge": request["challenge"],
        "status": "passed",
        "bridge_stopped": True,
        "bridge_operator_guard_held": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dispatcher_unchanged": True,
    }
    return runner._validate_observer_response(response, request)


@pytest.mark.parametrize(
    ("worker_statuses", "expected_calls", "expected_status"),
    ((["failed", "passed"], 1, "failed"), (["passed", "passed"], 2, "passed")),
)
def test_controller_orders_worker1_receipt_observer_worker2_exactly(
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
    ordering: list[str] = []
    observer_challenges: list[str] = []

    def run_worker(command, *, environment, private_log, controller_signal_handlers):
        nonlocal calls
        del private_log
        assert controller_signal_handlers is not None
        calls += 1
        run_index = int(command[-1])
        run_id = environment[runner._RUN_ID_ENV]
        worker_run_ids.append(run_id)
        status = next(statuses)
        ordering.append(f"worker-{run_index}")
        return _closed_worker_outcome(runner, run_id, run_index, status=status)

    def observer(barrier_dir, request):
        ordering.append("observer")
        observer_challenges.append(request["challenge"])
        receipt = runner._read_pinned_private_json(barrier_dir, "run-1-receipt.json")
        assert receipt["teardown_clear"] is True
        assert receipt["run_index"] == request["run_index"] == 1
        assert receipt["worker_report_sha256"] == request["worker_report_sha256"]
        return _clear_observer_projection(runner, request), "b" * 64

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner, "_run_worker_process", run_worker)
    monkeypatch.setattr(runner, "_await_inter_run_observer", observer)
    report_path = tmp_path / "closed-report.json"
    barrier_parent = tmp_path / "barrier-parent"
    barrier_parent.mkdir(mode=0o700)
    barrier_dir = barrier_parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
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
    assert all(challenge not in json.dumps(report) for challenge in observer_challenges)
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == expected_status
    assert ordering == (["worker-1"] if expected_calls == 1 else ["worker-1", "observer", "worker-2"])
    assert (barrier_dir / "run-1-receipt.json").is_file()
    assert (barrier_dir / "run-2-receipt.json").is_file() is (expected_calls == 2)
    failure_summary = barrier_dir / "run-1-failure-summary.json"
    assert failure_summary.is_file() is (expected_calls == 1)
    if expected_calls == 1:
        closed = json.loads(failure_summary.read_text(encoding="utf-8"))
        assert closed == {
            "schema": runner.FAILURE_SUMMARY_SCHEMA,
            "commit": "a" * 40,
            "run_id_hash": runner._run_id_hash(worker_run_ids[0]),
            "run_index": 1,
            "worker_report_sha256": report["run_receipts"][0]["worker_report_sha256"],
            "worker_failure_codes": ["synthetic_failure"],
            "failed_cases": [],
        }


def test_failure_summary_projects_only_closed_case_diagnostics() -> None:
    runner = _module()
    payload = runner._build_failure_summary(
        commit="a" * 40,
        run_hash="b" * 64,
        run_index=1,
        worker_report_sha256="c" * 64,
        report={
            "failure_codes": ["worker_exit_nonzero", "private detail must not escape"],
            "cases": [
                {
                    "case_id": "D06",
                    "status": "failed",
                    "failure_codes": ["D06_answer_target", "private detail must not escape"],
                    "checks": {"answer_target": False, "safe": True, "private detail": False},
                    "counters": {"llm_chat_attempts": 1, "secret": "value", "negative": -1},
                },
                {"case_id": "D07", "status": "passed", "failure_codes": []},
            ],
        },
    )

    assert payload["worker_failure_codes"] == ["worker_exit_nonzero"]
    assert payload["failed_cases"] == [
        {
            "case_id": "D06",
            "failure_codes": ["D06_answer_target"],
            "failed_checks": ["answer_target"],
            "counters": {"llm_chat_attempts": 1},
        }
    ]
    assert "private detail" not in json.dumps(payload)
    assert "value" not in json.dumps(payload)


@pytest.mark.parametrize(
    "mutation",
    (
        "orphan_reaped_after_term",
        "process_group_still_alive",
        "missing_lifecycle_teardown",
        "false_lifecycle_teardown",
        "mcp_close_timeout_warning",
        "false_persisted_teardown",
        "missing_persisted_receipt",
        "observer_exception",
        "receipt_changed_by_observer",
        "backend_mismatch",
        "queue_mismatch",
    ),
)
def test_every_inter_run_teardown_or_observer_red_prevents_worker2(
    tmp_path,
    monkeypatch,
    mutation,
) -> None:
    runner = _module()
    calls: list[int] = []

    def run_worker(command, *, environment, private_log, controller_signal_handlers):
        del private_log
        assert controller_signal_handlers is not None
        run_index = int(command[-1])
        calls.append(run_index)
        changes = {}
        if mutation == "orphan_reaped_after_term":
            changes = {
                "process_group_clear_initial": False,
                "cleanup_failure_codes": (
                    "worker_group_term_sent",
                    "worker_process_group_survived",
                ),
            }
        elif mutation == "process_group_still_alive":
            changes = {
                "process_group_clear_initial": False,
                "process_group_clear": False,
                "cleanup_failure_codes": (
                    "worker_group_kill_sent",
                    "worker_group_term_sent",
                    "worker_process_group_not_clear",
                    "worker_process_group_survived",
                ),
            }
        elif mutation == "missing_lifecycle_teardown":
            changes = {
                "payload": {
                    "lifecycle_teardown_clear": None,
                    "lifecycle_failure_codes": None,
                }
            }
        elif mutation == "false_lifecycle_teardown":
            changes = {"payload": {"lifecycle_teardown_clear": False}}
        elif mutation == "mcp_close_timeout_warning":
            changes = {
                "payload": {
                    "lifecycle_teardown_clear": False,
                    "lifecycle_failure_codes": ["mcp_cleanup_timeout_warning"],
                }
            }
        return _closed_worker_outcome(
            runner,
            environment[runner._RUN_ID_ENV],
            run_index,
            **changes,
        )

    def observer(_barrier_dir, _request):
        if mutation == "observer_exception":
            raise RuntimeError("private observer detail")
        if mutation == "receipt_changed_by_observer":
            receipt_path = _barrier_dir / "run-1-receipt.json"
            receipt = runner._read_pinned_private_json(_barrier_dir, receipt_path.name)
            receipt["worker_report_sha256"] = "0" * 64
            replacement = _barrier_dir / ".replacement.tmp"
            replacement.write_bytes(runner._canonical_json(receipt) + b"\n")
            replacement.chmod(0o600)
            os.replace(replacement, receipt_path)
        if mutation == "backend_mismatch":
            raise runner.BatteryFailure("inter_run_observer_backend_unchanged_failed")
        if mutation == "queue_mismatch":
            raise runner.BatteryFailure("inter_run_observer_outbound_pending_zero_failed")
        return _clear_observer_projection(runner, _request), "c" * 64

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner, "_run_worker_process", run_worker)
    monkeypatch.setattr(runner, "_await_inter_run_observer", observer)
    if mutation == "false_persisted_teardown":
        original_build_receipt = runner._build_run_receipt

        def false_receipt(**kwargs):
            return {**original_build_receipt(**kwargs), "teardown_clear": False}

        monkeypatch.setattr(runner, "_build_run_receipt", false_receipt)
    elif mutation == "missing_persisted_receipt":
        monkeypatch.setattr(
            runner,
            "_persist_run_receipt",
            lambda barrier_dir, _payload: (barrier_dir / "missing.json", "f" * 64),
        )
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    report = runner.run_controller(args)

    assert calls == [1]
    assert report["status"] == "failed"
    assert report["runs_completed"] == 1
    assert not (barrier_dir / "run-2-receipt.json").exists()


def test_controller_baseexception_never_starts_a_following_worker(tmp_path, monkeypatch) -> None:
    runner = _module()
    calls = 0

    def interrupted_worker(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise runner.ControllerSignal(int(runner.signal.SIGTERM))

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner, "_run_worker_process", interrupted_worker)
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    with pytest.raises(runner.ControllerSignal):
        runner.run_controller(args)

    assert calls == 1
    assert not (barrier_dir / "run-2-receipt.json").exists()


def test_controller_never_starts_worker2_after_barrier_directory_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    calls: list[int] = []
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)

    def run_worker(command, *, environment, private_log, controller_signal_handlers):
        del private_log
        assert controller_signal_handlers is not None
        run_index = int(command[-1])
        calls.append(run_index)
        return _closed_worker_outcome(
            runner,
            environment[runner._RUN_ID_ENV],
            run_index,
        )

    def replace_barrier(_barrier, request):
        moved = tmp_path / "moved-barrier"
        barrier_dir.rename(moved)
        barrier_dir.mkdir(mode=0o700)
        return _clear_observer_projection(runner, request), "b" * 64

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner, "_run_worker_process", run_worker)
    monkeypatch.setattr(runner, "_await_inter_run_observer", replace_barrier)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
        runner.run_controller(args)

    assert calls == [1]
    assert not (barrier_dir / "run-2-receipt.json").exists()


def test_fast_exit_leader_with_term_ignoring_descendant_gets_one_kill(monkeypatch) -> None:
    runner = _module()
    group_alive = True
    delivered: list[int] = []

    class FastLeader:
        pid = 445561
        returncode = 0
        calls = 0

        @classmethod
        def communicate(cls, *, timeout):
            del timeout
            cls.calls += 1
            assert cls.calls == 1
            return b"{}", None

        @staticmethod
        def poll():
            return 0

    def killpg(process_group, selected_signal):
        nonlocal group_alive
        assert process_group == FastLeader.pid
        if selected_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        delivered.append(selected_signal)
        # Model a descendant that ignores TERM even though its leader is reaped.
        if selected_signal == runner.signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: FastLeader())
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "PROCESS_GROUP_EXIT_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_TERM_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_KILL_GRACE_SEC", 0.0)

    outcome = runner._run_worker_process(
        [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
        environment={},
        private_log=io.BytesIO(),
    )

    assert delivered == [runner.signal.SIGTERM, runner.signal.SIGKILL]
    assert FastLeader.calls == 1
    assert outcome.worker_reaped is True
    assert outcome.process_group_clear_initial is False
    assert outcome.process_group_clear is True
    assert outcome.cleanup_failure_codes == (
        "worker_group_kill_sent",
        "worker_group_term_sent",
        "worker_process_group_survived",
    )


def test_signal_during_first_post_communicate_group_audit_still_cleans(monkeypatch) -> None:
    runner = _module()
    group_alive = True
    audits = 0
    delivered: list[int] = []

    class ReapedLeader:
        pid = 445562
        returncode = 0

        @staticmethod
        def communicate(*, timeout):
            del timeout
            return b"{}", None

        @staticmethod
        def poll():
            return 0

    def killpg(process_group, selected_signal):
        nonlocal group_alive
        assert process_group == ReapedLeader.pid
        if selected_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        delivered.append(selected_signal)
        if selected_signal == runner.signal.SIGTERM:
            group_alive = False

    def audited_wait(process_group, _timeout):
        nonlocal audits
        assert process_group == ReapedLeader.pid
        audits += 1
        if audits == 1:
            raise runner.ControllerSignal(int(runner.signal.SIGTERM))
        return not group_alive

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: ReapedLeader())
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "_wait_process_group_clear", audited_wait)

    with pytest.raises(runner.ControllerSignal) as captured:
        runner._run_worker_process(
            [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
            environment={},
            private_log=io.BytesIO(),
        )

    assert audits == 3  # interrupted initial audit, TERM wait, mandatory final audit
    assert delivered == [runner.signal.SIGTERM]
    assert captured.value.worker_cleanup_clear is True
    assert captured.value.worker_cleanup_failure_codes == ("worker_group_term_sent",)


def test_repeat_signal_is_masked_until_cleanup_and_first_signal_wins(monkeypatch) -> None:
    runner = _module()
    group_alive = True

    class InterruptedLeader:
        pid = 445563
        returncode = 0
        calls = 0

        @classmethod
        def communicate(cls, *, timeout):
            del timeout
            cls.calls += 1
            if cls.calls == 1:
                _signal_current_thread(runner, runner.signal.SIGINT)
                raise AssertionError("SIGINT handler did not interrupt communicate")
            return b"", None

        @staticmethod
        def poll():
            return 0

    def killpg(process_group, selected_signal):
        nonlocal group_alive
        assert process_group == InterruptedLeader.pid
        if selected_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        if selected_signal == runner.signal.SIGTERM:
            # This repeat is pending, not delivered, until the complete cleanup
            # sequence and final group audit have finished.
            current = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
            assert set(runner._CONTROLLER_SIGNALS) <= current
            group_alive = False
            _signal_current_thread(runner, runner.signal.SIGTERM)

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: InterruptedLeader(),
    )
    monkeypatch.setattr(runner.os, "killpg", killpg)
    controller_signals = runner._install_controller_signal_handlers()
    try:
        runner._activate_controller_signal_handlers(controller_signals)
        with pytest.raises(runner.ControllerSignal) as captured:
            runner._run_worker_process(
                [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
                environment={},
                private_log=io.BytesIO(),
                controller_signal_handlers=controller_signals,
            )
    finally:
        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)

    assert controller_signals.first_signal == runner.signal.SIGINT
    assert captured.value.signal_number == runner.signal.SIGINT
    assert captured.value.worker_cleanup_clear is True
    assert captured.value.worker_cleanup_failure_codes == ("worker_group_term_sent",)


def test_controller_finalizer_masks_repeat_signal_and_restores_handlers() -> None:
    runner = _module()
    original_handlers = {
        selected: runner.signal.getsignal(selected) for selected in runner._CONTROLLER_SIGNALS
    }
    cleanup_masks: list[frozenset[object]] = []
    controller_signals = runner._install_controller_signal_handlers()

    def cleanup() -> None:
        current = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        cleanup_masks.append(current)
        _signal_current_thread(runner, runner.signal.SIGTERM)

    runner._activate_controller_signal_handlers(controller_signals)
    with pytest.raises(runner.ControllerSignal) as captured:
        try:
            _signal_current_thread(runner, runner.signal.SIGINT)
        finally:
            runner._finalize_controller_signal_handlers(controller_signals, cleanup)

    assert set(runner._CONTROLLER_SIGNALS) <= cleanup_masks[0]
    assert controller_signals.first_signal == runner.signal.SIGINT
    assert captured.value.signal_number == runner.signal.SIGINT
    assert {
        selected: runner.signal.getsignal(selected) for selected in runner._CONTROLLER_SIGNALS
    } == original_handlers


def test_pending_repeat_drain_is_nonblocking_if_another_thread_consumed_it(monkeypatch) -> None:
    runner = _module()
    timed_waits: list[tuple[frozenset[object], float]] = []
    monkeypatch.setattr(
        runner.signal,
        "sigpending",
        lambda: frozenset((runner.signal.SIGTERM,)),
    )

    def consumed_elsewhere(pending, timeout):
        timed_waits.append((frozenset(pending), timeout))
        return None

    monkeypatch.setattr(runner.signal, "sigtimedwait", consumed_elsewhere)

    assert runner._drain_pending_controller_signals() is True

    assert timed_waits == [(frozenset((runner.signal.SIGTERM,)), 0)]


def test_pending_repeat_drain_is_bounded_under_continuous_replenishment(monkeypatch) -> None:
    runner = _module()
    timed_waits: list[tuple[frozenset[object], float]] = []
    monkeypatch.setattr(
        runner.signal,
        "sigpending",
        lambda: frozenset((runner.signal.SIGTERM,)),
    )

    def replenished(pending, timeout):
        timed_waits.append((frozenset(pending), timeout))
        return runner.signal.SIGTERM

    monkeypatch.setattr(runner.signal, "sigtimedwait", replenished)

    assert runner._drain_pending_controller_signals() is False
    assert len(timed_waits) == runner._MAX_PENDING_CONTROLLER_SIGNAL_DRAINS
    assert all(item == (frozenset((runner.signal.SIGTERM,)), 0) for item in timed_waits)


def test_controller_owns_signals_even_when_operator_spawn_mask_is_inherited() -> None:
    runner = _module()
    original_mask = frozenset(
        runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, runner._CONTROLLER_SIGNALS)
    )
    controller_signals = None
    try:
        controller_signals = runner._install_controller_signal_handlers()
        assert not set(runner._CONTROLLER_SIGNALS).intersection(controller_signals.previous_mask)

        runner._activate_controller_signal_handlers(controller_signals)
        active_mask = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        assert not set(runner._CONTROLLER_SIGNALS).intersection(active_mask)

        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)
        controller_signals = None
        final_mask = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        assert not set(runner._CONTROLLER_SIGNALS).intersection(final_mask)
    finally:
        if controller_signals is not None:
            runner._finalize_controller_signal_handlers(controller_signals, lambda: None)
        runner.signal.pthread_sigmask(runner.signal.SIG_SETMASK, original_mask)


def test_spawn_binding_window_is_masked_and_cleanup_uses_bound_pgid(monkeypatch) -> None:
    runner = _module()
    group_alive = True
    cleanup_targets: list[int] = []
    spawn_masks: list[frozenset[object]] = []

    class BoundLeader:
        pid = 445564
        returncode = 0

        @staticmethod
        def communicate(*, timeout):
            del timeout
            return b"", None

        @staticmethod
        def poll():
            return 0

    def popen(*_args, **_kwargs):
        current = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        spawn_masks.append(current)
        assert set(runner._CONTROLLER_SIGNALS) <= current
        # Pending delivery occurs only after _run_worker_process has stored both
        # the process handle and its exact PGID.
        _signal_current_thread(runner, runner.signal.SIGTERM)
        return BoundLeader()

    def killpg(process_group, selected_signal):
        nonlocal group_alive
        assert process_group == BoundLeader.pid
        if selected_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        cleanup_targets.append(process_group)
        if selected_signal == runner.signal.SIGTERM:
            group_alive = False

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "killpg", killpg)
    controller_signals = runner._install_controller_signal_handlers()
    try:
        runner._activate_controller_signal_handlers(controller_signals)
        with pytest.raises(runner.ControllerSignal) as captured:
            runner._run_worker_process(
                [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
                environment={},
                private_log=io.BytesIO(),
                controller_signal_handlers=controller_signals,
            )
    finally:
        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)

    assert spawn_masks
    assert cleanup_targets == [BoundLeader.pid]
    assert captured.value.signal_number == runner.signal.SIGTERM
    assert captured.value.worker_cleanup_clear is True


def test_false_final_cleanup_audit_is_attached_to_signal_and_never_green(monkeypatch) -> None:
    runner = _module()

    class InterruptedLeader:
        pid = 445565
        returncode = 0
        calls = 0

        @classmethod
        def communicate(cls, *, timeout):
            del timeout
            cls.calls += 1
            if cls.calls == 1:
                raise runner.ControllerSignal(int(runner.signal.SIGTERM))
            return b"", None

        @staticmethod
        def poll():
            return 0

    def killpg(process_group, selected_signal):
        assert process_group == InterruptedLeader.pid
        # The synthetic group survives TERM, KILL and the mandatory final audit.
        return None

    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: InterruptedLeader(),
    )
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "PROCESS_GROUP_TERM_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_KILL_GRACE_SEC", 0.0)

    with pytest.raises(runner.ControllerSignal) as captured:
        runner._run_worker_process(
            [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
            environment={},
            private_log=io.BytesIO(),
        )

    assert captured.value.worker_cleanup_clear is False
    assert captured.value.worker_cleanup_failure_codes == (
        "worker_group_kill_sent",
        "worker_group_term_sent",
        "worker_process_group_not_clear",
    )


def test_late_first_signal_after_cleanup_preserves_false_audit_and_closed_main_code(
    monkeypatch,
    capsys,
) -> None:
    runner = _module()
    group_alive = True
    delivered: list[int] = []
    signal_injected = False

    class ReapedLeader:
        pid = 445566
        returncode = 0

        @staticmethod
        def communicate(*, timeout):
            del timeout
            return b"{}", None

        @staticmethod
        def poll():
            return 0

    def killpg(process_group, selected_signal):
        assert process_group == ReapedLeader.pid
        if selected_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        delivered.append(selected_signal)
        # Model a group whose final audit remains false after the one KILL.

    original_cleanup = runner._cleanup_bound_worker

    class PostCleanupSignalOutcome:
        def __init__(self, outcome):
            self._outcome = outcome

        @property
        def stdout(self):
            nonlocal signal_injected
            assert self._outcome.worker_reaped is True
            assert self._outcome.process_group_clear is False
            signal_injected = True
            # This is a real process-directed signal injected only after the
            # complete production cleanup returned and before its projection.
            _signal_current_thread(runner, runner.signal.SIGTERM)
            return self._outcome.stdout

        @property
        def worker_reaped(self):
            return self._outcome.worker_reaped

        @property
        def process_group_clear(self):
            return self._outcome.process_group_clear

        @property
        def cleanup_failure_codes(self):
            return self._outcome.cleanup_failure_codes

        @property
        def deferred_baseexception(self):
            return self._outcome.deferred_baseexception

    def cleanup_with_late_signal(process, process_group):
        return PostCleanupSignalOutcome(original_cleanup(process, process_group))

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: ReapedLeader())
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "_cleanup_bound_worker", cleanup_with_late_signal)
    monkeypatch.setattr(runner, "PROCESS_GROUP_EXIT_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_TERM_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_KILL_GRACE_SEC", 0.0)
    controller_signals = runner._install_controller_signal_handlers()
    try:
        runner._activate_controller_signal_handlers(controller_signals)
        with pytest.raises(runner.ControllerSignal) as captured:
            runner._run_worker_process(
                [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
                environment={},
                private_log=io.BytesIO(),
                controller_signal_handlers=controller_signals,
            )
    finally:
        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)

    assert signal_injected is True
    assert delivered == [runner.signal.SIGTERM, runner.signal.SIGKILL]
    assert ReapedLeader.poll() == 0  # no real subprocess or survivor escaped the regression
    assert captured.value.signal_number == runner.signal.SIGTERM
    assert captured.value.worker_cleanup_clear is False
    assert "worker_process_group_not_clear" in captured.value.worker_cleanup_failure_codes

    def replay_signal(_args):
        raise captured.value

    monkeypatch.setattr(runner, "run_controller", replay_signal)
    assert runner.main(["--run-live"]) == 128 + int(runner.signal.SIGTERM)
    assert capsys.readouterr().err == "controller_signal_worker_cleanup_not_clear\n"


def test_signal_delivered_after_worker_return_reuses_the_bound_false_audit(monkeypatch) -> None:
    runner = _module()

    class ReapedLeader:
        pid = 445567
        returncode = 0

        @staticmethod
        def communicate(*, timeout):
            del timeout
            return b"{}", None

        @staticmethod
        def poll():
            return 0

    def persistent_group(process_group, selected_signal):
        assert process_group == ReapedLeader.pid
        del selected_signal

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: ReapedLeader())
    monkeypatch.setattr(runner.os, "killpg", persistent_group)
    monkeypatch.setattr(runner, "PROCESS_GROUP_EXIT_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_TERM_GRACE_SEC", 0.0)
    monkeypatch.setattr(runner, "PROCESS_GROUP_KILL_GRACE_SEC", 0.0)
    controller_signals = runner._install_controller_signal_handlers()
    try:
        runner._activate_controller_signal_handlers(controller_signals)
        outcome = runner._run_worker_process(
            [sys.executable, str(RUNNER), "--worker", "--run-index", "1"],
            environment={},
            private_log=io.BytesIO(),
            controller_signal_handlers=controller_signals,
        )
        assert outcome.process_group_clear is False

        with pytest.raises(runner.ControllerSignal) as captured:
            _signal_current_thread(runner, runner.signal.SIGTERM)
    finally:
        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)

    assert captured.value.worker_cleanup_clear is False
    assert "worker_process_group_not_clear" in captured.value.worker_cleanup_failure_codes


def test_worker_first_action_unblocks_inherited_int_and_term_mask() -> None:
    runner = _module()
    original_mask = frozenset(
        runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, runner._CONTROLLER_SIGNALS)
    )
    try:
        inherited = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        assert set(runner._CONTROLLER_SIGNALS) <= inherited

        runner._unblock_worker_control_signals()

        worker_mask = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        assert set(runner._CONTROLLER_SIGNALS).isdisjoint(worker_mask)
    finally:
        runner.signal.pthread_sigmask(runner.signal.SIG_SETMASK, original_mask)


def test_worker_main_calls_unblock_before_worker_setup(monkeypatch, capsys) -> None:
    runner = _module()
    ordering: list[str] = []

    monkeypatch.setattr(
        runner,
        "_unblock_worker_control_signals",
        lambda: ordering.append("unblock"),
    )

    def execute_worker(run_index):
        assert run_index == 1
        assert ordering == ["unblock"]
        ordering.append("execute")
        return {
            "schema": runner.WORKER_SCHEMA,
            "run_index": 1,
            "status": "failed",
            "failure_codes": ["offline_test"],
            "lifecycle_teardown_clear": True,
            "lifecycle_failure_codes": [],
            "cases": [],
        }

    monkeypatch.setattr(runner, "execute_worker", execute_worker)

    assert runner._worker_main(SimpleNamespace(run_index=1)) == 1
    assert ordering == ["unblock", "execute"]
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_main_returns_signal_exit_code_and_reports_closed_cleanup_failure(
    monkeypatch,
    capsys,
) -> None:
    runner = _module()

    def interrupted(_args):
        exc = runner.ControllerSignal(int(runner.signal.SIGTERM))
        exc.worker_cleanup_clear = False
        exc.worker_cleanup_failure_codes = ("worker_process_group_not_clear",)
        raise exc

    monkeypatch.setattr(runner, "run_controller", interrupted)

    assert runner.main(["--run-live"]) == 128 + int(runner.signal.SIGTERM)
    assert capsys.readouterr().err == "controller_signal_worker_cleanup_not_clear\n"


def test_barrier_receipts_are_atomic_private_regular_files_and_response_is_bound(tmp_path) -> None:
    runner = _module()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    request = runner._observer_request(
        commit="a" * 40,
        run_hash="b" * 64,
        receipt_sha256="c" * 64,
        worker_report_sha256="d" * 64,
        challenge="e" * 64,
    )
    response = {
        "schema": runner.OBSERVER_RESPONSE_SCHEMA,
        "commit": request["commit"],
        "run_id_hash": request["run_id_hash"],
        "run_index": request["run_index"],
        "run_receipt_sha256": request["run_receipt_sha256"],
        "worker_report_sha256": request["worker_report_sha256"],
        "challenge": request["challenge"],
        "status": "passed",
        "bridge_stopped": True,
        "bridge_operator_guard_held": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dispatcher_unchanged": True,
    }
    response_path = prepared / "run-1-observer.json"
    try:
        runner._atomic_pinned_private_write(
            prepared,
            response_path.name,
            runner._canonical_json(response) + b"\n",
        )

        projection, response_sha256 = runner._await_inter_run_observer(prepared, request)

        assert projection["status"] == "passed"
        assert len(response_sha256) == 64
        for path in (
            prepared / "run-1-observer-request.json",
            response_path,
        ):
            metadata = os.lstat(path)
            assert metadata.st_nlink == 1
            assert metadata.st_uid == os.getuid()
            assert metadata.st_mode & 0o777 == 0o600
            assert path.is_file() and not path.is_symlink()
        assert not list(barrier_dir.glob(".*.tmp"))
    finally:
        prepared.close()


def test_pinned_barrier_publish_is_create_only_and_preserves_existing_bytes(tmp_path) -> None:
    runner = _module()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    try:
        name = "run-1-receipt.json"
        existing = runner._canonical_json({"existing": True}) + b"\n"
        runner._atomic_pinned_private_write(prepared, name, existing)

        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_file_exists"):
            runner._atomic_pinned_private_write(
                prepared,
                name,
                runner._canonical_json({"replacement": True}) + b"\n",
            )

        assert (barrier_dir / name).read_bytes() == existing
        assert not list(barrier_dir.glob(".*.tmp"))
    finally:
        prepared.close()


def test_pinned_barrier_publish_never_exposes_a_multi_link_target(tmp_path, monkeypatch) -> None:
    runner = _module()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    original = runner._rename_noreplace
    observed: list[tuple[int, int]] = []

    def observe(source_dir, source_name, target_dir, target_name):
        source_before = os.stat(source_name, dir_fd=source_dir, follow_symlinks=False)
        with pytest.raises(FileNotFoundError):
            os.stat(target_name, dir_fd=target_dir, follow_symlinks=False)
        original(source_dir, source_name, target_dir, target_name)
        target_after = os.stat(target_name, dir_fd=target_dir, follow_symlinks=False)
        with pytest.raises(FileNotFoundError):
            os.stat(source_name, dir_fd=source_dir, follow_symlinks=False)
        observed.append((source_before.st_nlink, target_after.st_nlink))

    monkeypatch.setattr(runner, "_rename_noreplace", observe)
    try:
        runner._atomic_pinned_private_write(
            prepared,
            "run-1-receipt.json",
            runner._canonical_json({"status": "passed"}) + b"\n",
        )
    finally:
        prepared.close()

    assert observed == [(1, 1)]


@pytest.mark.parametrize("replacement_mode", (0o700, 0o755))
def test_pinned_barrier_open_rejects_replacement_after_initial_validation(
    tmp_path,
    monkeypatch,
    replacement_mode,
) -> None:
    runner = _module()
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    barrier_dir = parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    original = runner._validated_private_barrier_path

    def replace_after_validation(value):
        validated = original(value)
        barrier_dir.rename(parent / "validated-away")
        barrier_dir.mkdir(mode=replacement_mode)
        barrier_dir.chmod(replacement_mode)
        return validated

    monkeypatch.setattr(runner, "_validated_private_barrier_path", replace_after_validation)

    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
        runner._PinnedBarrierDirectory.open(barrier_dir)


def test_pinned_barrier_parent_is_private_and_quiescent_by_contract(tmp_path) -> None:
    runner = _module()
    public_parent = tmp_path / "public-parent"
    public_parent.mkdir(mode=0o755)
    public_parent.chmod(0o755)
    public_barrier = public_parent / "barrier"
    public_barrier.mkdir(mode=0o700)

    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_invalid"):
        runner._PinnedBarrierDirectory.open(public_barrier)

    private_parent = tmp_path / "private-parent"
    private_parent.mkdir(mode=0o700)
    barrier_dir = private_parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    try:
        (private_parent / "unexpected-sibling").write_text("x", encoding="utf-8")
        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
            prepared.revalidate()
    finally:
        prepared.close()

    assert "dedicated, quiescent" in runner.build_parser().format_help()


def test_private_worker_log_binds_and_closes_fd_before_pending_signal_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    captured: list[int] = []
    original_open = runner.os.open
    controller_signals = runner._install_controller_signal_handlers()

    def signal_after_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        captured.append(descriptor)
        _signal_current_thread(runner, runner.signal.SIGTERM)
        return descriptor

    monkeypatch.setattr(runner.os, "open", signal_after_open)
    try:
        runner._activate_controller_signal_handlers(controller_signals)
        with (
            pytest.raises(runner.ControllerSignal),
            runner._private_worker_log(tmp_path / "private-worker.log"),
        ):
            pytest.fail("the interrupted ownership handoff must not enter the body")
    finally:
        runner._finalize_controller_signal_handlers(controller_signals, lambda: None)

    assert len(captured) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(captured[0])
    assert closed.value.errno == 9


def test_pinned_barrier_rejects_noncanonical_json_and_directory_replacement(tmp_path) -> None:
    runner = _module()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    try:
        name = "run-1-observer.json"
        runner._atomic_pinned_private_write(prepared, name, b'{"status": "passed"}\n')
        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_file_invalid"):
            runner._read_pinned_private_json(prepared, name)

        moved = tmp_path / "moved-barrier"
        barrier_dir.rename(moved)
        barrier_dir.mkdir(mode=0o700)
        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
            prepared.revalidate()
        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
            runner._atomic_pinned_private_write(
                prepared,
                "run-2-receipt.json",
                runner._canonical_json({"status": "passed"}) + b"\n",
            )
    finally:
        prepared.close()


def test_pinned_barrier_detects_swap_away_and_same_inode_restore(tmp_path) -> None:
    runner = _module()
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    try:
        moved = tmp_path / "moved-barrier"
        barrier_dir.rename(moved)
        moved.rename(barrier_dir)

        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_changed"):
            prepared.revalidate()
    finally:
        prepared.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("run_index", 2),
        ("run_receipt_sha256", "0" * 64),
        ("worker_report_sha256", "1" * 64),
        ("challenge", "2" * 64),
    ),
)
def test_observer_cannot_substitute_run_or_report_binding(field, replacement) -> None:
    runner = _module()
    request = runner._observer_request(
        commit="a" * 40,
        run_hash="b" * 64,
        receipt_sha256="c" * 64,
        worker_report_sha256="d" * 64,
        challenge="e" * 64,
    )
    response = {
        "schema": runner.OBSERVER_RESPONSE_SCHEMA,
        "commit": request["commit"],
        "run_id_hash": request["run_id_hash"],
        "run_index": request["run_index"],
        "run_receipt_sha256": request["run_receipt_sha256"],
        "worker_report_sha256": request["worker_report_sha256"],
        "challenge": request["challenge"],
        "status": "passed",
        "bridge_stopped": True,
        "bridge_operator_guard_held": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dispatcher_unchanged": True,
    }
    response[field] = replacement

    with pytest.raises(runner.BatteryFailure, match="inter_run_observer_binding_mismatch"):
        runner._validate_observer_response(response, request)


def test_observer_requires_v2_operator_guard_semantics() -> None:
    runner = _module()
    request = runner._observer_request(
        commit="a" * 40,
        run_hash="b" * 64,
        receipt_sha256="c" * 64,
        worker_report_sha256="d" * 64,
        challenge="e" * 64,
    )
    response = {
        "schema": "friday.document-contour-live-battery.observer-response.v1",
        "commit": request["commit"],
        "run_id_hash": request["run_id_hash"],
        "run_index": request["run_index"],
        "run_receipt_sha256": request["run_receipt_sha256"],
        "worker_report_sha256": request["worker_report_sha256"],
        "challenge": request["challenge"],
        "status": "passed",
        "bridge_stopped": True,
        "bridge_lease_free": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dispatcher_unchanged": True,
    }

    with pytest.raises(runner.BatteryFailure, match="inter_run_observer_response_invalid"):
        runner._validate_observer_response(response, request)

    response["schema"] = runner.OBSERVER_RESPONSE_SCHEMA
    response["bridge_operator_guard_held"] = response.pop("bridge_lease_free")
    response["bridge_operator_guard_held"] = False
    with pytest.raises(
        runner.BatteryFailure,
        match="inter_run_observer_bridge_operator_guard_held_failed",
    ):
        runner._validate_observer_response(response, request)


def test_barrier_rejects_symlink_or_nonprivate_files(tmp_path) -> None:
    runner = _module()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir(mode=0o700)
    prepared = runner._PinnedBarrierDirectory.open(barrier_dir)
    try:
        response = barrier_dir / "run-1-observer.json"
        response.symlink_to(target)

        with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_file_invalid"):
            runner._read_pinned_private_json(prepared, response.name)
    finally:
        prepared.close()


def test_barrier_directory_must_be_empty_owner_only_0700_and_not_a_symlink(tmp_path) -> None:
    runner = _module()
    nonprivate = tmp_path / "nonprivate"
    nonprivate.mkdir(mode=0o700)
    nonprivate.chmod(0o755)
    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_invalid"):
        runner._require_private_barrier_dir(nonprivate)

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_invalid"):
        runner._require_private_barrier_dir(linked)

    (private / "stale").write_text("stale", encoding="utf-8")
    with pytest.raises(runner.BatteryFailure, match="inter_run_barrier_dir_not_empty"):
        runner._require_private_barrier_dir(private)


@pytest.mark.parametrize("controller_exit", ("normal", "exception", "baseexception"))
def test_controller_closes_every_bound_private_descriptor_on_all_exit_classes(
    tmp_path,
    monkeypatch,
    controller_exit,
) -> None:
    runner = _module()
    pinned_descriptors: list[int] = []
    transient_descriptors: list[int] = []
    log_descriptors: list[int] = []
    private_roots: list[Path] = []
    original_open = runner._PinnedBarrierDirectory.open
    original_owned_descriptor = runner._owned_os_descriptor
    original_log = runner._private_worker_log
    original_mkdtemp = runner.tempfile.mkdtemp

    def capture_open(cls, value, *, owner=None):
        del cls
        prepared = original_open(value, owner=owner)
        pinned_descriptors.extend((prepared.parent_descriptor, prepared.descriptor))
        return prepared

    @contextmanager
    def capture_log(path):
        with original_log(path) as stream:
            log_descriptors.append(stream.fileno())
            yield stream

    @contextmanager
    def capture_owned_descriptor(*args, **kwargs):
        with original_owned_descriptor(*args, **kwargs) as descriptor:
            transient_descriptors.append(descriptor)
            yield descriptor

    def capture_mkdtemp(*args, **kwargs):
        path = Path(original_mkdtemp(*args, **kwargs))
        private_roots.append(path)
        return str(path)

    def run_worker(command, *, environment, private_log, controller_signal_handlers):
        del private_log
        assert controller_signal_handlers is not None
        if controller_exit == "exception":
            raise RuntimeError("private worker failure")
        if controller_exit == "baseexception":
            raise runner.ControllerSignal(int(runner.signal.SIGTERM))
        run_index = int(command[-1])
        return _closed_worker_outcome(
            runner,
            environment[runner._RUN_ID_ENV],
            run_index,
            status="failed",
        )

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner._PinnedBarrierDirectory, "open", classmethod(capture_open))
    monkeypatch.setattr(runner, "_owned_os_descriptor", capture_owned_descriptor)
    monkeypatch.setattr(runner, "_private_worker_log", capture_log)
    monkeypatch.setattr(runner.tempfile, "mkdtemp", capture_mkdtemp)
    monkeypatch.setattr(runner, "_run_worker_process", run_worker)
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    barrier_dir = parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    if controller_exit == "baseexception":
        with pytest.raises(runner.ControllerSignal):
            runner.run_controller(args)
    else:
        report = runner.run_controller(args)
        assert report["status"] == "failed"

    assert len(pinned_descriptors) == 2
    assert len(log_descriptors) == 1
    if controller_exit == "baseexception":
        assert transient_descriptors == []
    else:
        assert transient_descriptors
    for descriptor in (*pinned_descriptors, *transient_descriptors, *log_descriptors):
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == 9
    assert len(private_roots) == 1
    assert not private_roots[0].exists()


def test_controller_setup_owner_closes_pinned_fds_if_return_handoff_is_interrupted(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    captured: list[int] = []
    original_open = runner._PinnedBarrierDirectory.open

    def interrupted_open(cls, value, *, owner=None):
        del cls
        prepared = original_open(value, owner=owner)
        captured.extend((prepared.parent_descriptor, prepared.descriptor))
        raise runner.ControllerSignal(int(runner.signal.SIGTERM))

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner._PinnedBarrierDirectory, "open", classmethod(interrupted_open))
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    barrier_dir = parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    with pytest.raises(runner.ControllerSignal):
        runner.run_controller(args)

    assert len(captured) == 2
    for descriptor in captured:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == 9


def test_pending_setup_signal_is_delivered_only_inside_cleanup_protected_contour(
    tmp_path,
    monkeypatch,
) -> None:
    runner = _module()
    captured_descriptors: list[int] = []
    captured_roots: list[Path] = []
    original_open = runner._PinnedBarrierDirectory.open
    original_mkdtemp = runner.tempfile.mkdtemp

    def capture_open(cls, value, *, owner=None):
        del cls
        prepared = original_open(value, owner=owner)
        captured_descriptors.extend((prepared.parent_descriptor, prepared.descriptor))
        return prepared

    def signal_after_mkdtemp(*args, **kwargs):
        private_root = Path(original_mkdtemp(*args, **kwargs))
        captured_roots.append(private_root)
        _signal_current_thread(runner, runner.signal.SIGTERM)
        return str(private_root)

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(runner._PinnedBarrierDirectory, "open", classmethod(capture_open))
    monkeypatch.setattr(runner.tempfile, "mkdtemp", signal_after_mkdtemp)
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    barrier_dir = parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    with pytest.raises(runner.ControllerSignal) as captured:
        runner.run_controller(args)

    assert captured.value.signal_number == runner.signal.SIGTERM
    assert len(captured_descriptors) == 2
    for descriptor in captured_descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == 9
    assert len(captured_roots) == 1
    assert not captured_roots[0].exists()


@pytest.mark.parametrize("failure_kind", ("exception", "baseexception"))
def test_pre_activation_setup_failure_restores_normalized_controller_signal_mask(
    tmp_path,
    monkeypatch,
    failure_kind,
) -> None:
    runner = _module()
    original_handlers = {
        selected: runner.signal.getsignal(selected) for selected in runner._CONTROLLER_SIGNALS
    }
    original_mask = frozenset(
        runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, runner._CONTROLLER_SIGNALS)
    )

    def fail_before_activation(cls, _value, *, owner=None):
        del cls, owner
        if failure_kind == "exception":
            raise runner.BatteryFailure("synthetic_setup_failure")
        raise runner.ControllerSignal(int(runner.signal.SIGTERM))

    monkeypatch.setattr(runner, "_validate_live_gate", lambda *_args: "a" * 40)
    monkeypatch.setattr(runner, "_controller_source_env_file", lambda _value: None)
    monkeypatch.setattr(
        runner._PinnedBarrierDirectory,
        "open",
        classmethod(fail_before_activation),
    )
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    barrier_dir = parent / "barrier"
    barrier_dir.mkdir(mode=0o700)
    args = SimpleNamespace(
        freeze_commit="a" * 40,
        bridge_stopped=True,
        source_env_file="",
        inter_run_barrier_dir=str(barrier_dir),
        keep_private_run_dir=False,
        report="",
    )

    expected = runner.BatteryFailure if failure_kind == "exception" else runner.ControllerSignal
    try:
        with pytest.raises(expected):
            runner.run_controller(args)

        final_mask = frozenset(runner.signal.pthread_sigmask(runner.signal.SIG_BLOCK, ()))
        assert not set(runner._CONTROLLER_SIGNALS).intersection(final_mask)
        assert {
            selected: runner.signal.getsignal(selected) for selected in runner._CONTROLLER_SIGNALS
        } == original_handlers
    finally:
        runner.signal.pthread_sigmask(runner.signal.SIG_SETMASK, original_mask)


def test_lifecycle_audit_projects_close_warning_and_exception_to_closed_codes(
    monkeypatch,
) -> None:
    runner = _module()
    import friday.mcp_runtime.client as mcp_client

    async def broken_close(*_args, **_kwargs):
        raise RuntimeError("private close detail")

    monkeypatch.setattr(mcp_client, "_bounded_stack_close", broken_close)
    audit = runner.LifecycleAudit()
    audit.install()
    try:
        logging_record = logging.LogRecord(
            "friday.mcp_runtime.client",
            30,
            __file__,
            1,
            "MCP server %s cleanup exceeded %.0fs",
            ("workspace", 5.0),
            None,
        )
        audit._handler.emit(logging_record)
        with pytest.raises(RuntimeError, match="private close detail"):
            asyncio.run(mcp_client._bounded_stack_close(None, alias="workspace"))
    finally:
        audit.close()

    assert audit.closed_failure_codes() == (
        "mcp_cleanup_exception",
        "mcp_cleanup_timeout_warning",
    )


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
    assert report["live_scenario_ids"] == ["D06", "D07", "D08"]
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
