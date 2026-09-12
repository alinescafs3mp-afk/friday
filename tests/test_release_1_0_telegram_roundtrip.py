from __future__ import annotations

import ast
import asyncio
import dataclasses
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "release_1_0_telegram_roundtrip.py"
    name = "release_1_0_telegram_roundtrip_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rt = _load_module()
OBSERVER_REQUESTED_AT = dt.datetime(2026, 9, 11, 8, 59, tzinfo=dt.UTC)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy_document(tmp_path: Path, *, mode: str = "owner_manual_readback") -> dict:
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    isolation = tmp_path / "isolation"
    isolation.mkdir(mode=0o700)
    observer_parent = tmp_path / "observer"
    observer_parent.mkdir(mode=0o700)
    home = isolation / "roundtrip-home"
    data = home / "data"
    state = data / "state"
    cache = home / "cache"
    logs = home / "logs"
    database = data / "friday.sqlite3"
    inbox = state / "telegram-inbox.sqlite3"
    files = data / "files"
    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")
    python = Path(sys.executable).resolve()
    access = tmp_path / "access.json"
    access.write_text("{}")
    access.chmod(0o600)
    env_file = tmp_path / "roundtrip.env"
    env_file.write_text("")
    env_file.chmod(0o600)
    observer = {
        "mode": mode,
        "result_path": str(observer_parent / "result.json"),
    }
    if mode == "external_user_adapter":
        adapter = tmp_path / "adapter"
        adapter.write_text("#!/bin/sh\nexit 0\n")
        adapter.chmod(0o700)
        observer.update(
            {
                "adapter_argv": [str(adapter), "--session", str(tmp_path / "session")],
                "adapter_executable_sha256": _sha(adapter),
            }
        )
    return {
        "schema": rt.POLICY_SCHEMA,
        "attempt_id": "attempt_0123456789abcdef",
        "canary": "TG:attempt_0123456789abcdef:canary",
        "candidate": {
            "manifest_path": str(candidate),
            "manifest_sha256": "a" * 64,
            "source_root": str(tmp_path / "pinned-source"),
            "candidate_sha": "b" * 40,
            "candidate_tree": "c" * 40,
            "suite_revision": "r10-test",
            "python_executable": str(python),
            "python_sha256": _sha(python),
        },
        "contour": {
            "contour_id": "telegram-test-contour",
            "isolation_root": str(isolation),
            "friday_home": str(home),
            "data_dir": str(data),
            "state_dir": str(state),
            "cache_dir": str(cache),
            "log_dir": str(logs),
            "env_file": str(env_file),
            "env_file_sha256": _sha(env_file),
            "database_path": str(database),
            "inbox_db_path": str(inbox),
            "files_dir": str(files),
            "backend_origin": "http://127.0.0.1:18421",
            "known_production_homes": [str(tmp_path / "production")],
            "foreign_lease_paths": [],
        },
        "access": {
            "manifest_path": str(access),
            "manifest_sha256": "d" * 64,
        },
        "observer": observer,
        "budgets": {
            "timeout_s": 600,
            "backend_ready_timeout_s": 10,
            "poll_interval_s": 0.1,
            "max_getupdates_rounds": 20,
            "max_inbound_user_messages": 1,
            "max_outbound_bot_posts": 2,
            "max_evidence_bytes": 1048576,
            "process_log_bytes": 65536,
            "cleanup_grace_s": 3,
        },
    }


def _policy(tmp_path: Path, *, mode: str = "owner_manual_readback"):
    return rt.Policy.from_document(_policy_document(tmp_path, mode=mode))


def _access_document(policy, *, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.UTC)
    return {
        "schema": rt.ACCESS_SCHEMA,
        "contour_id": policy.contour.contour_id,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "dedicated_for_release_test": True,
        "same_bot_production_consumer_stopped": True,
        "clean_backlog_expected": True,
        "bot_user_id": 900000001,
        "chat_id": 700000001,
        "user_id": 700000001,
        "observer_mode": policy.observer.mode,
        "issued_at": (now - dt.timedelta(minutes=1)).isoformat(),
        "expires_at": (now + dt.timedelta(hours=1)).isoformat(),
        "nonce": "owner-approved-nonce-0123456789",
    }


def _access(policy):
    now = dt.datetime.now(dt.UTC)
    return rt.AccessManifest.from_document(
        _access_document(policy, now=now),
        policy=policy,
        now=now,
    )


def _write_isolated_env(policy, access, *, replacements: dict[str, str] | None = None):
    values = {
        "FRIDAY_HOME": str(policy.contour.friday_home),
        "FRIDAY_DATA_DIR": str(policy.contour.data_dir),
        "FRIDAY_STATE_DIR": str(policy.contour.state_dir),
        "FRIDAY_CACHE_DIR": str(policy.contour.cache_dir),
        "FRIDAY_LOG_DIR": str(policy.contour.log_dir),
        "FRIDAY_DATABASE_PATH": str(policy.contour.database_path),
        "FRIDAY_FILES_DIR": str(policy.contour.files_dir),
        "FRIDAY_API_HOST": "127.0.0.1",
        "FRIDAY_API_BIND_ADDRESS": "127.0.0.1",
        "FRIDAY_API_PORT": "18421",
        "FRIDAY_BACKEND_URL": policy.contour.backend_origin,
        "FRIDAY_TELEGRAM_BOT_TOKEN": "900000001:dedicated-test-token",
        "FRIDAY_TELEGRAM_BRIDGE_SECRET": "b" * 48,
        "FRIDAY_API_TOKEN": "a" * 48,
        "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS": "",
        "FRIDAY_TELEGRAM_OWNER_CHAT_IDS": str(access.chat_id),
        "FRIDAY_TELEGRAM_OPEN_REGISTRATION": "0",
        "FRIDAY_SHARED_ARCHIVE": "0",
        "FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS": "0",
        "FRIDAY_TRUST_PROXY_HEADERS": "0",
        "FRIDAY_HOST_CONTROL_ENABLED": "0",
        "FRIDAY_OPERATOR_FULL_AUTONOMY": "0",
        "FRIDAY_WORKERS_ENABLED": "0",
        "FRIDAY_AUTONOMY_ENABLED": "0",
        "FRIDAY_COGNITION_ENABLED": "0",
        "FRIDAY_REMINDERS_ENABLED": "0",
        "FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK": "1",
        "FRIDAY_LLM_ENABLED": "1",
        "FRIDAY_SSL_CERTFILE": "",
        "FRIDAY_SSL_KEYFILE": "",
        "FRIDAY_CORS_ORIGINS": "",
    }
    values.update(replacements or {})
    policy.contour.env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    policy.contour.env_file.chmod(0o600)
    return dataclasses.replace(
        policy,
        contour=dataclasses.replace(
            policy.contour,
            env_file_sha256=_sha(policy.contour.env_file),
        ),
    )


