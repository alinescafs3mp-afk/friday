"""Focused nodes for exact legacy Telegram uploader provenance backfill."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from friday.audit_privacy import sanitize_audit_action
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id
from tools.backfill_legacy_telegram_uploader_provenance import (
    AUDIT_ACTION,
    CLAIM_SCHEMA,
    CLAIM_SCOPE,
    EVIDENCE_IDENTITY_CURRENT,
    EVIDENCE_LEGACY_EXTERNAL_CURRENT,
    PLAN_SCHEMA,
    ContractError,
    _connect,
    _tag,
    _write_private_json,
    apply_plan,
    build_plan,
)


def _register_bytes(settings, tenant: str, body: bytes) -> tuple[str, str, dict]:
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{tenant[:12]}/{digest[:2]}/{digest}.bin"
    path = Path(settings.files_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    metadata = {
        "filename": "doc.bin",
        "mime_type": "application/octet-stream",
        "sha256": digest,
        "size_bytes": len(body),
        "stored_path": relative,
    }
    return digest, relative, metadata


def _insert_tg_file(
    settings,
    storage,
    *,
    tenant: str,
    body: bytes,
    chat_id: int | str | None,
    channel: str = "telegram-bridge",
    uploaded_by: object = ...,
    import_path: str | None = None,
    legacy: bool = False,
    corrupt_hash: bool = False,
    source: str = "upload",
    filename: str = "doc.bin",
    mime_type: str = "application/octet-stream",
    media_kind: str | None = None,
) -> str:
    digest, _relative, metadata = _register_bytes(settings, tenant, body)
    metadata["filename"] = filename
    metadata["mime_type"] = mime_type
    if media_kind is not None:
        metadata["media_kind"] = media_kind
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    if channel:
        metadata["channel"] = channel
    if uploaded_by is not ...:
        metadata["uploaded_by"] = uploaded_by
    if import_path is not None:
        metadata["import_source_path"] = import_path
    if legacy:
        metadata.pop("sha256", None)
        metadata.pop("size_bytes", None)
        metadata.pop("stored_path", None)
    if corrupt_hash:
        metadata["sha256"] = "0" * 64
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source=source,
        source_ref=f"telegram-file:AgADTEST-{digest[:12]}",
        raw_content="telegram file body",
        content_type="file",
        content_hash=digest,
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    return raw.id


def _seed_users_and_sessions(
    storage,
    *,
    now: str = "2026-01-01T00:00:00Z",
    with_identities: bool = True,
) -> None:
    with storage.transaction() as db:
        db.executemany(
            """INSERT OR IGNORE INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                ("tg-exact", "telegram", "101", "user", "active", now, now, now),
                ("tg-jbl", "telegram", "202", "user", "active", now, now, now),
                ("tg-ambiguous-a", "telegram", "303", "user", "active", now, now, now),
                ("tg-ambiguous-b", "telegram", "303", "user", "active", now, now, now),
                ("tg-unmapped-session", "telegram", "404", "user", "active", now, now, now),
            ],
        )
        db.executemany(
            """INSERT OR IGNORE INTO conversations(
                   id,user_id,title,created_at,updated_at,is_archived
               ) VALUES(?,?,?,?,?,0)""",
            [
                ("conv-exact", "tg-exact", "exact", now, now),
                ("conv-jbl", "tg-jbl", "jbl", now, now),
                ("conv-a", "tg-ambiguous-a", "a", now, now),
                ("conv-b", "tg-ambiguous-b", "b", now, now),
                ("conv-orphan", "tg-exact", "orphan", now, now),
            ],
        )
        db.executemany(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            [
                ("tg-exact", "telegram", "101", "conv-exact", now),
                ("tg-jbl", "telegram", "202", "conv-jbl", now),
                ("tg-ambiguous-a", "telegram", "303", "conv-a", now),
                ("tg-ambiguous-b", "telegram", "303", "conv-b", now),
            ],
        )
        if with_identities:
            db.executemany(
                """INSERT OR IGNORE INTO user_identities(
                       source,external_id,user_id,linked_by,created_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    ("telegram", "101", "tg-exact", "test", now),
                    ("telegram", "202", "tg-jbl", "test", now),
                    ("telegram", "303", "tg-ambiguous-a", "test", now),
                    # Second identity for same chat 303 → ambiguous at identity layer when both present.
                    # Keep only one identity for 303 so session-level ambiguity still fires.
                ],
            )


def _claim(tmp_path: Path, plan) -> Path:
    claim_path = tmp_path / "claim.json"
    os.chmod(tmp_path, 0o700)
    _write_private_json(
        claim_path,
        {
            "approved": True,
            "candidate_count": plan.candidate_count,
            "claim_scope": CLAIM_SCOPE,
            "owner_id": plan.owner_id,
            "plan_sha256": plan.plan_sha256,
            "schema": CLAIM_SCHEMA,
            "tenant_id": plan.tenant_id,
        },
    )
    return claim_path


def test_exact_owner_and_jbl_mapping_accepted(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    exact_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"EXACT-OWNER-BYTES", chat_id=101)
    jbl_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"EXACT-JBL-BYTES", chat_id=202)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan.candidate_count == 2
    by_raw = {c.raw_id: c for c in plan.candidates}
    assert by_raw[exact_id].mapped_uploader_id == "tg-exact"
    assert by_raw[jbl_id].mapped_uploader_id == "tg-jbl"
    assert by_raw[exact_id].mapping_evidence_class == EVIDENCE_IDENTITY_CURRENT
    assert by_raw[jbl_id].mapping_evidence_class == EVIDENCE_IDENTITY_CURRENT
    assert by_raw[exact_id].conversation_id == "conv-exact"
    assert plan.counts["exact"] == 2
    assert plan.counts["exact_identity_current"] == 2
    assert plan.disk_verified is True
    assert sanitize_audit_action(AUDIT_ACTION) == AUDIT_ACTION


def test_foreign_tenant_with_matching_identity_session_refused(settings, storage) -> None:
    """Owner A cannot plan Raw rows belonging to foreign tenant B."""

    owner = LEGACY_OWNER_USER_ID
    foreign = "bob-tenant"
    storage.ensure_user(owner, preset_key="owner")
    storage.ensure_user(foreign, preset_key="user")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    # Matching identity/session for chat 101, but Raw lives under foreign tenant.
    _insert_tg_file(settings, storage, tenant=foreign, body=b"FOREIGN-TENANT", chat_id=101)
    # Owner archive itself has no unattributed telegram file.
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=owner,
            owner_id=owner,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 0

    # Explicit mismatch tenant != owner fails closed.
    with pytest.raises(ContractError, match="tenant id must equal owner id"):
        conn = _connect(Path(settings.database_path), read_only=True)
        try:
            build_plan(
                conn,
                tenant_id=foreign,
                owner_id=owner,
                files_root=files_root,
            )
        finally:
            conn.close()


def test_session_conversation_owner_mismatch_and_archived_refused(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)
    now = "2026-01-01T00:00:00Z"

    # Chat 606: identity → tg-exact, but session points at conversation owned by tg-jbl.
    with storage.transaction() as db:
        db.execute(
            """INSERT OR IGNORE INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("tg-mismatch", "telegram", "606", "user", "active", now, now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO user_identities(source,external_id,user_id,linked_by,created_at)
               VALUES(?,?,?,?,?)""",
            ("telegram", "606", "tg-exact", "test", now),
        )
        db.execute(
            """INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at,is_archived)
               VALUES(?,?,?,?,?,0)""",
            ("conv-wrong-owner", "tg-jbl", "wrong", now, now),
        )
        # Direct session with conversation_id belonging to another user — JOIN fails closed.
        db.execute(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            ("tg-exact", "telegram", "606", "conv-wrong-owner", now),
        )

        # Chat 707: identity + session OK shape but conversation is archived.
        db.execute(
            """INSERT OR IGNORE INTO user_identities(source,external_id,user_id,linked_by,created_at)
               VALUES(?,?,?,?,?)""",
            ("telegram", "707", "tg-exact", "test", now),
        )
        db.execute(
            """INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at,is_archived)
               VALUES(?,?,?,?,?,1)""",
            ("conv-archived", "tg-exact", "archived", now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            ("tg-exact", "telegram", "707", "conv-archived", now),
        )

    _insert_tg_file(settings, storage, tenant=tenant, body=b"MISMATCH-SESS", chat_id=606)
    _insert_tg_file(settings, storage, tenant=tenant, body=b"ARCHIVED-SESS", chat_id=707)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 0
    assert plan.counts["exact"] == 0
    assert plan.counts["unmapped"] + plan.counts["ambiguous"] >= 2


def test_duplicate_identity_and_session_refused(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)
    now = "2026-01-01T00:00:00Z"

    with storage.transaction() as db:
        # Duplicate identities for chat 808.
        db.execute(
            """INSERT OR IGNORE INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("tg-dup-a", "telegram", "808", "user", "active", now, now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("tg-dup-b", "telegram", "808b", "user", "active", now, now, now),
        )
        # PK is (source, external_id) — only one identity per external_id possible.
        # Force ambiguity via two sessions for same chat with two owned conversations.
        db.execute(
            """INSERT OR IGNORE INTO user_identities(source,external_id,user_id,linked_by,created_at)
               VALUES(?,?,?,?,?)""",
            ("telegram", "808", "tg-dup-a", "test", now),
        )
        db.executemany(
            """INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at,is_archived)
               VALUES(?,?,?,?,?,0)""",
            [
                ("conv-dup-a", "tg-dup-a", "a", now, now),
                ("conv-dup-b", "tg-dup-b", "b", now, now),
            ],
        )
        db.executemany(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            [
                ("tg-dup-a", "telegram", "808", "conv-dup-a", now),
                ("tg-dup-b", "telegram", "808", "conv-dup-b", now),
            ],
        )

    _insert_tg_file(settings, storage, tenant=tenant, body=b"DUP-SESS", chat_id=808)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 0
    assert plan.counts["ambiguous"] >= 1


