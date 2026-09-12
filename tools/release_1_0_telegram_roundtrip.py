#!/usr/bin/env python3
"""Finite live Telegram roundtrip witness for one isolated Friday candidate.

This executable intentionally uses only the Python standard library.  It starts
one pinned Friday backend and one pinned Telegram bridge in an isolated home,
but it never impersonates a Telegram user and never treats Bot API transport as
user-visible delivery.  A separate real-user observer supplies the inbound
message and the destination readback.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "friday.release-1-0-telegram-roundtrip-policy.v1"
ACCESS_SCHEMA = "friday.release-1-0-telegram-dedicated-access.v1"
OBSERVER_REQUEST_SCHEMA = "friday.release-1-0-telegram-observer-request.v1"
OBSERVER_RESULT_SCHEMA = "friday.release-1-0-telegram-observer-result.v1"
REPORT_SCHEMA = "friday.release-1-0-telegram-roundtrip-report.v1"
EVENT_SCHEMA = "friday.release-1-0-telegram-roundtrip-event.v1"
MODULE_RELATIVE_PATH = "tools/release_1_0_telegram_roundtrip.py"
CLI_RELATIVE_PATH = "friday/cli.py"
BRIDGE_BASE_RELATIVE_PATH = "friday/telegram_bridge/_base.py"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
ATTEMPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,79}")
CANARY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{23,119}")
SAFE_CONTOUR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,79}")
OWNER_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "jericho://owner"))
BOT_API_ROOT = "https://api.telegram.org"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_OBSERVER_BYTES = 128 * 1024
EFFECT_LEDGER_SCHEMA = "friday.release-1-0-telegram-effect-ledger.v1"
EFFECT_LEDGER_RESERVATION_BYTES = 512 * 1024
EFFECT_LEDGER_SLOT_BYTES = 64
EFFECT_REQUEST_MAX_BYTES = 64 * 1024
EFFECT_RESPONSE_MAX_BYTES = 1024 * 1024
EFFECT_DURATION_LIMIT_S = 600
EFFECT_TOTAL_ATTEMPT_CAP = 433
EFFECT_METHOD_CAPS: dict[str, dict[str, int]] = {
    "driver_preflight": {"getMe": 1, "getWebhookInfo": 1},
    "bridge": {
        "getMe": 1,
        "setMyCommands": 1,
        "getUpdates": 20,
        "sendMessage": 2,
        "sendChatAction": 151,
        "editMessageText": 256,
    },
}
EFFECT_CHAT_METHODS = frozenset({"sendMessage", "sendChatAction", "editMessageText"})
EFFECT_WRITE_METHODS = frozenset({"setMyCommands", "sendMessage", "sendChatAction", "editMessageText"})
EFFECT_PROVEN_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 422, 429})


class RoundtripError(RuntimeError):
    """Expected fail-closed outcome with a public, secret-free reason."""

    def __init__(self, outcome: str, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.outcome = outcome
        self.code = code
        self.detail = detail


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{where} must be an object")
    return dict(value)


def _exact_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    where: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise ValueError(f"{where} keys invalid: missing={missing}, extra={extra}")


def _string(value: object, where: str, *, minimum: int = 1, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{where} length is outside policy")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{where} contains a control separator")
    return value


def _integer(value: object, where: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{where} is outside policy")
    return value


def _number(value: object, where: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{where} is outside policy")
    return result


def _boolean(value: object, where: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{where} must be a boolean")
    return value


def _absolute_path(value: object, where: str) -> Path:
    raw = _string(value, where)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{where} must be absolute")
    return Path(os.path.abspath(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, *, maximum: int | None = None) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise ValueError(f"{path} exceeds the byte budget")
            digest.update(chunk)
    return digest.hexdigest()


def _secure_regular_file(path: Path, where: str, *, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{where} is missing") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError(f"{where} must be a regular non-symlink file")
    if info.st_uid != os.getuid():
        raise ValueError(f"{where} has a foreign owner")
    if info.st_mode & 0o077:
        raise ValueError(f"{where} must not be accessible by group or others")
    if info.st_size > maximum:
        raise ValueError(f"{where} exceeds the byte budget")
    return info


def _secure_directory(path: Path, where: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{where} is missing") from exc
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"{where} must be a real directory")
    if info.st_uid != os.getuid():
        raise ValueError(f"{where} has a foreign owner")
    if info.st_mode & 0o077:
        raise ValueError(f"{where} must not be accessible by group or others")
    return info


def _load_json(path: Path, where: str, *, maximum: int, private: bool) -> dict[str, Any]:
    if private:
        _secure_regular_file(path, where, maximum=maximum)
    else:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > maximum:
            raise ValueError(f"{where} must be a bounded regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where} is not valid UTF-8 JSON") from exc
    return _mapping(value, where)


def _parse_utc(value: object, where: str) -> dt.datetime:
    raw = _string(value, where, maximum=64)
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{where} must include an offset")
    return parsed.astimezone(dt.UTC)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_new_private(path: Path, value: object) -> int:
    payload = _json_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return len(payload)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _effect_url_parts(raw_url: str) -> tuple[str, str, int | None, str, str, str]:
    try:
        parsed = urllib.parse.urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise RoundtripError("FAIL", "telegram_effect_url_invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise RoundtripError("FAIL", "telegram_effect_url_credentials_refused")
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        port,
        parsed.path,
        parsed.query,
        parsed.fragment,
    )


def _durable_effect_write(root: Path, path: Path, document: Mapping[str, Any]) -> None:
    payload = _json_bytes(document)
    if len(payload) > 4096:
        raise RoundtripError("FAIL", "telegram_effect_record_too_large")
    reservation = root / "reservation.bin"
    try:
        descriptor = os.open(reservation, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = os.fstat(descriptor).st_size
            claimed = (
                (len(payload) + EFFECT_LEDGER_SLOT_BYTES - 1) // EFFECT_LEDGER_SLOT_BYTES
            ) * EFFECT_LEDGER_SLOT_BYTES
            if claimed <= 0 or current < claimed:
                raise RoundtripError("FAIL", "telegram_effect_evidence_reservation_exhausted")
            os.ftruncate(descriptor, current - claimed)
            os.fsync(descriptor)
        finally:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        _write_new_private(path, document)
        _fsync_directory(path.parent)
    except RoundtripError:
        raise
    except OSError as exc:
        raise RoundtripError("FAIL", "telegram_effect_record_write_failed") from exc
    except BaseException as exc:
        raise RoundtripError("FAIL", "telegram_effect_record_write_failed") from exc


def _validated_effect_spec(document: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "attempt_id",
        "candidate_sha",
        "contour_id",
        "env_sha256",
        "bot_user_id",
        "chat_id",
        "user_id",
        "token_sha256",
        "telegram_origin",
        "backend_origin",
        "created_monotonic_ns",
        "deadline_monotonic_ns",
        "duration_limit_s",
        "request_max_bytes",
        "response_max_bytes",
        "inbound_unique_messages",
        "method_attempt_caps",
        "max_total_attempts",
        "expected_inbound_sha256",
        "unknown_effect_policy",
        "spec_hmac_sha256",
    }
    _exact_keys(document, required=required, where="Telegram effect spec")
    result = dict(document)
    if (
        result["schema"] != EFFECT_LEDGER_SCHEMA
        or result["method_attempt_caps"] != EFFECT_METHOD_CAPS
        or result["max_total_attempts"] != EFFECT_TOTAL_ATTEMPT_CAP
        or result["duration_limit_s"] != EFFECT_DURATION_LIMIT_S
        or result["request_max_bytes"] != EFFECT_REQUEST_MAX_BYTES
        or result["response_max_bytes"] != EFFECT_RESPONSE_MAX_BYTES
        or result["inbound_unique_messages"] != 1
        or result["telegram_origin"] != BOT_API_ROOT
        or result["unknown_effect_policy"] != "latch_and_never_retry"
        or SHA256_RE.fullmatch(str(result["token_sha256"])) is None
        or SHA256_RE.fullmatch(str(result["expected_inbound_sha256"])) is None
        or SHA256_RE.fullmatch(str(result["spec_hmac_sha256"])) is None
        or isinstance(result["deadline_monotonic_ns"], bool)
        or not isinstance(result["deadline_monotonic_ns"], int)
        or isinstance(result["created_monotonic_ns"], bool)
        or not isinstance(result["created_monotonic_ns"], int)
        or not (
            0
            < result["deadline_monotonic_ns"] - result["created_monotonic_ns"]
            <= EFFECT_DURATION_LIMIT_S * 1_000_000_000
        )
    ):
        raise RoundtripError("FAIL", "telegram_effect_spec_invalid")
    return result


def _effect_spec_document(
    *,
    policy: Policy,
    access: AccessManifest,
    token: str,
    deadline_monotonic_ns: int,
) -> dict[str, Any]:
    inbound_text = (
        f"Friday roundtrip {policy.canary}. Reply with exactly this marker and nothing else: {policy.canary}"
    )
    document = {
        "schema": EFFECT_LEDGER_SCHEMA,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "contour_id": policy.contour.contour_id,
        "env_sha256": policy.contour.env_file_sha256,
        "bot_user_id": access.bot_user_id,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "token_sha256": _sha256_bytes(token.encode("utf-8")),
        "telegram_origin": BOT_API_ROOT,
        "backend_origin": policy.contour.backend_origin,
        "created_monotonic_ns": time.monotonic_ns(),
        "deadline_monotonic_ns": deadline_monotonic_ns,
        "duration_limit_s": EFFECT_DURATION_LIMIT_S,
        "request_max_bytes": EFFECT_REQUEST_MAX_BYTES,
        "response_max_bytes": EFFECT_RESPONSE_MAX_BYTES,
        "inbound_unique_messages": 1,
        "method_attempt_caps": EFFECT_METHOD_CAPS,
        "max_total_attempts": EFFECT_TOTAL_ATTEMPT_CAP,
        "expected_inbound_sha256": _sha256_bytes(inbound_text.encode("utf-8")),
        "unknown_effect_policy": "latch_and_never_retry",
    }
    document["spec_hmac_sha256"] = hmac.new(
        token.encode("utf-8"),
        _json_bytes(document),
        hashlib.sha256,
    ).hexdigest()
    return document


def _verify_effect_spec_hmac(spec: Mapping[str, Any], token: str) -> None:
    unsigned = dict(spec)
    received = str(unsigned.pop("spec_hmac_sha256", ""))
    expected = hmac.new(token.encode("utf-8"), _json_bytes(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise RoundtripError("FAIL", "telegram_effect_spec_tampered")


def prepare_effect_ledger(
    evidence: EvidenceStore,
    *,
    policy: Policy,
    access: AccessManifest,
    token: str,
    deadline_monotonic_ns: int,
) -> tuple[Path, TelegramEffectLedger]:
    evidence.reserve_external_tree(EFFECT_LEDGER_RESERVATION_BYTES)
    root = evidence.root / "telegram-effects"
    os.mkdir(root, 0o700)
    for role in EFFECT_METHOD_CAPS:
        os.mkdir(root / role, 0o700)
    reservation = root / "reservation.bin"
    descriptor = os.open(reservation, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if not hasattr(os, "posix_fallocate"):
            raise RoundtripError("NOT_RUN", "telegram_effect_fallocate_unavailable")
        os.posix_fallocate(descriptor, 0, EFFECT_LEDGER_RESERVATION_BYTES)
        info = os.fstat(descriptor)
        if info.st_size != EFFECT_LEDGER_RESERVATION_BYTES or (
            hasattr(info, "st_blocks") and info.st_blocks * 512 < EFFECT_LEDGER_RESERVATION_BYTES
        ):
            raise RoundtripError("NOT_RUN", "telegram_effect_physical_reservation_failed")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(root)
    spec = _effect_spec_document(
        policy=policy,
        access=access,
        token=token,
        deadline_monotonic_ns=deadline_monotonic_ns,
    )
    spec_path = root / "spec.json"
    _durable_effect_write(root, spec_path, spec)
    return spec_path, TelegramEffectLedger(root, "driver_preflight", token)


def _effect_record_inventory(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for role in sorted(EFFECT_METHOD_CAPS):
        role_root = root / role
        _secure_directory(role_root, "Telegram effect role ledger")
        for path in sorted(role_root.iterdir(), key=lambda item: item.name):
            info = _secure_regular_file(path, "Telegram effect record", maximum=4096)
            if not stat.S_ISREG(info.st_mode):
                raise RoundtripError("FAIL", "telegram_effect_record_invalid")
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(sha256_file(path).encode("ascii"))
            count += 1
    return digest.hexdigest(), count


def seal_effect_ledger(root: Path, env_file: Path) -> Path:
    if (root / "seal.json").exists() or (root / "seal.json").is_symlink():
        raise RoundtripError("FAIL", "telegram_effect_ledger_already_sealed")
    spec = _validated_effect_spec(
        _load_json(root / "spec.json", "Telegram effect spec", maximum=16384, private=True)
    )
    env = _parse_env_file(env_file)
    token = env.get("FRIDAY_TELEGRAM_BOT_TOKEN", "").strip()
    _verify_effect_spec_hmac(spec, token)
    inventory_sha256, record_count = _effect_record_inventory(root)
    unsigned = {
        "schema": EFFECT_LEDGER_SCHEMA,
        "kind": "seal",
        "attempt_id": spec["attempt_id"],
        "inventory_sha256": inventory_sha256,
        "record_count": record_count,
        "sealed_at_ns": time.time_ns(),
    }
    document = {
        **unsigned,
        "hmac_sha256": hmac.new(token.encode("utf-8"), _json_bytes(unsigned), hashlib.sha256).hexdigest(),
    }
    path = root / "seal.json"
    _durable_effect_write(root, path, document)
    return path


class TelegramEffectLedger:
    """Fail-closed per-process guard around the original Telegram transport."""

    def __init__(self, root: Path, role: str, token: str) -> None:
        if role not in EFFECT_METHOD_CAPS:
            raise ValueError("invalid Telegram effect role")
        self.root = root
        self.role = role
        self.role_root = root / role
        self._lock = threading.RLock()
        self._latched = ""
        self._sequence = 0
        self._counts: dict[str, int] = {}
        self._active: dict[int, dict[str, Any]] = {}
        self._receipt_ids: set[int] = set()
        self._update_ids: set[int] = set()
        spec = _load_json(root / "spec.json", "Telegram effect spec", maximum=16384, private=True)
        self.spec = _validated_effect_spec(spec)
        if self.spec["token_sha256"] != _sha256_bytes(token.encode("utf-8")):
            raise RoundtripError("NOT_RUN", "telegram_effect_token_binding_mismatch")
        self._token = token
        _verify_effect_spec_hmac(self.spec, token)

    @property
    def latched(self) -> str:
        return self._latched

    def _record(self, name: str, document: Mapping[str, Any]) -> None:
        unsigned = dict(document)
        signed = {
            **unsigned,
            "hmac_sha256": hmac.new(
                self._token.encode("utf-8"),
                _json_bytes(unsigned),
                hashlib.sha256,
            ).hexdigest(),
        }
        _durable_effect_write(self.root, self.role_root / name, signed)

    def _violate(self, code: str) -> None:
        with self._lock:
            if not self._latched:
                self._latched = code
                with suppress(RoundtripError, OSError):
                    self._record(
                        f"violation-{time.time_ns()}.json",
                        {"v": 1, "kind": "violation", "role": self.role, "code": code},
                    )
        raise RoundtripError("FAIL", code)

    def classify_bridge_url(self, raw_url: str) -> str:
        try:
            scheme, host, port, path, query, fragment = _effect_url_parts(raw_url)
        except RoundtripError as exc:
            self._violate(exc.code)
        backend = _effect_url_parts(str(self.spec["backend_origin"]))
        backend_path = backend[3].rstrip("/")
        if (scheme, host, port) == backend[:3] and (
            path == backend_path or path.startswith(backend_path + "/")
        ):
            if query or fragment:
                self._violate("telegram_effect_backend_url_invalid")
            return "backend"
        if scheme != "https" or host != "api.telegram.org" or port not in {None, 443}:
            self._violate("telegram_effect_origin_mismatch")
        prefix = f"/bot{self._token}/"
        if not path.startswith(prefix) or query or fragment:
            self._violate("telegram_effect_token_or_endpoint_mismatch")
        method = path[len(prefix) :]
        if not method or "/" in method:
            self._violate("telegram_effect_method_unclassified")
        return method

    def check_backend(self) -> None:
        with self._lock:
            if self._latched:
                raise RoundtripError("FAIL", self._latched)
            if time.monotonic_ns() >= int(self.spec["deadline_monotonic_ns"]):
                self._violate("telegram_effect_deadline_exceeded")

    def remaining_seconds(self, maximum: float | None = None) -> float:
        with self._lock:
            if self._latched:
                raise RoundtripError("FAIL", self._latched)
            remaining = (int(self.spec["deadline_monotonic_ns"]) - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                self._violate("telegram_effect_deadline_exceeded")
            return max(0.001, min(remaining, maximum if maximum is not None else remaining))

    def begin(self, method: str, verb: str, body: bytes, *, origin: str = BOT_API_ROOT) -> int:
        with self._lock:
            if self._latched:
                raise RoundtripError("FAIL", self._latched)
            if (self.root / "seal.json").exists() or (self.root / "seal.json").is_symlink():
                self._latched = "telegram_effect_ledger_sealed"
                raise RoundtripError("FAIL", self._latched)
            if time.monotonic_ns() >= int(self.spec["deadline_monotonic_ns"]):
                self._violate("telegram_effect_deadline_exceeded")
            caps = EFFECT_METHOD_CAPS[self.role]
            if method not in caps:
                self._violate("telegram_effect_method_unclassified")
            if origin != self.spec["telegram_origin"]:
                self._violate("telegram_effect_origin_mismatch")
            expected_verb = "GET" if self.role == "driver_preflight" else "POST"
            if verb.upper() != expected_verb:
                self._violate("telegram_effect_http_verb_mismatch")
            if len(body) > EFFECT_REQUEST_MAX_BYTES:
                self._violate("telegram_effect_request_too_large")
            try:
                payload = json.loads(body) if body else {}
            except (UnicodeError, json.JSONDecodeError):
                self._violate("telegram_effect_request_json_invalid")
            if not isinstance(payload, dict):
                self._violate("telegram_effect_request_shape_invalid")
            chat_id: int | None = None
            message_id: int | None = None
            if method in EFFECT_CHAT_METHODS:
                raw_chat = payload.get("chat_id")
                try:
                    chat_id = int(raw_chat)
                except (TypeError, ValueError):
                    self._violate("telegram_effect_chat_invalid")
                if isinstance(raw_chat, bool) or chat_id != int(self.spec["chat_id"]):
                    self._violate("telegram_effect_chat_mismatch")
            if method == "editMessageText":
                raw_message = payload.get("message_id")
                if isinstance(raw_message, bool) or not isinstance(raw_message, int):
                    self._violate("telegram_effect_edit_receipt_missing")
                message_id = raw_message
                if message_id not in self._receipt_ids:
                    self._violate("telegram_effect_edit_receipt_not_owned")
            count = self._counts.get(method, 0) + 1
            if count > caps[method] or sum(self._counts.values()) + 1 > sum(caps.values()):
                self._violate("telegram_effect_attempt_cap_exceeded")
            self._counts[method] = count
            self._sequence += 1
            sequence = self._sequence
            record = {
                "v": 1,
                "kind": "start",
                "role": self.role,
                "seq": sequence,
                "method": method,
                "request_bytes": len(body),
                "request_sha256": _sha256_bytes(body),
                "origin_sha256": _sha256_bytes(origin.encode("utf-8")),
                "token_sha256": self.spec["token_sha256"],
                "chat_id": chat_id,
                "message_id": message_id,
                "at_ns": time.time_ns(),
            }
            self._record(f"{sequence:06d}-start.json", record)
            self._active[sequence] = record
            return sequence

    def not_dispatched(self, sequence: int, reason: str) -> None:
        self._finish_record(sequence, "not_dispatched", 0, b"", reason=reason[:80])

    def unknown(self, sequence: int, reason: str, body: bytes | None = None) -> None:
        with self._lock:
            if sequence not in self._active:
                return
            record = self._active.pop(sequence)
            self._latched = "telegram_effect_unknown"
            facts: dict[str, Any] = {}
            if body is not None:
                facts = {
                    "response_bytes": len(body),
                    "response_sha256": _sha256_bytes(body),
                }
            self._record(
                f"{sequence:06d}-unknown.json",
                {
                    "v": 1,
                    "kind": "unknown",
                    "role": self.role,
                    "seq": sequence,
                    "method": record["method"],
                    "reason": reason[:80],
                    "at_ns": time.time_ns(),
                    **facts,
                },
            )

    def finish(self, sequence: int, status: int, body: bytes) -> dict[str, Any]:
        if len(body) > EFFECT_RESPONSE_MAX_BYTES:
            self.unknown(sequence, "response_too_large", body)
            raise RoundtripError("FAIL", "telegram_effect_response_too_large")
        with self._lock:
            record = self._active.get(sequence)
            if record is None:
                self._violate("telegram_effect_terminal_without_start")
            method = str(record["method"])
            if status < 200 or status >= 300:
                if method in EFFECT_WRITE_METHODS and status not in EFFECT_PROVEN_REJECTION_STATUSES:
                    ambiguous = True
                else:
                    return self._finish_record_locked(
                        sequence, "rejected", status, body, reason="http_response"
                    )
            else:
                ambiguous = False
            if not ambiguous:
                try:
                    document = json.loads(body)
                except (UnicodeError, json.JSONDecodeError):
                    document = None
                if not isinstance(document, dict) or document.get("ok") is not True:
                    if method in EFFECT_WRITE_METHODS:
                        ambiguous = True
                    else:
                        return self._finish_record_locked(
                            sequence, "rejected", status, body, reason="telegram_rejected"
                        )
                if not ambiguous:
                    try:
                        summary = self._validate_success_locked(method, document)
                    except RoundtripError as exc:
                        self.unknown(sequence, exc.code, body)
                        raise
                    return self._finish_record_locked(sequence, "ok", status, body, **summary)
        self.unknown(sequence, "ambiguous_response", body)
        raise RoundtripError("FAIL", "telegram_effect_unknown")

    def _validate_success_locked(self, method: str, document: Mapping[str, Any]) -> dict[str, Any]:
        result = document.get("result")
        summary: dict[str, Any] = {}
        if method in {"getMe", "getWebhookInfo"} and not isinstance(result, dict):
            self._violate("telegram_effect_response_shape_invalid")
        if method == "getMe" and result.get("id") != int(self.spec["bot_user_id"]):
            self._violate("telegram_effect_bot_identity_mismatch")
        if method in {"setMyCommands", "sendChatAction"} and result is not True:
            self._violate("telegram_effect_response_shape_invalid")
        if method == "sendMessage":
            if not isinstance(result, dict):
                self._violate("telegram_effect_send_receipt_invalid")
            message_id = result.get("message_id")
            chat = result.get("chat")
            returned_chat = chat.get("id") if isinstance(chat, dict) else None
            if (
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                or isinstance(returned_chat, bool)
                or not isinstance(returned_chat, int)
                or returned_chat != int(self.spec["chat_id"])
                or message_id in self._receipt_ids
            ):
                self._violate("telegram_effect_send_receipt_invalid")
            self._receipt_ids.add(message_id)
            summary["receipt_message_id"] = message_id
        elif method == "editMessageText":
            returned_chat = result.get("chat") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("message_id") not in self._receipt_ids
                or not isinstance(returned_chat, dict)
                or returned_chat.get("id") != int(self.spec["chat_id"])
            ):
                self._violate("telegram_effect_edit_receipt_invalid")
            summary["receipt_message_id"] = int(result["message_id"])
        elif method == "getUpdates":
            if not isinstance(result, list):
                self._violate("telegram_effect_updates_shape_invalid")
            update_ids: list[int] = []
            message_ids: list[int] = []
            for update in result:
                if not isinstance(update, dict):
                    self._violate("telegram_effect_update_unclassified")
                update_id = update.get("update_id")
                message = update.get("message")
                if (
                    isinstance(update_id, bool)
                    or not isinstance(update_id, int)
                    or update_id in self._update_ids
                    or not isinstance(message, dict)
                ):
                    self._violate("telegram_effect_update_unclassified")
                sender = message.get("from")
                chat = message.get("chat")
                message_id = message.get("message_id")
                text = message.get("text")
                if (
                    not isinstance(sender, dict)
                    or sender.get("id") != int(self.spec["user_id"])
                    or not isinstance(chat, dict)
                    or chat.get("id") != int(self.spec["chat_id"])
                    or isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or not isinstance(text, str)
                    or _sha256_bytes(text.encode("utf-8")) != self.spec["expected_inbound_sha256"]
                ):
                    self._violate("telegram_effect_update_unclassified")
                self._update_ids.add(update_id)
                update_ids.append(update_id)
                message_ids.append(message_id)
            if len(self._update_ids) > int(self.spec["inbound_unique_messages"]):
                self._violate("telegram_effect_inbound_cap_exceeded")
            summary["update_ids"] = update_ids
            summary["message_ids"] = message_ids
        return summary

    def _finish_record(
        self,
        sequence: int,
        state: str,
        status: int,
        body: bytes,
        **facts: Any,
    ) -> dict[str, Any]:
        with self._lock:
            return self._finish_record_locked(sequence, state, status, body, **facts)

    def _finish_record_locked(
        self,
        sequence: int,
        state: str,
        status: int,
        body: bytes,
        **facts: Any,
    ) -> dict[str, Any]:
        record = self._active.get(sequence)
        if record is None:
            self._violate("telegram_effect_terminal_without_start")
        terminal = {
            "v": 1,
            "kind": "terminal",
            "role": self.role,
            "seq": sequence,
            "method": record["method"],
            "state": state,
            "status": status,
            "response_bytes": len(body),
            "response_sha256": _sha256_bytes(body),
            "at_ns": time.time_ns(),
            **facts,
        }
        self._record(f"{sequence:06d}-terminal.json", terminal)
        self._active.pop(sequence, None)
        return terminal


class TelegramEffectStop(BaseException):
    """A stop that escapes Friday's best-effort Exception handlers."""