def _observer_document(policy, access, *, duplicates: bool = False) -> tuple[dict, str]:
    bot_id = 900000001
    request = rt.build_observer_request(
        policy,
        access,
        {"bot_user_id": bot_id, "bot_username": "dedicated_friday"},
    )
    message = {
        "message_id": 812,
        "from_bot_id": bot_id,
        "chat_id": access.chat_id,
        "text": policy.canary,
        "observed_at": "2026-09-11T09:01:02+00:00",
    }
    outbound = [message]
    if duplicates:
        outbound.append({**message, "message_id": 813})
    result = {
        "schema": rt.OBSERVER_RESULT_SCHEMA,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "contour_id": access.contour_id,
        "canary": policy.canary,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "bot_user_id": bot_id,
        "observer_mode": policy.observer.mode,
        "window_complete": True,
        "inbound_message": {
            "message_id": 411,
            "from_user_id": access.user_id,
            "chat_id": access.chat_id,
            "text": request["inbound_text"],
            "sent_at": "2026-09-11T09:00:00+00:00",
        },
        "outbound_messages": outbound,
        "observed_at": "2026-09-11T09:01:03+00:00",
    }
    return result, request["inbound_text"]


def test_policy_is_exact_finite_and_binds_canary(tmp_path):
    document = _policy_document(tmp_path)
    policy = rt.Policy.from_document(document)
    assert policy.attempt_id in policy.canary
    assert policy.budgets.timeout_s == 600
    document["unexpected"] = True
    with pytest.raises(ValueError, match="keys invalid"):
        rt.Policy.from_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 601),
        ("max_getupdates_rounds", 21),
        ("max_inbound_user_messages", 2),
        ("max_outbound_bot_posts", 3),
        ("max_evidence_bytes", 2_000_000),
    ],
)
def test_policy_rejects_unbounded_work(tmp_path, field, value):
    document = _policy_document(tmp_path)
    document["budgets"][field] = value
    with pytest.raises(ValueError):
        rt.Policy.from_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 599),
        ("max_getupdates_rounds", 19),
        ("max_evidence_bytes", 524288),
    ],
)
def test_policy_refuses_noncanonical_live_effect_budget(tmp_path, field, value):
    document = _policy_document(tmp_path)
    document["budgets"][field] = value
    with pytest.raises(ValueError, match="frozen live scope"):
        rt.Policy.from_document(document)


def test_policy_rejects_capacity_that_cannot_hold_logs(tmp_path):
    document = _policy_document(tmp_path, mode="external_user_adapter")
    document["budgets"]["process_log_bytes"] = 262144
    with pytest.raises(ValueError, match="cannot contain"):
        rt.Policy.from_document(document)


@pytest.mark.parametrize("mode", ["fake", "injected_update", "local_http", "bot_api_only"])
def test_policy_rejects_synthetic_observer_modes(tmp_path, mode):
    document = _policy_document(tmp_path)
    document["observer"]["mode"] = mode
    with pytest.raises(ValueError, match="real-user mode"):
        rt.Policy.from_document(document)


def test_manual_mode_cannot_smuggle_an_adapter(tmp_path):
    document = _policy_document(tmp_path)
    document["observer"]["adapter_argv"] = ["/bin/true"]
    document["observer"]["adapter_executable_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="manual observer"):
        rt.Policy.from_document(document)


def test_external_adapter_is_absolute_pinned_and_bounded(tmp_path):
    policy = _policy(tmp_path, mode="external_user_adapter")
    assert policy.observer.adapter_argv[0].startswith("/")
    document = _policy_document(tmp_path / "other", mode="external_user_adapter")
    document["observer"]["adapter_executable_sha256"] = ""
    with pytest.raises(ValueError, match="must be pinned"):
        rt.Policy.from_document(document)


def test_access_requires_dedicated_private_clean_contour(tmp_path):
    policy = _policy(tmp_path)
    now = dt.datetime.now(dt.UTC)
    good = _access_document(policy, now=now)
    access = rt.AccessManifest.from_document(good, policy=policy, now=now)
    assert access.chat_id == access.user_id
    for key in (
        "dedicated_for_release_test",
        "same_bot_production_consumer_stopped",
        "clean_backlog_expected",
    ):
        bad = dict(good)
        bad[key] = False
        with pytest.raises(ValueError):
            rt.AccessManifest.from_document(bad, policy=policy, now=now)
    wrong_chat = dict(good)
    wrong_chat["chat_id"] += 1
    with pytest.raises(ValueError, match="private chat"):
        rt.AccessManifest.from_document(wrong_chat, policy=policy, now=now)


def test_access_manifest_expires_and_is_bound_to_observer(tmp_path):
    policy = _policy(tmp_path)
    now = dt.datetime.now(dt.UTC)
    expired = _access_document(policy, now=now)
    expired["expires_at"] = (now - dt.timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="lifetime"):
        rt.AccessManifest.from_document(expired, policy=policy, now=now)
    wrong_mode = _access_document(policy, now=now)
    wrong_mode["observer_mode"] = "external_user_adapter"
    with pytest.raises(ValueError, match="observer binding"):
        rt.AccessManifest.from_document(wrong_mode, policy=policy, now=now)


def test_contour_requires_new_private_loopback_state_and_exact_owner(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(policy, access)
    env, summary = rt._validate_contour(policy, access)
    assert summary["backend_origin"] == "http://127.0.0.1:18421"
    assert env["FRIDAY_TELEGRAM_OWNER_CHAT_IDS"] == str(access.chat_id)
    assert not policy.contour.friday_home.exists()


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("FRIDAY_TELEGRAM_OPEN_REGISTRATION", "1", "isolated_env_mismatch"),
        ("FRIDAY_SHARED_ARCHIVE", "1", "isolated_env_mismatch"),
        ("FRIDAY_API_BIND_ADDRESS", "0.0.0.0", "isolated_env_mismatch"),
        ("FRIDAY_TELEGRAM_OWNER_CHAT_IDS", "700000002", "dedicated_chat_allowlist_mismatch"),
        ("FRIDAY_REMINDERS_ENABLED", "1", "isolated_env_mismatch"),
    ],
)
def test_contour_refuses_widened_or_background_configuration(tmp_path, key, value, reason):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(policy, access, replacements={key: value})
    with pytest.raises(rt.RoundtripError) as captured:
        rt._validate_contour(policy, access)
    assert captured.value.code == reason