def test_plan_sha_changes_when_session_conversation_evidence_changes(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    _insert_tg_file(settings, storage, tenant=tenant, body=b"SHA-BODY-A", chat_id=101)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_a = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan_a.candidate_count == 1
    sha_a = plan_a.plan_sha256

    # Mutate private session/conversation evidence for the same mapped user.
    with storage.transaction() as db:
        now = "2026-08-12T12:00:00Z"
        db.execute(
            """INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at,is_archived)
               VALUES(?,?,?,?,?,0)""",
            ("conv-exact-2", "tg-exact", "exact-2", now, now),
        )
        db.execute(
            """UPDATE channel_sessions
                  SET conversation_id=?, updated_at=?
                WHERE user_id=? AND channel='telegram' AND channel_id=?""",
            ("conv-exact-2", now, "tg-exact", "101"),
        )

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_b = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan_b.candidate_count == 1
    assert plan_b.plan_sha256 != sha_a
    assert plan_b.candidates[0].conversation_id == "conv-exact-2"

    public = plan_b.public_summary(mode="dry_run")
    public_text = json.dumps(public, ensure_ascii=True, sort_keys=True)
    assert "tg-exact" not in public_text
    assert "conv-exact" not in public_text
    assert "101" not in public_text
    for cand in plan_b.candidates:
        assert cand.raw_id not in public_text
        assert cand.conversation_id not in public_text
        assert cand.session_user_id not in public_text


def test_mismatched_ambiguous_unmapped_null_ignored_invalid_refused(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    # Ambiguous: two sessions share chat 303.
    _insert_tg_file(settings, storage, tenant=tenant, body=b"AMB-BYTES", chat_id=303)
    # Unmapped chat: no user/session/identity.
    _insert_tg_file(settings, storage, tenant=tenant, body=b"UNMAP-BYTES", chat_id=999001)
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"NULL-BYTES",
        chat_id=101,
        uploaded_by=None,
    )
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"EXIST-BYTES",
        chat_id=101,
        uploaded_by="someone",
    )
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"CLI-BYTES",
        chat_id=101,
        import_path="/import/x.docx",
    )
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"LEGACY-BYTES",
        chat_id=101,
        legacy=True,
    )
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"INVALID-BYTES",
        chat_id=101,
        corrupt_hash=True,
    )
    ignored_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"IGNORED-BYTES", chat_id=101)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant,
            raw_object_id=ignored_id,
            status=InboxStatus.IGNORED,
        )
    )
    with storage.transaction() as db:
        now = "2026-01-01T00:00:00Z"
        db.execute(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            ("tg-exact", "telegram", "505", "conv-orphan", now),
        )
    _insert_tg_file(settings, storage, tenant=tenant, body=b"ORPHAN-SESSION", chat_id=505)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan.candidate_count == 0
    assert plan.counts["planned_inserts"] == 0
    assert plan.counts["ambiguous"] >= 1
    assert plan.counts["unmapped"] >= 1
    assert plan.counts["explicit_null_uploader"] >= 1
    assert plan.counts["existing_uploader"] >= 1
    assert plan.counts["cli_import"] >= 1
    assert plan.counts["refused_registration_legacy"] >= 1
    assert plan.counts["refused_registration_invalid"] >= 1
    assert all(c.raw_id != ignored_id for c in plan.candidates)


