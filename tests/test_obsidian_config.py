from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from friday.config import ensure_runtime_dirs, validate_settings


def _obsidian_errors(configured) -> list[str]:
    markers = ("OBSIDIAN", "SYNCTHING", "PUBLIC_BASE_URL", "Obsidian", "Syncthing")
    return [item for item in validate_settings(configured) if any(mark in item for mark in markers)]


def test_obsidian_is_disabled_by_default_and_public_config_contains_no_secret(settings) -> None:
    assert settings.obsidian_enabled is False
    published = settings.public_dict()["obsidian"]
    assert published["enabled"] is False
    assert published["vault_name"] == "Friday"
    assert "binary" not in published
    assert "api_key" not in published


def test_enabled_obsidian_configuration_is_fail_closed_and_creates_private_roots(settings, tmp_path) -> None:
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=tmp_path / "obsidian",
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    assert _obsidian_errors(configured) == []

    ensure_runtime_dirs(configured)
    assert configured.obsidian_effective_root.is_dir()
    assert configured.obsidian_effective_root.stat().st_mode & 0o077 == 0

    insecure_url = replace(configured, obsidian_public_base_url="http://friday.example")
    assert any("HTTPS" in item for item in _obsidian_errors(insecure_url))
    missing_binary = replace(configured, obsidian_syncthing_binary=str(tmp_path / "missing"))
    assert any("existing absolute file" in item for item in _obsidian_errors(missing_binary))
    wrong_transport = replace(configured, obsidian_transport_mode="direct")
    assert any("discovery_relay" in item for item in _obsidian_errors(wrong_transport))
    assert any(
        "VAULT_NAME" in item
        for item in _obsidian_errors(replace(configured, obsidian_vault_name="../Friday"))
    )
    assert _obsidian_errors(replace(configured, obsidian_vault_name="Friday-Test")) == []


def test_documented_obsidian_root_under_data_dir_is_supported(settings) -> None:
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=settings.data_dir / "obsidian",
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )

    assert _obsidian_errors(configured) == []


def test_obsidian_root_must_not_overlap_durable_core_state(settings) -> None:
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=settings.state_dir,
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    assert any("must not overlap" in item for item in _obsidian_errors(configured))


@pytest.mark.parametrize("path_name", ["home", "data_dir", "cache_dir", "log_dir", "model_root"])
def test_obsidian_root_rejects_broad_friday_directories(settings, path_name: str) -> None:
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=getattr(settings, path_name),
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    assert any(
        "dedicated, non-broad" in item or "must not overlap" in item for item in _obsidian_errors(configured)
    )


@pytest.mark.parametrize("root", [Path("/"), Path("/tmp"), Path("/var/log")])
def test_obsidian_root_rejects_system_directories(settings, root: Path) -> None:
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=root,
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    assert _obsidian_errors(configured)


def test_existing_non_private_obsidian_root_is_not_silently_chmodded(settings, tmp_path) -> None:
    root = tmp_path / "shared-looking-root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=root,
        obsidian_syncthing_binary="/bin/dash",
        obsidian_public_base_url="https://friday.example",
    )
    assert any("non-private permissions" in item for item in _obsidian_errors(configured))
    with pytest.raises(ValueError, match="non-private permissions"):
        ensure_runtime_dirs(configured)
    assert root.stat().st_mode & 0o077 == 0o055
