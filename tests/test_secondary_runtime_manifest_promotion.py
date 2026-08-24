from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "secondary-brain" / "windows-sglang"
OPERATOR_PATH = BUNDLE / "scripts" / "accept_runtime_manifest.py"


@pytest.fixture(scope="module")
def operator() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "test_accept_runtime_manifest",
        OPERATOR_PATH,
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _write_json(path: Path, value: Any) -> Path:
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    )
    return path


def _valid_preflight(operator: ModuleType, observed_hardware_path: Path) -> dict[str, Any]:
    return {
        "schema": operator.PREFLIGHT_SCHEMA,
        "status": "automated_preflight_checks_passed",
        "observed_at": "2026-08-24T12:34:56.1234567Z",
        "computer": {
            "manufacturer": "Friday test manufacturer",
            "model": "Friday test model",
            "windows_caption": operator.EXPECTED_WINDOWS["caption"],
            "windows_version": operator.EXPECTED_WINDOWS["version"],
            "windows_build": operator.EXPECTED_WINDOWS["build"],
            "expected_address": "192.168.1.35",
            "expected_address_present": True,
        },
        "wsl": {
            "components": copy.deepcopy(operator.EXPECTED_WSL),
            "version_output_sha256": "1" * 64,
            "status_output_sha256": "2" * 64,
        },
        "docker": {
            **copy.deepcopy(operator.EXPECTED_DOCKER),
            "desktop_autostart_observed": True,
        },
        "host_gpu": copy.deepcopy(operator.EXPECTED_GPU),
        "runtime_gpu": copy.deepcopy(operator.EXPECTED_GPU),
        "gpu_container_canary": copy.deepcopy(operator.EXPECTED_GPU_CANARY),
        "sglang_help": copy.deepcopy(operator.EXPECTED_SGLANG_HELP),
        "gateway_image": copy.deepcopy(operator.EXPECTED_GATEWAY),
        "hardware_runtime_receipt": {
            "status": "observed_unaccepted",
            "sha256": operator.EXPECTED_OBSERVED_HARDWARE_SHA256,
            "output_path": str(observed_hardware_path.absolute()),
        },
        "operator_checks_required": [
            "wsl_update_state",
            "docker_desktop_wsl2_setting",
            "ac_sleep_disabled",
        ],
        "credentials_retained": False,
    }


def _valid_inputs(operator: ModuleType, tmp_path: Path) -> dict[str, Path]:
    template = tmp_path / "runtime-manifest.example.json"
    template.write_bytes((BUNDLE / "runtime-manifest.example.json").read_bytes())
    observed_hardware = _write_json(
        tmp_path / "hardware-runtime.observed.json",
        copy.deepcopy(operator.EXPECTED_OBSERVED_HARDWARE),
    )
    preflight = _write_json(
        tmp_path / "preflight.observed.json",
        _valid_preflight(operator, observed_hardware),
    )
    hardware = _write_json(
        tmp_path / "hardware-runtime.accepted.json",
        copy.deepcopy(operator.EXPECTED_HARDWARE),
    )
    return {
        "template": template,
        "preflight": preflight,
        "observed_hardware": observed_hardware,
        "hardware": hardware,
        "output": tmp_path / "runtime.accepted.json",
    }


def _promote(operator: ModuleType, paths: dict[str, Path]) -> dict[str, Any]:
    return operator.promote_runtime_manifest(
        paths["template"],
        paths["preflight"],
        paths["observed_hardware"],
        paths["hardware"],
        paths["output"],
    )


def _set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    parent: dict[str, Any] = value
    for key in path[:-1]:
        child = parent[key]
        assert isinstance(child, dict)
        parent = child
    parent[path[-1]] = replacement


def test_operator_forces_binary_descriptors_for_windows_exact_bytes() -> None:
    source = OPERATOR_PATH.read_text(encoding="utf-8")

    assert source.count('getattr(os, "O_BINARY", 0)') == 2


