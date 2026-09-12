#!/usr/bin/env python3
"""Finite, evidence-bearing soak of one persistent Friday application.

This profile is deliberately narrower than release acceptance.  It never emits
GO and its in-process destination is evidence about the delivery contract, not
proof of live Telegram delivery.  A native outer103 owner supplies containment,
an isolated HOME and an explicit policy.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import stat
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "friday.release-1-0-app-soak.v1"
POLICY_SCHEMA = "friday.release-1-0-app-soak-policy.v1"
EXPECTED_SCHEMA = "friday.release-1-0-app-soak-expected.v1"
MAX_POLICY_BYTES = 1 << 20
MAX_DURATION_SEC = 7 * 24 * 60 * 60
MAX_DEADLINE_SEC = MAX_DURATION_SEC + 3600
MAX_GLOBAL_REQUESTS = 16_384
MIN_CORE_HTTP_REQUESTS = 21
MAX_EVIDENCE_FILES = 100_000
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}\Z")
_CLAIM = re.compile(r"/api/notifications/[A-Za-z0-9_-]{1,128}/claim\Z")

OPERATION_STATUSES = frozenset({"PASS", "FAIL", "NOT_RUN", "BLOCKED"})
REMINDER_DEPENDENTS = (
    "reminder_due_execution",
    "notification_pending_read",
    "notification_claim",
    "destination_write_and_readback",
    "notification_ack",
    "reminder_due_replay",
    "notification_after_replay",
)
REQUIRED_OPERATIONS = (
    "owner_bootstrap",
    "observe_principals",
    "owner_upload",
    "owner_source_query",
    "foreign_source_query",
    "owner_artifact_read",
    "foreign_artifact_refusal",
    "reminder_public_creation",
    *REMINDER_DEPENDENTS,
)
_CONTINUABLE_FAILURES = frozenset({"reminder_creation_not_observed"})
_BASE_CONTINUATION_OPERATIONS = frozenset(
    {
        "owner_bootstrap",
        "observe_principals",
        "owner_upload",
        "owner_source_query",
        "foreign_source_query",
        "owner_artifact_read",
        "foreign_artifact_refusal",
    }
)
_BLOCKED_FAILURE_PREFIXES = (
    "concurrent_",
    "config_",
    "evidence_",
    "global_",
    "json_evidence_",
    "request_",
    "resource_",
    "session_",
)


class AppSoakError(RuntimeError):
    """Closed failure vocabulary; raw response material stays in evidence."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        clean = str(code or "").strip()
        if not _TOKEN.fullmatch(clean):
            clean = "app_soak_failure"
        super().__init__(clean)
        self.code = clean
        self.details = dict(details or {})


def _failure_operation_status(code: str) -> str:
    if code in {
        "operation_exception",
        "unexpected_app_soak_exception",
        "continuation_safety_unproved",
        "session_close_incomplete",
    } or code.startswith(_BLOCKED_FAILURE_PREFIXES):
        return "BLOCKED"
    return "FAIL"


def _tools_used_reference(value: Any) -> list[str] | str:
    if isinstance(value, list) and all(type(item) is str for item in value):
        return list(value)
    return "INVALID_SHAPE"


def _exact_keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise AppSoakError(code)


