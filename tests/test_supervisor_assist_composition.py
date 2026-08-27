from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import friday.orchestration.supervisor_assist_composition as composition
from friday.orchestration.supervisor_assist_activation import (
    AssistPromotionActivationMaterial,
)
from friday.orchestration.supervisor_assist_graph_adapter import SupervisorAssistGraphAdapter
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.permissions import AuthorizationService


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    values = {
        "semantic_supervisor_mode": "assist",
        "semantic_supervisor_tasks": ("compare_current_file_with_current_web",),
        "semantic_supervisor_max_steps": 6,
        "semantic_supervisor_max_review_rounds": 1,
        "semantic_supervisor_timeout_sec": 12.0,
        "files_dir": tmp_path,
        "max_upload_bytes": 4096,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _configured_material(mode: SupervisorMode) -> AssistPromotionActivationMaterial:
    material = object.__new__(AssistPromotionActivationMaterial)
    object.__setattr__(material, "configured", True)
    object.__setattr__(material, "requested_mode", mode)
    return cast(AssistPromotionActivationMaterial, material)


def test_composition_stays_absent_without_the_exact_promoted_configuration(
    storage: Any,
    tmp_path: Path,
) -> None:
    authorization = AuthorizationService(storage)
    graph_adapter = SupervisorAssistGraphAdapter(storage)
    common = {
        "primary": object(),
        "storage": storage,
        "authorization": authorization,
        "scheduler": object(),
        "activation_material": _configured_material(SupervisorMode.ASSIST),
        "graph_adapter": graph_adapter,
        "web": object(),
        "primary_model_runtime": object(),
    }

    assert (
        composition.build_supervisor_assist_production_runtime(
            settings=_settings(tmp_path, semantic_supervisor_tasks=("compare_archive_with_current_web",)),
            **common,
        )
        is None
    )
    assert (
        composition.build_supervisor_assist_production_runtime(
            settings=_settings(tmp_path, semantic_supervisor_max_review_rounds=0),
            **common,
        )
        is None
    )
    assert (
        composition.build_supervisor_assist_production_runtime(
            settings=_settings(tmp_path, semantic_supervisor_mode="shadow"),
            **common,
        )
        is None
    )
    assert (
        composition.build_supervisor_assist_production_runtime(
            settings=_settings(tmp_path, semantic_supervisor_timeout_sec=12.001),
            **common,
        )
        is None
    )


def test_composition_binds_one_controller_and_both_postcommit_observers(
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user("owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph_adapter = SupervisorAssistGraphAdapter(storage)
    controller_calls: list[dict[str, Any]] = []
    runtime_calls: list[dict[str, Any]] = []
    controller = object()
    runtime = object()

    def build_controller(**kwargs: Any) -> object:
        controller_calls.append(kwargs)
        return controller

    def build_runtime(**kwargs: Any) -> object:
        runtime_calls.append(kwargs)
        return runtime

    monkeypatch.setattr(composition, "SupervisorAssistController", build_controller)
    monkeypatch.setattr(composition, "SemanticSupervisorAssistRuntime", build_runtime)

    built = composition.build_supervisor_assist_production_runtime(
        settings=_settings(tmp_path),
        primary=object(),
        storage=storage,
        authorization=authorization,
        scheduler=object(),
        activation_material=_configured_material(SupervisorMode.ASSIST),
        graph_adapter=graph_adapter,
        web=SimpleNamespace(research=lambda **_kwargs: None),
        primary_model_runtime=object(),
    )

    assert built is runtime
    assert len(controller_calls) == 1
    assert controller_calls[0]["graph_adapter"] is graph_adapter
    assert controller_calls[0]["max_review_rounds"] == 1
    assert callable(controller_calls[0]["post_commit_observer"])
    assert len(runtime_calls) == 1
    assert runtime_calls[0]["controller"] is controller
    assert callable(runtime_calls[0]["ordinary_observer"])
