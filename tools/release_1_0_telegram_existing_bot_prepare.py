#!/usr/bin/env python3
"""Prepare and own one explicit existing-Friday-bot Telegram handoff.

Preparation is offline.  The ``execute`` subcommand is deliberately gated by an
exact owner confirmation and performs one systemd stop, one finite driver run,
and one systemd restore.  This module never fabricates a Telegram receipt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "friday.telegram-existing-bot-preparation-plan.v1"
IDENTITY_SCHEMA = "friday.telegram-existing-bot-candidate-identity.v1"
PREPARATION_SCHEMA = "friday.telegram-existing-bot-preparation.v1"
EXECUTE_CONFIRMATION = "OWNER_CONFIRMS_ONE_LIVE_CANARY_AND_BOUNDED_HANDOFF"
MAX_JSON_BYTES = 2 << 20
ROOT = Path(__file__).resolve().parents[1]
SERVICE_UNIT = "friday-bridge.service"
SERVICE_ACTIONS = frozenset({"is-active", "stop", "start"})
SERVICE_TIMEOUT_S = 60

# Python -I omits the script's parent package directory. The receipt reader
# imports sibling tools lazily; append the verified controller's own root,
# leaving installed-product imports ahead of source code in the search path.
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


telegram = _load_module(
    "friday_release_1_0_telegram_roundtrip_prepare",
    ROOT / "tools/release_1_0_telegram_roundtrip.py",
)
receipts = _load_module(
    "friday_release_1_0_telegram_receipts_prepare",
    ROOT / "tools/release_1_0_telegram_receipts.py",
)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(path: Path, maximum: int = MAX_JSON_BYTES) -> str:
    return telegram.sha256_file(path, maximum=maximum)


def _exact(value: object, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{where} shape invalid")
    return dict(value)


def _private_json(path: Path, digest: str | None = None) -> dict[str, Any]:
    document = telegram._load_json(path, path.name, maximum=MAX_JSON_BYTES, private=True)
    if digest is not None and _sha256(path) != digest:
        raise ValueError(f"{path.name} digest mismatch")
    return document


def _reference(path: Path, *, sized: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "sha256": _sha256(path)}
    if sized:
        result["size_bytes"] = path.stat().st_size
    return result


def _private_parent(path: Path) -> None:
    parent = path.parent.resolve(strict=True)
    telegram._secure_directory(parent, f"parent of {path.name}")
    if path.exists() or path.is_symlink():
        raise ValueError(f"{path.name} must be new")


def _write_new(path: Path, value: object) -> dict[str, Any]:
    _private_parent(path)
    telegram._write_new_private(path, value)
    telegram._fsync_directory(path.parent)
    return _reference(path)


def _path(value: object, where: str) -> Path:
    return telegram._absolute_path(value, where)


def _bool_env(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("feature flag is not boolean")


def _installed_startup_commands(policy: Any) -> tuple[dict[str, Any], str]:
    installed = policy.candidate.installed_wheel
    if installed is None:
        raise ValueError("installed wheel policy missing")
    env = telegram._parse_env_file(policy.contour.env_file)
    code = r"""import importlib.util,json,sys
from pathlib import Path
from friday.telegram_bridge._base import BOT_COMMANDS
site=Path(sys.argv[1]).resolve(strict=True)
origin=Path(importlib.util.find_spec("friday.telegram_bridge._base").origin).resolve(strict=True)
if site not in origin.parents:
    raise SystemExit("installed command producer origin mismatch")
