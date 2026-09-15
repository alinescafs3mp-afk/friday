"""Offline producer controls; never invoke the live execute subcommand."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
