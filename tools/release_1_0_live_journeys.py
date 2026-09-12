#!/usr/bin/env python3
"""Additional R10 user-journey suite.

Deterministic cases use an isolated TestClient (LLM off). Isolated-live cases
require --run-live and a dedicated env file; they never target the production
home. This runner does not rewrite sealed A/B manifests.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_1_0_acceptance as acceptance  # noqa: E402

SCHEMA = "friday.release-1-0-live-journeys.v1"
CANONICAL_PRIVATE_URL = "http://127.0.0.1:9/private-r10-never-search"


class JourneyError(RuntimeError):
    pass


def _owner_headers(settings: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _issue_token(storage: Any, user_id: str, preset: str, secret: str) -> None:
    storage.ensure_user(user_id, source="api-token", display_name=user_id, preset_key=preset)
    storage.update_user(user_id, preset_key=preset)
    token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    storage.create_api_token(user_id, token_hash, label="r10", created_by="r10")


def _upload_bytes(
    client: TestClient, headers: dict[str, str], filename: str, payload: bytes, mime: str
) -> dict[str, Any]:
    response = client.post(
        "/api/files",
        headers=headers,
        files={"file": (filename, payload, mime)},
        data={"source_ref": f"r10:{filename}"},
    )
    return {"status_code": response.status_code, "body": response.json() if response.content else {}}


def _file_count(client: TestClient, headers: dict[str, str]) -> int:
    response = client.get("/api/files", headers=headers)
    if response.status_code != 200:
        return -1
    return int(response.json().get("count") or 0)


def _canary(case_id: str) -> str:
    return f"CANARY_{case_id.replace('-', '_')}_R10"


def _json_object(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _listed_raw_ids(body: dict[str, Any]) -> set[str]:
    return {
        str(item["id"]) for item in (body.get("items") or []) if isinstance(item, dict) and item.get("id")
    }


def run_j01(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    canary = _canary("R10_J01")
    payload = f"event kitchen participants 4 {canary}\n".encode()
    expected_digest = hashlib.sha256(payload).hexdigest()
    headers = _owner_headers(settings)
    failures: list[str] = []
    observations: list[dict[str, Any]] = []
    raw_id = ""
    for phase in ("initial", "restart"):
        with TestClient(create_app(settings)) as client:
            if phase == "initial":
                uploaded = _upload_bytes(client, headers, "r10-j01.txt", payload, "text/plain")
                raw_id = str(uploaded["body"].get("raw_object_id") or "")
                if uploaded["status_code"] != 200 or not raw_id:
                    failures.append("upload_missing_handle")
            inbox = client.get("/api/admin/inbox", headers=headers)
            if inbox.status_code != 200:
                failures.append(f"{phase}_inbox_unreachable")
            search = client.get("/api/search", params={"q": canary}, headers=headers)
            results = search.json().get("results", []) if search.status_code == 200 else []
            hits = [
                item
                for item in results
                if isinstance(item, dict) and canary in str(item.get("content") or "")
            ]
            sources = [
                item for item in hits if raw_id and item.get("raw_object_id") == raw_id and item.get("id")
            ]
            if search.status_code != 200:
                failures.append(f"{phase}_search_failed")
            elif not hits:
                failures.append(f"{phase}_search_missing_hit")
            elif not sources:
                failures.append(f"{phase}_search_wrong_source")
            if sources:
                citation = client.get(f"/api/knowledge/{sources[0]['id']}", headers=headers)
                cited = citation.json().get("item", {}) if citation.status_code == 200 else {}
                if cited.get("raw_object_id") != raw_id or canary not in str(cited.get("content") or ""):
                    failures.append(f"{phase}_search_citation_unreadable")
            download = client.get(f"/api/files/{raw_id or 'raw_missing_r10'}", headers=headers)
            digest = hashlib.sha256(download.content).hexdigest() if download.status_code == 200 else ""
            if digest != expected_digest:
                failures.append(f"{phase}_artifact_missing_or_changed")
            observations.append(
                {
                    "phase": phase,
                    "search_status": search.status_code,
                    "hits": len(sources),
                    "download_status": download.status_code,
                    "file_sha256": digest,
                }
            )
    return {
        "id": "R10-J01-UPLOAD-SEARCH-RESTART",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "observed_safe": observations,
    }


def run_j02(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    owner_canary = _canary("R10_J02_OWNER")
    guest_canary = _canary("R10_J02_GUEST")
    app = create_app(settings)
    owner = _owner_headers(settings)
    guest_secret = "jrc_r10_guest_" + hashlib.sha256(owner_canary.encode()).hexdigest()[:12]
    guest = {"Authorization": f"Bearer {guest_secret}"}
    with TestClient(app) as client:
        _issue_token(app.state.storage, "r10-guest", "user", guest_secret)
        owner_upload = _upload_bytes(client, owner, "owner-r10-j02.txt", owner_canary.encode(), "text/plain")
        guest_upload = _upload_bytes(client, guest, "guest-r10-j02.txt", guest_canary.encode(), "text/plain")
        owner_files = client.get("/api/files", headers=owner)
        guest_files = client.get("/api/files", headers=guest)
        guest_admin = client.get("/api/admin/overview", headers=guest)
        owner_body = _json_object(owner_files)
        guest_body = _json_object(guest_files)
    owner_id = str(owner_upload["body"].get("raw_object_id") or "")
    guest_id = str(guest_upload["body"].get("raw_object_id") or "")
    owner_ids = _listed_raw_ids(owner_body)
    guest_ids = _listed_raw_ids(guest_body)
    failures: list[str] = []
    if owner_upload["status_code"] != 200 or not owner_id:
        failures.append("owner_upload_missing_handle")
    if guest_upload["status_code"] != 200 or not guest_id:
        failures.append("guest_upload_missing_handle")
    if owner_id and guest_id and owner_id == guest_id:
        failures.append("principal_handles_not_distinct")
    if owner_files.status_code != 200:
        failures.append("owner_file_listing_unavailable")
    if guest_files.status_code != 200:
        failures.append("guest_file_listing_unavailable")
    if owner_id and owner_id not in owner_ids:
        failures.append("owner_handle_missing_from_owner_listing")
    if guest_id and guest_id not in guest_ids:
        failures.append("guest_handle_missing_from_guest_listing")
    if guest_id and guest_id in owner_ids:
        failures.append("guest_handle_exposed_to_owner")
    if owner_id and owner_id in guest_ids:
        failures.append("owner_handle_exposed_to_guest")
    owner_blob = json.dumps(owner_body, ensure_ascii=False)
    guest_blob = json.dumps(guest_body, ensure_ascii=False)
    if guest_canary in owner_blob or owner_canary in guest_blob:
        failures.append("foreign_canary_exposed")
    if guest_admin.status_code not in {401, 403}:
        failures.append("guest_admin_not_refused")
    return {
        "id": "R10-J02-CROSS-CONV-PRIVACY",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "expected_outcome": "refuse",
        "observed_safe": {
            "owner_upload": owner_upload["status_code"],
            "guest_upload": guest_upload["status_code"],
            "owner_listing": owner_files.status_code,
            "guest_listing": guest_files.status_code,
            "guest_admin": guest_admin.status_code,
        },
    }


def run_j03(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    fixtures = (
        ("holdings-v1.csv", b"id,name\nBRK.A,alpha-r10\n", [{"id": "BRK.A", "name": "alpha-r10"}]),
        ("holdings-v2.csv", b"id,name\nBRK.B,beta-r10\n", [{"id": "BRK.B", "name": "beta-r10"}]),
    )
    headers = _owner_headers(settings)
    failures: list[str] = []
    ids = []
    digests = []
    with TestClient(create_app(settings)) as client:
        for filename, payload, expected_rows in fixtures:
            uploaded = _upload_bytes(client, headers, filename, payload, "text/csv")
            raw_id = uploaded["body"].get("raw_object_id")
            if uploaded["status_code"] != 200 or not raw_id:
                failures.append("table_upload_missing_handle")
                continue
            ids.append(raw_id)
            download = client.get(f"/api/files/{raw_id}", headers=headers)
            if download.status_code != 200:
                failures.append("table_artifact_missing")
                continue
            digest = hashlib.sha256(download.content).hexdigest()
            digests.append(digest)
            if digest != hashlib.sha256(payload).hexdigest():
                failures.append("table_bytes_changed_or_swapped")
            try:
                rows = list(csv.DictReader(io.StringIO(download.content.decode("utf-8"))))
            except (UnicodeError, csv.Error):
                rows = []
            if rows != expected_rows:
                failures.append("table_values_changed_or_swapped")
        if len(set(ids)) != 2:
            failures.append("versions_not_distinct")
    return {
        "id": "R10-J03-TABLE-VERSIONS",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "observed_safe": {"count": len(set(ids)), "file_sha256": digests},
    }


def _url_refusal_state(app: Any) -> str:
    tables = (
        "raw_objects",
        "file_source_aliases",
        "inbox",
        "knowledge_objects",
        "knowledge_object_versions",
    )
    digest = hashlib.sha256()
    size = 0

    def observe(value: Any) -> None:
        nonlocal size
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        size += len(payload)
        if size > 64 * 1024 * 1024:
            raise JourneyError("url_refusal_state_limit")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    for table in tables:
        observe(table)
        for row in app.state.storage.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            observe(tuple(row))
    files_dir = app.state.settings.files_dir
    if files_dir.is_symlink():
        raise JourneyError("url_refusal_files_symlink")
    files = _snapshot_files(files_dir) if files_dir.exists() else {}
    observe(files)
    return digest.hexdigest()


def run_j04(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        before = client.get("/api/knowledge", headers=headers)
        before_state = _url_refusal_state(app)
        response = client.post("/api/ingest/url", headers=headers, json={"url": CANONICAL_PRIVATE_URL})
        after_state = _url_refusal_state(app)
        after = client.get("/api/knowledge", headers=headers)
    failures: list[str] = []
    if before.status_code != 200:
        failures.append("knowledge_listing_before_unavailable")
    if after.status_code != 200:
        failures.append("knowledge_listing_after_unavailable")
    if response.status_code != 422:
        failures.append("private_url_not_refused")
    if "source_fact_private" not in response.text:
        failures.append("private_url_reason_missing")
    if after_state != before_state:
        failures.append("private_url_persistent_effect")
    return {
        "id": "R10-J04-PRIVATE-URL",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "expected_outcome": "refuse",
        "observed_safe": {
            "before_listing": before.status_code,
            "refusal": response.status_code,
            "after_listing": after.status_code,
            "before_state_sha256": before_state,
            "after_state_sha256": after_state,
        },
    }


def _engineer_refusal_state(app: Any) -> str:
    # Observe actual channel/conversation rows and the command ledger/workspace
    # bytes. sqlite_master only describes schemas and cannot prove no effects.
    rows = []
    for table in ("conversations", "channel_sessions"):
        rows.append(
            [
                tuple(row)
                for row in app.state.storage.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            ]
        )
    root = app.state.settings.engineer_command_store_dir
    file_rows = []
    remaining = 64 * 1024 * 1024
    if root.is_symlink():
        raise JourneyError("command_store_snapshot_symlink")
    if root.exists():
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                file_rows.append((relative, "symlink"))
            elif path.is_file():
                with path.open("rb") as handle:
                    content = handle.read(remaining + 1)
                remaining -= len(content)
                if remaining < 0:
                    raise JourneyError("command_store_snapshot_limit")
                file_rows.append((relative, hashlib.sha256(content).hexdigest()))
            elif path.is_dir():
                file_rows.append((relative, "directory"))
            else:
                raise JourneyError("command_store_snapshot_special_file")
    return hashlib.sha256(json.dumps([rows, file_rows], sort_keys=True).encode()).hexdigest()


def run_j05(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        before = _engineer_refusal_state(app)
        response = client.post(
            "/api/conversations/channel/mode",
            headers=headers,
            json={"channel": "api", "channel_id": "r10", "mode": "engineer"},
        )
        after = _engineer_refusal_state(app)
    observed = {"status_code": response.status_code, "effect": after != before, "collected": True}
    expected = {"effect_forbidden": True, "expected_outcome": "refuse"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code not in {400, 403, 503}:
        verdict["failure_codes"].append("engineer_not_refused")
        verdict["status"] = "FAIL"
    return {
        "id": "R10-J05-ENGINEER-REFUSE-UNSIGNED",
        **verdict,
        "observed_safe": {
            "status_code": response.status_code,
            "before_sha256": before,
            "after_sha256": after,
        },
    }


def run_j06(settings: Any) -> dict[str, Any]:
    from friday.orchestration.coding_mode_execute_claim import (
        CodingModeExecuteClaimState,
        build_coding_mode_execute_claim,
    )
    from friday.orchestration.coding_mode_intent import build_coding_mode_intent
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/channel/mode",
            headers=headers,
            json={"channel": "api", "channel_id": "r10", "mode": "coding"},
        )
    intent = build_coding_mode_intent("r10-intent", "r10-turn", upload={"name": "source.zip"})
    claim = build_coding_mode_execute_claim("r10-claim", "r10-turn", intent, operation="build")
    observed = {
        "status_code": response.status_code,
        "body": f"{response.status_code} {claim.state}",
        "effect": claim.state
        not in {
            CodingModeExecuteClaimState.BLOCKED,
            CodingModeExecuteClaimState.EMPTY,
            CodingModeExecuteClaimState.STATIC,
        },
        "collected": True,
    }
    expected = {"effect_forbidden": True, "expected_outcome": "refuse"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code not in {400, 403, 503}:
        verdict["failure_codes"] = [*verdict["failure_codes"], "coding_mode_not_refused"]
        verdict["status"] = "FAIL"
    if claim.state is CodingModeExecuteClaimState.EMPTY:
        # EMPTY without worker is fail-closed for execute; treat as refuse success.
        pass
    elif claim.state is not CodingModeExecuteClaimState.BLOCKED:
        if claim.state is CodingModeExecuteClaimState.STATIC:
            pass
        else:
            verdict["failure_codes"] = [*verdict["failure_codes"], "execute_claim_not_closed"]
            verdict["status"] = "FAIL"
    return {
        "id": "R10-J06-CODING-EXECUTE-CLOSED",
        **verdict,
        "observed_safe": {"mode": response.status_code, "claim": str(claim.state)},
    }


def run_j07(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        body = _json_object(health)
    expected_obsidian = {
        "mode": "disabled",
        "root_sha256": hashlib.sha256(
            str(settings.obsidian_effective_root).encode("utf-8", errors="strict")
        ).hexdigest(),
    }
    obsidian = body.get("obsidian")
    failures: list[str] = []
    if health.status_code != 200:
        failures.append("health_unavailable")
    if body.get("status") != "ok":
        failures.append("health_status_not_ok")
    if getattr(settings, "obsidian_enabled", None) is not False:
        failures.append("obsidian_setting_not_disabled")
    if obsidian != expected_obsidian:
        failures.append("obsidian_health_not_exactly_disabled")
    return {
        "id": "R10-J07-OBSIDIAN-DISABLED",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "expected_outcome": "pass",
        "observed_safe": {
            "health": health.status_code,
            "status_ok": body.get("status") == "ok",
            "obsidian_sha256": hashlib.sha256(json.dumps(obsidian, sort_keys=True).encode()).hexdigest(),
        },
    }


def _settings_in_home(settings: Any, home: Path) -> Any:
    overrides = {}
    for field in fields(settings):
        value = getattr(settings, field.name)
        if isinstance(value, Path) and value.is_relative_to(settings.home):
            overrides[field.name] = home / value.relative_to(settings.home)
    return replace(settings, **overrides)


def _snapshot_files(root: Path) -> dict[str, str]:
    inventory = {}
    remaining = 64 * 1024 * 1024
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise JourneyError("restore_snapshot_symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise JourneyError("restore_snapshot_special_file")
        with path.open("rb") as handle:
            content = handle.read(remaining + 1)
        remaining -= len(content)
        if remaining < 0:
            raise JourneyError("restore_snapshot_limit")
        inventory[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
    return inventory


def _backup_listing(response: Any, failures: list[str], phase: str) -> dict[str, dict[str, Any]]:
    body = _json_object(response)
    items = body.get("items")
    valid = (
        response.status_code == 200
        and isinstance(items, list)
        and type(body.get("count")) is int
        and body["count"] == len(items)
    )
    names = {}
    for item in items if isinstance(items, list) else []:
        name = item.get("database") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".sqlite3")
            or name in names
        ):
            valid = False
        else:
            names[name] = item
    if not valid:
        failures.append(f"backup_{phase}_listing_invalid")
    return names


def _observe_backup_api(
    client: TestClient, settings: Any, raw_id: str, failures: list[str]
) -> dict[str, Any]:
    headers = _owner_headers(settings)
    audit_before = {row["id"] for row in client.app.state.storage.list_audit_log(None, limit=500)}
    key_row = client.app.state.storage.execute(
        "SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'"
    ).fetchone()
    encoded_key = key_row[0] if key_row else None
    if (
        not isinstance(encoded_key, str)
        or len(encoded_key) != 64
        or any(char not in "0123456789abcdef" for char in encoded_key)
    ):
        failures.append("backup_audit_key_unavailable")
        return {}
    # Freeze the existing isolated installation key before the operation. Do
    # not use the product sanitizer to generate this independent expected ref.
    audit_key = bytes.fromhex(encoded_key)
    before = _backup_listing(client.get("/api/admin/backups", headers=headers), failures, "before")
    created = client.post("/api/admin/backups", headers=headers, json={"label": "r10-j08"})
    manifest = _json_object(created).get("backup")
    name = manifest.get("database") if isinstance(manifest, dict) else None
    if (
        created.status_code != 200
        or not isinstance(name, str)
        or Path(name).name != name
        or not name.endswith(".sqlite3")
    ):
        failures.append("backup_create_response_invalid")
        return {"create_status": created.status_code}
    after = _backup_listing(client.get("/api/admin/backups", headers=headers), failures, "after")
    if set(after) != set(before) | {name} or name in before or name not in after:
        failures.append("backup_created_item_not_listed")
    downloaded = client.get(f"/api/admin/backups/{name}/download", headers=headers)
    blob = downloaded.content
    digest = hashlib.sha256(blob).hexdigest()
    if (
        downloaded.status_code != 200
        or downloaded.headers.get("content-type") != "application/vnd.sqlite3"
        or downloaded.headers.get("content-disposition") != f'attachment; filename="{name}"'
        or not blob.startswith(b"SQLite format 3\x00")
        or len(blob) > 64 * 1024 * 1024
    ):
        failures.append("backup_download_invalid")
    else:
        for entry in (manifest, after.get(name, {})):
            if (
                entry.get("sha256") != digest
                or type(entry.get("size_bytes")) is not int
                or entry["size_bytes"] != len(blob)
                or entry.get("label") != "r10-j08"
                or entry.get("integrity_check") != "ok"
            ):
                failures.append("backup_manifest_download_mismatch")
        # Read only the actual downloaded bytes in a private temporary file.
        # immutable avoids importing a sidecar or writing SQLite runtime state.
        with tempfile.TemporaryDirectory(prefix="r10-download-", dir=settings.home) as scratch:
            database_path = Path(scratch) / "download.sqlite3"
            database_path.write_bytes(blob)
            database_path.chmod(0o600)
            try:
                with sqlite3.connect(database_path.as_uri() + "?mode=ro&immutable=1", uri=True) as database:
                    database.execute("PRAGMA trusted_schema=OFF")
                    database.execute("PRAGMA query_only=ON")
                    valid = database.execute("PRAGMA quick_check").fetchall() == [("ok",)]
                    row = database.execute(
                        "SELECT raw_content FROM raw_objects WHERE id=?", (raw_id,)
                    ).fetchone()
                if not valid or row is None or _canary("R10_J08") not in str(row[0]):
                    failures.append("backup_download_source_missing")
            except sqlite3.Error:
                failures.append("backup_download_database_unreadable")
    secret = "jrc_r10_backup_guest_" + "3" * 16
    _issue_token(client.app.state.storage, "r10-backup-guest", "user", secret)
    guest = {"Authorization": f"Bearer {secret}"}
    for principal in ({}, guest):
        responses = (
            client.get("/api/admin/backups", headers=principal),
            client.post("/api/admin/backups", headers=principal, json={"label": "r10-denied"}),
            client.get(f"/api/admin/backups/{name}/download", headers=principal),
        )
        if any(response.status_code not in {401, 403} for response in responses):
            failures.append("backup_unauthorized_access")
        if any(response.content.startswith(b"SQLite format 3\x00") for response in responses):
            failures.append("backup_unauthorized_bytes")
    final = _backup_listing(client.get("/api/admin/backups", headers=headers), failures, "final")
    if final != after:
        failures.append("backup_denied_create_effect")
    missing = client.get("/api/admin/backups/r10-missing.sqlite3/download", headers=headers)
    # An owned outside-root canary proves the download path cannot follow an
    # escaping symlink. This never addresses any existing installation file.
    with tempfile.TemporaryDirectory(prefix="r10-escape-", dir=settings.home) as scratch:
        outside = Path(scratch) / "canary.sqlite3"
        outside.write_bytes(b"R10_BACKUP_OUTSIDE_ROOT")
        link = settings.backups_dir / "r10-escape.sqlite3"
        link.symlink_to(outside)
        try:
            escaped = client.get("/api/admin/backups/r10-escape.sqlite3/download", headers=headers)
        finally:
            link.unlink()
    if missing.status_code != 404 or escaped.status_code != 404:
        failures.append("backup_download_path_boundary")
    rows = [
        row
        for row in client.app.state.storage.list_audit_log(None, limit=500)
        if row["id"] not in audit_before
    ]
    audit_targets = []
    expected_target = (
        "backup:ref:"
        + hmac.new(audit_key, ("target:backup\x00" + name).encode(), hashlib.sha256).hexdigest()[:24]
    )
    for action in ("admin.backup.create", "admin.backup.download"):
        matches = [row for row in rows if row.get("action") == action]
        if len(matches) != 1 or matches[0].get("target_type") != "backup" or not matches[0].get("target_id"):
            failures.append("backup_audit_missing")
        else:
            audit_targets.append(matches[0]["target_id"])
            if matches[0]["target_id"] != expected_target:
                failures.append("backup_audit_private_target_invalid")
        for row in matches:
            projection = dict(row)
            for field in ("before_json", "after_json"):
                if isinstance(projection.get(field), str):
                    try:
                        projection[field] = json.loads(projection[field])
                    except (TypeError, ValueError):
                        failures.append("backup_audit_payload_invalid")
            if name in json.dumps(projection, ensure_ascii=False):
                failures.append("backup_audit_filename_exposed")
    # Backup filenames are privacy-tokenized by the canonical audit writer.
    # Require new, single create/download entries for the same opaque target.
    if len(audit_targets) == 2 and audit_targets[0] != audit_targets[1]:
        failures.append("backup_audit_target_mismatch")
    return {"download_sha256": digest, "download_bytes": len(blob), "download_status": downloaded.status_code}


def run_j08(settings: Any) -> dict[str, Any]:
    from friday.config import ensure_runtime_dirs
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.server import create_app
    from friday.storage import init_storage

    payload = f"restore-me {_canary('R10_J08')}\n".encode()
    expected_digest = hashlib.sha256(payload).hexdigest()
    failures = []
    observed: dict[str, Any] = {}
    # Everything moved or deleted below is newly created by this case. Neither
    # the caller's HOME nor an existing installation is a recovery-drill target.
    with tempfile.TemporaryDirectory(prefix="r10-recovery-", dir=Path(settings.home).parent) as scratch:
        root = Path(scratch)
        source = _settings_in_home(settings, root / "runtime-a")
        ensure_runtime_dirs(source)
        for path in (source.database_path, source.files_dir, source.backups_dir, source.state_dir):
            if not path.is_relative_to(source.home):
                raise JourneyError("configured_snapshot_path_outside_home")
        with TestClient(create_app(source)) as client:
            headers = _owner_headers(source)
            uploaded = _upload_bytes(client, headers, "r10-j08.txt", payload, "text/plain")
            raw_id = str(uploaded["body"].get("raw_object_id") or "")
            if uploaded["status_code"] != 200 or not raw_id:
                failures.append("backup_input_unavailable")
            observed["backup_api"] = _observe_backup_api(client, source, raw_id, failures)
        # Follow the stopped-snapshot runbook: fresh backup after service
        # shutdown, then freeze one whole configured private runtime generation.
        storage = init_storage(source)
        try:
            backup = storage.create_backup(label="r10-j08-stopped")
            backup_name = str(backup.get("database") or "")
            verified = storage.verify_backup(backup_name)
        finally:
            storage.close()
        if not backup_name or verified.get("ok") is not True:
            failures.append("backup_verify_failed")
        with sqlite3.connect((source.backups_dir / backup_name).as_uri() + "?mode=ro", uri=True) as database:
            row = database.execute("SELECT raw_content FROM raw_objects WHERE id=?", (raw_id,)).fetchone()
            if row is None or _canary("R10_J08") not in str(row[0]):
                failures.append("backup_missing_source")
        inventory = _snapshot_files(source.home)
        snapshot = root / "snapshot"
        shutil.copytree(source.home, snapshot)
        if _snapshot_files(snapshot) != inventory:
            raise JourneyError("restore_snapshot_copy_mismatch")
        observed["snapshot_sha256"] = hashlib.sha256(
            json.dumps(inventory, sort_keys=True).encode()
        ).hexdigest()
        # The source pathname is unavailable for the entire restore and API
        # read. All inputs below come from the frozen snapshot, never runtime A.
        source.home.rename(root / "unavailable-source")
        restored = _settings_in_home(source, root / "runtime-b")
        shutil.copytree(snapshot, restored.home)
        # Start with no active database: copied file listings cannot mask a
        # missing backup or failed restore into the separate runtime.
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(restored.database_path) + suffix).unlink(missing_ok=True)
        storage = init_storage(restored)
        try:
            with ProcessLease(restored.state_dir / "backend.lock", protocol="friday.backend.v1"):
                result = storage.restore_backup(backup_name, safety_label="r10-j08-pre")
            if result.get("ok") is not True:
                failures.append("restore_separate_runtime_failed")
        finally:
            storage.close()
        with TestClient(create_app(restored)) as client:
            downloaded = client.get(
                f"/api/files/{raw_id or 'raw_missing_r10'}", headers=_owner_headers(restored)
            )
            digest = hashlib.sha256(downloaded.content).hexdigest() if downloaded.status_code == 200 else ""
            if digest != expected_digest:
                failures.append("restored_file_missing_or_changed")
            search = client.get(
                "/api/search", headers=_owner_headers(restored), params={"q": _canary("R10_J08")}
            )
            hits = search.json().get("results", []) if search.status_code == 200 else []
            if not any(
                hit.get("raw_object_id") == raw_id and _canary("R10_J08") in str(hit.get("content") or "")
                for hit in hits
            ):
                failures.append("restore_search_source_missing")
            observed.update(
                file_sha256=digest,
                download_status=downloaded.status_code,
                original_runtime_unavailable=not source.home.exists(),
            )
    return {
        "id": "R10-J08-BACKUP-RESTORE-ISOLATED",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "observed_safe": observed,
    }


def run_j09(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    secret_a = "jrc_r10_user_a_" + "1" * 8
    secret_b = "jrc_r10_user_b_" + "2" * 8
    headers_a = {"Authorization": f"Bearer {secret_a}"}
    headers_b = {"Authorization": f"Bearer {secret_b}"}
    with TestClient(app) as client:
        _issue_token(app.state.storage, "r10-user-a", "user", secret_a)
        _issue_token(app.state.storage, "r10-user-b", "user", secret_b)
        upload_a = _upload_bytes(client, headers_a, "a.txt", b"token-a-only R10J09A", "text/plain")
        upload_b = _upload_bytes(client, headers_b, "b.txt", b"token-b-only R10J09B", "text/plain")
        list_a = client.get("/api/files", headers=headers_a)
        list_b = client.get("/api/files", headers=headers_b)
        body_a = _json_object(list_a)
        body_b = _json_object(list_b)
    raw_a = str(upload_a["body"].get("raw_object_id") or "")
    raw_b = str(upload_b["body"].get("raw_object_id") or "")
    ids_a = _listed_raw_ids(body_a)
    ids_b = _listed_raw_ids(body_b)
    failures: list[str] = []
    if upload_a["status_code"] != 200 or not raw_a:
        failures.append("principal_a_upload_missing_handle")
    if upload_b["status_code"] != 200 or not raw_b:
        failures.append("principal_b_upload_missing_handle")
    if raw_a and raw_b and raw_a == raw_b:
        failures.append("principal_handles_not_distinct")
    if list_a.status_code != 200:
        failures.append("principal_a_listing_unavailable")
    if list_b.status_code != 200:
        failures.append("principal_b_listing_unavailable")
    if raw_a and raw_a not in ids_a:
        failures.append("principal_a_own_handle_missing")
    if raw_b and raw_b not in ids_b:
        failures.append("principal_b_own_handle_missing")
    if raw_b and raw_b in ids_a:
        failures.append("principal_b_handle_exposed_to_a")
    if raw_a and raw_a in ids_b:
        failures.append("principal_a_handle_exposed_to_b")
    blob_a = json.dumps(body_a, ensure_ascii=False)
    blob_b = json.dumps(body_b, ensure_ascii=False)
    if "R10J09B" in blob_a or "R10J09A" in blob_b:
        failures.append("foreign_canary_exposed")
    return {
        "id": "R10-J09-TWO-PRINCIPALS",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "expected_outcome": "pass",
        "observed_safe": {
            "upload_a": upload_a["status_code"],
            "upload_b": upload_b["status_code"],
            "listing_a": list_a.status_code,
            "listing_b": list_b.status_code,
        },
    }


def run_j10(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        body = _json_object(health)
    expected_secondary = {
        "schema": "friday.optional-secondary-health.v1",
        "role": "optional_advisory",
        "enabled": False,
        "configured": False,
        "mode": "disabled",
        "state": "disabled",
        "available": False,
    }
    secondary = body.get("secondary")
    failures: list[str] = []
    if health.status_code != 200:
        failures.append("health_unavailable")
    if body.get("status") != "ok":
        failures.append("health_status_not_ok")
    if getattr(settings, "secondary_llm_enabled", None) is not False:
        failures.append("secondary_setting_not_disabled")
    if getattr(settings, "secondary_llm_mode", None) != "disabled":
        failures.append("secondary_mode_not_disabled")
    if settings.secondary_llm_configured is not False:
        failures.append("secondary_configuration_not_disabled")
    if not isinstance(secondary, dict) or any(
        secondary.get(key) != value for key, value in expected_secondary.items()
    ):
        failures.append("secondary_health_not_exactly_disabled")
    return {
        "id": "R10-J10-SECONDARY-ABSENT",
        "status": "FAIL" if failures else "PASS",
        "failure_codes": failures,
        "expected_outcome": "pass",
        "observed_safe": {
            "health": health.status_code,
            "status_ok": body.get("status") == "ok",
            "secondary_sha256": hashlib.sha256(json.dumps(secondary, sort_keys=True).encode()).hexdigest(),
        },
    }


def run_empty_chat(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        before = client.get("/api/admin/backups", headers=headers)
        response = client.post("/api/chat", headers=headers, json={"message": ""})
        after = client.get("/api/admin/backups", headers=headers)
    observed = {
        "status_code": response.status_code,
        "body": response.text[:500],
        "effect": (after.json() or {}).get("count") != (before.json() or {}).get("count"),
        "collected": True,
    }
    expected = {"effect_forbidden": True, "expected_outcome": "refuse"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code < 400:
        verdict["failure_codes"] = [*verdict["failure_codes"], "empty_chat_not_refused"]
        verdict["status"] = "FAIL"
    return {"id": "R10-DIA-EMPTY-NO-ADMIN", **verdict}


def _passworded_zip_bytes() -> bytes:
    # Frozen ZipCrypto fixture; independent stdlib decryption checks it before
    # any product call. No host-tool fallback can substitute an ordinary ZIP.
    return base64.b64decode(
        "UEsDBAoACQAAAKOrJ128drBhFgAAAAoAAAAKAAAAc2VjcmV0LnR4dKEFSUD+ajKUox8dXTuR4/dPRudVpPJQSwcIvHawYRYAAAAKAAAAUEsBAh4DCgAJAAAAo6snXbx2sGEWAAAACgAAAAoAAAAAAAAAAQAAALSBAAAAAHNlY3JldC50eHRQSwUGAAAAAAEAAQA4AAAATgAAAAAA"
    )


def _verify_password_fixture(payload: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        if archive.namelist() != ["secret.txt"] or not archive.infolist()[0].flag_bits & 1:
            raise JourneyError("password_fixture_not_encrypted")
        for password in (None, b"wrong-r10-password"):
            try:
                archive.read("secret.txt", pwd=password)
            except RuntimeError:
                continue
            raise JourneyError("password_fixture_boundary_invalid")
        if archive.read("secret.txt", pwd=b"r10-secret") != b"hidden-r10":
            raise JourneyError("password_fixture_content_invalid")


def run_password_zip(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    payload = _passworded_zip_bytes()
    _verify_password_fixture(payload)
    # Freeze the refusal contract before observing the product.
    expected = {"status_code": 200, "effect_forbidden": True, "expected_outcome": "refuse"}
    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        before = _file_count(client, headers)
        response = client.post(
            "/api/files",
            headers=headers,
            files={"file": ("secret.zip", payload, "application/zip")},
            data={"source_ref": "r10:secret.zip"},
        )
        after = _file_count(client, headers)
        body = response.json() if response.content else {}
    observed = {"status_code": response.status_code, "effect": after != before, "collected": True}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if before < 0 or after < 0:
        verdict["failure_codes"].append("file_inventory_unavailable")
    if body.get("archive_password_required") is not True:
        verdict["failure_codes"].append("password_challenge_missing")
    if body.get("persisted") is not False or body.get("raw_object_id"):
        verdict["failure_codes"].append("locked_archive_persistence_claim")
    verdict["status"] = "FAIL" if verdict["failure_codes"] else "PASS"
    return {"id": "R10-FILE-UNSUPPORTED-ZIP", **verdict}


def run_missing_file(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        response = client.get("/api/files/raw_missing_r10_does_not_exist", headers=headers)
    observed = {
        "status_code": response.status_code,
        "body": response.text[:300],
        "effect": False,
        "collected": True,
    }
    expected = {"status_code": 404, "effect_forbidden": True, "expected_outcome": "refuse"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code not in {404, 422}:
        verdict["failure_codes"] = [*verdict["failure_codes"], "missing_file_not_closed"]
        verdict["status"] = "FAIL"
    else:
        verdict["status"] = "PASS"
        verdict["failure_codes"] = [
            code for code in verdict["failure_codes"] if code != "status_code_mismatch"
        ]
    return {"id": "R10-FILE-MISSING", **verdict}


def run_forged_auth(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/me", headers={"Authorization": "Bearer forged-r10-token"})
    observed = {
        "status_code": response.status_code,
        "body": response.text[:200],
        "collected": True,
        "effect": False,
    }
    expected = {"status_code": 401, "effect_forbidden": True, "expected_outcome": "refuse"}
    return {"id": "R10-AUTH-FORGED", **acceptance.evaluate_oracle(expected, observed)}


def run_guest_admin(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    secret = "jrc_r10_guest_admin"
    with TestClient(app) as client:
        _issue_token(app.state.storage, "r10-guest-admin", "user", secret)
        response = client.get("/api/admin/overview", headers={"Authorization": f"Bearer {secret}"})
    observed = {
        "status_code": response.status_code,
        "body": response.text[:200],
        "effect": response.status_code == 200,
        "collected": True,
    }
    expected = {"effect_forbidden": True, "expected_outcome": "refuse"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code not in {401, 403}:
        verdict["failure_codes"] = [*verdict["failure_codes"], "guest_admin_allowed"]
        verdict["status"] = "FAIL"
    return {"id": "R10-AUTH-GUEST-ADMIN", **verdict}


RUNNERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "R10-J01-UPLOAD-SEARCH-RESTART": run_j01,
    "R10-J02-CROSS-CONV-PRIVACY": run_j02,
    "R10-J03-TABLE-VERSIONS": run_j03,
    "R10-J04-PRIVATE-URL": run_j04,
    "R10-J05-ENGINEER-REFUSE-UNSIGNED": run_j05,
    "R10-J06-CODING-EXECUTE-CLOSED": run_j06,
    "R10-J07-OBSIDIAN-DISABLED": run_j07,
    "R10-J08-BACKUP-RESTORE-ISOLATED": run_j08,
    "R10-J09-TWO-PRINCIPALS": run_j09,
    "R10-J10-SECONDARY-ABSENT": run_j10,
    "R10-DIA-EMPTY-NO-ADMIN": run_empty_chat,
    "R10-FILE-UNSUPPORTED-ZIP": run_password_zip,
    "R10-FILE-MISSING": run_missing_file,
    "R10-AUTH-FORGED": run_forged_auth,
    "R10-AUTH-GUEST-ADMIN": run_guest_admin,
}


def deterministic_case_ids() -> tuple[str, ...]:
    return tuple(RUNNERS)


_SAFE_HARNESS_ERROR_CODES = frozenset(
    {
        "deterministic_timeout_invalid",
        "deterministic_root_not_private",
        "deterministic_gate_context_invalid",
        "deterministic_gate_runtime_mismatch",
        "deterministic_settings_depth",
        "deterministic_settings_type",
        "deterministic_settings_shape",
        "deterministic_request_oversized",
        "deterministic_request_identity",
    }
)


def run_deterministic_suite(
    settings: Any, case_ids: Sequence[str] | None = None, *, gate_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    matrix_cases = acceptance.load_matrix()["cases"]
    delegated = (
        [
            case
            for case in matrix_cases
            if case["executable"]
            and case["layer"] == "deterministic"
            and case["execution_driver"] == "canonical-pytest"
        ]
        if case_ids is None
        else []
    )
    handlers = acceptance.registered_case_handlers()
    for case in delegated:
        registered = handlers.get(case["id"])
        if (
            registered is None
            or not callable(registered[0])
            or registered[3] != "canonical-pytest"
            or list(registered[2]) != case["node_ids"]
            or registered[1] != case["layer"]
            or not acceptance._pytest_source_bindings_exist(registered[2])
        ):
            raise acceptance.AcceptanceError("executable_handler_missing")
    selected = (
        [
            case["id"]
            for case in matrix_cases
            if case["executable"]
            and case["layer"] == "deterministic"
            and case["execution_driver"] == "journey"
        ]
        if case_ids is None
        else list(case_ids)
    )
    if not selected:
        raise acceptance.AcceptanceError("zero_collected_cases")
    if len(selected) != len(set(selected)):
        raise acceptance.AcceptanceError("duplicate_selected_cases")
    if any(case_id in acceptance.PYTEST_CASE_BINDINGS for case_id in selected):
        raise acceptance.AcceptanceError("case_requires_canonical_gate")
    if any(not callable(RUNNERS.get(case_id)) for case_id in selected):
        raise acceptance.AcceptanceError("executable_handler_missing")
    from tools import release_1_0_deterministic as deterministic

    specs = {case["id"]: case for case in matrix_cases}
    if any(case_id not in specs for case_id in selected):
        raise acceptance.AcceptanceError("executable_case_missing")
    results = []
    fenced = False
    attempted = 0
    for case_id in selected:
        if fenced:
            results.append(
                {"id": case_id, "status": "NOT_RUN", "failure_codes": [], "reason": "prior_cleanup_uncertain"}
            )
            continue
        attempted += 1
        try:
            # Evidence survives the subprocess; only positively audited HOME
            # and environment paths can be removed by the lifecycle owner.
            # Keep retained evidence and uncertain workers outside pytest/gate
            # temporary trees: their outer cleanup must not delete these paths.
            scratch = Path(tempfile.mkdtemp(prefix="friday-r10-case-", dir="/var/tmp"))
            case_settings = _settings_in_home(settings, scratch / "home")
            options = {"gate_context": gate_context} if gate_context is not None else {}
            result = deterministic.run_case(case_id, case_settings, specs[case_id]["timeout_s"], **options)
            if result.get("id") != case_id:
                raise JourneyError("case_result_identity_mismatch")
            results.append(result)
            fenced = result.get("cleanup_clear") is not True
        except Exception as exc:
            observed_safe = {"error_type": type(exc).__name__}
            # Only literal internal codes may leave the private harness. File,
            # settings and transport exception text can contain owner data.
            if (
                type(exc) is ValueError
                and len(exc.args) == 1
                and type(exc.args[0]) is str
                and exc.args[0] in _SAFE_HARNESS_ERROR_CODES
            ):
                observed_safe["error_code"] = exc.args[0]
            results.append(
                {
                    "id": case_id,
                    "status": "FAIL",
                    "failure_codes": ["harness_exception"],
                    "observed_safe": observed_safe,
                }
            )
            fenced = True
    failed = [row for row in results if row.get("status") == "FAIL"]
    results.extend(
        {
            "id": case["id"],
            "status": "NOT_RUN",
            "failure_codes": [],
            "reason": "requires_canonical_gate_execution",
            "execution_driver": "canonical-pytest",
        }
        for case in delegated
    )
    return {
        "schema": SCHEMA,
        "layer": "deterministic",
        "scope": "additional journey execution; canonical pytest cases require their gate receipt",
        "planned": len(selected) + len(delegated),
        "attempted": attempted,
        "executed": sum(row.get("execution_observed") is True for row in results),
        "pass": sum(row.get("status") == "PASS" for row in results),
        "fail": len(failed),
        "blocked": 0,
        "not_run": sum(row.get("status") == "NOT_RUN" for row in results),
        "go_emitted": False,
        "results": results,
        "status": "FAIL" if failed else "INCOMPLETE" if delegated else "PASS",
    }


def live_inventory() -> dict[str, Any]:
    matrix = acceptance.load_matrix()
    live = [case for case in matrix["cases"] if case.get("layer") in {"isolated-live", "deployment-device"}]
    return {
        "schema": SCHEMA,
        "live_cases": [
            {
                "id": case["id"],
                "executable": case.get("executable"),
                "release_required": case.get("release_required"),
                "blocked_reason": case.get("blocked_reason"),
                "layer": case.get("layer"),
            }
            for case in live
        ],
        "note": "isolated-live requires exclusive model slot and FRIDAY_ENV_FILE; production home is forbidden",
        "go_emitted": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Additional R10 journeys")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit-only", action="store_true")
    mode.add_argument("--run-live", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--candidate-sha")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--base-sha")
    parser.add_argument("--context-out", type=Path)
    parser.add_argument("--case-id", action="append")
    args = parser.parse_args(argv)
    context_group = (args.run_id, args.base_sha, args.context_out)
    if (
        any(value is not None for value in context_group)
        and not all(value is not None for value in context_group)
    ) or (
        (any(value is not None for value in context_group) or args.case_id is not None) and not args.run_live
    ):
        parser.error("native context requires --run-live and all of --run-id, --base-sha, --context-out")
    if args.audit_only:
        print(json.dumps(live_inventory(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.run_live:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from tools import release_1_0_native as native

        if not (args.env_file and args.candidate_sha and args.evidence_dir):
            report = {
                "schema": SCHEMA,
                "status": "NOT_RUN",
                "reason": "native_inputs_missing",
                "go_emitted": False,
            }
            code = 5
        else:
            try:
                explicit = {}
                if args.context_out is not None:
                    explicit.update(run_id=args.run_id, base_sha=args.base_sha, context_path=args.context_out)
                if args.case_id is not None:
                    explicit["case_ids"] = args.case_id
                report = native.run_native(
                    env_file=args.env_file,
                    candidate_sha=args.candidate_sha,
                    run_dir=args.evidence_dir,
                    **explicit,
                )
                code = 0 if report["status"] == "PASS" else 4
                if (report.get("root_failure") or {}).get("signal_number") in {2, 15}:
                    code = 128 + report["root_failure"]["signal_number"]
            except BaseException as exc:
                lifecycle = native._dependencies()[0]
                if isinstance(exc, lifecycle.ControllerSignal):
                    reason = "native_controller_interrupted"
                    code = 128 + exc.signal_number
                elif isinstance(exc, Exception):
                    reason = native._failure_code(exc)
                    code = 5
                else:
                    raise
                report = {"schema": SCHEMA, "status": "NOT_RUN", "reason": reason, "go_emitted": False}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return code
    parser.error("choose --audit-only (or pytest for deterministic cases)")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
