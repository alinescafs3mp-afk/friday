"""Content-free audit projection for engineer tool arguments."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel, ToolSpec
from friday.permissions import ActorContext, AuthorizationService


def _engineer_test_kernel(settings, storage) -> tuple[ExecutionKernel, ActorContext]:
    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())
    return kernel, authorization.actor_for_user("alice", source="test")


@pytest.mark.parametrize(
    "tool_name",
    [
        "engineer_hunt",
        "engineer_audit_host",
        "engineer_http_enum",
        "engineer_dns",
        "engineer_adversary_rehearsal",
    ],
)
def test_engineer_network_audit_projection_never_returns_host_or_ticket(
    tool_name: str,
) -> None:
    host = "  Private-Customer.Example.Test. "
    canonical_host = "private-customer.example.test"
    ticket = "signed-target-ticket.private-material.4815"

    details = ExecutionKernel._audit_details(  # noqa: SLF001
        tool_name,
        {
            "host": host,
            "target_ticket": ticket,
            "ports": [443, 22, True, 0, "8080"],
        },
    )

    assert details == {
        "host_sha256": hashlib.sha256(canonical_host.encode()).hexdigest(),
        "host_chars": len(host),
        "target_ticket_sha256": hashlib.sha256(ticket.encode()).hexdigest(),
        "target_ticket_chars": len(ticket),
        "ports_count": 5,
        "ports_valid_count": 2,
        "ports_min": 22,
        "ports_max": 443,
    }
    encoded = json.dumps(details, ensure_ascii=False, sort_keys=True)
    assert host not in encoded
    assert canonical_host not in encoded
    assert ticket not in encoded
    assert "target_ticket" not in details
    assert "host" not in details


def test_engineer_http_scalar_port_is_only_a_count_and_range() -> None:
    details = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_http_enum",
        {
            "host": "127.0.0.1",
            "target_ticket": "ticket-4815",
            "port": 8_443,
        },
    )

    assert details["ports_count"] == 1
    assert details["ports_valid_count"] == 1
    assert details["ports_min"] == details["ports_max"] == 8_443
    assert "port" not in details


@pytest.mark.parametrize(
    "tool_name",
    ["engineer_analyze_artifact", "engineer_decompile_artifact"],
)
def test_valid_raw_handle_stays_structural_and_invalid_handle_is_fingerprinted(
    tool_name: str,
) -> None:
    valid_raw_id = "raw_0123456789abcdef"
    valid = ExecutionKernel._audit_details(  # noqa: SLF001
        tool_name,
        {"raw_id": valid_raw_id},
    )
    assert valid == {"raw_object_id": valid_raw_id}

    invalid_raw_id = "raw_private-customer-secret"
    invalid = ExecutionKernel._audit_details(  # noqa: SLF001
        tool_name,
        {"raw_id": invalid_raw_id},
    )
    assert invalid == {
        "raw_id_sha256": hashlib.sha256(invalid_raw_id.encode()).hexdigest(),
        "raw_id_chars": len(invalid_raw_id),
    }
    assert invalid_raw_id not in json.dumps(invalid, ensure_ascii=False)


def test_decompile_audit_projection_ignores_report_content_and_paths() -> None:
    raw_id = "raw_0123456789abcdef"
    private_path = "/home/jericho/private/customer.exe"
    pseudocode = 'const char *password = "4815";'

    details = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_decompile_artifact",
        {
            "raw_id": raw_id,
            "path": private_path,
            "pseudocode": pseudocode,
            "function_name": "private_customer_function",
        },
    )

    assert details == {"raw_object_id": raw_id}
    encoded = json.dumps(details, ensure_ascii=False, sort_keys=True)
    assert private_path not in encoded
    assert pseudocode not in encoded
    assert "private_customer_function" not in encoded


def test_patch_operations_have_ordered_canonical_digest_and_closed_kind_summary() -> None:
    raw_id = "raw_fedcba9876543210"
    private_bytes = "50415353574f52442d34383135"
    operations = [
        {"kind": "write_at", "offset": 12, "bytes": private_bytes},
        {
            "kind": "replace_bytes",
            "find": "736563726574",
            "replace": "7265646163746564",
        },
        {"kind": "private-operation-name", "bytes": "00"},
    ]
    same_operations_with_reordered_keys = [
        {"bytes": private_bytes, "offset": 12, "kind": "write_at"},
        {
            "replace": "7265646163746564",
            "find": "736563726574",
            "kind": "replace_bytes",
        },
        {"bytes": "00", "kind": "private-operation-name"},
    ]

    first = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_patch_artifact",
        {"raw_id": raw_id, "operations": operations, "filename": "private.bin"},
    )
    reordered = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_patch_artifact",
        {"raw_id": raw_id, "operations": same_operations_with_reordered_keys},
    )

    canonical = json.dumps(
        operations,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    assert first["raw_object_id"] == raw_id
    assert first["operations_count"] == 3
    assert first["operation_kinds"] == ["replace_bytes", "write_at"]
    assert first["operations_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert reordered["operations_sha256"] == first["operations_sha256"]
    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert private_bytes not in encoded
    assert "private-operation-name" not in encoded
    assert "private.bin" not in encoded
    assert "operations" not in first


def test_reversing_patch_operation_order_changes_the_audit_fingerprint() -> None:
    operations = [
        {"kind": "write_at", "offset": 1, "bytes": "aa"},
        {"kind": "write_at", "offset": 2, "bytes": "bb"},
    ]
    forward = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_patch_artifact",
        {"operations": operations},
    )
    reversed_order = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_patch_artifact",
        {"operations": list(reversed(operations))},
    )

    assert forward["operations_sha256"] != reversed_order["operations_sha256"]


def test_engineer_local_inventory_has_no_argument_projection() -> None:
    assert (
        ExecutionKernel._audit_details(  # noqa: SLF001
            "engineer_local_tools",
            {"unexpected": "private"},
        )
        == {}
    )


@pytest.mark.asyncio
async def test_configured_network_scope_survives_durable_audit_as_an_opaque_reference(
    settings,
    storage,
) -> None:
    kernel, actor = _engineer_test_kernel(settings, storage)
    cidr = "192.168.1.0/24"

    async def handler(*, actor, cidr: str, profile: str):  # noqa: ANN001
        del actor, cidr, profile
        return {"ok": True, "active_probes_sent": True}

    kernel.register(
        ToolSpec(
            name="engineer_scan_configured_network",
            description="synthetic configured-network observation",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            handler=handler,
        )
    )

    result = await kernel.execute(
        "engineer_scan_configured_network",
        {"cidr": cidr, "profile": "discover"},
        actor=actor,
    )

    assert result.success is True
    rows = [
        row
        for row in storage.list_audit_log("alice", limit=20)
        if row["target_id"] == "engineer_scan_configured_network"
    ]
    assert len(rows) == 1
    payload = json.loads(str(rows[0]["after_json"] or "{}"))
    assert str(payload["cidr_ref"]).startswith("fpref_")
    assert payload["cidr_chars"] == len(cidr)
    assert payload["network_profile"] == "discover"
    assert cidr not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_cancelled_engineer_observation_gets_an_uncertain_terminal_audit(
    settings,
    storage,
) -> None:
    kernel, actor = _engineer_test_kernel(settings, storage)
    started = asyncio.Event()

    async def handler(*, actor, host: str, target_ticket: str):  # noqa: ANN001
        del actor, host, target_ticket
        started.set()
        await asyncio.Event().wait()

    kernel.register(
        ToolSpec(
            name="engineer_audit_host",
            description="synthetic cancelled engineer observation",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            handler=handler,
        )
    )
    task = asyncio.create_task(
        kernel.execute(
            "engineer_audit_host",
            {"host": "private.example.test", "target_ticket": "private-ticket"},
            actor=actor,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = [
        row for row in storage.list_audit_log("alice", limit=20) if row["target_id"] == "engineer_audit_host"
    ]
    assert len(rows) == 1
    payload = json.loads(str(rows[0]["after_json"] or "{}"))
    assert payload["success"] is False
    assert payload["reason"] == "uncertain"
    assert str(payload["host_ref"]).startswith("fpref_")
    assert str(payload["target_ticket_ref"]).startswith("fpref_")
    assert "private.example.test" not in json.dumps(payload)
    assert "private-ticket" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["engineer_analyze_artifact", "engineer_decompile_artifact"],
)
async def test_engineer_false_envelope_is_a_failed_kernel_result_and_audit(
    settings,
    storage,
    tool_name: str,
) -> None:
    kernel, actor = _engineer_test_kernel(settings, storage)

    async def handler(*, actor, raw_id: str):  # noqa: ANN001
        del actor, raw_id
        return {"ok": False, "error": "file_unavailable"}

    kernel.register(
        ToolSpec(
            name=tool_name,
            description="synthetic refused engineer observation",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            handler=handler,
        )
    )
    result = await kernel.execute(
        tool_name,
        {"raw_id": "raw_0123456789abcdef"},
        actor=actor,
    )

    assert result.success is False
    assert result.data is None
    assert result.error == "Engineer tool refused: file_unavailable"
    rows = [row for row in storage.list_audit_log("alice", limit=20) if row["target_id"] == tool_name]
    assert len(rows) == 1
    payload = json.loads(str(rows[0]["after_json"] or "{}"))
    assert payload["success"] is False
    assert payload["reason"] == "failed"