def test_legacy_external_fallback_when_identity_absent(settings, storage) -> None:
    """Closed legacy class: active users.source/external_id + owned session, no identity."""

    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    files_root = Path(settings.files_dir)
    now = "2026-01-01T00:00:00Z"
    with storage.transaction() as db:
        db.execute(
            """INSERT OR IGNORE INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("tg-legacy", "telegram", "909", "user", "active", now, now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO conversations(id,user_id,title,created_at,updated_at,is_archived)
               VALUES(?,?,?,?,?,0)""",
            ("conv-legacy", "tg-legacy", "legacy", now, now),
        )
        db.execute(
            """INSERT OR IGNORE INTO channel_sessions(
                   user_id,channel,channel_id,conversation_id,mode,updated_at
               ) VALUES(?,?,?,?,'dialogue',?)""",
            ("tg-legacy", "telegram", "909", "conv-legacy", now),
        )
        # Explicitly no user_identities row for 909.

    raw_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"LEGACY-EXT", chat_id=909)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 1
    assert plan.candidates[0].raw_id == raw_id
    assert plan.candidates[0].mapped_uploader_id == "tg-legacy"
    assert plan.candidates[0].mapping_evidence_class == EVIDENCE_LEGACY_EXTERNAL_CURRENT
    assert plan.counts["exact_legacy_external_current"] == 1
    assert plan.counts["exact_identity_current"] == 0


