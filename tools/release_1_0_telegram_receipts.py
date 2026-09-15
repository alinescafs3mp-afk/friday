"""Independently read one bound live-Telegram attempt without dispatching it.

The reader has no network path and never starts Friday.  A PASS is reconstructed
from the original observer bytes, the two durable databases, the effect ledger,
the candidate source map, and both inner and outer process-cleanup evidence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CASE_ID = "R10-LIVE-TELEGRAM-ROUNDTRIP"
CONTEXT_SCHEMA = "friday.telegram-roundtrip-expected-context.v1"
CONTEXT_SCHEMA_V2 = "friday.telegram-roundtrip-expected-context.v2"
RECEIPT_SCHEMA = "friday.telegram-roundtrip-owned-attempt.v1"
RECEIPT_SCHEMA_V2 = "friday.telegram-roundtrip-owned-attempt.v2"
HANDOFF_PLAN_SCHEMA = "friday.telegram-existing-bot-handoff-plan.v1"
HANDOFF_RECEIPT_SCHEMA = "friday.telegram-existing-bot-handoff-receipt.v1"
EFFECT_ADMISSION_SCHEMA = "friday.release-1-0-telegram-existing-bot-effect-admission.v1"
ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
ATTEMPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,79}")
MAX_CONTEXT_BYTES = 128 << 10
MAX_RECEIPT_BYTES = 2 << 20
MAX_DATABASE_BYTES = 64 << 20
MAX_CONTROLLER_BYTES = 2 << 20

FROZEN_BUDGETS: dict[str, Any] = {
    "duration_s": 600,
    "total_attempt_cap": 433,
    "max_inbound_user_messages": 1,
    "request_max_bytes": 65536,
    "response_max_bytes": 1048576,
    "evidence_max_bytes": 1048576,
    "ledger_reservation_bytes": 524288,
    "method_caps": {
        "driver_preflight": {"getMe": 1, "getWebhookInfo": 1},
        "bridge": {
            "getMe": 1,
            "setMyCommands": 1,
            "getUpdates": 20,
            "sendMessage": 2,
            "sendChatAction": 151,
            "editMessageText": 256,
        },
    },
}


class _EvidenceBlocked(RuntimeError):
    pass


def _acceptance_error(code: str) -> Exception:
    from tools.release_1_0_acceptance import AcceptanceError

    return AcceptanceError("telegram_" + code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _acceptance_error(code)


def _exact(value: object, keys: set[str], code: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys, code)
    return dict(value)  # type: ignore[arg-type]


def _digest(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _decode_json(raw: bytes, code: str) -> dict[str, Any]:
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate key")
        return value

    def constant(_value):
        raise ValueError("nonfinite constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite float")
        return number

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _acceptance_error(code) from exc
    _require(isinstance(value, dict), code)
    return value


def _read_bytes(path: Path, digest: str, *, maximum: int, allow_empty: bool = False) -> bytes:
    from tools.release_1_0_acceptance import _read_bound_receipt_bytes

    return _read_bound_receipt_bytes(path, digest, max_bytes=maximum, allow_empty=allow_empty)


def _read_json(path: Path, digest: str, *, maximum: int, code: str) -> dict[str, Any]:
    return _decode_json(_read_bytes(path, digest, maximum=maximum), code)


def _reference(value: object, code: str, *, sized: bool) -> dict[str, Any]:
    keys = {"path", "sha256", "size_bytes"} if sized else {"path", "sha256"}
    result = _exact(value, keys, code)
    _require(
        isinstance(result["path"], str)
        and Path(result["path"]).is_absolute()
        and isinstance(result["sha256"], str)
        and SHA256_RE.fullmatch(result["sha256"]) is not None,
        code,
    )
    if sized:
        _require(
            type(result["size_bytes"]) is int and 0 <= result["size_bytes"] <= MAX_DATABASE_BYTES,
            code,
        )
    return result


def _path(raw: object, code: str) -> Path:
    _require(isinstance(raw, str) and Path(raw).is_absolute(), code)
    path = Path(str(raw))
    try:
        _require(path.resolve(strict=True) == path, code)
    except OSError as exc:
        raise _acceptance_error(code) from exc
    return path


def _future_or_existing_path(raw: object, code: str) -> Path:
    _require(isinstance(raw, str) and Path(raw).is_absolute(), code)
    path = Path(str(raw))
    try:
        parent = path.parent.resolve(strict=True)
        _require(parent == path.parent and not path.is_symlink(), code)
        if path.exists():
            _require(path.resolve(strict=True) == path, code)
    except OSError as exc:
        raise _acceptance_error(code) from exc
    return path


def _source_digest(path: Path, expected: str, maximum: int) -> None:
    """Bind non-secret source/controller bytes without requiring mode 0600."""
    _require(SHA256_RE.fullmatch(expected) is not None, "source_digest_invalid")
    try:
        before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
        opened = os.fstat(descriptor)
        _require(
            path.is_absolute()
            and path.resolve(strict=True) == path
            and stat.S_ISREG(opened.st_mode)
            and not path.is_symlink()
            and opened.st_uid == os.getuid()
            and opened.st_nlink == 1
            and not opened.st_mode & 0o022
            and 0 < opened.st_size <= maximum
            and (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ),
            "source_file_invalid",
        )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        after = path.lstat()
        final_opened = os.fstat(descriptor)
        _require(
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            and (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            == (
                final_opened.st_dev,
                final_opened.st_ino,
                final_opened.st_size,
                final_opened.st_mtime_ns,
                final_opened.st_ctime_ns,
            )
            and digest.hexdigest() == expected,
            "source_digest_mismatch",
        )
    except OSError as exc:
        raise _acceptance_error("source_file_invalid") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _parse_time(value: object, code: str) -> dt.datetime:
    _require(isinstance(value, str) and len(value) <= 64, code)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise _acceptance_error(code) from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, code)
    return parsed.astimezone(dt.UTC)


def read_context(path: Path, digest: str) -> dict[str, Any]:
    context = _read_json(path, digest, maximum=MAX_CONTEXT_BYTES, code="context_json_invalid")
    schema = context.get("schema")
    _require(schema in {CONTEXT_SCHEMA, CONTEXT_SCHEMA_V2}, "context_schema")
    context = _exact(
        context,
        {
            "schema",
            "case_id",
            "attempt_id",
            "output_root",
            "evidence_root",
            "candidate_identity",
            "policy",
            "access",
            "controller",
            "driver",
            "expected",
            "budgets",
        }
        | (
            {"harness_identity", "effect_admission", "handoff_plan"} if schema == CONTEXT_SCHEMA_V2 else set()
        ),
        "context_shape_invalid",
    )
    _require(
        context["case_id"] == CASE_ID,
        "context_schema",
    )
    _require(
        isinstance(context["attempt_id"], str) and ATTEMPT_RE.fullmatch(context["attempt_id"]) is not None,
        "context_attempt",
    )
    output_root = _path(context["output_root"], "context_output_root")
    evidence_root = _future_or_existing_path(context["evidence_root"], "context_evidence_root")
    _require(evidence_root == output_root / "evidence", "context_evidence_scope")
    _require(not path.resolve().is_relative_to(output_root), "context_not_independent")

    identity = _exact(
        context["candidate_identity"],
        {
            "candidate_sha",
            "candidate_tree",
            "base_sha",
            "wheel_sha256",
            "inventory_sha256",
            "source_map_sha256",
            "source_root",
            "suite_revision",
        },
        "context_candidate_shape",
    )
    for key in ("candidate_sha", "candidate_tree", "base_sha"):
        _require(
            isinstance(identity[key], str) and GIT_SHA_RE.fullmatch(identity[key]),
            "context_candidate",
        )
    for key in ("wheel_sha256", "inventory_sha256", "source_map_sha256"):
        _require(
            isinstance(identity[key], str) and SHA256_RE.fullmatch(identity[key]),
            "context_candidate",
        )
    source_root = _path(identity["source_root"], "context_source_root")
    _require(
        not source_root.is_relative_to(output_root)
        and isinstance(identity["suite_revision"], str)
        and bool(identity["suite_revision"]),
        "context_candidate",
    )
    if schema == CONTEXT_SCHEMA:
        _require(source_root == ROOT, "context_candidate")
    else:
        harness = _exact(
            context["harness_identity"],
            {
                "manifest_path",
                "manifest_sha256",
                "source_root",
                "source_map_sha256",
                "source_file_count",
                "driver_sha256",
            },
            "context_harness_shape",
        )
        for key in ("manifest_sha256", "source_map_sha256", "driver_sha256"):
            _require(
                isinstance(harness[key], str) and SHA256_RE.fullmatch(harness[key]) is not None,
                "context_harness_identity",
            )
        _require(
            type(harness["source_file_count"]) is int and harness["source_file_count"] == 5,
            "context_harness_identity",
        )
        _require(
            _path(harness["source_root"], "context_harness_source_root") == ROOT
            and _path(harness["manifest_path"], "context_harness_manifest_path").parent != output_root,
            "context_harness_identity",
        )

    reference_names = ["policy", "access", "controller", "driver"]
    if schema == CONTEXT_SCHEMA_V2:
        reference_names.extend(("effect_admission", "handoff_plan"))
    for name in reference_names:
        context[name] = _reference(context[name], f"context_{name}_ref", sized=False)
        ref_path = _path(context[name]["path"], f"context_{name}_path")
        _require(not ref_path.is_relative_to(output_root), f"context_{name}_independence")
    _require(
        Path(context["driver"]["path"]) == ROOT / "tools/release_1_0_telegram_roundtrip.py",
        "context_driver_path",
    )
    _require(
        len({context[name]["path"] for name in reference_names}) == len(reference_names),
        "context_reference_alias",
    )

    expected_keys = {
        "contour_id",
        "bot_user_id",
        "observer_mode",
        "bot_token_sha256",
        "env_sha256",
    }
    if schema == CONTEXT_SCHEMA:
        expected_keys |= {"chat_id", "user_id"}
    else:
        expected_keys |= {
            "access_route",
            "owner_private_chat_hmac_sha256",
            "owner_authority_sha256",
            "startup_commands_sha256",
            "effect_admission_sha256",
            "installed_wheel_sha256",
        }
    expected = _exact(
        context["expected"],
        expected_keys,
        "context_expected_shape",
    )
    _require(
        isinstance(expected["contour_id"], str)
        and 8 <= len(expected["contour_id"]) <= 80
        and expected["observer_mode"] in {"owner_manual_readback", "external_user_adapter"},
        "context_expected_identity",
    )
    for key in ("bot_user_id", "chat_id", "user_id") if schema == CONTEXT_SCHEMA else ("bot_user_id",):
        _require(
            type(expected[key]) is int and 0 < expected[key] < 10**20,
            "context_expected_identity",
        )
    if schema == CONTEXT_SCHEMA:
        _require(expected["chat_id"] == expected["user_id"], "context_expected_private_chat")
    else:
        _require(
            expected["access_route"] == "existing_friday_bot_one_canary"
            and SHA256_RE.fullmatch(str(expected["owner_private_chat_hmac_sha256"])) is not None
            and SHA256_RE.fullmatch(str(expected["owner_authority_sha256"])) is not None
            and SHA256_RE.fullmatch(str(expected["startup_commands_sha256"])) is not None
            and SHA256_RE.fullmatch(str(expected["effect_admission_sha256"])) is not None
            and SHA256_RE.fullmatch(str(expected["installed_wheel_sha256"])) is not None,
            "context_expected_owner_binding",
        )
    _require(
        all(SHA256_RE.fullmatch(str(expected[key])) for key in ("bot_token_sha256", "env_sha256")),
        "context_expected_digest",
    )
    _require(context["budgets"] == FROZEN_BUDGETS, "context_budget_mismatch")
    if schema == CONTEXT_SCHEMA_V2:
        _validate_effect_admission(
            _read_json(
                Path(context["effect_admission"]["path"]),
                context["effect_admission"]["sha256"],
                maximum=MAX_CONTEXT_BYTES,
                code="effect_admission_json_invalid",
            ),
            identity=identity,
            harness=context["harness_identity"],
        )
        _require(
            context["effect_admission"]["sha256"] == expected["effect_admission_sha256"],
            "effect_admission_expected_mismatch",
        )
        _validate_handoff_plan(
            _read_json(
                Path(context["handoff_plan"]["path"]),
                context["handoff_plan"]["sha256"],
                maximum=MAX_CONTEXT_BYTES,
                code="handoff_plan_json_invalid",
            ),
            identity=identity,
        )
    context["_context_version"] = 2 if schema == CONTEXT_SCHEMA_V2 else 1
    return context


def _validate_handoff_plan(document: object, *, identity: Mapping[str, Any]) -> dict[str, Any]:
    plan = _exact(
        document,
        {
            "schema",
            "candidate_sha",
            "candidate_tree",
            "wheel_sha256",
            "systemctl_path",
            "systemctl_sha256",
            "unit_name",
            "production_lease_path",
            "exclusive_slot_lock",
            "max_stop_calls",
            "max_restore_calls",
            "required_initial_state",
            "required_final_state",
            "no_second_consumer",
            "production_home_as_scratch",
            "history_policy",
            "GO",
        },
        "handoff_plan_shape_invalid",
    )
    _require(
        plan["schema"] == HANDOFF_PLAN_SCHEMA
        and plan["candidate_sha"] == identity["candidate_sha"]
        and plan["candidate_tree"] == identity["candidate_tree"]
        and plan["wheel_sha256"] == identity["wheel_sha256"]
        and isinstance(plan["systemctl_path"], str)
        and Path(plan["systemctl_path"]).is_absolute()
        and isinstance(plan["systemctl_sha256"], str)
        and SHA256_RE.fullmatch(plan["systemctl_sha256"]) is not None
        and plan["unit_name"] == "friday-bridge.service"
        and isinstance(plan["production_lease_path"], str)
        and Path(plan["production_lease_path"]).is_absolute()
        and isinstance(plan["exclusive_slot_lock"], str)
        and Path(plan["exclusive_slot_lock"]).is_absolute()
        and plan["max_stop_calls"] == 1
        and plan["max_restore_calls"] == 1
        and plan["required_initial_state"] == "active"
        and plan["required_final_state"] == "active"
        and plan["no_second_consumer"] is True
        and plan["production_home_as_scratch"] is False
        and plan["history_policy"] == "do_not_read_replay_delete_or_drain"
        and plan["GO"] is False,
        "handoff_plan_binding_invalid",
    )
    return plan


def _validate_effect_admission(
    document: object,
    *,
    identity: Mapping[str, Any],
    harness: Mapping[str, Any],
) -> dict[str, Any]:
    admission = _exact(
        document,
        {
            "schema",
            "route_id",
            "candidate_sha",
            "candidate_tree",
            "wheel_sha256",
            "harness_manifest_sha256",
            "base_effect_scope",
            "owner_directive",
            "existing_friday_bot_one_canary",
            "same_bot_production_consumer_stopped",
            "isolated_non_production_home",
            "installed_wheel_origin",
            "owner_private_recipient_only",
            "startup_effect",
            "frozen_limits",
            "unknown_effect_policy",
            "nonowner_ingress_policy",
            "GO",
        },
        "effect_admission_shape_invalid",
    )
    base = _reference(admission["base_effect_scope"], "effect_admission_base_ref", sized=False)
    owner = _reference(admission["owner_directive"], "effect_admission_owner_ref", sized=False)
    startup = _exact(
        admission["startup_effect"],
        {
            "method",
            "max_attempts",
            "target",
            "canonical_payload_digest_required",
            "restoration_claimed",
        },
        "effect_admission_startup_shape_invalid",
    )
    expected_limits = {
        "duration_s": FROZEN_BUDGETS["duration_s"],
        "genuine_inbound": FROZEN_BUDGETS["max_inbound_user_messages"],
        "bot_posts": FROZEN_BUDGETS["method_caps"]["bridge"]["sendMessage"],
        "total_attempts": FROZEN_BUDGETS["total_attempt_cap"],
        "method_caps": FROZEN_BUDGETS["method_caps"],
    }
    _require(
        admission["schema"] == EFFECT_ADMISSION_SCHEMA
        and admission["route_id"] == "existing-friday-bot-sole-consumer-handoff-installed-final"
        and admission["candidate_sha"] == identity["candidate_sha"]
        and admission["candidate_tree"] == identity["candidate_tree"]
        and admission["wheel_sha256"] == identity["wheel_sha256"]
        and admission["harness_manifest_sha256"] == harness["manifest_sha256"]
        and all(
            admission[key] is True
            for key in (
                "existing_friday_bot_one_canary",
                "same_bot_production_consumer_stopped",
                "isolated_non_production_home",
                "installed_wheel_origin",
                "owner_private_recipient_only",
            )
        )
        and startup
        == {
            "method": "setMyCommands",
            "max_attempts": 1,
            "target": "existing_friday_bot",
            "canonical_payload_digest_required": True,
            "restoration_claimed": False,
        }
        and admission["frozen_limits"] == expected_limits
        and admission["unknown_effect_policy"] == "latch_and_never_retry"
        and admission["nonowner_ingress_policy"] == "fail_before_bridge_observation_no_reply_no_drain"
        and admission["GO"] is False,
        "effect_admission_binding_invalid",
    )
    base_document = _read_json(
        Path(base["path"]),
        base["sha256"],
        maximum=MAX_CONTEXT_BYTES,
        code="effect_admission_base_invalid",
    )
    owner_document = _read_json(
        Path(owner["path"]),
        owner["sha256"],
        maximum=MAX_CONTEXT_BYTES,
        code="effect_admission_owner_invalid",
    )
    _require(
        base_document.get("schema") == "friday.astra-telegram-effect-scope.v1"
        and base_document.get("GO") is False
        and owner_document.get("schema") == "friday.owner-telegram-recipient-restriction.v1"
        and owner_document.get("GO") is False
        and isinstance(owner_document.get("recipient_scope"), dict)
        and owner_document["recipient_scope"].get("only_owner_private_chat") is True,
        "effect_admission_authority_invalid",
    )
    return admission


def _secure_tree(
    root: Path,
    declared: Mapping[str, Any],
    expected_digest: str,
    total: int,
) -> dict[str, bytes]:
    from tools import release_1_0_telegram_roundtrip as telegram

    _require(root.is_absolute() and root.resolve(strict=True) == root, "evidence_root_invalid")
    telegram._secure_directory(root, "Telegram receipt evidence")
    _require(isinstance(declared, dict) and bool(declared), "evidence_inventory_invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw_ref in declared.items():
        _require(
            isinstance(name, str)
            and name == Path(name).as_posix()
            and not Path(name).is_absolute()
            and ".." not in Path(name).parts,
            "evidence_inventory_path",
        )
        ref = _reference(raw_ref, "evidence_inventory_ref", sized=True)
        _require(ref["path"] == str(root / name), "evidence_inventory_alias")
        normalized[name] = {"sha256": ref["sha256"], "size_bytes": ref["size_bytes"]}
    _require(_digest(normalized) == expected_digest, "evidence_inventory_digest")

    actual: set[str] = set()
    for directory, names, files in os.walk(root):
        base = Path(directory)
        telegram._secure_directory(base, "Telegram receipt evidence directory")
        for name in names:
            _require(not (base / name).is_symlink(), "evidence_symlink")
        for name in files:
            actual.add((base / name).relative_to(root).as_posix())
    _require(actual == set(normalized), "evidence_inventory_members")
    required = {
        "events.jsonl",
        "report.json",
        "observer-request.json",
        "observer-sanitized.json",
        "durable-observation.json",
        "origin-server.json",
        "origin-telegram-bridge.json",
        "log-server.json",
        "log-telegram-bridge.json",
        "telegram-effects/spec.json",
        "telegram-effects/seal.json",
        "telegram-effects/reservation.bin",
    }
    _require(required.issubset(actual), "evidence_required_files_missing")
    payloads: dict[str, bytes] = {}
    observed_total = 0
    for name, ref in normalized.items():
        raw = _read_bytes(
            root / name,
            str(ref["sha256"]),
            maximum=FROZEN_BUDGETS["evidence_max_bytes"],
            allow_empty=name == "events.jsonl",
        )
        _require(len(raw) == ref["size_bytes"], "evidence_size_mismatch")
        observed_total += len(raw)
        payloads[name] = raw
    _require(
        observed_total == total and 0 < total <= FROZEN_BUDGETS["evidence_max_bytes"],
        "evidence_total_invalid",
    )
    return payloads


def _inventory_json(payloads: Mapping[str, bytes], name: str) -> dict[str, Any]:
    _require(name in payloads, "evidence_required_files_missing")
    return _decode_json(payloads[name], "evidence_json_invalid")


def _events(raw: bytes, *, attempt_id: str, candidate_sha: str) -> list[dict[str, Any]]:
    _require(bool(raw), "events_missing")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        row = _decode_json(line, "event_json_invalid")
        row = _exact(row, {"schema", "at", "stage", "state", "facts"}, "event_shape_invalid")
        _require(
            row["schema"] == "friday.release-1-0-telegram-roundtrip-event.v1"
            and isinstance(row["facts"], dict),
            "event_shape_invalid",
        )
        _parse_time(row["at"], "event_time_invalid")
        rows.append(row)
    expected = [
        ("preflight", "STARTED"),
        ("candidate", "VERIFIED"),
        ("telegram_preflight", "VERIFIED"),
        ("server", "STARTED"),
        ("backend", "READY"),
        ("telegram-bridge", "STARTED"),
        ("bridge", "READY"),
        ("observer", "REQUESTED"),
        ("observer", "VERIFIED"),
        ("four_legs", "VERIFIED"),
    ]
    positions = []
    for pair in expected:
        matches = [index for index, row in enumerate(rows) if (row["stage"], row["state"]) == pair]
        _require(len(matches) == 1, "event_cardinality_invalid")
        positions.append(matches[0])
    _require(positions == sorted(positions), "event_order_invalid")
    candidate = rows[positions[1]]["facts"]
    _require(
        candidate.get("candidate_sha") == candidate_sha,
        "event_candidate_mismatch",
    )
    return rows


def _origin(
    document: object,
    *,
    role: str,
    policy: Any,
    spec_sha256: str,
    driver_sha256: str,
) -> dict[str, Any]:
    installed = getattr(policy.candidate, "installed_wheel", None)
    origin_keys = {
        "schema",
        "role",
        "pid",
        "candidate_sha",
        "cli_origin",
        "effect_guard_origin",
        "effect_guard_sha256",
        "effect_spec_sha256",
        "argv",
        "observed_at_ns",
    }
    if installed is None:
        origin_keys.add("source_root")
    else:
        origin_keys |= {
            "installed_site_root",
            "wheel_sha256",
            "source_checkout_imported",
        }
    row = _exact(
        document,
        origin_keys,
        "origin_shape_invalid",
    )
    guard_path = str(ROOT / "tools/release_1_0_telegram_roundtrip.py") if role == "telegram-bridge" else ""
    if installed is None:
        origin_binding = (
            row["schema"] == "friday.release-1-0-child-origin.v1"
            and row["source_root"] == str(ROOT)
            and row["cli_origin"] == str(ROOT / "friday/cli.py")
        )
    else:
        origin_binding = (
            row["schema"] == "friday.release-1-0-installed-wheel-child-origin.v1"
            and row["installed_site_root"] == str(installed.site_root.resolve())
            and row["wheel_sha256"] == installed.wheel_sha256
            and row["cli_origin"] == str((installed.site_root / "friday/cli.py").resolve())
            and row["source_checkout_imported"] is False
        )
    _require(
        origin_binding
        and row["role"] == role
        and type(row["pid"]) is int
        and row["pid"] > 1
        and row["candidate_sha"] == policy.candidate.candidate_sha
        and row["effect_guard_origin"] == guard_path
        and row["effect_guard_sha256"] == (driver_sha256 if role == "telegram-bridge" else "")
        and row["effect_spec_sha256"] == (spec_sha256 if role == "telegram-bridge" else "")
        and row["argv"] == ["friday.cli", "--env-file", str(policy.contour.env_file.resolve()), role]
        and type(row["observed_at_ns"]) is int
        and row["observed_at_ns"] > 0,
        "origin_binding_mismatch",
    )
    return row


def _cleanup(document: object, *, role: str, pid: int, process_log_bytes: int) -> dict[str, Any]:
    row = _exact(
        document,
        {
            "schema",
            "role",
            "pid",
            "returncode",
            "process_group_clear",
            "capture_drained",
            "captured_bytes",
            "discarded_bytes",
            "secrets_persisted",
        },
        "cleanup_shape_invalid",
    )
    _require(
        row["schema"] == "friday.release-1-0-owned-process-log.v1"
        and row["role"] == role
        and row["pid"] == pid
        and type(row["returncode"]) is int
        and row["process_group_clear"] is True
        and row["capture_drained"] is True
        and type(row["captured_bytes"]) is int
        and 0 <= row["captured_bytes"] <= process_log_bytes
        and type(row["discarded_bytes"]) is int
        and row["discarded_bytes"] >= 0
        and row["secrets_persisted"] is False,
        "cleanup_incomplete",
    )
    return row


def _status(status: str, failure: str | None, **facts: object) -> dict[str, Any]:
    return {
        "status": status,
        "case_layers": {CASE_ID: "deployment-device"} if status == "PASS" else {},
        "case_statuses": {CASE_ID: status},
        "root_failure": failure,
        "go_emitted": False,
        "full_gate_credit": False,
        "unit_controls_grant_live_credit": False,
        **facts,
    }


def _validate_handoff_receipt(document: object, *, plan_reference: Mapping[str, Any]) -> dict[str, Any]:
    plan = _read_json(
        Path(str(plan_reference["path"])),
        str(plan_reference["sha256"]),
        maximum=MAX_CONTEXT_BYTES,
        code="handoff_plan_json_invalid",
    )
    row = _exact(
        document,
        {
            "schema",
            "plan",
            "unit_name",
            "systemctl_sha256",
            "initial_state",
            "stop_count",
            "stop_returncode",
            "post_stop_state",
            "production_lease_inactive",
            "driver_started_after_stop",
            "isolated_process_group_clear_before_restore",
            "restore_count",
            "restore_returncode",
            "post_restore_state",
            "production_lease_active",
            "no_second_consumer",
            "production_home_as_scratch",
            "history_touched",
            "started_at",
            "stopped_at",
            "restored_at",
            "GO",
        },
        "handoff_receipt_shape_invalid",
    )
    started = _parse_time(row["started_at"], "handoff_receipt_time_invalid")
    stopped = _parse_time(row["stopped_at"], "handoff_receipt_time_invalid")
    restored = _parse_time(row["restored_at"], "handoff_receipt_time_invalid")
    _require(
        row["schema"] == HANDOFF_RECEIPT_SCHEMA
        and row["plan"] == dict(plan_reference)
        and row["unit_name"] == plan.get("unit_name") == "friday-bridge.service"
        and row["systemctl_sha256"] == plan.get("systemctl_sha256")
        and row["initial_state"] == "active"
        and row["stop_count"] == 1
        and row["stop_returncode"] == 0
        and row["post_stop_state"] == "inactive"
        and row["production_lease_inactive"] is True
        and row["driver_started_after_stop"] is True
        and row["isolated_process_group_clear_before_restore"] is True
        and row["restore_count"] == 1
        and row["restore_returncode"] == 0
        and row["post_restore_state"] == "active"
        and row["production_lease_active"] is True
        and row["no_second_consumer"] is True
        and row["production_home_as_scratch"] is False
        and row["history_touched"] is False
        and started <= stopped <= restored
        and row["GO"] is False,
        "handoff_receipt_binding_invalid",
    )
    return row


def audit_telegram_execution(
    *,
    receipt_path: Path,
    receipt_sha256: str,
    context_path: Path,
    context_sha256: str,
    expected_identity: Mapping[str, str],
    expected_suite: str,
) -> dict[str, Any]:
    """Return only deployment-device credit reconstructed from original bytes."""
    from tools import release_1_0_telegram_roundtrip as telegram
    from tools.release_1_0_acceptance import AcceptanceError

    try:
        context = read_context(context_path, context_sha256)
        identity = context["candidate_identity"]
        _require(
            set(expected_identity)
            == {"candidate_sha", "candidate_tree", "base_sha", "wheel_sha256", "inventory_sha256"},
            "expected_candidate_shape",
        )
        gate_identity = {key: identity[key] for key in expected_identity}
        _require(gate_identity == dict(expected_identity), "candidate_identity_mismatch")
        _require(identity["suite_revision"] == expected_suite, "suite_mismatch")

        receipt = _read_json(
            receipt_path,
            receipt_sha256,
            maximum=MAX_RECEIPT_BYTES,
            code="receipt_json_invalid",
        )
        receipt_keys = {
            "schema",
            "case_id",
            "attempt_id",
            "attempt",
            "expected_context",
            "policy",
            "access",
            "evidence",
            "observer_result",
            "databases",
            "controller",
            "GO",
            "release_ready",
            "full_gate_credit",
        }
        if context["_context_version"] == 2:
            receipt_keys.add("handoff_receipt")
        receipt = _exact(
            receipt,
            receipt_keys,
            "receipt_shape_invalid",
        )
        expected_receipt_schema = RECEIPT_SCHEMA_V2 if context["_context_version"] == 2 else RECEIPT_SCHEMA
        _require(
            receipt["schema"] == expected_receipt_schema
            and receipt["case_id"] == CASE_ID
            and receipt["attempt_id"] == context["attempt_id"]
            and type(receipt["attempt"]) is int
            and receipt["attempt"] == 1
            and receipt["GO"] is False
            and receipt["release_ready"] is False
            and receipt["full_gate_credit"] is False,
            "receipt_binding_mismatch",
        )
        _require(receipt_path.parent == Path(context["output_root"]), "receipt_scope")
        _require(
            receipt["expected_context"] == {"path": str(context_path), "sha256": context_sha256}
            and receipt["policy"] == context["policy"]
            and receipt["access"] == context["access"],
            "receipt_input_binding_mismatch",
        )
        if context["_context_version"] == 2:
            handoff_ref = _reference(
                receipt["handoff_receipt"],
                "handoff_receipt_ref",
                sized=True,
            )
            handoff_path = Path(handoff_ref["path"])
            _require(
                handoff_path.parent == Path(context["output_root"]),
                "handoff_receipt_scope_invalid",
            )
            _validate_handoff_receipt(
                _read_json(
                    handoff_path,
                    handoff_ref["sha256"],
                    maximum=MAX_CONTEXT_BYTES,
                    code="handoff_receipt_json_invalid",
                ),
                plan_reference=context["handoff_plan"],
            )

        controller = _exact(
            receipt["controller"],
            {
                "executable",
                "pid",
                "argv",
                "driver_sha256",
                "policy_sha256",
                "evidence_root",
                "outcome",
                "leader_returncode",
                "timed_out",
                "error_code",
                "start_count",
                "terminal_count",
                "unknown_count",
                "started_at",
                "ended_at",
                "process_group_clear",
                "leader_reaped",
                "descendants_clear",
                "capture_drained",
                "stop_unconfirmed_emitted",
            },
            "controller_shape_invalid",
        )
        _require(
            controller["executable"] == context["controller"],
            "controller_executable_mismatch",
        )
        _source_digest(
            Path(context["controller"]["path"]),
            context["controller"]["sha256"],
            MAX_CONTROLLER_BYTES,
        )
        _require(
            type(controller["pid"]) is int
            and controller["pid"] > 1
            and isinstance(controller["argv"], list)
            and all(isinstance(item, str) and item and "\x00" not in item for item in controller["argv"])
            and controller["driver_sha256"] == context["driver"]["sha256"]
            and controller["policy_sha256"] == context["policy"]["sha256"]
            and controller["evidence_root"] == context["evidence_root"],
            "controller_binding_mismatch",
        )
        started = _parse_time(controller["started_at"], "controller_time_invalid")
        ended = _parse_time(controller["ended_at"], "controller_time_invalid")
        _require(ended >= started, "controller_time_invalid")
        if (
            type(controller["start_count"]) is not int
            or controller["start_count"] != 1
            or type(controller["terminal_count"]) is not int
            or controller["terminal_count"] != 1
            or type(controller["unknown_count"]) is not int
            or controller["unknown_count"] != 0
            or type(controller["timed_out"]) is not bool
            or controller["timed_out"] is not False
            or type(controller["stop_unconfirmed_emitted"]) is not bool
            or controller["stop_unconfirmed_emitted"] is not False
        ):
            raise _EvidenceBlocked("telegram_controller_outcome_unconfirmed")
        if not all(
            controller.get(key) is True
            for key in (
                "process_group_clear",
                "leader_reaped",
                "descendants_clear",
                "capture_drained",
            )
        ):
            raise _EvidenceBlocked("telegram_controller_cleanup_incomplete")

        _source_digest(Path(context["driver"]["path"]), context["driver"]["sha256"], 4 << 20)
        _require(
            FROZEN_BUDGETS["duration_s"] == telegram.EFFECT_DURATION_LIMIT_S
            and FROZEN_BUDGETS["total_attempt_cap"] == telegram.EFFECT_TOTAL_ATTEMPT_CAP
            and FROZEN_BUDGETS["request_max_bytes"] == telegram.EFFECT_REQUEST_MAX_BYTES
            and FROZEN_BUDGETS["response_max_bytes"] == telegram.EFFECT_RESPONSE_MAX_BYTES
            and FROZEN_BUDGETS["ledger_reservation_bytes"] == telegram.EFFECT_LEDGER_RESERVATION_BYTES
            and FROZEN_BUDGETS["method_caps"] == telegram.EFFECT_METHOD_CAPS,
            "driver_budget_mismatch",
        )
        policy_path = Path(context["policy"]["path"])
        _read_bytes(policy_path, context["policy"]["sha256"], maximum=2 << 20)
        try:
            policy = telegram.load_policy(policy_path)
        except (OSError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("policy_invalid") from exc
        _read_bytes(policy_path, context["policy"]["sha256"], maximum=2 << 20)
        _require(
            policy.attempt_id == context["attempt_id"]
            and policy.candidate.source_root.resolve()
            == Path(context["candidate_identity"]["source_root"]).resolve()
            and policy.candidate.candidate_sha == identity["candidate_sha"]
            and policy.candidate.candidate_tree == identity["candidate_tree"]
            and policy.candidate.suite_revision == expected_suite
            and policy.access.manifest_path == Path(context["access"]["path"])
            and policy.access.manifest_sha256 == context["access"]["sha256"]
            and policy.contour.contour_id == context["expected"]["contour_id"]
            and policy.contour.env_file_sha256 == context["expected"]["env_sha256"]
            and policy.observer.mode == context["expected"]["observer_mode"],
            "policy_binding_mismatch",
        )
        if context["_context_version"] == 2:
            harness_identity = context["harness_identity"]
            _require(
                policy.harness is not None
                and policy.harness.source_root.resolve() == ROOT
                and str(policy.harness.manifest_path) == harness_identity["manifest_path"]
                and policy.harness.manifest_sha256 == harness_identity["manifest_sha256"]
                and policy.candidate.installed_wheel is not None
                and policy.candidate.installed_wheel.wheel_sha256
                == context["expected"]["installed_wheel_sha256"]
                and policy.effect_admission is not None
                and str(policy.effect_admission.manifest_path) == context["effect_admission"]["path"]
                and policy.effect_admission.manifest_sha256 == context["expected"]["effect_admission_sha256"],
                "policy_harness_binding_mismatch",
            )
        _require(
            controller["argv"]
            == [
                str(policy.candidate.python_executable),
                "-I",
                "-B",
                str(Path(context["driver"]["path"])),
                "--policy",
                str(policy_path),
                "--evidence-dir",
                context["evidence_root"],
            ],
            "controller_argv_mismatch",
        )
        _source_digest(
            policy.candidate.python_executable,
            policy.candidate.python_sha256,
            64 << 20,
        )
        _require(
            policy.contour.isolation_root.is_relative_to(Path(context["output_root"]))
            and not policy.contour.env_file.is_relative_to(Path(context["output_root"])),
            "policy_path_scope_invalid",
        )
        for directory in (
            Path(context["output_root"]),
            policy.contour.isolation_root,
            policy.contour.friday_home,
            policy.contour.data_dir,
            policy.contour.state_dir,
        ):
            try:
                telegram._secure_directory(directory, "Telegram retained contour")
            except (OSError, ValueError, telegram.RoundtripError) as exc:
                raise _acceptance_error("contour_directory_invalid") from exc
            _require(directory.resolve(strict=True) == directory, "contour_directory_alias")
        for directory in (
            policy.contour.cache_dir,
            policy.contour.log_dir,
            policy.contour.files_dir,
        ):
            _require(
                directory.is_relative_to(policy.contour.friday_home),
                "contour_directory_scope",
            )
            if directory.exists() or directory.is_symlink():
                try:
                    telegram._secure_directory(directory, "Telegram retained contour")
                except (OSError, ValueError, telegram.RoundtripError) as exc:
                    raise _acceptance_error("contour_directory_invalid") from exc
                _require(directory.resolve(strict=True) == directory, "contour_directory_alias")
        _require(
            policy.budgets.timeout_s == 600
            and policy.budgets.max_getupdates_rounds == 20
            and policy.budgets.max_inbound_user_messages == 1
            and policy.budgets.max_outbound_bot_posts == 2
            and policy.budgets.max_evidence_bytes == 1048576,
            "policy_budget_mismatch",
        )

        access_document = _read_json(
            Path(context["access"]["path"]),
            context["access"]["sha256"],
            maximum=65536,
            code="access_json_invalid",
        )
        try:
            access = telegram.AccessManifest.from_document(
                access_document,
                policy=policy,
                now=started,
            )
        except (ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("access_invalid") from exc
        _require(
            access.contour_id == context["expected"]["contour_id"]
            and access.bot_user_id == context["expected"]["bot_user_id"]
            and access.observer_mode == context["expected"]["observer_mode"],
            "access_binding_mismatch",
        )
        if context["_context_version"] == 1:
            _require(
                access.chat_id == context["expected"]["chat_id"]
                and access.user_id == context["expected"]["user_id"],
                "access_binding_mismatch",
            )
        else:
            _require(
                access.route == context["expected"]["access_route"]
                and access.owner_private_chat_hmac_sha256
                == context["expected"]["owner_private_chat_hmac_sha256"]
                and access.owner_authority_sha256 == context["expected"]["owner_authority_sha256"]
                and access.startup_commands_sha256 == context["expected"]["startup_commands_sha256"],
                "access_owner_binding_mismatch",
            )

        env_raw = _read_bytes(
            policy.contour.env_file,
            context["expected"]["env_sha256"],
            maximum=256 << 10,
        )
        try:
            env = telegram._parse_env_file(policy.contour.env_file)
        except (OSError, UnicodeError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("config_invalid") from exc
        _read_bytes(
            policy.contour.env_file,
            context["expected"]["env_sha256"],
            maximum=256 << 10,
        )
        token = str(env.get("FRIDAY_TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise _EvidenceBlocked("telegram_effect_hmac_key_missing")
        _require(
            token.encode() in env_raw
            and hashlib.sha256(token.encode()).hexdigest() == context["expected"]["bot_token_sha256"],
            "token_identity_mismatch",
        )

        try:
            source_before = (
                telegram._verified_source_map(policy.candidate, policy.harness)
                if getattr(policy, "harness", None) is not None
                else telegram._verified_source_map(policy.candidate)
            )
        except (OSError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("candidate_source_invalid") from exc
        _require(
            source_before["source_map_sha256"] == identity["source_map_sha256"]
            and source_before["module_sha256"] == context["driver"]["sha256"],
            "candidate_source_binding_mismatch",
        )
        if context["_context_version"] == 2:
            harness_identity = context["harness_identity"]
            _require(
                source_before.get("harness")
                == {
                    "manifest_sha256": harness_identity["manifest_sha256"],
                    "source_map_sha256": harness_identity["source_map_sha256"],
                    "source_file_count": harness_identity["source_file_count"],
                    "driver_sha256": harness_identity["driver_sha256"],
                }
                and source_before.get("installed_wheel", {}).get("wheel_sha256")
                == context["expected"]["installed_wheel_sha256"]
                and source_before.get("installed_wheel", {}).get("source_checkout_imported") is False,
                "harness_source_binding_mismatch",
            )

        evidence = _exact(
            receipt["evidence"],
            {"root", "files", "inventory_sha256", "total_bytes"},
            "evidence_shape_invalid",
        )
        _require(
            evidence["root"] == context["evidence_root"]
            and isinstance(evidence["inventory_sha256"], str)
            and SHA256_RE.fullmatch(evidence["inventory_sha256"]) is not None
            and type(evidence["total_bytes"]) is int,
            "evidence_root_mismatch",
        )
        payloads = _secure_tree(
            Path(evidence["root"]),
            evidence["files"],
            str(evidence["inventory_sha256"]),
            evidence["total_bytes"],
        )
        rows = _events(
            payloads["events.jsonl"],
            attempt_id=context["attempt_id"],
            candidate_sha=identity["candidate_sha"],
        )

        spec_sha = hashlib.sha256(payloads["telegram-effects/spec.json"]).hexdigest()
        origins = {
            role: _origin(
                _inventory_json(payloads, f"origin-{role}.json"),
                role=role,
                policy=policy,
                spec_sha256=spec_sha,
                driver_sha256=context["driver"]["sha256"],
            )
            for role in ("server", "telegram-bridge")
        }
        _require(origins["server"]["pid"] != origins["telegram-bridge"]["pid"], "origin_pid_alias")
        cleanups = {
            role: _cleanup(
                _inventory_json(payloads, f"log-{role}.json"),
                role=role,
                pid=origins[role]["pid"],
                process_log_bytes=policy.budgets.process_log_bytes,
            )
            for role in ("server", "telegram-bridge")
        }

        observer_ref = _reference(receipt["observer_result"], "observer_reference", sized=True)
        observer_path = Path(observer_ref["path"])
        _require(
            observer_path == policy.observer.result_path
            and observer_path.is_relative_to(Path(context["output_root"]))
            and not observer_path.is_relative_to(Path(context["evidence_root"]))
            and not observer_path.is_relative_to(policy.contour.friday_home)
            and not observer_path.is_relative_to(ROOT),
            "observer_scope_invalid",
        )
        observer_document = _read_json(
            observer_path,
            observer_ref["sha256"],
            maximum=128 << 10,
            code="observer_json_invalid",
        )
        _require(
            observer_path.stat().st_size == observer_ref["size_bytes"],
            "observer_size_mismatch",
        )
        request = _inventory_json(payloads, "observer-request.json")
        access_route = getattr(access, "route", "dedicated_test_bot")
        request_identity_ok = (
            request.get("schema") == telegram.OBSERVER_REQUEST_SCHEMA
            and request.get("chat_id") == access.chat_id
            and request.get("user_id") == access.user_id
            if access_route == "dedicated_test_bot"
            else request.get("schema") == telegram.OBSERVER_REQUEST_SCHEMA_V2
            and request.get("owner_private_chat_hmac_sha256") == access.owner_private_chat_hmac_sha256
            and "chat_id" not in request
            and "user_id" not in request
        )
        _require(
            request_identity_ok
            and request.get("attempt_id") == context["attempt_id"]
            and request.get("candidate_sha") == identity["candidate_sha"]
            and request.get("candidate_manifest_sha256") == policy.candidate.manifest_sha256
            and request.get("contour_id") == access.contour_id
            and request.get("bot_user_id") == access.bot_user_id
            and request.get("observer_mode") == access.observer_mode
            and request.get("result_path") == str(observer_path)
            and request.get("max_inbound_user_messages") == 1
            and request.get("max_outbound_bot_posts") == 2
            and request.get("unknown_effect_policy") == "do_not_resend",
            "observer_request_binding_mismatch",
        )
        try:
            observer = telegram.validate_observer_result(
                observer_document,
                policy=policy,
                access=access,
                bot={"bot_user_id": access.bot_user_id},
                inbound_text=str(request.get("inbound_text") or ""),
                request_issued_at=telegram._parse_utc(request["issued_at"], "issued_at"),
            )
        except (OSError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("observer_invalid") from exc

        databases = _exact(receipt["databases"], {"main", "inbox"}, "database_shape_invalid")
        main_ref = _reference(databases["main"], "database_main_ref", sized=True)
        inbox_ref = _reference(databases["inbox"], "database_inbox_ref", sized=True)
        for ref, expected_path in (
            (main_ref, policy.contour.database_path),
            (inbox_ref, policy.contour.inbox_db_path),
        ):
            path = Path(ref["path"])
            _require(
                path == expected_path
                and path.is_relative_to(policy.contour.friday_home)
                and path.is_relative_to(Path(context["output_root"])),
                "database_scope_invalid",
            )
            raw = _read_bytes(path, ref["sha256"], maximum=MAX_DATABASE_BYTES)
            _require(len(raw) == ref["size_bytes"], "database_size_mismatch")
            _require(
                not path.with_name(path.name + "-wal").exists()
                and not path.with_name(path.name + "-shm").exists(),
                "database_sidecar_present",
            )
        try:
            durable = telegram.inspect_durable_outcome(policy, access, observer)
        except (OSError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("durable_outcome_invalid") from exc
        for ref in (main_ref, inbox_ref):
            _read_bytes(Path(ref["path"]), ref["sha256"], maximum=MAX_DATABASE_BYTES)

        try:
            effect_summary = telegram.inspect_effect_ledger(
                Path(context["evidence_root"]) / "telegram-effects",
                policy=policy,
                access=access,
                observer=observer,
                durable=durable,
                require_complete=True,
            )
        except telegram.RoundtripError as exc:
            raise _acceptance_error(exc.code) from exc
        except (OSError, ValueError) as exc:
            raise _acceptance_error("effect_ledger_invalid") from exc

        stored_observer = _inventory_json(payloads, "observer-sanitized.json")
        stored_durable = _inventory_json(payloads, "durable-observation.json")
        _require(
            stored_observer
            == {
                "schema": "friday.release-1-0-observer-sanitized.v1",
                "attempt_id": context["attempt_id"],
                "candidate_sha": identity["candidate_sha"],
                "observer_result_path": str(observer_path),
                "observer_result_sha256": observer_ref["sha256"],
                "observer_result_bytes": observer_ref["size_bytes"],
                **observer,
            }
            and stored_durable
            == {
                "schema": "friday.release-1-0-telegram-durable-observation.v1",
                "attempt_id": context["attempt_id"],
                "candidate_sha": identity["candidate_sha"],
                **durable,
            },
            "stored_observation_mismatch",
        )

        receipt_ids = effect_summary.get("send_message_receipt_ids", [])
        four_legs = {
            "real_user_inbound": (
                effect_summary.get("telegram_update_ids") == [durable.get("telegram_update_id")]
                and effect_summary.get("telegram_message_ids") == [durable.get("telegram_message_id")]
                and durable.get("raw_inbound_count") == 1
            ),
            "friday_signed_admission_and_processing": (
                durable.get("backend_user_message_count") == 1 and durable.get("idempotency_complete") is True
            ),
            "botapi_outbound_receipt": (
                durable.get("botapi_receipt_count") == 1
                and durable.get("botapi_receipt_message_id") in receipt_ids
            ),
            "independent_visible_destination_readback": (
                observer.get("window_complete") is True
                and observer.get("matching_canary_count") == 1
                and observer.get("matching_outbound_message_id")
                == durable.get("visible_destination_message_id")
                == durable.get("botapi_receipt_message_id")
            ),
        }
        _require(all(four_legs.values()), "four_leg_reconstruction_failed")

        event_by_pair = {(row["stage"], row["state"]): row["facts"] for row in rows}
        _require(
            event_by_pair[("candidate", "VERIFIED")] == source_before
            and event_by_pair[("telegram_preflight", "VERIFIED")]
            == {
                "bot_user_id": access.bot_user_id,
                "webhook_empty": True,
                "pending_update_count": 0,
            }
            and event_by_pair[("server", "STARTED")].get("pid") == origins["server"]["pid"]
            and event_by_pair[("backend", "READY")].get("pid") == origins["server"]["pid"]
            and event_by_pair[("telegram-bridge", "STARTED")].get("pid") == origins["telegram-bridge"]["pid"]
            and event_by_pair[("bridge", "READY")].get("pid") == origins["telegram-bridge"]["pid"]
            and event_by_pair[("four_legs", "VERIFIED")]
            == {
                "telegram_update_id": durable["telegram_update_id"],
                "inbound_message_id": durable["telegram_message_id"],
                "outbound_message_id": durable["visible_destination_message_id"],
            },
            "event_fact_mismatch",
        )

        report = _inventory_json(payloads, "report.json")
        report_cleanup = [
            {
                key: cleanups[role][key]
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
        ]
        report_origin_keys = {
            "role",
            "pid",
            "candidate_sha",
            "cli_origin",
            "effect_guard_origin",
            "effect_guard_sha256",
            "effect_spec_sha256",
            "argv",
        }
        if getattr(policy.candidate, "installed_wheel", None) is not None:
            report_origin_keys |= {
                "installed_site_root",
                "wheel_sha256",
                "source_checkout_imported",
            }
        report_origins_expected = [
            {key: origins[role][key] for key in report_origin_keys} for role in ("server", "telegram-bridge")
        ]
        _require(
            report.get("schema") == telegram.REPORT_SCHEMA
            and report.get("assignment") == "ASTRA-R10-TELEGRAM-BUDGET-SOL-082"
            and report.get("attempt_id") == context["attempt_id"]
            and report.get("case_pass") is True
            and report.get("case_outcome") == "LIVE_TELEGRAM_ROUNDTRIP_OBSERVED"
            and report.get("reasons") == []
            and report.get("observer") == observer
            and report.get("durable") == durable
            and report.get("telegram_effects") == effect_summary
            and report.get("cleanup") == report_cleanup
            and report.get("origins") == report_origins_expected
            and report.get("candidate") == source_before
            and report.get("source_unchanged") is True
            and report.get("cleanup_clear") is True
            and report.get("GO") is False
            and report.get("release_ready") is False
            and report.get("full_gate_credit") is False,
            "driver_report_inconsistent",
        )
        elapsed = report.get("elapsed_s")
        controller_elapsed = (ended - started).total_seconds()
        _require(
            type(elapsed) in {int, float}
            and math.isfinite(elapsed)
            and 0 <= elapsed <= 600 + 2 * policy.budgets.cleanup_grace_s
            and elapsed <= controller_elapsed + 5
            and controller_elapsed <= 600 + 2 * policy.budgets.cleanup_grace_s + 5,
            "duration_invalid",
        )
        _require(
            controller["outcome"] == "completed"
            and type(controller["leader_returncode"]) is int
            and controller["leader_returncode"] == 0
            and controller["error_code"] is None,
            "controller_exit_mismatch",
        )

        try:
            source_after = (
                telegram._verified_source_map(policy.candidate, policy.harness)
                if getattr(policy, "harness", None) is not None
                else telegram._verified_source_map(policy.candidate)
            )
        except (OSError, ValueError, telegram.RoundtripError) as exc:
            raise _acceptance_error("candidate_source_changed") from exc
        _require(source_after == source_before, "candidate_source_changed")
        _source_digest(Path(context["driver"]["path"]), context["driver"]["sha256"], 4 << 20)
        _read_bytes(
            observer_path,
            observer_ref["sha256"],
            maximum=128 << 10,
        )
        _read_bytes(
            Path(context["access"]["path"]),
            context["access"]["sha256"],
            maximum=65536,
        )
        _read_bytes(
            policy.contour.env_file,
            context["expected"]["env_sha256"],
            maximum=256 << 10,
        )
        _require(
            _secure_tree(
                Path(evidence["root"]),
                evidence["files"],
                str(evidence["inventory_sha256"]),
                evidence["total_bytes"],
            )
            == payloads,
            "evidence_changed_during_read",
        )
        _read_bytes(context_path, context_sha256, maximum=MAX_CONTEXT_BYTES)
        _read_bytes(receipt_path, receipt_sha256, maximum=MAX_RECEIPT_BYTES)

        return _status(
            "PASS",
            None,
            evidence_status="EVIDENCE_COMPLETE",
            first_attempt_verified=True,
            four_legs=four_legs,
            effect_attempts=effect_summary.get("attempts"),
            controller_cleanup_clear=True,
            source_unchanged=True,
        )
    except _EvidenceBlocked as exc:
        return _status("BLOCKED", str(exc), evidence_status="EVIDENCE_BLOCKED")
    except AcceptanceError as exc:
        return _status("FAIL", str(exc), evidence_status="EVIDENCE_INVALID")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        return _status(
            "FAIL",
            "telegram_reader_boundary_error:" + type(exc).__name__,
            evidence_status="EVIDENCE_INVALID",
        )