hidden=set()
if sys.argv[2] != "1": hidden.update({"obsidian","obsidian_alias"})
if sys.argv[3] != "1": hidden.add("engineer")
payload={"commands":[{"command":name,"description":desc} for name,desc in BOT_COMMANDS if name not in hidden]}
print(json.dumps({"origin":str(origin),"payload":payload},ensure_ascii=False,sort_keys=True,separators=(",",":")))"""
    command = [
        str(policy.candidate.python_executable),
        "-I",
        "-B",
        "-c",
        code,
        str(installed.site_root),
        "1" if _bool_env(env.get("FRIDAY_OBSIDIAN_ENABLED", "0")) else "0",
        "1" if _bool_env(env.get("FRIDAY_ENGINEER_MODE_ENABLED", "0")) else "0",
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        env={"HOME": str(policy.contour.isolation_root), "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 65536:
        raise ValueError("installed command producer failed")
    try:
        result = _exact(
            json.loads(completed.stdout.decode("utf-8")),
            {"origin", "payload"},
            "installed command producer",
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("installed command producer output invalid") from exc
    payload = telegram._validated_startup_commands_payload(result["payload"])
    expected_origin = (installed.site_root / "friday/telegram_bridge/_base.py").resolve(strict=True)
    if Path(result["origin"]).resolve(strict=True) != expected_origin:
        raise ValueError("installed command producer origin mismatch")
    return payload, telegram.canonical_startup_commands_sha256(payload)


def _identity(document: object) -> dict[str, Any]:
    value = _exact(
        document,
        {
            "schema",
            "candidate_sha",
            "candidate_tree",
            "base_sha",
            "wheel_sha256",
            "inventory_sha256",
            "suite_revision",
            "canonical_status",
            "GO",
        },
        "identity",
    )
    if (
        value["schema"] != IDENTITY_SCHEMA
        or any(
            telegram.GIT_SHA_RE.fullmatch(str(value[key])) is None
            for key in ("candidate_sha", "candidate_tree", "base_sha")
        )
        or any(
            telegram.SHA256_RE.fullmatch(str(value[key])) is None
            for key in ("wheel_sha256", "inventory_sha256")
        )
        or not isinstance(value["suite_revision"], str)
        or value["canonical_status"] != "PRIVATE_DUAL_ROOT_HARNESS_REQUIRES_INDEPENDENT_ADOPTION"
        or value["GO"] is not False
    ):
        raise ValueError("identity binding invalid")
    return value


def _plan(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = _private_json(path)
    plan = _exact(
        raw,
        {
            "schema",
            "identity",
            "candidate",
            "harness",
            "effect_admission",
            "contour",
            "owner_authority",
            "bot_user_id",
            "observer",
            "budgets",
            "handoff_plan",
            "outputs",
            "GO",
        },
        "preparation plan",
    )
    if plan["schema"] != PLAN_SCHEMA or plan["GO"] is not False:
        raise ValueError("preparation plan schema invalid")
    identity_ref = _exact(plan["identity"], {"path", "sha256"}, "identity reference")
    identity = _identity(_private_json(_path(identity_ref["path"], "identity.path"), identity_ref["sha256"]))
    return plan, identity


def _policy_document(plan: Mapping[str, Any], *, access_path: Path, access_sha256: str) -> dict[str, Any]:
    return {
        "schema": telegram.POLICY_SCHEMA_V2,
        "attempt_id": plan["attempt_id"] if "attempt_id" in plan else plan["candidate"]["attempt_id"],
        "canary": plan["canary"] if "canary" in plan else plan["candidate"]["canary"],
        "candidate": plan["candidate"]["policy"],
        "harness": plan["harness"],
        "effect_admission": plan["effect_admission"],
        "contour": plan["contour"],
        "access": {"manifest_path": str(access_path), "manifest_sha256": access_sha256},
        "observer": plan["observer"],
        "budgets": plan["budgets"],
    }


def prepare(plan_path: Path) -> dict[str, Any]:
    plan, identity = _plan(plan_path)
    candidate_envelope = _exact(plan["candidate"], {"attempt_id", "canary", "policy"}, "candidate envelope")
    outputs = _exact(
        plan["outputs"],
        {
            "access_path",
            "policy_path",
            "startup_commands_path",
            "context_path",
            "preparation_path",
            "output_root",
        },
        "outputs",
    )
    resolved_outputs = {key: _path(value, f"outputs.{key}") for key, value in outputs.items()}
    if len(set(resolved_outputs.values())) != len(resolved_outputs):
        raise ValueError("output paths alias")
    output_root = resolved_outputs["output_root"]
    candidate_policy = _exact(
        candidate_envelope["policy"],
        {
            "manifest_path",
            "manifest_sha256",
            "source_root",
            "candidate_sha",
            "candidate_tree",
            "suite_revision",
            "python_executable",
            "python_sha256",
            "installed_wheel",
        },
        "candidate policy envelope",
    )
    installed_policy = _exact(
        candidate_policy["installed_wheel"],
        {"wheel_path", "wheel_sha256", "site_root", "distribution_version"},
        "installed wheel envelope",
    )
    harness_policy = _exact(
        plan["harness"],
        {"manifest_path", "manifest_sha256", "source_root"},
        "harness envelope",
    )
    contour_policy = telegram._mapping(plan["contour"], "contour envelope")
    production_homes = contour_policy.get("known_production_homes")
    if not isinstance(production_homes, list):
        raise ValueError("contour known production homes invalid")
    protected_roots = tuple(
        path.resolve()
        for path in (
            _path(candidate_policy["source_root"], "candidate.source_root"),
            _path(harness_policy["source_root"], "harness.source_root"),
            _path(installed_policy["site_root"], "installed_wheel.site_root"),
            *(_path(item, f"known_production_homes[{index}]") for index, item in enumerate(production_homes)),
        )
    )
    for name, path in resolved_outputs.items():
        resolved = path.resolve()
        if any(
            telegram._is_within(resolved, protected)
            or (name == "output_root" and telegram._is_within(protected, resolved))
            for protected in protected_roots
        ):
            raise ValueError(f"outputs.{name} overlaps a protected source, install, or production root")
    if any(
        telegram._is_within(path.resolve(), output_root.resolve())
        for name, path in resolved_outputs.items()
        if name != "output_root"
    ):
        raise ValueError("control outputs must remain outside the attempt output root")
    if output_root.exists() or output_root.is_symlink():
        raise ValueError("output root must be new")
    telegram._secure_directory(output_root.parent.resolve(strict=True), "output root parent")
    os.mkdir(output_root, 0o700)
    telegram._fsync_directory(output_root.parent)
    isolation_root = _path(plan["contour"]["isolation_root"], "contour.isolation_root")
    if isolation_root != output_root / "contour":
        raise ValueError("isolation root must be output_root/contour")
    os.mkdir(isolation_root, 0o700)
    observer_result = _path(plan["observer"]["result_path"], "observer.result_path")
    if observer_result.parent != output_root or observer_result.exists():
        raise ValueError("observer result must be a new direct output child")

    access_path = resolved_outputs["access_path"]
    provisional = _policy_document(plan, access_path=access_path, access_sha256="0" * 64)
    provisional["attempt_id"] = candidate_envelope["attempt_id"]
    provisional["canary"] = candidate_envelope["canary"]
    policy = telegram.Policy.from_document(provisional)
    startup_payload, startup_sha256 = _installed_startup_commands(policy)
    startup_ref = _write_new(resolved_outputs["startup_commands_path"], startup_payload)
    if startup_ref["sha256"] != startup_sha256:
        raise ValueError("startup command canonical file digest mismatch")

    authority_ref = _exact(plan["owner_authority"], {"path", "sha256"}, "owner authority reference")
    authority_path = _path(authority_ref["path"], "owner_authority.path")
    owner_id, owner_binding = telegram._resolved_owner_private_chat(
        policy,
        authority_path=authority_path,
        authority_sha256=authority_ref["sha256"],
    )
    del owner_id
    now = dt.datetime.now(dt.UTC)
    access_document = {
        "schema": telegram.EXISTING_BOT_ACCESS_SCHEMA,
        "contour_id": policy.contour.contour_id,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "existing_friday_bot_one_canary": True,
        "same_bot_production_consumer_stopped": True,
        "isolated_non_production_home": True,
        "installed_wheel_origin": True,
        "clean_backlog_expected": True,
        "bot_user_id": plan["bot_user_id"],
        "observer_mode": policy.observer.mode,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "nonce": hashlib.sha256(_json_bytes(plan) + os.urandom(32)).hexdigest(),
        "owner_identity_source": "pinned_existing_friday_configuration",
        "owner_authority_path": str(authority_path),
        "owner_authority_sha256": authority_ref["sha256"],
        "owner_private_chat_hmac_sha256": owner_binding,
        "startup_commands_path": startup_ref["path"],
        "startup_commands_sha256": startup_sha256,
    }
    if "chat_id" in access_document or "user_id" in access_document:
        raise ValueError("raw owner identity escaped into access document")
    access_ref = _write_new(access_path, access_document)
    policy_document = _policy_document(plan, access_path=access_path, access_sha256=access_ref["sha256"])
    policy_document["attempt_id"] = candidate_envelope["attempt_id"]
    policy_document["canary"] = candidate_envelope["canary"]
    policy_ref = _write_new(resolved_outputs["policy_path"], policy_document)
    policy = telegram.load_policy(Path(policy_ref["path"]))
    admission = telegram._verified_effect_admission(policy)
    access = telegram.AccessManifest.from_document(access_document, policy=policy, now=now)
    source = telegram._verified_source_map(policy.candidate, policy.harness)
    if (
        identity["candidate_sha"] != policy.candidate.candidate_sha
        or identity["candidate_tree"] != policy.candidate.candidate_tree
        or identity["wheel_sha256"] != policy.candidate.installed_wheel.wheel_sha256
        or identity["suite_revision"] != policy.candidate.suite_revision
    ):
        raise ValueError("candidate identity and policy mismatch")
    handoff_ref = _exact(plan["handoff_plan"], {"path", "sha256"}, "handoff plan reference")
    handoff_document = _private_json(_path(handoff_ref["path"], "handoff_plan.path"), handoff_ref["sha256"])
    receipts._validate_handoff_plan(handoff_document, identity=identity)
    driver_path = ROOT / telegram.MODULE_RELATIVE_PATH
    controller_path = Path(__file__).resolve()
    effect_admission_ref = {
        "path": str(policy.effect_admission.manifest_path),
        "sha256": policy.effect_admission.manifest_sha256,
    }
    context = {
        "schema": receipts.CONTEXT_SCHEMA_V2,
        "case_id": receipts.CASE_ID,
        "attempt_id": policy.attempt_id,
        "output_root": str(output_root),
        "evidence_root": str(output_root / "evidence"),
        "candidate_identity": {
            "candidate_sha": identity["candidate_sha"],
            "candidate_tree": identity["candidate_tree"],
            "base_sha": identity["base_sha"],
            "wheel_sha256": identity["wheel_sha256"],
            "inventory_sha256": identity["inventory_sha256"],
            "source_map_sha256": source["source_map_sha256"],
            "source_root": str(policy.candidate.source_root.resolve()),
            "suite_revision": identity["suite_revision"],
        },
        "harness_identity": {
            "manifest_path": str(policy.harness.manifest_path),
            "manifest_sha256": source["harness"]["manifest_sha256"],
            "source_root": str(policy.harness.source_root.resolve()),
            "source_map_sha256": source["harness"]["source_map_sha256"],
            "source_file_count": source["harness"]["source_file_count"],
            "driver_sha256": source["harness"]["driver_sha256"],
        },
        "policy": policy_ref,
        "access": access_ref,
        "controller": _reference(controller_path),
        "driver": _reference(driver_path),
        "effect_admission": effect_admission_ref,
        "handoff_plan": handoff_ref,
        "expected": {
            "contour_id": policy.contour.contour_id,
            "bot_user_id": access.bot_user_id,
            "observer_mode": access.observer_mode,
            "bot_token_sha256": hashlib.sha256(
                telegram._parse_env_file(policy.contour.env_file)["FRIDAY_TELEGRAM_BOT_TOKEN"].encode("utf-8")
            ).hexdigest(),
            "env_sha256": policy.contour.env_file_sha256,
            "access_route": access.route,
            "owner_private_chat_hmac_sha256": access.owner_private_chat_hmac_sha256,
            "owner_authority_sha256": access.owner_authority_sha256,
            "startup_commands_sha256": access.startup_commands_sha256,
            "effect_admission_sha256": admission["manifest_sha256"],
            "installed_wheel_sha256": policy.candidate.installed_wheel.wheel_sha256,
        },
        "budgets": receipts.FROZEN_BUDGETS,
    }
    if any(key in context["expected"] for key in ("chat_id", "user_id")):
        raise ValueError("raw owner identity escaped into context")
    context_ref = _write_new(resolved_outputs["context_path"], context)
    preparation = {
        "schema": PREPARATION_SCHEMA,
        "plan": _reference(plan_path),
        "identity": plan["identity"],
        "policy": policy_ref,
        "access": access_ref,
        "startup_commands": startup_ref,
        "effect_admission": plan["effect_admission"],
        "context": context_ref,
        "handoff_plan": handoff_ref,
        "output_root": str(output_root),
        "evidence_root": str(output_root / "evidence"),
        "execution": "NOT_RUN",
        "canonical_status": identity["canonical_status"],
        "GO": False,
    }
    preparation_ref = _write_new(resolved_outputs["preparation_path"], preparation)
    return {
        "schema": PREPARATION_SCHEMA,
        "preparation": preparation_ref,
        "context": context_ref,
        "execution": "NOT_RUN",
        "GO": False,
    }


def _service_environment(*, runtime_root: Path = Path("/run/user")) -> dict[str, str]:
    owner_uid = os.getuid()
    if owner_uid != os.geteuid():
        raise ValueError("service owner uid mismatch")
    if not runtime_root.is_absolute():
        raise ValueError("unsafe service owner runtime directory")
    runtime = runtime_root / str(owner_uid)
    try:
        info = runtime.lstat()
        resolved_runtime = runtime.resolve(strict=True)
    except OSError as exc:
        raise ValueError("unsafe service owner runtime directory") from exc
    if (
        resolved_runtime != runtime
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe service owner runtime directory")
    bus = runtime / "bus"
    try:
        info = bus.lstat()
        resolved_bus = bus.resolve(strict=True)
    except OSError as exc:
        raise ValueError("unsafe service owner bus") from exc
    if resolved_bus != bus or not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner_uid:
        raise ValueError("unsafe service owner bus")
    try:
        account = pwd.getpwuid(owner_uid)
        home = Path(account.pw_dir)
        account_uid = account.pw_uid
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("unsafe service owner home") from exc
    if account_uid != owner_uid or not home.is_absolute():
        raise ValueError("unsafe service owner home")
    try:
        home_info = home.lstat()
        resolved_home = home.resolve(strict=True)
    except OSError as exc:
        raise ValueError("unsafe service owner home") from exc
    if (
        resolved_home != home
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != owner_uid
        or home_info.st_mode & 0o022
    ):
        raise ValueError("unsafe service owner home")
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
    }


def _service(systemctl: Path, unit: str, action: str) -> subprocess.CompletedProcess[bytes]:
    if unit != SERVICE_UNIT or action not in SERVICE_ACTIONS:
        raise ValueError("service action outside the fixed handoff")
    return subprocess.run(
        [str(systemctl), "--user", action, unit],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=SERVICE_TIMEOUT_S,
        env=_service_environment(),
    )


def _service_state(systemctl: Path, unit: str) -> str:
    result = _service(systemctl, unit, "is-active")
    value = result.stdout.decode("utf-8", "replace").strip()
    return "active" if result.returncode == 0 and value == "active" else "inactive"


def _process_group_clear(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _terminate_process_group(pid: int, grace_s: float) -> bool:
    if _process_group_clear(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if _process_group_clear(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _process_group_clear(pid):
            return True
        time.sleep(0.05)
    return _process_group_clear(pid)


def _evidence_inventory(root: Path) -> tuple[dict[str, Any], str, int]:
    files: dict[str, Any] = {}
    normalized: dict[str, Any] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        reference = _reference(path, sized=True)
        name = path.relative_to(root).as_posix()
        files[name] = reference
        normalized[name] = {
            "sha256": reference["sha256"],
            "size_bytes": reference["size_bytes"],
        }
        total += reference["size_bytes"]
    return files, receipts._digest(normalized), total


def execute(preparation_path: Path, preparation_sha256: str, confirmation: str) -> dict[str, Any]:
    if confirmation != EXECUTE_CONFIRMATION:
        raise ValueError("exact owner live-handoff confirmation missing")
    preparation = _exact(
        _private_json(preparation_path, preparation_sha256),
        {
            "schema",
            "plan",
            "identity",
            "policy",
            "access",
            "startup_commands",
            "effect_admission",
            "context",
            "handoff_plan",
            "output_root",
            "evidence_root",
            "execution",
            "canonical_status",
            "GO",
        },
        "preparation",
    )
    if (
        preparation["schema"] != PREPARATION_SCHEMA
        or preparation["execution"] != "NOT_RUN"
        or preparation["GO"] is not False
    ):
        raise ValueError("preparation not executable")
    identity = _identity(
        _private_json(
            _path(preparation["identity"]["path"], "identity.path"),
            preparation["identity"]["sha256"],
        )
    )
    policy_path = _path(preparation["policy"]["path"], "policy.path")
    policy = telegram.load_policy(policy_path)
    if policy.effect_admission is None or preparation["effect_admission"] != {
        "manifest_path": str(policy.effect_admission.manifest_path),
        "manifest_sha256": policy.effect_admission.manifest_sha256,
    }:
        raise ValueError("preparation effect-admission binding mismatch")
    telegram._verified_effect_admission(policy)
    source_before = telegram._verified_source_map(policy.candidate, policy.harness)
    access_document = _private_json(
        _path(preparation["access"]["path"], "access.path"),
        preparation["access"]["sha256"],
    )
    access = telegram.AccessManifest.from_document(
        access_document,
        policy=policy,
        now=dt.datetime.now(dt.UTC),
    )
    telegram._validate_contour(policy, access)
    context_path = _path(preparation["context"]["path"], "context.path")
    context_before = receipts.read_context(context_path, preparation["context"]["sha256"])
    handoff_plan_path = _path(preparation["handoff_plan"]["path"], "handoff_plan.path")
    handoff_plan = receipts._validate_handoff_plan(
        _private_json(handoff_plan_path, preparation["handoff_plan"]["sha256"]),
        identity=identity,
    )
    systemctl = _path(handoff_plan["systemctl_path"], "handoff.systemctl_path").resolve(strict=True)
    info = systemctl.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or systemctl.is_symlink()
        or not os.access(systemctl, os.X_OK)
        or _sha256(systemctl, 64 << 20) != handoff_plan["systemctl_sha256"]
    ):
        raise ValueError("systemctl identity mismatch")
    unit = handoff_plan["unit_name"]
    output_root = _path(preparation["output_root"], "output_root").resolve(strict=True)
    evidence_root = _path(preparation["evidence_root"], "evidence_root")
    if (
        evidence_root != output_root / "evidence"
        or evidence_root.exists()
        or context_before["output_root"] != str(output_root)
        or context_before["evidence_root"] != str(evidence_root)
    ):
        raise ValueError("evidence root is not new")
    lock_path = _path(handoff_plan["exclusive_slot_lock"], "exclusive_slot_lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise ValueError("live Telegram slot is busy") from exc

    started_at = dt.datetime.now(dt.UTC)
    stopped_at = started_at
    restored_at = started_at
    stop_count = restore_count = 0
    stop_returncode = restore_returncode = -1
    try:
        initial_state = _service_state(systemctl, unit)
    except (OSError, subprocess.SubprocessError):
        os.close(lock_fd)
        raise
    post_stop_state = post_restore_state = "unknown"
    lease_path = _path(handoff_plan["production_lease_path"], "production_lease_path")
    lease_inactive = lease_active = False
    child: subprocess.Popen[bytes] | None = None
    child_stdout = b""
    child_stderr = b""
    timed_out = False
    child_group_clear_before_restore = True
    if initial_state != "active":
        os.close(lock_fd)
        raise ValueError("production bridge initial state is not active")
    pending_error: BaseException | None = None
    try:
        try:
            stop_count = 1
            stopped = _service(systemctl, unit, "stop")
            stop_returncode = stopped.returncode
            stopped_at = dt.datetime.now(dt.UTC)
            post_stop_state = _service_state(systemctl, unit)
            lease_inactive = not lease_path.exists() or not telegram._lease_is_active(lease_path)
            if stop_returncode != 0 or post_stop_state != "inactive" or not lease_inactive:
                raise ValueError("sole-consumer stop proof failed")
            driver = ROOT / telegram.MODULE_RELATIVE_PATH
            child_argv = [
                str(policy.candidate.python_executable),
                "-I",
                "-B",
                str(driver),
                "--policy",
                str(policy_path),
                "--evidence-dir",
                str(evidence_root),
            ]
            child = subprocess.Popen(
                child_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env={"HOME": str(policy.contour.isolation_root), "PATH": "/usr/bin:/bin"},
            )
            try:
                child_stdout, child_stderr = child.communicate(
                    timeout=policy.budgets.timeout_s + 2 * policy.budgets.cleanup_grace_s + 5
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child_stdout, child_stderr = child.communicate(timeout=policy.budgets.cleanup_grace_s)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child_stdout, child_stderr = child.communicate(timeout=5)
        except BaseException as exc:
            pending_error = exc
    finally:
        try:
            if child is not None:
                child_group_clear_before_restore = _terminate_process_group(
                    child.pid,
                    policy.budgets.cleanup_grace_s,
                )
                if not child_group_clear_before_restore and pending_error is None:
                    pending_error = ValueError("isolated process group survived bounded cleanup")
            restore_count = 1
            try:
                restored = _service(systemctl, unit, "start")
                restore_returncode = restored.returncode
            except (OSError, subprocess.SubprocessError) as exc:
                if pending_error is None:
                    pending_error = exc
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    post_restore_state = _service_state(systemctl, unit)
                except (OSError, subprocess.SubprocessError) as exc:
                    if pending_error is None:
                        pending_error = exc
                    break
                lease_active = lease_path.exists() and telegram._lease_is_active(lease_path)
                if post_restore_state == "active" and lease_active:
                    break
                time.sleep(0.1)
            restored_at = dt.datetime.now(dt.UTC)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    handoff_receipt = {
        "schema": receipts.HANDOFF_RECEIPT_SCHEMA,
        "plan": preparation["handoff_plan"],
        "unit_name": unit,
        "systemctl_sha256": handoff_plan["systemctl_sha256"],
        "initial_state": initial_state,
        "stop_count": stop_count,
        "stop_returncode": stop_returncode,
        "post_stop_state": post_stop_state,
        "production_lease_inactive": lease_inactive,
        "driver_started_after_stop": child is not None,
        "isolated_process_group_clear_before_restore": child_group_clear_before_restore,
        "restore_count": restore_count,
        "restore_returncode": restore_returncode,
        "post_restore_state": post_restore_state,
        "production_lease_active": lease_active,
        "no_second_consumer": True,
        "production_home_as_scratch": False,
        "history_touched": False,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "stopped_at": stopped_at.isoformat().replace("+00:00", "Z"),
        "restored_at": restored_at.isoformat().replace("+00:00", "Z"),
        "GO": False,
    }
    handoff_path = output_root / "handoff-receipt.json"
    handoff_ref = _write_new(handoff_path, handoff_receipt)
    handoff_sized = {**handoff_ref, "size_bytes": handoff_path.stat().st_size}
    if pending_error is not None:
        raise ValueError("live attempt failed; bounded restoration receipt retained") from pending_error
    if (
        child is None
        or child.returncode != 0
        or timed_out
        or not child_group_clear_before_restore
        or restore_returncode != 0
        or post_restore_state != "active"
        or not lease_active
        or source_before != telegram._verified_source_map(policy.candidate, policy.harness)
    ):
        raise ValueError("live attempt or bounded restoration did not complete")
    if len(child_stdout) > 65536 or len(child_stderr) > 65536:
        raise ValueError("controller output exceeded bound")
    context = receipts.read_context(context_path, preparation["context"]["sha256"])
    if context != context_before:
        raise ValueError("context changed during the owned attempt")
    files, inventory_sha256, total_bytes = _evidence_inventory(evidence_root)
    observer_path = policy.observer.result_path
    main_path = policy.contour.database_path
    inbox_path = policy.contour.inbox_db_path
    for required in (observer_path, main_path, inbox_path):
        telegram._secure_regular_file(required, required.name, maximum=64 << 20)
    controller = {
        "executable": context["controller"],
        "pid": os.getpid(),
        "argv": child_argv,
        "driver_sha256": context["driver"]["sha256"],
        "policy_sha256": preparation["policy"]["sha256"],
        "evidence_root": str(evidence_root),
        "outcome": "completed",
        "leader_returncode": child.returncode,
        "timed_out": False,
        "error_code": None,
        "start_count": 1,
        "terminal_count": 1,
        "unknown_count": 0,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "ended_at": restored_at.isoformat().replace("+00:00", "Z"),
        "process_group_clear": True,
        "leader_reaped": True,
        "descendants_clear": True,
        "capture_drained": True,
        "stop_unconfirmed_emitted": False,
    }
    receipt = {
        "schema": receipts.RECEIPT_SCHEMA_V2,
        "case_id": receipts.CASE_ID,
        "attempt_id": policy.attempt_id,
        "attempt": 1,
        "expected_context": preparation["context"],
        "policy": preparation["policy"],
        "access": preparation["access"],
        "evidence": {
            "root": str(evidence_root),
            "files": files,
            "inventory_sha256": inventory_sha256,
            "total_bytes": total_bytes,
        },
        "observer_result": {**_reference(observer_path), "size_bytes": observer_path.stat().st_size},
        "databases": {
            "main": {**_reference(main_path), "size_bytes": main_path.stat().st_size},
            "inbox": {**_reference(inbox_path), "size_bytes": inbox_path.stat().st_size},
        },
        "controller": controller,
        "handoff_receipt": handoff_sized,
        "GO": False,
        "release_ready": False,
        "full_gate_credit": False,
    }
    result_path = output_root / "result.json"
    result_ref = _write_new(result_path, receipt)
    return {
        "schema": receipts.RECEIPT_SCHEMA_V2,
        "result": result_ref,
        "case_id": receipts.CASE_ID,
        "execution": "OBSERVED_NOT_RELEASE_ADMISSION",
        "GO": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--plan", required=True, type=Path)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--preparation", required=True, type=Path)
    execute_parser.add_argument("--preparation-sha256", required=True)
    execute_parser.add_argument("--confirmation", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.plan.resolve(strict=True))
        else:
            result = execute(
                args.preparation.resolve(strict=True),
                args.preparation_sha256,
                args.confirmation,
            )
    except (OSError, ValueError, subprocess.SubprocessError, telegram.RoundtripError) as exc:
        print(
            json.dumps(
                {
                    "schema": PREPARATION_SCHEMA,
                    "status": "REFUSED",
                    "reason": type(exc).__name__,
                    "execution": "NOT_RUN_OR_NOT_CREDITED",
                    "GO": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
