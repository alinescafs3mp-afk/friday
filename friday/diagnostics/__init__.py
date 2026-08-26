"""Actionable local diagnostics for operators and the Admin API."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import stat
import sys
import time
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from friday.config import FridaySettings, validate_settings
from friday.diagnostics.runtime_lease import (
    ProcessLease,
    RuntimeLeaseError,
    inspect_process_lease,
    process_owns_lease,
)
from friday.host_control.client import HostControlClient, HostControlClientError
from friday.host_control.contracts import PROTOCOL_VERSION, AdapterState
from friday.host_control.network_approval import NetworkApprovalSigner
from friday.host_control.policy import NetworkPolicy
from friday.memory import MemoryVaultDeletionHandle, VaultProjectionBoundaryError
from friday.telemetry import SystemTelemetry
from friday.telemetry.logging import redact_text
from friday_package_broker.approval import load_backend_approval_signing_key

if TYPE_CHECKING:
    from friday.storage import FridayStorage


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "readable": os.access(path, os.R_OK) if path.exists() else False,
        "writable": os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK),
    }


def _memory_vault_projection_status(settings: FridaySettings) -> dict[str, Any]:
    """Return body-free projection facts without opening a Markdown note.

    A disabled projector must not erase a pre-existing projection automatically:
    those files may be the operator's last intentional export.  Diagnostics only
    inventories regular artifacts (including crash-temporary files and unexpected
    users-level entries) in the code-owned layout and publishes only a note count
    plus presence/completeness; paths, filenames and file bodies never enter the result.
    """

    try:
        root_present, count, projection_artifact_present, scan_complete = MemoryVaultDeletionHandle(
            settings.memory_vault_dir
        ).inventory_projection()
    except VaultProjectionBoundaryError:
        # A refused no-follow boundary says nothing about lexical existence: the
        # root itself, an ancestor, or ``users`` may be a symlink.  Reporting
        # ``False`` would contradict the generic path inventory and falsely turn
        # "not safely inspectable" into "absent".
        root_present, count, projection_artifact_present, scan_complete = None, 0, False, False

    legacy_present: bool | None
    if settings.memory_vault_mode != "disabled":
        legacy_present = False
    elif projection_artifact_present:
        legacy_present = True
    elif scan_complete:
        legacy_present = False
    else:
        legacy_present = None
    return {
        "mode": settings.memory_vault_mode,
        "body_free_mode": settings.memory_vault_mode == "disabled",
        "body_projection_enabled": settings.memory_vault_mode == "full_owner",
        "projection_root_present": root_present,
        "projection_note_count": count,
        "legacy_projection_present": legacy_present,
        "scan_complete": scan_complete,
    }


def _model_status(path: Path) -> dict[str, Any]:
    status = _path_status(path)
    status.update({"usable_files_detected": 0, "placeholder_only": False})
    if not path.is_dir():
        return status
    ignored = {".gitkeep", ".keep", "README", "README.md", "PLACEHOLDER"}
    count = 0
    try:
        for _root, directories, files in os.walk(path):
            directories[:] = [item for item in directories if not item.startswith(".")]
            for filename in files:
                if filename in ignored or filename.startswith("."):
                    continue
                count += 1
                if count >= 1000:
                    break
            if count >= 1000:
                break
    except OSError as exc:
        status["scan_error"] = f"{type(exc).__name__}: {exc}"
    status["usable_files_detected"] = count
    status["placeholder_only"] = count == 0
    return status


_HOST_BUILD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,119}$")
_HOST_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_HOST_REPORTED_STATE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HOST_PROTOCOL = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}$")
_HOST_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HOST_ADAPTER_STATES = frozenset(item.value for item in AdapterState)
_EMPTY_HOST_SQLITE_COUNTS: dict[str, bool | int] = {
    "available": False,
    "running_jobs": 0,
    "unknown_jobs": 0,
    "pending_package_jobs": 0,
    "events": 0,
}


def _host_control_sqlite_counts(execute: Any) -> dict[str, bool | int]:
    """Return only physical lifecycle counts from a schema-43 connection."""

    try:
        tables = {
            str(row[0])
            for row in execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('host_action_jobs','host_action_events')"
            ).fetchall()
        }
        if tables != {"host_action_jobs", "host_action_events"}:
            return dict(_EMPTY_HOST_SQLITE_COUNTS)
        jobs = execute(
            """SELECT
                   COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END),0) AS running_jobs,
                   COALESCE(SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END),0) AS unknown_jobs,
                   COALESCE(SUM(CASE WHEN risk_class='package_mutation'
                                      AND status='awaiting_approval' THEN 1 ELSE 0 END),0)
                       AS pending_package_jobs
                 FROM host_action_jobs"""
        ).fetchone()
        events = execute("SELECT COUNT(*) AS count FROM host_action_events").fetchone()
        if jobs is None or events is None:
            return dict(_EMPTY_HOST_SQLITE_COUNTS)
        return {
            "available": True,
            "running_jobs": max(0, int(jobs["running_jobs"])),
            "unknown_jobs": max(0, int(jobs["unknown_jobs"])),
            "pending_package_jobs": max(0, int(jobs["pending_package_jobs"])),
            "events": max(0, int(events["count"])),
        }
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return dict(_EMPTY_HOST_SQLITE_COUNTS)


def _offline_host_control_sqlite_counts(path: Path) -> dict[str, bool | int]:
    if not path.is_file():
        return dict(_EMPTY_HOST_SQLITE_COUNTS)
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            return _host_control_sqlite_counts(conn.execute)
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return dict(_EMPTY_HOST_SQLITE_COUNTS)


def _host_control_flags(settings: FridaySettings) -> dict[str, bool | int]:
    return {
        "package_install": bool(settings.host_package_install_enabled),
        "desktop_control": bool(settings.host_desktop_control_enabled),
        "one_shot_exec": bool(settings.host_one_shot_exec_enabled),
        "public_network": bool(settings.host_public_network_enabled),
        "allowed_cidr_count": min(len(settings.host_allowed_cidrs), 32),
        "allowed_path_root_count": min(len(settings.host_allowed_path_roots), 8),
    }


def _bounded_host_agent_health(
    health: object,
    *,
    expected_agent_id: str,
    expected_network_policy_digest: str,
    expected_network_approval_public_key_digest: str | None,
) -> dict[str, Any] | None:
    if not isinstance(health, dict) or health.get("agent_id") != expected_agent_id:
        return None
    build_id = health.get("build_id")
    protocols = health.get("protocol_versions")
    inventory = health.get("inventory")
    running = health.get("running_job_count")
    desktop = health.get("desktop_capability")
    package_broker = health.get("package_broker")
    network_policy_digest = health.get("network_policy_digest")
    network_approval_public_key_digest = health.get("network_approval_public_key_digest")
    if (
        not isinstance(build_id, str)
        or _HOST_BUILD_ID.fullmatch(build_id) is None
        or not isinstance(protocols, list)
        or not 1 <= len(protocols) <= 8
        or any(not isinstance(item, str) or _HOST_PROTOCOL.fullmatch(item) is None for item in protocols)
        or PROTOCOL_VERSION not in protocols
        or not isinstance(inventory, list)
        or len(inventory) > 64
        or isinstance(running, bool)
        or not isinstance(running, int)
        or not 0 <= running <= 1_000_000
        or not isinstance(desktop, str)
        or _HOST_REPORTED_STATE.fullmatch(desktop) is None
        or not isinstance(package_broker, str)
        or package_broker not in {"configured", "unavailable"}
        or network_policy_digest != expected_network_policy_digest
        or (
            expected_network_approval_public_key_digest is not None
            and (
                not isinstance(network_approval_public_key_digest, str)
                or _HOST_DIGEST.fullmatch(network_approval_public_key_digest) is None
                or not hmac.compare_digest(
                    network_approval_public_key_digest,
                    expected_network_approval_public_key_digest,
                )
            )
        )
    ):
        return None

    adapter_states: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in inventory:
        if not isinstance(raw, dict):
            return None
        adapter_id = raw.get("adapter_id")
        state = raw.get("state")
        if (
            not isinstance(adapter_id, str)
            or _HOST_ADAPTER_ID.fullmatch(adapter_id) is None
            or adapter_id in seen
            or not isinstance(state, str)
            or state not in _HOST_ADAPTER_STATES
        ):
            return None
        seen.add(adapter_id)
        adapter_states.append({"adapter_id": adapter_id, "state": state})
    adapter_states.sort(key=lambda item: item["adapter_id"])
    bounded: dict[str, Any] = {
        "agent": {
            "reachable": True,
            "authenticated": True,
            "identity": expected_agent_id,
            "build_id": build_id,
            "protocol_versions": protocols,
        },
        "adapter_states": adapter_states,
        "desktop": desktop,
        "package_broker": package_broker,
        "network_policy_match": True,
        "running_job_count": running,
    }
    if expected_network_approval_public_key_digest is not None:
        bounded["network_approval_public_key_match"] = True
    return bounded


def _host_control_status(
    settings: FridaySettings,
    sqlite_counts: dict[str, bool | int],
) -> dict[str, Any]:
    enabled = bool(settings.host_control_enabled)
    status: dict[str, Any] = {
        "enabled": enabled,
        "flags": _host_control_flags(settings),
        "state": "disabled" if not enabled else "unavailable",
        "healthy": not enabled,
        "agent": {"reachable": False, "authenticated": False},
        "adapter_states": [],
        "desktop": "not_reported",
        "package_broker": "not_reported",
        "network_approval_public_key_match": None,
        "running_job_count": None,
        "sqlite": dict(sqlite_counts),
    }
    if not enabled:
        return status
    try:
        client = HostControlClient(
            settings.host_agent_socket,
            key_file=settings.host_agent_key_file,
            agent_id=settings.host_agent_id,
            timeout_sec=1.0,
        )
        health = client.handshake_sync(timeout_sec=1.0)
    except HostControlClientError as exc:
        code = str(exc.code or "host_agent_unavailable")
        status["error_code"] = code if _HOST_REPORTED_STATE.fullmatch(code) else "host_agent_unavailable"
        return status
    except Exception:  # noqa: BLE001 - optional diagnostics must remain fail-soft
        status["error_code"] = "host_agent_probe_failed"
        return status

    expected_policy_digest = NetworkPolicy(
        connected_cidrs=(),
        allowed_cidrs=tuple(settings.host_allowed_cidrs),
        allow_public=bool(settings.host_public_network_enabled),
    ).digest
    expected_approval_key_digest: str | None = None
    if settings.host_public_network_enabled:
        try:
            expected_approval_key_digest = NetworkApprovalSigner(
                load_backend_approval_signing_key(settings.host_approval_signing_key_file)
            ).public_key_digest
        except Exception:  # noqa: BLE001 - diagnostics must fail closed without leaking key errors
            status.update(
                {
                    "state": "approval_key_unavailable",
                    "agent": {"reachable": True, "authenticated": True},
                    "error_code": "network_approval_signer_unavailable",
                }
            )
            return status
    bounded = _bounded_host_agent_health(
        health,
        expected_agent_id=settings.host_agent_id,
        expected_network_policy_digest=expected_policy_digest,
        expected_network_approval_public_key_digest=expected_approval_key_digest,
    )
    if bounded is None:
        status.update(
            {
                "state": "invalid_report",
                "agent": {"reachable": True, "authenticated": True},
                "error_code": "host_agent_report_invalid",
            }
        )
        return status
    status.update(bounded)
    healthy = bool(sqlite_counts.get("available"))
    if settings.host_package_install_enabled and bounded["package_broker"] != "configured":
        healthy = False
    if settings.host_desktop_control_enabled and bounded["desktop"] == "no_graphical_session":
        healthy = False
    status["healthy"] = healthy
    status["state"] = "ready" if healthy else "degraded"
    return status


def _add_host_control_actions(add_action: Any, status: dict[str, Any]) -> None:
    counts_value = status.get("sqlite")
    counts: dict[str, Any] = counts_value if isinstance(counts_value, dict) else {}
    unknown_jobs = int(counts.get("unknown_jobs") or 0)
    if unknown_jobs:
        add_action(
            "host_control_unknown_jobs",
            "warning",
            "Есть host-задачи с неизвестным исходом",
            f"Неизвестный исход сохранён для {unknown_jobs} задач; сверяйте их состояние до повтора.",
            "jericho status",
        )
    if not status.get("enabled"):
        return
    state = str(status.get("state") or "unavailable")
    if state == "unavailable":
        add_action(
            "host_control_agent_unavailable",
            "error",
            "Host-agent недоступен",
            "Host Control включён, но аутентифицированный ответ user-service не получен. "
            "Проверьте состояние friday-host-agent и его журнал.",
            "systemctl --user status friday-host-agent.service",
        )
        return
    if state == "invalid_report":
        add_action(
            "host_control_agent_report_invalid",
            "error",
            "Host-agent вернул неприемлемый health-отчёт",
            "Ответ аутентифицирован, но его метаданные не прошли закрытый диагностический контракт.",
            "systemctl --user status friday-host-agent.service",
        )
        return
    if state == "approval_key_unavailable":
        add_action(
            "host_network_approval_signer_unavailable",
            "error",
            "Ключ подтверждения public-network недоступен",
            "Public-network включён, но backend не смог загрузить свой Ed25519 signer; "
            "сетевые возможности не подтверждены.",
            "jericho doctor",
        )
        return
    if not counts.get("available"):
        add_action(
            "host_control_store_unavailable",
            "error",
            "Durable Host Control state недоступен",
            "Schema-backed счётчики задач не прочитаны; не повторяйте неизвестные операции вслепую.",
            "jericho doctor",
        )
    flags_value = status.get("flags")
    flags: dict[str, Any] = flags_value if isinstance(flags_value, dict) else {}
    if flags.get("package_install") and status.get("package_broker") != "configured":
        add_action(
            "host_package_broker_unavailable",
            "error",
            "Package broker недоступен",
            "Установка пакетов включена, но host-agent не подтвердил подключение root broker.",
            "systemctl status friday-package-broker.socket friday-package-broker.service",
        )
    if flags.get("desktop_control") and status.get("desktop") == "no_graphical_session":
        add_action(
            "host_desktop_session_unavailable",
            "warning",
            "Графическая сессия для Host Control недоступна",
            "Desktop control включён, но user-service не видит активную графическую сессию.",
            "systemctl --user status friday-host-agent.service",
        )


def _port_reachable(url: str, timeout: float = 1.0) -> dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return {"reachable": False, "error": "invalid URL"}
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"reachable": True, "host": host, "port": port}
    except OSError as exc:
        return {"reachable": False, "host": host, "port": port, "error": str(exc)}


def _llm_endpoint_status(
    base_url: str, model: str, *, api_key: str = "", timeout: float = 2.0
) -> dict[str, Any]:
    """Beyond a bare TCP connect: query ``{base_url}/models`` and confirm the
    configured model is actually served. A wrong ``FRIDAY_LLM_MODEL`` is the most
    common local-LLM footgun and a socket probe reports it as 'reachable'."""
    status: dict[str, Any] = {
        **_port_reachable(base_url, timeout=1.0),
        "model_expected": model,
        "model_served": None,
        "served_models": [],
    }
    if not status.get("reachable"):
        return status
    try:
        headers = {"Accept": "application/json"}
        if api_key:
            # An authenticated endpoint (e.g. vLLM --api-key) 401s /models otherwise.
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - configured local URL
            payload = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 - any failure means we cannot confirm the model
        status["models_error"] = f"{type(exc).__name__}: {exc}"
        return status
    data = payload.get("data") if isinstance(payload, dict) else None
    served = (
        [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
        if isinstance(data, list)
        else []
    )
    status["served_models"] = served[:20]
    status["model_served"] = (model in served) if served else None
    return status


def served_model_name(base_url: str, *, api_key: str = "", timeout: float = 3.0) -> str:
    """Как НА САМОМ ДЕЛЕ называется модель, которую обслуживает эндпойнт.

    Нужно там, где имя показывают человеку. В настройках владельца `llm_model`
    равно «dispatcher» — это псевдоним маршрутизатора, и на вопрос «какая ты
    модель?» он не отвечает ничего. Настоящее имя лежит в поле `root`: путь к
    весам вида «/models/qwen3.6-35b-a3b-uncensored-nvfp4».

    Пустая строка значит «не узнали», и это законный исход: показывать нечего —
    скажем то, что есть в настройках. Выдумывать нельзя ровно потому, что этот
    вызов и появился из-за выдумки.
    """
    try:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - configured local URL
            payload = json.loads(response.read())
    except Exception:  # noqa: BLE001 — незнание имени не должно ронять вызов
        return ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return ""
    first = data[0] if isinstance(data[0], dict) else {}
    root = str(first.get("root") or "").strip()
    # `root` — путь к весам; человеку нужно имя, а не каталог.
    return root.rsplit("/", 1)[-1] if root else str(first.get("id") or "").strip()


def _llm_generates(base_url: str, model: str, *, api_key: str = "", timeout: float = 25.0) -> dict[str, Any]:
    """Отвечает ли модель НА САМОМ ДЕЛЕ — не «открыт ли порт» и не «есть ли в списке».

    Найдено на живом отказе 2026-08-03, и это единственная проверка, которая его
    поймала бы. Сервер модели принимал соединения и отдавал `/models` за 0.019 с —
    то есть и `_port_reachable`, и `_llm_endpoint_status` считали его здоровым, —
    а генерация висела и обрывалась пустым ответом. Так продолжалось двадцать
    минут; живой человек за это время получил восемь испорченных ответов подряд, и
    никто об этом не узнал, потому что сторож смотрел не туда.

    Проба намеренно копеечная: один токен, температура ноль. Дороже неё стоит
    молчание — за него платит человек по ту сторону чата.

    Потолок ожидания больше, чем у соседних проверок (25 с против 2 с): здоровый,
    но занятый сервер отвечает не мгновенно, и объявлять его мёртвым за две
    секунды значило бы будить владельца по каждому всплеску нагрузки. Зато
    ЗАВИСШАЯ генерация не отвечает вовсе, и её ловит любой конечный потолок.
    """
    status: dict[str, Any] = {"generates": None, "seconds": None}
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ок"}],
            "max_tokens": 1,
            "temperature": 0,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=payload, headers=headers
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - configured local URL
            response.read()
    except Exception as exc:  # noqa: BLE001 — любой отказ означает «не отвечает»
        status["generates"] = False
        status["seconds"] = round(time.monotonic() - started, 2)
        status["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return status
    status["generates"] = True
    status["seconds"] = round(time.monotonic() - started, 2)
    return status


def _database_status(path: Path) -> dict[str, Any]:
    """Inspect an initialized SQLite database without migrating or modifying it."""
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "schema_version": None,
            "ok": True,
            "state": "not_initialized",
        }

    database: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "database_size_bytes": path.stat().st_size,
        "schema_version": None,
    }
    try:
        # ``mode=ro`` is important here: status/doctor must never create a
        # database file or run migrations merely by inspecting an installation.
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            table_names = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "schema_meta" in table_names:
                marker = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                if marker is not None:
                    try:
                        database["schema_version"] = int(marker[0])
                    except (TypeError, ValueError):
                        database["schema_version_error"] = "invalid schema_version marker"
            else:
                database["schema_version_error"] = "schema_meta table is missing"

            database["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            database["foreign_key_violations"] = [
                dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            ]
            counts: dict[str, int] = {}
            for table in (
                "users",
                "raw_objects",
                "knowledge_objects",
                "inbox",
                "entities",
                "relations",
                "messages",
            ):
                if table in table_names:
                    # ``table`` is selected from the fixed diagnostic allowlist above.
                    row = conn.execute(
                        f'SELECT COUNT(*) AS count FROM "{table}"'  # nosec B608
                    ).fetchone()
                    counts[table] = int(row["count"] if row else 0)
            database["counts"] = counts
            if "outbound_notifications" in table_names:
                pending_row = conn.execute(
                    "SELECT COUNT(*) AS count, MIN(created_at) AS oldest "
                    "FROM outbound_notifications WHERE status='pending'"
                ).fetchone()
                database["outbound_pending"] = int(pending_row["count"] if pending_row else 0)
                # Возраст важнее размера: очередь наполняет backend, а разгребает
                # МОСТ. Мёртвый мост backend видел (count рос) и молчал — застрявшее
                # уведомление неотличимо от только что положенного без отметки времени.
                oldest_raw = pending_row["oldest"] if pending_row else None
                if oldest_raw:
                    with suppress(ValueError, TypeError):
                        oldest_at = datetime.fromisoformat(str(oldest_raw))
                        if oldest_at.tzinfo is None:
                            oldest_at = oldest_at.replace(tzinfo=UTC)
                        age_minutes = (datetime.now(UTC) - oldest_at).total_seconds() / 60
                        database["outbound_oldest_minutes"] = round(age_minutes, 1)
            if "inbox" in table_names:
                # A pending item is not knowledge yet — it cannot be found by search. So a
                # backlog is not untidiness, it is material the owner imported and can no
                # longer reach. Age matters more than size: thousands of items minutes
                # after an import is expected, the same thousands a month later means the
                # review never happened.
                backlog_row = conn.execute(
                    "SELECT COUNT(*) AS count, MIN(created_at) AS oldest FROM inbox WHERE status='pending'"
                ).fetchone()
                database["inbox_pending"] = int(backlog_row["count"] if backlog_row else 0)
                database["inbox_oldest_pending_at"] = (
                    str(backlog_row["oldest"]) if backlog_row and backlog_row["oldest"] else None
                )
            database["ok"] = (
                database["integrity_check"] == "ok"
                and not database["foreign_key_violations"]
                and database["schema_version"] is not None
            )
            database["state"] = "ready" if database["ok"] else "needs_attention"
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        database.update({"ok": False, "state": "unreadable", "error": f"{type(exc).__name__}: {exc}"})
    return database


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mirror_status(settings: FridaySettings, storage: FridayStorage | None) -> dict[str, Any]:
    """Decode the mirror worker's last report, and say whether it is still current.

    Written every run to ``workers:last_backup_mirror`` and, until now, read by
    nothing at all.
    """
    if settings.backup_mirror_dir is None:
        return {"enabled": False}
    if storage is None:
        return {"enabled": True, "state": "unknown"}
    raw = storage.kv_get("workers:last_backup_mirror")
    if not raw:
        return {"enabled": True, "state": "never_ran", "mirror_dir": str(settings.backup_mirror_dir)}
    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        return {"enabled": True, "state": "invalid", "mirror_dir": str(settings.backup_mirror_dir)}
    if not isinstance(report, dict):
        return {"enabled": True, "state": "invalid", "mirror_dir": str(settings.backup_mirror_dir)}
    report.setdefault("mirror_dir", str(settings.backup_mirror_dir))
    # Stale = the local side moved on and the offsite side did not follow. The
    # comparison is against the newest local manifest, because that is the thing
    # the mirror is supposed to have picked up.
    reported_at = str(report.get("reported_at") or "")
    if reported_at:
        newest_local = 0.0
        with suppress(OSError):
            newest_local = max(
                (path.stat().st_mtime for path in settings.backups_dir.glob("*.manifest.json")),
                default=0.0,
            )
        with suppress(ValueError):
            stamp = datetime.fromisoformat(reported_at)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            report["stale"] = bool(newest_local and newest_local > stamp.timestamp() + 60)
    return report


def _latest_backup_status(backups_dir: Path) -> dict[str, Any]:
    """Return the newest safe backup pair and verify its manifest contract.

    This is intentionally read-only and independent from an open storage
    object, which makes CLI ``status`` and ``doctor`` useful after a restart.
    """
    status: dict[str, Any] = {
        "path": str(backups_dir),
        "available": False,
        "verified": False,
        "latest": None,
    }
    if not backups_dir.is_dir():
        status["state"] = "directory_missing"
        return status

    root = backups_dir.resolve()
    manifests = sorted(
        (path for path in backups_dir.glob("*.manifest.json") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not manifests:
        status["state"] = "no_backups"
        return status

    errors: list[str] = []
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            database_name = str(manifest.get("database") or "")
            if Path(database_name).name != database_name or not database_name.endswith(".sqlite3"):
                raise ValueError("unsafe database filename")
            database_candidate = backups_dir / database_name
            if database_candidate.is_symlink():
                raise ValueError("backup symlinks are not allowed")
            database_path = database_candidate.resolve()
            if database_path.parent != root or not database_path.is_file():
                raise FileNotFoundError("database file is missing")

            expected_size = manifest.get("size_bytes")
            if expected_size is None or isinstance(expected_size, bool):
                raise ValueError("size_bytes is missing or invalid")
            try:
                expected_size_value = int(str(expected_size))
            except ValueError as exc:
                raise ValueError("size_bytes is invalid") from exc
            if expected_size_value != database_path.stat().st_size:
                raise ValueError("size_bytes does not match")
            expected_digest = str(manifest.get("sha256") or "")
            if len(expected_digest) != 64 or _sha256_file(database_path) != expected_digest:
                raise ValueError("sha256 does not match")

            database = _database_status(database_path)
            if not database.get("ok"):
                raise ValueError("backup database integrity check failed")
            manifest_schema = manifest.get("schema_version")
            if manifest_schema is None or isinstance(manifest_schema, bool):
                raise ValueError("schema_version is missing or invalid")
            try:
                manifest_schema_value = int(str(manifest_schema))
            except ValueError as exc:
                raise ValueError("schema_version is invalid") from exc
            if manifest_schema_value != database.get("schema_version"):
                raise ValueError("schema_version does not match")

            created_at = str(manifest.get("created_at") or "")
            age_seconds: float | None = None
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_seconds = max(0.0, (datetime.now(UTC) - created.astimezone(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
            status.update(
                {
                    "available": True,
                    "verified": True,
                    "state": "verified",
                    "latest": {
                        "database": database_path.name,
                        "manifest": manifest_path.name,
                        "created_at": created_at,
                        "age_seconds": age_seconds,
                        "size_bytes": database_path.stat().st_size,
                        "schema_version": database.get("schema_version"),
                        "label": str(manifest.get("label") or ""),
                    },
                }
            )
            return status
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{manifest_path.name}: {type(exc).__name__}: {exc}")

    status.update(
        {
            "available": True,
            "state": "invalid",
            "errors": errors[:5],
        }
    )
    return status


def _decode_worker_states(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC)
    degraded: list[str] = []
    stale: list[str] = []
    for row in rows:
        key = str(row.get("key") or "")
        name = key.removeprefix("workers:health:")
        if not name:
            continue
        try:
            value = json.loads(str(row.get("value") or "{}"))
        except json.JSONDecodeError:
            value = {"status": "invalid", "error_type": "InvalidWorkerState"}
        if not isinstance(value, dict):
            value = {"status": "invalid", "error_type": "InvalidWorkerState"}
        task = dict(value)
        invalid_fields: list[str] = []
        try:
            failures = max(0, int(task.get("consecutive_failures") or 0))
        except (TypeError, ValueError, OverflowError):
            failures = 0
            invalid_fields.append("consecutive_failures")
        status = str(task.get("status") or "unknown")
        # "skipped" is published by the orphan-thread guard in the worker supervisor
        # and was missing here, so the record decoded as `invalid` with
        # `state_errors: ["status"]` and the worker was reported degraded. The guard
        # working correctly looked like corrupt state — and a real corrupt record
        # became indistinguishable from a healthy skip.
        if status not in {"scheduled", "running", "ok", "error", "timeout", "skipped", "unknown"}:
            invalid_fields.append("status")
        try:
            interval_value = float(task.get("interval_sec") or 1.0)
            if not math.isfinite(interval_value) or interval_value <= 0:
                raise ValueError
            interval = max(1.0, interval_value)
        except (TypeError, ValueError, OverflowError):
            interval = 1.0
            invalid_fields.append("interval_sec")
        if invalid_fields:
            status = "invalid"
            task["status"] = status
            task["state_errors"] = sorted(set(invalid_fields))
        if failures >= 3 or status == "invalid":
            degraded.append(name)
        last_finished = str(task.get("last_finished_at") or task.get("last_started_at") or "")
        if last_finished and status not in {"scheduled", "running"}:
            try:
                parsed = datetime.fromisoformat(last_finished)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                if now - parsed > timedelta(seconds=max(900.0, interval * 2.5)):
                    stale.append(name)
            except ValueError:
                stale.append(name)
        elif not last_finished and status == "scheduled":
            # Задача, НИ РАЗУ не отработавшая, была освобождена от проверки по
            # построению: у неё нет `last_finished`, и она сидит в «scheduled». То есть
            # чем дольше она мертва, тем надёжнее выглядит здоровой. На живой установке
            # так и вышло: `chronicle` и `reflection` не дали ни одной записи за всё
            # время жизни системы, а диагностика показывала «healthy».
            #
            # Судим по обещанному сроку: он в состоянии есть всегда. Просрочка больше
            # чем на два с половиной интервала — то же правило, что и для отработавших.
            promised = str(task.get("next_run_at") or "")
            try:
                due = datetime.fromisoformat(promised) if promised else None
            except ValueError:
                due = None
            if due is not None:
                if due.tzinfo is None:
                    due = due.replace(tzinfo=UTC)
                if now - due > timedelta(seconds=max(900.0, interval * 2.5)):
                    stale.append(name)
        tasks[name] = task
    return {
        "state": "ready" if tasks else "no_history",
        "healthy": not degraded and not stale,
        "task_count": len(tasks),
        "degraded_tasks": sorted(degraded),
        "stale_tasks": sorted(stale),
        "tasks": tasks,
    }


def _worker_status(
    settings: FridaySettings,
    storage: FridayStorage | None,
) -> dict[str, Any]:
    if not settings.workers_enabled:
        return {"state": "disabled", "healthy": True, "task_count": 0, "tasks": {}}
    if storage is not None:
        return _decode_worker_states(storage.kv_list_prefix("workers:health:"))
    if not settings.database_path.is_file():
        return {"state": "not_initialized", "healthy": True, "task_count": 0, "tasks": {}}
    try:
        uri = f"{settings.database_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_kv'"
            ).fetchone()
            if table is None:
                return {"state": "unavailable", "healthy": True, "task_count": 0, "tasks": {}}
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT key, value FROM runtime_kv WHERE key LIKE 'workers:health:%' ORDER BY key"
                ).fetchall()
            ]
        finally:
            conn.close()
        return _decode_worker_states(rows)
    except (OSError, sqlite3.Error) as exc:
        return {
            "state": "unreadable",
            "healthy": False,
            "task_count": 0,
            "tasks": {},
            "error": f"{type(exc).__name__}: {exc}",
        }


def _configured_worker_status(
    settings: FridaySettings,
    status: dict[str, Any],
) -> dict[str, Any]:
    """Do not diagnose a retired plaintext projector as a live stalled task."""

    if settings.memory_vault_mode != "disabled":
        return status
    tasks_value = status.get("tasks")
    if not isinstance(tasks_value, dict) or "memory_vault_sync" not in tasks_value:
        return status
    projected = dict(status)
    tasks = dict(tasks_value)
    tasks.pop("memory_vault_sync", None)
    projected["tasks"] = tasks
    projected["task_count"] = len(tasks)
    for key in ("degraded_tasks", "stale_tasks"):
        values = projected.get(key)
        if isinstance(values, list):
            projected[key] = sorted(str(value) for value in values if value != "memory_vault_sync")
    if projected.get("state") in {"ready", "no_history"}:
        projected["state"] = "ready" if tasks else "no_history"
        projected["healthy"] = not projected.get("degraded_tasks") and not projected.get("stale_tasks")
    return projected


def _bridge_queue_status(path: Path) -> dict[str, Any]:
    """Read-only view of the Telegram bridge's durable queue — pending and
    dead-lettered update counts — so lost/rejected messages are observable
    without hand-reading SQLite. Never creates or migrates the file."""
    if not path.is_file():
        return {"state": "absent", "pending": 0, "dead_letter": 0, "healthy": True}
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            tables = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "updates" not in tables:
                return {"state": "empty", "pending": 0, "dead_letter": 0, "healthy": True}
            counts = {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM updates GROUP BY status"
                ).fetchall()
            }
            recent = conn.execute(
                "SELECT last_error FROM updates WHERE status='dead_letter' AND last_error!='' "
                "ORDER BY failed_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "state": "unreadable",
            "pending": 0,
            "dead_letter": 0,
            "healthy": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    dead_letter = int(counts.get("dead_letter", 0))
    return {
        "state": "present",
        "pending": int(counts.get("pending", 0)),
        "dead_letter": dead_letter,
        "healthy": dead_letter == 0,
        # Redacted a second time on the way out. The bridge already cleans what it
        # writes, but this row can predate that fix or come from a queue file some
        # other build produced, and diagnostics is exactly where a leaked
        # credential gets copied into a bug report.
        "last_dead_letter_error": (redact_text(str(recent["last_error"]))[:200] if recent else ""),
    }


def _bridge_queue_status_without_live_open(path: Path) -> dict[str, Any]:
    """Inspect the bridge queue only when no bridge process owns its WAL.

    Read-only SQLite connections still map ``-shm``.  On this installation a
    second process touching a live WAL has twice coincided with ``SIGBUS`` in the
    owning process, so diagnostics must not create that second mapping merely to
    obtain counters.  The running bridge remains authoritative; stopped queues
    keep the existing offline inspection path.
    """

    lease_path = path.with_name(f"{path.name}.lock")
    lease = inspect_process_lease(
        lease_path,
        protocol="friday.telegram-bridge.v1",
    )
    if lease.get("active") is True or lease.get("state") == "active_hint":
        return {
            "state": "active_uninspected",
            "pending": None,
            "dead_letter": None,
            "healthy": bool(lease.get("healthy", True)),
        }
    if path.is_symlink():
        return {
            "state": "unsafe_symlink_uninspected",
            "pending": None,
            "dead_letter": None,
            "healthy": False,
        }
    if not path.is_file():
        # No SQLite open follows this observation, so a bridge creating the
        # queue immediately afterwards is harmless and needs no lease handoff.
        return {"state": "absent", "pending": 0, "dead_letter": 0, "healthy": True}

    # Inspection and SQLite open cannot be two independent operations.  A
    # bridge may acquire its lease in between them and turn this apparently
    # offline read into a second mapping of its live ``-shm`` file.  Claiming
    # the same lease closes that window: either this process owns the whole
    # read, or a starting/running bridge wins and the queue remains untouched.
    boundary = ProcessLease(lease_path, protocol="friday.telegram-bridge.v1")
    try:
        boundary.acquire()
    except (OSError, RuntimeLeaseError):
        contender = inspect_process_lease(
            lease_path,
            protocol="friday.telegram-bridge.v1",
        )
        return {
            "state": "active_uninspected",
            "pending": None,
            "dead_letter": None,
            "healthy": bool(contender.get("healthy", False)),
        }
    try:
        return _bridge_queue_status(path)
    finally:
        boundary.release()


_DOCUMENT_CONTOUR_OBSERVER_SCHEMA = "friday.document-contour-observer-snapshot.v1"
_DOCUMENT_CONTOUR_GUARDED_QUEUE_SCHEMA = "friday.document-contour-guarded-bridge-queue.v1"


def _private_directory_identity(status: os.stat_result) -> tuple[int, ...]:
    mode = stat.S_IMODE(status.st_mode)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid() or mode & 0o077:
        raise RuntimeError("observer queue directory is not private")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        mode,
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _private_regular_identity(status: os.stat_result) -> tuple[int, ...]:
    mode = stat.S_IMODE(status.st_mode)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or mode & 0o077
    ):
        raise RuntimeError("observer queue file is not private")
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        mode,
        int(status.st_nlink),
        int(status.st_size),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


class _PinnedBridgeQueue:
    """Descriptor-bound stopped queue plus identities needed for revalidation."""

    def __init__(self, path: Path) -> None:
        if not sys.platform.startswith("linux") or not path.is_absolute():
            raise RuntimeError("descriptor-bound observer queue is unsupported")
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self.path = path
        self.parent_descriptor = os.open(path.parent, directory_flags)
        self.file_descriptor = -1
        try:
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            self.file_descriptor = os.open(path.name, file_flags, dir_fd=self.parent_descriptor)
            self.file_identity = _private_regular_identity(os.fstat(self.file_descriptor))
            lexical_file = _private_regular_identity(
                os.stat(path.name, dir_fd=self.parent_descriptor, follow_symlinks=False)
            )
            if lexical_file != self.file_identity:
                raise RuntimeError("observer queue identity changed during open")
            self._require_no_sidecars()
            # Capture directory timestamps only after the lease file and both
            # descriptors exist.  Rename/create/delete (including an ABA
            # restore) changes this identity and therefore closes the result.
            self.parent_identity = _private_directory_identity(os.fstat(self.parent_descriptor))
            lexical_parent = _private_directory_identity(os.stat(path.parent, follow_symlinks=False))
            if lexical_parent != self.parent_identity:
                raise RuntimeError("observer queue directory identity changed")
        except BaseException:
            self.close()
            raise

    def _require_no_sidecars(self) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(
                    f"{self.path.name}{suffix}",
                    dir_fd=self.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            raise RuntimeError("observer queue is not a checkpointed stopped database")

    def revalidate(self) -> None:
        if self.file_descriptor < 0 or self.parent_descriptor < 0:
            raise RuntimeError("observer queue descriptors are closed")
        if _private_directory_identity(os.fstat(self.parent_descriptor)) != self.parent_identity:
            raise RuntimeError("observer queue directory changed")
        if (
            _private_directory_identity(os.stat(self.path.parent, follow_symlinks=False))
            != self.parent_identity
        ):
            raise RuntimeError("observer queue directory entry changed")
        if _private_regular_identity(os.fstat(self.file_descriptor)) != self.file_identity:
            raise RuntimeError("observer queue file changed")
        if (
            _private_regular_identity(
                os.stat(
                    self.path.name,
                    dir_fd=self.parent_descriptor,
                    follow_symlinks=False,
                )
            )
            != self.file_identity
        ):
            raise RuntimeError("observer queue entry changed")
        self._require_no_sidecars()

    def close(self) -> None:
        file_descriptor = getattr(self, "file_descriptor", -1)
        parent_descriptor = getattr(self, "parent_descriptor", -1)
        self.file_descriptor = -1
        self.parent_descriptor = -1
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _bridge_queue_counts_only(queue: _PinnedBridgeQueue) -> dict[str, Any]:
    """Read status aggregates from the exact descriptor-bound stopped queue."""

    queue.revalidate()
    descriptor_path = f"/proc/self/fd/{queue.file_descriptor}"
    if _private_regular_identity(os.stat(descriptor_path)) != queue.file_identity:
        raise RuntimeError("observer descriptor projection changed")
    uri = f"file:{descriptor_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "updates" not in tables:
            raise RuntimeError("observer queue schema is missing")
        counts = {
            str(row["status"]): int(row["n"])
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM updates GROUP BY status").fetchall()
        }
    finally:
        conn.close()
    if not set(counts) <= {"pending", "dead_letter"}:
        raise RuntimeError("observer queue contains an unknown state")
    queue.revalidate()
    return {
        "state": "present",
        "pending": int(counts.get("pending", 0)),
        "dead_letter": int(counts.get("dead_letter", 0)),
    }


def _held_lease_file_identity(boundary: ProcessLease, path: Path) -> tuple[int, ...]:
    lexical = _private_regular_identity(os.stat(path, follow_symlinks=False))
    if boundary.held_file_identity != lexical[:2]:
        raise RuntimeError("observer bridge lease file identity changed")
    return lexical


def _require_lease_file_identity(path: Path, expected: tuple[int, ...]) -> None:
    if _private_regular_identity(os.stat(path, follow_symlinks=False)) != expected:
        raise RuntimeError("observer bridge lease file changed")


def _physical_outbound_pending(storage: FridayStorage) -> int:
    row = storage.execute(
        "SELECT COUNT(*) AS count FROM outbound_notifications WHERE status='pending'"
    ).fetchone()
    return int(row["count"] if row else 0)


def collect_document_contour_guarded_bridge_queue_snapshot(
    settings: FridaySettings,
    boundary: ProcessLease,
) -> dict[str, Any]:
    """Read content-free queue aggregates while the caller retains the bridge lease.

    The live operator uses this between the two document-contour workers.  It
    deliberately does not acquire or release the supplied boundary: successful
    return means the caller still owns the exact process-private guard, so the
    stopped queue cannot change through the supported bridge lifecycle before
    the observer response is published.
    """

    queue_path = settings.state_dir / "telegram-inbox.sqlite3"
    bridge_lease_path = queue_path.with_name(f"{queue_path.name}.lock")
    protocol = "friday.telegram-bridge.v1"
    if (
        os.path.normcase(os.path.abspath(boundary.path))
        != os.path.normcase(os.path.abspath(bridge_lease_path))
        or boundary.protocol != protocol
    ):
        raise RuntimeError("observer bridge guard identity is invalid")
    if not boundary.acquired or not process_owns_lease(bridge_lease_path, protocol=protocol):
        raise RuntimeError("observer bridge guard is not held by this process")

    lease_identity = _held_lease_file_identity(boundary, bridge_lease_path)
    pinned: _PinnedBridgeQueue | None = None
    try:
        pinned = _PinnedBridgeQueue(queue_path)
        queue = _bridge_queue_counts_only(pinned)
        pinned.revalidate()
        _require_lease_file_identity(bridge_lease_path, lease_identity)
    finally:
        if pinned is not None:
            pinned.close()

    if not boundary.acquired or not process_owns_lease(bridge_lease_path, protocol=protocol):
        raise RuntimeError("observer bridge guard ownership changed")
    _require_lease_file_identity(bridge_lease_path, lease_identity)
    return {
        "schema": _DOCUMENT_CONTOUR_GUARDED_QUEUE_SCHEMA,
        "bridge_guard_held": True,
        "bridge_queue_state": str(queue["state"]),
        "inbound_pending": queue["pending"],
        "dead_letter": queue["dead_letter"],
    }


def collect_document_contour_observer_snapshot(
    settings: FridaySettings,
    storage: FridayStorage,
) -> dict[str, Any]:
    """Build the owner-only, content-free inter-run observer projection.

    Unlike the public diagnostics count, ``physical_outbound_pending`` includes
    quarantined/private pending rows.  That stronger physical invariant is
    required only for a stopped-bridge release barrier and must not change the
    privacy semantics of the ordinary delegated diagnostics surface.
    """

    backend_lease_path = settings.state_dir / "backend.lock"
    if not process_owns_lease(backend_lease_path, protocol="friday.backend.v1"):
        raise RuntimeError("observer snapshot requires the live backend lease")

    physical_outbound_pending = _physical_outbound_pending(storage)

    queue_path = settings.state_dir / "telegram-inbox.sqlite3"
    bridge_lease_path = queue_path.with_name(f"{queue_path.name}.lock")
    boundary = ProcessLease(bridge_lease_path, protocol="friday.telegram-bridge.v1")
    try:
        boundary.acquire()
    except (OSError, RuntimeLeaseError):
        contender = inspect_process_lease(
            bridge_lease_path,
            protocol="friday.telegram-bridge.v1",
        )
        active = contender.get("active") is True or contender.get("state") == "active_hint"
        state = (
            "active_uninspected"
            if active and contender.get("protocol_matches") is not False
            else "lease_unavailable"
        )
        if _physical_outbound_pending(storage) != physical_outbound_pending:
            raise RuntimeError("physical outbound queue changed during observer snapshot") from None
        return {
            "schema": _DOCUMENT_CONTOUR_OBSERVER_SCHEMA,
            "backend_pid": os.getpid(),
            "backend_lease_owned": True,
            "physical_outbound_pending": physical_outbound_pending,
            "bridge_queue_state": state,
            "bridge_lease_acquired_for_snapshot": False,
            "bridge_lease_released": False,
            "inbound_pending": None,
            "dead_letter": None,
        }

    pinned: _PinnedBridgeQueue | None = None
    released = False
    try:
        lease_identity = _held_lease_file_identity(boundary, bridge_lease_path)
        pinned = _PinnedBridgeQueue(queue_path)
        queue = _bridge_queue_counts_only(pinned)
        pinned.revalidate()
        _require_lease_file_identity(bridge_lease_path, lease_identity)
        # Keep both queue descriptors live across release, then prove neither
        # their entries nor the advisory file changed before publishing true.
        boundary.release()
        released = True
        pinned.revalidate()
        _require_lease_file_identity(bridge_lease_path, lease_identity)
    finally:
        if pinned is not None:
            pinned.close()
        if not released and boundary.acquired:
            boundary.release()
    if _physical_outbound_pending(storage) != physical_outbound_pending:
        raise RuntimeError("physical outbound queue changed during observer snapshot")
    return {
        "schema": _DOCUMENT_CONTOUR_OBSERVER_SCHEMA,
        "backend_pid": os.getpid(),
        "backend_lease_owned": True,
        "physical_outbound_pending": physical_outbound_pending,
        "bridge_queue_state": str(queue["state"]),
        "bridge_lease_acquired_for_snapshot": True,
        "bridge_lease_released": True,
        "inbound_pending": queue["pending"],
        "dead_letter": queue["dead_letter"],
    }


# Bound the auth-failure scan: only a threshold comparison is needed, and a
# request flood keeps appending auth.failed rows even while rate-limited, so an
# unbounded COUNT could scan a huge trailing window on every diagnostics call.
_AUTH_FAILURE_SCAN_CAP = 1000


def _count_auth_failures(db_path: Path, storage: FridayStorage | None, since: str, cap: int) -> int:
    """Count ``auth.failed`` audit entries at/after an ISO timestamp, scan capped at ``cap``.

    Uses the live storage when available, else a read-only connection so
    ``jericho status`` (which has no open storage) still sees the count.
    """
    if storage is not None:
        try:
            return int(storage.count_recent_audit("auth.failed", since, limit=cap))
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return 0
    if not db_path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(
                "SELECT COUNT(*) FROM "
                "(SELECT 1 FROM audit_log WHERE action='auth.failed' AND created_at>=? LIMIT ?)",
                (since, cap),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return 0


def _auth_failure_status(
    db_path: Path, storage: FridayStorage | None, *, threshold: int, window_hours: int = 24
) -> dict[str, Any]:
    """Recent auth-failure count vs the alert threshold (threshold 0 = disabled).

    A 24h window (wider than the hourly sentinel tick and quiet hours) keeps a
    sustained or overnight burst from being aliased away between polls.
    """
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    cap = max(threshold, _AUTH_FAILURE_SCAN_CAP)
    count = _count_auth_failures(db_path, storage, since, cap)
    return {
        "window_hours": window_hours,
        "recent_failures": count,
        "threshold": threshold,
        "capped": count >= cap,
    }


# A backlog only becomes a problem once it has been ignored: right after `jericho
# import` a large pending queue is exactly what should have happened. These two
# thresholds together say "material has been waiting long enough that the review is
# not going to happen on its own", which is the only version of this worth a push
# notification.
INBOX_BACKLOG_MIN_ITEMS = 25
INBOX_BACKLOG_MIN_AGE_DAYS = 14


def _add_inbox_backlog_action(add_action: Any, database: dict[str, Any]) -> None:
    """Warn when reviewable material has been waiting long enough to be forgotten."""
    pending = int(database.get("inbox_pending") or 0)
    oldest_raw = database.get("inbox_oldest_pending_at")
    if pending < INBOX_BACKLOG_MIN_ITEMS or not oldest_raw:
        return
    try:
        oldest = datetime.fromisoformat(str(oldest_raw))
    except ValueError:
        return
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - oldest).days
    if age_days < INBOX_BACKLOG_MIN_AGE_DAYS:
        return
    add_action(
        "inbox_backlog",
        "warning",
        "Inbox не разобран",
        f"{pending} материалов ждут проверки, самый старый — {age_days} дн. "
        "Пока они не подтверждены, они не ищутся и фактически недоступны.",
        "jericho status",
    )


def _add_outbound_stall_action(add_action: Any, database: dict[str, Any]) -> None:
    """Возраст важнее размера: очередь наполняет backend, а разгребает МОСТ.

    Мёртвый мост backend видел (count рос) и молчал — застрявшее уведомление
    неотличимо от только что положенного, пока не смотреть на отметку времени.
    """
    age_minutes = float(database.get("outbound_oldest_minutes") or 0.0)
    if age_minutes <= 30:
        return
    add_action(
        "outbound_queue_stalled",
        "warning",
        "Мост не забирает уведомления",
        f"Старейшее уведомление ждёт {age_minutes:.0f} мин при опросе раз в 15 с — "
        "мост Telegram, вероятно, не работает. Напоминания и тревоги не доходят; "
        "проверьте процесс telegram-bridge.",
    )


def _add_versions_growth_action(add_action: Any, database: dict[str, Any]) -> None:
    """Версии хранят полный content в каждом снапшоте, и чистки нет нигде.

    Массовое ре-обогащение добавляет копию корпуса в базу навсегда; растут и
    сама база, и каждый из хранимых суточных бэкапов. Порог намеренно грубый:
    в три раза больше снапшотов, чем объектов, или больше четверти гигабайта —
    это уже не история правок, а второй корпус.
    """
    rows = int(database.get("versions_rows") or 0)
    size = int(database.get("versions_bytes") or 0)
    objects = int((database.get("counts") or {}).get("knowledge_objects") or 0)
    if rows <= max(1000, objects * 3) and size <= 256 * 1024 * 1024:
        return
    add_action(
        "versions_table_growing",
        "warning",
        "История версий разрослась",
        f"Снапшотов {rows} ({size / 1_048_576:.0f} МБ) на {objects} объектов; каждый несёт "
        "полный текст, чистки нет. База и все хранимые бэкапы растут кратно быстрее самого "
        "знания. Лечение — ретеншн (N последних полных версий, старые сжимать), в очереди работ.",
    )


def _add_secret_hygiene_actions(add_action: Any, settings: FridaySettings) -> None:
    """Report this instance's own credentials found outside the files meant to hold them.

    Not a heuristic: the comparison is against the exact values this process was started
    with, so a hit means that file contains this bot token, not something shaped like
    one. Paths are reported; values never are.
    """
    from friday.config import local_env_file_path
    from friday.secret_hygiene import (
        MAX_FILE_BYTES,
        MAX_FILES,
        MAX_SCAN_BYTES,
        MAX_WALK_ENTRIES,
        scan,
    )

    # The env file is protected by PATH now, not by name: skipping every file called
    # `.env` anywhere in the tree hid copies of live credentials in other projects,
    # and passing the real one here is also what gets its permissions checked — that
    # check ran over `protected` only, which used to be empty by default.
    protected = [path for path in (settings.backup_encryption_key_file, local_env_file_path()) if path]
    roots = [settings.home, Path.home()]
    main_database = settings.database_path
    bridge_queue = settings.state_dir / "telegram-inbox.sqlite3"
    excluded: list[Path] = []
    for live_sqlite in (main_database, bridge_queue):
        excluded.extend(
            [
                live_sqlite,
                live_sqlite.with_name(f"{live_sqlite.name}-wal"),
                live_sqlite.with_name(f"{live_sqlite.name}-shm"),
                live_sqlite.with_name(f"{live_sqlite.name}-journal"),
                live_sqlite.with_name(f"{live_sqlite.name}.lock"),
            ]
        )
    excluded.append(settings.state_dir / "backend.lock")
    try:
        # The scanner deliberately walks the whole owner home.  These exact
        # runtime paths (and hardlinks to their current inodes) are a stronger
        # exclusion than ``protected``: even a raw ``open(2)`` of a live SQLite,
        # WAL or SHM from an external doctor process violates the single-owner
        # boundary that prevents SIGBUS.
        report = scan(roots, protected=protected, excluded=excluded)
    except Exception:  # a hygiene check must never be why diagnostics fail
        # Do not include the exception: a filesystem/backend error can carry a
        # credential-bearing filename.  Silence would falsely look like complete,
        # clean coverage.
        add_action(
            "secret_scan_unavailable",
            "warning",
            "Проверка секретов не завершилась",
            "Filesystem scan завершился внутренней ошибкой; отсутствие находок не значит их отсутствие.",
        )
        return

    for path, mode in report.loose_permissions:
        add_action(
            "secret_file_permissions",
            "warning",
            "Файл с секретами доступен другим пользователям",
            f"{path} имеет права {mode:o}. Ожидается 600.",
            f"chmod 600 {path}",
        )
    for exposure in report.exposures:
        add_action(
            "secret_exposed_in_file",
            "error",
            "Секрет Friday лежит в постороннем файле",
            f"{exposure.path} содержит значение {exposure.secret_name}"
            + (" и доступен на чтение другим пользователям" if exposure.world_readable else "")
            + ". Удалите файл и перевыпустите этот секрет.",
        )
    # `clean` only speaks for complete coverage.  Large files are now streamed, but
    # file/byte bounds and unreadable paths remain intentionally fail-honest.
    if not report.complete:
        parts = []
        if report.stopped_early:
            parts.append(f"остановлено на пределе в {MAX_FILES} файлов")
        if report.discovery_limit_exhausted:
            parts.append(f"остановлено на пределе обхода в {MAX_WALK_ENTRIES} записей")
        if report.files_not_fully_scanned:
            parts.append(f"{report.files_not_fully_scanned} файл(ов) проверены не полностью")
        elif report.oversized_skipped:
            # Backward-compatible reports from a mixed-version backend may only
            # carry the legacy counter.
            parts.append(f"{report.oversized_skipped} файл(ов) крупнее {MAX_FILE_BYTES // (1 << 20)} МиБ")
        if report.byte_budget_exhausted:
            parts.append(f"исчерпан предел чтения {MAX_SCAN_BYTES // (1 << 20)} МиБ")
        if report.unreadable_skipped:
            parts.append(f"{report.unreadable_skipped} файл(ов) недоступны для чтения")
        if report.traversal_errors:
            parts.append(f"{report.traversal_errors} каталог(ов) не удалось обойти")
        add_action(
            "secret_scan_incomplete",
            "warning",
            "Проверка секретов охватила не всё",
            "Пропущено: " + ", ".join(parts) + ". Отсутствие находок для них не значит их отсутствие.",
        )


_LIVE_DIAGNOSTICS_MAX_BYTES = 4 * 1_048_576


class _NoLiveDiagnosticsRedirects(urllib.request.HTTPRedirectHandler):
    """Keep the bearer token on the one host-local request it was built for."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _diagnostic_state(ok: bool, actions: list[dict[str, Any]]) -> str:
    severities = {str(item.get("severity")) for item in actions}
    if not ok or "error" in severities:
        return "degraded"
    if "setup" in severities:
        return "setup_required"
    if "warning" in severities:
        return "attention"
    return "ready"