def test_contour_refuses_missing_bot_and_overlapping_credentials(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(policy, access, replacements={"FRIDAY_TELEGRAM_BOT_TOKEN": ""})
    with pytest.raises(rt.RoundtripError) as missing:
        rt._validate_contour(policy, access)
    assert missing.value.outcome == "NOT_RUN"
    assert missing.value.code == "dedicated_bot_credential_missing"

    policy = _write_isolated_env(
        policy,
        access,
        replacements={
            "FRIDAY_TELEGRAM_BOT_TOKEN": "x:" + "a" * 46,
            "FRIDAY_TELEGRAM_BRIDGE_SECRET": "x:" + "a" * 46,
        },
    )
    with pytest.raises(rt.RoundtripError) as overlap:
        rt._validate_contour(policy, access)
    assert overlap.value.code == "credentials_must_be_distinct"


def test_contour_env_is_digest_pinned_before_process_start(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(policy, access)
    with policy.contour.env_file.open("a", encoding="utf-8") as handle:
        handle.write("FRIDAY_LOG_LEVEL=DEBUG\n")
    with pytest.raises(rt.RoundtripError) as captured:
        rt._validate_contour(policy, access)
    assert captured.value.code == "contour_env_digest_mismatch"


def test_active_lease_is_detected_without_creating_missing_path(tmp_path):
    absent = tmp_path / "missing" / "bridge.lock"
    assert rt._lease_is_active(absent) is False
    assert not absent.exists()
    lock = tmp_path / "bridge.lock"
    lock.touch(mode=0o600)
    with lock.open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rt._lease_is_active(lock) is True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    assert rt._lease_is_active(lock) is False


def test_cli_bootstrap_pins_origin_and_preserves_global_option_order(tmp_path):
    policy = _policy(tmp_path)
    receipt = tmp_path / "origin.json"
    effect_spec = tmp_path / "effects" / "spec.json"
    argv = rt.build_cli_argv(policy, "telegram-bridge", receipt, effect_spec)
    assert argv[:4] == [str(policy.candidate.python_executable), "-I", "-B", "-c"]
    assert argv[-2:] == [str(policy.contour.env_file), "telegram-bridge"]
    assert 'sys.argv = document["argv"]' in argv[4]
    assert "install_bridge_effect_guard" in argv[4]
    assert str(effect_spec) in argv
    expected = ["friday.cli", "--env-file", str(policy.contour.env_file.resolve()), "telegram-bridge"]
    document = {"argv": expected}
    assert document["argv"][1] == "--env-file"
    assert document["argv"][-1] == "telegram-bridge"
    assert "-m" not in argv


def test_isolated_bootstrap_probe_loads_pinned_guard_and_config_without_running_cli(tmp_path):
    import shutil
    import subprocess

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path / "prepared")
    source = tmp_path / "probe-source"
    (source / "friday").mkdir(parents=True)
    (source / "tools").mkdir()
    (source / "friday" / "__init__.py").write_text("")
    (source / "friday" / "cli.py").write_text('raise RuntimeError("probe must not execute friday.cli")\n')
    (source / "httpx.py").write_text(
        "class AsyncClient:\n    async def send(self, request, **kwargs):\n        return None\n"
    )
    shutil.copyfile(
        Path(rt.__file__),
        source / "tools" / "release_1_0_telegram_roundtrip.py",
    )
    receipt = tmp_path / "probe-origin.json"
    argv = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        rt._BOOTSTRAP,
        str(source),
        str(receipt),
        "telegram-bridge",
        policy.candidate.candidate_sha,
        str(spec_path),
        str(policy.contour.env_file),
        "__effect-guard-probe__",
    ]
    completed = subprocess.run(
        argv,
        cwd=tmp_path,
        env=rt._child_environment(policy),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    observed = json.loads(receipt.read_text())
    assert observed["role"] == "telegram-bridge"
    assert observed["effect_guard_origin"] == str(
        (source / "tools" / "release_1_0_telegram_roundtrip.py").resolve()
    )
    assert observed["effect_spec_sha256"] == _sha(spec_path)


def test_driver_has_no_user_impersonation_or_product_import(tmp_path):
    source_path = Path(__file__).resolve().parents[1] / "tools" / "release_1_0_telegram_roundtrip.py"
    source = source_path.read_text()
    tree = ast.parse(source)
    imports = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name == "friday" or name.startswith("friday.") for name in imports)
    assert rt.EFFECT_METHOD_CAPS["bridge"]["sendMessage"] == 2
    assert sum(sum(role.values()) for role in rt.EFFECT_METHOD_CAPS.values()) == 433
    assert "deleteWebhook" not in source
    assert "setWebhook" not in source
    assert "4dbe4a1a66353d7fd04c542f39419b207ff6bab9" not in source
    assert rt.BotApi._read.__doc__ is None
    assert {"getMe", "getWebhookInfo"} <= set(
        ast.literal_eval(
            next(
                node.comparators[0]
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "method"
                and isinstance(node.ops[0], ast.NotIn)
                and isinstance(node.comparators[0], ast.Set)
            )
        )
    )