def test_plan_mutation_changes_sha_and_public_report_is_private(settings, storage) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    raw_a = _insert_tg_file(settings, storage, tenant=tenant, body=b"SHA-BODY-A", chat_id=101)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_a = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    raw_b = _insert_tg_file(settings, storage, tenant=tenant, body=b"SHA-BODY-B", chat_id=202)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_b = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan_a.candidate_count == 1
    assert plan_b.candidate_count == 2
    assert plan_a.plan_sha256 != plan_b.plan_sha256

    public = plan_b.public_summary(mode="dry_run")
    public_text = json.dumps(public, ensure_ascii=True, sort_keys=True)
    assert raw_a not in public_text
    assert raw_b not in public_text
    assert "tg-exact" not in public_text
    assert "tg-jbl" not in public_text
    assert "101" not in public_text
    assert "doc.bin" not in public_text
    for cand in plan_b.candidates:
        assert cand.raw_id not in public_text
        assert cand.mapped_uploader_id not in public_text
        assert cand.content_hash not in public_text
        assert cand.metadata_sha256 not in public_text
    for sample in public["sample_candidate_tags"]:
        assert set(sample.keys()) <= {
            "mapped_uploader_tag",
            "mapping_evidence_class",
            "raw_tag",
            "registration_class",
        }
        assert len(sample["raw_tag"]) == 16
        assert sample["raw_tag"] == _tag(
            next(c.raw_id for c in plan_b.candidates if _tag(c.raw_id) == sample["raw_tag"])
        )


