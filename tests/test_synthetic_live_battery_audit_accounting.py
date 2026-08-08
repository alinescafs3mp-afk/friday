"""Closed accounting regressions for the synthetic live-battery tool ledger."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _AuditStorage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def execute(self, _query: str, _arguments: tuple[Any, ...]) -> _Rows:
        return _Rows(self._rows)


def _row(name: str, reason: str, success: bool) -> dict[str, str]:
    return {
        "target_id": name,
        "after_json": json.dumps({"reason": reason, "success": success, "source": "synthetic"}),
    }


def _kernel() -> SimpleNamespace:
    return SimpleNamespace(
        _tools={
            "list_tags": SimpleNamespace(risk="observe"),
            "make_file": SimpleNamespace(risk="observe"),
            "remind": SimpleNamespace(risk="mutate"),
        }
    )


def _cases(pass_index: int) -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["A"])
    return [case for case in battery.expand_manifest_cases(manifest) if case.pass_index == pass_index]


@pytest.mark.asyncio
async def test_production_kernel_emits_the_lifecycle_consumed_by_the_harness(
    settings: Any,
    storage: Any,
) -> None:
    from friday.execution_kernel import ExecutionKernel
    from friday.permissions import AuthorizationService

    storage.ensure_user("synthetic-audit-user", preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    actor = authorization.actor_for_user("synthetic-audit-user", source="synthetic")

    async def observe(*, actor: Any) -> dict[str, bool]:  # noqa: ARG001
        return {"observed": True}

    async def mutate(*, actor: Any, what: str, when: str) -> dict[str, bool]:  # noqa: ARG001
        return {"mutated": True}

    kernel._tools["list_tags"].handler = observe  # noqa: SLF001 - production audit contract
    kernel._tools["remind"].handler = mutate  # noqa: SLF001 - production audit contract

    observe_cursor = battery._tool_audit_cursor(storage, "synthetic-audit-user")
    observe_result = await kernel.execute("list_tags", {}, actor=actor)
    observe_audit = battery._tool_audit_delta(
        storage,
        "synthetic-audit-user",
        observe_cursor,
    )
    mutate_cursor = battery._tool_audit_cursor(storage, "synthetic-audit-user")
    mutate_result = await kernel.execute(
        "remind",
        {"what": "synthetic", "when": "2035-09-01"},
        actor=actor,
    )
    mutate_audit = battery._tool_audit_delta(
        storage,
        "synthetic-audit-user",
        mutate_cursor,
    )

    assert observe_result.success is True
    assert mutate_result.success is True
    assert battery._audit_lifecycle_exact(kernel, ["list_tags"], observe_audit) is True
    assert battery._audit_lifecycle_exact(kernel, ["remind"], mutate_audit) is True


def test_observing_call_has_one_terminal_audit_row() -> None:
    audit = battery._tool_audit_delta(
        _AuditStorage([_row("list_tags", "ok", True)]),
        "synthetic-user",
        0,
    )

    assert audit == battery.ToolAuditDelta(
        terminal=(("list_tags", True),),
        started=(),
        row_count=1,
        valid=True,
    )
    assert battery._audit_lifecycle_exact(_kernel(), ["list_tags"], audit) is True


def test_mutating_call_has_started_and_terminal_rows_without_double_counting_dispatch() -> None:
    audit = battery._tool_audit_delta(
        _AuditStorage(
            [
                _row("remind", "started", True),
                _row("remind", "ok", True),
            ]
        ),
        "synthetic-user",
        0,
    )

    assert audit.started == ("remind",)
    assert audit.terminal == (("remind", True),)
    assert audit.row_count == 2
    assert battery._audit_lifecycle_exact(_kernel(), ["remind"], audit) is True


def test_effect_accounting_uses_kernel_calls_but_started_rows_follow_declared_risk() -> None:
    audit = battery.ToolAuditDelta(
        terminal=(("make_file", True),),
        started=(),
        row_count=1,
        valid=True,
    )

    assert battery._effectful_tool_calls(_kernel(), ["make_file"]) == 1
    assert battery._audit_lifecycle_exact(_kernel(), ["make_file"], audit) is True


def test_missing_start_failed_terminal_and_malformed_payload_fail_closed() -> None:
    missing_start = battery.ToolAuditDelta(
        terminal=(("remind", True),),
        started=(),
        row_count=1,
        valid=True,
    )
    failed = battery.ToolAuditDelta(
        terminal=(("remind", False),),
        started=("remind",),
        row_count=2,
        valid=True,
    )
    malformed = battery._tool_audit_delta(
        _AuditStorage(
            [
                {
                    "target_id": "remind",
                    "after_json": json.dumps({"reason": "started", "success": "yes"}),
                }
            ]
        ),
        "synthetic-user",
        0,
    )

    assert battery._audit_lifecycle_exact(_kernel(), ["remind"], missing_start) is False
    assert battery._audit_lifecycle_exact(_kernel(), ["remind"], failed) is False
    assert malformed.valid is False
    assert battery._audit_lifecycle_exact(_kernel(), ["remind"], malformed) is False


def test_pass_tools_reject_hidden_failed_prefetch_and_unreported_late_file_call() -> None:
    reminder_cases = _cases(8)
    reminder_ids = [case.id for case in reminder_cases]
    reminder_public = {case_id: ["remind"] for case_id in reminder_ids}
    reminder_kernel = {case_id: ["remind"] for case_id in reminder_ids}
    reminder_public[reminder_ids[0]] = []

    assert (
        battery._pass_tool_ledgers_exact(
            reminder_cases,
            reminder_public,
            reminder_kernel,
        )
        is False
    )

    attachment_cases = _cases(7)
    attachment_ids = [case.id for case in attachment_cases]
    attachment_public = {case_id: [] for case_id in attachment_ids}
    attachment_kernel = {case_id: [] for case_id in attachment_ids}
    attachment_kernel[attachment_ids[-1]] = ["make_file"]

    assert (
        battery._pass_tool_ledgers_exact(
            attachment_cases,
            attachment_public,
            attachment_kernel,
        )
        is False
    )


def test_pass_audit_reconciliation_accepts_exact_two_row_mutator_lifecycles_only() -> None:
    cases = _cases(8)
    kernel_by_case = {case.id: ["remind"] for case in cases}
    audit_by_case = {
        case.id: battery.ToolAuditDelta(
            terminal=(("remind", True),),
            started=("remind",),
            row_count=2,
            valid=True,
        )
        for case in cases
    }
    deltas = {case.id: {"audit_tools": 2} for case in cases}

    assert (
        battery._pass_audit_ledgers_exact(
            _kernel(),
            cases,
            kernel_by_case,
            audit_by_case,
            deltas,
            2 * len(cases),
        )
        is True
    )

    first_id = cases[0].id
    incomplete = dict(audit_by_case)
    incomplete[first_id] = battery.ToolAuditDelta(
        terminal=(("remind", True),),
        started=(),
        row_count=1,
        valid=True,
    )
    incomplete_deltas = dict(deltas)
    incomplete_deltas[first_id] = {"audit_tools": 1}
    assert (
        battery._pass_audit_ledgers_exact(
            _kernel(),
            cases,
            kernel_by_case,
            incomplete,
            incomplete_deltas,
            2 * len(cases) - 1,
        )
        is False
    )

    extra_row = dict(deltas)
    extra_row[first_id] = {"audit_tools": 3}
    assert (
        battery._pass_audit_ledgers_exact(
            _kernel(),
            cases,
            kernel_by_case,
            audit_by_case,
            extra_row,
            2 * len(cases) + 1,
        )
        is False
    )