def install_bridge_effect_guard(
    httpx_module: Any, spec_path: Path, env_file: Path
) -> tuple[TelegramEffectLedger, Any]:
    import asyncio

    spec = _load_json(spec_path, "Telegram effect spec", maximum=16384, private=True)
    _validated_effect_spec(spec)
    if sha256_file(env_file, maximum=256 * 1024) != spec["env_sha256"]:
        raise TelegramEffectStop("telegram_effect_env_changed")
    env = _parse_env_file(env_file)
    token = env.get("FRIDAY_TELEGRAM_BOT_TOKEN", "").strip()
    guard = TelegramEffectLedger(spec_path.parent, "bridge", token)
    original_send = httpx_module.AsyncClient.send

    async def guarded_send(client: Any, request: Any, **kwargs: Any) -> Any:
        try:
            classified = guard.classify_bridge_url(str(request.url))
            follow = kwargs.get("follow_redirects")
            effective_follow = (
                follow if isinstance(follow, bool) else bool(getattr(client, "follow_redirects", False))
            )
            if effective_follow:
                guard._violate("telegram_effect_redirects_enabled")
            if classified == "backend":
                guard.check_backend()
                try:
                    return await asyncio.wait_for(
                        original_send(client, request, **kwargs),
                        timeout=guard.remaining_seconds(),
                    )
                except TimeoutError:
                    with suppress(RoundtripError):
                        guard._violate("telegram_effect_deadline_exceeded")
                    raise TelegramEffectStop("telegram_effect_deadline_exceeded") from None
            if kwargs.get("stream") is True:
                guard._violate("telegram_effect_streaming_response_refused")
            try:
                body = bytes(request.content)
            except BaseException:
                guard._violate("telegram_effect_request_body_unavailable")
            sequence = guard.begin(classified, str(request.method), body)
            try:
                response = await asyncio.wait_for(
                    original_send(client, request, **kwargs),
                    timeout=guard.remaining_seconds(),
                )
            except httpx_module.ConnectError as exc:
                guard.not_dispatched(sequence, type(exc).__name__)
                raise
            except BaseException as exc:
                guard.unknown(sequence, type(exc).__name__)
                raise TelegramEffectStop("telegram_effect_unknown") from None
            try:
                guard.finish(sequence, int(response.status_code), bytes(response.content))
            except RoundtripError as exc:
                guard.unknown(sequence, exc.code)
                raise
            return response
        except TelegramEffectStop:
            raise
        except RoundtripError as exc:
            raise TelegramEffectStop(exc.code) from None
        except Exception:
            with suppress(RoundtripError):
                guard._violate("telegram_effect_guard_internal_failure")
            raise TelegramEffectStop("telegram_effect_guard_internal_failure") from None

    httpx_module.AsyncClient.send = guarded_send
    return guard, original_send


