from __future__ import annotations

import os
import subprocess
from dataclasses import replace

import pytest

from friday.host_control.adapters.base import (
    ActionSpec,
    AdapterSpec,
    ExecutableRequirement,
    ExecutionSpec,
    PackageRequirement,
)
from friday.host_control.contracts import (
    ContractError,
    ExecutableAttestation,
    ExecutionProfile,
    RiskClass,
)
from friday.host_control.plans import HostActionPlan, create_action_plan
from friday_host_agent.adapter_registry import AdapterRegistry, AdapterValidationError
from friday_host_agent.executable_attestation import (
    ExecutableAttestationError,
    attest_executable,
    verify_executable,
)
from friday_host_agent.inventory import ExecutableInventory, InventoryEntry, PackageIdentity


class _Packages:
    def __init__(self, identity: PackageIdentity | None) -> None:
        self.identity = identity

    def resolve(self, path: str) -> PackageIdentity | None:
        del path
        return self.identity


class _RegistryInventoryFixture:
    """Start registry-only tests at the already-authenticated inventory boundary."""

    def __init__(self, delegate: ExecutableInventory) -> None:
        entry = delegate.inspect("data.echo")
        assert entry.attestation is not None
        attestation = replace(entry.attestation, owner_uid=0)
        self._entry = replace(entry, attestation=attestation)

    def inspect(self, adapter_id: str) -> InventoryEntry:
        if adapter_id != self._entry.adapter_id:
            raise KeyError(adapter_id)
        return self._entry


class _EchoAdapter:
    def __init__(self, path: str) -> None:
        self.spec = AdapterSpec(
            adapter_id="data.echo",
            adapter_schema_version=1,
            implementation_version=3,
            summary="Bounded literal-output adapter",
            categories=("data",),
            supported_platforms=("ubuntu",),
            packages=(PackageRequirement("apt", "coreutils"),),
            executable=ExecutableRequirement("printf", "coreutils", (path,)),
            actions=(
                ActionSpec(
                    action_id="render",
                    capability_id="data.echo.render",
                    summary="Render one bounded literal",
                    security_id="host.actions.execute",
                    risk_class=RiskClass.LOCAL_READONLY,
                    execution_profile=ExecutionProfile.CLI_LOCAL_READONLY,
                    input_schema_id="echo_render_v1",
                    output_parser_id="bounded_text_v1",
                    timeout_sec=5,
                    max_output_bytes=1024,
                ),
            ),
        )

    def normalize_arguments(self, action_id: str, arguments: dict) -> dict:
        if action_id != "render" or set(arguments) != {"text"}:
            raise ContractError("echo arguments are closed")
        text = arguments["text"]
        if not isinstance(text, str) or not 1 <= len(text) <= 80:
            raise ContractError("echo text is invalid")
        return {"text": text}

    def build_execution(self, plan: HostActionPlan, attestation: ExecutableAttestation) -> ExecutionSpec:
        normalized_arguments = self.normalize_arguments(plan.action_id, plan.normalized_arguments)
        return ExecutionSpec(
            executable=attestation.canonical_path,
            argv=(attestation.canonical_path, "%s", normalized_arguments["text"]),
            profile=ExecutionProfile.CLI_LOCAL_READONLY,
            timeout_sec=5,
            max_output_bytes=1024,
        )


def _inventory(adapter: _EchoAdapter, package: PackageIdentity | None = None) -> ExecutableInventory:
    return ExecutableInventory(
        (adapter.spec,),
        package_resolver=_Packages(package or PackageIdentity("coreutils", "9.9", "amd64")),
        version_probes={adapter.spec.adapter_id: lambda _path, _descriptor: "printf (coreutils) 9.9"},
        allowed_owner_uids=(os.geteuid(),),
    )


def _owned_executable(tmp_path) -> str:
    executable = tmp_path / "owned-executable"
    executable.write_bytes(b"code-owned test executable\n")
    executable.chmod(0o700)
    return str(executable)


def test_attestation_round_trips_through_the_shared_dto_and_detects_replacement(tmp_path) -> None:
    executable = tmp_path / "reviewed-tool"
    executable.write_bytes(b"reviewed executable\n")
    executable.chmod(0o700)
    observed = attest_executable(
        executable,
        allowed_paths=(executable,),
        allowed_owner_uids=(os.getuid(),),
        package_name="synthetic-package",
        package_version="1.2.3-1",
        architecture="amd64",
        adapter_id="data.synthetic",
        adapter_schema_version=1,
        implementation_version=2,
        observed_version="synthetic 1.2.3",
    )

    assert ExecutableAttestation.from_payload(observed.to_payload()) == observed
    assert observed.adapter_id == "data.synthetic"
    assert observed.observed_version == "synthetic 1.2.3"
    executable.write_bytes(executable.read_bytes() + b"changed")
    executable.chmod(0o700)
    with pytest.raises(ExecutableAttestationError, match="changed"):
        verify_executable(observed)


def test_attestation_rejects_symlink_and_writable_executable(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"reviewed executable\n")
    target.chmod(0o722)
    link = tmp_path / "link"
    link.symlink_to(target)
    metadata = {
        "allowed_owner_uids": (os.getuid(),),
        "package_name": "synthetic-package",
        "package_version": "1",
        "architecture": "amd64",
        "adapter_id": "data.synthetic",
        "adapter_schema_version": 1,
        "implementation_version": 1,
        "observed_version": "v1",
    }
    with pytest.raises(ExecutableAttestationError, match="without following"):
        attest_executable(link, allowed_paths=(link,), **metadata)
    with pytest.raises(ExecutableAttestationError, match="could not be opened"):
        attest_executable(tmp_path / "missing", allowed_paths=(tmp_path / "missing",), **metadata)
    with pytest.raises(ExecutableAttestationError, match="writable"):
        attest_executable(target, allowed_paths=(target,), **metadata)


