from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.host_control.client import HostControlUnavailable
from friday.host_control.jobs import HostJobStore
from friday.host_control.network_approval import NetworkApprovalSigner
from friday.host_control.policy import NetworkPolicy


def _enabled_settings(settings, tmp_path: Path, **changes: Any):  # noqa: ANN001, ANN202
    key = tmp_path / "host-agent.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    job_root = tmp_path / "host-control-jobs"
    job_root.mkdir()
    return replace(
        settings,
        host_control_enabled=True,
        host_agent_socket=tmp_path / "host-agent.sock",
        host_agent_key_file=key,
        host_agent_id="host-agent-test",
        host_job_root=job_root,
        **changes,
    )


def _seed_host_job_counts(storage) -> None:  # noqa: ANN001
    jobs = HostJobStore(storage)

    def create(
        suffix: str,
        *,
        risk_class: str,
        awaiting_approval: bool = False,
    ) -> dict[str, Any]:
        job, created = jobs.create_or_get(
            user_id="alice",
            actor_own_id="alice",
            conversation_id=None,
            source_message_id=None,
            host_agent_id="host-agent-test",
            capability_id="network.nmap.scan",
            adapter_id="network.nmap",
            adapter_version=1,
            action_id="discover",
            normalized_arguments={"target": f"private-target-{suffix}"},
            plan={"private_plan_body": f"private-plan-{suffix}"},
            plan_digest=suffix * 64,
            risk_class=risk_class,
            authorization_basis="host.actions.execute",
            idempotency_key=f"diagnostic:{suffix}",
            awaiting_approval=awaiting_approval,
            job_id="hjob_" + suffix * 32,
        )
        assert created is True
        return job

    running = create("a", risk_class="network_observe")
    running = jobs.transition(
        running["id"],
        user_id="alice",
        actor_own_id="alice",
        expected_status="planned",
        status="admitted",
        stage="agent_admission",
        outcome_code="admitted",
    )
    jobs.transition(
        running["id"],
        user_id="alice",
        actor_own_id="alice",
        expected_status="admitted",
        status="running",
        stage="host_process",
        outcome_code="started",
    )

    unknown = create("b", risk_class="network_observe")
    for expected, status in (
        ("planned", "admitted"),
        ("admitted", "running"),
        ("running", "unknown"),
    ):
        unknown = jobs.transition(
            unknown["id"],
            user_id="alice",
            actor_own_id="alice",
            expected_status=expected,
            status=status,
            stage="diagnostic_test",
            outcome_code=status,
        )

    create("c", risk_class="package_mutation", awaiting_approval=True)


def test_disabled_host_control_never_constructs_an_agent_client(
    settings,
    storage,
    monkeypatch,
) -> None:
    import friday.diagnostics as diagnostics

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("disabled diagnostics connected to host-agent")

    monkeypatch.setattr(diagnostics, "HostControlClient", ForbiddenClient)

    report = diagnostics.collect_diagnostics(settings, storage=storage)

    section = report["host_control"]
    assert section["enabled"] is False
    assert section["state"] == "disabled"
    assert section["healthy"] is True
    assert section["agent"] == {"reachable": False, "authenticated": False}
    assert not any(
        str(action.get("code", "")).startswith("host_control_agent") for action in report["actions"]
    )


def test_host_control_sqlite_diagnostics_are_counts_only(settings, storage) -> None:
    from friday.diagnostics import collect_diagnostics

    storage.ensure_user("alice", preset_key="owner")
    _seed_host_job_counts(storage)

    report = collect_diagnostics(settings, storage=storage)

    section = report["host_control"]
    assert section["sqlite"] == {
        "available": True,
        "running_jobs": 1,
        "unknown_jobs": 1,
        "pending_package_jobs": 1,
        "events": 8,
    }
    rendered = repr(section)
    assert "private-target" not in rendered
    assert "private-plan" not in rendered
    assert any(action["code"] == "host_control_unknown_jobs" for action in report["actions"])