def inspect_effect_ledger(
    root: Path,
    *,
    policy: Policy,
    access: AccessManifest,
    observer: Mapping[str, Any] | None = None,
    durable: Mapping[str, Any] | None = None,
    require_complete: bool,
) -> dict[str, Any]:
    _secure_directory(root, "Telegram effect ledger")
    spec = _validated_effect_spec(
        _load_json(root / "spec.json", "Telegram effect spec", maximum=16384, private=True)
    )
    env = _parse_env_file(policy.contour.env_file)
    token = env.get("FRIDAY_TELEGRAM_BOT_TOKEN", "").strip()
    expected_inbound = (
        f"Friday roundtrip {policy.canary}. Reply with exactly this marker and nothing else: {policy.canary}"
    )
    expected = {
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "contour_id": policy.contour.contour_id,
        "env_sha256": policy.contour.env_file_sha256,
        "bot_user_id": access.bot_user_id,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "token_sha256": _sha256_bytes(token.encode("utf-8")),
        "backend_origin": policy.contour.backend_origin,
        "expected_inbound_sha256": _sha256_bytes(expected_inbound.encode("utf-8")),
    }
    if any(spec.get(key) != value for key, value in expected.items()):
        raise RoundtripError("FAIL", "telegram_effect_spec_binding_mismatch")
    _verify_effect_spec_hmac(spec, token)
    seal = _load_json(root / "seal.json", "Telegram effect ledger seal", maximum=4096, private=True)
    received_seal_hmac = str(seal.pop("hmac_sha256", ""))
    expected_seal_hmac = hmac.new(token.encode("utf-8"), _json_bytes(seal), hashlib.sha256).hexdigest()
    inventory_digest, inventory_count = _effect_record_inventory(root)
    if (
        not hmac.compare_digest(received_seal_hmac, expected_seal_hmac)
        or seal.get("schema") != EFFECT_LEDGER_SCHEMA
        or seal.get("kind") != "seal"
        or seal.get("attempt_id") != policy.attempt_id
        or seal.get("inventory_sha256") != inventory_digest
        or seal.get("record_count") != inventory_count
    ):
        raise RoundtripError("FAIL", "telegram_effect_ledger_seal_invalid")
    _secure_regular_file(
        root / "reservation.bin",
        "Telegram effect reservation",
        maximum=EFFECT_LEDGER_RESERVATION_BYTES,
    )
    starts: dict[tuple[str, int], dict[str, Any]] = {}
    endings: dict[tuple[str, int], dict[str, Any]] = {}
    violations: list[str] = []
    unknown_at: list[int] = []
    counts: dict[str, dict[str, int]] = {role: {} for role in EFFECT_METHOD_CAPS}
    successful: dict[str, dict[str, int]] = {role: {} for role in EFFECT_METHOD_CAPS}
    request_digests: dict[str, dict[str, list[str]]] = {role: {} for role in EFFECT_METHOD_CAPS}
    receipt_sequences: dict[int, int] = {}
    update_ids: list[int] = []
    message_ids: list[int] = []
    inventory_sha = hashlib.sha256()
    total_bytes = 0
    for directory, names, files in os.walk(root):
        names.sort()
        base = Path(directory)
        _secure_directory(base, "Telegram effect ledger directory")
        for name in names:
            if (base / name).is_symlink():
                raise RoundtripError("FAIL", "telegram_effect_ledger_symlink")
        for name in sorted(files):
            path = base / name
            info = _secure_regular_file(
                path,
                "Telegram effect ledger file",
                maximum=EFFECT_LEDGER_RESERVATION_BYTES,
            )
            total_bytes += info.st_size
            relative = path.relative_to(root).as_posix()
            inventory_sha.update(relative.encode("utf-8") + b"\0")
            inventory_sha.update(sha256_file(path).encode("ascii"))
            if path in {root / "spec.json", root / "reservation.bin", root / "seal.json"}:
                continue
            if token.encode("utf-8") in path.read_bytes():
                raise RoundtripError("FAIL", "telegram_effect_secret_persisted")
            if base.parent != root or base.name not in EFFECT_METHOD_CAPS:
                raise RoundtripError("FAIL", "telegram_effect_ledger_path_invalid")
            role = base.name
            if name.startswith("violation-") and name.endswith(".json"):
                document = _load_json(path, "Telegram effect violation", maximum=4096, private=True)
                received_hmac = document.pop("hmac_sha256", "")
                expected_hmac = hmac.new(
                    token.encode("utf-8"), _json_bytes(document), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(str(received_hmac), expected_hmac):
                    raise RoundtripError("FAIL", "telegram_effect_record_tampered")
                if document.get("kind") != "violation" or document.get("role") != role:
                    raise RoundtripError("FAIL", "telegram_effect_violation_invalid")
                violations.append(str(document.get("code") or "unknown_violation"))
                continue
            match = re.fullmatch(r"([0-9]{6})-(start|terminal|unknown)\.json", name)
            if match is None:
                raise RoundtripError("FAIL", "telegram_effect_ledger_filename_invalid")
            sequence = int(match.group(1))
            kind = match.group(2)
            document = _load_json(path, "Telegram effect record", maximum=4096, private=True)
            received_hmac = document.pop("hmac_sha256", "")
            expected_hmac = hmac.new(token.encode("utf-8"), _json_bytes(document), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(received_hmac), expected_hmac):
                raise RoundtripError("FAIL", "telegram_effect_record_tampered")
            if (
                document.get("v") != 1
                or document.get("kind") != kind
                or document.get("role") != role
                or document.get("seq") != sequence
            ):
                raise RoundtripError("FAIL", "telegram_effect_record_binding_mismatch")
            key = (role, sequence)
            if kind == "start":
                if key in starts:
                    raise RoundtripError("FAIL", "telegram_effect_duplicate_start")
                method = document.get("method")
                if method not in EFFECT_METHOD_CAPS[role]:
                    raise RoundtripError("FAIL", "telegram_effect_method_unclassified")
                request_bytes = document.get("request_bytes")
                if (
                    isinstance(request_bytes, bool)
                    or not isinstance(request_bytes, int)
                    or not 0 <= request_bytes <= EFFECT_REQUEST_MAX_BYTES
                    or SHA256_RE.fullmatch(str(document.get("request_sha256"))) is None
                    or document.get("origin_sha256") != _sha256_bytes(BOT_API_ROOT.encode("utf-8"))
                    or document.get("token_sha256") != spec["token_sha256"]
                ):
                    raise RoundtripError("FAIL", "telegram_effect_start_invalid")
                if method in EFFECT_CHAT_METHODS and document.get("chat_id") != access.chat_id:
                    raise RoundtripError("FAIL", "telegram_effect_chat_mismatch")
                starts[key] = document
                role_counts = counts[role]
                role_counts[method] = role_counts.get(method, 0) + 1
                request_digests[role].setdefault(method, []).append(str(document["request_sha256"]))
                if role_counts[method] > EFFECT_METHOD_CAPS[role][method]:
                    raise RoundtripError("FAIL", "telegram_effect_attempt_cap_exceeded")
            else:
                if key in endings:
                    raise RoundtripError("FAIL", "telegram_effect_duplicate_terminal")
                endings[key] = document
                if kind == "unknown":
                    unknown_at.append(int(document.get("at_ns") or 0))
                else:
                    response_bytes = document.get("response_bytes")
                    if (
                        isinstance(response_bytes, bool)
                        or not isinstance(response_bytes, int)
                        or not 0 <= response_bytes <= EFFECT_RESPONSE_MAX_BYTES
                        or SHA256_RE.fullmatch(str(document.get("response_sha256"))) is None
                        or document.get("state") not in {"ok", "rejected", "not_dispatched"}
                    ):
                        raise RoundtripError("FAIL", "telegram_effect_terminal_invalid")
    if total_bytes > EFFECT_LEDGER_RESERVATION_BYTES:
        raise RoundtripError("FAIL", "telegram_effect_reservation_exceeded")
    unfinished = sorted(set(starts) ^ set(endings)) if set(starts) != set(endings) else []
    for role in EFFECT_METHOD_CAPS:
        sequences = sorted(sequence for record_role, sequence in starts if record_role == role)
        if sequences and sequences != list(range(1, sequences[-1] + 1)):
            raise RoundtripError("FAIL", "telegram_effect_sequence_gap")
    for key, start in starts.items():
        end = endings.get(key)
        if end is not None and end.get("method") != start.get("method"):
            raise RoundtripError("FAIL", "telegram_effect_terminal_method_mismatch")
        if end is None:
            continue
        if end.get("kind") != "terminal" or end.get("state") != "ok":
            continue
        method = str(start["method"])
        successful[key[0]][method] = successful[key[0]].get(method, 0) + 1
        if method == "sendMessage":
            receipt = end.get("receipt_message_id")
            if isinstance(receipt, bool) or not isinstance(receipt, int) or receipt in receipt_sequences:
                raise RoundtripError("FAIL", "telegram_effect_send_receipt_invalid")
            receipt_sequences[receipt] = key[1]
        elif method == "editMessageText":
            receipt = start.get("message_id")
            if (
                isinstance(receipt, bool)
                or not isinstance(receipt, int)
                or receipt not in receipt_sequences
                or receipt_sequences[receipt] >= key[1]
                or end.get("receipt_message_id") != receipt
            ):
                raise RoundtripError("FAIL", "telegram_effect_edit_receipt_not_owned")
        elif method == "getUpdates":
            returned_updates = end.get("update_ids", [])
            returned_messages = end.get("message_ids", [])
            if (
                not isinstance(returned_updates, list)
                or not isinstance(returned_messages, list)
                or len(returned_updates) != len(returned_messages)
                or not all(isinstance(item, int) and not isinstance(item, bool) for item in returned_updates)
                or not all(isinstance(item, int) and not isinstance(item, bool) for item in returned_messages)
            ):
                raise RoundtripError("FAIL", "telegram_effect_update_evidence_invalid")
            update_ids.extend(returned_updates)
            message_ids.extend(returned_messages)
    if len(update_ids) != len(set(update_ids)) or len(update_ids) > 1:
        raise RoundtripError("FAIL", "telegram_effect_inbound_cardinality_invalid")
    if unknown_at:
        first_unknown = min(unknown_at)
        if any(int(item.get("at_ns") or 0) > first_unknown for item in starts.values()):
            raise RoundtripError("FAIL", "telegram_effect_started_after_unknown")
    total_attempts = sum(sum(items.values()) for items in counts.values())
    if total_attempts > EFFECT_TOTAL_ATTEMPT_CAP:
        raise RoundtripError("FAIL", "telegram_effect_total_cap_exceeded")
    if require_complete:
        required_exact = {
            ("driver_preflight", "getMe"): 1,
            ("driver_preflight", "getWebhookInfo"): 1,
            ("bridge", "getMe"): 1,
            ("bridge", "setMyCommands"): 1,
        }
        if (
            violations
            or unknown_at
            or unfinished
            or any(counts[role].get(method, 0) != amount for (role, method), amount in required_exact.items())
            or any(
                successful[role].get(method, 0) != amount for (role, method), amount in required_exact.items()
            )
            or not 1 <= counts["bridge"].get("getUpdates", 0) <= EFFECT_METHOD_CAPS["bridge"]["getUpdates"]
            or not 1 <= counts["bridge"].get("sendMessage", 0) <= EFFECT_METHOD_CAPS["bridge"]["sendMessage"]
            or len(update_ids) != 1
            or not receipt_sequences
        ):
            raise RoundtripError("FAIL", "telegram_effect_ledger_incomplete")
        if observer is None or durable is None:
            raise RoundtripError("FAIL", "telegram_effect_reconciliation_missing")
        expected_receipt = durable.get("botapi_receipt_message_id")
        if (
            expected_receipt not in receipt_sequences
            or observer.get("matching_outbound_message_id") != expected_receipt
            or durable.get("visible_destination_message_id") != expected_receipt
            or durable.get("telegram_update_id") not in update_ids
            or durable.get("telegram_message_id") not in message_ids
        ):
            raise RoundtripError("FAIL", "telegram_effect_four_leg_reconciliation_failed")
    return {
        "schema": EFFECT_LEDGER_SCHEMA,
        "source": "independent_parent_reader",
        "attempts": total_attempts,
        "counts": counts,
        "successful_counts": successful,
        "request_sha256": request_digests,
        "unknown_count": len(unknown_at),
        "violations": violations,
        "unfinished": [f"{role}:{sequence}" for role, sequence in unfinished],
        "telegram_update_ids": update_ids,
        "telegram_message_ids": message_ids,
        "send_message_receipt_ids": sorted(receipt_sequences),
        "ledger_bytes": total_bytes,
        "inventory_sha256": inventory_sha.hexdigest(),
    }


@dataclasses.dataclass(frozen=True)
class CandidatePolicy:
    manifest_path: Path
    manifest_sha256: str
    source_root: Path
    candidate_sha: str
    candidate_tree: str
    suite_revision: str
    python_executable: Path
    python_sha256: str


@dataclasses.dataclass(frozen=True)
class ContourPolicy:
    contour_id: str
    isolation_root: Path
    friday_home: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path
    log_dir: Path
    env_file: Path
    env_file_sha256: str
    database_path: Path
    inbox_db_path: Path
    files_dir: Path
    backend_origin: str
    known_production_homes: tuple[Path, ...]
    foreign_lease_paths: tuple[Path, ...]


@dataclasses.dataclass(frozen=True)
class AccessPolicy:
    manifest_path: Path
    manifest_sha256: str


@dataclasses.dataclass(frozen=True)
class ObserverPolicy:
    mode: str
    result_path: Path
    adapter_argv: tuple[str, ...]
    adapter_executable_sha256: str


@dataclasses.dataclass(frozen=True)
class Budgets:
    timeout_s: float
    backend_ready_timeout_s: float
    poll_interval_s: float
    max_getupdates_rounds: int
    max_inbound_user_messages: int
    max_outbound_bot_posts: int
    max_evidence_bytes: int
    process_log_bytes: int
    cleanup_grace_s: float


@dataclasses.dataclass(frozen=True)
class Policy:
    attempt_id: str
    canary: str
    candidate: CandidatePolicy
    contour: ContourPolicy
    access: AccessPolicy
    observer: ObserverPolicy
    budgets: Budgets

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Policy:
        _exact_keys(
            document,
            required={
                "schema",
                "attempt_id",
                "canary",
                "candidate",
                "contour",
                "access",
                "observer",
                "budgets",
            },
            where="policy",
        )
        if document["schema"] != POLICY_SCHEMA:
            raise ValueError("policy schema mismatch")
        attempt_id = _string(document["attempt_id"], "attempt_id", minimum=16, maximum=80)
        if ATTEMPT_RE.fullmatch(attempt_id) is None:
            raise ValueError("attempt_id is not canonical")
        canary = _string(document["canary"], "canary", minimum=24, maximum=120)
        if CANARY_RE.fullmatch(canary) is None or attempt_id not in canary:
            raise ValueError("canary must be canonical and bind attempt_id")

        candidate_raw = _mapping(document["candidate"], "candidate")
        _exact_keys(
            candidate_raw,
            required={
                "manifest_path",
                "manifest_sha256",
                "source_root",
                "candidate_sha",
                "candidate_tree",
                "suite_revision",
                "python_executable",
                "python_sha256",
            },
            where="candidate",
        )
        manifest_sha = _string(candidate_raw["manifest_sha256"], "candidate.manifest_sha256")
        python_sha = _string(candidate_raw["python_sha256"], "candidate.python_sha256")
        candidate_sha = _string(candidate_raw["candidate_sha"], "candidate.candidate_sha")
        candidate_tree = _string(candidate_raw["candidate_tree"], "candidate.candidate_tree")
        if SHA256_RE.fullmatch(manifest_sha) is None or SHA256_RE.fullmatch(python_sha) is None:
            raise ValueError("candidate SHA-256 pins are invalid")
        if GIT_SHA_RE.fullmatch(candidate_sha) is None or GIT_SHA_RE.fullmatch(candidate_tree) is None:
            raise ValueError("candidate identity pins are invalid")
        candidate = CandidatePolicy(
            manifest_path=_absolute_path(candidate_raw["manifest_path"], "candidate.manifest_path"),
            manifest_sha256=manifest_sha,
            source_root=_absolute_path(candidate_raw["source_root"], "candidate.source_root"),
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
            suite_revision=_string(candidate_raw["suite_revision"], "candidate.suite_revision", maximum=160),
            python_executable=_absolute_path(
                candidate_raw["python_executable"], "candidate.python_executable"
            ),
            python_sha256=python_sha,
        )

        contour_raw = _mapping(document["contour"], "contour")
        _exact_keys(
            contour_raw,
            required={
                "contour_id",
                "isolation_root",
                "friday_home",
                "data_dir",
                "state_dir",
                "cache_dir",
                "log_dir",
                "env_file",
                "env_file_sha256",
                "database_path",
                "inbox_db_path",
                "files_dir",
                "backend_origin",
                "known_production_homes",
                "foreign_lease_paths",
            },
            where="contour",
        )
        contour_id = _string(contour_raw["contour_id"], "contour.contour_id", minimum=8, maximum=80)
        if SAFE_CONTOUR_RE.fullmatch(contour_id) is None:
            raise ValueError("contour_id is not canonical")
        production_values = contour_raw["known_production_homes"]
        lease_values = contour_raw["foreign_lease_paths"]
        if not isinstance(production_values, list) or not isinstance(lease_values, list):
            raise ValueError("contour path lists must be arrays")
        if not production_values or len(production_values) > 16 or len(lease_values) > 16:
            raise ValueError("contour path list cardinality is invalid")
        production_homes = tuple(
            _absolute_path(item, f"known_production_homes[{index}]")
            for index, item in enumerate(production_values)
        )
        foreign_leases = tuple(
            _absolute_path(item, f"foreign_lease_paths[{index}]") for index, item in enumerate(lease_values)
        )
        env_file_sha256 = _string(contour_raw["env_file_sha256"], "contour.env_file_sha256")
        if SHA256_RE.fullmatch(env_file_sha256) is None:
            raise ValueError("contour env SHA-256 pin is invalid")
        contour = ContourPolicy(
            contour_id=contour_id,
            isolation_root=_absolute_path(contour_raw["isolation_root"], "contour.isolation_root"),
            friday_home=_absolute_path(contour_raw["friday_home"], "contour.friday_home"),
            data_dir=_absolute_path(contour_raw["data_dir"], "contour.data_dir"),
            state_dir=_absolute_path(contour_raw["state_dir"], "contour.state_dir"),
            cache_dir=_absolute_path(contour_raw["cache_dir"], "contour.cache_dir"),
            log_dir=_absolute_path(contour_raw["log_dir"], "contour.log_dir"),
            env_file=_absolute_path(contour_raw["env_file"], "contour.env_file"),
            env_file_sha256=env_file_sha256,
            database_path=_absolute_path(contour_raw["database_path"], "contour.database_path"),
            inbox_db_path=_absolute_path(contour_raw["inbox_db_path"], "contour.inbox_db_path"),
            files_dir=_absolute_path(contour_raw["files_dir"], "contour.files_dir"),
            backend_origin=_string(contour_raw["backend_origin"], "contour.backend_origin", maximum=128),
            known_production_homes=production_homes,
            foreign_lease_paths=foreign_leases,
        )

        access_raw = _mapping(document["access"], "access")
        _exact_keys(access_raw, required={"manifest_path", "manifest_sha256"}, where="access")
        access_sha = _string(access_raw["manifest_sha256"], "access.manifest_sha256")
        if SHA256_RE.fullmatch(access_sha) is None:
            raise ValueError("access manifest SHA-256 is invalid")
        access = AccessPolicy(
            manifest_path=_absolute_path(access_raw["manifest_path"], "access.manifest_path"),
            manifest_sha256=access_sha,
        )

        observer_raw = _mapping(document["observer"], "observer")
        _exact_keys(
            observer_raw,
            required={"mode", "result_path"},
            optional={"adapter_argv", "adapter_executable_sha256"},
            where="observer",
        )
        mode = _string(observer_raw["mode"], "observer.mode", maximum=64)
        if mode not in {"owner_manual_readback", "external_user_adapter"}:
            raise ValueError("observer mode is not a real-user mode")
        raw_argv = observer_raw.get("adapter_argv", [])
        if not isinstance(raw_argv, list) or not all(
            isinstance(item, str) and item and "\x00" not in item for item in raw_argv
        ):
            raise ValueError("observer.adapter_argv is invalid")
        adapter_sha = str(observer_raw.get("adapter_executable_sha256") or "")
        if mode == "external_user_adapter":
            if not 1 <= len(raw_argv) <= 32:
                raise ValueError("external observer needs a bounded adapter argv")
            if SHA256_RE.fullmatch(adapter_sha) is None:
                raise ValueError("external observer executable must be pinned")
            if not Path(raw_argv[0]).is_absolute():
                raise ValueError("observer adapter executable must be absolute")
        elif raw_argv or adapter_sha:
            raise ValueError("manual observer cannot carry an adapter")
        observer = ObserverPolicy(
            mode=mode,
            result_path=_absolute_path(observer_raw["result_path"], "observer.result_path"),
            adapter_argv=tuple(raw_argv),
            adapter_executable_sha256=adapter_sha,
        )

        budgets_raw = _mapping(document["budgets"], "budgets")
        _exact_keys(
            budgets_raw,
            required={
                "timeout_s",
                "backend_ready_timeout_s",
                "poll_interval_s",
                "max_getupdates_rounds",
                "max_inbound_user_messages",
                "max_outbound_bot_posts",
                "max_evidence_bytes",
                "process_log_bytes",
                "cleanup_grace_s",
            },
            where="budgets",
        )
        budgets = Budgets(
            timeout_s=_number(budgets_raw["timeout_s"], "budgets.timeout_s", minimum=30, maximum=600),
            backend_ready_timeout_s=_number(
                budgets_raw["backend_ready_timeout_s"],
                "budgets.backend_ready_timeout_s",
                minimum=2,
                maximum=120,
            ),
            poll_interval_s=_number(
                budgets_raw["poll_interval_s"], "budgets.poll_interval_s", minimum=0.05, maximum=2
            ),
            max_getupdates_rounds=_integer(
                budgets_raw["max_getupdates_rounds"],
                "budgets.max_getupdates_rounds",
                minimum=1,
                maximum=20,
            ),
            max_inbound_user_messages=_integer(
                budgets_raw["max_inbound_user_messages"],
                "budgets.max_inbound_user_messages",
                minimum=1,
                maximum=1,
            ),
            max_outbound_bot_posts=_integer(
                budgets_raw["max_outbound_bot_posts"],
                "budgets.max_outbound_bot_posts",
                minimum=1,
                maximum=2,
            ),
            max_evidence_bytes=_integer(
                budgets_raw["max_evidence_bytes"],
                "budgets.max_evidence_bytes",
                minimum=65536,
                maximum=1048576,
            ),
            process_log_bytes=_integer(
                budgets_raw["process_log_bytes"],
                "budgets.process_log_bytes",
                minimum=4096,
                maximum=262144,
            ),
            cleanup_grace_s=_number(
                budgets_raw["cleanup_grace_s"],
                "budgets.cleanup_grace_s",
                minimum=1,
                maximum=30,
            ),
        )
        if (
            budgets.timeout_s != EFFECT_DURATION_LIMIT_S
            or budgets.max_getupdates_rounds != EFFECT_METHOD_CAPS["bridge"]["getUpdates"]
            or budgets.max_inbound_user_messages != 1
            or budgets.max_outbound_bot_posts != EFFECT_METHOD_CAPS["bridge"]["sendMessage"]
            or budgets.max_evidence_bytes != EFFECT_RESPONSE_MAX_BYTES
        ):
            raise ValueError("Telegram effect budgets must match the frozen live scope")
        log_count = 3 if mode == "external_user_adapter" else 2
        if (
            budgets.process_log_bytes * log_count + 65536 + EFFECT_LEDGER_RESERVATION_BYTES
            > budgets.max_evidence_bytes
        ):
            raise ValueError("evidence budget cannot contain the bounded process logs")
        return cls(
            attempt_id=attempt_id,
            canary=canary,
            candidate=candidate,
            contour=contour,
            access=access,
            observer=observer,
            budgets=budgets,
        )


@dataclasses.dataclass(frozen=True)
class AccessManifest:
    contour_id: str
    attempt_id: str
    candidate_sha: str
    candidate_manifest_sha256: str
    bot_user_id: int
    chat_id: int
    user_id: int
    observer_mode: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    nonce: str

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        *,
        policy: Policy,
        now: dt.datetime,
    ) -> AccessManifest:
        _exact_keys(
            document,
            required={
                "schema",
                "contour_id",
                "attempt_id",
                "candidate_sha",
                "candidate_manifest_sha256",
                "dedicated_for_release_test",
                "same_bot_production_consumer_stopped",
                "clean_backlog_expected",
                "bot_user_id",
                "chat_id",
                "user_id",
                "observer_mode",
                "issued_at",
                "expires_at",
                "nonce",
            },
            where="access_manifest",
        )
        if document["schema"] != ACCESS_SCHEMA:
            raise ValueError("access manifest schema mismatch")
        if (
            _boolean(document["dedicated_for_release_test"], "access_manifest.dedicated_for_release_test")
            is not True
        ):
            raise ValueError("access is not dedicated")
        if (
            _boolean(
                document["same_bot_production_consumer_stopped"],
                "access_manifest.same_bot_production_consumer_stopped",
            )
            is not True
        ):
            raise ValueError("same-bot production consumer is not stopped")
        if _boolean(document["clean_backlog_expected"], "access_manifest.clean_backlog_expected") is not True:
            raise ValueError("clean backlog is not attested")
        contour_id = _string(document["contour_id"], "access_manifest.contour_id", maximum=80)
        attempt_id = _string(document["attempt_id"], "access_manifest.attempt_id", maximum=80)
        candidate_sha = _string(document["candidate_sha"], "access_manifest.candidate_sha", maximum=64)
        candidate_manifest_sha256 = _string(
            document["candidate_manifest_sha256"],
            "access_manifest.candidate_manifest_sha256",
            maximum=64,
        )
        bot_user_id = _integer(
            document["bot_user_id"], "access_manifest.bot_user_id", minimum=1, maximum=10**20 - 1
        )
        chat_id = _integer(document["chat_id"], "access_manifest.chat_id", minimum=1, maximum=10**20 - 1)
        user_id = _integer(document["user_id"], "access_manifest.user_id", minimum=1, maximum=10**20 - 1)
        observer_mode = _string(document["observer_mode"], "access_manifest.observer_mode", maximum=64)
        issued_at = _parse_utc(document["issued_at"], "access_manifest.issued_at")
        expires_at = _parse_utc(document["expires_at"], "access_manifest.expires_at")
        nonce = _string(document["nonce"], "access_manifest.nonce", minimum=16, maximum=128)
        if contour_id != policy.contour.contour_id:
            raise ValueError("access contour binding mismatch")
        if attempt_id != policy.attempt_id:
            raise ValueError("access attempt binding mismatch")
        if candidate_sha != policy.candidate.candidate_sha:
            raise ValueError("access candidate binding mismatch")
        if candidate_manifest_sha256 != policy.candidate.manifest_sha256:
            raise ValueError("access candidate manifest binding mismatch")
        if observer_mode != policy.observer.mode:
            raise ValueError("access observer binding mismatch")
        if chat_id != user_id:
            raise ValueError("live contour requires the real user's private chat")
        if issued_at > now + dt.timedelta(minutes=5):
            raise ValueError("access manifest is future-dated")
        if expires_at <= now or expires_at > issued_at + dt.timedelta(hours=24):
            raise ValueError("access manifest lifetime is invalid")
        required_validity = dt.timedelta(
            seconds=policy.budgets.timeout_s + 2 * policy.budgets.cleanup_grace_s
        )
        if expires_at < now + required_validity:
            raise ValueError("access manifest expires before the finite run can finish")
        return cls(
            contour_id=contour_id,
            attempt_id=attempt_id,
            candidate_sha=candidate_sha,
            candidate_manifest_sha256=candidate_manifest_sha256,
            bot_user_id=bot_user_id,
            chat_id=chat_id,
            user_id=user_id,
            observer_mode=observer_mode,
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
        )


