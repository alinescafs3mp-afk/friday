from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import replace

import pytest

from friday.host_control.adapters.base import ExecutionSpec
from friday.host_control.contracts import (
    PROTOCOL_VERSION,
    ExecutableAttestation,
    ExecutionProfile,
    RiskClass,
    WireRequest,
    canonical_json_bytes,
)
from friday.host_control.plans import HostActionPlan
from friday.host_control.policy import NetworkPolicy
from friday.host_control.receipts import HostActionReceipt
from friday_host_agent.__main__ import _require_exact_peer_uid
from friday_host_agent.adapter_registry import AdapterRegistry
from friday_host_agent.authentication import HMACAuthenticator, ReplayGuard
from friday_host_agent.daemon import HostAgentDaemon
from friday_host_agent.inventory import ExecutableInventory, PackageIdentity
from friday_host_agent.process_runner import ProcessResult
from friday_host_agent.receipts import ReceiptSigner, build_receipt

_KEY = b"h" * 32
_DIGEST = "a" * 64


def test_production_entrypoint_allows_only_its_exact_non_root_peer_uid() -> None:
    _require_exact_peer_uid([1000], runtime_uid=1000)
    for peer_uids, runtime_uid in (
        ([0], 1000),
        ([1001], 1000),
        ([1000, 1000], 1000),
        ([1000], 0),
    ):
        with pytest.raises(ValueError, match="exactly match"):
            _require_exact_peer_uid(peer_uids, runtime_uid=runtime_uid)


class _NoPackages:
    def resolve(self, path: str) -> PackageIdentity | None:
        del path
        return None


def _daemon(*, client_timeout_sec: float = 10.0):
    auth = HMACAuthenticator(_KEY, agent_id="host-agent:one")
    inventory = ExecutableInventory(
        (), package_resolver=_NoPackages(), version_probes={}, allowed_owner_uids=(0,)
    )
    registry = AdapterRegistry((), inventory=inventory)
    return (
        HostAgentDaemon(
            agent_id="host-agent:one",
            authenticator=auth,
            replay_guard=ReplayGuard(),
            inventory=inventory,
            registry=registry,
            allowed_peer_uids=frozenset({1000}),
            build_id="test-build",
            client_timeout_sec=client_timeout_sec,
        ),
        auth,
    )


def _request(auth: HMACAuthenticator, method: str, body: dict, *, sequence: int = 1) -> bytes:
    envelope = auth.create_envelope(
        request_id=f"request:{sequence}",
        sequence=sequence,
        issued_at=1_000,
        expires_at=1_030,
        method=method,
        job_id=f"job:{sequence}",
        actor_id="actor:one",
        own_id="owner:one",
        idempotency_key=f"idempotency:{sequence}",
        plan_digest=_DIGEST,
        body=body,
    )
    return WireRequest.create(envelope, body).encode()


async def test_socket_read_deadline_releases_an_idle_client(tmp_path) -> None:
    daemon, auth = _daemon(client_timeout_sec=0.05)
    daemon._allowed_peer_uids = frozenset({os.geteuid()})  # noqa: SLF001
    socket_path = tmp_path / "agent.sock"
    serving = asyncio.create_task(daemon.serve(socket_path))
    try:
        for _attempt in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        assert socket_path.exists()
        reader, writer = await asyncio.open_unix_connection(socket_path)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=1.0)
        finally:
            writer.close()
            await writer.wait_closed()
        response = json.loads(raw)
        signature = response.pop("signature")
        assert response["ok"] is False
        assert response["result"] == {"error_code": "invalid_framing"}
        assert auth.verify_bytes(
            canonical_json_bytes(response), signature, domain=b"friday-host-agent-response-v1"
        )
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving


async def test_agent_stop_removes_only_socket_and_preserves_private_parent(tmp_path) -> None:
    daemon, _auth = _daemon()
    daemon._allowed_peer_uids = frozenset({os.geteuid()})  # noqa: SLF001
    socket_parent = tmp_path
    socket_parent.chmod(0o700)
    socket_path = socket_parent / "a.sock"
    serving = asyncio.create_task(daemon.serve(socket_path))
    try:
        for _attempt in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        assert socket_path.is_socket()
    finally:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving

    assert socket_parent.is_dir()
    assert socket_parent.stat().st_mode & 0o777 == 0o700
    assert not socket_path.exists()


