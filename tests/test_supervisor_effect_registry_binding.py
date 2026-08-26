from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.orchestration.capability_binding import (
    expected_effect_capability_snapshot,
    operational_capability_snapshot,
    operational_effect_capability_snapshot,
)
from friday.orchestration.supervisor_contracts import (
    CapabilityEffectClass,
    SupervisorContractError,
)
from friday.organs import ServiceContext
from friday.organs.obsidian import OBSIDIAN_WRITE
from friday.organs.obsidian.runtime import ObsidianRuntime
from friday.organs.obsidian.tools import build_obsidian_tools
from friday.permissions import AuthorizationService
from tools import build_semantic_supervisor_promotion_evidence as evidence_cli

_EFFECT_TOOLS = frozenset({"obsidian_create_note", "obsidian_append_note"})


def _composition(
    *,
    enabled: bool = True,
    register_permission: bool = True,
    registered_tools: frozenset[str] = _EFFECT_TOOLS,
) -> tuple[object, ExecutionKernel, AuthorizationService, ObsidianRuntime]:
    settings = SimpleNamespace(obsidian_enabled=enabled)
    storage = SimpleNamespace(marker="private-storage")
    authorization = AuthorizationService(storage)  # type: ignore[arg-type]
    if register_permission:
        authorization.register_capability(OBSIDIAN_WRITE)
    kernel = ExecutionKernel(authorization, settings)  # type: ignore[arg-type]
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    runtime = ObsidianRuntime(settings, storage, object())  # type: ignore[arg-type]
    context = ServiceContext(
        settings=settings,  # type: ignore[arg-type]
        storage=storage,
        kg=kernel.kg,
        ingestion=kernel.ingestion,
        auth=authorization,
        obsidian=runtime,
    )
    for tool in build_obsidian_tools(context):
        if tool.name in registered_tools:
            kernel.register(tool)
    return settings, kernel, authorization, runtime


def _snapshot(
    composition: tuple[object, ExecutionKernel, AuthorizationService, ObsidianRuntime],
) -> Any:
    settings, kernel, authorization, runtime = composition
    return operational_effect_capability_snapshot(
        settings=settings,
        kernel=kernel,
        authorization=authorization,
        obsidian_runtime=runtime,
    )


def test_effect_registry_binding_is_stable_body_free_and_write_contour_only() -> None:
    first = _snapshot(_composition())
    second = _snapshot(_composition())

    assert first.digest_hex() == second.digest_hex()
    assert first.digest_hex() == expected_effect_capability_snapshot().digest_hex()
    assert first.digest_hex() != operational_capability_snapshot().digest_hex()
    assert [(binding.supervisor_capability_id, binding.tool_id) for binding in first.bindings] == [
        ("obsidian_note_mutation:create", "obsidian_create_note"),
        ("obsidian_note_mutation:append", "obsidian_append_note"),
    ]
    assert all(
        binding.available
        and binding.effect_class is CapabilityEffectClass.WRITE
        and binding.security_id == "obsidian.write"
        for binding in first.bindings
    )
    encoded = json.dumps(
        [binding.identity_payload() for binding in first.bindings],
        ensure_ascii=True,
        sort_keys=True,
    )
    for forbidden in (
        "private-storage",
        "path",
        "content",
        "text",
        "actor",
        "conversation",
        "request",
    ):
        assert forbidden not in encoded.casefold()


def test_operator_can_derive_effect_registry_before_activation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert evidence_cli.main(["effect-registry-binding"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {
        "schema": "friday.semantic-supervisor-effect-registry-binding-receipt.v1",
        "effect_registry_binding_sha256": expected_effect_capability_snapshot().digest_hex(),
        "body_free": True,
        "runtime_composition_verified": False,
        "activation_performed": False,
        "write_effect_authorized": False,
    }


@pytest.mark.parametrize(
    ("enabled", "permission", "tools"),
    (
        (False, True, _EFFECT_TOOLS),
        (True, False, _EFFECT_TOOLS),
        (True, True, frozenset({"obsidian_append_note"})),
        (True, True, frozenset({"obsidian_create_note"})),
    ),
)
def test_effect_registry_binding_rejects_disabled_or_unregistered_contour(
    enabled: bool,
    permission: bool,
    tools: frozenset[str],
) -> None:
    with pytest.raises(SupervisorContractError):
        _snapshot(
            _composition(
                enabled=enabled,
                register_permission=permission,
                registered_tools=tools,
            )
        )


@pytest.mark.parametrize("mismatch", ("risk", "schema", "runtime", "authorization"))
def test_effect_registry_binding_rejects_every_composition_mismatch(mismatch: str) -> None:
    settings, kernel, authorization, runtime = _composition()
    selected_runtime = runtime
    selected_authorization = authorization
    if mismatch == "risk":
        tool = kernel.get_tool("obsidian_create_note")
        assert tool is not None
        tool.risk = "observe"
    elif mismatch == "schema":
        tool = kernel.get_tool("obsidian_append_note")
        assert tool is not None
        tool.parameters = {**tool.parameters, "additionalProperties": True}
    elif mismatch == "runtime":
        selected_runtime = ObsidianRuntime(settings, authorization.storage, object())  # type: ignore[arg-type]
    else:
        assert mismatch == "authorization"
        selected_authorization = AuthorizationService(authorization.storage)
        selected_authorization.register_capability(OBSIDIAN_WRITE)

    with pytest.raises(SupervisorContractError):
        operational_effect_capability_snapshot(
            settings=settings,
            kernel=kernel,
            authorization=selected_authorization,
            obsidian_runtime=selected_runtime,
        )