class EvidenceStore:
    def __init__(self, root: Path, limit: int) -> None:
        if root.exists() or root.is_symlink():
            raise ValueError("evidence directory must not exist")
        parent = root.parent
        _secure_directory(parent, "evidence parent")
        os.mkdir(root, 0o700)
        self.root = root
        self.limit = limit
        self.used = 0
        self.external_reservation = 0
        self.events_path = root / "events.jsonl"
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        _fsync_directory(root)

    def _reserve(self, amount: int) -> None:
        if amount < 0 or self.used + amount > self.limit:
            raise RoundtripError("FAIL", "evidence_budget_exceeded")
        self.used += amount

    def event(self, stage: str, state: str, **facts: object) -> None:
        document = {
            "schema": EVENT_SCHEMA,
            "at": _utc_now(),
            "stage": stage,
            "state": state,
            "facts": facts,
        }
        payload = _json_bytes(document)
        self._reserve(len(payload))
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def write(self, name: str, document: object) -> Path:
        if "/" in name or name in {"", ".", ".."}:
            raise ValueError("invalid evidence filename")
        payload = _json_bytes(document)
        self._reserve(len(payload))
        path = self.root / name
        _write_new_private(path, document)
        _fsync_directory(self.root)
        return path

    def account_external_file(self, path: Path) -> None:
        size = path.stat().st_size
        self._reserve(size)

    def reserve_external_tree(self, amount: int) -> None:
        if self.external_reservation:
            raise RoundtripError("FAIL", "external_evidence_already_reserved")
        self._reserve(amount)
        self.external_reservation = amount

    def seal_external_tree(self, path: Path) -> int:
        if not self.external_reservation:
            raise RoundtripError("FAIL", "external_evidence_not_reserved")
        if not _is_within(path, self.root):
            raise RoundtripError("FAIL", "external_evidence_outside_store")
        total = 0
        for directory, names, files in os.walk(path):
            base = Path(directory)
            info = base.lstat()
            if not stat.S_ISDIR(info.st_mode) or base.is_symlink() or info.st_uid != os.getuid():
                raise RoundtripError("FAIL", "external_evidence_directory_invalid")
            if info.st_mode & 0o077:
                raise RoundtripError("FAIL", "external_evidence_not_private")
            for name in names:
                target = base / name
                if target.is_symlink():
                    raise RoundtripError("FAIL", "external_evidence_symlink_refused")
            for name in files:
                target = base / name
                info = target.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or target.is_symlink()
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                ):
                    raise RoundtripError("FAIL", "external_evidence_file_invalid")
                total += info.st_size
        if total > self.external_reservation:
            raise RoundtripError("FAIL", "external_evidence_reservation_exceeded")
        self.used -= self.external_reservation
        self.external_reservation = 0
        self._reserve(total)
        return total

    def release_empty_external_reservation(self) -> None:
        if self.external_reservation:
            self.used -= self.external_reservation
            self.external_reservation = 0

    def actual_size(self) -> int:
        total = 0
        for directory, _names, files in os.walk(self.root):
            base = Path(directory)
            total += sum((base / name).stat().st_size for name in files if not (base / name).is_symlink())
        return total