def _append_secret_hygiene_report(result: dict[str, Any], settings: FridaySettings) -> None:
    """Merge the filesystem-only hygiene scan into an in-process API report."""

    current = result.get("actions")
    if not isinstance(current, list) or any(not isinstance(item, dict) for item in current):
        current = []
        result["actions"] = current
    additions: list[dict[str, Any]] = []

    def add_action(
        code: str,
        severity: str,
        title: str,
        detail: str,
        command: str | None = None,
    ) -> None:
        action: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
        }
        if command:
            action["command"] = command
        additions.append(action)

    _add_secret_hygiene_actions(add_action, settings)
    existing = {str(item.get("code")) for item in current}
    current.extend(item for item in additions if str(item.get("code")) not in existing)
    result["state"] = _diagnostic_state(bool(result.get("ok")), current)


def _local_live_diagnostics_host(configured_host: str) -> str | None:
    """Return a numeric host only when the OS confirms it belongs to this host.

    The request carries the owner bearer token.  A configured hostname must not
    get a chance to resolve off-host (or be rebound between validation and
    connection), so the only accepted name is the fixed localhost alias.  A
    concrete non-loopback bind is accepted only when a no-packet ``bind(2)``
    probe proves that the address is assigned locally.
    """

    host = str(configured_host or "").strip()
    if host in {"", "0.0.0.0", "localhost"}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_multicast:
        return None
    if address.is_unspecified:
        return "::1" if address.version == 6 else "127.0.0.1"
    if address.is_loopback:
        return str(address)

    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        if address.version == 6:
            probe.bind((str(address), 0, 0, 0))
        else:
            probe.bind((str(address), 0))
    except OSError:
        return None
    finally:
        probe.close()
    return str(address)