@pytest.mark.parametrize(
    ("me", "webhook", "code"),
    [
        ({"id": 1}, {"url": "", "pending_update_count": 0}, "dedicated_bot_identity_mismatch"),
        (
            {"id": 900000001},
            {"url": "https://example.invalid/hook", "pending_update_count": 0},
            "active_webhook_refused",
        ),
        (
            {"id": 900000001},
            {"url": "", "pending_update_count": 2},
            "dedicated_bot_backlog_not_clean",
        ),
    ],
)
def test_botapi_preflight_refuses_wrong_identity_webhook_or_backlog(tmp_path, me, webhook, code):
    policy = _policy(tmp_path)
    access = _access(policy)

    class ReadOnlyStub(rt.BotApi):
        def __init__(self):
            pass

        def _read(self, method):
            return me if method == "getMe" else webhook

    with pytest.raises(rt.RoundtripError) as captured:
        ReadOnlyStub().preflight(access)
    assert captured.value.code == code
    assert captured.value.outcome == "NOT_RUN"


def test_observer_request_states_no_resend_and_real_user_identity(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    request = rt.build_observer_request(
        policy,
        access,
        {"bot_user_id": 900000001, "bot_username": "dedicated_friday"},
    )
    assert policy.canary in request["inbound_text"]
    assert request["chat_id"] == request["user_id"] == access.user_id
    assert request["unknown_effect_policy"] == "do_not_resend"
    assert request["max_inbound_user_messages"] == 1


def test_observer_result_binds_exact_visible_message(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    observed = rt.validate_observer_result(
        document,
        policy=policy,
        access=access,
        bot={"bot_user_id": 900000001},
        inbound_text=inbound_text,
        request_issued_at=OBSERVER_REQUESTED_AT,
    )
    assert observed["inbound_message_id"] == 411
    assert observed["matching_outbound_message_id"] == 812
    assert observed["window_complete"] is True


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("candidate_sha",), "f" * 40, "observer_binding_mismatch"),
        (("chat_id",), 700000002, "observer_binding_mismatch"),
        (("inbound_message", "from_user_id"), 700000002, "observer_inbound_mismatch"),
        (("inbound_message", "text"), "wrong-canary", "observer_inbound_mismatch"),
    ],
)
def test_observer_result_refuses_mixed_binding(tmp_path, path, value, code):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(rt.RoundtripError) as captured:
        rt.validate_observer_result(
            document,
            policy=policy,
            access=access,
            bot={"bot_user_id": 900000001},
            inbound_text=inbound_text,
            request_issued_at=OBSERVER_REQUESTED_AT,
        )
    assert captured.value.code == code


def test_observer_result_refuses_duplicate_canary_delivery(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access, duplicates=True)
    with pytest.raises(rt.RoundtripError) as captured:
        rt.validate_observer_result(
            document,
            policy=policy,
            access=access,
            bot={"bot_user_id": 900000001},
            inbound_text=inbound_text,
            request_issued_at=OBSERVER_REQUESTED_AT,
        )
    assert captured.value.code == "observer_canary_delivery_not_exactly_once"