def test_authenticated_health_and_handshake_are_signed_and_bounded() -> None:
    daemon, auth = _daemon()
    response = json.loads(daemon.handle_request(_request(auth, "Health", {}), peer_uid=1000, now=1_001))
    signature = response.pop("signature")
    assert response["ok"] is True
    assert response["request_id"] == "request:1"
    health = response["result"]
    assert health["agent_id"] == "host-agent:one"
    assert health["protocol_versions"] == [PROTOCOL_VERSION]
    assert health["adapter_catalog_digest"]
    assert health["network_policy_digest"] == NetworkPolicy(connected_cidrs=()).digest
    assert auth.verify_bytes(
        canonical_json_bytes(response), signature, domain=b"friday-host-agent-response-v1"
    )

    handshake = json.loads(
        daemon.handle_request(
            _request(
                auth,
                "Handshake",
                {"client_protocol_version": PROTOCOL_VERSION},
                sequence=2,
            ),
            peer_uid=1000,
            now=1_001,
        )
    )
    assert handshake["ok"] is True
    assert handshake["result"]["accepted"] is True
    assert handshake["result"]["network_policy_digest"] == health["network_policy_digest"]


def test_daemon_rejects_wrong_peer_replay_and_unexposed_methods() -> None:
    daemon, auth = _daemon()
    raw = _request(auth, "Health", {})
    denied = json.loads(daemon.handle_request(raw, peer_uid=2000, now=1_001))
    assert denied["ok"] is False
    assert denied["result"]["error_code"] == "peer_not_allowed"

    assert json.loads(daemon.handle_request(raw, peer_uid=1000, now=1_001))["ok"] is True
    replay = json.loads(daemon.handle_request(raw, peer_uid=1000, now=1_001))
    assert replay["ok"] is False
    assert replay["result"]["error_code"] == "replayed_request"

    unknown = json.loads(
        daemon.handle_request(_request(auth, "RunShell", {}, sequence=3), peer_uid=1000, now=1_001)
    )
    assert unknown["ok"] is False
    assert unknown["result"]["error_code"] == "unknown_method"


def test_receipt_is_signed_redacted_and_preserves_unknown_outcome() -> None:
    auth = HMACAuthenticator(_KEY, agent_id="host-agent:one")
    executable = ExecutableAttestation(
        schema_version=1,
        canonical_path="/usr/bin/synthetic",
        device=1,
        inode=2,
        mode=0o755,
        owner_uid=0,
        owner_gid=0,
        size_bytes=10,
        mtime_ns=20,
        sha256="b" * 64,
        package_name="synthetic-package",
        package_version="1.0",
        architecture="amd64",
        observed_version="synthetic 1.0",
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=1,
    )
    execution = ExecutionSpec(
        executable=executable.canonical_path,
        argv=(
            executable.canonical_path,
            "--token",
            "private-value",
            "https://user:pass@example.test/path?debug=opaque",
        ),
        profile=ExecutionProfile.CLI_LOCAL_READONLY,
        timeout_sec=5,
        max_output_bytes=1024,
    )
    plan = HostActionPlan(
        schema_version=1,
        plan_id="plan:receipt",
        actor_user_id="actor:one",
        actor_own_id="owner:one",
        conversation_id="conversation:one",
        source_message_id="message:one",
        continuation_work_item_id=None,
        host_agent_id="host-agent:one",
        idempotency_key="idempotency:one",
        capability_id="data.synthetic.run",
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=1,
        adapter_digest="d" * 64,
        action_id="run",
        normalized_arguments_json=canonical_json_bytes({"value": "opaque"}),
        risk_class=RiskClass.LOCAL_READONLY,
        security_id="host.actions.execute",
        execution_profile=ExecutionProfile.CLI_LOCAL_READONLY,
        timeout_sec=5,
        max_output_bytes=1024,
        target_snapshot_json=None,
        workspace_grants=(),
        executable_attestation_digest=executable.digest,
        created_at=1_000,
        expires_at=1_300,
    )
    result = ProcessResult(
        outcome="unknown",
        effect_boundary_crossed=True,
        unit_id="friday-host-0123456789abcdef.service",
        cgroup_identity="systemd-user:friday-host-0123456789abcdef.service",
        exit_code=None,
        signal=None,
        started_at=1.0,
        finished_at=2.0,
        timed_out=False,
        cancelled=False,
        output_truncated=True,
        stdout=b"untrusted output",
        stderr=b"uncertain",
        error_code="termination_unconfirmed",
    )
    signer = ReceiptSigner(auth)
    receipt = signer.sign(
        build_receipt(
            job_id="job_0123456789abcdef",
            plan=plan,
            host_agent_version="test-build",
            executable_attestation=executable,
            execution=execution,
            result=result,
        )
    )
    serialized = canonical_json_bytes(receipt.to_payload())
    assert b"private-value" not in serialized
    assert b"opaque" not in serialized
    assert b"pass" not in serialized
    assert receipt.effect_outcome.value == "unknown"
    assert receipt.process.finished_at is None
    assert HostActionReceipt.from_payload(receipt.to_payload()) == receipt
    assert signer.verify(receipt)
    assert not signer.verify(replace(receipt, plan_digest="c" * 64))