def _live_backend_diagnostics_url(
    settings: FridaySettings,
    *,
    check_llm_port: bool,
) -> str | None:
    host = _local_live_diagnostics_host(settings.api_host)
    if host is None:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = "https" if settings.api_tls_enabled else "http"
    flag = "true" if check_llm_port else "false"
    return f"{scheme}://{host}:{settings.api_port}/api/admin/diagnostics?check_llm={flag}"


def _fetch_live_backend_diagnostics(
    settings: FridaySettings,
    backend_lease: dict[str, Any],
    *,
    check_llm_port: bool,
) -> dict[str, Any] | None:
    """Ask the process that already owns SQLite; never echo response failures."""

    try:
        url = _live_backend_diagnostics_url(settings, check_llm_port=check_llm_port)
        if url is None:
            return None
        headers = {"Accept": "application/json"}
        if settings.api_token:
            headers["Authorization"] = f"Bearer {settings.api_token}"
        request = urllib.request.Request(  # noqa: S310 - proven host-local destination
            url,
            headers=headers,
            method="GET",
        )
        handlers: list[Any] = [
            urllib.request.ProxyHandler({}),
            _NoLiveDiagnosticsRedirects(),
        ]
        if url.startswith("https://"):
            # The diagnostics request carries the owner bearer token, so a
            # self-signed backend must be authenticated explicitly rather than
            # retried insecurely.  Keep hostname verification on and add only
            # the public CA/certificate, never the server key.
            context = ssl.create_default_context()
            ca_file = settings.backend_ca_file or settings.ssl_certfile
            if ca_file:
                context.load_verify_locations(cafile=ca_file)
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(request, timeout=15.0) as response:  # noqa: S310 - proven host-local API
            if int(getattr(response, "status", 0) or 0) != 200:
                return None
            payload = response.read(_LIVE_DIAGNOSTICS_MAX_BYTES + 1)
        if len(payload) > _LIVE_DIAGNOSTICS_MAX_BYTES:
            return None
        result = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001 - every transport/parse failure is fail-closed
        return None
    if not isinstance(result, dict):
        return None
    if not {
        "ok",
        "state",
        "database",
        "workers",
        "backend_lease",
        "bridge_queue",
        "actions",
    }.issubset(result):
        return None
    reported_lease = result.get("backend_lease")
    if not isinstance(reported_lease, dict):
        return None
    expected_pid = backend_lease.get("pid")
    if (
        not isinstance(expected_pid, int)
        or isinstance(expected_pid, bool)
        or reported_lease.get("pid") != expected_pid
        or not (reported_lease.get("active") is True or reported_lease.get("state") == "active_hint")
    ):
        return None
    current_lease = inspect_process_lease(
        settings.state_dir / "backend.lock",
        protocol="friday.backend.v1",
    )
    if current_lease.get("pid") != expected_pid or not (
        current_lease.get("active") is True or current_lease.get("state") == "active_hint"
    ):
        return None
    return result