def test_observer_result_without_visible_delivery_cannot_pass(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    document["outbound_messages"] = []
    with pytest.raises(rt.RoundtripError) as captured:
        rt.validate_observer_result(
            document,
            policy=policy,
            access=access,
            bot={"bot_user_id": 900000001},
            inbound_text=inbound_text,
            request_issued_at=OBSERVER_REQUESTED_AT,
        )
    assert captured.value.code == "observer_outbound_cardinality_invalid"


def test_observer_result_cannot_predate_the_attempt(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    document["inbound_message"]["sent_at"] = "2026-09-11T08:00:00+00:00"
    with pytest.raises(rt.RoundtripError) as captured:
        rt.validate_observer_result(
            document,
            policy=policy,
            access=access,
            bot={"bot_user_id": 900000001},
            inbound_text=inbound_text,
            request_issued_at=OBSERVER_REQUESTED_AT,
        )
    assert captured.value.code == "observer_inbound_predates_request"


def _build_durable_databases(policy, access, observer, *, duplicate_receipt: bool = False):
    policy.contour.database_path.parent.mkdir(parents=True, mode=0o700)
    policy.contour.inbox_db_path.parent.mkdir(parents=True, mode=0o700)
    main = sqlite3.connect(policy.contour.database_path)
    main.executescript(
        """
        CREATE TABLE raw_objects(
            id TEXT,user_id TEXT,source TEXT,source_ref TEXT,raw_content TEXT,
            metadata_json TEXT,created_at TEXT,deleted_at TEXT
        );
        CREATE TABLE user_identities(source TEXT,external_id TEXT,user_id TEXT);
        CREATE TABLE request_idempotency(user_id TEXT,request_key TEXT,state TEXT);
        CREATE TABLE messages(id TEXT,user_id TEXT,role TEXT,content TEXT);
        """
    )
    source_ref = "telegram-update:501"
    metadata = json.dumps(
        {
            "chat_id": access.chat_id,
            "telegram_message_id": observer["inbound_message_id"],
            "uploaded_by": rt.OWNER_USER_ID,
            "channel": "telegram-bridge",
        }
    )
    main.execute(
        "INSERT INTO raw_objects VALUES(?,?,?,?,?,?,?,NULL)",
        (
            "raw1",
            rt.OWNER_USER_ID,
            "telegram",
            source_ref,
            observer["inbound_text"],
            metadata,
            "2026-09-11T09:00:00Z",
        ),
    )
    main.execute(
        "INSERT INTO user_identities VALUES('telegram',?,?)",
        (str(access.user_id), rt.OWNER_USER_ID),
    )
    main.execute(
        "INSERT INTO request_idempotency VALUES(?,?,?)",
        (rt.OWNER_USER_ID, source_ref, "complete"),
    )
    main.execute(
        "INSERT INTO messages VALUES('user1',?,'user',?)",
        (rt.OWNER_USER_ID, observer["inbound_text"]),
    )
    main.execute(
        "INSERT INTO messages VALUES('answer1',?,'assistant',?)",
        (rt.OWNER_USER_ID, policy.canary),
    )
    if duplicate_receipt:
        main.execute(
            "INSERT INTO messages VALUES('answer2',?,'assistant',?)",
            (rt.OWNER_USER_ID, policy.canary),
        )
    main.commit()
    main.close()

    inbox = sqlite3.connect(policy.contour.inbox_db_path)
    inbox.executescript(
        """
        CREATE TABLE outbound_reply_context(
            chat_id INTEGER,telegram_message_id INTEGER,backend_message_id TEXT
        );
        CREATE TABLE state(key TEXT,value TEXT);
        CREATE TABLE updates(update_id INTEGER);
        """
    )
    inbox.execute(
        "INSERT INTO outbound_reply_context VALUES(?,?,?)",
        (access.chat_id, observer["matching_outbound_message_id"], "answer1"),
    )
    if duplicate_receipt:
        inbox.execute(
            "INSERT INTO outbound_reply_context VALUES(?,?,?)",
            (access.chat_id, observer["matching_outbound_message_id"] + 1, "answer2"),
        )
    inbox.execute("INSERT INTO state VALUES('offset','502')")
    inbox.commit()
    inbox.close()


def test_independent_sqlite_reader_proves_all_durable_bindings(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    observer = rt.validate_observer_result(
        document,
        policy=policy,
        access=access,
        bot={"bot_user_id": 900000001},
        inbound_text=inbound_text,
        request_issued_at=OBSERVER_REQUESTED_AT,
    )
    _build_durable_databases(policy, access, observer)
    durable = rt.inspect_durable_outcome(policy, access, observer)
    assert durable["telegram_update_id"] == 501
    assert durable["botapi_receipt_message_id"] == 812
    assert durable["visible_destination_message_id"] == 812
    assert durable["idempotency_complete"] is True
    assert durable["remaining_inbox_updates"] == 0


def test_independent_sqlite_reader_refuses_duplicate_botapi_receipt(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    document, inbound_text = _observer_document(policy, access)
    observer = rt.validate_observer_result(
        document,
        policy=policy,
        access=access,
        bot={"bot_user_id": 900000001},
        inbound_text=inbound_text,
        request_issued_at=OBSERVER_REQUESTED_AT,
    )
    _build_durable_databases(policy, access, observer, duplicate_receipt=True)
    with pytest.raises(rt.RoundtripError) as captured:
        rt.inspect_durable_outcome(policy, access, observer)
    assert captured.value.code == "botapi_receipt_not_exactly_once"


def test_case_evaluator_grants_only_narrow_live_case_credit(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    observer = {
        "inbound_text": "exact inbound",
        "matching_outbound_message_id": 812,
        "matching_canary_count": 1,
        "window_complete": True,
    }
    durable = {
        "visible_destination_message_id": 812,
        "botapi_receipt_message_id": 812,
        "backend_user_message_count": 1,
        "idempotency_complete": True,
        "raw_inbound_count": 1,
        "raw_content_sha256": hashlib.sha256(b"exact inbound").hexdigest(),
        "botapi_receipt_count": 1,
        "telegram_update_id": 501,
        "telegram_message_id": 411,
    }
    origins = [
        {"role": "server", "candidate_sha": policy.candidate.candidate_sha},
        {"role": "telegram-bridge", "candidate_sha": policy.candidate.candidate_sha},
    ]
    result = rt.evaluate_bound_roundtrip(
        policy=policy,
        access=access,
        bot={"bot_user_id": access.bot_user_id, "webhook_empty": True},
        observer=observer,
        durable=durable,
        origins=origins,
        cleanup_clear=True,
        source_unchanged=True,
    )
    assert result["case_outcome"] == "LIVE_TELEGRAM_ROUNDTRIP_OBSERVED"
    assert result["case_pass"] is True
    assert result["live_case_credit_only"] is True
    assert result["GO"] is False
    assert result["release_ready"] is False
    assert result["full_gate_credit"] is False


@pytest.mark.parametrize(
    ("target", "key", "value", "reason"),
    [
        ("durable", "raw_inbound_count", 0, "real_user_inbound_missing"),
        ("durable", "idempotency_complete", False, "signed_admission_missing"),
        ("durable", "botapi_receipt_count", 0, "botapi_receipt_missing"),
        ("observer", "matching_canary_count", 0, "independent_destination_readback_missing"),
    ],
)
def test_case_evaluator_requires_each_real_leg(tmp_path, target, key, value, reason):
    policy = _policy(tmp_path)
    access = _access(policy)
    observer = {
        "inbound_text": "exact",
        "matching_outbound_message_id": 9,
        "matching_canary_count": 1,
        "window_complete": True,
    }
    durable = {
        "visible_destination_message_id": 9,
        "botapi_receipt_message_id": 9,
        "botapi_receipt_count": 1,
        "backend_user_message_count": 1,
        "idempotency_complete": True,
        "raw_inbound_count": 1,
        "raw_content_sha256": hashlib.sha256(b"exact").hexdigest(),
    }
    {"observer": observer, "durable": durable}[target][key] = value
    result = rt.evaluate_bound_roundtrip(
        policy=policy,
        access=access,
        bot={"bot_user_id": access.bot_user_id, "webhook_empty": True},
        observer=observer,
        durable=durable,
        origins=[
            {"role": "server", "candidate_sha": policy.candidate.candidate_sha},
            {"role": "telegram-bridge", "candidate_sha": policy.candidate.candidate_sha},
        ],
        cleanup_clear=True,
        source_unchanged=True,
    )
    assert result["case_pass"] is False
    assert reason in result["reasons"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"cleanup_clear": False}, "owned_process_cleanup_incomplete"),
        ({"source_unchanged": False}, "candidate_source_changed"),
    ],
)
def test_case_evaluator_refuses_cleanup_or_source_drift(tmp_path, mutation, reason):
    policy = _policy(tmp_path)
    access = _access(policy)
    kwargs = {
        "policy": policy,
        "access": access,
        "bot": {"bot_user_id": access.bot_user_id, "webhook_empty": True},
        "observer": {
            "inbound_text": "exact",
            "matching_outbound_message_id": 9,
            "matching_canary_count": 1,
            "window_complete": True,
        },
        "durable": {
            "visible_destination_message_id": 9,
            "botapi_receipt_message_id": 9,
            "backend_user_message_count": 1,
            "idempotency_complete": True,
            "raw_inbound_count": 1,
            "raw_content_sha256": hashlib.sha256(b"exact").hexdigest(),
            "botapi_receipt_count": 1,
        },
        "origins": [
            {"role": "server", "candidate_sha": policy.candidate.candidate_sha},
            {"role": "telegram-bridge", "candidate_sha": policy.candidate.candidate_sha},
        ],
        "cleanup_clear": True,
        "source_unchanged": True,
    }
    kwargs.update(mutation)
    result = rt.evaluate_bound_roundtrip(**kwargs)
    assert result["case_pass"] is False
    assert reason in result["reasons"]
    assert result["live_case_credit_only"] is False


def test_evidence_store_is_private_no_overwrite_and_bounded(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    store = rt.EvidenceStore(parent / "evidence", 65536)
    store.event("preflight", "STARTED")
    path = store.write("one.json", {"safe": True})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        store.write("one.json", {"safe": False})
    with pytest.raises(rt.RoundtripError) as captured:
        store.write("large.json", {"body": "x" * 70000})
    assert captured.value.code == "evidence_budget_exceeded"


@pytest.mark.parametrize("failure_point", ["start_event", "capture_start"])
def test_failed_start_cleans_real_child_before_losing_ownership(tmp_path, monkeypatch, failure_point):
    """Fail real post-spawn bookkeeping; a failed start must not orphan the child."""
    import signal
    import subprocess

    policy = _policy(tmp_path)
    policy.contour.friday_home.mkdir(parents=True, mode=0o700)
    children = []
    captures = []
    original_popen = subprocess.Popen
    original_capture = rt.BoundedCapture

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)
        return process

    class Capture(original_capture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captures.append(self)

        def start(self):
            if failure_point == "capture_start":
                raise RuntimeError("injected_capture_start_failure")
            super().start()

    class Evidence:
        root = tmp_path / "evidence"

        def event(self, *args, **kwargs):
            raise rt.RoundtripError("FAIL", "evidence_budget_exceeded")

    Evidence.root.mkdir(mode=0o700)
    monkeypatch.setattr(rt.subprocess, "Popen", popen)
    monkeypatch.setattr(rt, "BoundedCapture", Capture)
    monkeypatch.setattr(
        rt,
        "build_cli_argv",
        lambda *args: [sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"],
    )
    try:
        with pytest.raises((rt.RoundtripError, RuntimeError)):
            rt.start_owned_process(policy, Evidence(), "server")
        assert len(children) == 1
        assert children[0].poll() is not None, "failed start lost ownership of a live actual child"
        assert not rt._process_group_alive(children[0].pid), "failed start retained an owned process group"
    finally:
        # Test fixture owns exactly this Popen handle; never a broad process sweep.
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        for capture in captures:
            if capture.thread.ident is not None:
                capture.finish(5)
            capture.pipe.close()


@pytest.mark.parametrize("failure_point", ["capture_start", "wait_interrupt"])
def test_adapter_start_or_wait_failure_reaps_actual_child(tmp_path, monkeypatch, failure_point):
    import signal
    import subprocess
    import time

    policy = _policy(tmp_path, mode="external_user_adapter")
    policy = dataclasses.replace(
        policy,
        observer=dataclasses.replace(
            policy.observer,
            adapter_argv=(sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"),
        ),
    )
    children = []
    captures = []
    original_popen = subprocess.Popen
    original_capture = rt.BoundedCapture

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)
        if failure_point == "wait_interrupt":
            real_wait = process.wait
            interrupted = False

            def interrupt_once(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("injected_adapter_interruption")
                return real_wait(*args, **kwargs)

            process.wait = interrupt_once
        return process

    class Capture(original_capture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captures.append(self)

        def start(self):
            if failure_point == "capture_start":
                raise RuntimeError("injected_adapter_capture_failure")
            super().start()

    monkeypatch.setattr(rt.subprocess, "Popen", popen)
    monkeypatch.setattr(rt, "BoundedCapture", Capture)
    try:
        with pytest.raises((RuntimeError, KeyboardInterrupt)):
            rt._run_adapter_once(policy, tmp_path / "request.json", time.monotonic() + 10)
        assert len(children) == 1
        assert children[0].poll() is not None, "adapter failure orphaned its real child"
        assert not rt._process_group_alive(children[0].pid)
    finally:
        for child in children:
            if child.poll() is None:
                child.send_signal(signal.SIGKILL)
            child.wait(timeout=5)
        for capture in captures:
            if capture.thread.ident is not None:
                capture.finish(5)
            if capture.pipe is not None:
                capture.pipe.close()


@pytest.mark.parametrize("key", ["FRIDAY_TELEGRAM_INBOX_DB_PATH", "JERICHO_TELEGRAM_INBOX_DB_PATH"])
@pytest.mark.parametrize("inside_home", [True, False])
def test_effective_inbox_override_cannot_escape_declared_write_target(tmp_path, key, inside_home):
    policy = _policy(tmp_path)
    access = _access(policy)
    redirected = (policy.contour.friday_home if inside_home else tmp_path) / "another-inbox.sqlite3"
    policy = _write_isolated_env(policy, access, replacements={key: str(redirected)})
    with pytest.raises(rt.RoundtripError) as caught:
        rt._validate_contour(policy, access)
    assert caught.value.code == "inbox_path_binding_mismatch"
    assert caught.value.outcome == "NOT_RUN"
    assert not redirected.exists()
    assert not policy.contour.friday_home.exists()


def test_canonical_inbox_key_takes_precedence_over_legacy_key(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(
        policy,
        access,
        replacements={
            "FRIDAY_TELEGRAM_INBOX_DB_PATH": str(policy.contour.inbox_db_path),
            "JERICHO_TELEGRAM_INBOX_DB_PATH": str(tmp_path / "unused-legacy.sqlite3"),
        },
    )
    _, observed = rt._validate_contour(policy, access)
    assert observed["inbox_db_path"] == str(policy.contour.inbox_db_path)
    assert not policy.contour.friday_home.exists()


def _prepared_effect_guard(tmp_path):
    policy = _policy(tmp_path)
    access = _access(policy)
    policy = _write_isolated_env(policy, access)
    evidence = rt.EvidenceStore(tmp_path / "effect-evidence", policy.budgets.max_evidence_bytes)
    spec_path, driver = rt.prepare_effect_ledger(
        evidence,
        policy=policy,
        access=access,
        token="900000001:dedicated-test-token",
        deadline_monotonic_ns=time.monotonic_ns() + 590_000_000_000,
    )
    return policy, access, evidence, spec_path, driver


def _run(coro):
    return asyncio.run(coro)


def test_driver_urllib_boundary_preserves_real_responses_and_ledgers_both_reads(tmp_path):
    policy, access, _evidence, spec_path, driver = _prepared_effect_guard(tmp_path)
    calls = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def read(self, _maximum):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, request, *, timeout):
            assert timeout > 0
            method = request.full_url.rsplit("/", 1)[-1]
            calls.append(method)
            if method == "getMe":
                return Response({"ok": True, "result": {"id": access.bot_user_id, "username": "friday"}})
            return Response({"ok": True, "result": {"url": "", "pending_update_count": 0}})

    bot = rt.BotApi(
        "900000001:dedicated-test-token",
        effect_guard=driver,
    )
    bot._opener = Opener()
    assert bot.preflight(access)["bot_user_id"] == access.bot_user_id
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    observed = rt.inspect_effect_ledger(
        spec_path.parent,
        policy=policy,
        access=access,
        require_complete=False,
    )
    assert calls == ["getMe", "getWebhookInfo"]
    assert observed["counts"]["driver_preflight"] == {"getMe": 1, "getWebhookInfo": 1}
    assert observed["unknown_count"] == 0


def test_bridge_mocktransport_preserves_response_and_same_attempt_receipt_for_edit(tmp_path):
    import httpx

    policy, access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    responses = {}

    async def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        response = httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 812,
                    "chat": {"id": access.chat_id},
                },
            },
            request=request,
        )
        responses[method] = response
        return response

    guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            sent = await client.post(
                "https://api.telegram.org/bot900000001:dedicated-test-token/sendMessage",
                json={"chat_id": access.chat_id, "text": "one"},
            )
            edited = await client.post(
                "https://api.telegram.org/bot900000001:dedicated-test-token/editMessageText",
                json={"chat_id": access.chat_id, "message_id": 812, "text": "two"},
            )
            return sent, edited

    try:
        sent, edited = _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert sent is responses["sendMessage"]
    assert edited is responses["editMessageText"]
    assert guard.latched == ""
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    observed = rt.inspect_effect_ledger(
        spec_path.parent,
        policy=policy,
        access=access,
        require_complete=False,
    )
    assert observed["send_message_receipt_ids"] == [812]


def test_bridge_guard_counts_concurrent_sends_atomically_before_third_dispatch(tmp_path):
    import httpx

    policy, access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        message_id = 900 + calls
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": message_id, "chat": {"id": access.chat_id}}},
            request=request,
        )

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            return await asyncio.gather(
                *[
                    client.post(
                        "https://api.telegram.org/bot900000001:dedicated-test-token/sendMessage",
                        json={"chat_id": access.chat_id, "text": str(index)},
                    )
                    for index in range(3)
                ],
                return_exceptions=True,
            )

    try:
        outcomes = _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 2
    assert sum(isinstance(item, rt.TelegramEffectStop) for item in outcomes) == 1


def test_rapid_getupdates_stops_twenty_first_before_transport(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": []}, request=request)

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            for _ in range(20):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getUpdates",
                    json={"offset": 1, "timeout": 30},
                )
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getUpdates",
                    json={"offset": 1, "timeout": 30},
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 20


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        (
            "https://api.telegram.org/bot900000001:dedicated-test-token/sendDocument",
            {"chat_id": 700000001},
        ),
        (
            "https://api.telegram.org/bot900000001:dedicated-test-token/sendMessage",
            {"chat_id": 700000002, "text": "wrong"},
        ),
        (
            "https://api.telegram.org/bot900000001:wrong-token/sendMessage",
            {"chat_id": 700000001, "text": "wrong"},
        ),
    ],
)
def test_unclassified_method_wrong_chat_and_wrong_token_never_dispatch(tmp_path, url, payload):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(url, json=payload)

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 0


