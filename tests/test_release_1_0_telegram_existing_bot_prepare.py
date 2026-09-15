"""Offline producer controls; never invoke the live execute subcommand."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import release_1_0_telegram_existing_bot_prepare as prepare

ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 810_000_001
BOT_ID = 820_000_001
TOKEN = "820000001:synthetic-preparer-existing-bot-token"
CANDIDATE_SHA = "46747aa06f91cbbf19aa035d19015e32aa041d95"
CANDIDATE_TREE = "78d80c87476f8b21ced49975b4f59f387ba76d3c"
_RUNTIME: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _installed_runtime(telegram_offline_runtime):
    with pytest.MonkeyPatch.context() as patch:
        for name, value in telegram_offline_runtime.items():
            patch.setitem(_RUNTIME, name, value)
        yield


def _write(path: Path, value: object | bytes) -> dict[str, str]:
    raw = value if isinstance(value, bytes) else prepare._json_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _fixture(tmp_path: Path):
    tmp_path.chmod(0o700)
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(mode=0o700)
    candidate_manifest = _write(tmp_path / "candidate-manifest.json", {})
    harness_manifest = _write(tmp_path / "harness-manifest.json", {})
    base_scope = _write(
        tmp_path / "base-effect-scope.json",
        {"schema": "friday.astra-telegram-effect-scope.v1", "GO": False},
    )
    owner_directive = _write(
        tmp_path / "owner-directive.json",
        {
            "schema": "friday.owner-telegram-recipient-restriction.v1",
            "recipient_scope": {"only_owner_private_chat": True},
            "GO": False,
        },
    )
    effect_admission = _write(
        tmp_path / "effect-admission.json",
        {
            "schema": prepare.telegram.EFFECT_ADMISSION_SCHEMA,
            "route_id": "existing-friday-bot-sole-consumer-handoff-installed-final",
            "candidate_sha": CANDIDATE_SHA,
            "candidate_tree": CANDIDATE_TREE,
            "wheel_sha256": _RUNTIME["WHEEL_SHA"],
            "harness_manifest_sha256": harness_manifest["sha256"],
            "base_effect_scope": base_scope,
            "owner_directive": owner_directive,
            "existing_friday_bot_one_canary": True,
            "same_bot_production_consumer_stopped": True,
            "isolated_non_production_home": True,
            "installed_wheel_origin": True,
            "owner_private_recipient_only": True,
            "startup_effect": {
                "method": "setMyCommands",
                "max_attempts": 1,
                "target": "existing_friday_bot",
                "canonical_payload_digest_required": True,
                "restoration_claimed": False,
            },
            "frozen_limits": {
                "duration_s": 600,
                "genuine_inbound": 1,
                "bot_posts": 2,
                "total_attempts": 433,
                "method_caps": prepare.telegram.EFFECT_METHOD_CAPS,
            },
            "unknown_effect_policy": "latch_and_never_retry",
            "nonowner_ingress_policy": "fail_before_bridge_observation_no_reply_no_drain",
            "GO": False,
        },
    )
    authority = _write(
        tmp_path / "established.env",
        (
            f"FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS={OWNER_ID}\nFRIDAY_TELEGRAM_OWNER_CHAT_IDS={OWNER_ID}\n"
            f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n"
        ).encode(),
    )
    output_root = tmp_path / "attempt-output"
    home = output_root / "contour/home"
    env = _write(
        tmp_path / "isolated.env",
        (
            f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n"
            f"FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS={OWNER_ID}\n"
            f"FRIDAY_TELEGRAM_OWNER_CHAT_IDS={OWNER_ID}\n"
            "FRIDAY_OBSIDIAN_ENABLED=0\n"
            "FRIDAY_ENGINEER_MODE_ENABLED=0\n"
        ).encode(),
    )
    identity = _write(
        tmp_path / "identity.json",
        {
            "schema": prepare.IDENTITY_SCHEMA,
            "candidate_sha": CANDIDATE_SHA,
            "candidate_tree": CANDIDATE_TREE,
            "base_sha": "1bfd839c7906d2ff055437f64755b4601baf101d",
            "wheel_sha256": _RUNTIME["WHEEL_SHA"],
            "inventory_sha256": "717ff63688f989f8c9702ed87b256448d29e8dfebe4552f9a0faa8bb4ccb7065",
            "suite_revision": "r10-astra-harness-084",
            "canonical_status": "PRIVATE_DUAL_ROOT_HARNESS_REQUIRES_INDEPENDENT_ADOPTION",
            "GO": False,
        },
    )
    systemctl = Path("/usr/bin/systemctl")
    handoff = _write(
        tmp_path / "handoff-plan.json",
        {
            "schema": prepare.receipts.HANDOFF_PLAN_SCHEMA,
            "candidate_sha": CANDIDATE_SHA,
            "candidate_tree": CANDIDATE_TREE,
            "wheel_sha256": _RUNTIME["WHEEL_SHA"],
            "systemctl_path": str(systemctl),
            "systemctl_sha256": prepare.telegram.sha256_file(systemctl),
            "unit_name": "friday-bridge.service",
            "production_lease_path": str(tmp_path / "production-inbox.lock"),
            "exclusive_slot_lock": str(tmp_path / "live-slot.lock"),
            "max_stop_calls": 1,
            "max_restore_calls": 1,
            "required_initial_state": "active",
            "required_final_state": "active",
            "no_second_consumer": True,
            "production_home_as_scratch": False,
            "history_policy": "do_not_read_replay_delete_or_drain",
            "GO": False,
        },
    )
    plan = {
        "schema": prepare.PLAN_SCHEMA,
        "identity": identity,
        "candidate": {
            "attempt_id": "existing_prepare_attempt_0001",
            "canary": "canary:existing_prepare_attempt_0001",
            "policy": {
                "manifest_path": candidate_manifest["path"],
                "manifest_sha256": candidate_manifest["sha256"],
                "source_root": str(candidate_root),
                "candidate_sha": CANDIDATE_SHA,
                "candidate_tree": CANDIDATE_TREE,
                "suite_revision": "r10-astra-harness-084",
                "python_executable": str(_RUNTIME["INSTALLED"] / "bin/python"),
                "python_sha256": prepare.telegram.sha256_file(_RUNTIME["INSTALLED"] / "bin/python"),
                "installed_wheel": {
                    "wheel_path": str(_RUNTIME["WHEEL"]),
                    "wheel_sha256": _RUNTIME["WHEEL_SHA"],
                    "site_root": str(_RUNTIME["SITE"]),
                    "distribution_version": _RUNTIME["VERSION"],
                },
            },
        },
        "harness": {
            "manifest_path": harness_manifest["path"],
            "manifest_sha256": harness_manifest["sha256"],
            "source_root": str(ROOT),
        },
        "effect_admission": {
            "manifest_path": effect_admission["path"],
            "manifest_sha256": effect_admission["sha256"],
        },
        "contour": {
            "contour_id": "existing-prepare-contour",
            "isolation_root": str(output_root / "contour"),
            "friday_home": str(home),
            "data_dir": str(home / "data"),
            "state_dir": str(home / "state"),
            "cache_dir": str(home / "cache"),
            "log_dir": str(home / "log"),
            "env_file": env["path"],
            "env_file_sha256": env["sha256"],
            "database_path": str(home / "data/friday.sqlite3"),
            "inbox_db_path": str(home / "state/telegram-inbox.sqlite3"),
            "files_dir": str(home / "data/files"),
            "backend_origin": "http://127.0.0.1:18992",
            "known_production_homes": [str(tmp_path / "production-home")],
            "foreign_lease_paths": [],
        },
        "owner_authority": authority,
        "bot_user_id": BOT_ID,
        "observer": {"mode": "owner_manual_readback", "result_path": str(output_root / "observer.json")},
        "budgets": {
            "timeout_s": 600,
            "backend_ready_timeout_s": 30,
            "poll_interval_s": 0.1,
            "max_getupdates_rounds": 20,
            "max_inbound_user_messages": 1,
            "max_outbound_bot_posts": 2,
            "max_evidence_bytes": 1048576,
            "process_log_bytes": 65536,
            "cleanup_grace_s": 5,
        },
        "handoff_plan": handoff,
        "outputs": {
            "access_path": str(tmp_path / "access.json"),
            "policy_path": str(tmp_path / "policy.json"),
            "startup_commands_path": str(tmp_path / "startup-commands.json"),
            "context_path": str(tmp_path / "context.json"),
            "preparation_path": str(tmp_path / "prepared.json"),
            "output_root": str(output_root),
        },
        "GO": False,
    }
    return _write(tmp_path / "plan.json", plan), plan


def _source_summary(plan):
    return {
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "suite_revision": "r10-astra-harness-084",
        "manifest_sha256": "1" * 64,
        "source_map_sha256": "2" * 64,
        "source_file_count": 2076,
        "module_sha256": "3" * 64,
        "cli_sha256": "4" * 64,
        "bridge_base_sha256": "5" * 64,
        "harness": {
            "manifest_sha256": plan["harness"]["manifest_sha256"],
            "source_map_sha256": "7" * 64,
            "source_file_count": 5,
            "driver_sha256": "8" * 64,
        },
        "installed_wheel": {
            "wheel_sha256": _RUNTIME["WHEEL_SHA"],
            "source_checkout_imported": False,
        },
    }


def _synthetic_bus_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Canonical gate fixture roots can exceed sockaddr_un's pathname limit.
    # Bind through the directory descriptor without changing process cwd;
    # the actual socket still lives at the exact runtime / "bus" path.
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        listener.bind(f"/proc/self/fd/{descriptor}/{path.name}")
    except BaseException:
        listener.close()
        raise
    finally:
        os.close(descriptor)
    return listener


def _synthetic_user_bus(tmp_path: Path) -> tuple[Path, Path, socket.socket]:
    runtime_root = tmp_path / "run-user"
    runtime_root.mkdir(mode=0o700)
    runtime = runtime_root / str(os.getuid())
    runtime.mkdir(mode=0o700)
    listener = _synthetic_bus_listener(runtime / "bus")
    return runtime_root, runtime, listener


def test_service_environment_is_owner_derived_exact_and_bounded(tmp_path, monkeypatch):
    runtime_root, runtime, listener = _synthetic_user_bus(tmp_path)
    try:
        monkeypatch.setenv("HOME", "/ambient/home/must-not-pass")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/ambient/runtime/must-not-pass")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/ambient/bus")
        monkeypatch.setenv("PYTHONPATH", "/ambient/python/must-not-pass")
        environment = prepare._service_environment(runtime_root=runtime_root)
        assert environment == {
            "HOME": prepare.pwd.getpwuid(os.getuid()).pw_dir,
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        }

        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"active\n", b"")

        monkeypatch.setattr(prepare, "_service_environment", lambda: environment)
        monkeypatch.setattr(prepare.subprocess, "run", run)
        completed = prepare._service(
            Path("/usr/bin/systemctl"),
            prepare.SERVICE_UNIT,
            "is-active",
        )
        assert completed.returncode == 0
        assert calls == [
            (
                ["/usr/bin/systemctl", "--user", "is-active", "friday-bridge.service"],
                {
                    "check": False,
                    "stdin": subprocess.DEVNULL,
                    "capture_output": True,
                    "timeout": 60,
                    "env": environment,
                },
            )
        ]
        with pytest.raises(ValueError, match="fixed handoff"):
            prepare._service(Path("/usr/bin/systemctl"), "foreign.service", "stop")
        with pytest.raises(ValueError, match="fixed handoff"):
            prepare._service(Path("/usr/bin/systemctl"), prepare.SERVICE_UNIT, "restart")
        assert len(calls) == 1
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("uid-mismatch", "uid mismatch"),
        ("relative-runtime", "runtime directory"),
        ("open-runtime", "runtime directory"),
        ("foreign-runtime", "runtime directory"),
        ("runtime-symlink", "runtime directory"),
        ("missing-bus", "owner bus"),
        ("regular-bus", "owner bus"),
        ("bus-symlink", "owner bus"),
        ("foreign-bus", "owner bus"),
        ("account-uid-mismatch", "owner home"),
        ("relative-home", "owner home"),
        ("open-home", "owner home"),
        ("foreign-home", "owner home"),
        ("home-symlink", "owner home"),
    ),
)
def test_service_environment_rejects_unsafe_identity_paths_bus_or_home(
    tmp_path,
    monkeypatch,
    case,
    reason,
):
    runtime_root, runtime, listener = _synthetic_user_bus(tmp_path)
    call_root = runtime_root
    original_lstat = Path.lstat
    try:
        if case == "uid-mismatch":
            monkeypatch.setattr(prepare.os, "geteuid", lambda: os.getuid() + 1)
        elif case == "relative-runtime":
            call_root = Path("relative-run-user")
        elif case == "open-runtime":
            runtime.chmod(0o755)
        elif case == "foreign-runtime":

            def foreign_runtime_lstat(path):
                info = original_lstat(path)
                if path == runtime:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                return info

            monkeypatch.setattr(Path, "lstat", foreign_runtime_lstat)
        elif case == "runtime-symlink":
            listener.close()
            real_runtime = runtime_root / "actual-runtime"
            runtime.rename(real_runtime)
            runtime.symlink_to(real_runtime.name, target_is_directory=True)
        elif case == "missing-bus":
            listener.close()
            (runtime / "bus").unlink()
        elif case == "regular-bus":
            listener.close()
            (runtime / "bus").unlink()
            (runtime / "bus").write_text("not a socket")
        elif case == "bus-symlink":
            listener.close()
            (runtime / "bus").unlink()
            listener = _synthetic_bus_listener(runtime / "real-bus")
            (runtime / "bus").symlink_to("real-bus")
        elif case == "foreign-bus":
            bus = runtime / "bus"

            def foreign_bus_lstat(path):
                info = original_lstat(path)
                if path == bus:
                    return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                return info

            monkeypatch.setattr(Path, "lstat", foreign_bus_lstat)
        elif case == "account-uid-mismatch":
            monkeypatch.setattr(
                prepare.pwd,
                "getpwuid",
                lambda uid: SimpleNamespace(pw_uid=uid + 1, pw_dir=str(tmp_path)),
            )
        elif case == "relative-home":
            monkeypatch.setattr(
                prepare.pwd,
                "getpwuid",
                lambda uid: SimpleNamespace(pw_uid=uid, pw_dir="relative-home"),
            )
        elif case in {"open-home", "foreign-home"}:
            unsafe_home = tmp_path / "unsafe-home"
            unsafe_home.mkdir(mode=0o700)
            monkeypatch.setattr(
                prepare.pwd,
                "getpwuid",
                lambda uid: SimpleNamespace(pw_uid=uid, pw_dir=str(unsafe_home)),
            )
            if case == "open-home":
                unsafe_home.chmod(0o770)
            else:

                def foreign_home_lstat(path):
                    info = original_lstat(path)
                    if path == unsafe_home:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                monkeypatch.setattr(Path, "lstat", foreign_home_lstat)
        elif case == "home-symlink":
            real_home = tmp_path / "real-home"
            real_home.mkdir(mode=0o700)
            linked_home = tmp_path / "linked-home"
            linked_home.symlink_to(real_home.name, target_is_directory=True)
            monkeypatch.setattr(
                prepare.pwd,
                "getpwuid",
                lambda uid: SimpleNamespace(pw_uid=uid, pw_dir=str(linked_home)),
            )
        with pytest.raises(ValueError, match=reason):
            prepare._service_environment(runtime_root=call_root)
    finally:
        listener.close()


def test_execute_rejects_changed_service_binary_before_any_service_action(
    tmp_path,
    monkeypatch,
):
    plan_ref, plan = _fixture(tmp_path)
    systemctl = tmp_path / "synthetic-systemctl"
    systemctl.write_bytes(b"#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o700)
    handoff_path = Path(plan["handoff_plan"]["path"])
    handoff = json.loads(handoff_path.read_text())
    handoff["systemctl_path"] = str(systemctl)
    handoff["systemctl_sha256"] = hashlib.sha256(systemctl.read_bytes()).hexdigest()
    plan["handoff_plan"] = _write(handoff_path, handoff)
    plan_ref = _write(Path(plan_ref["path"]), plan)
    source = _source_summary(plan)
    monkeypatch.setattr(prepare.telegram, "_verified_source_map", lambda *_args: source)
    prepared = prepare.prepare(Path(plan_ref["path"]))["preparation"]
    systemctl.write_bytes(b"#!/bin/sh\nexit 7\n")
    systemctl.chmod(0o700)
    monkeypatch.setattr(prepare.telegram, "_validate_contour", lambda *_args: ({}, {}))

    def service_must_not_run(*_args, **_kwargs):
        raise AssertionError("service action preceded binary validation")

    monkeypatch.setattr(prepare, "_service", service_must_not_run)
    with pytest.raises(ValueError, match="systemctl identity mismatch"):
        prepare.execute(
            Path(prepared["path"]),
            prepared["sha256"],
            prepare.EXECUTE_CONFIRMATION,
        )
    assert not (Path(plan["outputs"]["output_root"]) / "handoff-receipt.json").exists()


def test_isolated_cli_loads_lazy_sibling_receipts_and_keeps_installed_precedence(
    tmp_path,
):
    controller = ROOT / "tools/release_1_0_telegram_existing_bot_prepare.py"
    installed_python = _RUNTIME["INSTALLED"] / "bin/python"
    installed_site = _RUNTIME["SITE"]
    poison = tmp_path / "poison"
    (poison / "tools").mkdir(parents=True, mode=0o700)
    (poison / "tools/__init__.py").write_text("")
    (poison / "tools/release_1_0_acceptance.py").write_text("raise RuntimeError('poison-tools-imported')\n")
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(poison),
    }
    help_result = subprocess.run(
        [str(installed_python), "-I", "-B", str(controller), "--help"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
        env=environment,
    )
    assert help_result.returncode == 0, help_result.stderr.decode("utf-8", "replace")

    probe = r"""import importlib.util,json,sys
