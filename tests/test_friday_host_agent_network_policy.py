from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday.host_control.policy import NetworkPolicy
from friday_host_agent import network_policy as policy_module


def _write_policy(path: Path, *, cidrs: str = '"192.168.1.0/24"', public: str = "false") -> None:
    path.write_text(
        f"[network]\nschema_version = 1\nallowed_cidrs = [{cidrs}]\nallow_public = {public}\n",
        encoding="utf-8",
    )
    path.chmod(0o644)


def test_root_owned_policy_loader_returns_exact_canonical_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_ROOT_UID", os.geteuid())
    tmp_path.chmod(0o700)
    source = tmp_path / "host-agent-policy.toml"
    _write_policy(source)

    observed = policy_module.load_agent_network_policy(source)

    expected = NetworkPolicy(
        connected_cidrs=(),
        allowed_cidrs=("192.168.1.0/24",),
        allow_public=False,
    )
    assert observed == expected
    assert observed.digest == expected.digest


def test_policy_loader_rejects_writable_or_linked_operator_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(policy_module, "_ROOT_UID", os.geteuid())
    tmp_path.chmod(0o700)
    source = tmp_path / "source.toml"
    _write_policy(source)
    source.chmod(0o664)
    with pytest.raises(ValueError, match="metadata is unsafe"):
        policy_module.load_agent_network_policy(source)

    source.chmod(0o644)
    linked = tmp_path / "linked.toml"
    linked.symlink_to(source)
    with pytest.raises(ValueError, match="canonical"):
        policy_module.load_agent_network_policy(linked)


@pytest.mark.parametrize(
    ("cidrs", "public", "error"),
    [
        ('"8.8.8.0/24"', "false", "requires allow_public"),
        ('"192.168.1.0/24", "192.168.1.7/32"', "false", "overlap"),
        ('"192.168.1.1/24"', "false", "invalid"),
        ('"0.0.0.0/0"', "true", "exact and canonical"),
    ],
)
def test_policy_loader_rejects_unsafe_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cidrs: str,
    public: str,
    error: str,
) -> None:
    monkeypatch.setattr(policy_module, "_ROOT_UID", os.geteuid())
    tmp_path.chmod(0o700)
    source = tmp_path / "host-agent-policy.toml"
    _write_policy(source, cidrs=cidrs, public=public)

    with pytest.raises(ValueError, match=error):
        policy_module.load_agent_network_policy(source)
