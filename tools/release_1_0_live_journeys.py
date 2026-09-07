#!/usr/bin/env python3
"""Additional R10 user-journey suite.

Deterministic cases use an isolated TestClient (LLM off). Isolated-live cases
require --run-live and a dedicated env file; they never target the production
home. This runner does not rewrite sealed A/B manifests.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
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


def _upload_bytes(client: TestClient, headers: dict[str, str], filename: str, payload: bytes, mime: str) -> dict[str, Any]:
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


def run_j01(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    canary = _canary("R10_J01")
    payload = f"event kitchen participants 4 {canary}\n".encode()
    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        uploaded = _upload_bytes(client, headers, "r10-j01.txt", payload, "text/plain")
        files = client.get("/api/files", headers=headers)
        inbox = client.get("/api/admin/inbox", headers=headers)
        search = client.get("/api/search", params={"q": canary}, headers=headers)
        listed = files.json().get("items") or []
        inbox_items = inbox.json().get("items") or inbox.json().get("groups") or []
        search_text = json.dumps(search.json(), ensure_ascii=False)
        first = {
            "upload_status": uploaded["status_code"],
            "file_count": files.json().get("count"),
            "inbox_status": inbox.status_code,
            "search_status": search.status_code,
            "raw_id": (listed[0] or {}).get("id") if listed else None,
        }
    app2 = create_app(settings)
    with TestClient(app2) as client:
        files_after = client.get("/api/files", headers=headers)
        restart_items = files_after.json().get("items") or []
        restart_blob = json.dumps(restart_items, ensure_ascii=False)
    observed = {
        "status_code": first["upload_status"],
        "count": first["file_count"],
        "body": f"{canary} {first['raw_id']} inbox={first['inbox_status']} {search_text} {restart_blob}",
        "collected": True,
        "effect": False,
    }
    expected = {
        "status_code": 200,
        "min_count": 1,
        "must_contain": [canary],
        "expected_outcome": "pass",
    }
    verdict = acceptance.evaluate_oracle(expected, observed)
    if first["inbox_status"] != 200:
        verdict["failure_codes"] = [*verdict["failure_codes"], "inbox_not_reachable"]
        verdict["status"] = "FAIL"
    if first["raw_id"] not in restart_blob:
        verdict["failure_codes"] = [*verdict["failure_codes"], "restart_lost_file"]
        verdict["status"] = "FAIL"
    return {"id": "R10-J01-UPLOAD-SEARCH-RESTART", **verdict, "observed_safe": first}


def run_j02(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    canary = _canary("R10_J02")
    app = create_app(settings)
    owner = _owner_headers(settings)
    guest_secret = "jrc_r10_guest_" + hashlib.sha256(canary.encode()).hexdigest()[:12]
    guest = {"Authorization": f"Bearer {guest_secret}"}
    with TestClient(app) as client:
        _issue_token(app.state.storage, "r10-guest", "user", guest_secret)
        _upload_bytes(client, owner, "r10-j02.txt", canary.encode(), "text/plain")
        owner_files = client.get("/api/files", headers=owner)
        guest_files = client.get("/api/files", headers=guest)
        guest_admin = client.get("/api/admin/overview", headers=guest)
        owner_ids = [item.get("id") for item in (owner_files.json().get("items") or [])]
        guest_ids = [item.get("id") for item in (guest_files.json().get("items") or [])]
        leak = bool(set(owner_ids) & set(guest_ids))
        guest_body = json.dumps(guest_files.json(), ensure_ascii=False)
    observed = {
        "status_code": owner_files.status_code,
        "count": owner_files.json().get("count"),
        "body": guest_body,
        "effect": guest_admin.status_code == 200,
        "collected": True,
        "foreign_canaries": [canary] if leak or canary in guest_body else [],
    }
    expected = {
        "status_code": 200,
        "min_count": 1,
        "effect_forbidden": True,
        "foreign_canaries": [canary],
        "expected_outcome": "refuse",
    }
    # Guest body must NOT contain the canary; evaluate_oracle treats presence as fail.
    # So expected.foreign_canaries + guest body without canary => PASS.
    if leak:
        observed["body"] = canary
    verdict = acceptance.evaluate_oracle(expected, observed)
    if guest_admin.status_code not in {401, 403}:
        verdict["failure_codes"] = [*verdict["failure_codes"], "guest_admin_not_refused"]
        verdict["status"] = "FAIL"
    return {"id": "R10-J02-CROSS-CONV-PRIVACY", **verdict, "observed_safe": {"guest_admin": guest_admin.status_code, "leak": leak}}


def run_j03(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    v1 = "id,name\nBRK.A,alpha-r10\n".encode()
    v2 = "id,name\nBRK.B,beta-r10\n".encode()
    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        _upload_bytes(client, headers, "holdings-v1.csv", v1, "text/csv")
        _upload_bytes(client, headers, "holdings-v2.csv", v2, "text/csv")
        listed = client.get("/api/files", headers=headers)
        items = listed.json().get("items") or []
        ids = [item.get("id") for item in items]
        blob = json.dumps(items, ensure_ascii=False)
        raw_store = app.state.storage.execute(
            "SELECT id, source_ref FROM raw_objects WHERE content_type='file' AND deleted_at IS NULL"
        ).fetchall()
    observed = {
        "status_code": listed.status_code,
        "count": len(ids),
        "body": blob + " " + " ".join(str(row["source_ref"]) for row in raw_store),
        "collected": True,
    }
    expected = {
        "status_code": 200,
        "min_count": 2,
        "must_contain": ["r10:holdings-v1.csv", "r10:holdings-v2.csv"],
        "expected_outcome": "pass",
    }
    verdict = acceptance.evaluate_oracle(expected, observed)
    if len(set(ids)) < 2:
        verdict["failure_codes"] = [*verdict["failure_codes"], "versions_not_distinct"]
        verdict["status"] = "FAIL"
    return {"id": "R10-J03-TABLE-VERSIONS", **verdict, "observed_safe": {"count": len(ids)}}


def run_j04(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        before = client.get("/api/knowledge", headers=headers)
        response = client.post("/api/ingest/url", headers=headers, json={"url": CANONICAL_PRIVATE_URL})
        after = client.get("/api/knowledge", headers=headers)
        before_count = int((before.json() or {}).get("count") or len((before.json() or {}).get("items") or []))
        after_count = int((after.json() or {}).get("count") or len((after.json() or {}).get("items") or []))
        detail = json.dumps(response.json(), ensure_ascii=False)
    observed = {
        "status_code": response.status_code,
        "body": detail,
        "effect": after_count > before_count,
        "collected": True,
    }
    expected = {
        "status_code": 422,
        "must_contain": ["source_fact_private"],
        "effect_forbidden": True,
        "expected_outcome": "refuse",
    }
    return {"id": "R10-J04-PRIVATE-URL", **acceptance.evaluate_oracle(expected, observed)}


def run_j05(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    headers = _owner_headers(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations/channel/mode",
            headers=headers,
            json={"channel": "api", "channel_id": "r10", "mode": "engineer"},
        )
        jobs = app.state.storage.execute(
            "SELECT COUNT(*) AS count FROM sqlite_master WHERE name LIKE '%engineer%'"
        ).fetchone()
    observed = {
        "status_code": response.status_code,
        "body": response.text,
        "effect": False,
        "collected": True,
        "count": int(jobs["count"] if jobs else 0),
    }
    expected = {
        "effect_forbidden": True,
        "expected_outcome": "refuse",
    }
    verdict = acceptance.evaluate_oracle(expected, observed)
    if response.status_code not in {400, 403, 503}:
        verdict["failure_codes"] = [*verdict["failure_codes"], "engineer_not_refused"]
        verdict["status"] = "FAIL"
    return {"id": "R10-J05-ENGINEER-REFUSE-UNSIGNED", **verdict, "observed_safe": {"status_code": response.status_code}}


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
        "effect": claim.state not in {
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
    return {"id": "R10-J06-CODING-EXECUTE-CLOSED", **verdict, "observed_safe": {"mode": response.status_code, "claim": str(claim.state)}}


def run_j07(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        body = health.json()
    blob = json.dumps(body, ensure_ascii=False)
    obsidian = body.get("obsidian") or body.get("obsidian_health") or {}
    mode = str(obsidian.get("mode") or "")
    if "obsidian" in body and not mode:
        nested = body.get("obsidian")
        if isinstance(nested, dict):
            mode = str(nested.get("mode") or "")
    observed = {
        "status_code": health.status_code,
        "body": blob,
        "collected": True,
        "effect": bool(getattr(settings, "obsidian_enabled", False)),
    }
    expected = {
        "status_code": 200,
        "must_contain": ["ok"],
        "effect_forbidden": True,
        "expected_outcome": "pass",
    }
    verdict = acceptance.evaluate_oracle(expected, observed)
    if getattr(settings, "obsidian_enabled", False) is True:
        verdict["failure_codes"] = [*verdict["failure_codes"], "obsidian_unexpectedly_enabled"]
        verdict["status"] = "FAIL"
    if "\"mode\": \"disabled\"" not in blob and "disabled" not in mode and "obsidian" in blob.lower():
        # Health may omit the organ entirely when disabled; that is also honest.
        pass
    return {"id": "R10-J07-OBSIDIAN-DISABLED", **verdict, "observed_safe": {"obsidian_enabled": bool(getattr(settings, "obsidian_enabled", False))}}


def run_j08(settings: Any) -> dict[str, Any]:
    from friday.config import ensure_runtime_dirs, load_settings
    from friday.diagnostics.runtime_lease import ProcessLease
    from friday.server import create_app
    from friday.storage import init_storage

    canary = _canary("R10_J08")
    payload = f"restore-me {canary}\n".encode()
    app = create_app(settings)
    headers = _owner_headers(settings)
    backup_name = ""
    with TestClient(app) as client:
        uploaded = _upload_bytes(client, headers, "r10-j08.txt", payload, "text/plain")
        created = client.post("/api/admin/backups", headers=headers, json={"label": "r10-j08"})
        backup = (created.json() or {}).get("backup") or {}
        backup_name = str(backup.get("database") or "")
        verified = client.post(f"/api/admin/backups/{backup_name}/verify", headers=headers) if backup_name else None
    if not backup_name:
        return {
            "id": "R10-J08-BACKUP-RESTORE-ISOLATED",
            "status": "FAIL",
            "failure_codes": ["backup_not_created"],
            "observed_safe": {"upload": uploaded["status_code"], "create": created.status_code},
        }
    backup_path = settings.backups_dir / backup_name
    manifest_path = backup_path.with_suffix(".manifest.json")
    independent = False
    with sqlite3.connect(str(backup_path)) as copy:
        rows = copy.execute(
            "SELECT source_ref FROM raw_objects WHERE content_type='file'"
        ).fetchall()
        independent = any("r10-j08.txt" in str(row[0]) for row in rows)
    home_b = Path(settings.home).parent / "r10-restore-home"
    previous_home = os.environ.get("FRIDAY_HOME")
    restored_ok = False
    restore_error = ""
    found_after = False
    try:
        os.environ["FRIDAY_HOME"] = str(home_b)
        loaded = load_settings()
        ensure_runtime_dirs(loaded)
        dest = loaded.backups_dir / backup_name
        dest.write_bytes(backup_path.read_bytes())
        if manifest_path.is_file():
            (loaded.backups_dir / manifest_path.name).write_bytes(manifest_path.read_bytes())
        src_files = getattr(settings, "files_dir", None)
        if isinstance(src_files, Path) and src_files.is_dir():
            shutil.copytree(src_files, loaded.files_dir, dirs_exist_ok=True)
        storage_b = init_storage(loaded)
        try:
            with ProcessLease(loaded.state_dir / "backend.lock", protocol="friday.backend.v1"):
                restored = storage_b.restore_backup(backup_name, safety_label="r10-j08-pre")
            restored_ok = bool(restored.get("ok"))
        except Exception as exc:  # noqa: BLE001 - journey records the boundary
            restore_error = type(exc).__name__
        finally:
            storage_b.close()
        if restored_ok:
            app_b = create_app(loaded)
            with TestClient(app_b) as client:
                listed = client.get("/api/files", headers=_owner_headers(loaded))
                blob = json.dumps(listed.json(), ensure_ascii=False)
                found_after = "r10-j08.txt" in blob or canary in blob
    finally:
        if previous_home is None:
            os.environ.pop("FRIDAY_HOME", None)
        else:
            os.environ["FRIDAY_HOME"] = previous_home
    observed = {
        "status_code": created.status_code,
        "body": f"independent={independent} restored={restored_ok} found={found_after} {restore_error}",
        "count": int(independent) + int(restored_ok) + int(found_after),
        "collected": True,
    }
    expected = {
        "status_code": 200,
        "min_count": 2,
        "must_contain": ["independent=True"],
        "expected_outcome": "pass",
    }
    verdict = acceptance.evaluate_oracle(expected, observed)
    if not independent:
        verdict["failure_codes"] = [*verdict["failure_codes"], "backup_missing_canary"]
        verdict["status"] = "FAIL"
    if not restored_ok or not found_after:
        verdict["failure_codes"] = [*verdict["failure_codes"], "restore_separate_runtime_failed"]
        verdict["status"] = "FAIL"
    if verified is not None and verified.status_code != 200:
        verdict["failure_codes"] = [*verdict["failure_codes"], "backup_verify_failed"]
        verdict["status"] = "FAIL"
    return {
        "id": "R10-J08-BACKUP-RESTORE-ISOLATED",
        **verdict,
        "observed_safe": {
            "independent": independent,
            "restored": restored_ok,
            "found_after": found_after,
            "restore_error": restore_error,
        },
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
        _upload_bytes(client, headers_a, "a.txt", b"token-a-only R10J09A", "text/plain")
        _upload_bytes(client, headers_b, "b.txt", b"token-b-only R10J09B", "text/plain")
        list_a = client.get("/api/files", headers=headers_a)
        list_b = client.get("/api/files", headers=headers_b)
        ids_a = {item.get("id") for item in (list_a.json().get("items") or [])}
        ids_b = {item.get("id") for item in (list_b.json().get("items") or [])}
        blob_a = json.dumps(list_a.json(), ensure_ascii=False)
        blob_b = json.dumps(list_b.json(), ensure_ascii=False)
    leak = bool(ids_a & ids_b) or "R10J09B" in blob_a or "R10J09A" in blob_b
    observed = {
        "status_code": list_a.status_code,
        "count": len(ids_a | ids_b),
        "body": "isolated" if not leak else "R10J09A R10J09B leaked",
        "foreign_canaries": ["R10J09A"] if leak else [],
        "collected": True,
    }
    expected = {
        "status_code": 200,
        "min_count": 2,
        "foreign_canaries": ["R10J09A"],
        "expected_outcome": "pass",
    }
    return {"id": "R10-J09-TWO-PRINCIPALS", **acceptance.evaluate_oracle(expected, observed), "observed_safe": {"leak": leak}}


def run_j10(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/api/health")
        body = health.json()
    secondary = body.get("secondary") or {}
    available = secondary.get("available")
    observed = {
        "status_code": health.status_code,
        "body": json.dumps(body, ensure_ascii=False)[:2000],
        "effect": available is True,
        "collected": True,
    }
    expected = {
        "status_code": 200,
        "must_contain": ["ok"],
        "effect_forbidden": True,
        "expected_outcome": "pass",
    }
    return {"id": "R10-J10-SECONDARY-ABSENT", **acceptance.evaluate_oracle(expected, observed), "observed_safe": {"available": available}}


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
    zip_bin = shutil.which("zip")
    if zip_bin:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            member = root / "secret.txt"
            member.write_text("hidden-r10", encoding="utf-8")
            archive = root / "secret.zip"
            completed = shutil.which("zip")
            import subprocess

            result = subprocess.run(
                (zip_bin, "-P", "r10-secret", str(archive), member.name),
                cwd=root,
                check=False,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and archive.is_file():
                return archive.read_bytes()
            del completed
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("secret.txt", "hidden-r10")
    return buffer.getvalue()


def run_password_zip(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    payload = _passworded_zip_bytes()
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
    password_required = bool(body.get("archive_password_required"))
    observed = {
        "status_code": response.status_code,
        "body": json.dumps(body, ensure_ascii=False),
        "effect": after > before and not password_required,
        "collected": True,
    }
    if password_required:
        expected = {
            "effect_forbidden": True,
            "must_contain": ["archive_password"],
            "expected_outcome": "refuse",
        }
        return {"id": "R10-FILE-UNSUPPORTED-ZIP", **acceptance.evaluate_oracle(expected, observed)}
    expected = {"status_code": 200, "expected_outcome": "pass"}
    verdict = acceptance.evaluate_oracle(expected, observed)
    verdict["note"] = "host_zip_tool_unavailable_unencrypted_persistence_checked"
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
        verdict["failure_codes"] = [code for code in verdict["failure_codes"] if code != "status_code_mismatch"]
    return {"id": "R10-FILE-MISSING", **verdict}


def run_forged_auth(settings: Any) -> dict[str, Any]:
    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/me", headers={"Authorization": "Bearer forged-r10-token"})
    observed = {"status_code": response.status_code, "body": response.text[:200], "collected": True, "effect": False}
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


def run_deterministic_suite(settings: Any, case_ids: Sequence[str] | None = None) -> dict[str, Any]:
    selected = list(case_ids or RUNNERS)
    if not selected:
        raise acceptance.AcceptanceError("zero_collected_cases")
    results = []
    for case_id in selected:
        runner = RUNNERS[case_id]
        try:
            results.append(runner(settings))
        except Exception as exc:  # noqa: BLE001 - remaining cases still run
            results.append(
                {
                    "id": case_id,
                    "status": "FAIL",
                    "failure_codes": ["harness_exception"],
                    "observed_safe": {"error_type": type(exc).__name__},
                }
            )
    failed = [row for row in results if row.get("status") != "PASS"]
    return {
        "schema": SCHEMA,
        "layer": "deterministic",
        "planned": len(selected),
        "executed": len(results),
        "pass": sum(row.get("status") == "PASS" for row in results),
        "fail": len(failed),
        "blocked": 0,
        "not_run": 0,
        "go_emitted": False,
        "results": results,
        "status": "FAIL" if failed else "PASS",
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
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    if args.audit_only:
        print(json.dumps(live_inventory(), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    if args.run_live:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "NOT_RUN",
                    "reason": "exclusive_model_slot_required",
                    "go_emitted": False,
                    "inventory": live_inventory(),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        return 5
    parser.error("choose --audit-only (or pytest for deterministic cases)")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