from pathlib import Path
controller=Path(sys.argv[1]).resolve(strict=True)
source=Path(sys.argv[2]).resolve(strict=True)
site=Path(sys.argv[3]).resolve(strict=True)
expected_isolated=int(sys.argv[4])
if int(bool(sys.flags.isolated)) != expected_isolated or not sys.dont_write_bytecode:
    raise SystemExit("wrong interpreter flags")
if str(source) in sys.path:
    raise SystemExit("consumer injected source path")
spec=importlib.util.spec_from_file_location("isolated_existing_bot_prepare",controller)
module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=module
spec.loader.exec_module(module)
error=module.receipts._acceptance_error("isolated_probe")
from tools import release_1_0_telegram_roundtrip as sibling
import friday
payload={
    "controller":str(Path(module.__file__).resolve()),
    "lazy_error_type":type(error).__name__,
    "sibling":str(Path(sibling.__file__).resolve()),
    "installed_friday":str(Path(friday.__file__).resolve()),
    "source_count":sys.path.count(str(source)),
    "source_after_installed":sys.path.index(str(site)) < sys.path.index(str(source)),
}
print(json.dumps(payload,sort_keys=True,separators=(",",":")))
"""
    command = [
        str(installed_python),
        "-I",
        "-B",
        "-c",
        probe,
        str(controller),
        str(ROOT),
        str(installed_site),
        "1",
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    observed = json.loads(completed.stdout)
    assert observed == {
        "controller": str(controller.resolve()),
        "lazy_error_type": "AcceptanceError",
        "sibling": str((ROOT / "tools/release_1_0_telegram_roundtrip.py").resolve()),
        "installed_friday": str((installed_site / "friday/__init__.py").resolve()),
        "source_count": 1,
        "source_after_installed": True,
    }

    unsafe = subprocess.run(
        [command[0], "-B", *command[3:-1], "0"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        cwd=tmp_path,
        env=environment,
    )
    assert unsafe.returncode != 0
    assert b"poison-tools-imported" in unsafe.stderr


def test_prepare_emits_bound_v2_shapes_without_raw_owner_ids(tmp_path, monkeypatch):
    plan_ref, plan = _fixture(tmp_path)
    source = _source_summary(plan)
    monkeypatch.setattr(prepare.telegram, "_verified_source_map", lambda *_args: source)
    result = prepare.prepare(Path(plan_ref["path"]))
    assert result["execution"] == "NOT_RUN" and result["GO"] is False
    access = json.loads((tmp_path / "access.json").read_text())
    context = json.loads((tmp_path / "context.json").read_text())
    prepared = json.loads((tmp_path / "prepared.json").read_text())
    assert "chat_id" not in access and "user_id" not in access
    assert "chat_id" not in context["expected"] and "user_id" not in context["expected"]
    assert access["owner_identity_source"] == "pinned_existing_friday_configuration"
    assert context["schema"] == prepare.receipts.CONTEXT_SCHEMA_V2
    assert context["harness_identity"]["source_file_count"] == 5
    assert prepared["canonical_status"] == "PRIVATE_DUAL_ROOT_HARNESS_REQUIRES_INDEPENDENT_ADOPTION"
    startup = json.loads((tmp_path / "startup-commands.json").read_text())
    names = [item["command"] for item in startup["commands"]]
    assert "engineer" not in names and "obsidian" not in names and "obsidian_alias" not in names
    assert access["startup_commands_sha256"] == prepare.telegram.canonical_startup_commands_sha256(startup)
    evidence = Path(context["evidence_root"])
    evidence.mkdir(mode=0o700)
    reread = prepare.receipts.read_context(
        tmp_path / "context.json",
        hashlib.sha256((tmp_path / "context.json").read_bytes()).hexdigest(),
    )
    assert reread["_context_version"] == 2


def test_prepare_refuses_mismatched_authoritative_owner(tmp_path, monkeypatch):
    plan_ref, plan = _fixture(tmp_path)
    authority_path = Path(plan["owner_authority"]["path"])
    authority_ref = _write(
        authority_path,
        (
            f"FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS={OWNER_ID + 1}\n"
            f"FRIDAY_TELEGRAM_OWNER_CHAT_IDS={OWNER_ID + 1}\n"
            f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n"
        ).encode(),
    )
    plan["owner_authority"] = authority_ref
    plan_ref = _write(Path(plan_ref["path"]), plan)
    monkeypatch.setattr(prepare.telegram, "_verified_source_map", lambda *_args: {})
    with pytest.raises(ValueError, match="authoritative owner allowlist"):
        prepare.prepare(Path(plan_ref["path"]))


def test_prepare_refuses_output_inside_pinned_candidate_before_creation(tmp_path):
    plan_ref, plan = _fixture(tmp_path)
    forbidden = Path(plan["candidate"]["policy"]["source_root"]) / "attempt-output"
    plan["outputs"]["output_root"] = str(forbidden)
    plan_ref = _write(Path(plan_ref["path"]), plan)
    with pytest.raises(ValueError, match="protected"):
        prepare.prepare(Path(plan_ref["path"]))
    assert not forbidden.exists()


def test_execute_refuses_without_exact_live_confirmation(tmp_path):
    preparation = _write(tmp_path / "prepared.json", {})
    with pytest.raises(ValueError, match="confirmation"):
        prepare.execute(Path(preparation["path"]), preparation["sha256"], "no")


def test_driver_failure_clears_isolated_group_then_restores_exactly_once(
    tmp_path,
    monkeypatch,
):
    plan_ref, plan = _fixture(tmp_path)
    source = _source_summary(plan)
    monkeypatch.setattr(prepare.telegram, "_verified_source_map", lambda *_args: source)
    prepared_result = prepare.prepare(Path(plan_ref["path"]))
    prepared_ref = prepared_result["preparation"]
    lease = Path(json.loads(Path(plan["handoff_plan"]["path"]).read_text())["production_lease_path"])
    state = "active"
    events = []

    def service(_systemctl, _unit, action):
        nonlocal state
        events.append(action)
        if action == "stop":
            state = "inactive"
        elif action == "start":
            state = "active"
            lease.write_text("restored\n")
            lease.chmod(0o600)
        return type("Completed", (), {"returncode": 0})()

    def popen(*_args, **_kwargs):
        assert state == "inactive"
        events.append("driver")

        class Child:
            pid = 987654
            returncode = 7

            @staticmethod
            def communicate(timeout):
                assert timeout > 600
                return b"", b""

        return Child()

    monkeypatch.setattr(prepare, "_service", service)
    monkeypatch.setattr(prepare, "_service_state", lambda *_args: state)
    monkeypatch.setattr(prepare.subprocess, "Popen", popen)
    monkeypatch.setattr(prepare.telegram, "_validate_contour", lambda *_args: ({}, {}))
    monkeypatch.setattr(prepare.telegram, "_lease_is_active", lambda _path: state == "active")

    def clear_group(_pid, _grace):
        events.append("group-clear")
        return True

    monkeypatch.setattr(prepare, "_terminate_process_group", clear_group)
    with pytest.raises(ValueError, match="bounded restoration"):
        prepare.execute(
            Path(prepared_ref["path"]),
            prepared_ref["sha256"],
            prepare.EXECUTE_CONFIRMATION,
        )
    assert events == ["stop", "driver", "group-clear", "start"]
    receipt = json.loads((Path(plan["outputs"]["output_root"]) / "handoff-receipt.json").read_text())
    assert receipt["stop_count"] == receipt["restore_count"] == 1
    assert receipt["isolated_process_group_clear_before_restore"] is True
    assert receipt["post_restore_state"] == "active"


def test_handoff_receipt_requires_isolated_group_clear_before_restore(tmp_path):
    _plan_ref, plan = _fixture(tmp_path)
    receipt = {
        "schema": prepare.receipts.HANDOFF_RECEIPT_SCHEMA,
        "plan": plan["handoff_plan"],
        "unit_name": "friday-bridge.service",
        "systemctl_sha256": json.loads(Path(plan["handoff_plan"]["path"]).read_text())["systemctl_sha256"],
        "initial_state": "active",
        "stop_count": 1,
        "stop_returncode": 0,
        "post_stop_state": "inactive",
        "production_lease_inactive": True,
        "driver_started_after_stop": True,
        "isolated_process_group_clear_before_restore": True,
        "restore_count": 1,
        "restore_returncode": 0,
        "post_restore_state": "active",
        "production_lease_active": True,
        "no_second_consumer": True,
        "production_home_as_scratch": False,
        "history_touched": False,
        "started_at": "2026-09-15T10:00:00Z",
        "stopped_at": "2026-09-15T10:00:01Z",
        "restored_at": "2026-09-15T10:00:02Z",
        "GO": False,
    }
    prepare.receipts._validate_handoff_receipt(receipt, plan_reference=plan["handoff_plan"])
    receipt["isolated_process_group_clear_before_restore"] = False
    with pytest.raises(Exception, match="telegram_handoff_receipt_binding_invalid"):
        prepare.receipts._validate_handoff_receipt(receipt, plan_reference=plan["handoff_plan"])
