"""Actionable local diagnostics for operators and the Admin API."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from jericho.config import JerichoSettings, validate_settings
from jericho.diagnostics.runtime_lease import inspect_process_lease
from jericho.telemetry import SystemTelemetry

if TYPE_CHECKING:
    from jericho.storage import JerichoStorage


def _path_status(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "readable": os.access(path, os.R_OK) if path.exists() else False,
        "writable": os.access(path, os.W_OK) if path.exists() else os.access(path.parent, os.W_OK),
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
    configured model is actually served. A wrong ``JERICHO_LLM_MODEL`` is the most
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
                    "SELECT COUNT(*) AS count FROM outbound_notifications WHERE status='pending'"
                ).fetchone()
                database["outbound_pending"] = int(pending_row["count"] if pending_row else 0)
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
        if status not in {"scheduled", "running", "ok", "error", "timeout", "unknown"}:
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
    settings: JerichoSettings,
    storage: JerichoStorage | None,
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
        "last_dead_letter_error": (str(recent["last_error"])[:200] if recent else ""),
    }


def collect_diagnostics(
    settings: JerichoSettings,
    storage: JerichoStorage | None = None,
    *,
    check_llm_port: bool = False,
) -> dict[str, Any]:
    """Collect safe diagnostics without exposing secrets or document contents."""
    configuration = validate_settings(settings, production=not settings.is_loopback_bind)
    database = storage.diagnostics() if storage is not None else _database_status(settings.database_path)
    backups = _latest_backup_status(settings.backups_dir)
    workers = _worker_status(settings, storage)
    backend_lease = inspect_process_lease(
        settings.state_dir / "backend.lock",
        protocol="jericho.backend.v1",
    )
    bridge_queue = _bridge_queue_status(settings.state_dir / "telegram-inbox.sqlite3")
    backend_active = backend_lease.get("active") is True or backend_lease.get("state") == "active_hint"
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

    for issue in configuration:
        warning = issue.startswith("warning:")
        add_action(
            "configuration_warning" if warning else "configuration_error",
            "warning" if warning else "error",
            "Проверьте конфигурацию",
            issue.removeprefix("warning:").strip(),
            "jericho doctor",
        )
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
    if settings.llm_enabled and model.get("placeholder_only"):
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
    dead_letters = int(bridge_queue.get("dead_letter", 0))
    if dead_letters:
        add_action(
            "inspect_bridge_dead_letters",
            "warning",
            "Есть недоставленные сообщения Telegram (dead-letter)",
            f"{dead_letters} обновлений не удалось обработать — они не потеряны, "
            "но требуют внимания. Проверьте журнал моста и последнюю ошибку.",
            "jericho status",
        )

    result: dict[str, Any] = {
        "ok": not any(not issue.startswith("warning:") for issue in configuration)
        and bool(database.get("ok", True))
        and backups.get("state") != "invalid"
        and bool(workers.get("healthy", True))
        and bool(backend_lease.get("healthy", True)),
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
        "workers": workers,
        "backend_lease": backend_lease,
        "bridge_queue": bridge_queue,
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
                f"Проверьте JERICHO_LLM_MODEL и имя модели vLLM. Обслуживаются: {served}.",
            )
    severities = {str(item.get("severity")) for item in actions}
    if not result["ok"] or "error" in severities:
        result["state"] = "degraded"
    elif "setup" in severities:
        result["state"] = "setup_required"
    elif "warning" in severities:
        result["state"] = "attention"
    else:
        result["state"] = "ready"
    return result


__all__ = ["collect_diagnostics"]
