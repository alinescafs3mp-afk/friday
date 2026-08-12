from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from friday.audit_privacy import sanitize_audit_action
from tools.backfill_file_uploader_provenance import (
    AUDIT_ACTION,
    CLAIM_SCHEMA,
    CLAIM_SCOPE,
    ContractError,
    apply_plan,
    audit_plan,
)


def _metadata(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _seed_database(settings: Any) -> tuple[Path, str, str]:
    from friday.storage import init_storage

    path = settings.database_path
    tenant = "shared-tenant"
    owner = "owner-person"
    storage = init_storage(settings)
    now = "2026-01-01T00:00:00Z"

    def raw(raw_id: str, metadata_json: str, *, source_ref: str = "sha256:fixture") -> tuple[object, ...]:
        body = f"body-{raw_id}"
        return (
            raw_id,
            tenant,
            "upload",
            source_ref if source_ref != "sha256:fixture" else f"sha256:{raw_id}",
            body,
            "file",
            metadata_json,
            hashlib.sha256(body.encode()).hexdigest(),
            1,
            now,
            now,
            None,
        )

    with storage.transaction() as db:
        db.executemany(
            """INSERT INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                (owner, "local", "", "owner", "active", now, now, now),
                ("telegram-exact", "telegram", "101", "user", "active", now, now, now),
                ("telegram-ambiguous", "telegram", "202", "user", "active", now, now, now),
                ("telegram-other", "telegram", "303", "user", "active", now, now, now),
            ],
        )
        db.executemany(
            """INSERT INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            [
                ("telegram-exact", "telegram", "101", "conv-exact", now),
                ("telegram-ambiguous", "telegram", "202", "conv-ambiguous", now),
                ("telegram-other", "telegram", "202", "conv-other", now),
            ],
        )
        db.executemany(
            """INSERT INTO raw_objects(
                   id,user_id,source,source_ref,raw_content,content_type,metadata_json,
                   content_hash,version,received_at,created_at,deleted_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                raw("raw_import_a", _metadata(import_source_path="/import/a.docx", filename="a.docx")),
                raw("raw_import_b", _metadata(filename="b.pdf", import_source_path="/import/b.pdf")),
                raw(
                    "raw_import_owned",
                    _metadata(import_source_path="/import/c.odt", uploaded_by="somebody-else"),
                ),
                raw("raw_import_null", _metadata(import_source_path="/import/d.xlsx", uploaded_by=None)),
                raw("raw_non_import", _metadata(filename="ordinary.docx")),
                raw(
                    "raw_tg_exact",
                    _metadata(channel="telegram-bridge", chat_id=101, filename="voice.ogg"),
                    source_ref="telegram-file:exact",
                ),
                raw(
                    "raw_tg_ambiguous",
                    _metadata(channel="telegram-bridge", chat_id=202, filename="voice.ogg"),
                    source_ref="telegram-file:ambiguous",
                ),
                raw(
                    "raw_tg_unmapped",
                    _metadata(channel="api-token", chat_id="", filename="legacy.bin"),
                    source_ref="legacy-other",
                ),
            ],
        )
    storage.close(final=True)
    return path, tenant, owner


def _claim(path: Path, plan: object) -> None:
    path.write_text(
        json.dumps(
            {
                "approved": True,
                "candidate_count": plan.candidate_count,
                "claim_scope": CLAIM_SCOPE,
                "owner_id": plan.owner_id,
                "plan_sha256": plan.plan_sha256,
                "schema": CLAIM_SCHEMA,
                "tenant_id": plan.tenant_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_preview_is_stable_read_only_and_telegram_is_report_only(settings: Any) -> None:
    database, tenant, owner = _seed_database(settings)
    before = database.read_bytes()

    first = audit_plan(database, tenant_id=tenant, owner_id=owner)
    second = audit_plan(database, tenant_id=tenant, owner_id=owner)

    assert first.candidate_count == 2
    assert first.plan_sha256 == second.plan_sha256
    assert first.explicit_uploader_rows == 2
    assert first.non_import_unattributed_rows == 4
    assert (
        first.telegram_exact_rows,
        first.telegram_ambiguous_rows,
        first.telegram_unmapped_rows,
    ) == (1, 1, 1)
    assert database.read_bytes() == before
    assert sanitize_audit_action(AUDIT_ACTION) == AUDIT_ACTION


def test_apply_changes_only_claimed_import_metadata_with_backup_and_audit(
    settings: Any, tmp_path: Path
) -> None:
    database, tenant, owner = _seed_database(settings)
    preview = audit_plan(database, tenant_id=tenant, owner_id=owner)
    claim = tmp_path / "claim.json"
    _claim(claim, preview)
    backups = tmp_path / "private-backups"

    applied, backup = apply_plan(
        database,
        tenant_id=tenant,
        owner_id=owner,
        claim_manifest=claim,
        expected_count=2,
        expected_plan_sha256=preview.plan_sha256,
        backup_dir=backups,
    )

    assert applied.plan_sha256 == preview.plan_sha256
    assert backup["verified"] is True
    assert (backups / backup["database"]).stat().st_mode & 0o777 == 0o600
    assert (backups / backup["manifest"]).stat().st_mode & 0o777 == 0o600
    db = sqlite3.connect(database)
    rows = {
        row[0]: json.loads(row[1])
        for row in db.execute("SELECT id,metadata_json FROM raw_objects ORDER BY id")
    }
    assert rows["raw_import_a"]["uploaded_by"] == owner
    assert rows["raw_import_b"]["uploaded_by"] == owner
    assert rows["raw_import_owned"]["uploaded_by"] == "somebody-else"
    assert "uploaded_by" not in rows["raw_non_import"]
    assert "uploaded_by" not in rows["raw_tg_exact"]
    assert rows["raw_import_null"]["uploaded_by"] is None
    audit = db.execute(
        "SELECT action,target_type,target_id,request_id FROM audit_log ORDER BY target_id"
    ).fetchall()
    assert len(audit) == 2
    assert {row[0] for row in audit} == {AUDIT_ACTION}
    assert {row[1] for row in audit} == {"raw_object"}
    assert len({row[3] for row in audit}) == 1
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    db.close()


def test_apply_refuses_stale_or_unapproved_exact_contract_without_writes(
    settings: Any, tmp_path: Path
) -> None:
    database, tenant, owner = _seed_database(settings)
    preview = audit_plan(database, tenant_id=tenant, owner_id=owner)
    claim = tmp_path / "claim.json"
    _claim(claim, preview)
    before = database.read_bytes()

    with pytest.raises(ContractError, match="count/checksum"):
        apply_plan(
            database,
            tenant_id=tenant,
            owner_id=owner,
            claim_manifest=claim,
            expected_count=preview.candidate_count + 1,
            expected_plan_sha256=preview.plan_sha256,
            backup_dir=tmp_path / "backups",
        )

    db = sqlite3.connect(database)
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert database.read_bytes() == before
    db.close()


def test_audit_failure_rolls_back_every_candidate_and_audit(settings: Any, tmp_path: Path) -> None:
    database, tenant, owner = _seed_database(settings)
    preview = audit_plan(database, tenant_id=tenant, owner_id=owner)
    claim = tmp_path / "claim.json"
    _claim(claim, preview)
    db = sqlite3.connect(database)
    db.execute(
        """CREATE TRIGGER reject_backfill_audit BEFORE INSERT ON audit_log
           WHEN NEW.action='cli.file_uploader.backfill'
           BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"""
    )
    db.commit()
    db.close()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        apply_plan(
            database,
            tenant_id=tenant,
            owner_id=owner,
            claim_manifest=claim,
            expected_count=preview.candidate_count,
            expected_plan_sha256=preview.plan_sha256,
            backup_dir=tmp_path / "backups",
        )

    db = sqlite3.connect(database)
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert {
        row[0]: "uploaded_by" in json.loads(row[1])
        for row in db.execute(
            "SELECT id,metadata_json FROM raw_objects WHERE id IN ('raw_import_a','raw_import_b')"
        )
    } == {"raw_import_a": False, "raw_import_b": False}
    db.close()