def test_enabled_host_control_reports_only_bounded_authenticated_health(
    settings,
    storage,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import friday.diagnostics as diagnostics

    private_path = "/private/customer/nmap"
    private_reason = "private target and stdout"

    class ConnectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def handshake_sync(self, *, timeout_sec: float) -> dict[str, Any]:
            assert timeout_sec == 1.0
            return {
                "accepted": True,
                "agent_id": "host-agent-test",
                "build_id": "friday-0.208.0-a1b2c3",
                "protocol_versions": ["1.0"],
                "inventory": [
                    {
                        "adapter_id": "network.nmap",
                        "state": "available",
                        "configured_paths": [private_path],
                        "reason": private_reason,
                        "attestation": {"canonical_path": private_path},
                    },
                    {
                        "adapter_id": "data.jq",
                        "state": "missing_package",
                        "configured_paths": ["/another/private/path"],
                        "attestation": None,
                    },
                ],
                "desktop_capability": "launch_only",
                "package_broker": "configured",
                "network_policy_digest": NetworkPolicy(connected_cidrs=()).digest,
                "running_job_count": 2,
                "os_release": "private-kernel-build",
                "user_uid": 4815,
            }

    monkeypatch.setattr(diagnostics, "HostControlClient", ConnectedClient)
    configured = _enabled_settings(
        settings,
        tmp_path,
        host_package_install_enabled=True,
        host_desktop_control_enabled=True,
        host_one_shot_exec_enabled=True,
    )

    report = diagnostics.collect_diagnostics(configured, storage=storage)

    section = report["host_control"]
    assert section["state"] == "ready"
    assert section["healthy"] is True
    assert section["agent"] == {
        "reachable": True,
        "authenticated": True,
        "identity": "host-agent-test",
        "build_id": "friday-0.208.0-a1b2c3",
        "protocol_versions": ["1.0"],
    }
    assert section["adapter_states"] == [
        {"adapter_id": "data.jq", "state": "missing_package"},
        {"adapter_id": "network.nmap", "state": "available"},
    ]
    assert section["desktop"] == "launch_only"
    assert section["package_broker"] == "configured"
    assert section["network_policy_match"] is True
    assert section["running_job_count"] == 2
    rendered = repr(section)
    assert private_path not in rendered
    assert private_reason not in rendered
    assert "private-kernel-build" not in rendered
    assert "4815" not in rendered


def test_public_network_diagnostics_require_the_backend_signer_identity(
    settings,
    storage,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import friday.diagnostics as diagnostics

    seed = b"A" * 32
    approval_key = tmp_path / "network-approval.key"
    expected_digest = NetworkApprovalSigner(seed).public_key_digest

    def load_signing_key(path: Path) -> bytes:
        assert path == approval_key
        return seed

    class ConnectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def handshake_sync(self, *, timeout_sec: float) -> dict[str, Any]:
            assert timeout_sec == 1.0
            return {
                "agent_id": "host-agent-test",
                "build_id": "friday-0.208.0-key-match",
                "protocol_versions": ["1.0"],
                "inventory": [{"adapter_id": "network.nmap", "state": "available"}],
                "desktop_capability": "launch_only",
                "package_broker": "unavailable",
                "network_policy_digest": NetworkPolicy(
                    connected_cidrs=(),
                    allow_public=True,
                ).digest,
                "network_approval_public_key_digest": expected_digest,
                "running_job_count": 0,
            }

    monkeypatch.setattr(diagnostics, "HostControlClient", ConnectedClient)
    monkeypatch.setattr(diagnostics, "load_backend_approval_signing_key", load_signing_key)
    configured = _enabled_settings(
        settings,
        tmp_path,
        host_public_network_enabled=True,
        host_approval_signing_key_file=approval_key,
    )

    section = diagnostics.collect_diagnostics(configured, storage=storage)["host_control"]

    assert section["state"] == "ready"
    assert section["healthy"] is True
    assert section["network_approval_public_key_match"] is True
    assert section["adapter_states"] == [{"adapter_id": "network.nmap", "state": "available"}]


@pytest.mark.parametrize(
    "reported_digest",
    [pytest.param(None, id="missing"), pytest.param("0" * 64, id="mismatch")],
)
def test_public_network_diagnostics_reject_missing_or_mismatched_agent_key(
    settings,
    storage,
    tmp_path: Path,
    monkeypatch,
    reported_digest: str | None,
) -> None:
    import friday.diagnostics as diagnostics

    seed = b"A" * 32
    approval_key = tmp_path / "network-approval.key"

    class ConnectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def handshake_sync(self, *, timeout_sec: float) -> dict[str, Any]:
            assert timeout_sec == 1.0
            health: dict[str, Any] = {
                "agent_id": "host-agent-test",
                "build_id": "friday-0.208.0-untrusted-key",
                "protocol_versions": ["1.0"],
                "inventory": [{"adapter_id": "network.nmap", "state": "available"}],
                "desktop_capability": "launch_only",
                "package_broker": "configured",
                "network_policy_digest": NetworkPolicy(
                    connected_cidrs=(),
                    allow_public=True,
                ).digest,
                "running_job_count": 0,
            }
            if reported_digest is not None:
                health["network_approval_public_key_digest"] = reported_digest
            return health

    monkeypatch.setattr(diagnostics, "HostControlClient", ConnectedClient)
    monkeypatch.setattr(
        diagnostics,
        "load_backend_approval_signing_key",
        lambda _path: seed,
    )
    configured = _enabled_settings(
        settings,
        tmp_path,
        host_public_network_enabled=True,
        host_approval_signing_key_file=approval_key,
    )

    report = diagnostics.collect_diagnostics(configured, storage=storage)
    section = report["host_control"]

    assert section["state"] == "invalid_report"
    assert section["healthy"] is False
    assert section["network_approval_public_key_match"] is None
    assert section["adapter_states"] == []
    assert section["desktop"] == "not_reported"
    assert section["package_broker"] == "not_reported"
    assert report["ok"] is False
    assert any(item["code"] == "host_control_agent_report_invalid" for item in report["actions"])


def test_public_network_diagnostics_fail_closed_when_backend_signer_is_unavailable(
    settings,
    storage,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import friday.diagnostics as diagnostics

    approval_key = tmp_path / "missing-network-approval.key"

    class ConnectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def handshake_sync(self, *, timeout_sec: float) -> dict[str, Any]:
            assert timeout_sec == 1.0
            return {
                "agent_id": "host-agent-test",
                "build_id": "friday-0.208.0-signer-unavailable",
                "protocol_versions": ["1.0"],
                "inventory": [{"adapter_id": "network.nmap", "state": "available"}],
                "desktop_capability": "launch_only",
                "package_broker": "configured",
                "network_policy_digest": NetworkPolicy(
                    connected_cidrs=(),
                    allow_public=True,
                ).digest,
                "network_approval_public_key_digest": "0" * 64,
                "running_job_count": 0,
            }

    def unavailable_signer(_path: Path) -> bytes:
        raise OSError("private path must not escape diagnostics")

    monkeypatch.setattr(diagnostics, "HostControlClient", ConnectedClient)
    monkeypatch.setattr(diagnostics, "load_backend_approval_signing_key", unavailable_signer)
    configured = _enabled_settings(
        settings,
        tmp_path,
        host_public_network_enabled=True,
        host_approval_signing_key_file=approval_key,
    )

    report = diagnostics.collect_diagnostics(configured, storage=storage)
    section = report["host_control"]

    assert section["state"] == "approval_key_unavailable"
    assert section["healthy"] is False
    assert section["network_approval_public_key_match"] is None
    assert section["adapter_states"] == []
    assert section["desktop"] == "not_reported"
    assert section["package_broker"] == "not_reported"
    assert "private path" not in repr(section)
    assert report["ok"] is False
    assert any(item["code"] == "host_network_approval_signer_unavailable" for item in report["actions"])


def test_enabled_disconnected_host_control_is_actionable_and_degraded(
    settings,
    storage,
    tmp_path: Path,
    monkeypatch,
) -> None:
    import friday.diagnostics as diagnostics

    private_error = "/private/socket/customer-name"

    class DisconnectedClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def handshake_sync(self, *, timeout_sec: float) -> dict[str, Any]:
            assert timeout_sec == 1.0
            raise HostControlUnavailable("agent_unavailable", private_error)

    monkeypatch.setattr(diagnostics, "HostControlClient", DisconnectedClient)
    configured = _enabled_settings(settings, tmp_path)

    report = diagnostics.collect_diagnostics(configured, storage=storage)

    section = report["host_control"]
    assert section["state"] == "unavailable"
    assert section["healthy"] is False
    assert section["agent"] == {"reachable": False, "authenticated": False}
    assert section["error_code"] == "agent_unavailable"
    action = next(item for item in report["actions"] if item["code"] == "host_control_agent_unavailable")
    assert action["severity"] == "error"
    assert report["ok"] is False
    assert report["state"] == "degraded"
    assert private_error not in repr(section)
    assert private_error not in repr(action)