def _active_backend_diagnostics_unavailable(
    settings: FridaySettings,
    backend_lease: dict[str, Any],
    *,
    check_secrets: bool,
) -> dict[str, Any]:
    """Fail closed without mapping the live main database in this process."""

    configuration = validate_settings(settings, production=not settings.is_loopback_bind)
    memory_vault = _memory_vault_projection_status(settings)
    actions: list[dict[str, Any]] = []

    def add_action(
        code: str,
        severity: str,
        title: str,
        detail: str,
        command: str | None = None,
    ) -> None:
        action: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
        }
        if command:
            action["command"] = command
        actions.append(action)

    host_control = _host_control_status(settings, dict(_EMPTY_HOST_SQLITE_COUNTS))
    _add_host_control_actions(add_action, host_control)

    for issue in configuration:
        warning = issue.startswith("warning:")
        add_action(
            "configuration_warning" if warning else "configuration_error",
            "warning" if warning else "error",
            "Проверьте конфигурацию",
            issue.removeprefix("warning:").strip(),
            "jericho doctor",
        )
    if memory_vault["legacy_projection_present"] is True:
        add_action(
            "legacy_memory_vault_projection",
            "warning",
            "Отключённая Markdown-проекция всё ещё существует",
            f"Найдено полнотекстовых заметок проекции: "
            f"{memory_vault['projection_note_count']}. "
            "Friday не читает и не удаляет их автоматически; сначала проверьте "
            "назначение и резервную копию, затем выберите отдельный план remediation.",
        )
    elif memory_vault["legacy_projection_present"] is None:
        add_action(
            "legacy_memory_vault_projection_uninspected",
            "warning",
            "Отключённую Markdown-проекцию не удалось полностью проверить",
            "Friday не читал и не изменял файлы проекции; проверьте каталог локально.",
            "jericho doctor",
        )
    add_action(
        "active_backend_diagnostics_unavailable",
        "error",
        "Живой backend не отдал диагностику",
        "Process lease активен, но локальный диагностический API не ответил. "
        "Основная SQLite намеренно не открывалась вторым процессом; проверьте event loop "
        "и журнал backend.",
    )
    if check_secrets:
        _add_secret_hygiene_actions(add_action, settings)
    model = _model_status(settings.model_dir)
    secondary_configured = settings.secondary_llm_configured
    secondary_state = (
        "disabled"
        if not settings.secondary_llm_enabled or settings.secondary_llm_mode == "disabled"
        else "not_observed"
        if secondary_configured
        else "misconfigured"
    )
    result: dict[str, Any] = {
        "ok": False,
        "configuration_issues": configuration,
        "paths": {
            "home": _path_status(settings.home),
            "state": _path_status(settings.state_dir),
            "files": _path_status(settings.files_dir),
            "vault": _path_status(settings.memory_vault_dir),
            "backups": _path_status(settings.backups_dir),
            "exports": _path_status(settings.exports_dir),
            "model": model,
        },
        "database": {
            "path": str(settings.database_path),
            "exists": settings.database_path.is_file(),
            "schema_version": None,
            "ok": False,
            "state": "active_backend_uninspected",
        },
        "backups": _latest_backup_status(settings.backups_dir),
        "files_backup": {},
        "workers": {
            "state": "active_backend_uninspected",
            "healthy": False,
            "task_count": 0,
            "tasks": {},
        },
        "backend_lease": backend_lease,
        "bridge_queue": _bridge_queue_status_without_live_open(settings.state_dir / "telegram-inbox.sqlite3"),
        "auth_failures": {"state": "active_backend_uninspected"},
        "embeddings_index": {"available": False},
        "memory_vault": memory_vault,
        "host_control": host_control,
        "secondary": {
            "schema": "friday.optional-secondary-health.v1",
            "role": "optional_advisory",
            "enabled": bool(settings.secondary_llm_enabled),
            "configured": secondary_configured,
            "mode": settings.secondary_llm_mode,
            "state": secondary_state,
            "available": False,
        },
        "runtime": SystemTelemetry(settings.home).snapshot(),
        "features": {
            "llm_enabled": settings.llm_enabled,
            "embeddings_enabled": settings.embeddings_enabled,
            "workers_enabled": settings.workers_enabled,
            "code_execution_enabled": settings.code_execution_enabled,
            "web_private_networks_allowed": settings.web_allow_private_networks,
        },
        "actions": actions,
    }
    result["state"] = _diagnostic_state(False, actions)
    return result


