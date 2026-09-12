"""Synthetic mechanics controls for the offline Telegram receipt reader.

These tests exercise reader wiring and refusals.  They never dispatch Telegram,
start Friday, or grant deployment-device credit to an acceptance run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import release_1_0_acceptance as acceptance
from tools import release_1_0_telegram_receipts as receipts
from tools import release_1_0_telegram_roundtrip as telegram


def _write(path: Path, value: object | bytes) -> dict[str, object]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode() + b"\n"
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def _unsized(ref: dict[str, object]) -> dict[str, object]:
    return {key: ref[key] for key in ("path", "sha256")}


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token: bool = True):
    output = tmp_path / "run"
    evidence = output / "evidence"
    effect_root = evidence / "telegram-effects"
    isolation = output / "contour"
    home = isolation / "friday-home"
    data = home / "data"
    state = home / "state"
    cache = home / "cache"
    logs = home / "logs"
    files_dir = data / "files"
    for path in (output, evidence, effect_root, isolation, home, data, state, cache, logs, files_dir):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)

    attempt_id = "telegram_reader_attempt_0001"
    canary = "telegram:telegram_reader_attempt_0001"
    candidate_sha, candidate_tree, base_sha = "1" * 40, "2" * 40, "3" * 40
    source_map_sha = "6" * 64
    suite = "fixture-suite"
    bot_token = "123456789:synthetic-high-entropy-reader-token"
    env_body = f"FRIDAY_TELEGRAM_BOT_TOKEN={bot_token}\n" if token else "FRIDAY_API_TOKEN=missing-bot\n"
    env_ref = _write(tmp_path / "contour.env", env_body.encode())
    policy_ref = _write(tmp_path / "policy.json", {})
    access_ref = _write(tmp_path / "access.json", {})
    controller_path = tmp_path / "owned-controller.py"
    controller_path.write_text("raise SystemExit('fixture only')\n")
    controller_path.chmod(0o700)
    controller_ref = {
        "path": str(controller_path),
        "sha256": hashlib.sha256(controller_path.read_bytes()).hexdigest(),
    }
    driver_path = receipts.ROOT / "tools/release_1_0_telegram_roundtrip.py"
    driver_ref = {
        "path": str(driver_path),
        "sha256": hashlib.sha256(driver_path.read_bytes()).hexdigest(),
    }
    observer_path = output / "observer-result.json"
    observer_ref = _write(observer_path, {"synthetic": "reader mechanics only"})
    main_ref = _write(home / "friday.sqlite3", b"synthetic-main-db")
    inbox_ref = _write(state / "telegram-inbox.sqlite3", b"synthetic-inbox-db")

    identity = {
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "base_sha": base_sha,
        "wheel_sha256": "4" * 64,
        "inventory_sha256": "5" * 64,
        "source_map_sha256": source_map_sha,
        "source_root": str(receipts.ROOT),
        "suite_revision": suite,
    }
    expected = {
        "contour_id": "telegram-fixture-contour",
        "bot_user_id": 7001,
        "chat_id": 8001,
        "user_id": 8001,
        "observer_mode": "owner_manual_readback",
        "bot_token_sha256": hashlib.sha256(bot_token.encode()).hexdigest(),
        "env_sha256": env_ref["sha256"],
    }
    context = {
        "schema": receipts.CONTEXT_SCHEMA,
        "case_id": receipts.CASE_ID,
        "attempt_id": attempt_id,
        "output_root": str(output),
        "evidence_root": str(evidence),
        "candidate_identity": identity,
        "policy": _unsized(policy_ref),
        "access": _unsized(access_ref),
        "controller": controller_ref,
        "driver": driver_ref,
        "expected": expected,
        "budgets": receipts.FROZEN_BUDGETS,
    }
    context_ref = _write(tmp_path / "expected-context.json", context)

    budgets = SimpleNamespace(
        timeout_s=600,
        max_getupdates_rounds=20,
        max_inbound_user_messages=1,
        max_outbound_bot_posts=2,
        max_evidence_bytes=1048576,
        process_log_bytes=65536,
        cleanup_grace_s=5,
    )
    policy = SimpleNamespace(
        attempt_id=attempt_id,
        canary=canary,
        candidate=SimpleNamespace(
            source_root=receipts.ROOT,
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
            suite_revision=suite,
            manifest_sha256="7" * 64,
            python_executable=driver_path,
            python_sha256=driver_ref["sha256"],
        ),
        contour=SimpleNamespace(
            contour_id=expected["contour_id"],
            isolation_root=output / "contour",
            env_file=Path(env_ref["path"]),
            env_file_sha256=env_ref["sha256"],
            friday_home=home,
            data_dir=data,
            state_dir=state,
            cache_dir=cache,
            log_dir=logs,
            files_dir=files_dir,
            database_path=Path(main_ref["path"]),
            inbox_db_path=Path(inbox_ref["path"]),
        ),
        access=SimpleNamespace(manifest_path=Path(access_ref["path"]), manifest_sha256=access_ref["sha256"]),
        observer=SimpleNamespace(mode=expected["observer_mode"], result_path=observer_path),
        budgets=budgets,
    )
    access = SimpleNamespace(
        contour_id=expected["contour_id"],
        attempt_id=attempt_id,
        candidate_sha=candidate_sha,
        candidate_manifest_sha256="7" * 64,
        bot_user_id=expected["bot_user_id"],
        chat_id=expected["chat_id"],
        user_id=expected["user_id"],
        observer_mode=expected["observer_mode"],
    )
    observer = {
        "inbound_message_id": 901,
        "inbound_text": (
            f"Friday roundtrip {canary}. Reply with exactly this marker and nothing else: {canary}"
        ),
        "matching_outbound_message_id": 902,
        "matching_canary_count": 1,
        "outbound_count": 1,
        "observer_mode": expected["observer_mode"],
        "window_complete": True,
    }
    durable = {
        "owner_user_id": telegram.OWNER_USER_ID,
        "telegram_update_id": 900,
        "telegram_message_id": 901,
        "source_ref_sha256": "8" * 64,
        "raw_content_sha256": hashlib.sha256(observer["inbound_text"].encode()).hexdigest(),
        "raw_inbound_count": 1,
        "backend_user_message_count": 1,
        "idempotency_complete": True,
        "botapi_receipt_message_id": 902,
        "botapi_receipt_count": 1,
        "backend_assistant_message_id": "assistant-1",
        "backend_assistant_content_sha256": "9" * 64,
        "visible_destination_message_id": 902,
        "remaining_inbox_updates": 0,
        "durable_offset": 901,
    }
    effects = {
        "schema": telegram.EFFECT_LEDGER_SCHEMA,
        "source": "independent_parent_reader",
        "attempts": 7,
        "counts": {},
        "successful_counts": {},
        "request_sha256": {},
        "unknown_count": 0,
        "violations": [],
        "unfinished": [],
        "telegram_update_ids": [900],
        "telegram_message_ids": [901],
        "send_message_receipt_ids": [902],
        "ledger_bytes": 1024,
        "inventory_sha256": "a" * 64,
    }
    source_summary = {
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "suite_revision": suite,
        "manifest_sha256": "7" * 64,
        "source_map_sha256": source_map_sha,
        "source_file_count": 10,
        "module_sha256": driver_ref["sha256"],
        "cli_sha256": "b" * 64,
        "bridge_base_sha256": "c" * 64,
    }
    request = {
        "schema": telegram.OBSERVER_REQUEST_SCHEMA,
        "attempt_id": attempt_id,
        "candidate_sha": candidate_sha,
        "candidate_manifest_sha256": "7" * 64,
        "contour_id": expected["contour_id"],
        "canary": canary,
        "chat_id": expected["chat_id"],
        "user_id": expected["user_id"],
        "bot_user_id": expected["bot_user_id"],
        "bot_username": "fixture_bot",
        "observer_mode": expected["observer_mode"],
        "inbound_text": observer["inbound_text"],
        "result_path": str(observer_path),
        "result_schema": telegram.OBSERVER_RESULT_SCHEMA,
        "max_inbound_user_messages": 1,
        "max_outbound_bot_posts": 2,
        "unknown_effect_policy": "do_not_resend",
        "issued_at": "2026-09-11T00:00:01Z",
    }
    origin = {}
    cleanup = {}
    for role, pid in (("server", 41001), ("telegram-bridge", 41002)):
        origin[role] = {
            "schema": "friday.release-1-0-child-origin.v1",
            "role": role,
            "pid": pid,
            "candidate_sha": candidate_sha,
            "source_root": str(receipts.ROOT),
            "cli_origin": str(receipts.ROOT / "friday/cli.py"),
            "effect_guard_origin": str(driver_path) if role == "telegram-bridge" else "",
            "effect_guard_sha256": driver_ref["sha256"] if role == "telegram-bridge" else "",
            "effect_spec_sha256": "PLACEHOLDER" if role == "telegram-bridge" else "",
            "argv": ["friday.cli", "--env-file", str(Path(env_ref["path"]).resolve()), role],
            "observed_at_ns": 1,
        }
        cleanup[role] = {
            "schema": "friday.release-1-0-owned-process-log.v1",
            "role": role,
            "pid": pid,
            "returncode": 0,
            "process_group_clear": True,
            "capture_drained": True,
            "captured_bytes": 0,
            "discarded_bytes": 0,
            "secrets_persisted": False,
        }

    spec_ref = _write(effect_root / "spec.json", {"synthetic": "mechanics"})
    origin["telegram-bridge"]["effect_spec_sha256"] = spec_ref["sha256"]
    documents = {
        "observer-request.json": request,
        "observer-sanitized.json": {
            "schema": "friday.release-1-0-observer-sanitized.v1",
            "attempt_id": attempt_id,
            "candidate_sha": candidate_sha,
            "observer_result_path": str(observer_path),
            "observer_result_sha256": observer_ref["sha256"],
            "observer_result_bytes": observer_ref["size_bytes"],
            **observer,
        },
        "durable-observation.json": {
            "schema": "friday.release-1-0-telegram-durable-observation.v1",
            "attempt_id": attempt_id,
            "candidate_sha": candidate_sha,
            **durable,
        },
        "origin-server.json": origin["server"],
        "origin-telegram-bridge.json": origin["telegram-bridge"],
        "log-server.json": cleanup["server"],
        "log-telegram-bridge.json": cleanup["telegram-bridge"],
    }
    stages = [
        ("preflight", "STARTED", {}),
        ("candidate", "VERIFIED", source_summary),
        (
            "telegram_preflight",
            "VERIFIED",
            {
                "bot_user_id": expected["bot_user_id"],
                "webhook_empty": True,
                "pending_update_count": 0,
            },
        ),
        ("server", "STARTED", {"pid": 41001}),
        ("backend", "READY", {"pid": 41001}),
        ("telegram-bridge", "STARTED", {"pid": 41002}),
        ("bridge", "READY", {"pid": 41002}),
        ("observer", "REQUESTED", {}),
        ("observer", "VERIFIED", {}),
        (
            "four_legs",
            "VERIFIED",
            {
                "telegram_update_id": durable["telegram_update_id"],
                "inbound_message_id": durable["telegram_message_id"],
                "outbound_message_id": durable["visible_destination_message_id"],
            },
        ),
    ]
    event_raw = b"".join(
        json.dumps(
            {
                "schema": "friday.release-1-0-telegram-roundtrip-event.v1",
                "at": f"2026-09-11T00:00:{index:02d}Z",
                "stage": stage,
                "state": status,
                "facts": facts,
            },
            sort_keys=True,
        ).encode()
        + b"\n"
        for index, (stage, status, facts) in enumerate(stages)
    )
    _write(evidence / "events.jsonl", event_raw)
    for name, document in documents.items():
        _write(evidence / name, document)
    _write(effect_root / "seal.json", {"synthetic": "mechanics"})
    _write(effect_root / "reservation.bin", b"0" * (512 << 10))
    _write(effect_root / "driver_preflight" / "000001-start.json", {"synthetic": "mechanics"})
    _write(effect_root / "driver_preflight" / "000001-terminal.json", {"synthetic": "mechanics"})

    report = {
        "schema": telegram.REPORT_SCHEMA,
        "assignment": "ASTRA-R10-TELEGRAM-BUDGET-SOL-082",
        "attempt_id": attempt_id,
        "case_pass": True,
        "case_outcome": "LIVE_TELEGRAM_ROUNDTRIP_OBSERVED",
        "reasons": [],
        "observer": observer,
        "durable": durable,
        "telegram_effects": effects,
        "cleanup": [
            {
                key: cleanup[role][key]
                for key in (
                    "role",
                    "pid",
                    "returncode",
                    "process_group_clear",
                    "capture_drained",
                    "captured_bytes",
                    "discarded_bytes",
                )
            }
            for role in ("telegram-bridge", "server")
        ],
        "origins": [
            {
                key: origin[role][key]
                for key in (
                    "role",
                    "pid",
                    "candidate_sha",
                    "cli_origin",
                    "effect_guard_origin",
                    "effect_guard_sha256",
                    "effect_spec_sha256",
                    "argv",
                )
            }
            for role in ("server", "telegram-bridge")
        ],
        "candidate": source_summary,
        "source_unchanged": True,
        "cleanup_clear": True,
        "GO": False,
        "release_ready": False,
        "full_gate_credit": False,
        "elapsed_s": 5.0,
    }
    _write(evidence / "report.json", report)

    files = {}
    for path in sorted(evidence.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            files[path.relative_to(evidence).as_posix()] = {
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
    normalized = {
        name: {"sha256": ref["sha256"], "size_bytes": ref["size_bytes"]} for name, ref in files.items()
    }
    receipt = {
        "schema": receipts.RECEIPT_SCHEMA,
        "case_id": receipts.CASE_ID,
        "attempt_id": attempt_id,
        "attempt": 1,
        "expected_context": _unsized(context_ref),
        "policy": _unsized(policy_ref),
        "access": _unsized(access_ref),
        "evidence": {
            "root": str(evidence),
            "files": files,
            "inventory_sha256": receipts._digest(normalized),
            "total_bytes": sum(ref["size_bytes"] for ref in files.values()),
        },
        "observer_result": observer_ref,
        "databases": {"main": main_ref, "inbox": inbox_ref},
        "controller": {
            "executable": controller_ref,
            "pid": 41000,
            "argv": [
                str(driver_path),
                "-I",
                "-B",
                str(driver_path),
                "--policy",
                str(policy_ref["path"]),
                "--evidence-dir",
                str(evidence),
            ],
            "driver_sha256": driver_ref["sha256"],
            "policy_sha256": policy_ref["sha256"],
            "evidence_root": str(evidence),
            "outcome": "completed",
            "leader_returncode": 0,
            "timed_out": False,
            "error_code": None,
            "start_count": 1,
            "terminal_count": 1,
            "unknown_count": 0,
            "started_at": "2026-09-11T00:00:00Z",
            "ended_at": "2026-09-11T00:00:10Z",
            "process_group_clear": True,
            "leader_reaped": True,
            "descendants_clear": True,
            "capture_drained": True,
            "stop_unconfirmed_emitted": False,
        },
        "GO": False,
        "release_ready": False,
        "full_gate_credit": False,
    }
    receipt_ref = _write(output / "result.json", receipt)

    monkeypatch.setattr(telegram, "load_policy", lambda _path: policy)
    monkeypatch.setattr(
        telegram.AccessManifest,
        "from_document",
        classmethod(lambda _cls, _document, **_kwargs: access),
    )
    monkeypatch.setattr(telegram, "_verified_source_map", lambda _candidate: dict(source_summary))
    monkeypatch.setattr(
        telegram,
        "validate_observer_result",
        lambda *_args, **_kwargs: dict(observer),
    )
    monkeypatch.setattr(
        telegram,
        "inspect_durable_outcome",
        lambda *_args, **_kwargs: dict(durable),
    )
    monkeypatch.setattr(telegram, "inspect_effect_ledger", lambda *_args, **_kwargs: dict(effects))

    return {
        "receipt": receipt,
        "receipt_ref": receipt_ref,
        "context": context,
        "context_ref": context_ref,
        "identity": {
            key: identity[key]
            for key in (
                "candidate_sha",
                "candidate_tree",
                "base_sha",
                "wheel_sha256",
                "inventory_sha256",
            )
        },
        "suite": suite,
        "observer_path": observer_path,
        "durable": durable,
    }


def _audit(bundle):
    return receipts.audit_telegram_execution(
        receipt_path=Path(bundle["receipt_ref"]["path"]),
        receipt_sha256=bundle["receipt_ref"]["sha256"],
        context_path=Path(bundle["context_ref"]["path"]),
        context_sha256=bundle["context_ref"]["sha256"],
        expected_identity=bundle["identity"],
        expected_suite=bundle["suite"],
    )


def test_registration_points_to_actual_driver_without_pytest_execution_credit():
    case = next(item for item in acceptance.load_matrix()["cases"] if item["id"] == receipts.CASE_ID)
    handler, layer, nodes, driver = acceptance.registered_case_handlers()[receipts.CASE_ID]
    assert handler is telegram.run_roundtrip
    assert layer == "deployment-device" and driver == "telegram-roundtrip"
    assert case["handler"] == "tools/release_1_0_telegram_roundtrip.py:run_roundtrip"
    assert case["node_ids"] == list(nodes)
    assert case["release_required"] is True and case["timeout_s"] == 600


def test_plan_contains_both_telegram_harness_modules():
    plan = acceptance.plan_commands("final")
    command = next(item for item in plan["commands"]["harness"] if "-m pytest" in item)
    assert "tests/test_release_1_0_telegram_roundtrip.py" in command
    assert "tests/test_release_1_0_telegram_receipts.py" in command
    deployment = " ".join(plan["commands"]["telegram_deployment_device"])
    assert "tools/release_1_0_telegram_roundtrip.py" in deployment
    assert "--telegram-context-sha256" in deployment and "--telegram-receipt-sha256" in deployment


def test_synthetic_complete_reader_path_is_mechanics_only(tmp_path, monkeypatch):
    result = _audit(_fixture(tmp_path, monkeypatch))
    assert result["status"] == "PASS"
    assert result["case_layers"] == {receipts.CASE_ID: "deployment-device"}
    assert all(result["four_legs"].values())
    assert result["first_attempt_verified"] is True
    assert result["unit_controls_grant_live_credit"] is False
    assert result["go_emitted"] is False and result["full_gate_credit"] is False


@pytest.mark.parametrize(
    "field",
    ["start_count", "terminal_count", "unknown_count", "process_group_clear"],
)
def test_incomplete_or_duplicate_controller_outcome_is_blocked(tmp_path, monkeypatch, field):
    bundle = _fixture(tmp_path, monkeypatch)
    bundle["receipt"]["controller"][field] = 2 if field.endswith("count") else False
    bundle["receipt_ref"] = _write(Path(bundle["receipt_ref"]["path"]), bundle["receipt"])
    result = _audit(bundle)
    assert result["status"] == "BLOCKED"
    assert result["case_layers"] == {}


def test_rehashed_envelope_cannot_bless_mutated_observer_bytes(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path, monkeypatch)
    forged_ref = _write(Path(bundle["observer_path"]), {"mutated": True})
    bundle["receipt"]["observer_result"] = forged_ref
    bundle["receipt_ref"] = _write(Path(bundle["receipt_ref"]["path"]), bundle["receipt"])
    result = _audit(bundle)
    assert result["status"] == "FAIL"
    assert result["evidence_status"] == "EVIDENCE_INVALID"


def test_missing_hmac_secret_is_a_precise_blocker(tmp_path, monkeypatch):
    result = _audit(_fixture(tmp_path, monkeypatch, token=False))
    assert result["status"] == "BLOCKED"
    assert result["root_failure"] == "telegram_effect_hmac_key_missing"


def test_foreign_token_identity_cannot_pass(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path, monkeypatch)
    bundle["context"]["expected"]["bot_token_sha256"] = "f" * 64
    bundle["context_ref"] = _write(Path(bundle["context_ref"]["path"]), bundle["context"])
    bundle["receipt"]["expected_context"] = _unsized(bundle["context_ref"])
    bundle["receipt_ref"] = _write(Path(bundle["receipt_ref"]["path"]), bundle["receipt"])
    assert _audit(bundle)["status"] == "FAIL"


def test_foreign_chat_context_cannot_pass(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path, monkeypatch)
    bundle["context"]["expected"]["chat_id"] += 1
    bundle["context"]["expected"]["user_id"] += 1
    bundle["context_ref"] = _write(Path(bundle["context_ref"]["path"]), bundle["context"])
    bundle["receipt"]["expected_context"] = _unsized(bundle["context_ref"])
    bundle["receipt_ref"] = _write(Path(bundle["receipt_ref"]["path"]), bundle["receipt"])
    assert _audit(bundle)["status"] == "FAIL"


def test_fake_visible_readback_cannot_pass(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path, monkeypatch)
    forged = {**bundle["durable"], "visible_destination_message_id": 999999}
    monkeypatch.setattr(telegram, "inspect_durable_outcome", lambda *_args, **_kwargs: forged)
    assert _audit(bundle)["status"] == "FAIL"


def test_unknown_effect_ledger_cannot_pass(tmp_path, monkeypatch):
    bundle = _fixture(tmp_path, monkeypatch)

    def unknown(*_args, **_kwargs):
        raise telegram.RoundtripError("FAIL", "telegram_effect_ledger_incomplete")

    monkeypatch.setattr(telegram, "inspect_effect_ledger", unknown)
    assert _audit(bundle)["status"] == "FAIL"


def test_partial_telegram_cli_evidence_is_refused_before_audit():
    with pytest.raises(SystemExit) as raised:
        acceptance.main(["--audit-only", "--telegram-receipt", "/var/tmp/missing-telegram.json"])
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "raw",
    [b'{"schema":1,"schema":2}', b'{"number":NaN}', b'{"number":1e400}'],
)
def test_duplicate_or_nonfinite_context_is_refused(tmp_path, raw):
    path = tmp_path / "context.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(acceptance.AcceptanceError, match="telegram_context_json_invalid"):
        receipts.read_context(path, hashlib.sha256(raw).hexdigest())