def test_operator_promotes_only_status_to_one_atomic_exclusive_manifest(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)

    result = _promote(operator, paths)
    template = json.loads(paths["template"].read_text(encoding="utf-8"))
    accepted_raw = paths["output"].read_bytes()
    accepted = json.loads(accepted_raw)

    assert result == {
        "schema": operator.PROMOTION_SCHEMA,
        "status": "accepted_runtime_manifest_created",
        "template_sha256": operator.EXPECTED_TEMPLATE_SHA256,
        "automated_preflight_sha256": hashlib.sha256(paths["preflight"].read_bytes()).hexdigest(),
        "hardware_runtime_receipt_sha256": operator.EXPECTED_ACCEPTED_HARDWARE_SHA256,
        "runtime_manifest_sha256": operator.EXPECTED_ACCEPTED_RUNTIME_SHA256,
        "overwritten": False,
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    assert set(accepted) == set(template)
    assert accepted["status"] == "accepted"
    assert all(accepted[key] == template[key] for key in template if key != "status")
    assert accepted_raw == operator.canonical_json(accepted)
    assert hashlib.sha256(accepted_raw).hexdigest() == operator.EXPECTED_ACCEPTED_RUNTIME_SHA256
    if os.name != "nt":
        assert stat.S_IMODE(paths["output"].stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".runtime.accepted.json.tmp-*"))


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("status",), "inventory_incomplete"),
        (("credentials_retained",), True),
        (("docker", "server_version"), "29.7.3"),
        (("runtime_gpu", "memory_total_mib"), 16_304),
        (("gpu_container_canary", "observation", "memory_total_bytes"), 17_094_475_775),
        (("sglang_help", "runtime_versions", "sglang_version"), "0.5.18"),
        (("sglang_help", "required_flags_sha256"), "0" * 64),
        (("gateway_image", "config_digest"), "sha256:" + "0" * 64),
        (("hardware_runtime_receipt", "sha256"), "0" * 64),
        (("hardware_runtime_receipt", "output_path"), "hardware-runtime.other.json"),
    ),
)
def test_operator_rejects_preflight_status_and_identity_drift_before_output(
    operator: ModuleType,
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: Any,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    preflight = _valid_preflight(operator, paths["observed_hardware"])
    _set_path(preflight, path, replacement)
    _write_json(paths["preflight"], preflight)

    with pytest.raises(operator.RuntimeManifestPromotionError):
        _promote(operator, paths)
    assert not paths["output"].exists()


def test_operator_rejects_unknown_preflight_key_and_wrong_scalar_type(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    preflight = _valid_preflight(operator, paths["observed_hardware"])
    preflight["unexpected"] = False
    _write_json(paths["preflight"], preflight)
    with pytest.raises(operator.RuntimeManifestPromotionError, match="shape"):
        _promote(operator, paths)

    del preflight["unexpected"]
    preflight["docker"]["desktop_autostart_observed"] = 1
    _write_json(paths["preflight"], preflight)
    with pytest.raises(operator.RuntimeManifestPromotionError, match="type"):
        _promote(operator, paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    "observed_at",
    (
        "0000-99-99TanythingZ",
        "2026-02-30T12:34:56.1234567Z",
        "2026-08-24T25:34:56.1234567Z",
        "2026-08-24T12:34:56Z",
        "2026-08-24T12:34:56.1234567+00:00",
    ),
)
def test_operator_rejects_noncanonical_or_impossible_preflight_timestamp(
    operator: ModuleType,
    tmp_path: Path,
    observed_at: str,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    preflight = _valid_preflight(operator, paths["observed_hardware"])
    preflight["observed_at"] = observed_at
    _write_json(paths["preflight"], preflight)

    with pytest.raises(operator.RuntimeManifestPromotionError, match="timestamp"):
        _promote(operator, paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    ("key", "replacement"),
    (
        ("status", "accepted"),
        ("gpu_vram_mib", 16_303.0),
        ("plain_sglang_lan_published", 0),
    ),
)
def test_operator_rejects_template_status_and_type_drift(
    operator: ModuleType,
    tmp_path: Path,
    key: str,
    replacement: Any,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    template = copy.deepcopy(operator.EXPECTED_RUNTIME_TEMPLATE)
    template[key] = replacement
    _write_json(paths["template"], template)

    with pytest.raises(operator.RuntimeManifestPromotionError, match="template identity"):
        _promote(operator, paths)
    assert not paths["output"].exists()


def test_operator_rejects_reformatted_or_duplicate_template(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    _write_json(paths["template"], copy.deepcopy(operator.EXPECTED_RUNTIME_TEMPLATE))
    with pytest.raises(operator.RuntimeManifestPromotionError, match="raw identity"):
        _promote(operator, paths)

    paths["template"].write_text('{"schema":"first","schema":"second"}\n', encoding="utf-8")
    with pytest.raises(operator.RuntimeManifestPromotionError, match="duplicate key"):
        _promote(operator, paths)
    assert not paths["output"].exists()


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("status",), "observed_unaccepted"),
        (("gpu", "memory_total_mib"), 16_304),
        (("docker", "server_version"), "29.7.3"),
    ),
)
def test_operator_rejects_nonexact_accepted_hardware_receipt(
    operator: ModuleType,
    tmp_path: Path,
    path: tuple[str, ...],
    replacement: Any,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    hardware = copy.deepcopy(operator.EXPECTED_HARDWARE)
    _set_path(hardware, path, replacement)
    _write_json(paths["hardware"], hardware)

    with pytest.raises(operator.RuntimeManifestPromotionError, match="hardware receipt"):
        _promote(operator, paths)
    assert not paths["output"].exists()


def test_operator_rejects_nonexact_observed_hardware_receipt(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    observed = copy.deepcopy(operator.EXPECTED_OBSERVED_HARDWARE)
    observed["gpu"]["memory_total_mib"] = 16_304
    _write_json(paths["observed_hardware"], observed)

    with pytest.raises(operator.RuntimeManifestPromotionError, match="observed hardware receipt"):
        _promote(operator, paths)
    assert not paths["output"].exists()


def test_operator_rejects_symlink_and_oversize_inputs(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    hardware_target = tmp_path / "hardware-target.json"
    paths["hardware"].replace(hardware_target)
    paths["hardware"].symlink_to(hardware_target)
    with pytest.raises(operator.RuntimeManifestPromotionError, match="bounded regular"):
        _promote(operator, paths)

    paths["hardware"].unlink()
    hardware_target.replace(paths["hardware"])
    paths["preflight"].write_bytes(b" " * (operator.MAX_PREFLIGHT_BYTES + 1))
    with pytest.raises(operator.RuntimeManifestPromotionError, match="bounded regular"):
        _promote(operator, paths)
    assert not paths["output"].exists()


def test_operator_never_overwrites_an_existing_output(
    operator: ModuleType,
    tmp_path: Path,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    sentinel = b"existing operator evidence\n"
    paths["output"].write_bytes(sentinel)

    with pytest.raises(operator.RuntimeManifestPromotionError, match="not new"):
        _promote(operator, paths)
    assert paths["output"].read_bytes() == sentinel


def test_output_race_never_deletes_a_peer_replacement(
    operator: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    peer_bytes = b"peer-created evidence\n"
    real_link = os.link

    def replace_after_link(source: Path, destination: Path) -> None:
        real_link(source, destination)
        Path(destination).unlink()
        Path(destination).write_bytes(peer_bytes)

    monkeypatch.setattr(operator.os, "link", replace_after_link)
    with pytest.raises(operator.RuntimeManifestPromotionError, match="could not be created"):
        _promote(operator, paths)

    assert paths["output"].read_bytes() == peer_bytes
    assert not list(tmp_path.glob(".runtime.accepted.json.tmp-*"))


def test_temporary_source_replacement_can_never_be_promoted(
    operator: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _valid_inputs(operator, tmp_path)
    accepted = copy.deepcopy(operator.EXPECTED_RUNTIME_TEMPLATE)
    accepted["status"] = "accepted"
    peer_bytes = b"x" * len(operator.canonical_json(accepted))
    real_link = os.link

    def replace_source_before_link(source: Path, destination: Path) -> None:
        Path(source).unlink()
        Path(source).write_bytes(peer_bytes)
        real_link(source, destination)

    monkeypatch.setattr(operator.os, "link", replace_source_before_link)
    with pytest.raises(operator.RuntimeManifestPromotionError, match="could not be created"):
        _promote(operator, paths)

    assert paths["output"].read_bytes() == peer_bytes
    assert not list(tmp_path.glob(".runtime.accepted.json.tmp-*"))


def test_cli_emits_only_content_free_json_on_success_and_rejection(
    operator: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    success_directory = tmp_path / "success"
    success_directory.mkdir()
    paths = _valid_inputs(operator, success_directory)
    arguments = [
        "--template",
        str(paths["template"]),
        "--preflight-evidence",
        str(paths["preflight"]),
        "--observed-hardware-receipt",
        str(paths["observed_hardware"]),
        "--hardware-receipt",
        str(paths["hardware"]),
        "--output",
        str(paths["output"]),
    ]
    assert operator.main(arguments) == 0
    success_text = capsys.readouterr().out
    success = json.loads(success_text)
    assert success["status"] == "accepted_runtime_manifest_created"
    assert "NVIDIA" not in success_text
    assert "lmsysorg" not in success_text

    rejected_directory = tmp_path / "rejected"
    rejected_directory.mkdir()
    rejected_paths = _valid_inputs(operator, rejected_directory)
    preflight = _valid_preflight(operator, rejected_paths["observed_hardware"])
    preflight["status"] = "inventory_incomplete"
    _write_json(rejected_paths["preflight"], preflight)
    rejected_arguments = [
        "--template",
        str(rejected_paths["template"]),
        "--preflight-evidence",
        str(rejected_paths["preflight"]),
        "--observed-hardware-receipt",
        str(rejected_paths["observed_hardware"]),
        "--hardware-receipt",
        str(rejected_paths["hardware"]),
        "--output",
        str(rejected_paths["output"]),
    ]
    assert operator.main(rejected_arguments) == 2
    rejection_text = capsys.readouterr().out
    rejection = json.loads(rejection_text)
    assert rejection == {
        "schema": operator.PROMOTION_SCHEMA,
        "status": "rejected",
        "reason": "automated preflight status is invalid",
        "raw_content_retained": False,
        "credentials_retained": False,
    }
    assert not rejected_paths["output"].exists()
    assert "NVIDIA" not in rejection_text
    assert "lmsysorg" not in rejection_text