def _live_backend_report(
    settings: FridaySettings,
    backend_lease: dict[str, Any],
    *,
    check_llm_port: bool,
    check_secrets: bool,
) -> dict[str, Any]:
    pid = backend_lease.get("pid")
    active = backend_lease.get("active") is True or backend_lease.get("state") == "active_hint"
    if not active or not isinstance(pid, int) or isinstance(pid, bool):
        return _active_backend_diagnostics_unavailable(
            settings,
            backend_lease,
            check_secrets=check_secrets,
        )
    live = _fetch_live_backend_diagnostics(
        settings,
        backend_lease,
        check_llm_port=check_llm_port,
    )
    if live is None:
        return _active_backend_diagnostics_unavailable(
            settings,
            backend_lease,
            check_secrets=check_secrets,
        )
    if check_secrets:
        _append_secret_hygiene_report(live, settings)
    return live


def collect_diagnostics(
    settings: FridaySettings,
    storage: FridayStorage | None = None,
    *,
    check_llm_port: bool = False,
    check_secrets: bool = False,
) -> dict[str, Any]:
    """Collect safe diagnostics without exposing secrets or document contents."""
    lease_path = settings.state_dir / "backend.lock"
    backend_lease = inspect_process_lease(
        lease_path,
        protocol="friday.backend.v1",
    )
    backend_active = backend_lease.get("active") is True or backend_lease.get("state") == "active_hint"
    this_process_owns_backend = process_owns_lease(
        lease_path,
        protocol="friday.backend.v1",
    )
    if storage is not None and this_process_owns_backend:
        return _collect_diagnostics_under_boundary(
            settings,
            storage,
            backend_lease,
            check_llm_port=check_llm_port,
            check_secrets=check_secrets,
        )
    if backend_active:
        return _live_backend_report(
            settings,
            backend_lease,
            check_llm_port=check_llm_port,
            check_secrets=check_secrets,
        )

    if storage is not None:
        # A storage object is not proof that this is the backend process.  Tests,
        # maintenance commands and third-party callers can construct one too.
        # Admit it only while this process owns the same boundary a backend
        # would need; a concurrently starting backend wins or loses atomically.
        storage_boundary = ProcessLease(lease_path, protocol="friday.backend.v1")
        try:
            storage_boundary.acquire()
        except Exception:  # noqa: BLE001 - never use storage after losing the lease race
            contender = inspect_process_lease(
                lease_path,
                protocol="friday.backend.v1",
            )
            return _live_backend_report(
                settings,
                contender,
                check_llm_port=check_llm_port,
                check_secrets=check_secrets,
            )
        try:
            return _collect_diagnostics_under_boundary(
                settings,
                storage,
                backend_lease,
                check_llm_port=check_llm_port,
                check_secrets=check_secrets,
            )
        finally:
            storage_boundary.release()

    # A negative inspection is only a point-in-time observation.  Hold the
    # backend's exact lease while all four main-database snapshots are taken,
    # otherwise the backend can start after inspection and before any one of
    # these read-only connections maps its live WAL.  Release before backup
    # verification, filesystem scans and model probes so diagnostics cannot
    # delay normal startup beyond the bounded SQLite snapshot itself.
    boundary = ProcessLease(lease_path, protocol="friday.backend.v1")
    try:
        boundary.acquire()
    except Exception:  # noqa: BLE001 - losing/unsafe boundary must fail closed
        contender = inspect_process_lease(
            lease_path,
            protocol="friday.backend.v1",
        )
        return _live_backend_report(
            settings,
            contender,
            check_llm_port=check_llm_port,
            check_secrets=check_secrets,
        )
    try:
        offline_database = _database_status(settings.database_path)
        offline_workers = _worker_status(settings, None)
        offline_auth_failures = _auth_failure_status(
            settings.database_path,
            None,
            threshold=settings.auth_failure_alert_threshold,
        )
        offline_host_control_counts = _offline_host_control_sqlite_counts(settings.database_path)
    finally:
        boundary.release()
    return _collect_diagnostics_under_boundary(
        settings,
        None,
        backend_lease,
        check_llm_port=check_llm_port,
        check_secrets=check_secrets,
        offline_database=offline_database,
        offline_workers=offline_workers,
        offline_auth_failures=offline_auth_failures,
        offline_host_control_counts=offline_host_control_counts,
    )