def test_redirect_permission_is_refused_before_first_dispatch(tmp_path):
    import httpx

    policy, access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://example.invalid/"}, request=request)

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=True
        ) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/sendMessage",
                    json={"chat_id": access.chat_id, "text": "never"},
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 0


def test_unknown_send_effect_latches_and_cannot_retry(tmp_path):
    import httpx

    policy, access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadError("lost terminal", request=request)

    guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            for _ in range(2):
                with pytest.raises(rt.TelegramEffectStop):
                    await client.post(
                        "https://api.telegram.org/bot900000001:dedicated-test-token/sendMessage",
                        json={"chat_id": access.chat_id, "text": "once"},
                    )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 1
    assert guard.latched == "telegram_effect_unknown"
    assert list((spec_path.parent / "bridge").glob("*-unknown.json"))


def test_reserved_evidence_exhaustion_stops_before_dispatch(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)
    (spec_path.parent / "reservation.bin").write_bytes(b"")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getMe",
                    json={},
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 0


def test_oversize_request_stops_before_dispatch(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getMe",
                    content=b"x" * (rt.EFFECT_REQUEST_MAX_BYTES + 1),
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 0


def test_oversize_response_latches_before_any_later_effect(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"x" * (rt.EFFECT_RESPONSE_MAX_BYTES + 1), request=request)

    guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getMe",
                    json={},
                )
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(policy.contour.backend_origin + "/health")

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 1