def load_policy(path: Path) -> Policy:
    document = _load_json(path, "policy", maximum=MAX_JSON_BYTES, private=True)
    return Policy.from_document(document)


def _verified_source_map(candidate: CandidatePolicy) -> dict[str, Any]:
    manifest = _load_json(
        candidate.manifest_path,
        "candidate manifest",
        maximum=MAX_JSON_BYTES,
        private=False,
    )
    if sha256_file(candidate.manifest_path, maximum=MAX_JSON_BYTES) != candidate.manifest_sha256:
        raise RoundtripError("NOT_RUN", "candidate_manifest_digest_mismatch")
    source_root = candidate.source_root.resolve(strict=True)
    if not source_root.is_dir() or candidate.source_root.is_symlink():
        raise RoundtripError("NOT_RUN", "candidate_source_root_invalid")
    if str(Path(manifest.get("source_root", "")).resolve()) != str(source_root):
        raise RoundtripError("NOT_RUN", "candidate_source_root_binding_mismatch")
    for field, expected in (
        ("candidate_sha", candidate.candidate_sha),
        ("candidate_tree", candidate.candidate_tree),
        ("suite_revision", candidate.suite_revision),
    ):
        if manifest.get(field) != expected:
            raise RoundtripError("NOT_RUN", f"candidate_{field}_mismatch")
    raw_hashes = manifest.get("source_sha256")
    if not isinstance(raw_hashes, dict) or not raw_hashes:
        raise RoundtripError("NOT_RUN", "candidate_source_hash_map_missing")
    declared: dict[str, str] = {}
    observed: dict[str, str] = {}
    for raw_name, raw_digest in raw_hashes.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise RoundtripError("NOT_RUN", "candidate_source_hash_map_invalid")
        if raw_name.startswith("/") or ".." in Path(raw_name).parts:
            raise RoundtripError("NOT_RUN", "candidate_source_path_invalid")
        if SHA256_RE.fullmatch(raw_digest) is None:
            raise RoundtripError("NOT_RUN", "candidate_source_digest_invalid")
        target = source_root / raw_name
        try:
            info = target.lstat()
        except FileNotFoundError as exc:
            raise RoundtripError("NOT_RUN", "candidate_source_file_missing", raw_name) from exc
        if not stat.S_ISREG(info.st_mode) or target.is_symlink():
            raise RoundtripError("NOT_RUN", "candidate_source_file_not_regular", raw_name)
        actual = sha256_file(target)
        if actual != raw_digest:
            raise RoundtripError("NOT_RUN", "candidate_source_file_digest_mismatch", raw_name)
        declared[raw_name] = raw_digest
        observed[raw_name] = actual
    actual_names: set[str] = set()
    for directory, names, files in os.walk(source_root):
        names[:] = [name for name in names if name != ".git"]
        base = Path(directory)
        for filename in files:
            target = base / filename
            relative = target.relative_to(source_root).as_posix()
            if target.is_symlink() or not target.is_file():
                raise RoundtripError("NOT_RUN", "candidate_source_extra_nonregular", relative)
            actual_names.add(relative)
    if actual_names != set(declared):
        raise RoundtripError("NOT_RUN", "candidate_source_inventory_mismatch")
    for required in (MODULE_RELATIVE_PATH, CLI_RELATIVE_PATH, BRIDGE_BASE_RELATIVE_PATH):
        if required not in declared:
            raise RoundtripError("NOT_RUN", "candidate_required_source_missing", required)
    current_module = Path(__file__).resolve()
    expected_module = (source_root / MODULE_RELATIVE_PATH).resolve()
    if current_module != expected_module:
        raise RoundtripError("NOT_RUN", "driver_not_executed_from_pinned_candidate")
    map_digest = _sha256_bytes(_json_bytes(observed))
    return {
        "candidate_sha": candidate.candidate_sha,
        "candidate_tree": candidate.candidate_tree,
        "suite_revision": candidate.suite_revision,
        "manifest_sha256": candidate.manifest_sha256,
        "source_map_sha256": map_digest,
        "source_file_count": len(observed),
        "module_sha256": observed[MODULE_RELATIVE_PATH],
        "cli_sha256": observed[CLI_RELATIVE_PATH],
        "bridge_base_sha256": observed[BRIDGE_BASE_RELATIVE_PATH],
    }


def _parse_env_file(path: Path) -> dict[str, str]:
    _secure_regular_file(path, "contour env file", maximum=256 * 1024)
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError(f"env line {line_number} is invalid")
        if key in result:
            raise ValueError(f"env key {key} is duplicated")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"env key {key} has an invalid value")
        result[key] = value
    return result


def _parse_id_list(raw: str, where: str) -> set[int]:
    if not raw.strip():
        return set()
    pieces = [item for item in re.split(r"[\s,]+", raw.strip()) if item]
    if len(pieces) > 32:
        raise ValueError(f"{where} has too many ids")
    result: set[int] = set()
    for item in pieces:
        if re.fullmatch(r"-?[1-9][0-9]{0,19}", item) is None:
            raise ValueError(f"{where} contains an invalid id")
        result.add(int(item))
    return result


def _require_env_value(env: Mapping[str, str], key: str, expected: str) -> None:
    if env.get(key) != expected:
        raise RoundtripError("NOT_RUN", "isolated_env_mismatch", key)


def _validate_contour(policy: Policy, access: AccessManifest) -> tuple[dict[str, str], dict[str, Any]]:
    contour = policy.contour
    _secure_directory(contour.isolation_root, "isolation root")
    if not _is_within(contour.friday_home, contour.isolation_root):
        raise RoundtripError("NOT_RUN", "friday_home_outside_isolation_root")
    if contour.friday_home in contour.known_production_homes or contour.friday_home == Path.home().resolve():
        raise RoundtripError("NOT_RUN", "production_home_refused")
    if any(
        _is_within(contour.friday_home, root) or _is_within(root, contour.friday_home)
        for root in (policy.candidate.source_root, Path.cwd().resolve())
    ):
        raise RoundtripError("NOT_RUN", "friday_home_overlaps_source_or_workdir")
    if contour.friday_home.exists() or contour.friday_home.is_symlink():
        raise RoundtripError("NOT_RUN", "friday_home_must_be_new")
    for path, code in (
        (contour.data_dir, "data_dir_outside_home"),
        (contour.state_dir, "state_dir_outside_home"),
        (contour.cache_dir, "cache_dir_outside_home"),
        (contour.log_dir, "log_dir_outside_home"),
        (contour.database_path, "database_path_outside_home"),
        (contour.inbox_db_path, "inbox_path_outside_home"),
        (contour.files_dir, "files_path_outside_home"),
    ):
        if not _is_within(path, contour.friday_home):
            raise RoundtripError("NOT_RUN", code)
        if path.exists() or path.is_symlink():
            raise RoundtripError("NOT_RUN", "isolated_state_already_exists", str(path))
    if not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{3,4}", contour.backend_origin):
        raise RoundtripError("NOT_RUN", "backend_origin_must_be_loopback_http")
    port = int(contour.backend_origin.rsplit(":", 1)[1])
    if port > 65535:
        raise RoundtripError("NOT_RUN", "backend_port_invalid")

    if sha256_file(contour.env_file, maximum=256 * 1024) != contour.env_file_sha256:
        raise RoundtripError("NOT_RUN", "contour_env_digest_mismatch")
    env = _parse_env_file(contour.env_file)
    _require_env_value(env, "FRIDAY_HOME", str(contour.friday_home))
    _require_env_value(env, "FRIDAY_DATA_DIR", str(contour.data_dir))
    _require_env_value(env, "FRIDAY_STATE_DIR", str(contour.state_dir))
    _require_env_value(env, "FRIDAY_CACHE_DIR", str(contour.cache_dir))
    _require_env_value(env, "FRIDAY_LOG_DIR", str(contour.log_dir))
    _require_env_value(env, "FRIDAY_DATABASE_PATH", str(contour.database_path))
    _require_env_value(env, "FRIDAY_FILES_DIR", str(contour.files_dir))
    effective_inbox = env.get(
        "FRIDAY_TELEGRAM_INBOX_DB_PATH",
        env.get("JERICHO_TELEGRAM_INBOX_DB_PATH", str(contour.state_dir / "telegram-inbox.sqlite3")),
    )
    if effective_inbox != str(contour.inbox_db_path):
        raise RoundtripError("NOT_RUN", "inbox_path_binding_mismatch")
    _require_env_value(env, "FRIDAY_API_HOST", "127.0.0.1")
    _require_env_value(env, "FRIDAY_API_BIND_ADDRESS", "127.0.0.1")
    _require_env_value(env, "FRIDAY_API_PORT", str(port))
    if env.get("FRIDAY_BACKEND_URL", contour.backend_origin).rstrip("/") != contour.backend_origin:
        raise RoundtripError("NOT_RUN", "backend_url_binding_mismatch")
    for key in (
        "FRIDAY_TELEGRAM_OPEN_REGISTRATION",
        "FRIDAY_SHARED_ARCHIVE",
        "FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS",
        "FRIDAY_TRUST_PROXY_HEADERS",
        "FRIDAY_HOST_CONTROL_ENABLED",
        "FRIDAY_OPERATOR_FULL_AUTONOMY",
        "FRIDAY_WORKERS_ENABLED",
        "FRIDAY_AUTONOMY_ENABLED",
        "FRIDAY_COGNITION_ENABLED",
        "FRIDAY_REMINDERS_ENABLED",
    ):
        _require_env_value(env, key, "0")
    _require_env_value(env, "FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK", "1")
    _require_env_value(env, "FRIDAY_LLM_ENABLED", "1")
    for key in ("FRIDAY_SSL_CERTFILE", "FRIDAY_SSL_KEYFILE", "FRIDAY_CORS_ORIGINS"):
        if env.get(key, ""):
            raise RoundtripError("NOT_RUN", "isolated_env_forbidden_value", key)

    bot_token = env.get("FRIDAY_TELEGRAM_BOT_TOKEN", "").strip()
    bridge_secret = env.get("FRIDAY_TELEGRAM_BRIDGE_SECRET", "").strip()
    api_token = env.get("FRIDAY_API_TOKEN", "").strip()
    if not bot_token or ":" not in bot_token:
        raise RoundtripError("NOT_RUN", "dedicated_bot_credential_missing")
    if len(bridge_secret) < 32 or len(api_token) < 32:
        raise RoundtripError("NOT_RUN", "backend_or_bridge_credential_missing")
    if len({bot_token, bridge_secret, api_token}) != 3:
        raise RoundtripError("NOT_RUN", "credentials_must_be_distinct")
    telegram_proxy = env.get("FRIDAY_TELEGRAM_PROXY", "").strip()
    if telegram_proxy and not telegram_proxy.startswith(("http://", "https://")):
        raise RoundtripError("NOT_RUN", "telegram_proxy_invalid")
    allowed = _parse_id_list(env.get("FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS", ""), "allowed chats")
    owners = _parse_id_list(env.get("FRIDAY_TELEGRAM_OWNER_CHAT_IDS", ""), "owner chats")
    if owners != {access.chat_id} or allowed | owners != {access.chat_id}:
        raise RoundtripError("NOT_RUN", "dedicated_chat_allowlist_mismatch")

    python = policy.candidate.python_executable
    info = python.lstat()
    if not stat.S_ISREG(info.st_mode) or python.is_symlink() or not os.access(python, os.X_OK):
        raise RoundtripError("NOT_RUN", "python_executable_invalid")
    if sha256_file(python) != policy.candidate.python_sha256:
        raise RoundtripError("NOT_RUN", "python_executable_digest_mismatch")
    adapter_summary: dict[str, Any] = {"mode": policy.observer.mode}
    if policy.observer.mode == "external_user_adapter":
        executable = Path(policy.observer.adapter_argv[0])
        info = executable.lstat()
        if not stat.S_ISREG(info.st_mode) or executable.is_symlink() or not os.access(executable, os.X_OK):
            raise RoundtripError("NOT_RUN", "observer_adapter_invalid")
        if sha256_file(executable) != policy.observer.adapter_executable_sha256:
            raise RoundtripError("NOT_RUN", "observer_adapter_digest_mismatch")
        adapter_summary["adapter_executable_sha256"] = policy.observer.adapter_executable_sha256
    parent = policy.observer.result_path.parent
    _secure_directory(parent, "observer result parent")
    if _is_within(policy.observer.result_path, contour.friday_home) or _is_within(
        policy.observer.result_path, policy.candidate.source_root
    ):
        raise RoundtripError("NOT_RUN", "observer_result_not_independent")
    if policy.observer.result_path.exists() or policy.observer.result_path.is_symlink():
        raise RoundtripError("NOT_RUN", "observer_result_must_be_new")

    return env, {
        "home": str(contour.friday_home),
        "data_dir": str(contour.data_dir),
        "state_dir": str(contour.state_dir),
        "database_path": str(contour.database_path),
        "inbox_db_path": str(contour.inbox_db_path),
        "backend_origin": contour.backend_origin,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "observer": adapter_summary,
    }