def _collect_diagnostics_under_boundary(
    settings: FridaySettings,
    storage: FridayStorage | None,
    backend_lease: dict[str, Any],
    *,
    check_llm_port: bool,
    check_secrets: bool,
    offline_database: dict[str, Any] | None = None,
    offline_workers: dict[str, Any] | None = None,
    offline_auth_failures: dict[str, Any] | None = None,
    offline_host_control_counts: dict[str, bool | int] | None = None,
) -> dict[str, Any]:
    """Build a report from in-process storage or lease-protected snapshots."""

    backend_active = backend_lease.get("active") is True or backend_lease.get("state") == "active_hint"
    if storage is None:
        if (
            offline_database is None
            or offline_workers is None
            or offline_auth_failures is None
            or offline_host_control_counts is None
        ):
            raise RuntimeError("offline diagnostics require lease-protected snapshots")
        database = offline_database
        workers = offline_workers
        host_control_counts = offline_host_control_counts
    else:
        database = storage.diagnostics()
        workers = _worker_status(settings, storage)
        host_control_counts = _host_control_sqlite_counts(storage.execute)
    workers = _configured_worker_status(settings, workers)

    configuration = validate_settings(settings, production=not settings.is_loopback_bind)
    backups = _latest_backup_status(settings.backups_dir)
    memory_vault = _memory_vault_projection_status(settings)
    bridge_queue = _bridge_queue_status_without_live_open(settings.state_dir / "telegram-inbox.sqlite3")
    if not backend_active and workers.get("stale_tasks"):
        # A stopped backend naturally has old worker timestamps.  Keep the
        # evidence visible, but do not diagnose intentional downtime as a
        # failed scheduler.
        workers["healthy"] = not bool(workers.get("degraded_tasks"))
        workers["stale_while_backend_stopped"] = True

    model = _model_status(settings.model_dir)
    actions: list[dict[str, Any]] = []

    def add_action(
        code: str,
        severity: str,
        title: str,
        detail: str,
        command: str | None = None,
    ) -> None:
        action: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "title": title,
            "detail": detail,
        }
        if command:
            action["command"] = command
        actions.append(action)

    host_control = _host_control_status(settings, host_control_counts)
    _add_host_control_actions(add_action, host_control)

    for issue in configuration:
        warning = issue.startswith("warning:")
        add_action(
            "configuration_warning" if warning else "configuration_error",
            "warning" if warning else "error",
            "Проверьте конфигурацию",
            issue.removeprefix("warning:").strip(),
            "jericho doctor",
        )
    if memory_vault["legacy_projection_present"] is True:
        add_action(
            "legacy_memory_vault_projection",
            "warning",
            "Отключённая Markdown-проекция всё ещё существует",
            f"Найдено полнотекстовых заметок проекции: "
            f"{memory_vault['projection_note_count']}. "
            "Friday не читает и не удаляет их автоматически; сначала проверьте "
            "назначение и резервную копию, затем выберите отдельный план remediation.",
        )
    elif memory_vault["legacy_projection_present"] is None:
        add_action(
            "legacy_memory_vault_projection_uninspected",
            "warning",
            "Отключённую Markdown-проекцию не удалось полностью проверить",
            "Friday не читал и не изменял файлы проекции; проверьте каталог локально.",
            "jericho doctor",
        )
    if check_secrets:
        _add_secret_hygiene_actions(add_action, settings)
    _add_inbox_backlog_action(add_action, database)
    _add_outbound_stall_action(add_action, database)
    _add_versions_growth_action(add_action, database)
    database_state = str(database.get("state") or "")
    if database_state == "not_initialized":
        add_action(
            "initialize_database",
            "setup",
            "База знаний ещё не инициализирована",
            "Запустите backend: схема и служебный пользователь будут созданы атомарно.",
            "jericho server",
        )
    elif not database.get("ok", True):
        add_action(
            "repair_database",
            "error",
            "SQLite требует внимания",
            "Не продолжайте запись до проверки целостности или восстановления подтверждённой копии.",
            "jericho restore-backup --yes",
        )
    # Local weights are only needed when this host is the one serving the model. With
    # the endpoint on another machine there is nothing to place in model_dir, and the
    # advice ("or configure a working vLLM endpoint") is telling the owner to do what
    # they have already done.
    llm_host = urlparse(settings.llm_base_url).hostname or ""
    serves_locally = llm_host in {"127.0.0.1", "localhost", "::1", ""}
    if settings.llm_enabled and model.get("placeholder_only") and serves_locally:
        add_action(
            "install_model_weights",
            "warning",
            "Веса локальной модели не обнаружены",
            f"Поместите snapshot модели в {settings.model_dir} или настройте рабочий vLLM endpoint.",
        )
    backup_state = str(backups.get("state") or "")
    if backup_state in {"directory_missing", "no_backups"} and database.get("exists"):
        add_action(
            "create_first_backup",
            "warning",
            "Нет проверенной резервной копии",
            "Создайте первую online-копию SQLite и сохраните отдельно файловое хранилище.",
            "jericho backup --label first",
        )
    elif backup_state == "invalid":
        add_action(
            "replace_invalid_backup",
            "error",
            "Резервные копии не прошли проверку",
            "Не используйте их для восстановления; создайте новую подтверждённую копию.",
            "jericho backup --label recovery",
        )

    # Оригиналы файлов: бэкап базы уносит извлечённый текст, а PDF/сканы/фото/голос
    # жили в одном экземпляре. Воркер пишет отчёт в `workers:last_files_backup`;
    # молчащий или неполный отчёт — это документы без единой копии.
    files_backup: dict[str, Any] = {}
    if storage is not None:
        raw_files_report = storage.kv_get("workers:last_files_backup")
        if raw_files_report:
            with suppress(TypeError, ValueError):
                parsed = json.loads(raw_files_report)
                if isinstance(parsed, dict):
                    files_backup = parsed
        files_present = False
        with suppress(OSError):
            files_present = any(path.is_file() for path in settings.files_dir.rglob("*"))
        if files_present and not files_backup:
            add_action(
                "files_backup_never_ran",
                "warning",
                "Оригиналы файлов не имеют ни одной резервной копии",
                "Бэкап базы уносит извлечённый текст, но сами PDF, сканы, фото и голосовые "
                "существуют в одном экземпляре. Суточный воркер скопирует их в "
                "backups/files/ при следующем прогоне; если строка не исчезнет — "
                "проверьте журнал воркера.",
            )
        elif files_backup and not files_backup.get("complete", True):
            add_action(
                "files_backup_incomplete",
                "warning",
                "Бэкап оригиналов файлов не дошёл до конца",
                f"Отложено {files_backup.get('pending', 0)}, ошибок {files_backup.get('failed', 0)} "
                f"из {files_backup.get('total', 0)}. Остаток докопирует следующий суточный прогон; "
                "повторяющиеся ошибки — повод посмотреть на диск.",
            )

    # The mirror worker has always written its outcome to `workers:last_backup_mirror`
    # and nothing has ever read it. An offsite copy that stopped being made — an
    # unplugged disk, a failing copy — was therefore invisible everywhere: the report
    # said the local backups were fine, which they were, and said nothing at all
    # about the copy that exists to survive the local disk dying.
    mirror = _mirror_status(settings, storage)
    if not mirror.get("enabled") and database.get("exists"):
        # ЕДИНСТВЕННОЕ состояние, о котором система молчала, — и самое опасное. Шесть
        # тщательно написанных тревог ниже (каталог не смонтирован, тот же диск,
        # копия устарела, копирование упало) начинаются с «если зеркало включено»,
        # то есть срабатывают только у того, кто уже позаботился. Тому, кто не
        # позаботился, не говорилось ничего.
        #
        # Замерено на живой машине: 1.3 ГБ данных, всё на одном разделе. Копии
        # честные — 12 пар с манифестами, все контрольные суммы сошлись, — но это
        # защита от повреждения базы, а не от гибели диска. Формулировка обязана
        # называть именно эту разницу, иначе человек посмотрит на «Latest backup:
        # verified» и успокоится.
        add_action(
            "backup_mirror_not_configured",
            "warning",
            "Копии есть, но все на одном диске",
            "Резервные копии лежат рядом с базой, на том же разделе. Они спасут от "
            "повреждения файла, но не от гибели диска: погибнет и база, и копии, и "
            "оригиналы документов. Укажите FRIDAY_BACKUP_MIRROR_DIR на внешний диск "
            "или синхронизируемую папку.",
            "jericho backup --label offsite",
        )
    if mirror.get("enabled"):
        if mirror.get("error") == "mirror_dir_missing":
            add_action(
                "mirror_dir_missing",
                "error",
                "Каталог зеркала бэкапов недоступен",
                f"{mirror.get('mirror_dir')} не смонтирован. Offsite-копии НЕ создаются; "
                "локальные копии не защищают от отказа диска.",
            )
        elif mirror.get("plaintext_leftovers"):
            leftovers = mirror.get("plaintext_leftovers") or []
            add_action(
                "mirror_plaintext_leftovers",
                "warning",
                "В зеркале остались незашифрованные копии базы",
                f"{len(leftovers)} шт. в {mirror.get('mirror_dir')}: шифрование включили позже, "
                "и старые открытые копии остались рядом с новыми. Их не удаляет ни зеркало, ни "
                "прореживание бэкапов — удалите вручную: " + ", ".join(str(name) for name in leftovers[:5]),
            )
        elif mirror.get("failed"):
            add_action(
                "mirror_failed",
                "error",
                "Зеркалирование бэкапов завершилось с ошибками",
                f"Не удалось скопировать {mirror.get('failed')} копий в {mirror.get('mirror_dir')}.",
            )
        elif mirror.get("same_device"):
            add_action(
                "mirror_same_device",
                "warning",
                "Зеркало на том же устройстве, что и бэкапы",
                "Копия на том же диске не переживёт его отказ — укажите внешний носитель.",
            )
        elif mirror.get("stale"):
            add_action(
                "mirror_stale",
                "warning",
                "Зеркало отстаёт от локальных бэкапов",
                f"Последнее зеркалирование: {mirror.get('reported_at') or 'неизвестно'}, "
                "а локальная копия новее.",
            )
    else:
        age = (backups.get("latest") or {}).get("age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool) and age > 7 * 86400:
            add_action(
                "refresh_backup",
                "warning",
                "Последняя копия старше семи дней",
                "Обновите резервную копию с учётом приемлемого для вас RPO.",
                "jericho backup --label scheduled",
            )
    if workers.get("degraded_tasks"):
        add_action(
            "inspect_failed_workers",
            "error",
            "Фоновые задачи повторно завершаются ошибкой",
            "Проверьте перечисленные worker-задачи и журнал backend перед перезапуском.",
            "jericho doctor",
        )
    if backend_active and workers.get("stale_tasks"):
        add_action(
            "inspect_stale_workers",
            "error",
            "Backend активен, но worker-задачи не обновляют состояние",
            "Проверьте зависание event loop, доступность SQLite и последние ошибки задач.",
            "jericho doctor",
        )
    if not backend_lease.get("healthy", True):
        add_action(
            "inspect_backend_lease",
            "error",
            "Файл process lease небезопасен или принадлежит другому протоколу",
            "Не удаляйте lock-файл при работающем процессе; сначала установите владельца lease.",
        )
    dead_letters = int(bridge_queue.get("dead_letter") or 0)
    if dead_letters:
        add_action(
            "inspect_bridge_dead_letters",
            "warning",
            "Есть недоставленные сообщения Telegram (dead-letter)",
            f"{dead_letters} обновлений не удалось обработать — они не потеряны, "
            "но требуют внимания. Проверьте журнал моста и последнюю ошибку.",
            "jericho status",
        )
    auth_failures = (
        offline_auth_failures
        if storage is None
        else _auth_failure_status(
            settings.database_path,
            storage,
            threshold=settings.auth_failure_alert_threshold,
        )
    )
    if auth_failures is None:  # guarded at the helper boundary; keeps the type explicit
        raise RuntimeError("offline auth diagnostics snapshot is missing")
    if auth_failures["threshold"] > 0 and auth_failures["recent_failures"] >= auth_failures["threshold"]:
        shown = f"{'≥' if auth_failures['capped'] else ''}{auth_failures['recent_failures']}"
        add_action(
            "inspect_auth_failure_burst",
            "warning",
            "Всплеск неудачных аутентификаций",
            f"{shown} провалов auth за 24 часа (порог {auth_failures['threshold']}). "
            "Возможен брутфорс или злоупотребление токеном. Смотреть: "
            "GET /api/admin/audit?action=auth.failed — в поле after_json каждой записи "
            "лежат метод и путь, рядом ip_address. Собственная массовая работа сюда "
            "больше не попадает: придержанный по частоте вошедший пользователь "
            "пишется отдельным действием request.throttled.",
            "jericho status",
        )

    # Embedding-index coverage had no observability at all, so a chunking regression
    # (rows silently not written) would only surface later as degraded answers.
    embeddings_coverage: dict[str, Any] = {"available": False}
    if storage is not None:
        try:
            embeddings_coverage = {
                "available": True,
                "indexed_objects": storage.count_knowledge_embeddings(),
                "chunked_objects": storage.count_chunked_knowledge_objects(),
                "chunk_rows": storage.count_knowledge_chunk_embeddings(),
            }
        except Exception:  # noqa: BLE001 - coverage is advisory, never a health gate
            embeddings_coverage = {"available": False}

    result: dict[str, Any] = {
        "ok": not any(not issue.startswith("warning:") for issue in configuration)
        and bool(database.get("ok", True))
        and backups.get("state") != "invalid"
        and bool(workers.get("healthy", True))
        and bool(backend_lease.get("healthy", True))
        and bool(host_control.get("healthy", True)),
        "configuration_issues": configuration,
        "paths": {
            "home": _path_status(settings.home),
            "state": _path_status(settings.state_dir),
            "files": _path_status(settings.files_dir),
            "vault": _path_status(settings.memory_vault_dir),
            "backups": _path_status(settings.backups_dir),
            "exports": _path_status(settings.exports_dir),
            "model": model,
        },
        "database": database,
        "backups": backups,
        "files_backup": files_backup,
        "workers": workers,
        "backend_lease": backend_lease,
        "bridge_queue": bridge_queue,
        "auth_failures": auth_failures,
        "embeddings_index": embeddings_coverage,
        "memory_vault": memory_vault,
        "host_control": host_control,
        "runtime": SystemTelemetry(settings.home).snapshot(),
        "features": {
            "llm_enabled": settings.llm_enabled,
            "embeddings_enabled": settings.embeddings_enabled,
            "workers_enabled": settings.workers_enabled,
            "code_execution_enabled": settings.code_execution_enabled,
            "web_private_networks_allowed": settings.web_allow_private_networks,
        },
        "actions": actions,
    }
    if check_llm_port and settings.llm_enabled:
        llm = _llm_endpoint_status(settings.llm_base_url, settings.llm_model, api_key=settings.llm_api_key)
        result["llm_endpoint"] = llm
        reachable = bool(llm.get("reachable"))
        model_served = llm.get("model_served")
        result["ok"] = result["ok"] and reachable and model_served is not False
        if not reachable:
            add_action(
                "start_llm_runtime",
                "error",
                "Локальная модель недоступна",
                f"Проверьте vLLM endpoint {settings.llm_base_url} и профиль {settings.profile.name}.",
                "docker compose --profile vllm up -d",
            )
        elif model_served is False:
            served = ", ".join(llm.get("served_models") or []) or "—"
            add_action(
                "llm_model_not_served",
                "error",
                "Настроенная модель не обслуживается endpoint'ом",
                f"vLLM отвечает, но не отдаёт модель '{settings.llm_model}'. "
                f"Проверьте FRIDAY_LLM_MODEL и имя модели vLLM. Обслуживаются: {served}.",
            )
        else:
            # Порт открыт и модель в списке — но это ещё не «работает».
            #
            # Живой отказ 2026-08-03: обе проверки выше были зелёными (список
            # отдавался за 0.019 с), а генерация висела и обрывалась пустым
            # ответом. Двадцать минут, восемь испорченных ответов живому человеку,
            # и ни одного сигнала владельцу — сторож смотрел не туда.
            generation = _llm_generates(
                settings.llm_base_url, settings.llm_model, api_key=settings.llm_api_key
            )
            llm["generation"] = generation
            if generation.get("generates") is False:
                result["ok"] = False
                add_action(
                    "llm_not_generating",
                    "error",
                    "Модель принимает соединения, но не отвечает",
                    f"Порт {settings.llm_base_url} открыт и модель '{settings.llm_model}' в списке, "
                    f"но запрос на генерацию не вернул ответа за {generation.get('seconds')} с. "
                    "Людям в это время уходят испорченные ответы. Нужен перезапуск сервера модели.",
                )
    # Покрытие корпуса векторами. Число собиралось и НИ С ЧЕМ не сравнивалось: лежало
    # в свёрнутом JSON-дампе рядом с числом объектов, и сопоставить их было некому.
    # А расходятся они буднично — после смены модели, после правки разбиения на
    # пассажи, после ночи с недоступным сервисом. Поиск при этом работает: просто
    # часть архива в него не попадает, и узнать об этом можно только по ответам.
    counts = (result.get("database") or {}).get("counts") or {}
    live_objects = int(counts.get("knowledge_objects") or 0)
    indexed = int(embeddings_coverage.get("indexed_objects") or 0)
    if settings.embeddings_enabled and embeddings_coverage.get("available") and live_objects:
        embeddings_coverage["expected_objects"] = live_objects
        embeddings_coverage["coverage"] = round(indexed / live_objects, 4)
        # Десятая часть — не придирка, а порог заметности: при таком разрыве плотный
        # канал уже теряет документы, но система всё ещё выглядит здоровой.
        if indexed < live_objects * 0.9:
            add_action(
                "embeddings_coverage_low",
                "warning",
                "Часть архива не попала в смысловой поиск",
                f"Векторов {indexed} на {live_objects} объектов "
                f"({indexed / live_objects:.0%}). Поиск работает и не выглядит сломанным, "
                "но найти по смыслу то, чего нет в индексе, нельзя. Обычно догоняется само; "
                "если число не растёт — проверьте сервис эмбеддингов.",
            )
        # Потолки плотного отбора. Скан на запросе берёт только НОВЕЙШИЕ N строк
        # (окно по updated_at), и корпус, переросший окно, теряет из смыслового
        # поиска ровно старейшие документы — молча: признак dense_chunks_capped
        # жил только в explain-трейсе отдельного запроса, доктор и sentinel его не
        # видели. На корпусе владельца окно пассажей исчерпано уже сейчас.
        chunk_rows = int(embeddings_coverage.get("chunk_rows") or 0)
        object_cap = max(0, int(settings.embeddings_dense_max_objects))
        chunk_cap = object_cap * max(1, int(settings.embeddings_chunk_scan_multiplier))
        if object_cap:
            embeddings_coverage["dense_object_cap"] = object_cap
            embeddings_coverage["dense_chunk_cap"] = chunk_cap
            if chunk_rows >= chunk_cap * 0.85 or indexed >= object_cap * 0.85:
                clipped = chunk_rows >= chunk_cap or indexed >= object_cap
                add_action(
                    "dense_scan_window_near_cap",
                    "warning",
                    (
                        "Смысловой поиск уже не видит часть архива"
                        if clipped
                        else "Корпус приближается к окну плотного отбора"
                    ),
                    f"Пассажей {chunk_rows} при окне {chunk_cap}, объектных векторов "
                    f"{indexed} при окне {object_cap}. Скан берёт новейшие строки; всё, "
                    "что старше окна, выпадает из смыслового поиска без внешних признаков. "
                    "Быстрое лечение — поднять FRIDAY_EMBEDDINGS_DENSE_MAX_OBJECTS "
                    "(цена: время каждого запроса растёт с окном); настоящее — "
                    "резидентный индекс векторов, он в очереди работ.",
                )

    # Свободное место. Тоже собиралось и лежало в байтах внутри дампа: при 99%
    # занятости состояние осталось бы «ready». А кончившееся место — это не медленная
    # деградация, а мгновенная остановка записи, и SQLite отдаёт «disk I/O error»,
    # который человеку ничего не объясняет.
    disk = (result.get("runtime") or {}).get("disk") or {}
    total_bytes = int(disk.get("total_bytes") or 0)
    free_bytes = int(disk.get("free_bytes") or 0)
    if total_bytes > 0:
        free_share = free_bytes / total_bytes
        disk["free_share"] = round(free_share, 4)
        if free_share < 0.05 or free_bytes < 1_000_000_000:
            add_action(
                "disk_space_low",
                "error" if free_share < 0.02 else "warning",
                "Заканчивается место на диске",
                f"Свободно {free_bytes / 1_000_000_000:.1f} ГБ из "
                f"{total_bytes / 1_000_000_000:.1f} ({free_share:.0%}). "
                "При нуле запись останавливается сразу: база отдаёт «disk I/O error», "
                "и это выглядит как поломка, а не как кончившееся место.",
            )

    if check_llm_port and settings.embeddings_enabled and settings.embeddings_base_url:
        # Сервис эмбеддингов проверяется ОТДЕЛЬНО от чат-модели, потому что падает он
        # отдельно и молча. Замерено на этой установке: при мёртвом :8002 фоновая
        # индексация завершается УСПЕШНО (backend возвращает None, исключения нет,
        # `consecutive_failures` остаётся нулём, воркер считается здоровым), а поиск
        # продолжает отвечать за прежние ~1.5 с — просто без семантического канала.
        # Ни один индикатор при этом не меняется, и человек узнаёт об этом только по
        # ухудшившимся ответам, то есть никогда.
        #
        # Проба та же, что у чат-модели: список моделей, а не генерация. Она не грузит
        # видеокарту — важно, потому что на этой установке карта одна на три сервиса.
        embeddings = _llm_endpoint_status(
            settings.embeddings_base_url,
            settings.embeddings_model,
            api_key=settings.embeddings_api_key,
        )
        result["embeddings_endpoint"] = embeddings
        reachable = bool(embeddings.get("reachable"))
        model_served = embeddings.get("model_served")
        result["ok"] = result["ok"] and reachable and model_served is not False
        if not reachable:
            add_action(
                "start_embeddings_runtime",
                "error",
                "Сервис эмбеддингов недоступен — поиск работает без смысла",
                f"Не отвечает {settings.embeddings_base_url}. Лексический и полнотекстовый "
                "каналы продолжат отвечать, поэтому поиск НЕ выглядит сломанным — он просто "
                "перестаёт находить по смыслу, а новые документы не попадают в индекс.",
            )
        elif model_served is False:
            served = ", ".join(embeddings.get("served_models") or []) or "—"
            add_action(
                "embeddings_model_not_served",
                "error",
                "Сервис эмбеддингов не отдаёт настроенную модель",
                f"Endpoint отвечает, но модели '{settings.embeddings_model}' у него нет. "
                f"Обслуживаются: {served}. Вектора, посчитанные другой моделью, несравнимы "
                "с уже сохранёнными.",
            )

    if check_llm_port and settings.rerank_top > 0 and settings.rerank_base_url:
        # Третья служба в том же положении, что и вторая, и падает так же тихо. Клиент
        # переранжировщика по построению НЕ роняет поиск: не ответила — выдача остаётся
        # в прежнем порядке. Замерено, чего это стоит: внутри пула прежний порядок
        # различает отвечающие документы на уровне монетки (AUC 0.512) против 0.754 с
        # переранжировщиком. То есть отказ службы откатывает поиск на ту самую точку,
        # ради ухода из которой её и поднимали, и не меняет при этом НИ ОДНОГО признака.
        rerank = _llm_endpoint_status(
            settings.rerank_base_url,
            settings.rerank_model,
            api_key=settings.rerank_api_key,
        )
        result["rerank_endpoint"] = rerank
        reachable = bool(rerank.get("reachable"))
        model_served = rerank.get("model_served")
        result["ok"] = result["ok"] and reachable and model_served is not False
        if not reachable:
            add_action(
                "start_rerank_runtime",
                "error",
                "Переранжировщик недоступен — выдача снова в случайном порядке",
                f"Не отвечает {settings.rerank_base_url}. Поиск продолжит находить те же "
                "документы, но перестанет ставить отвечающие наверх, и по виду выдачи это "
                "не отличить от обычного дня.",
            )
        elif model_served is False:
            served = ", ".join(rerank.get("served_models") or []) or "—"
            add_action(
                "rerank_model_not_served",
                "error",
                "Переранжировщик не отдаёт настроенную модель",
                f"Endpoint отвечает, но модели '{settings.rerank_model}' у него нет. "
                f"Обслуживаются: {served}.",
            )

    result["state"] = _diagnostic_state(bool(result["ok"]), actions)
    return result


__all__ = [
    "collect_diagnostics",
    "collect_document_contour_guarded_bridge_queue_snapshot",
    "collect_document_contour_observer_snapshot",
]