def test_expired_deadline_stops_backend_and_telegram_before_dispatch(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

    guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)
    guard.spec["deadline_monotonic_ns"] = 0

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(policy.contour.backend_origin + "/health")
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/getMe",
                    json={},
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 0


def test_cancellation_after_dispatch_becomes_unknown_and_blocks_retry(tmp_path):
    import httpx

    policy, _access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    calls = 0
    entered = None

    async def handler(request):
        nonlocal calls
        calls += 1
        assert entered is not None
        entered.set()
        await asyncio.Event().wait()

    guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            task = asyncio.create_task(
                client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/sendChatAction",
                    json={"chat_id": 700000001, "action": "typing"},
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(rt.TelegramEffectStop):
                await task
            with pytest.raises(rt.TelegramEffectStop):
                await client.post(
                    "https://api.telegram.org/bot900000001:dedicated-test-token/sendChatAction",
                    json={"chat_id": 700000001, "action": "typing"},
                )

    try:
        _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert calls == 1
    assert guard.latched == "telegram_effect_unknown"


def test_getupdates_is_checked_and_recorded_before_original_response_returns(tmp_path):
    import httpx

    policy, access, _evidence, spec_path, _driver = _prepared_effect_guard(tmp_path)
    inbound = rt.build_observer_request(
        policy,
        access,
        {"bot_user_id": access.bot_user_id, "bot_username": "friday"},
    )["inbound_text"]
    actual_response = None

    def handler(request):
        nonlocal actual_response
        actual_response = httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 501,
                        "message": {
                            "message_id": 411,
                            "from": {"id": access.user_id},
                            "chat": {"id": access.chat_id},
                            "text": inbound,
                        },
                    }
                ],
            },
            request=request,
        )
        return actual_response

    _guard, original_send = rt.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            return await client.post(
                "https://api.telegram.org/bot900000001:dedicated-test-token/getUpdates",
                json={"offset": 1, "timeout": 30},
            )

    try:
        returned = _run(scenario())
    finally:
        httpx.AsyncClient.send = original_send
    assert returned is actual_response
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    observed = rt.inspect_effect_ledger(
        spec_path.parent,
        policy=policy,
        access=access,
        require_complete=False,
    )
    assert observed["telegram_update_ids"] == [501]
    assert observed["telegram_message_ids"] == [411]