def test_successful_apply_writes_mapped_uploader_and_audit(settings, storage, tmp_path) -> None:
    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    raw_exact = _insert_tg_file(settings, storage, tenant=tenant, body=b"APPLY-EXACT", chat_id=101)
    raw_jbl = _insert_tg_file(settings, storage, tenant=tenant, body=b"APPLY-JBL", chat_id=202)
    _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"APPLY-NULL",
        chat_id=101,
        uploaded_by=None,
    )
    _insert_tg_file(settings, storage, tenant=tenant, body=b"APPLY-AMB", chat_id=303)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 2

    claim_path = _claim(tmp_path, plan)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)

    with pytest.raises(ContractError, match="stopped-writers"):
        apply_plan(
            Path(settings.database_path),
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
            claim_manifest=claim_path,
            expected_count=plan.candidate_count,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backup_dir,
            writers_stopped_acknowledged=False,
        )

    applied, extra = apply_plan(
        Path(settings.database_path),
        tenant_id=tenant,
        owner_id=tenant,
        files_root=files_root,
        claim_manifest=claim_path,
        expected_count=plan.candidate_count,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backup_dir,
        writers_stopped_acknowledged=True,
    )
    assert applied.candidate_count == 2
    assert extra["applied"] == 2
    assert extra["backup"]["verified"] is True

    meta_exact = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_exact,)).fetchone()[0]
    )
    meta_jbl = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_jbl,)).fetchone()[0]
    )
    assert meta_exact["uploaded_by"] == "tg-exact"
    assert meta_jbl["uploaded_by"] == "tg-jbl"
    assert meta_exact["channel"] == "telegram-bridge"
    assert meta_exact["chat_id"] == 101

    audits = storage.execute(
        "SELECT action, target_type, target_id, user_id FROM audit_log WHERE action=?",
        (AUDIT_ACTION,),
    ).fetchall()
    assert len(audits) == 2
    assert {row[1] for row in audits} == {"raw_object"}
    assert {row[2] for row in audits} == {raw_exact, raw_jbl}
    assert {row[3] for row in audits} == {tenant}

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        remaining = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert remaining.candidate_count == 0


def test_postcondition_failure_after_writes_rolls_back_metadata_and_audit(
    settings, storage, tmp_path, monkeypatch
) -> None:
    """One exact node: writes are visible inside the tx, then both roll back."""

    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)
    raw_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"ROLLBACK-BYTES", chat_id=101)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    claim_path = _claim(tmp_path, plan)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)

    import tools.backfill_legacy_telegram_uploader_provenance as mod

    original_postcheck = mod._postcheck_transaction
    seen = {"writes_visible": False}

    def wrapped_postcheck(conn, plan_arg, *, audit_request_id, files_root):  # noqa: ANN001
        meta_row = conn.execute(
            "SELECT metadata_json FROM raw_objects WHERE id=?",
            (raw_id,),
        ).fetchone()
        assert meta_row is not None
        meta = json.loads(meta_row[0] if not hasattr(meta_row, "keys") else meta_row["metadata_json"])
        assert meta.get("uploaded_by") == "tg-exact"
        audit_row = conn.execute(
            """SELECT 1 FROM audit_log
                WHERE action=? AND target_type='raw_object' AND target_id=?""",
            (AUDIT_ACTION, raw_id),
        ).fetchone()
        assert audit_row is not None
        seen["writes_visible"] = True
        raise ContractError("synthetic postcondition failure after visible writes")

    monkeypatch.setattr(mod, "_postcheck_transaction", wrapped_postcheck)

    with pytest.raises(ContractError, match="synthetic postcondition"):
        mod.apply_plan(
            Path(settings.database_path),
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
            claim_manifest=claim_path,
            expected_count=plan.candidate_count,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backup_dir,
            writers_stopped_acknowledged=True,
        )

    assert seen["writes_visible"] is True
    meta_after = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_id,)).fetchone()[0]
    )
    assert "uploaded_by" not in meta_after
    audit_n = storage.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action=?",
        (AUDIT_ACTION,),
    ).fetchone()
    n = int(audit_n["n"] if hasattr(audit_n, "keys") else audit_n[0])
    assert n == 0
    assert original_postcheck is not None