def test_inventory_never_runs_version_probe_before_executable_metadata_is_safe(tmp_path) -> None:
    executable = tmp_path / "unsafe-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o722)
    probes = 0

    def probe(_path: str, _descriptor: int) -> str:
        nonlocal probes
        probes += 1
        raise AssertionError("unsafe executable reached its version probe")

    adapter = _EchoAdapter(str(executable))
    inventory = ExecutableInventory(
        (adapter.spec,),
        package_resolver=_Packages(PackageIdentity("coreutils", "9.9", "amd64")),
        version_probes={adapter.spec.adapter_id: probe},
        allowed_owner_uids=(os.getuid(),),
    )

    entry = inventory.inspect(adapter.spec.adapter_id)
    assert entry.state == "unattested"
    assert entry.attestation is None
    assert probes == 0


def test_inventory_version_probe_executes_held_inode_and_rejects_path_swap(tmp_path) -> None:
    executable = tmp_path / "reviewed-tool"
    replacement = tmp_path / "replacement-tool"
    marker = tmp_path / "replacement-ran"
    executable.write_text("#!/bin/sh\nprintf 'approved 1.0\\n'\n", encoding="utf-8")
    replacement.write_text(
        f"#!/bin/sh\nprintf attacker > {marker}\nprintf 'attacker 9.9\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    replacement.chmod(0o700)
    observed: list[str] = []

    def probe(path: str, descriptor: int) -> str:
        os.replace(replacement, path)
        completed = subprocess.run(
            (path,),
            executable=f"/proc/self/fd/{descriptor}",
            pass_fds=(descriptor,),
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=3,
        )
        assert completed.returncode == 0
        value = completed.stdout.decode("utf-8").strip()
        observed.append(value)
        return value

    adapter = _EchoAdapter(str(executable))
    inventory = ExecutableInventory(
        (adapter.spec,),
        package_resolver=_Packages(PackageIdentity("coreutils", "9.9", "amd64")),
        version_probes={adapter.spec.adapter_id: probe},
        allowed_owner_uids=(os.getuid(),),
    )

    entry = inventory.inspect(adapter.spec.adapter_id)
    assert entry.state == "unattested"
    assert entry.attestation is None
    assert observed == ["approved 1.0"]
    assert not marker.exists()


def test_reviewed_inventory_is_not_available_without_package_ownership(tmp_path) -> None:
    adapter = _EchoAdapter(_owned_executable(tmp_path))
    inventory = _inventory(adapter, PackageIdentity("not-coreutils", "1", "amd64"))
    entry = inventory.inspect(adapter.spec.adapter_id)
    assert entry.state == "unattested"
    assert entry.attestation is None
    assert "ownership" in str(entry.reason)


def test_registry_consumes_exact_shared_plan_and_preserves_target_snapshot(tmp_path) -> None:
    adapter = _EchoAdapter(_owned_executable(tmp_path))
    inventory = _RegistryInventoryFixture(_inventory(adapter))
    attestation = inventory.inspect(adapter.spec.adapter_id).attestation
    assert attestation is not None
    plan = create_action_plan(
        plan_id="plan:golden",
        actor_user_id="actor:one",
        actor_own_id="owner:one",
        conversation_id="conversation:one",
        source_message_id="message:one",
        host_agent_id="host-agent:one",
        idempotency_key="idempotency:one",
        adapter=adapter.spec,
        action=adapter.spec.action("render"),
        normalized_arguments={"text": "; echo not-a-shell"},
        executable_attestation=attestation,
        target_snapshot={"targets": ["127.0.0.1"], "scope": "exact"},
        now=1_000,
    )
    payload = plan.to_payload()
    assert HostActionPlan.from_payload(payload) == plan
    assert HostActionPlan.from_dict(payload).digest == plan.digest

    validated = AdapterRegistry((adapter,), inventory=inventory).validate_action(
        plan_payload=payload,
        approved_plan_digest=plan.digest,
        now=1_001,
    )
    assert validated.plan == plan
    assert validated.plan_digest == plan.digest
    assert validated.execution.argv[-1] == "; echo not-a-shell"


def test_registry_rejects_plan_or_adapter_drift(tmp_path) -> None:
    adapter = _EchoAdapter(_owned_executable(tmp_path))
    inventory = _RegistryInventoryFixture(_inventory(adapter))
    attestation = inventory.inspect(adapter.spec.adapter_id).attestation
    assert attestation is not None
    plan = create_action_plan(
        plan_id="plan:drift",
        actor_user_id="actor:one",
        actor_own_id="owner:one",
        conversation_id="conversation:one",
        source_message_id="message:one",
        host_agent_id="host-agent:one",
        idempotency_key="idempotency:two",
        adapter=adapter.spec,
        action=adapter.spec.action("render"),
        normalized_arguments={"text": "safe"},
        executable_attestation=attestation,
        now=1_000,
    )
    registry = AdapterRegistry((adapter,), inventory=inventory)
    changed = plan.to_payload()
    changed["normalized_arguments"] = {"text": "different"}
    with pytest.raises(AdapterValidationError):
        registry.validate_action(
            plan_payload=changed,
            approved_plan_digest=plan.digest,
            now=1_001,
        )

    drifted_adapter = _EchoAdapter(adapter.spec.executable.allowed_paths[0])
    drifted_adapter.spec = replace(drifted_adapter.spec, implementation_version=4)
    with pytest.raises(AdapterValidationError, match="version"):
        AdapterRegistry((drifted_adapter,), inventory=inventory).validate_action(
            plan_payload=plan.to_payload(), approved_plan_digest=plan.digest, now=1_001
        )
