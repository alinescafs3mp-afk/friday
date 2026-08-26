from __future__ import annotations

import os
from dataclasses import replace

from friday.config import validate_settings


def _host_errors(configured) -> list[str]:  # noqa: ANN001
    return [
        item
        for item in validate_settings(configured)
        if "FRIDAY_HOST_" in item or item.startswith("Host-control") or item.startswith("host ")
    ]


def _private_key(tmp_path):  # noqa: ANN001
    (tmp_path / "jericho-home" / "data" / "host-control" / "jobs").mkdir(
        parents=True,
        exist_ok=True,
    )
    (tmp_path / "jericho-home" / "data" / "host-control" / "jobs").chmod(0o700)
    key = tmp_path / "host-agent.key"
    key.write_bytes(b"K" * 32)
    key.chmod(0o600)
    return key


def test_host_control_defaults_are_inert(settings) -> None:
    assert settings.host_control_enabled is False
    assert settings.host_package_install_enabled is False
    assert settings.host_desktop_control_enabled is False
    assert settings.host_one_shot_exec_enabled is False
    assert settings.host_public_network_enabled is False
    assert _host_errors(settings) == []


def test_public_network_scope_cannot_enable_without_host_control(settings) -> None:
    errors = _host_errors(replace(settings, host_public_network_enabled=True))
    assert errors == ["Host-control subfeatures require FRIDAY_HOST_CONTROL_ENABLED=1"]


def test_unimplemented_host_surfaces_fail_configuration_closed(settings, tmp_path) -> None:
    base = replace(
        settings,
        host_control_enabled=True,
        host_agent_key_file=_private_key(tmp_path),
    )
    for field in ("host_desktop_control_enabled", "host_one_shot_exec_enabled"):
        errors = _host_errors(replace(base, **{field: True}))
        assert len(errors) == 1
        assert "reserved and unsupported" in errors[0]


def test_enabled_host_control_requires_a_private_non_symlink_key(settings, tmp_path) -> None:
    key = _private_key(tmp_path)
    configured = replace(settings, host_control_enabled=True, host_agent_key_file=key)
    assert _host_errors(configured) == []

    alias = tmp_path / "alias.key"
    alias.symlink_to(key)
    errors = _host_errors(replace(configured, host_agent_key_file=alias))
    assert any("non-symlink" in item for item in errors)

    key.chmod(0o644)
    errors = _host_errors(configured)
    assert any("private" in item for item in errors)


def test_package_install_requires_a_separate_backend_approval_seed(settings, tmp_path) -> None:
    configured = replace(
        settings,
        host_control_enabled=True,
        host_package_install_enabled=True,
        host_agent_key_file=_private_key(tmp_path),
        host_approval_signing_key_file=tmp_path / "missing-approval.key",
    )
    assert any("APPROVAL_SIGNING_KEY_FILE" in item for item in _host_errors(configured))

    approval_key = tmp_path / "backend-approval-signing.key"
    approval_key.write_bytes(b"A" * 32)
    approval_key.chmod(0o600)
    assert _host_errors(replace(configured, host_approval_signing_key_file=approval_key)) == []


def test_host_control_rejects_public_scope_until_separately_enabled(settings, tmp_path) -> None:
    configured = replace(
        settings,
        host_control_enabled=True,
        host_agent_key_file=_private_key(tmp_path),
        host_allowed_cidrs=("8.8.8.0/24",),
    )
    errors = _host_errors(configured)
    assert any("PUBLIC_NETWORK" in item for item in errors)

    configured = replace(
        configured,
        host_public_network_enabled=True,
    )
    assert any("APPROVAL_SIGNING_KEY_FILE" in item for item in _host_errors(configured))
    approval_key = tmp_path / "public-network-approval-signing.key"
    approval_key.write_bytes(b"A" * 32)
    approval_key.chmod(0o600)
    assert _host_errors(replace(configured, host_approval_signing_key_file=approval_key)) == []


def test_host_path_roots_reject_broad_sensitive_missing_and_overlapping_scope(
    settings,
    tmp_path,
) -> None:
    key = _private_key(tmp_path)
    first = tmp_path / "external-a"
    nested = first / "nested"
    first.mkdir()
    nested.mkdir()
    missing = tmp_path / "missing"
    base = replace(settings, host_control_enabled=True, host_agent_key_file=key)

    assert any(
        "too broad" in item
        for item in _host_errors(replace(base, host_allowed_path_roots=(settings.data_dir,)))
    )
    assert any(
        "does not exist" in item for item in _host_errors(replace(base, host_allowed_path_roots=(missing,)))
    )
    assert any(
        "overlap" in item for item in _host_errors(replace(base, host_allowed_path_roots=(first, nested)))
    )


def test_host_key_validation_reads_only_the_bounded_prefix(settings, tmp_path, monkeypatch) -> None:
    key = _private_key(tmp_path)
    observed: list[int] = []
    original_read = os.read

    def bounded_read(fd: int, size: int) -> bytes:
        observed.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", bounded_read)
    configured = replace(settings, host_control_enabled=True, host_agent_key_file=key)

    assert _host_errors(configured) == []
    assert observed == [65]


def test_host_job_root_must_be_private_and_backend_owned(settings, tmp_path) -> None:
    key = _private_key(tmp_path)
    root = settings.host_job_root
    root.chmod(0o755)

    errors = _host_errors(replace(settings, host_control_enabled=True, host_agent_key_file=key))

    assert any("must be private" in item for item in errors)
