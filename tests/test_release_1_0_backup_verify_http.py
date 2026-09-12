"""Real backup-verification HTTP contract; database-only, no restore/live credit."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage.models import RawObject, new_id
from tests.test_api_tokens import _issue
from tests.test_release_1_0_cli_backup_export_paths import SCOPE, _same, _verify_expected

PEOPLE = ("backup089-http-a", "backup089-http-b")
STAMP = "2026-01-02T03:04:05+00:00"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw_rows(store):
    return [dict(row) for row in store.execute("SELECT * FROM raw_objects ORDER BY id").fetchall()]


def _backup_files(root):
    return {str(p.relative_to(root)): _sha(p) for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def _verify_audits(store):
    return [
        dict(row)
        for row in store.execute(
            "SELECT * FROM audit_log WHERE action='admin.backup.verify' ORDER BY rowid"
        ).fetchall()
    ]


def _url(name):
    return f"/api/admin/backups/{quote(name, safe='')}/verify"


@pytest.fixture
def backup_http(settings):
    current = replace(settings, shared_archive=False, backup_mirror_dir=None, backup_encryption_key_file=None)
    assert not current.llm_enabled and not current.workers_enabled
    with TestClient(create_app(current), raise_server_exceptions=False) as client:
        store = client.app.state.storage
        headers = {"owner": {"Authorization": f"Bearer {current.api_token}"}, "anonymous": {}}
        for role in ("admin", "user"):
            secret = f"jrc_backup089_{role}_synthetic_secret"
            _issue(store, f"backup089-http-{role}", role, secret)
            headers[role] = {"Authorization": f"Bearer {secret}"}
        for person in PEOPLE:
            store.ensure_user(person, preset_key="user")
            store.store_raw_object(
                RawObject(
                    id=new_id("raw"),
                    user_id=person,
                    source="upload",
                    source_ref="backup-http-fixture",
                    raw_content=f"PRIVATE_BACKUP_BODY_{person}",
                    content_type="text",
                    received_at=STAMP,
                    created_at=STAMP,
                )
            )
        backup = store.create_backup(label="http-verification")
        path = Path(backup["path"])
        assert path.parent == current.backups_dir and path.is_file()
        manifest = json.loads(path.with_suffix(".manifest.json").read_bytes())
        _same(manifest["scope"], SCOPE)
        with closing(sqlite3.connect(path)) as copied:
            rows = copied.execute("SELECT user_id,raw_content FROM raw_objects ORDER BY user_id").fetchall()
        assert rows == [(person, f"PRIVATE_BACKUP_BODY_{person}") for person in PEOPLE]
        yield client, store, current, path, headers


def _assert_verify_audit(store, before, response, actor, path, expected):
    rows = _verify_audits(store)
    _same(rows[:-1], before)
    assert len(rows) == len(before) + 1
    row = rows[-1]
    assert row["user_id"] == actor and row["target_type"] == "backup"
    assert re.fullmatch(r"backup:ref:[0-9a-f]{24}", row["target_id"])
    assert row["request_id"] == response.headers["x-request-id"]
    assert row["before_json"] is None
    payload = json.loads(row["after_json"])
    # Backup receipt fields are private audit input. Only size_bytes belongs to
    # the closed durable schema; derive the summary from the pre-call oracle.
    private = [value for key, value in expected.items() if key != "size_bytes"]
    _same(
        payload,
        {
            "size_bytes": path.stat().st_size,
            "private_fields_count": len(private),
            "private_chars": sum(len(value) for value in private if isinstance(value, str)),
        },
    )
    text = json.dumps(row, ensure_ascii=False)
    assert path.name not in text and str(path.parent) not in text
    assert "PRIVATE_BACKUP_BODY_" not in text and "synthetic_secret" not in text


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_http_verify_real_database_exact_receipt_private_audit_and_repeat(backup_http, role):
    client, store, settings, path, headers = backup_http
    expected = _verify_expected(path)
    files, rows = _backup_files(settings.backups_dir), _raw_rows(store)
    for _ in range(2):
        before = _verify_audits(store)
        response = client.post(_url(path.name), headers=headers[role])
        assert response.status_code == 200, response.text
        _same(response.json(), {"verification": expected})
        actor = LEGACY_OWNER_USER_ID if role == "owner" else "backup089-http-admin"
        _assert_verify_audit(store, before, response, actor, path, expected)
        _same(_raw_rows(store), rows)
        _same(_backup_files(settings.backups_dir), files)
        assert "PRIVATE_BACKUP_BODY_" not in response.text and "synthetic_secret" not in response.text


@pytest.mark.parametrize("damage", ["wrong-digest", "missing-manifest", "invalid-schema"])
def test_http_verify_real_corruption_is_false_and_preserves_copies(backup_http, damage):
    client, store, settings, path, headers = backup_http
    expected = _verify_expected(path)
    manifest_path = path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_bytes())
    if damage == "wrong-digest":
        manifest["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        expected.update(hash_matches_manifest=False, ok=False)
    elif damage == "missing-manifest":
        manifest_path.unlink()
        expected.update(
            manifest_present=False,
            hash_matches_manifest=None,
            manifest_database_matches=None,
            manifest_size_matches=None,
            manifest_schema_supported=None,
            manifest_schema_matches_database=None,
            manifest_scope_matches=None,
            manifest_error="Manifest is missing",
            ok=False,
        )
    else:
        with closing(sqlite3.connect(path)) as copied:
            copied.execute("UPDATE schema_meta SET value='not-an-integer' WHERE key='schema_version'")
            copied.commit()
        assert _sha(path) != manifest["sha256"], "fixture corruption must reach the main backup file"
        expected.update(
            size_bytes=path.stat().st_size,
            sha256=_sha(path),
            database_schema_version=None,
            database_schema_supported=False,
            database_error="Database schema_version marker is invalid",
            hash_matches_manifest=False,
            manifest_size_matches=manifest["size_bytes"] == path.stat().st_size,
            manifest_schema_matches_database=False,
            manifest_error="schema_version does not match the database",
            ok=False,
        )
    files, rows, audits = _backup_files(settings.backups_dir), _raw_rows(store), _verify_audits(store)
    response = client.post(_url(path.name), headers=headers["owner"])
    assert response.status_code == 200, response.text
    _same(response.json(), {"verification": expected})
    _assert_verify_audit(store, audits, response, LEGACY_OWNER_USER_ID, path, expected)
    _same(_backup_files(settings.backups_dir), files)
    _same(_raw_rows(store), rows)


@pytest.mark.parametrize("which", ["missing", "symlink", "backslash", "wrong-suffix"])
def test_http_verify_bad_backup_refuses_404_without_success_audit(backup_http, which):
    client, store, settings, path, headers = backup_http
    filename = "missing.sqlite3"
    if which == "symlink":
        filename = "linked.sqlite3"
        (settings.backups_dir / filename).symlink_to(path)
    elif which == "backslash":
        filename = "subdir\\" + path.name
    elif which == "wrong-suffix":
        filename = "copy.txt"
        (settings.backups_dir / filename).write_bytes(path.read_bytes())
    files, rows, audits = _backup_files(settings.backups_dir), _raw_rows(store), _verify_audits(store)
    response = client.post(_url(filename), headers=headers["owner"])
    assert response.status_code == 404, response.text
    body = response.json()
    assert body.get("detail") == "Резервная копия не найдена"
    assert "verification" not in body and "PRIVATE_BACKUP_BODY_" not in response.text
    _same(_verify_audits(store), audits)
    _same(_backup_files(settings.backups_dir), files)
    _same(_raw_rows(store), rows)


@pytest.mark.parametrize("role,status", [("anonymous", 401), ("user", 403)])
def test_http_verify_requires_backup_capability_before_provider(backup_http, monkeypatch, role, status):
    client, store, settings, path, headers = backup_http
    entered = []

    def forbidden(*args, **kwargs):
        entered.append((args, kwargs))
        raise AssertionError("unauthorized request entered backup provider")

    monkeypatch.setattr(store, "verify_backup", forbidden)
    files, rows, audits = _backup_files(settings.backups_dir), _raw_rows(store), _verify_audits(store)
    response = client.post(_url(path.name), headers=headers[role])
    assert response.status_code == status, response.text
    assert entered == [] and "verification" not in response.json()
    assert path.name not in response.text and "PRIVATE_BACKUP_BODY_" not in response.text
    _same(_verify_audits(store), audits)
    _same(_backup_files(settings.backups_dir), files)
    _same(_raw_rows(store), rows)