def _lease_is_active(path: Path) -> bool:
    if not path.exists():
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise RoundtripError("BLOCKED", "lease_path_not_regular", str(path))
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _assert_preflight_leases(policy: Policy) -> None:
    for path in policy.contour.foreign_lease_paths:
        if _lease_is_active(path):
            raise RoundtripError("BLOCKED", "foreign_bridge_lease_active", str(path))
    own_paths = (
        policy.contour.state_dir / "backend.lock",
        policy.contour.inbox_db_path.with_name(policy.contour.inbox_db_path.name + ".lock"),
    )
    for path in own_paths:
        if path.exists() or path.is_symlink():
            raise RoundtripError("NOT_RUN", "isolated_lease_path_preexists", str(path))


def _assert_port_available(origin: str) -> None:
    port = int(origin.rsplit(":", 1)[1])
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RoundtripError("BLOCKED", "isolated_backend_port_unavailable") from exc
    finally:
        probe.close()


class BotApi:
    """Two preflight reads only; user ingress and destination history are absent."""

    def __init__(
        self,
        token: str,
        *,
        proxy: str = "",
        timeout_s: float = 15.0,
        effect_guard: TelegramEffectLedger | None = None,
    ) -> None:
        self._token = token
        self._timeout_s = timeout_s
        self._effect_guard = effect_guard
        proxy_handler = (
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            if proxy
            else urllib.request.ProxyHandler({})
        )
        self._opener = urllib.request.build_opener(proxy_handler, _NoRedirectHandler())

    def _read(self, method: str) -> dict[str, Any]:
        if method not in {"getMe", "getWebhookInfo"}:
            raise ValueError("unsupported read-only Bot API method")
        if self._effect_guard is None:
            raise RoundtripError("NOT_RUN", "telegram_effect_guard_missing")
        sequence = self._effect_guard.begin(method, "GET", b"")
        request = urllib.request.Request(
            f"{BOT_API_ROOT}/bot{self._token}/{method}",
            method="GET",
            headers={"User-Agent": "friday-release-roundtrip/1"},
        )
        try:
            with self._opener.open(
                request,
                timeout=self._effect_guard.remaining_seconds(self._timeout_s),
            ) as response:
                payload = response.read(EFFECT_RESPONSE_MAX_BYTES + 1)
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read(EFFECT_RESPONSE_MAX_BYTES + 1)
            except (TimeoutError, OSError) as read_exc:
                self._effect_guard.unknown(sequence, type(read_exc).__name__)
                raise RoundtripError(
                    "BLOCKED", "telegram_botapi_unavailable", type(read_exc).__name__
                ) from None
            self._effect_guard.finish(sequence, int(exc.code), payload)
            raise RoundtripError("BLOCKED", "telegram_botapi_http_error", str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._effect_guard.unknown(sequence, type(exc).__name__)
            raise RoundtripError("BLOCKED", "telegram_botapi_unavailable", type(exc).__name__) from None
        self._effect_guard.finish(sequence, status, payload)
        try:
            document = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RoundtripError("FAIL", "telegram_botapi_response_invalid") from exc
        if not isinstance(document, dict) or document.get("ok") is not True:
            raise RoundtripError("FAIL", "telegram_botapi_read_rejected")
        result = document.get("result")
        if not isinstance(result, dict):
            raise RoundtripError("FAIL", "telegram_botapi_result_invalid")
        return dict(result)

    def preflight(self, access: AccessManifest) -> dict[str, Any]:
        me = self._read("getMe")
        webhook = self._read("getWebhookInfo")
        bot_id = me.get("id")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id != access.bot_user_id:
            raise RoundtripError("NOT_RUN", "dedicated_bot_identity_mismatch")
        webhook_url = webhook.get("url")
        pending = webhook.get("pending_update_count", 0)
        if not isinstance(webhook_url, str):
            raise RoundtripError("FAIL", "telegram_webhook_shape_invalid")
        if webhook_url:
            raise RoundtripError("NOT_RUN", "active_webhook_refused")
        if isinstance(pending, bool) or not isinstance(pending, int) or pending != 0:
            raise RoundtripError("NOT_RUN", "dedicated_bot_backlog_not_clean")
        username = me.get("username")
        return {
            "bot_user_id": bot_id,
            "bot_username": username if isinstance(username, str) else "",
            "webhook_empty": True,
            "pending_update_count": 0,
        }


class BoundedCapture:
    def __init__(self, pipe: Any, limit: int) -> None:
        self.pipe = pipe
        self.limit = limit
        self.buffer = bytearray()
        self.discarded = 0
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _drain(self) -> None:
        while True:
            chunk = self.pipe.read(8192)
            if not chunk:
                return
            room = max(0, self.limit - len(self.buffer))
            self.buffer.extend(chunk[:room])
            self.discarded += max(0, len(chunk) - room)

    def finish(self, timeout: float) -> None:
        if self.thread.ident is not None:
            self.thread.join(timeout=max(0.1, timeout))
        if not self.thread.is_alive() and self.pipe is not None:
            self.pipe.close()


@dataclasses.dataclass
class OwnedProcess:
    role: str
    process: subprocess.Popen[bytes]
    capture: BoundedCapture
    origin_receipt: Path


_BOOTSTRAP = r"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import time

source = Path(sys.argv[1]).resolve(strict=True)
receipt = Path(sys.argv[2])
role = sys.argv[3]
candidate_sha = sys.argv[4]
guard_spec = Path(sys.argv[5]).resolve(strict=True)
env_file = sys.argv[6]
command = sys.argv[7]
expected = (source / "friday" / "cli.py").resolve(strict=True)
sys.path.insert(0, str(source))
spec = importlib.util.find_spec("friday.cli")
origin = Path(spec.origin or "").resolve(strict=True) if spec is not None else Path()
if origin != expected:
    raise RuntimeError("pinned friday.cli origin mismatch")
guard_origin = ""
guard_sha256 = ""
guard_spec_sha256 = ""
if role == "telegram-bridge":
    guard_path = (source / "tools" / "release_1_0_telegram_roundtrip.py").resolve(strict=True)
    guard_name = "friday_release_1_0_effect_guard"
    guard_specification = importlib.util.spec_from_file_location(guard_name, guard_path)
    if guard_specification is None or guard_specification.loader is None:
        raise RuntimeError("pinned Telegram effect guard loader missing")
    guard_module = importlib.util.module_from_spec(guard_specification)
    sys.modules[guard_name] = guard_module
    guard_specification.loader.exec_module(guard_module)
    guard_module.install_bridge_effect_guard(__import__("httpx"), guard_spec, Path(env_file))
    guard_origin = str(guard_path)
    guard_sha256 = hashlib.sha256(guard_path.read_bytes()).hexdigest()
    guard_spec_sha256 = hashlib.sha256(guard_spec.read_bytes()).hexdigest()
document = {
    "schema": "friday.release-1-0-child-origin.v1",
    "role": role,
    "pid": os.getpid(),
    "candidate_sha": candidate_sha,
    "source_root": str(source),
    "cli_origin": str(origin),
    "effect_guard_origin": guard_origin,
    "effect_guard_sha256": guard_sha256,
    "effect_spec_sha256": guard_spec_sha256,
    "argv": ["friday.cli", "--env-file", str(Path(env_file).resolve()), command],
    "observed_at_ns": time.time_ns(),
}
payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(fd, payload)
    os.fsync(fd)
finally:
    os.close(fd)
if command == "__effect-guard-probe__":
    raise SystemExit(0)
sys.argv = document["argv"]
runpy.run_module("friday.cli", run_name="__main__")
""".strip()


def build_cli_argv(policy: Policy, role: str, origin_receipt: Path, effect_spec_path: Path) -> list[str]:
    if role not in {"server", "telegram-bridge"}:
        raise ValueError("invalid Friday service role")
    return [
        str(policy.candidate.python_executable),
        "-I",
        "-B",
        "-c",
        _BOOTSTRAP,
        str(policy.candidate.source_root),
        str(origin_receipt),
        role,
        policy.candidate.candidate_sha,
        str(effect_spec_path),
        str(policy.contour.env_file),
        role,
    ]


def _child_environment(policy: Policy) -> dict[str, str]:
    return {
        "HOME": str(policy.contour.friday_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
    }


def _assert_env_pin(policy: Policy) -> None:
    if sha256_file(policy.contour.env_file, maximum=256 * 1024) != policy.contour.env_file_sha256:
        raise RoundtripError("FAIL", "contour_env_changed")


def _cleanup_unreturned_process(
    process: subprocess.Popen[bytes], capture: BoundedCapture, grace: float
) -> None:
    try:
        stopped = stop_owned_process(OwnedProcess("unreturned-start", process, capture, Path()), grace)
    except BaseException as exc:
        raise RoundtripError("FAIL", "owned_start_cleanup_unconfirmed") from exc
    if not (
        stopped["returncode"] is not None and stopped["process_group_clear"] and stopped["capture_drained"]
    ):
        raise RoundtripError("FAIL", "owned_start_cleanup_unconfirmed")


def _start_captured_process(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], limit: int, grace: float
) -> tuple[subprocess.Popen[bytes], BoundedCapture]:
    # Allocate bookkeeping before the effect; keep ownership through every failure.
    capture = BoundedCapture(None, limit)
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        umask=0o077,
    )
    try:
        capture.pipe = process.stdout
        assert capture.pipe is not None
        capture.start()
    except BaseException:
        _cleanup_unreturned_process(process, capture, grace)
        raise
    return process, capture


def start_owned_process(
    policy: Policy,
    evidence: EvidenceStore,
    role: str,
    effect_spec_path: Path | None = None,
) -> OwnedProcess:
    _assert_env_pin(policy)
    receipt = evidence.root / f"origin-{role}.json"
    argv = build_cli_argv(
        policy,
        role,
        receipt,
        effect_spec_path or (evidence.root / "telegram-effects" / "spec.json"),
    )
    process, capture = _start_captured_process(
        argv,
        cwd=policy.contour.friday_home,
        env=_child_environment(policy),
        limit=policy.budgets.process_log_bytes,
        grace=policy.budgets.cleanup_grace_s,
    )
    try:
        owned = OwnedProcess(role=role, process=process, capture=capture, origin_receipt=receipt)
        evidence.event(role, "STARTED", pid=process.pid)
    except BaseException:
        _cleanup_unreturned_process(process, capture, policy.budgets.cleanup_grace_s)
        raise
    return owned


def _read_origin_receipt(process: OwnedProcess, policy: Policy) -> dict[str, Any]:
    try:
        document = _load_json(
            process.origin_receipt,
            f"{process.role} origin receipt",
            maximum=16384,
            private=True,
        )
    except ValueError as exc:
        raise RoundtripError("FAIL", "child_origin_receipt_missing", process.role) from exc
    _exact_keys(
        document,
        required={
            "schema",
            "role",
            "pid",
            "candidate_sha",
            "source_root",
            "cli_origin",
            "effect_guard_origin",
            "effect_guard_sha256",
            "effect_spec_sha256",
            "argv",
            "observed_at_ns",
        },
        where="child origin receipt",
    )
    expected_argv = [
        "friday.cli",
        "--env-file",
        str(policy.contour.env_file.resolve()),
        process.role,
    ]
    expected_guard_origin = (
        str((policy.candidate.source_root / MODULE_RELATIVE_PATH).resolve())
        if process.role == "telegram-bridge"
        else ""
    )
    expected_guard_sha256 = (
        sha256_file(policy.candidate.source_root / MODULE_RELATIVE_PATH)
        if process.role == "telegram-bridge"
        else ""
    )
    expected_spec_sha256 = (
        sha256_file(process.origin_receipt.parent / "telegram-effects" / "spec.json")
        if process.role == "telegram-bridge"
        else ""
    )
    if (
        document.get("schema") != "friday.release-1-0-child-origin.v1"
        or document.get("role") != process.role
        or document.get("pid") != process.process.pid
        or document.get("candidate_sha") != policy.candidate.candidate_sha
        or document.get("source_root") != str(policy.candidate.source_root.resolve())
        or document.get("cli_origin") != str((policy.candidate.source_root / CLI_RELATIVE_PATH).resolve())
        or document.get("argv") != expected_argv
        or document.get("effect_guard_origin") != expected_guard_origin
        or document.get("effect_guard_sha256") != expected_guard_sha256
        or document.get("effect_spec_sha256") != expected_spec_sha256
    ):
        raise RoundtripError("FAIL", "child_origin_binding_mismatch", process.role)
    return {
        "role": process.role,
        "pid": process.process.pid,
        "candidate_sha": policy.candidate.candidate_sha,
        "cli_origin": document["cli_origin"],
        "effect_guard_origin": document["effect_guard_origin"],
        "effect_guard_sha256": document["effect_guard_sha256"],
        "effect_spec_sha256": document["effect_spec_sha256"],
        "argv": expected_argv,
    }


def _process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _clear_process_group(process_group: int, grace: float) -> bool:
    if not _process_group_alive(process_group):
        return True
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _process_group_alive(process_group):
            return True
        time.sleep(0.05)
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _process_group_alive(process_group):
            return True
        time.sleep(0.05)
    return not _process_group_alive(process_group)


def stop_owned_process(process: OwnedProcess, grace: float) -> dict[str, Any]:
    if process.process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.process.pid, signal.SIGINT)
        try:
            process.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.process.pid, signal.SIGTERM)
            try:
                process.process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.process.pid, signal.SIGKILL)
                process.process.wait(timeout=grace)
    process.capture.finish(grace)
    group_clear = _clear_process_group(process.process.pid, grace)
    return {
        "role": process.role,
        "pid": process.process.pid,
        "returncode": process.process.returncode,
        "process_group_clear": group_clear,
        "capture_drained": not process.capture.thread.is_alive(),
        "captured_bytes": len(process.capture.buffer),
        "discarded_bytes": process.capture.discarded,
    }


def _http_health(origin: str, timeout: float) -> bool:
    request = urllib.request.Request(origin + "/health", method="GET")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(65537)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False
    if len(payload) > 65536:
        return False
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict)


def wait_backend_ready(process: OwnedProcess, policy: Policy, deadline: float) -> None:
    ready_deadline = min(deadline, time.monotonic() + policy.budgets.backend_ready_timeout_s)
    while time.monotonic() < ready_deadline:
        if process.process.poll() is not None:
            raise RoundtripError("FAIL", "backend_exited_before_ready")
        if process.origin_receipt.exists() and _http_health(
            policy.contour.backend_origin,
            min(1.0, policy.budgets.poll_interval_s * 2),
        ):
            return
        time.sleep(policy.budgets.poll_interval_s)
    raise RoundtripError("FAIL", "backend_not_ready")


def wait_bridge_ready(process: OwnedProcess, policy: Policy, deadline: float) -> None:
    lease = policy.contour.inbox_db_path.with_name(policy.contour.inbox_db_path.name + ".lock")
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise RoundtripError("FAIL", "bridge_exited_before_ready")
        if process.origin_receipt.exists() and lease.exists() and _lease_is_active(lease):
            return
        time.sleep(policy.budgets.poll_interval_s)
    raise RoundtripError("FAIL", "bridge_not_ready")


def build_observer_request(policy: Policy, access: AccessManifest, bot: Mapping[str, Any]) -> dict[str, Any]:
    inbound_text = (
        f"Friday roundtrip {policy.canary}. Reply with exactly this marker and nothing else: {policy.canary}"
    )
    return {
        "schema": OBSERVER_REQUEST_SCHEMA,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "contour_id": access.contour_id,
        "canary": policy.canary,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "bot_user_id": bot["bot_user_id"],
        "bot_username": bot.get("bot_username", ""),
        "observer_mode": policy.observer.mode,
        "inbound_text": inbound_text,
        "result_path": str(policy.observer.result_path),
        "result_schema": OBSERVER_RESULT_SCHEMA,
        "max_inbound_user_messages": policy.budgets.max_inbound_user_messages,
        "max_outbound_bot_posts": policy.budgets.max_outbound_bot_posts,
        "unknown_effect_policy": "do_not_resend",
        "issued_at": _utc_now(),
    }


def _run_adapter_once(
    policy: Policy,
    request_path: Path,
    deadline: float,
) -> tuple[subprocess.Popen[bytes], BoundedCapture, bool]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RoundtripError("FAIL", "observer_adapter_deadline_exceeded")
    argv = [
        *policy.observer.adapter_argv,
        "--request",
        str(request_path),
        "--result",
        str(policy.observer.result_path),
    ]
    process, capture = _start_captured_process(
        argv,
        cwd=policy.observer.result_path.parent,
        env={
            "HOME": str(policy.observer.result_path.parent),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        },
        limit=policy.budgets.process_log_bytes,
        grace=policy.budgets.cleanup_grace_s,
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RoundtripError("FAIL", "observer_adapter_deadline_exceeded")
        process.wait(timeout=remaining)
    except BaseException:
        _cleanup_unreturned_process(process, capture, policy.budgets.cleanup_grace_s)
        raise
    stopped = stop_owned_process(
        OwnedProcess("observer-adapter", process, capture, request_path),
        policy.budgets.cleanup_grace_s,
    )
    return process, capture, bool(stopped["process_group_clear"] and stopped["capture_drained"])


def validate_observer_result(
    document: Mapping[str, Any],
    *,
    policy: Policy,
    access: AccessManifest,
    bot: Mapping[str, Any],
    inbound_text: str,
    request_issued_at: dt.datetime,
) -> dict[str, Any]:
    _exact_keys(
        document,
        required={
            "schema",
            "attempt_id",
            "candidate_sha",
            "candidate_manifest_sha256",
            "contour_id",
            "canary",
            "chat_id",
            "user_id",
            "bot_user_id",
            "observer_mode",
            "window_complete",
            "inbound_message",
            "outbound_messages",
            "observed_at",
        },
        where="observer result",
    )
    expected = {
        "schema": OBSERVER_RESULT_SCHEMA,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "contour_id": access.contour_id,
        "canary": policy.canary,
        "chat_id": access.chat_id,
        "user_id": access.user_id,
        "bot_user_id": bot["bot_user_id"],
        "observer_mode": policy.observer.mode,
        "window_complete": True,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise RoundtripError("FAIL", "observer_binding_mismatch", key)
    observed_at = _parse_utc(document["observed_at"], "observer_result.observed_at")

    inbound = _mapping(document["inbound_message"], "observer_result.inbound_message")
    _exact_keys(
        inbound,
        required={"message_id", "from_user_id", "chat_id", "text", "sent_at"},
        where="observer result inbound",
    )
    inbound_message_id = _integer(
        inbound["message_id"], "observer inbound message_id", minimum=1, maximum=10**20 - 1
    )
    if (
        inbound.get("from_user_id") != access.user_id
        or inbound.get("chat_id") != access.chat_id
        or inbound.get("text") != inbound_text
    ):
        raise RoundtripError("FAIL", "observer_inbound_mismatch")
    inbound_at = _parse_utc(inbound["sent_at"], "observer_result.inbound.sent_at")
    if inbound_at < request_issued_at - dt.timedelta(seconds=5):
        raise RoundtripError("FAIL", "observer_inbound_predates_request")

    outbound_raw = document["outbound_messages"]
    if (
        not isinstance(outbound_raw, list)
        or not 1 <= len(outbound_raw) <= policy.budgets.max_outbound_bot_posts
    ):
        raise RoundtripError("FAIL", "observer_outbound_cardinality_invalid")
    outbound: list[dict[str, Any]] = []
    outbound_times: list[dt.datetime] = []
    for index, item in enumerate(outbound_raw):
        row = _mapping(item, f"observer_result.outbound_messages[{index}]")
        _exact_keys(
            row,
            required={"message_id", "from_bot_id", "chat_id", "text", "observed_at"},
            where=f"observer outbound {index}",
        )
        message_id = _integer(
            row["message_id"], f"observer outbound {index} message_id", minimum=1, maximum=10**20 - 1
        )
        text = _string(row["text"], f"observer outbound {index} text", maximum=65536)
        if row.get("from_bot_id") != bot["bot_user_id"] or row.get("chat_id") != access.chat_id:
            raise RoundtripError("FAIL", "observer_outbound_actor_mismatch")
        outbound_at = _parse_utc(row["observed_at"], f"observer outbound {index} observed_at")
        if outbound_at < inbound_at:
            raise RoundtripError("FAIL", "observer_outbound_predates_inbound")
        outbound_times.append(outbound_at)
        outbound.append({"message_id": message_id, "text": text})
    matching = [row for row in outbound if policy.canary in row["text"]]
    if len(matching) != 1:
        raise RoundtripError("FAIL", "observer_canary_delivery_not_exactly_once")
    if len({row["message_id"] for row in outbound}) != len(outbound):
        raise RoundtripError("FAIL", "observer_duplicate_message_id")
    if observed_at < max(outbound_times) or observed_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5):
        raise RoundtripError("FAIL", "observer_result_time_invalid")
    return {
        "inbound_message_id": inbound_message_id,
        "inbound_text": inbound_text,
        "matching_outbound_message_id": matching[0]["message_id"],
        "matching_outbound_text_sha256": _sha256_bytes(matching[0]["text"].encode("utf-8")),
        "matching_canary_count": 1,
        "outbound_count": len(outbound),
        "observer_mode": policy.observer.mode,
        "window_complete": True,
    }


def _read_sqlite(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise RoundtripError("FAIL", "durable_database_missing", str(path))
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def inspect_durable_outcome(
    policy: Policy,
    access: AccessManifest,
    observer: Mapping[str, Any],
) -> dict[str, Any]:
    expected_text = str(observer["inbound_text"])
    inbound_message_id = int(observer["inbound_message_id"])
    visible_outbound_id = int(observer["matching_outbound_message_id"])
    main = _read_sqlite(policy.contour.database_path)
    inbox = _read_sqlite(policy.contour.inbox_db_path)
    try:
        raw_rows = main.execute(
            """SELECT id,user_id,source,source_ref,raw_content,metadata_json
                 FROM raw_objects
                WHERE source='telegram' AND deleted_at IS NULL
                ORDER BY created_at,id"""
        ).fetchall()
        if len(raw_rows) != policy.budgets.max_inbound_user_messages:
            raise RoundtripError("FAIL", "durable_inbound_cardinality_invalid")
        raw = raw_rows[0]
        if raw["user_id"] != OWNER_USER_ID or raw["raw_content"] != expected_text:
            raise RoundtripError("FAIL", "durable_inbound_content_or_owner_mismatch")
        source_ref = str(raw["source_ref"])
        match = re.fullmatch(r"telegram-update:([0-9]{1,20})", source_ref)
        if match is None:
            raise RoundtripError("FAIL", "durable_inbound_source_ref_invalid")
        update_id = int(match.group(1))
        try:
            metadata = json.loads(str(raw["metadata_json"]))
        except json.JSONDecodeError as exc:
            raise RoundtripError("FAIL", "durable_inbound_metadata_invalid") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("chat_id") != access.chat_id
            or metadata.get("telegram_message_id") != inbound_message_id
            or metadata.get("uploaded_by") != OWNER_USER_ID
            or metadata.get("channel") != "telegram-bridge"
        ):
            raise RoundtripError("FAIL", "durable_inbound_metadata_mismatch")

        identity_rows = main.execute(
            """SELECT user_id FROM user_identities
                WHERE source='telegram' AND external_id=?""",
            (str(access.user_id),),
        ).fetchall()
        if len(identity_rows) != 1 or identity_rows[0]["user_id"] != OWNER_USER_ID:
            raise RoundtripError("FAIL", "durable_user_identity_mismatch")

        idempotency = main.execute(
            """SELECT user_id,state FROM request_idempotency
                WHERE request_key=?""",
            (source_ref,),
        ).fetchall()
        if (
            len(idempotency) != 1
            or idempotency[0]["user_id"] != OWNER_USER_ID
            or idempotency[0]["state"] != "complete"
        ):
            raise RoundtripError("FAIL", "signed_backend_processing_not_complete")

        user_messages = main.execute(
            """SELECT id FROM messages
                WHERE user_id=? AND role='user' AND content=?""",
            (OWNER_USER_ID, expected_text),
        ).fetchall()
        if len(user_messages) != 1:
            raise RoundtripError("FAIL", "backend_user_message_cardinality_invalid")

        contexts = inbox.execute(
            """SELECT chat_id,telegram_message_id,backend_message_id
                 FROM outbound_reply_context
                WHERE chat_id=?
                ORDER BY telegram_message_id""",
            (access.chat_id,),
        ).fetchall()
        context_matches: list[tuple[int, str, str]] = []
        for context in contexts:
            backend_id = str(context["backend_message_id"])
            messages = main.execute(
                """SELECT content FROM messages
                    WHERE id=? AND user_id=? AND role='assistant'""",
                (backend_id, OWNER_USER_ID),
            ).fetchall()
            if len(messages) == 1 and policy.canary in str(messages[0]["content"]):
                context_matches.append(
                    (
                        int(context["telegram_message_id"]),
                        backend_id,
                        str(messages[0]["content"]),
                    )
                )
        if len(context_matches) != 1:
            raise RoundtripError("FAIL", "botapi_receipt_not_exactly_once")
        telegram_message_id, backend_message_id, assistant_content = context_matches[0]
        if telegram_message_id != visible_outbound_id:
            raise RoundtripError("FAIL", "botapi_receipt_visible_readback_mismatch")

        offset_rows = inbox.execute("SELECT value FROM state WHERE key='offset'").fetchall()
        if len(offset_rows) != 1 or int(offset_rows[0]["value"]) <= update_id:
            raise RoundtripError("FAIL", "durable_telegram_offset_missing")
        remaining_updates = inbox.execute("SELECT COUNT(*) AS count FROM updates").fetchone()
        if remaining_updates is None or int(remaining_updates["count"]) != 0:
            raise RoundtripError("FAIL", "telegram_update_not_retired")

        return {
            "owner_user_id": OWNER_USER_ID,
            "telegram_update_id": update_id,
            "telegram_message_id": inbound_message_id,
            "source_ref_sha256": _sha256_bytes(source_ref.encode("utf-8")),
            "raw_content_sha256": _sha256_bytes(expected_text.encode("utf-8")),
            "raw_inbound_count": 1,
            "backend_user_message_count": 1,
            "idempotency_complete": True,
            "botapi_receipt_message_id": telegram_message_id,
            "botapi_receipt_count": 1,
            "backend_assistant_message_id": backend_message_id,
            "backend_assistant_content_sha256": _sha256_bytes(assistant_content.encode("utf-8")),
            "visible_destination_message_id": visible_outbound_id,
            "remaining_inbox_updates": 0,
            "durable_offset": int(offset_rows[0]["value"]),
        }
    finally:
        inbox.close()
        main.close()


def evaluate_bound_roundtrip(
    *,
    policy: Policy,
    access: AccessManifest,
    bot: Mapping[str, Any],
    observer: Mapping[str, Any],
    durable: Mapping[str, Any],
    origins: Sequence[Mapping[str, Any]],
    cleanup_clear: bool,
    source_unchanged: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    inbound_verified = (
        durable.get("raw_content_sha256")
        == _sha256_bytes(str(observer.get("inbound_text", "")).encode("utf-8"))
        and durable.get("raw_inbound_count") == 1
    )
    processing_verified = (
        durable.get("backend_user_message_count") == 1 and durable.get("idempotency_complete") is True
    )
    receipt_verified = (
        isinstance(durable.get("botapi_receipt_message_id"), int)
        and int(durable.get("botapi_receipt_message_id") or 0) > 0
        and durable.get("botapi_receipt_count") == 1
    )
    destination_verified = (
        observer.get("window_complete") is True and observer.get("matching_canary_count") == 1
    )
    if len(origins) != 2 or {item.get("role") for item in origins} != {
        "server",
        "telegram-bridge",
    }:
        reasons.append("owned_process_origins_incomplete")
    if any(item.get("candidate_sha") != policy.candidate.candidate_sha for item in origins):
        reasons.append("owned_process_candidate_mismatch")
    if bot.get("bot_user_id") != access.bot_user_id or bot.get("webhook_empty") is not True:
        reasons.append("dedicated_bot_preflight_mismatch")
    if observer.get("matching_outbound_message_id") != durable.get("visible_destination_message_id"):
        reasons.append("visible_destination_binding_mismatch")
    if durable.get("botapi_receipt_message_id") != durable.get("visible_destination_message_id"):
        reasons.append("transport_destination_binding_mismatch")
    if durable.get("backend_user_message_count") != 1:
        reasons.append("backend_processing_missing")
    if durable.get("idempotency_complete") is not True:
        reasons.append("signed_admission_missing")
    if not inbound_verified:
        reasons.append("real_user_inbound_missing")
    if not receipt_verified:
        reasons.append("botapi_receipt_missing")
    if not destination_verified:
        reasons.append("independent_destination_readback_missing")
    if not cleanup_clear:
        reasons.append("owned_process_cleanup_incomplete")
    if not source_unchanged:
        reasons.append("candidate_source_changed")
    return {
        "case_outcome": ("LIVE_TELEGRAM_ROUNDTRIP_OBSERVED" if not reasons else "FAIL"),
        "case_pass": not reasons,
        "reasons": reasons,
        "binding": {
            "attempt_id": policy.attempt_id,
            "canary_sha256": _sha256_bytes(policy.canary.encode("utf-8")),
            "candidate_sha": policy.candidate.candidate_sha,
            "candidate_manifest_sha256": policy.candidate.manifest_sha256,
            "contour_id": access.contour_id,
            "bot_user_id": access.bot_user_id,
            "chat_id": access.chat_id,
            "user_id": access.user_id,
            "telegram_update_id": durable.get("telegram_update_id"),
            "inbound_message_id": durable.get("telegram_message_id"),
            "outbound_message_id": durable.get("visible_destination_message_id"),
        },
        "four_legs": {
            "real_user_inbound": inbound_verified,
            "friday_signed_admission_and_processing": processing_verified,
            "botapi_outbound_receipt": receipt_verified,
            "independent_visible_destination_readback": destination_verified,
        },
        "cleanup_clear": cleanup_clear,
        "source_unchanged": source_unchanged,
        "GO": False,
        "release_ready": False,
        "full_gate_credit": False,
        "live_case_credit_only": not reasons,
    }


def _report_failure(
    policy: Policy,
    error: RoundtripError,
    *,
    candidate: Mapping[str, Any] | None,
    contour: Mapping[str, Any] | None,
    cleanup: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "assignment": "ASTRA-R10-TELEGRAM-BUDGET-SOL-082",
        "attempt_id": policy.attempt_id,
        "case_outcome": error.outcome,
        "case_pass": False,
        "reason": error.code,
        "detail": error.detail,
        "candidate": dict(candidate or {}),
        "contour": dict(contour or {}),
        "cleanup": list(cleanup),
        "GO": False,
        "release_ready": False,
        "full_gate_credit": False,
        "live_case_credit_only": False,
        "finished_at": _utc_now(),
    }


def run_roundtrip(policy: Policy, evidence: EvidenceStore) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + policy.budgets.timeout_s
    candidate_summary: dict[str, Any] | None = None
    contour_summary: dict[str, Any] | None = None
    processes: list[OwnedProcess] = []
    origins: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    env: dict[str, str] = {}
    source_unchanged = False
    pending_error: RoundtripError | None = None
    report: dict[str, Any] | None = None
    access: AccessManifest | None = None
    bot: dict[str, Any] | None = None
    observer: dict[str, Any] | None = None
    durable: dict[str, Any] | None = None
    effect_spec_path: Path | None = None
    effect_root = evidence.root / "telegram-effects"
    effect_summary: dict[str, Any] | None = None
    try:
        if _is_within(policy.observer.result_path, evidence.root):
            raise RoundtripError("NOT_RUN", "observer_result_inside_driver_evidence")
        evidence.event("preflight", "STARTED")
        candidate_summary = _verified_source_map(policy.candidate)
        evidence.event("candidate", "VERIFIED", **candidate_summary)

        access_document = _load_json(
            policy.access.manifest_path,
            "access manifest",
            maximum=65536,
            private=True,
        )
        if sha256_file(policy.access.manifest_path) != policy.access.manifest_sha256:
            raise RoundtripError("NOT_RUN", "access_manifest_digest_mismatch")
        access = AccessManifest.from_document(
            access_document,
            policy=policy,
            now=dt.datetime.now(dt.UTC),
        )
        env, contour_summary = _validate_contour(policy, access)
        _assert_preflight_leases(policy)
        _assert_port_available(policy.contour.backend_origin)

        effect_spec_path, driver_guard = prepare_effect_ledger(
            evidence,
            policy=policy,
            access=access,
            token=env["FRIDAY_TELEGRAM_BOT_TOKEN"],
            deadline_monotonic_ns=int(deadline * 1_000_000_000),
        )
        bot = BotApi(
            env["FRIDAY_TELEGRAM_BOT_TOKEN"],
            proxy=env.get("FRIDAY_TELEGRAM_PROXY", ""),
            effect_guard=driver_guard,
        ).preflight(access)
        if time.monotonic() >= deadline:
            raise RoundtripError("NOT_RUN", "preflight_exhausted_attempt_deadline")
        evidence.event(
            "telegram_preflight",
            "VERIFIED",
            bot_user_id=bot["bot_user_id"],
            webhook_empty=True,
            pending_update_count=0,
        )

        os.mkdir(policy.contour.friday_home, 0o700)
        _fsync_directory(policy.contour.isolation_root)
        server = start_owned_process(
            policy,
            evidence,
            "server",
            effect_spec_path,
        )
        processes.append(server)
        wait_backend_ready(server, policy, deadline)
        server_origin = _read_origin_receipt(server, policy)
        evidence.account_external_file(server.origin_receipt)
        origins.append(server_origin)
        evidence.event("backend", "READY", pid=server.process.pid)

        bridge = start_owned_process(
            policy,
            evidence,
            "telegram-bridge",
            effect_spec_path,
        )
        processes.append(bridge)
        wait_bridge_ready(bridge, policy, deadline)
        bridge_origin = _read_origin_receipt(bridge, policy)
        evidence.account_external_file(bridge.origin_receipt)
        origins.append(bridge_origin)
        evidence.event("bridge", "READY", pid=bridge.process.pid)

        observer_request = build_observer_request(policy, access, bot)
        request_path = evidence.write("observer-request.json", observer_request)
        issued_ns = request_path.stat().st_mtime_ns
        evidence.event(
            "observer",
            "REQUESTED",
            mode=policy.observer.mode,
            request_path=str(request_path),
            result_path=str(policy.observer.result_path),
        )

        if policy.observer.mode == "external_user_adapter":
            adapter, capture, adapter_group_clear = _run_adapter_once(policy, request_path, deadline)
            adapter_log = {
                "schema": "friday.release-1-0-observer-adapter-log.v1",
                "returncode": adapter.returncode,
                "captured_bytes": len(capture.buffer),
                "discarded_bytes": capture.discarded,
                "process_group_clear": adapter_group_clear,
            }
            evidence.write("observer-adapter-log.json", adapter_log)
            if adapter.returncode != 0 or not adapter_group_clear:
                raise RoundtripError("FAIL", "observer_adapter_failed")
        while time.monotonic() < deadline:
            if any(item.process.poll() is not None for item in processes):
                raise RoundtripError("FAIL", "owned_process_exited_during_attempt")
            if policy.observer.result_path.exists():
                break
            time.sleep(policy.budgets.poll_interval_s)
        if not policy.observer.result_path.exists():
            raise RoundtripError("FAIL", "observer_result_missing")
        info = _secure_regular_file(
            policy.observer.result_path,
            "observer result",
            maximum=MAX_OBSERVER_BYTES,
        )
        if info.st_mtime_ns < issued_ns:
            raise RoundtripError("FAIL", "observer_result_predates_instruction")
        observer_raw = _load_json(
            policy.observer.result_path,
            "observer result",
            maximum=MAX_OBSERVER_BYTES,
            private=True,
        )
        observer_result_sha256 = sha256_file(policy.observer.result_path, maximum=MAX_OBSERVER_BYTES)
        observer = validate_observer_result(
            observer_raw,
            policy=policy,
            access=access,
            bot=bot,
            inbound_text=str(observer_request["inbound_text"]),
            request_issued_at=_parse_utc(observer_request["issued_at"], "observer_request.issued_at"),
        )
        evidence.write(
            "observer-sanitized.json",
            {
                "schema": "friday.release-1-0-observer-sanitized.v1",
                "attempt_id": policy.attempt_id,
                "candidate_sha": policy.candidate.candidate_sha,
                "observer_result_path": str(policy.observer.result_path),
                "observer_result_sha256": observer_result_sha256,
                "observer_result_bytes": info.st_size,
                **observer,
            },
        )
        evidence.event("observer", "VERIFIED", mode=policy.observer.mode)

        durable = inspect_durable_outcome(policy, access, observer)
        if time.monotonic() > deadline:
            raise RoundtripError("FAIL", "attempt_deadline_exceeded")
        if any(item.process.poll() is not None for item in processes):
            raise RoundtripError("FAIL", "owned_process_exited_before_observation_closed")
        evidence.write(
            "durable-observation.json",
            {
                "schema": "friday.release-1-0-telegram-durable-observation.v1",
                "attempt_id": policy.attempt_id,
                "candidate_sha": policy.candidate.candidate_sha,
                **durable,
            },
        )
        evidence.event(
            "four_legs",
            "VERIFIED",
            telegram_update_id=durable["telegram_update_id"],
            inbound_message_id=durable["telegram_message_id"],
            outbound_message_id=durable["visible_destination_message_id"],
        )
    except RoundtripError as exc:
        pending_error = exc
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        pending_error = RoundtripError("FAIL", "driver_internal_boundary_error", type(exc).__name__)
    finally:
        for process in reversed(processes):
            try:
                stopped = stop_owned_process(process, policy.budgets.cleanup_grace_s)
                cleanup.append(stopped)
                evidence.write(
                    f"log-{process.role}.json",
                    {
                        "schema": "friday.release-1-0-owned-process-log.v1",
                        **stopped,
                        "secrets_persisted": False,
                    },
                )
            except (OSError, ValueError, RoundtripError, subprocess.SubprocessError) as exc:
                cleanup.append(
                    {
                        "role": process.role,
                        "pid": process.process.pid,
                        "cleanup_error": type(exc).__name__,
                    }
                )
                if pending_error is None:
                    pending_error = RoundtripError("FAIL", "owned_process_cleanup_failed")
        cleanup_clear = bool(processes) and all(
            item.get("returncode") is not None
            and item.get("process_group_clear") is True
            and item.get("capture_drained") is True
            and "cleanup_error" not in item
            for item in cleanup
        )
        if candidate_summary is not None:
            try:
                after = _verified_source_map(policy.candidate)
                source_unchanged = after == candidate_summary
            except (OSError, ValueError, RoundtripError):
                source_unchanged = False
                if pending_error is None:
                    pending_error = RoundtripError("FAIL", "candidate_source_changed")
        try:
            _assert_env_pin(policy)
        except (OSError, ValueError, RoundtripError):
            if pending_error is None:
                pending_error = RoundtripError("FAIL", "contour_env_changed")
        try:
            own_leases = (
                policy.contour.state_dir / "backend.lock",
                policy.contour.inbox_db_path.with_name(policy.contour.inbox_db_path.name + ".lock"),
            )
            if any(path.exists() and _lease_is_active(path) for path in own_leases):
                cleanup_clear = False
                if pending_error is None:
                    pending_error = RoundtripError("FAIL", "owned_lease_still_active")
        except OSError:
            cleanup_clear = False
            if pending_error is None:
                pending_error = RoundtripError("FAIL", "owned_lease_cleanup_unknown")
        if evidence.external_reservation:
            if effect_root.exists():
                try:
                    if access is None:
                        raise RoundtripError("FAIL", "telegram_effect_access_binding_missing")
                    seal_effect_ledger(effect_root, policy.contour.env_file)
                    effect_summary = inspect_effect_ledger(
                        effect_root,
                        policy=policy,
                        access=access,
                        observer=observer,
                        durable=durable,
                        require_complete=pending_error is None,
                    )
                except (OSError, ValueError, RoundtripError) as exc:
                    if pending_error is None:
                        pending_error = (
                            exc
                            if isinstance(exc, RoundtripError)
                            else RoundtripError(
                                "FAIL", "telegram_effect_ledger_read_failed", type(exc).__name__
                            )
                        )
                try:
                    evidence.seal_external_tree(effect_root)
                except (OSError, ValueError, RoundtripError) as exc:
                    if pending_error is None:
                        pending_error = RoundtripError(
                            "FAIL", "telegram_effect_evidence_seal_failed", type(exc).__name__
                        )
            else:
                evidence.release_empty_external_reservation()
                if pending_error is None:
                    pending_error = RoundtripError("FAIL", "telegram_effect_ledger_missing")

    if pending_error is not None:
        report = _report_failure(
            policy,
            pending_error,
            candidate=candidate_summary,
            contour=contour_summary,
            cleanup=cleanup,
        )
        report["source_unchanged"] = source_unchanged
        report["telegram_effects"] = effect_summary
    else:
        assert access is not None
        assert bot is not None
        assert observer is not None
        assert durable is not None
        assert effect_summary is not None
        report = {
            "schema": REPORT_SCHEMA,
            "assignment": "ASTRA-R10-TELEGRAM-BUDGET-SOL-082",
            "attempt_id": policy.attempt_id,
            "candidate": candidate_summary,
            "contour": contour_summary,
            "observer": observer,
            "durable": durable,
            "origins": origins,
            "telegram_effects": effect_summary,
            "cleanup": cleanup,
            **evaluate_bound_roundtrip(
                policy=policy,
                access=access,
                bot=bot,
                observer=observer,
                durable=durable,
                origins=origins,
                cleanup_clear=cleanup_clear,
                source_unchanged=source_unchanged,
            ),
            "elapsed_s": round(time.monotonic() - started, 6),
            "finished_at": _utc_now(),
        }
    evidence.write("report.json", report)
    if evidence.actual_size() != evidence.used:
        raise RoundtripError("FAIL", "evidence_accounting_mismatch")
    if evidence.actual_size() > policy.budgets.max_evidence_bytes:
        raise RoundtripError("FAIL", "evidence_budget_exceeded_after_report")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one finite, isolated, real-user Telegram roundtrip")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = load_policy(args.policy)
        evidence_root = _absolute_path(str(args.evidence_dir), "evidence_dir")
        if _is_within(evidence_root, policy.candidate.source_root) or _is_within(
            evidence_root, policy.contour.friday_home
        ):
            raise ValueError("evidence_dir overlaps source or isolated runtime")
        evidence = EvidenceStore(evidence_root, policy.budgets.max_evidence_bytes)
        report = run_roundtrip(policy, evidence)
    except (OSError, ValueError, RoundtripError) as exc:
        summary = {
            "schema": REPORT_SCHEMA,
            "case_outcome": exc.outcome if isinstance(exc, RoundtripError) else "NOT_RUN",
            "case_pass": False,
            "reason": exc.code if isinstance(exc, RoundtripError) else type(exc).__name__,
            "GO": False,
            "release_ready": False,
            "full_gate_credit": False,
        }
        print(json.dumps(summary, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "schema": REPORT_SCHEMA,
                "case_outcome": report["case_outcome"],
                "case_pass": report["case_pass"],
                "evidence_dir": str(evidence.root),
                "GO": False,
                "release_ready": False,
                "full_gate_credit": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report.get("case_pass") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