def test_independent_effect_reader_reconciles_complete_four_leg_ledger(tmp_path):
    policy, access, _evidence, spec_path, driver = _prepared_effect_guard(tmp_path)
    token = "900000001:dedicated-test-token"
    for method, result in (
        ("getMe", {"id": access.bot_user_id}),
        ("getWebhookInfo", {"url": "", "pending_update_count": 0}),
    ):
        sequence = driver.begin(method, "GET", b"")
        driver.finish(sequence, 200, json.dumps({"ok": True, "result": result}).encode())
    bridge = rt.TelegramEffectLedger(spec_path.parent, "bridge", token)
    sequence = bridge.begin("getMe", "POST", b"{}")
    bridge.finish(
        sequence,
        200,
        json.dumps({"ok": True, "result": {"id": access.bot_user_id}}).encode(),
    )
    commands = json.dumps({"commands": [{"command": "help", "description": "Help"}]}).encode()
    sequence = bridge.begin("setMyCommands", "POST", commands)
    bridge.finish(sequence, 200, b'{"ok":true,"result":true}')
    inbound_text = rt.build_observer_request(
        policy,
        access,
        {"bot_user_id": access.bot_user_id, "bot_username": "friday"},
    )["inbound_text"]
    update_body = json.dumps(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 501,
                    "message": {
                        "message_id": 411,
                        "from": {"id": access.user_id},
                        "chat": {"id": access.chat_id},
                        "text": inbound_text,
                    },
                }
            ],
        }
    ).encode()
    sequence = bridge.begin("getUpdates", "POST", b'{"offset":1,"timeout":30}')
    bridge.finish(sequence, 200, update_body)
    send_body = json.dumps({"chat_id": access.chat_id, "text": policy.canary}).encode()
    sequence = bridge.begin("sendMessage", "POST", send_body)
    bridge.finish(
        sequence,
        200,
        json.dumps(
            {
                "ok": True,
                "result": {"message_id": 812, "chat": {"id": access.chat_id}},
            }
        ).encode(),
    )
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    observed = rt.inspect_effect_ledger(
        spec_path.parent,
        policy=policy,
        access=access,
        observer={"matching_outbound_message_id": 812},
        durable={
            "botapi_receipt_message_id": 812,
            "visible_destination_message_id": 812,
            "telegram_update_id": 501,
            "telegram_message_id": 411,
        },
        require_complete=True,
    )
    assert observed["attempts"] == 6
    assert observed["send_message_receipt_ids"] == [812]


def test_independent_effect_reader_refuses_unfinished_record(tmp_path):
    policy, access, _evidence, spec_path, driver = _prepared_effect_guard(tmp_path)
    driver.begin("getMe", "GET", b"")
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    with pytest.raises(rt.RoundtripError, match="telegram_effect_ledger_incomplete"):
        rt.inspect_effect_ledger(
            spec_path.parent,
            policy=policy,
            access=access,
            observer={},
            durable={},
            require_complete=True,
        )


def test_independent_effect_reader_refuses_tampered_record(tmp_path):
    policy, access, _evidence, spec_path, driver = _prepared_effect_guard(tmp_path)
    sequence = driver.begin("getMe", "GET", b"")
    driver.finish(
        sequence,
        200,
        json.dumps({"ok": True, "result": {"id": access.bot_user_id}}).encode(),
    )
    rt.seal_effect_ledger(spec_path.parent, policy.contour.env_file)
    terminal = spec_path.parent / "driver_preflight" / "000001-terminal.json"
    document = json.loads(terminal.read_text())
    document["status"] = 201
    terminal.write_text(json.dumps(document))
    terminal.chmod(0o600)
    with pytest.raises(rt.RoundtripError, match="telegram_effect_ledger_seal_invalid"):
        rt.inspect_effect_ledger(
            spec_path.parent,
            policy=policy,
            access=access,
            require_complete=False,
        )