def _integer(value: Any, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AppSoakError(code)
    return value


def _number(value: Any, *, minimum: float, maximum: float, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AppSoakError(code)
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise AppSoakError(code)
    return parsed


def _token(value: Any, code: str) -> str:
    text = str(value or "")
    if not _TOKEN.fullmatch(text):
        raise AppSoakError(code)
    return text


def _sha256(value: Any, code: str) -> str:
    text = str(value or "")
    if not _SHA256.fullmatch(text):
        raise AppSoakError(code)
    return text


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class PrincipalPolicy:
    user_id: str
    chat_id: int
    preset_key: str

    @classmethod
    def from_value(cls, value: Any) -> PrincipalPolicy:
        if not isinstance(value, Mapping):
            raise AppSoakError("principal_object_required")
        _exact_keys(value, {"user_id", "chat_id", "preset_key"}, "principal_keys_invalid")
        preset = str(value["preset_key"] or "")
        if preset not in {"owner", "user"}:
            raise AppSoakError("principal_preset_invalid")
        return cls(
            _token(value["user_id"], "principal_user_id_invalid"),
            _integer(
                value["chat_id"],
                minimum=1,
                maximum=9_999_999_999_999_999_999,
                code="principal_chat_id_invalid",
            ),
            preset,
        )

    def to_payload(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "chat_id": self.chat_id, "preset_key": self.preset_key}


@dataclass(frozen=True)
class ResourcePolicy:
    max_rss_bytes: int
    max_fd_count: int
    max_thread_count: int
    max_database_bytes: int
    max_wal_bytes: int
    max_evidence_bytes: int

    @classmethod
    def from_value(cls, value: Any) -> ResourcePolicy:
        if not isinstance(value, Mapping):
            raise AppSoakError("resource_policy_object_required")
        keys = {
            "max_rss_bytes",
            "max_fd_count",
            "max_thread_count",
            "max_database_bytes",
            "max_wal_bytes",
            "max_evidence_bytes",
        }
        _exact_keys(value, keys, "resource_policy_keys_invalid")
        return cls(
            _integer(value["max_rss_bytes"], minimum=1 << 20, maximum=1 << 50, code="max_rss_bytes_invalid"),
            _integer(value["max_fd_count"], minimum=4, maximum=1_000_000, code="max_fd_count_invalid"),
            _integer(value["max_thread_count"], minimum=1, maximum=100_000, code="max_thread_count_invalid"),
            _integer(
                value["max_database_bytes"], minimum=4096, maximum=1 << 50, code="max_database_bytes_invalid"
            ),
            _integer(value["max_wal_bytes"], minimum=0, maximum=1 << 50, code="max_wal_bytes_invalid"),
            _integer(
                value["max_evidence_bytes"], minimum=4096, maximum=1 << 40, code="max_evidence_bytes_invalid"
            ),
        )

    def to_payload(self) -> dict[str, int]:
        return {
            "max_rss_bytes": self.max_rss_bytes,
            "max_fd_count": self.max_fd_count,
            "max_thread_count": self.max_thread_count,
            "max_database_bytes": self.max_database_bytes,
            "max_wal_bytes": self.max_wal_bytes,
            "max_evidence_bytes": self.max_evidence_bytes,
        }


@dataclass(frozen=True)
class SoakPolicy:
    run_id: str
    candidate_sha256: str
    suite_revision: str
    config_sha256: str
    duration_sec: float
    cadence_sec: float
    deadline_sec: float
    max_requests: int
    request_concurrency: int
    principals: tuple[PrincipalPolicy, ...]
    resources: ResourcePolicy

    @classmethod
    def from_value(cls, value: Any) -> SoakPolicy:
        if not isinstance(value, Mapping):
            raise AppSoakError("policy_object_required")
        keys = {
            "schema",
            "run_id",
            "candidate_sha256",
            "suite_revision",
            "config_sha256",
            "duration_sec",
            "cadence_sec",
            "deadline_sec",
            "max_requests",
            "request_concurrency",
            "principals",
            "resources",
        }
        _exact_keys(value, keys, "policy_keys_invalid")
        if value["schema"] != POLICY_SCHEMA:
            raise AppSoakError("policy_schema_invalid")
        run_id = str(value["run_id"] or "")
        if not _RUN_ID.fullmatch(run_id):
            raise AppSoakError("run_id_invalid")
        duration = _number(
            value["duration_sec"],
            minimum=0.25,
            maximum=MAX_DURATION_SEC,
            code="duration_invalid",
        )
        cadence = _number(
            value["cadence_sec"],
            minimum=0.25,
            maximum=3600,
            code="cadence_invalid",
        )
        deadline = _number(
            value["deadline_sec"],
            minimum=0.25,
            maximum=MAX_DEADLINE_SEC,
            code="deadline_invalid",
        )
        if cadence > duration or deadline < duration:
            raise AppSoakError("policy_time_order_invalid")
        raw_principals = value["principals"]
        if not isinstance(raw_principals, list) or len(raw_principals) != 4:
            raise AppSoakError("four_principals_required")
        principals = tuple(PrincipalPolicy.from_value(item) for item in raw_principals)
        if len({item.user_id for item in principals}) != 4:
            raise AppSoakError("principal_user_alias")
        if len({item.chat_id for item in principals}) != 4:
            raise AppSoakError("principal_chat_alias")
        if Counter(item.preset_key for item in principals) != {"owner": 1, "user": 3}:
            raise AppSoakError("one_owner_three_users_required")
        request_concurrency = _integer(
            value["request_concurrency"],
            minimum=1,
            maximum=4,
            code="request_concurrency_invalid",
        )
        maximum = _integer(
            value["max_requests"],
            minimum=MIN_CORE_HTTP_REQUESTS,
            maximum=MAX_GLOBAL_REQUESTS,
            code="max_requests_invalid",
        )
        # This is a fail-fast capacity check, not a promise to spend the cap.
        worst_rounds = math.floor(duration / cadence)
        if maximum < MIN_CORE_HTTP_REQUESTS + len(principals) * worst_rounds:
            raise AppSoakError("request_budget_cannot_cover_policy")
        return cls(
            run_id,
            _sha256(value["candidate_sha256"], "candidate_sha256_invalid"),
            _token(value["suite_revision"], "suite_revision_invalid"),
            _sha256(value["config_sha256"], "config_sha256_invalid"),
            duration,
            cadence,
            deadline,
            maximum,
            request_concurrency,
            principals,
            ResourcePolicy.from_value(value["resources"]),
        )

    @property
    def owner(self) -> PrincipalPolicy:
        return next(item for item in self.principals if item.preset_key == "owner")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "run_id": self.run_id,
            "candidate_sha256": self.candidate_sha256,
            "suite_revision": self.suite_revision,
            "config_sha256": self.config_sha256,
            "duration_sec": self.duration_sec,
            "cadence_sec": self.cadence_sec,
            "deadline_sec": self.deadline_sec,
            "max_requests": self.max_requests,
            "request_concurrency": self.request_concurrency,
            "principals": [item.to_payload() for item in self.principals],
            "resources": self.resources.to_payload(),
        }


def safe_settings_identity(settings: Any) -> dict[str, Any]:
    """Exact configuration identity with secret values deliberately absent."""

    profile = getattr(getattr(settings, "profile", None), "name", "")
    return {
        "schema": "friday.release-1-0-app-soak-config.v1",
        "home": str(Path(settings.home).resolve()),
        "database_path": str(Path(settings.database_path).resolve()),
        "files_dir": str(Path(settings.files_dir).resolve()),
        "profile": str(profile),
        "shared_archive": settings.shared_archive,
        "llm_enabled": settings.llm_enabled,
        "local_timezone": str(settings.local_timezone or "UTC"),
        "reminders_enabled": settings.reminders_enabled,
        "reminders_lead_days": settings.reminders_lead_days,
        "quiet_hours_start": settings.quiet_hours_start,
        "quiet_hours_end": settings.quiet_hours_end,
        "telegram_allowed_chat_ids": sorted(int(x) for x in settings.telegram_effective_allowed_chat_ids),
        "telegram_owner_chat_ids": sorted(int(x) for x in settings.telegram_owner_chat_ids),
        "api_token_configured": bool(settings.api_token),
        "bridge_secret_configured": bool(settings.telegram_bridge_secret),
    }


def validate_settings(settings: Any, policy: SoakPolicy, *, canonical_owner_id: str) -> dict[str, Any]:
    identity = safe_settings_identity(settings)
    if _digest(identity) != policy.config_sha256:
        raise AppSoakError("config_identity_changed")
    if settings.shared_archive is not False:
        raise AppSoakError("shared_archive_must_be_false")
    if settings.llm_enabled is not False:
        raise AppSoakError("model_must_be_disabled")
    if not settings.reminders_enabled:
        raise AppSoakError("reminders_must_be_enabled")
    if not settings.api_token or not settings.telegram_bridge_secret:
        raise AppSoakError("authentication_not_configured")
    if policy.owner.user_id != canonical_owner_id:
        raise AppSoakError("canonical_owner_policy_mismatch")
    allowed = set(settings.telegram_effective_allowed_chat_ids)
    if any(item.chat_id not in allowed for item in policy.principals):
        raise AppSoakError("principal_chat_not_allowlisted")
    if policy.owner.chat_id not in set(settings.telegram_owner_chat_ids):
        raise AppSoakError("owner_chat_not_configured")
    zone = ZoneInfo(str(settings.local_timezone or "UTC"))
    hour = datetime.now(zone).hour
    start, end = int(settings.quiet_hours_start), int(settings.quiet_hours_end)
    quiet = start != end and (
        (start < end and start <= hour < end) or (start > end and (hour >= start or hour < end))
    )
    if quiet:
        raise AppSoakError("current_time_is_quiet_hours")
    return identity


def build_expected(policy: SoakPolicy, *, local_day: str) -> dict[str, Any]:
    marker = policy.run_id[:16].upper()
    canary = f"APP_SOAK_FILE_{marker}"
    reminder = f"APP-SOAK-REMINDER-{marker}"
    file_text = f"owned canary {canary}\n"
    return {
        "schema": EXPECTED_SCHEMA,
        "run_id": policy.run_id,
        "candidate_sha256": policy.candidate_sha256,
        "suite_revision": policy.suite_revision,
        "config_sha256": policy.config_sha256,
        "bindings": [item.to_payload() for item in policy.principals],
        "owner_user_id": policy.owner.user_id,
        "owner_chat_id": policy.owner.chat_id,
        "foreign_user_id": next(item.user_id for item in policy.principals if item.preset_key == "user"),
        "file": {
            "filename": f"app-soak-{policy.run_id[:12]}.txt",
            "source_ref": f"app-soak:{policy.run_id}:owner-file",
            "text": file_text,
            "sha256": hashlib.sha256(file_text.encode("utf-8")).hexdigest(),
            "canary": canary,
        },
        "publication": {
            "request": f"На {local_day} поставь напоминание «{reminder}».",
            "canary": reminder,
            "occurred_at": local_day,
            "destination_chat_id": str(policy.owner.chat_id),
            "exact_body": f"🔔 Напоминание: «{reminder}» — сегодня.",
        },
    }


class PrivateBundle:
    """Create-only, owner-only evidence rooted at one new directory."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self._lock = threading.Lock()
        root.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(root, 0o700)
        self._fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        observed = os.fstat(self._fd)
        if observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
            os.close(self._fd)
            raise AppSoakError("evidence_root_not_private")

    def close(self) -> None:
        if getattr(self, "_fd", -1) >= 0:
            os.fsync(self._fd)
            os.close(self._fd)
            self._fd = -1

    def mkdir(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise AppSoakError("evidence_directory_name_invalid")
        os.mkdir(name, 0o700, dir_fd=self._fd)
        os.fsync(self._fd)
        return self.root / name

    def write_bytes(self, name: str, payload: bytes) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}", name):
            raise AppSoakError("evidence_filename_invalid")
        with self._lock:
            if self.bytes_written + len(payload) > self.max_bytes:
                raise AppSoakError("evidence_budget_exhausted")
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fd,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self.bytes_written += len(payload)
            os.fsync(self._fd)
        return self.root / name

    def write_json(self, name: str, value: Any) -> Path:
        return self.write_bytes(name, _canonical(value))


class BudgetGate:
    """Finite global and per-principal request admission."""

    def __init__(self, policy: SoakPolicy, *, started: float, clock: Callable[[], float]) -> None:
        self.policy = policy
        self.deadline_at = started + policy.deadline_sec
        self.clock = clock
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self.started = 0
        self.completed = 0
        self.errors = 0
        self.max_global_inflight = 0
        self.max_principal_inflight = 0

    def check_deadline(self) -> None:
        if self.clock() >= self.deadline_at:
            raise AppSoakError("global_deadline_exhausted")

    def begin(self, principal: str) -> None:
        with self._lock:
            if self.clock() >= self.deadline_at:
                raise AppSoakError("global_deadline_exhausted")
            if self.started >= self.policy.max_requests:
                raise AppSoakError("request_budget_exhausted")
            if principal in self._inflight:
                raise AppSoakError("concurrent_request_for_principal")
            if len(self._inflight) >= self.policy.request_concurrency:
                raise AppSoakError("global_request_concurrency_exceeded")
            self._inflight.add(principal)
            self.started += 1
            self.max_global_inflight = max(self.max_global_inflight, len(self._inflight))
            self.max_principal_inflight = max(self.max_principal_inflight, 1)

    def finish(self, principal: str, *, error: bool) -> None:
        with self._lock:
            if principal not in self._inflight:
                raise AppSoakError("request_budget_finish_without_start")
            self._inflight.remove(principal)
            if error:
                self.errors += 1
            else:
                self.completed += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "started": self.started,
                "completed": self.completed,
                "errors": self.errors,
                "inflight": len(self._inflight),
                "max_global_inflight": self.max_global_inflight,
                "max_principal_inflight": self.max_principal_inflight,
            }


class BudgetedClient:
    def __init__(self, client: Any, gate: BudgetGate, principal: str) -> None:
        self._client = client
        self._gate = gate
        self._principal = principal

    @contextmanager
    def stream(self, *args: Any, **kwargs: Any):
        self._gate.begin(self._principal)
        try:
            with self._client.stream(*args, **kwargs) as response:
                yield response
        except BaseException:
            self._gate.finish(self._principal, error=True)
            raise
        else:
            self._gate.finish(self._principal, error=False)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self._gate.begin(self._principal)
        try:
            response = self._client.request(*args, **kwargs)
        except BaseException:
            self._gate.finish(self._principal, error=True)
            raise
        self._gate.finish(self._principal, error=False)
        return response


class OperationRecorder:
    def __init__(self, bundle: PrivateBundle, check_deadline: Callable[[], None]) -> None:
        self.bundle = bundle
        self.check_deadline = check_deadline
        self.sequence = 0
        self.entries: list[dict[str, Any]] = []

    def perform(
        self,
        name: str,
        principal: str,
        action: Callable[[], tuple[Any, Mapping[str, Any]]],
    ) -> Any:
        self.check_deadline()
        self.sequence += 1
        prefix = f"operation-{self.sequence:04d}"
        started = time.monotonic_ns()
        self.bundle.write_json(
            prefix + ".start.json",
            {"name": name, "principal": principal, "started_monotonic_ns": started},
        )
        try:
            value, details = action()
        except BaseException as exc:
            code = exc.code if isinstance(exc, AppSoakError) else "operation_exception"
            terminal = {
                "name": name,
                "principal": principal,
                "status": _failure_operation_status(code),
                "reason_code": code,
                "exception_type": type(exc).__name__[:128],
                "elapsed_ns": time.monotonic_ns() - started,
            }
            if isinstance(exc, AppSoakError) and exc.details:
                terminal["details"] = exc.details
            self.bundle.write_json(prefix + ".error.json", terminal)
            self.entries.append(terminal)
            raise
        terminal = {
            "name": name,
            "principal": principal,
            "status": "PASS",
            "details": dict(details),
            "elapsed_ns": time.monotonic_ns() - started,
        }
        self.bundle.write_json(prefix + ".terminal.json", terminal)
        self.entries.append(terminal)
        return value

    def not_run(self, name: str, principal: str, *, depends_on: str, reason_code: str) -> None:
        self.sequence += 1
        outcome = {
            "name": name,
            "principal": principal,
            "status": "NOT_RUN",
            "reason_code": "dependency_failed",
            "depends_on": depends_on,
            "dependency_reason_code": reason_code,
        }
        self.bundle.write_json(f"operation-{self.sequence:04d}.not-run.json", outcome)
        self.entries.append(outcome)


@dataclass(frozen=True)
class CoreOutcome:
    observation: dict[str, Any]
    first_failure_code: str = ""
    continuation_allowed: bool = False
    continuation_stop_code: str = ""


class AdminObserver:
    """Owner bearer setup through real admin APIs, with no token in evidence."""

    def __init__(self, client: BudgetedClient, evidence: Path, *, max_response_bytes: int = 8 << 20) -> None:
        evidence.mkdir(mode=0o700)
        self.client = client
        self.root = evidence
        self.sequence = 0
        self.max_response_bytes = max_response_bytes

    def post(self, path: str, payload: Mapping[str, Any], token: str) -> tuple[int, Mapping[str, Any]]:
        if path not in {"/api/admin/users", "/api/admin/identities"}:
            raise AppSoakError("admin_route_outside_setup")
        body = _canonical(payload).rstrip(b"\n")
        self.sequence += 1
        prefix = self.root / f"request-{self.sequence:06d}"
        _exclusive_write(prefix.with_suffix(".request.bin"), body)
        _exclusive_json(
            prefix.with_suffix(".start.json"),
            {
                "method": "POST",
                "path": path,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "request_bytes": len(body),
            },
        )
        started = time.monotonic_ns()
        try:
            response = self.client.request(
                "POST",
                path,
                content=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                follow_redirects=False,
            )
            raw = bytes(response.content)
            if len(raw) > self.max_response_bytes:
                raise AppSoakError("admin_response_oversized")
            _exclusive_write(prefix.with_suffix(".response.bin"), raw)
            _exclusive_json(
                prefix.with_suffix(".terminal.json"),
                {
                    "status_code": response.status_code,
                    "response_sha256": hashlib.sha256(raw).hexdigest(),
                    "response_bytes": len(raw),
                    "elapsed_ns": time.monotonic_ns() - started,
                },
            )
        except BaseException as exc:
            _exclusive_json(
                prefix.with_suffix(".error.json"),
                {
                    "outcome": "INCOMPLETE_OR_ERROR",
                    "exception_type": type(exc).__name__[:128],
                    "elapsed_ns": time.monotonic_ns() - started,
                },
            )
            raise
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AppSoakError("admin_response_json_invalid") from None
        if not isinstance(value, dict):
            raise AppSoakError("admin_response_object_required")
        return int(response.status_code), value


def _exclusive_write(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _exclusive_json(path: Path, value: Any) -> None:
    _exclusive_write(path, _canonical(value))


def _query_pairs(path: str) -> tuple[str, list[tuple[str, str]]]:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise AppSoakError("signed_route_invalid")
    return parsed.path, parse_qsl(parsed.query, keep_blank_values=True)


def app_route_allowed(method: str, target: str) -> bool:
    path, pairs = _query_pairs(target)
    keys = [key for key, _ in pairs]
    if method == "GET" and path == "/api/knowledge/sources":
        return (
            len(pairs) == 2
            and set(keys) == {"q", "limit"}
            and len(set(keys)) == 2
            and bool(dict(pairs)["q"])
            and dict(pairs)["limit"] == "20"
        )
    if method == "GET" and path == "/api/notifications/pending":
        return pairs == [("limit", "100")]
    if method == "POST" and (path == "/api/notifications/ack" or _CLAIM.fullmatch(path)):
        return not pairs
    if pairs:
        return False
    return (
        method == "GET"
        and (
            path in {"/api/me", "/api/files"}
            or re.fullmatch(r"/api/files/[A-Za-z0-9_-]{1,128}", path) is not None
        )
    ) or (method == "POST" and path in {"/api/chat", "/api/files"})


def app_session_type() -> type[Any]:
    from tools.release_1_0_soak_client import SoakIngressError, SoakSession

    class AppSession(SoakSession):
        @staticmethod
        def _route(method: str, path: str) -> None:
            if not app_route_allowed(method, path):
                raise SoakIngressError("route_outside_app_soak")

        def bytes_request(self, method: str, path: str):
            return self._dispatch(method, path, b"", "application/octet-stream")

    return AppSession


@dataclass(frozen=True)
class ResourceSample:
    sequence: int
    monotonic_ns: int
    elapsed_ns: int
    rss_bytes: int
    fd_count: int
    thread_count: int
    database_bytes: int
    wal_bytes: int
    evidence_bytes: int

    def to_payload(self) -> dict[str, int]:
        return {
            "sequence": self.sequence,
            "monotonic_ns": self.monotonic_ns,
            "elapsed_ns": self.elapsed_ns,
            "rss_bytes": self.rss_bytes,
            "fd_count": self.fd_count,
            "thread_count": self.thread_count,
            "database_bytes": self.database_bytes,
            "wal_bytes": self.wal_bytes,
            "evidence_bytes": self.evidence_bytes,
        }


def _tree_bytes(root: Path) -> int:
    total = 0
    count = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_EVIDENCE_FILES:
                    raise AppSoakError("evidence_file_count_exceeded")
                if entry.is_symlink():
                    raise AppSoakError("evidence_symlink_forbidden")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    raise AppSoakError("evidence_special_file_forbidden")
    return total


def sample_resources(
    *,
    sequence: int,
    started_ns: int,
    database_path: Path,
    evidence_root: Path,
) -> ResourceSample:
    status: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    rss = status.get("VmRSS", "")
    threads = status.get("Threads", "")
    if not rss.endswith(" kB") or not threads.isdigit():
        raise AppSoakError("resource_sampler_unavailable")
    now_ns = time.monotonic_ns()
    database_bytes = database_path.stat().st_size if database_path.is_file() else 0
    wal_path = Path(str(database_path) + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    return ResourceSample(
        sequence,
        now_ns,
        now_ns - started_ns,
        int(rss[:-3].strip()) * 1024,
        _fd_count(),
        int(threads),
        database_bytes,
        wal_bytes,
        _tree_bytes(evidence_root),
    )


def _fd_count() -> int:
    with os.scandir("/proc/self/fd") as entries:
        return sum(1 for _ in entries)


def resource_failures(sample: ResourceSample, policy: ResourcePolicy) -> list[str]:
    failures = []
    comparisons = (
        ("rss_budget_exceeded", sample.rss_bytes, policy.max_rss_bytes),
        ("fd_budget_exceeded", sample.fd_count, policy.max_fd_count),
        ("thread_budget_exceeded", sample.thread_count, policy.max_thread_count),
        ("database_budget_exceeded", sample.database_bytes, policy.max_database_bytes),
        ("wal_budget_exceeded", sample.wal_bytes, policy.max_wal_bytes),
        ("evidence_budget_exceeded", sample.evidence_bytes, policy.max_evidence_bytes),
    )
    for code, observed, limit in comparisons:
        if observed > limit:
            failures.append(code)
    return failures


def _require_status(response: Any, expected: int, code: str) -> Mapping[str, Any]:
    if response.status_code != expected:
        raise AppSoakError(code)
    return response.object()


def _require_object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AppSoakError(code)
    return value


def validate_foreign_source_response(
    body: Mapping[str, Any],
    *,
    canary: str,
    raw_id: str,
    source_ref: str,
) -> None:
    """Allow only the exact input echo; source-derived fields remain leak checked."""
    if type(body.get("count")) is not int or body["count"] != 0 or body.get("items") != []:
        raise AppSoakError("foreign_source_visible")
    if body.get("query") != canary:
        raise AppSoakError("foreign_source_query_echo_invalid")
    # The endpoint explicitly returns q as query, even for an empty search.
    # Exclude only that verified field, never arbitrary occurrences of the canary.
    observed = _canonical({key: value for key, value in body.items() if key != "query"})
    if any(marker.encode("utf-8") in observed for marker in (canary, raw_id, source_ref)):
        raise AppSoakError("foreign_source_leaked")
    if set(body) != {"query", "items", "count", "excludes"} or body.get("excludes") != "ignored":
        raise AppSoakError("foreign_source_response_shape_changed")


def _reminder_creation_rows(
    storage: Any,
    *,
    reminder_name: str,
) -> list[dict[str, Any]]:
    rows = storage.execute(
        """SELECT e.id AS entity_id,e.user_id,e.name,e.entity_type,
                  et.user_id AS time_user_id,et.occurred_at,et.occurred_end,
                  et.precision,et.source,p.person_id,p.privacy_kind
             FROM entities e
        LEFT JOIN entity_time et ON et.entity_id=e.id
        LEFT JOIN private_entity_owners p ON p.entity_id=e.id
            WHERE e.name=? AND e.deleted_at IS NULL
         ORDER BY e.id""",
        (reminder_name,),
    ).fetchall()
    return [
        {
            "entity_id": str(row["entity_id"]),
            "user_id": str(row["user_id"]),
            "name": str(row["name"]),
            "entity_type": str(row["entity_type"]),
            "time_user_id": None if row["time_user_id"] is None else str(row["time_user_id"]),
            "occurred_at": None if row["occurred_at"] is None else str(row["occurred_at"]),
            "occurred_end": None if row["occurred_end"] is None else str(row["occurred_end"]),
            "precision": None if row["precision"] is None else str(row["precision"]),
            "source": None if row["source"] is None else str(row["source"]),
            "person_id": None if row["person_id"] is None else str(row["person_id"]),
            "privacy_kind": None if row["privacy_kind"] is None else str(row["privacy_kind"]),
        }
        for row in rows
    ]


def _reminder_creation_failure(
    *,
    tools_used: Any,
    rows: Sequence[Mapping[str, Any]],
    owner_user_id: str,
    reminder_name: str,
    occurred_at: str,
) -> str:
    if not isinstance(tools_used, list) or any(type(item) is not str for item in tools_used):
        return "reminder_tool_attribution_invalid"
    exact_row = (
        len(rows) == 1
        and isinstance(rows[0], Mapping)
        and rows[0].get("user_id") == owner_user_id
        and rows[0].get("name") == reminder_name
        and rows[0].get("entity_type") == "event"
        and rows[0].get("time_user_id") == owner_user_id
        and rows[0].get("occurred_at") == occurred_at
        and rows[0].get("occurred_end") is None
        and rows[0].get("precision") == "day"
        and rows[0].get("source") == f"reminder:{owner_user_id}"
        and rows[0].get("person_id") == owner_user_id
        and rows[0].get("privacy_kind") == "reminder"
    )
    if tools_used == ["remind"] and exact_row:
        return ""
    if tools_used == [] and len(rows) == 0:
        return "reminder_creation_not_observed"
    if len(rows) > 1:
        return "reminder_duplicate_effect_observed"
    if len(rows) == 1 and not exact_row:
        return "reminder_effect_identity_invalid"
    if rows:
        return "reminder_unattributed_effect_observed"
    if tools_used:
        return "reminder_tool_effect_without_durable_row"
    return "reminder_creation_evidence_conflict"


def continuation_safety_proved(
    *,
    failure_code: str,
    operations: Sequence[Mapping[str, Any]],
    sessions: Mapping[str, Any],
    policy: SoakPolicy,
    reminder_effect: Mapping[str, Any],
) -> bool:
    """Only the observed no-tool/no-effect case may continue with GET /api/me."""
    if failure_code not in _CONTINUABLE_FAILURES:
        return False
    by_name = {str(item.get("name") or ""): item for item in operations if isinstance(item, Mapping)}
    if any(by_name.get(name, {}).get("status") != "PASS" for name in _BASE_CONTINUATION_OPERATIONS):
        return False
    if set(sessions) != {item.user_id for item in policy.principals}:
        return False
    return (
        reminder_effect.get("tools_used") == []
        and reminder_effect.get("rows") == []
        and reminder_effect.get("http_status") == 200
    )


def _run_core(
    *,
    app: Any,
    settings: Any,
    policy: SoakPolicy,
    expected: Mapping[str, Any],
    sessions: Mapping[str, Any],
    admin: AdminObserver,
    operations: OperationRecorder,
    bundle: PrivateBundle,
) -> CoreOutcome:
    from friday.organs import ServiceContext
    from friday.organs.reminders import scan_reminders
    from tools.release_1_0_soak_client import observe_isolated_principals

    owner = policy.owner
    owner_session = sessions[owner.user_id]
    foreign = next(item for item in policy.principals if item.preset_key == "user")
    foreign_session = sessions[foreign.user_id]

    def bootstrap() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json("GET", "/api/me")
        body = _require_status(response, 200, "owner_bootstrap_refused")
        actor = _require_object(body.get("actor"), "owner_bootstrap_actor_missing")
        if actor.get("user_id") != owner.user_id or actor.get("preset_key") != "owner":
            raise AppSoakError("owner_bootstrap_binding_invalid")
        return body, {"status_code": 200, "user_id": actor["user_id"], "preset_key": "owner"}

    operations.perform("owner_bootstrap", owner.user_id, bootstrap)

    for principal in policy.principals:
        if principal.preset_key == "owner":
            continue

        def create_user(principal: PrincipalPolicy = principal) -> tuple[Any, Mapping[str, Any]]:
            status_code, body = admin.post(
                "/api/admin/users",
                {
                    "id": principal.user_id,
                    "preset_key": "user",
                    "source": "app-soak",
                    "display_name": f"App soak {principal.user_id}",
                    "metadata": {"app_soak_run_id": policy.run_id},
                },
                settings.api_token,
            )
            user = _require_object(body.get("user"), "created_user_missing")
            if status_code != 200 or user.get("id") != principal.user_id or user.get("preset_key") != "user":
                raise AppSoakError("created_user_binding_invalid")
            return body, {"status_code": status_code, "user_id": principal.user_id, "preset_key": "user"}

        operations.perform(f"create_user:{principal.user_id}", owner.user_id, create_user)

        def link_identity(principal: PrincipalPolicy = principal) -> tuple[Any, Mapping[str, Any]]:
            status_code, body = admin.post(
                "/api/admin/identities",
                {
                    "source": "telegram",
                    "external_id": str(principal.chat_id),
                    "user_id": principal.user_id,
                },
                settings.api_token,
            )
            identity = _require_object(body.get("identity"), "linked_identity_missing")
            if (
                status_code != 200
                or identity.get("source") != "telegram"
                or identity.get("external_id") != str(principal.chat_id)
                or identity.get("user_id") != principal.user_id
            ):
                raise AppSoakError("linked_identity_invalid")
            return body, {
                "status_code": status_code,
                "user_id": principal.user_id,
                "chat_id": str(principal.chat_id),
            }

        operations.perform(f"link_identity:{principal.user_id}", owner.user_id, link_identity)

    def identities() -> tuple[Any, Mapping[str, Any]]:
        observed = observe_isolated_principals(
            [sessions[item.user_id] for item in policy.principals],
            app.state.storage,
            shared_archive=False,
        )
        wanted = tuple(item.user_id for item in policy.principals)
        if observed != wanted:
            raise AppSoakError("principal_order_or_binding_changed")
        return observed, {"user_ids": list(observed), "distinct": len(set(observed))}

    bindings = operations.perform("observe_principals", "all", identities)

    file_contract = _require_object(expected["file"], "expected_file_missing")
    file_bytes = str(file_contract["text"]).encode("utf-8")

    def upload() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.upload(
            filename=str(file_contract["filename"]),
            content=file_bytes,
            source_ref=str(file_contract["source_ref"]),
        )
        body = _require_status(response, 200, "owner_upload_refused")
        raw_id = str(body.get("raw_object_id") or "")
        if not _RAW_ID.fullmatch(raw_id):
            raise AppSoakError("owner_upload_handle_missing")
        return (raw_id, body), {"status_code": 200, "raw_object_id": raw_id}

    raw_id, _upload_body = operations.perform("owner_upload", owner.user_id, upload)

    query_target = "/api/knowledge/sources?q=" + quote(str(file_contract["canary"]), safe="") + "&limit=20"

    def owner_query() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json("GET", query_target)
        body = _require_status(response, 200, "owner_source_query_refused")
        items = body.get("items")
        if not isinstance(items, list):
            raise AppSoakError("owner_source_items_missing")
        matching = [
            item
            for item in items
            if isinstance(item, Mapping)
            and item.get("id") == raw_id
            and item.get("source_ref") == file_contract["source_ref"]
            and str(file_contract["canary"]) in str(item.get("excerpt") or "")
        ]
        if len(matching) != 1:
            raise AppSoakError("owner_source_attribution_invalid")
        return body, {"status_code": 200, "matching_sources": 1, "raw_object_id": raw_id}

    operations.perform("owner_source_query", owner.user_id, owner_query)

    def foreign_query() -> tuple[Any, Mapping[str, Any]]:
        response = foreign_session.json("GET", query_target)
        body = _require_status(response, 200, "foreign_source_query_refused")
        validate_foreign_source_response(
            body,
            canary=str(file_contract["canary"]),
            raw_id=raw_id,
            source_ref=str(file_contract["source_ref"]),
        )
        return body, {"status_code": 200, "count": 0, "leak": False}

    operations.perform("foreign_source_query", foreign.user_id, foreign_query)

    def owner_artifact() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.bytes_request("GET", f"/api/files/{raw_id}")
        if response.status_code != 200:
            raise AppSoakError("owner_artifact_read_refused")
        digest = hashlib.sha256(response.body).hexdigest()
        if digest != file_contract["sha256"]:
            raise AppSoakError("owner_artifact_digest_changed")
        return digest, {"status_code": 200, "sha256": digest}

    artifact_sha = operations.perform("owner_artifact_read", owner.user_id, owner_artifact)

    def foreign_artifact() -> tuple[Any, Mapping[str, Any]]:
        response = foreign_session.bytes_request("GET", f"/api/files/{raw_id}")
        if response.status_code != 404:
            raise AppSoakError("foreign_artifact_not_refused")
        if str(file_contract["canary"]).encode() in response.body:
            raise AppSoakError("foreign_artifact_leaked")
        return response.status_code, {"status_code": 404, "leak": False}

    foreign_artifact_status = operations.perform(
        "foreign_artifact_refusal", foreign.user_id, foreign_artifact
    )

    row = app.state.storage.execute(
        "SELECT user_id,source_ref,content_hash FROM raw_objects WHERE id=?",
        (raw_id,),
    ).fetchone()
    if (
        row is None
        or str(row["user_id"]) != owner.user_id
        or str(row["source_ref"]) != file_contract["source_ref"]
        or str(row["content_hash"]) != file_contract["sha256"]
    ):
        raise AppSoakError("independent_source_row_invalid")
    source_effect = {
        "schema": "friday.app-soak-source-effect.v1",
        "raw_object_id": raw_id,
        "user_id": str(row["user_id"]),
        "source_ref": str(row["source_ref"]),
        "content_hash": str(row["content_hash"]),
    }
    bundle.write_json("effect-source-row.json", source_effect)

    core: dict[str, Any] = {
        "bindings": list(bindings),
        "source": {
            "raw_object_id": raw_id,
            "owner_user_id": str(row["user_id"]),
            "source_ref": str(row["source_ref"]),
            "canary_observed": True,
            "matching_sources": 1,
        },
        "foreign": {
            "user_id": foreign.user_id,
            "source_count": 0,
            "source_leak": False,
            "artifact_status": foreign_artifact_status,
            "artifact_leak": False,
        },
        "artifact": {"sha256": artifact_sha, "readback": True},
        "publication": {"creation_status": "NOT_RUN"},
    }

    publication = _require_object(expected["publication"], "expected_publication_missing")

    reminder_effect: dict[str, Any] = {}

    def create_reminder() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json(
            "POST",
            "/api/chat",
            {
                "message": publication["request"],
                "enable_tools": True,
                "telegram_user": {
                    "id": owner.chat_id,
                    "first_name": "AppSoak",
                    "language_code": "ru",
                },
            },
        )
        body = _require_status(response, 200, "reminder_public_creation_refused")
        rows = _reminder_creation_rows(
            app.state.storage,
            reminder_name=str(publication["canary"]),
        )
        tools_used = body.get("tools_used")
        reminder_effect.update(
            {
                "schema": "friday.app-soak-reminder-creation-effect.v1",
                "owner_user_id": owner.user_id,
                "reminder_name": publication["canary"],
                "http_status": 200,
                "response_sha256": hashlib.sha256(response.body).hexdigest(),
                "response_ref": (f"session-{owner.user_id}/request-{response.sequence:06d}.response.bin"),
                "tools_used": _tools_used_reference(tools_used),
                "rows": rows,
            }
        )
        bundle.write_json("effect-reminder-creation.json", reminder_effect)
        failure = _reminder_creation_failure(
            tools_used=tools_used,
            rows=rows,
            owner_user_id=owner.user_id,
            reminder_name=str(publication["canary"]),
            occurred_at=str(publication["occurred_at"]),
        )
        if failure:
            raise AppSoakError(
                failure,
                details={
                    "expected": {"tools_used": ["remind"], "durable_rows": 1},
                    "actual": {
                        "tools_used": _tools_used_reference(tools_used),
                        "durable_rows": len(rows),
                        "effect_ref": "effect-reminder-creation.json",
                        "response_ref": reminder_effect["response_ref"],
                    },
                },
            )
        return body, {
            "status_code": 200,
            "response_sha256": hashlib.sha256(response.body).hexdigest(),
            "tools_used": ["remind"],
            "durable_rows": 1,
            "entity_id": rows[0]["entity_id"],
            "effect_ref": "effect-reminder-creation.json",
        }

    try:
        operations.perform("reminder_public_creation", owner.user_id, create_reminder)
    except AppSoakError as exc:
        creation_status = str(operations.entries[-1]["status"])
        for dependent in REMINDER_DEPENDENTS:
            principal = (
                "product-reminder-worker"
                if dependent in {"reminder_due_execution", "reminder_due_replay"}
                else owner.user_id
            )
            operations.not_run(
                dependent,
                principal,
                depends_on="reminder_public_creation",
                reason_code=exc.code,
            )
        core["publication"] = {
            "creation_status": creation_status,
            "creation_failure_code": exc.code,
            "notification_id": None,
            "destination": None,
            "durable_rows": len(reminder_effect.get("rows") or []),
            "durable_status": "NOT_RUN",
            "replayed": None,
            "delivery_class": "in_process_synthetic_destination",
        }
        continuation_allowed = continuation_safety_proved(
            failure_code=exc.code,
            operations=operations.entries,
            sessions=sessions,
            policy=policy,
            reminder_effect=reminder_effect,
        )
        return CoreOutcome(
            observation=core,
            first_failure_code=exc.code,
            continuation_allowed=continuation_allowed,
            continuation_stop_code=(
                "continuation_safety_unproved"
                if exc.code in _CONTINUABLE_FAILURES and not continuation_allowed
                else ""
            ),
        )

    context = ServiceContext(
        settings=settings,
        storage=app.state.storage,
        kg=app.state.kg,
        ingestion=app.state.ingestion,
    )

    def scan_once(name: str) -> None:
        operations.perform(
            name,
            "product-reminder-worker",
            lambda: (asyncio.run(scan_reminders(context)), {"completed": True}),
        )

    scan_once("reminder_due_execution")

    def pending() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json("GET", "/api/notifications/pending?limit=100")
        body = _require_status(response, 200, "notification_pending_refused")
        items = body.get("items")
        if not isinstance(items, list):
            raise AppSoakError("notification_pending_items_missing")
        matching = [
            item
            for item in items
            if isinstance(item, Mapping)
            and item.get("kind") == "reminder"
            and str(item.get("chat_id") or "") == publication["destination_chat_id"]
        ]
        if len(matching) != 1:
            raise AppSoakError("scheduled_publication_cardinality_invalid")
        pointer = dict(matching[0])
        if set(pointer) != {"id", "chat_id", "kind", "dedup_key"}:
            raise AppSoakError("scheduled_publication_pointer_shape_changed")
        return pointer, {"status_code": 200, "matching": 1, "notification_id": pointer["id"]}

    pointer = operations.perform("notification_pending_read", owner.user_id, pending)

    def claim() -> tuple[Any, Mapping[str, Any]]:
        notification_id = str(pointer["id"])
        response = owner_session.json(
            "POST",
            f"/api/notifications/{notification_id}/claim",
            {**pointer, "status_messages": False},
        )
        body = _require_status(response, 200, "notification_claim_refused")
        item = _require_object(body.get("item"), "notification_claim_item_missing")
        if (
            item.get("id") != notification_id
            or item.get("chat_id") != publication["destination_chat_id"]
            or item.get("kind") != "reminder"
            or item.get("body") != publication["exact_body"]
        ):
            raise AppSoakError("notification_claim_projection_invalid")
        return dict(item), {
            "status_code": 200,
            "notification_id": notification_id,
            "body_sha256": hashlib.sha256(str(item["body"]).encode()).hexdigest(),
        }

    claimed = operations.perform("notification_claim", owner.user_id, claim)

    destination_record = {
        "schema": "friday.app-soak-synthetic-destination.v1",
        "delivery_class": "in_process_synthetic_destination",
        "notification_id": claimed["id"],
        "chat_id": claimed["chat_id"],
        "body": claimed["body"],
    }

    def destination_write() -> tuple[Any, Mapping[str, Any]]:
        path = bundle.write_json("destination-000001.json", destination_record)
        observed = json.loads(path.read_text(encoding="utf-8"))
        if observed != destination_record:
            raise AppSoakError("destination_readback_changed")
        return observed, {
            "notification_id": observed["notification_id"],
            "chat_id": observed["chat_id"],
            "body_sha256": hashlib.sha256(observed["body"].encode()).hexdigest(),
        }

    destination = operations.perform(
        "destination_write_and_readback",
        str(publication["destination_chat_id"]),
        destination_write,
    )

    def ack() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json(
            "POST",
            "/api/notifications/ack",
            {"sent": [pointer["id"]], "failed": [], "uncertain": []},
        )
        body = _require_status(response, 200, "notification_ack_refused")
        states = body.get("state_ids")
        if not isinstance(states, Mapping) or pointer["id"] not in (states.get("sent") or []):
            raise AppSoakError("notification_ack_not_durable")
        return body, {"status_code": 200, "sent": 1, "notification_id": pointer["id"]}

    operations.perform("notification_ack", owner.user_id, ack)
    scan_once("reminder_due_replay")

    def after_delivery() -> tuple[Any, Mapping[str, Any]]:
        response = owner_session.json("GET", "/api/notifications/pending?limit=100")
        body = _require_status(response, 200, "notification_pending_after_refused")
        items = body.get("items")
        if not isinstance(items, list):
            raise AppSoakError("notification_pending_after_items_missing")
        if any(isinstance(item, Mapping) and item.get("id") == pointer["id"] for item in items):
            raise AppSoakError("scheduled_publication_repeated")
        return body, {"status_code": 200, "notification_absent": True}

    operations.perform("notification_after_replay", owner.user_id, after_delivery)

    rows = app.state.storage.execute(
        """SELECT id,user_id,chat_id,kind,dedup_key,body,status
             FROM outbound_notifications
            WHERE user_id=? AND kind='reminder' AND body=?""",
        (owner.user_id, publication["exact_body"]),
    ).fetchall()
    if (
        len(rows) != 1
        or str(rows[0]["id"]) != str(pointer["id"])
        or str(rows[0]["chat_id"]) != publication["destination_chat_id"]
        or str(rows[0]["status"]) != "sent"
    ):
        raise AppSoakError("independent_publication_row_invalid")
    publication_effect = {
        "schema": "friday.app-soak-publication-effect.v1",
        "id": str(rows[0]["id"]),
        "user_id": str(rows[0]["user_id"]),
        "chat_id": str(rows[0]["chat_id"]),
        "kind": str(rows[0]["kind"]),
        "dedup_key": str(rows[0]["dedup_key"]),
        "body": str(rows[0]["body"]),
        "status": str(rows[0]["status"]),
    }
    bundle.write_json("effect-publication-row.json", publication_effect)
    core["publication"] = {
        "creation_status": "PASS",
        "notification_id": str(pointer["id"]),
        "destination": destination,
        "durable_rows": 1,
        "durable_status": "sent",
        "replayed": False,
        "delivery_class": "in_process_synthetic_destination",
    }
    return CoreOutcome(observation=core)


def _record_sample(
    samples: list[ResourceSample],
    bundle: PrivateBundle,
    policy: SoakPolicy,
    *,
    started_ns: int,
    database_path: Path,
) -> None:
    sample = sample_resources(
        sequence=len(samples) + 1,
        started_ns=started_ns,
        database_path=database_path,
        evidence_root=bundle.root,
    )
    samples.append(sample)
    bundle.write_json(f"resource-{sample.sequence:06d}.json", sample.to_payload())
    failures = resource_failures(sample, policy.resources)
    if failures:
        raise AppSoakError(failures[0])


def _ping_round(
    sessions: Mapping[str, Any],
    policy: SoakPolicy,
    *,
    during: Callable[[], None] | None = None,
) -> None:
    def ping(principal: PrincipalPolicy) -> None:
        response = sessions[principal.user_id].json("GET", "/api/me")
        body = _require_status(response, 200, "cadence_identity_refused")
        actor = _require_object(body.get("actor"), "cadence_identity_actor_missing")
        if (
            actor.get("user_id") != principal.user_id
            or actor.get("preset_key") != principal.preset_key
            or actor.get("source") != "telegram-bridge"
        ):
            raise AppSoakError("cadence_identity_changed")

    with ThreadPoolExecutor(
        max_workers=policy.request_concurrency,
        thread_name_prefix="friday-app-soak",
    ) as executor:
        futures = [executor.submit(ping, principal) for principal in policy.principals]
        if during is not None:
            during()
        for future in futures:
            future.result()


def evaluate_profile(
    *,
    policy: SoakPolicy,
    expected: Mapping[str, Any],
    core: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    samples: Sequence[ResourceSample],
    budget: Mapping[str, Any],
    elapsed_sec: float,
    config_sha256: str,
    evidence_mode: str,
    duration_coverage: Mapping[str, Any] | None = None,
    additional_failure_codes: Sequence[str] = (),
) -> dict[str, Any]:
    failures: list[str] = []
    operation_items = [item for item in operations if isinstance(item, Mapping)]
    primary_failure = next(
        (
            str(item.get("reason_code") or "operation_failure_reason_missing")
            for item in operation_items
            if item.get("status") in {"FAIL", "BLOCKED"}
        ),
        next((str(code) for code in additional_failure_codes if code), ""),
    )
    names = [str(item.get("name") or "") for item in operation_items]
    if len(operation_items) != len(operations) or any(names.count(name) != 1 for name in REQUIRED_OPERATIONS):
        failures.append("required_operation_cardinality_invalid")
    if any(item.get("status") not in OPERATION_STATUSES for item in operation_items):
        failures.append("operation_status_invalid")
    for item in operation_items:
        if item.get("status") in {"FAIL", "BLOCKED"}:
            failures.append(str(item.get("reason_code") or "operation_failure_reason_missing"))
        elif item.get("status") == "NOT_RUN" and (
            item.get("reason_code") != "dependency_failed"
            or not item.get("depends_on")
            or not item.get("dependency_reason_code")
        ):
            failures.append("not_run_dependency_invalid")

    expected_bindings = [item["user_id"] for item in expected["bindings"]]
    if list(core.get("bindings") or []) != expected_bindings or len(set(expected_bindings)) != 4:
        failures.append("binding_observation_changed")
    source = core.get("source") if isinstance(core.get("source"), Mapping) else {}
    file_contract = expected["file"]
    if (
        source.get("owner_user_id") != expected["owner_user_id"]
        or source.get("source_ref") != file_contract["source_ref"]
        or source.get("canary_observed") is not True
        or source.get("matching_sources") != 1
        or not _RAW_ID.fullmatch(str(source.get("raw_object_id") or ""))
    ):
        failures.append("source_outcome_invalid")
    foreign = core.get("foreign") if isinstance(core.get("foreign"), Mapping) else {}
    if (
        foreign.get("user_id") != expected["foreign_user_id"]
        or foreign.get("source_count") != 0
        or foreign.get("source_leak") is not False
        or foreign.get("artifact_status") != 404
        or foreign.get("artifact_leak") is not False
    ):
        failures.append("foreign_tenant_negative_invalid")
    artifact = core.get("artifact") if isinstance(core.get("artifact"), Mapping) else {}
    if artifact.get("sha256") != file_contract["sha256"] or artifact.get("readback") is not True:
        failures.append("artifact_readback_invalid")

    by_name = {str(item.get("name") or ""): item for item in operation_items}
    publication = core.get("publication") if isinstance(core.get("publication"), Mapping) else {}
    reminder_creation = by_name.get("reminder_public_creation", {})
    reminder_branch_passed = reminder_creation.get("status") == "PASS" and all(
        by_name.get(name, {}).get("status") == "PASS" for name in REMINDER_DEPENDENTS
    )
    if reminder_branch_passed:
        destination = (
            publication.get("destination") if isinstance(publication.get("destination"), Mapping) else {}
        )
        publication_contract = expected["publication"]
        if (
            publication.get("creation_status") != "PASS"
            or publication.get("durable_rows") != 1
            or publication.get("durable_status") != "sent"
            or publication.get("replayed") is not False
            or publication.get("delivery_class") != "in_process_synthetic_destination"
            or destination.get("chat_id") != publication_contract["destination_chat_id"]
            or destination.get("body") != publication_contract["exact_body"]
            or destination.get("notification_id") != publication.get("notification_id")
        ):
            failures.append("scheduled_publication_invalid")
    elif reminder_creation.get("status") in {"FAIL", "BLOCKED"}:
        cause = reminder_creation.get("reason_code")
        if any(
            by_name.get(name, {}).get("status") != "NOT_RUN"
            or by_name.get(name, {}).get("depends_on") != "reminder_public_creation"
            or by_name.get(name, {}).get("dependency_reason_code") != cause
            for name in REMINDER_DEPENDENTS
        ):
            failures.append("dependent_outcomes_invalid")

    started_count = budget.get("started")
    completed_count = budget.get("completed")
    error_count = budget.get("errors")
    accounting_valid = (
        type(started_count) is int
        and type(completed_count) is int
        and type(error_count) is int
        and started_count >= 0
        and completed_count >= 0
        and error_count >= 0
        and completed_count + error_count == started_count
        and budget.get("inflight") == 0
        and type(budget.get("max_global_inflight")) is int
        and 0 <= budget["max_global_inflight"] <= policy.request_concurrency
        and type(budget.get("max_principal_inflight")) is int
        and 0 <= budget["max_principal_inflight"] <= 1
    )
    if not accounting_valid or (not failures and started_count < MIN_CORE_HTTP_REQUESTS):
        failures.append("request_accounting_invalid")

    coverage = dict(duration_coverage or {})
    duration_completed = coverage.get("status") in {
        "FULL_MIXED_WORKLOAD",
        "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL",
    }
    if (
        elapsed_sec > policy.deadline_sec
        or (duration_completed and elapsed_sec + 0.01 < policy.duration_sec)
        or (not failures and elapsed_sec + 0.01 < policy.duration_sec)
    ):
        failures.append("time_contract_invalid")
    if not samples:
        failures.append("resource_samples_missing")
    for sample in samples:
        failures.extend(resource_failures(sample, policy.resources))
    if config_sha256 != policy.config_sha256:
        failures.append("config_identity_changed")
    failures.extend(str(code) for code in additional_failure_codes if code)
    failures = list(dict.fromkeys(failures))
    if primary_failure in failures:
        failures.remove(primary_failure)
        failures.insert(0, primary_failure)

    statuses = [str(item.get("status") or "") for item in operation_items]
    blocked = "BLOCKED" in statuses or any(_failure_operation_status(code) == "BLOCKED" for code in failures)
    status = (
        "APP_SOAK_PROFILE_OBSERVED"
        if not failures and evidence_mode == "actual_app"
        else "SYNTHETIC_MECHANICS_OBSERVED"
        if not failures and evidence_mode == "synthetic"
        else "APP_SOAK_PROFILE_INCOMPLETE"
        if blocked
        else "APP_SOAK_PROFILE_FAILED"
    )
    outcome_counts = Counter(statuses)
    return {
        "schema": SCHEMA,
        "status": status,
        "failure_codes": failures,
        "functional_status": "BLOCKED" if blocked else "FAIL" if failures else "PASS",
        "evidence_mode": evidence_mode,
        "candidate_sha256": policy.candidate_sha256,
        "suite_revision": policy.suite_revision,
        "config_sha256": config_sha256,
        "elapsed_sec": round(elapsed_sec, 6),
        "request_accounting": dict(budget),
        "resource_samples": len(samples),
        "operation_outcomes": {
            name: outcome_counts.get(name, 0) for name in ("PASS", "FAIL", "NOT_RUN", "BLOCKED")
        },
        "duration_coverage": coverage,
        "delivery_class": "in_process_synthetic_destination",
        "live_telegram_proof": False,
        "full_gate_credit": False,
        "release_ready_claimed": False,
        "go_emitted": False,
    }


_REQUEST_EVIDENCE = re.compile(
    r"request-(?P<sequence>[0-9]{6})\."
    r"(?P<kind>request\.bin|response\.bin|start\.json|terminal\.json|error\.json)\Z"
)
_OPERATION_EVIDENCE = re.compile(
    r"operation-(?P<sequence>[0-9]{4})\.(?P<kind>start|terminal|error|not-run)\.json\Z"
)


def _bounded_bytes(path: Path, *, maximum: int = 8 << 20) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise AppSoakError("raw_evidence_file_invalid")
    try:
        return path.read_bytes()
    except OSError:
        raise AppSoakError("raw_evidence_read_failed") from None


def _request_attempts(
    root: Path,
    *,
    policy: SoakPolicy,
) -> tuple[list[dict[str, Any]], list[str], tuple[int, int, int]]:
    failures: list[str] = []
    attempts: list[dict[str, Any]] = []
    expected_directories = {"admin", *(f"session-{item.user_id}" for item in policy.principals)}
    observed_directories = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path.name == "admin" or path.name.startswith("session-"))
    }
    if observed_directories != expected_directories:
        failures.append("request_principal_identity_changed")
    groups = 0
    terminals = 0
    errors = 0
    for directory_name in sorted(observed_directories | expected_directories):
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            failures.append("request_evidence_directory_missing")
            continue
        grouped: dict[int, dict[str, Path]] = {}
        for evidence_path in directory.iterdir():
            match = _REQUEST_EVIDENCE.fullmatch(evidence_path.name)
            if match is None:
                if evidence_path.name.startswith("request-"):
                    failures.append("request_evidence_filename_invalid")
                continue
            grouped.setdefault(int(match["sequence"]), {})[match["kind"]] = evidence_path
        if sorted(grouped) != list(range(1, len(grouped) + 1)):
            failures.append("request_sequence_invalid")
        groups += len(grouped)
        for sequence, files in sorted(grouped.items()):
            if "start.json" not in files or "request.bin" not in files:
                failures.append("request_predecessor_missing")
                continue
            try:
                start = _read_json(files["start.json"])
                request_bytes = _bounded_bytes(files["request.bin"])
            except AppSoakError as exc:
                failures.append(exc.code)
                continue
            if (
                start.get("request_sha256") != hashlib.sha256(request_bytes).hexdigest()
                or start.get("request_bytes") != len(request_bytes)
                or type(start.get("method")) is not str
                or type(start.get("path")) is not str
                or ("sequence" in start and start.get("sequence") != sequence)
            ):
                failures.append("request_start_evidence_invalid")
            method = str(start.get("method") or "")
            target = str(start.get("path") or "")
            if directory_name == "admin":
                if method != "POST" or target not in {"/api/admin/users", "/api/admin/identities"}:
                    failures.append("admin_request_route_invalid")
            elif not app_route_allowed(method, target):
                failures.append("session_request_route_invalid")
            terminal_present = "terminal.json" in files
            error_present = "error.json" in files
            if terminal_present == error_present:
                failures.append("request_terminal_cardinality_invalid")
                continue
            record: dict[str, Any] = {
                "directory": directory_name,
                "sequence": sequence,
                "method": start.get("method"),
                "path": start.get("path"),
                "started_monotonic_ns": start.get("started_monotonic_ns"),
                "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                "request_bytes": request_bytes,
            }
            try:
                if terminal_present:
                    terminal = _read_json(files["terminal.json"])
                    response_bytes = _bounded_bytes(files.get("response.bin", Path("/missing")))
                    if (
                        terminal.get("response_sha256") != hashlib.sha256(response_bytes).hexdigest()
                        or terminal.get("response_bytes") != len(response_bytes)
                        or type(terminal.get("status_code")) is not int
                        or ("sequence" in terminal and terminal.get("sequence") != sequence)
                    ):
                        failures.append("request_terminal_evidence_invalid")
                    terminals += 1
                    record.update(
                        {
                            "outcome": "HTTP_OBSERVED",
                            "status_code": terminal.get("status_code"),
                            "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
                            "response_bytes": response_bytes,
                        }
                    )
                else:
                    error = _read_json(files["error.json"])
                    if error.get("outcome") != "INCOMPLETE_OR_ERROR" or (
                        "sequence" in error and error.get("sequence") != sequence
                    ):
                        failures.append("request_error_evidence_invalid")
                    if "response.bin" in files:
                        failures.append("request_error_has_uncommitted_response")
                    errors += 1
                    record.update({"outcome": "INCOMPLETE_OR_ERROR"})
            except AppSoakError as exc:
                failures.append(exc.code)
            attempts.append(record)
    return attempts, failures, (groups, terminals, errors)


def _operation_attempts(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[str] = []
    grouped: dict[int, dict[str, Path]] = {}
    for evidence_path in root.iterdir():
        match = _OPERATION_EVIDENCE.fullmatch(evidence_path.name)
        if match is None:
            if evidence_path.name.startswith("operation-"):
                failures.append("operation_evidence_filename_invalid")
            continue
        grouped.setdefault(int(match["sequence"]), {})[match["kind"]] = evidence_path
    if sorted(grouped) != list(range(1, len(grouped) + 1)):
        failures.append("operation_sequence_invalid")
    outcomes: list[dict[str, Any]] = []
    for _sequence, files in sorted(grouped.items()):
        try:
            if "not-run" in files:
                if set(files) != {"not-run"}:
                    failures.append("not_run_has_attempt_evidence")
                    continue
                outcome = _read_json(files["not-run"])
                if outcome.get("status") != "NOT_RUN":
                    failures.append("not_run_status_invalid")
            else:
                if "start" not in files or ("terminal" in files) == ("error" in files):
                    failures.append("operation_terminal_cardinality_invalid")
                    continue
                start = _read_json(files["start"])
                outcome = _read_json(files["terminal"] if "terminal" in files else files["error"])
                if start.get("name") != outcome.get("name") or start.get("principal") != outcome.get(
                    "principal"
                ):
                    failures.append("operation_identity_changed")
                expected_status = (
                    "PASS"
                    if "terminal" in files
                    else _failure_operation_status(str(outcome.get("reason_code") or "operation_exception"))
                )
                if outcome.get("status") != expected_status:
                    failures.append("operation_terminal_status_invalid")
            if outcome.get("status") not in OPERATION_STATUSES:
                failures.append("operation_status_invalid")
            outcomes.append(outcome)
        except AppSoakError as exc:
            failures.append(exc.code)
    names = [str(item.get("name") or "") for item in outcomes]
    if not all(names) or len(names) != len(set(names)):
        failures.append("operation_identity_or_duplicate_invalid")
    return outcomes, failures


def _json_response(attempt: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = attempt.get("response_bytes")
    if not isinstance(raw, bytes):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _json_request(attempt: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = attempt.get("request_bytes")
    if not isinstance(raw, bytes):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _resource_observations(
    root: Path,
    *,
    policy: SoakPolicy,
) -> tuple[list[ResourceSample], list[str]]:
    failures: list[str] = []
    paths = sorted(root.glob("resource-*.json"))
    if [path.name for path in paths] != [
        f"resource-{sequence:06d}.json" for sequence in range(1, len(paths) + 1)
    ]:
        failures.append("resource_sequence_invalid")
    samples: list[ResourceSample] = []
    for sequence, path in enumerate(paths, 1):
        try:
            value = _read_json(path)
            if (
                set(value)
                != {
                    "sequence",
                    "monotonic_ns",
                    "elapsed_ns",
                    "rss_bytes",
                    "fd_count",
                    "thread_count",
                    "database_bytes",
                    "wal_bytes",
                    "evidence_bytes",
                }
                or value.get("sequence") != sequence
            ):
                failures.append("resource_evidence_shape_invalid")
                continue
            sample = ResourceSample(**value)
            if any(type(item) is not int or item < 0 for item in value.values()):
                failures.append("resource_evidence_value_invalid")
                continue
            samples.append(sample)
            failures.extend(resource_failures(sample, policy.resources))
        except (AppSoakError, TypeError):
            failures.append("resource_evidence_invalid")
    if not samples:
        failures.append("resource_samples_missing")
    return samples, failures


def _duration_request_topology_valid(
    attempts: Sequence[Mapping[str, Any]],
    *,
    policy: SoakPolicy,
    expected: Mapping[str, Any],
    rounds: int,
) -> bool:
    owner_directory = f"session-{policy.owner.user_id}"
    foreign_directory = f"session-{expected['foreign_user_id']}"
    identity_counts = Counter(
        item.get("directory")
        for item in attempts
        if item.get("method") == "GET" and item.get("path") == "/api/me"
    )
    wanted_identity_counts = {
        owner_directory: 2 + rounds,
        **{
            f"session-{item.user_id}": 1 + rounds
            for item in policy.principals
            if item.user_id != policy.owner.user_id
        },
    }
    route_paths = Counter(
        (
            item.get("directory"),
            item.get("method"),
            urlsplit(str(item.get("path") or "")).path,
        )
        for item in attempts
    )
    if (
        dict(identity_counts) != wanted_identity_counts
        or route_paths[("admin", "POST", "/api/admin/users")] != 3
        or route_paths[("admin", "POST", "/api/admin/identities")] != 3
        or route_paths[(owner_directory, "POST", "/api/files")] != 1
        or route_paths[(owner_directory, "POST", "/api/chat")] != 1
        or route_paths[(owner_directory, "GET", "/api/knowledge/sources")] != 1
        or route_paths[(foreign_directory, "GET", "/api/knowledge/sources")] != 1
        or sum(
            count
            for (directory, method, path), count in route_paths.items()
            if directory == owner_directory and method == "GET" and path.startswith("/api/files/")
        )
        != 1
        or sum(
            count
            for (directory, method, path), count in route_paths.items()
            if directory == foreign_directory and method == "GET" and path.startswith("/api/files/")
        )
        != 1
        or len(attempts) != 17 + rounds * len(policy.principals)
        or any(item.get("outcome") != "HTTP_OBSERVED" for item in attempts)
    ):
        return False
    principals = {f"session-{item.user_id}": item for item in policy.principals}
    for attempt in attempts:
        directory = str(attempt.get("directory") or "")
        method = attempt.get("method")
        path = urlsplit(str(attempt.get("path") or "")).path
        expected_status = 200
        if directory == foreign_directory and method == "GET" and path.startswith("/api/files/"):
            expected_status = 404
        if attempt.get("status_code") != expected_status:
            return False
        if method == "GET" and path == "/api/me":
            principal = principals.get(directory)
            body = _json_response(attempt)
            actor = body.get("actor") if isinstance(body, Mapping) else None
            user = body.get("user") if isinstance(body, Mapping) else None
            if (
                principal is None
                or not isinstance(actor, Mapping)
                or not isinstance(user, Mapping)
                or actor.get("user_id") != principal.user_id
                or actor.get("preset_key") != principal.preset_key
                or actor.get("source") != "telegram-bridge"
                or user.get("id") != principal.user_id
                or user.get("preset_key") != principal.preset_key
                or user.get("status") != "active"
            ):
                return False
    return True


def _base_raw_observation_failures(
    attempts: Sequence[Mapping[str, Any]],
    *,
    policy: SoakPolicy,
    expected: Mapping[str, Any],
    source_effect: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    owner_directory = f"session-{policy.owner.user_id}"
    foreign_directory = f"session-{expected['foreign_user_id']}"

    def matching(directory: str, method: str, path: str) -> list[Mapping[str, Any]]:
        return [
            item
            for item in attempts
            if item.get("directory") == directory
            and item.get("method") == method
            and urlsplit(str(item.get("path") or "")).path == path
        ]

    upload = matching(owner_directory, "POST", "/api/files")
    upload_body = _json_response(upload[0]) if len(upload) == 1 else None
    raw_id = str(source_effect.get("raw_object_id") or "")
    if (
        not isinstance(upload_body, Mapping)
        or upload[0].get("status_code") != 200
        or upload_body.get("raw_object_id") != raw_id
    ):
        failures.append("upload_response_effect_mismatch")

    owner_queries = matching(owner_directory, "GET", "/api/knowledge/sources")
    owner_body = _json_response(owner_queries[0]) if len(owner_queries) == 1 else None
    owner_status = owner_queries[0].get("status_code") if len(owner_queries) == 1 else None
    owner_path = owner_queries[0].get("path") if len(owner_queries) == 1 else None
    items = owner_body.get("items") if isinstance(owner_body, Mapping) else None
    file_contract = expected["file"]
    query_target = "/api/knowledge/sources?q=" + quote(str(file_contract["canary"]), safe="") + "&limit=20"
    if (
        owner_status != 200
        or owner_path != query_target
        or not isinstance(items, list)
        or len(
            [
                item
                for item in items
                if isinstance(item, Mapping)
                and item.get("id") == raw_id
                and item.get("source_ref") == file_contract["source_ref"]
                and str(file_contract["canary"]) in str(item.get("excerpt") or "")
            ]
        )
        != 1
    ):
        failures.append("owner_source_raw_observation_invalid")

    foreign_queries = matching(foreign_directory, "GET", "/api/knowledge/sources")
    foreign_body = _json_response(foreign_queries[0]) if len(foreign_queries) == 1 else None
    try:
        if (
            not isinstance(foreign_body, Mapping)
            or foreign_queries[0].get("status_code") != 200
            or foreign_queries[0].get("path") != query_target
        ):
            raise AppSoakError("foreign_source_raw_observation_invalid")
        validate_foreign_source_response(
            foreign_body,
            canary=str(file_contract["canary"]),
            raw_id=raw_id,
            source_ref=str(file_contract["source_ref"]),
        )
    except AppSoakError:
        failures.append("foreign_source_raw_observation_invalid")

    owner_artifacts = matching(owner_directory, "GET", f"/api/files/{raw_id}")
    owner_bytes = owner_artifacts[0].get("response_bytes") if len(owner_artifacts) == 1 else None
    if (
        not isinstance(owner_bytes, bytes)
        or owner_artifacts[0].get("status_code") != 200
        or hashlib.sha256(owner_bytes).hexdigest() != file_contract["sha256"]
    ):
        failures.append("owner_artifact_raw_observation_invalid")

    foreign_artifacts = matching(foreign_directory, "GET", f"/api/files/{raw_id}")
    foreign_bytes = foreign_artifacts[0].get("response_bytes") if len(foreign_artifacts) == 1 else None
    if (
        not isinstance(foreign_bytes, bytes)
        or foreign_artifacts[0].get("status_code") != 404
        or str(file_contract["canary"]).encode("utf-8") in foreign_bytes
    ):
        failures.append("foreign_artifact_raw_observation_invalid")
    return failures


def independent_evidence_check(
    root: Path,
    *,
    policy: SoakPolicy,
    expected: Mapping[str, Any],
    provisional: Mapping[str, Any],
) -> dict[str, Any]:
    failures: list[str] = []
    try:
        stored_expected = _read_json(root / "expected.json")
        if stored_expected != expected:
            failures.append("expected_contract_changed")
    except AppSoakError as exc:
        failures.append(exc.code)
    try:
        config_identity = _read_json(root / "config-identity.json")
        if _digest(config_identity) != policy.config_sha256:
            failures.append("config_identity_changed")
    except AppSoakError as exc:
        failures.append(exc.code)

    try:
        attempts, request_failures, counts = _request_attempts(root, policy=policy)
        failures.extend(request_failures)
    except (AppSoakError, OSError) as exc:
        attempts, counts = [], (0, 0, 0)
        failures.append(exc.code if isinstance(exc, AppSoakError) else "request_evidence_read_failed")
    starts, terminals, errors = counts
    budget = provisional.get("request_accounting")
    if not isinstance(budget, Mapping):
        failures.append("request_accounting_missing")
    elif (
        starts != budget.get("started")
        or terminals != budget.get("completed")
        or errors != budget.get("errors")
        or budget.get("inflight") != 0
    ):
        failures.append("attempt_evidence_incomplete")
    samples, resource_evidence_failures = _resource_observations(root, policy=policy)
    failures.extend(resource_evidence_failures)
    if provisional.get("resource_samples") != len(samples):
        failures.append("resource_sample_accounting_invalid")

    try:
        raw_operations, operation_failures = _operation_attempts(root)
        failures.extend(operation_failures)
    except (AppSoakError, OSError) as exc:
        raw_operations = []
        failures.append(exc.code if isinstance(exc, AppSoakError) else "operation_evidence_read_failed")
    try:
        aggregate = _read_json(root / "operations.json")
        if aggregate.get("items") != raw_operations or aggregate.get("count") != len(raw_operations):
            failures.append("operation_aggregate_changed")
    except AppSoakError as exc:
        failures.append(exc.code)
    by_name = {str(item.get("name") or ""): item for item in raw_operations}
    if any(name not in by_name for name in REQUIRED_OPERATIONS):
        failures.append("required_operation_cardinality_invalid")
    allowed_operation_principals = {
        "admin-owner",
        "all",
        "product-reminder-worker",
        *(item.user_id for item in policy.principals),
        *(str(item.chat_id) for item in policy.principals),
    }
    if any(item.get("principal") not in allowed_operation_principals for item in raw_operations):
        failures.append("operation_principal_identity_changed")

    first_failure = next(
        (item for item in raw_operations if item.get("status") in {"FAIL", "BLOCKED"}),
        None,
    )
    claimed_codes = provisional.get("failure_codes")
    if first_failure is not None and (
        not isinstance(claimed_codes, list)
        or not claimed_codes
        or claimed_codes[0] != first_failure.get("reason_code")
    ):
        failures.append("first_failure_changed")

    reminder_operation = by_name.get("reminder_public_creation", {})
    reminder_status = reminder_operation.get("status")
    chat_attempts = [
        item
        for item in attempts
        if item.get("directory") == f"session-{policy.owner.user_id}"
        and item.get("method") == "POST"
        and item.get("path") == "/api/chat"
    ]
    try:
        reminder_effect = _read_json(root / "effect-reminder-creation.json")
    except AppSoakError as exc:
        reminder_effect = {}
        failures.append(exc.code)
    if len(chat_attempts) != 1:
        failures.append("reminder_request_cardinality_invalid")
        chat_response: Mapping[str, Any] = {}
    else:
        chat_request = _json_request(chat_attempts[0])
        if chat_request != {
            "message": expected["publication"]["request"],
            "enable_tools": True,
            "telegram_user": {
                "id": policy.owner.chat_id,
                "first_name": "AppSoak",
                "language_code": "ru",
            },
        }:
            failures.append("reminder_request_evidence_invalid")
        chat_response = _json_response(chat_attempts[0]) or {}
        if not chat_response:
            failures.append("reminder_response_evidence_invalid")
    tools_used = chat_response.get("tools_used")
    rows = reminder_effect.get("rows")
    if not isinstance(rows, list):
        rows = []
        failures.append("reminder_effect_evidence_invalid")
    derived_reminder_failure = _reminder_creation_failure(
        tools_used=tools_used,
        rows=rows,
        owner_user_id=policy.owner.user_id,
        reminder_name=str(expected["publication"]["canary"]),
        occurred_at=str(expected["publication"]["occurred_at"]),
    )
    if (
        reminder_effect.get("schema") != "friday.app-soak-reminder-creation-effect.v1"
        or reminder_effect.get("owner_user_id") != policy.owner.user_id
        or reminder_effect.get("reminder_name") != expected["publication"]["canary"]
        or reminder_effect.get("http_status") != 200
        or reminder_effect.get("tools_used") != _tools_used_reference(tools_used)
        or (
            len(chat_attempts) == 1
            and (
                chat_attempts[0].get("status_code") != 200
                or reminder_effect.get("response_sha256") != chat_attempts[0].get("response_sha256")
                or reminder_effect.get("response_ref")
                != (
                    f"{chat_attempts[0]['directory']}/request-{chat_attempts[0]['sequence']:06d}.response.bin"
                )
            )
        )
    ):
        failures.append("reminder_effect_identity_changed")

    functional_status = "PASS"
    if derived_reminder_failure:
        functional_status = "FAIL"
        if reminder_status != "FAIL" or reminder_operation.get("reason_code") != derived_reminder_failure:
            failures.append("reminder_failure_outcome_changed")
        details = (
            reminder_operation.get("details")
            if isinstance(reminder_operation.get("details"), Mapping)
            else {}
        )
        actual = details.get("actual") if isinstance(details.get("actual"), Mapping) else {}
        if (
            details.get("expected") != {"tools_used": ["remind"], "durable_rows": 1}
            or actual.get("tools_used") != _tools_used_reference(tools_used)
            or actual.get("durable_rows") != len(rows)
            or actual.get("effect_ref") != "effect-reminder-creation.json"
            or actual.get("response_ref") != reminder_effect.get("response_ref")
        ):
            failures.append("first_failure_references_invalid")
        if any(
            by_name.get(name, {}).get("status") != "NOT_RUN"
            or by_name.get(name, {}).get("depends_on") != "reminder_public_creation"
            or by_name.get(name, {}).get("dependency_reason_code") != derived_reminder_failure
            for name in REMINDER_DEPENDENTS
        ):
            failures.append("dependent_outcomes_invalid")
        if list(root.glob("destination-*.json")) or (root / "effect-publication-row.json").exists():
            failures.append("dependent_effect_present_after_failed_creation")
        if derived_reminder_failure == "reminder_creation_not_observed":
            coverage = provisional.get("duration_coverage")
            rounds = coverage.get("identity_get_rounds") if isinstance(coverage, Mapping) else None
            if (
                not isinstance(coverage, Mapping)
                or coverage.get("status") != "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL"
                or coverage.get("requests_per_round") != len(policy.principals)
                or coverage.get("reminder_branch") != "MISSING_AFTER_CREATION_FAIL"
                or coverage.get("full_planned_mixed_soak") is not False
                or type(rounds) is not int
                or rounds < 0
            ):
                failures.append("independent_duration_coverage_invalid")
            elif not _duration_request_topology_valid(
                attempts,
                policy=policy,
                expected=expected,
                rounds=rounds,
            ):
                failures.append("independent_duration_requests_invalid")
        else:
            coverage = provisional.get("duration_coverage")
            if (
                not isinstance(coverage, Mapping)
                or coverage.get("status") != "NOT_RUN_SAFETY_STOP"
                or coverage.get("identity_get_rounds") != 0
                or coverage.get("requests_per_round") != len(policy.principals)
                or coverage.get("reminder_branch") != "MISSING_AFTER_CREATION_FAIL"
                or coverage.get("full_planned_mixed_soak") is not False
                or not _duration_request_topology_valid(
                    attempts,
                    policy=policy,
                    expected=expected,
                    rounds=0,
                )
            ):
                failures.append("unsafe_failure_did_not_stop")
    elif reminder_status != "PASS":
        functional_status = "BLOCKED"
        failures.append("reminder_success_outcome_missing")
    else:
        if any(by_name.get(name, {}).get("status") != "PASS" for name in REMINDER_DEPENDENTS):
            functional_status = "FAIL"
        destinations = list(root.glob("destination-*.json"))
        if len(destinations) != 1:
            failures.append("destination_cardinality_invalid")
        else:
            try:
                destination = _read_json(destinations[0])
                publication = expected["publication"]
                if (
                    destination.get("chat_id") != publication["destination_chat_id"]
                    or destination.get("body") != publication["exact_body"]
                ):
                    failures.append("destination_readback_invalid")
            except AppSoakError as exc:
                failures.append(exc.code)
        try:
            publication_effect = _read_json(root / "effect-publication-row.json")
            if (
                publication_effect.get("user_id") != policy.owner.user_id
                or publication_effect.get("chat_id") != expected["publication"]["destination_chat_id"]
                or publication_effect.get("kind") != "reminder"
                or publication_effect.get("body") != expected["publication"]["exact_body"]
                or publication_effect.get("status") != "sent"
            ):
                failures.append("publication_effect_identity_changed")
        except AppSoakError as exc:
            failures.append(exc.code)

    raw_statuses = {str(item.get("status") or "") for item in raw_operations}
    if "BLOCKED" in raw_statuses:
        functional_status = "BLOCKED"
    elif "FAIL" in raw_statuses:
        functional_status = "FAIL"
    if functional_status != "PASS":
        if provisional.get("functional_status") == "PASS" or provisional.get("status") in {
            "APP_SOAK_PROFILE_OBSERVED",
            "SYNTHETIC_MECHANICS_OBSERVED",
        }:
            failures.append("functional_failure_promoted")
        elif provisional.get("functional_status") != functional_status:
            failures.append("functional_outcome_changed")

    try:
        source_effect = _read_json(root / "effect-source-row.json")
        if (
            source_effect.get("schema") != "friday.app-soak-source-effect.v1"
            or source_effect.get("user_id") != policy.owner.user_id
            or source_effect.get("source_ref") != expected["file"]["source_ref"]
            or source_effect.get("content_hash") != expected["file"]["sha256"]
            or not _RAW_ID.fullmatch(str(source_effect.get("raw_object_id") or ""))
        ):
            failures.append("source_effect_identity_changed")
        failures.extend(
            _base_raw_observation_failures(
                attempts,
                policy=policy,
                expected=expected,
                source_effect=source_effect,
            )
        )
        core = _read_json(root / "core-observation.json")
        source = core.get("source") if isinstance(core.get("source"), Mapping) else {}
        foreign = core.get("foreign") if isinstance(core.get("foreign"), Mapping) else {}
        artifact = core.get("artifact") if isinstance(core.get("artifact"), Mapping) else {}
        publication = core.get("publication") if isinstance(core.get("publication"), Mapping) else {}
        if (
            list(core.get("bindings") or []) != [item.user_id for item in policy.principals]
            or source.get("raw_object_id") != source_effect.get("raw_object_id")
            or source.get("owner_user_id") != policy.owner.user_id
            or source.get("source_ref") != source_effect.get("source_ref")
            or source.get("canary_observed") is not True
            or source.get("matching_sources") != 1
            or foreign.get("user_id") != expected["foreign_user_id"]
            or foreign.get("source_count") != 0
            or foreign.get("source_leak") is not False
            or foreign.get("artifact_status") != 404
            or foreign.get("artifact_leak") is not False
            or artifact.get("sha256") != expected["file"]["sha256"]
            or artifact.get("readback") is not True
        ):
            failures.append("core_aggregate_changed")
        if derived_reminder_failure:
            if (
                publication.get("creation_status") != reminder_status
                or publication.get("creation_failure_code") != derived_reminder_failure
                or publication.get("notification_id") is not None
                or publication.get("destination") is not None
                or publication.get("durable_rows") != len(rows)
                or publication.get("durable_status") != "NOT_RUN"
                or publication.get("replayed") is not None
                or publication.get("delivery_class") != "in_process_synthetic_destination"
            ):
                failures.append("core_publication_failure_changed")
        elif (
            publication.get("creation_status") != "PASS"
            or publication.get("durable_rows") != 1
            or publication.get("durable_status") != "sent"
            or publication.get("replayed") is not False
        ):
            failures.append("core_publication_success_changed")
    except AppSoakError as exc:
        failures.append(exc.code)

    if (
        provisional.get("candidate_sha256") != policy.candidate_sha256
        or provisional.get("suite_revision") != policy.suite_revision
        or provisional.get("config_sha256") != policy.config_sha256
    ):
        failures.append("run_identity_changed")
    try:
        if _tree_bytes(root) > policy.resources.max_evidence_bytes:
            failures.append("evidence_budget_exceeded")
    except AppSoakError as exc:
        failures.append(exc.code)
    failures = list(dict.fromkeys(failures))
    return {
        "status": "EVIDENCE_COMPLETE" if not failures else "EVIDENCE_INCOMPLETE",
        "failure_codes": failures,
        "observed_profile_status": functional_status,
        "starts": starts,
        "terminals": terminals,
        "errors": errors,
    }


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_POLICY_BYTES:
        raise AppSoakError("json_evidence_file_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise AppSoakError("json_evidence_invalid") from None
    if not isinstance(value, dict):
        raise AppSoakError("json_evidence_object_required")
    return value


def _principal_for_operation(name: str, policy: SoakPolicy) -> str:
    if name in {"reminder_due_execution", "reminder_due_replay"}:
        return "product-reminder-worker"
    if name == "observe_principals":
        return "all"
    return policy.owner.user_id


def _record_unreached_operations(
    operations: OperationRecorder,
    *,
    policy: SoakPolicy,
    cause_name: str,
    cause_code: str,
) -> None:
    observed = {str(item.get("name") or "") for item in operations.entries}
    for name in REQUIRED_OPERATIONS:
        if name not in observed:
            operations.not_run(
                name,
                _principal_for_operation(name, policy),
                depends_on=cause_name,
                reason_code=cause_code,
            )


def run_actual_app_soak(
    settings: Any,
    policy: SoakPolicy,
    evidence_dir: Path,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from friday.permissions import LEGACY_OWNER_USER_ID
    from friday.server import create_app

    config_identity = validate_settings(
        settings,
        policy,
        canonical_owner_id=LEGACY_OWNER_USER_ID,
    )
    local_day = datetime.now(ZoneInfo(str(settings.local_timezone or "UTC"))).date().isoformat()
    expected = build_expected(policy, local_day=local_day)
    bundle = PrivateBundle(evidence_dir, policy.resources.max_evidence_bytes)
    started = clock()
    started_ns = time.monotonic_ns()
    gate = BudgetGate(policy, started=started, clock=clock)
    operations = OperationRecorder(bundle, gate.check_deadline)
    samples: list[ResourceSample] = []
    core: dict[str, Any] = {}
    core_outcome = CoreOutcome(observation=core)
    additional_failures: list[str] = []
    sessions: dict[str, Any] = {}
    duration_rounds = 0
    duration_status = "NOT_RUN_BEFORE_CORE_OUTCOME"
    try:
        bundle.write_json("policy.json", policy.to_payload())
        bundle.write_json("config-identity.json", config_identity)
        bundle.write_json("expected.json", expected)
        AppSession = app_session_type()
        app = create_app(settings)
        with TestClient(app) as client:
            for principal in policy.principals:
                directory = bundle.root / f"session-{principal.user_id}"
                sessions[principal.user_id] = AppSession(
                    BudgetedClient(client, gate, principal.user_id),
                    bridge_secret=settings.telegram_bridge_secret,
                    chat_id=principal.chat_id,
                    evidence=directory,
                )
            admin = AdminObserver(
                BudgetedClient(client, gate, "admin-owner"),
                bundle.root / "admin",
            )
            _record_sample(
                samples,
                bundle,
                policy,
                started_ns=started_ns,
                database_path=Path(settings.database_path),
            )
            core_outcome = _run_core(
                app=app,
                settings=settings,
                policy=policy,
                expected=expected,
                sessions=sessions,
                admin=admin,
                operations=operations,
                bundle=bundle,
            )
            core = core_outcome.observation
            if core_outcome.first_failure_code and not core_outcome.continuation_allowed:
                duration_status = "NOT_RUN_SAFETY_STOP"
                if core_outcome.continuation_stop_code:
                    additional_failures.append(core_outcome.continuation_stop_code)
            else:
                duration_status = (
                    "INDEPENDENT_READ_ONLY_AFTER_FUNCTIONAL_FAIL"
                    if core_outcome.first_failure_code
                    else "FULL_MIXED_WORKLOAD"
                )
                next_tick = clock() + policy.cadence_sec
                end_at = started + policy.duration_sec
                while clock() < end_at:
                    gate.check_deadline()
                    wake_at = min(next_tick, end_at)
                    delay = max(0.0, wake_at - clock())
                    if delay:
                        before_wait = clock()
                        sleeper(delay)
                        after_wait = clock()
                        if after_wait <= before_wait:
                            raise AppSoakError("monotonic_clock_did_not_advance")
                        if after_wait < wake_at:
                            continue
                    if clock() >= end_at:
                        break
                    _ping_round(
                        sessions,
                        policy,
                        during=lambda: _record_sample(
                            samples,
                            bundle,
                            policy,
                            started_ns=started_ns,
                            database_path=Path(settings.database_path),
                        ),
                    )
                    duration_rounds += 1
                    next_tick = clock() + policy.cadence_sec
                _record_sample(
                    samples,
                    bundle,
                    policy,
                    started_ns=started_ns,
                    database_path=Path(settings.database_path),
                )
    except AppSoakError as exc:
        additional_failures.append(exc.code)
        duration_status = "STOPPED_BY_BOUNDARY_FAILURE"
        failed = next(
            (item for item in reversed(operations.entries) if item.get("status") in {"FAIL", "BLOCKED"}),
            None,
        )
        cause_name = str((failed or {}).get("name") or "app_soak_boundary")
        try:
            _record_unreached_operations(
                operations,
                policy=policy,
                cause_name=cause_name,
                cause_code=exc.code,
            )
        except BaseException:
            additional_failures.append("evidence_outcome_completion_failed")
    except Exception:
        additional_failures.append("unexpected_app_soak_exception")
        duration_status = "STOPPED_BY_UNKNOWN_FAILURE"
        try:
            _record_unreached_operations(
                operations,
                policy=policy,
                cause_name="app_soak_unknown",
                cause_code="unexpected_app_soak_exception",
            )
        except BaseException:
            additional_failures.append("evidence_outcome_completion_failed")
    finally:
        for session in sessions.values():
            try:
                session.close()
            except Exception:
                additional_failures.append("session_close_incomplete")

    elapsed = clock() - started
    budget = gate.snapshot()
    duration_coverage = {
        "status": duration_status,
        "elapsed_sec": round(elapsed, 6),
        "identity_get_rounds": duration_rounds,
        "requests_per_round": len(policy.principals),
        "reminder_branch": (
            "MISSING_AFTER_CREATION_FAIL"
            if core_outcome.first_failure_code
            else "EXECUTED"
            if core
            else "NOT_REACHED"
        ),
        "full_planned_mixed_soak": not core_outcome.first_failure_code and not additional_failures,
    }
    report = evaluate_profile(
        policy=policy,
        expected=expected,
        core=core,
        operations=operations.entries,
        samples=samples,
        budget=budget,
        elapsed_sec=elapsed,
        config_sha256=_digest(config_identity),
        evidence_mode="actual_app",
        duration_coverage=duration_coverage,
        additional_failure_codes=additional_failures,
    )
    if core:
        bundle.write_json("core-observation.json", core)
    bundle.write_json(
        "operations.json",
        {"items": operations.entries, "count": len(operations.entries)},
    )
    bundle.write_json("provisional.json", report)
    try:
        independent = independent_evidence_check(
            bundle.root,
            policy=policy,
            expected=expected,
            provisional=report,
        )
    except AppSoakError as exc:
        independent = {
            "status": "EVIDENCE_INCOMPLETE",
            "failure_codes": [exc.code],
            "observed_profile_status": "BLOCKED",
            "starts": 0,
            "terminals": 0,
            "errors": 0,
        }
    if independent["status"] != "EVIDENCE_COMPLETE":
        report = {
            **report,
            "status": "APP_SOAK_PROFILE_INCOMPLETE",
            "failure_codes": list(
                dict.fromkeys(
                    [
                        *report.get("failure_codes", []),
                        *independent["failure_codes"],
                    ]
                )
            ),
        }
    report["evidence_status"] = independent["status"]
    report["independent_evidence"] = independent
    bundle.write_json("report.json", report)
    bundle.close()
    return report


def load_policy(path: Path) -> SoakPolicy:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_POLICY_BYTES:
        raise AppSoakError("policy_file_invalid")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (OSError, ValueError, UnicodeError):
        raise AppSoakError("policy_json_invalid") from None
    return SoakPolicy.from_value(value)


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_write(path, _canonical(report))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finite persistent Friday application soak")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.evidence_dir.exists() or args.report.exists():
            raise AppSoakError("output_path_already_exists")
        from friday.config import load_settings

        settings = load_settings()
        # All policy and setting validation happens inside before create_app.
        report = run_actual_app_soak(settings, policy, args.evidence_dir)
        write_report(args.report, report)
    except AppSoakError as exc:
        report = {
            "schema": SCHEMA,
            "status": "NOT_RUN",
            "failure_codes": [exc.code],
            "go_emitted": False,
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 5
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "APP_SOAK_PROFILE_OBSERVED" else 4


if __name__ == "__main__":
    raise SystemExit(main())