def test_missing_files_root_fails_closed() -> None:
    with pytest.raises(ContractError, match="files root"):
        from tools.backfill_legacy_telegram_uploader_provenance import _resolve_files_root

        _resolve_files_root(None)


def test_canonical_operator_with_peer_owner_presets_applies_exact_audio_provenance_but_audio_stays_out_of_document_readers(
    settings, storage, tmp_path, monkeypatch
) -> None:
    """Canonical owner + peer owners: exact audio is attributed offline, not readable."""

    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user("peer-owner-alpha", preset_key="owner")
    storage.ensure_user("peer-owner-beta", preset_key="owner")
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)

    assert PLAN_SCHEMA.endswith(".v3")
    assert CLAIM_SCHEMA.endswith(".v2")

    audio_id = _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"VOICE-OGG-BYTES-FAKE",
        chat_id=101,
        filename="voice.ogg",
        mime_type="audio/ogg",
        media_kind="voice",
    )
    doc_id = _insert_tg_file(
        settings,
        storage,
        tenant=tenant,
        body=b"EXACT-DOC-BYTES",
        chat_id=202,
        filename="note.bin",
    )
    amb_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"AMB-BYTES", chat_id=303)
    unmapped_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"UNMAP-BYTES", chat_id=999001)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        with pytest.raises(ContractError, match="canonical archive owner"):
            build_plan(
                conn,
                tenant_id="peer-owner-alpha",
                owner_id="peer-owner-alpha",
                files_root=files_root,
            )
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan.candidate_count == 2
    by_raw = {candidate.raw_id: candidate for candidate in plan.candidates}
    assert set(by_raw) == {audio_id, doc_id}
    assert by_raw[audio_id].mapped_uploader_id == "tg-exact"
    assert by_raw[doc_id].mapped_uploader_id == "tg-jbl"
    assert by_raw[audio_id].audio_carrier is True
    assert by_raw[doc_id].audio_carrier is False
    assert plan.counts["planned_audio"] == 1
    assert plan.counts["refused_audio"] == 0
    assert plan.counts["ambiguous"] >= 1
    assert plan.counts["unmapped"] >= 1
    public = plan.public_summary(mode="dry_run")
    assert public["counts"]["planned_audio"] == 1
    public_text = json.dumps(public, ensure_ascii=True, sort_keys=True)
    assert tenant not in public_text
    assert audio_id not in public_text
    assert "voice.ogg" not in public_text

    claim_path = _claim(tmp_path, plan)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    stale = json.loads(claim_path.read_text(encoding="utf-8"))
    stale["schema"] = "friday.legacy-telegram-uploader-provenance-claim.v1"
    stale_path = tmp_path / "stale-claim.json"
    _write_private_json(stale_path, stale)
    with pytest.raises(ContractError, match="schema or scope"):
        apply_plan(
            Path(settings.database_path),
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
            claim_manifest=stale_path,
            expected_count=plan.candidate_count,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backup_dir,
            writers_stopped_acknowledged=True,
        )

    import tools.backfill_legacy_telegram_uploader_provenance as mod

    original_build = mod.build_plan
    calls = {"n": 0}

    def wrapping_build_plan(conn_arg, *, tenant_id, owner_id, files_root):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            result = original_build(
                conn_arg,
                tenant_id=tenant_id,
                owner_id=owner_id,
                files_root=files_root,
            )
            now = "2026-01-02T00:00:00Z"
            with storage.transaction() as db:
                db.execute(
                    "UPDATE users SET status='disabled', updated_at=? WHERE id=?",
                    (now, tenant),
                )
            return result
        return original_build(
            conn_arg,
            tenant_id=tenant_id,
            owner_id=owner_id,
            files_root=files_root,
        )

    monkeypatch.setattr(mod, "build_plan", wrapping_build_plan)
    try:
        with pytest.raises(ContractError, match="canonical archive owner must be active"):
            mod.apply_plan(
                Path(settings.database_path),
                tenant_id=tenant,
                owner_id=tenant,
                files_root=files_root,
                claim_manifest=claim_path,
                expected_count=plan.candidate_count,
                expected_plan_sha256=plan.plan_sha256,
                backup_dir=backup_dir,
                writers_stopped_acknowledged=True,
            )
    finally:
        monkeypatch.setattr(mod, "build_plan", original_build)

    assert calls["n"] >= 2
    for raw_id in (audio_id, doc_id, amb_id, unmapped_id):
        meta_after = json.loads(
            storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_id,)).fetchone()[0]
        )
        assert "uploaded_by" not in meta_after
    audit_n = storage.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action=?",
        (AUDIT_ACTION,),
    ).fetchone()
    assert int(audit_n["n"] if hasattr(audit_n, "keys") else audit_n[0]) == 0

    with storage.transaction() as db:
        db.execute(
            "UPDATE users SET status='active', updated_at=? WHERE id=?",
            ("2026-01-02T00:01:00Z", tenant),
        )

    applied, extra = apply_plan(
        Path(settings.database_path),
        tenant_id=tenant,
        owner_id=tenant,
        files_root=files_root,
        claim_manifest=claim_path,
        expected_count=plan.candidate_count,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backup_dir,
        writers_stopped_acknowledged=True,
    )
    assert applied.candidate_count == 2
    assert extra["applied"] == 2
    assert applied.counts["planned_audio"] == 1

    meta_audio = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (audio_id,)).fetchone()[0]
    )
    meta_doc = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (doc_id,)).fetchone()[0]
    )
    meta_amb = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (amb_id,)).fetchone()[0]
    )
    meta_unmapped = json.loads(
        storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (unmapped_id,)).fetchone()[0]
    )
    assert meta_audio["uploaded_by"] == "tg-exact"
    assert meta_doc["uploaded_by"] == "tg-jbl"
    assert "uploaded_by" not in meta_amb
    assert "uploaded_by" not in meta_unmapped
    audits = storage.execute(
        "SELECT target_id FROM audit_log WHERE action=?",
        (AUDIT_ACTION,),
    ).fetchall()
    assert {row[0] for row in audits} == {audio_id, doc_id}

    visible = storage.get_searchable_file_sources(tenant, [audio_id, doc_id])
    assert [row["id"] for row in visible] == [doc_id]
    jbl_visible = storage.get_searchable_file_sources(tenant, [audio_id, doc_id], uploaded_by="tg-jbl")
    assert [row["id"] for row in jbl_visible] == [doc_id]
    assert storage.get_searchable_file_sources(tenant, [audio_id], uploaded_by="tg-exact") == []
    assert storage.find_owned_files_by_filename(tenant, "tg-exact", "voice.ogg") == []
    found_doc = storage.find_owned_files_by_filename(tenant, "tg-jbl", "note.bin")
    assert [row["id"] for row in found_doc] == [doc_id]


def test_unique_active_owner_still_accepted(settings, storage) -> None:
    """Canonical owner stays accepted when an inactive peer owner row exists."""

    tenant = LEGACY_OWNER_USER_ID
    storage.ensure_user(tenant, preset_key="owner")
    # Inactive second owner must not poison the unique-active gate.
    now = "2026-01-01T00:00:00Z"
    with storage.transaction() as db:
        db.execute(
            """INSERT INTO users(
                   id,source,external_id,preset_key,status,created_at,updated_at,last_seen_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            ("alice-inactive-owner", "local", "", "owner", "disabled", now, now, now),
        )
    _seed_users_and_sessions(storage)
    files_root = Path(settings.files_dir)
    raw_id = _insert_tg_file(settings, storage, tenant=tenant, body=b"UNIQUE-OWNER-OK", chat_id=101)

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan.candidate_count == 1
    assert plan.candidates[0].raw_id == raw_id
    assert plan.candidates[0].mapped_uploader_id == "tg-exact"
