"""Focused nodes for historical Telegram file-alias backfill."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id
from tools.backfill_telegram_file_aliases import (
    AUDIT_ACTION,
    CLAIM_SCHEMA,
    CLAIM_SCOPE,
    ContractError,
    _connect,
    _tag,
    _write_private_json,
    apply_plan,
    build_plan,
)


def _insert_authorized_file(
    settings,
    storage,
    *,
    tenant: str,
    uploader: str,
    source_ref: str,
    body: bytes,
    legacy: bool = False,
    corrupt_hash: bool = False,
) -> str:
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{tenant[:12]}/{digest[:2]}/{digest}.bin"
    path = Path(settings.files_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    metadata: dict = {
        "filename": "doc.bin",
        "mime_type": "application/octet-stream",
        "uploaded_by": uploader,
        "chat_id": "42",
    }
    if not legacy:
        metadata["sha256"] = ("0" * 64) if corrupt_hash else digest
        metadata["size_bytes"] = len(body)
        metadata["stored_path"] = relative
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="upload",
        source_ref=source_ref,
        raw_content="telegram file body",
        content_type="file",
        content_hash=digest,
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    return raw.id


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
            "uploader_id": plan.uploader_id,
        },
    )
    return claim_path


def test_recoverable_historical_file_ref_plans_and_applies(settings, storage, tmp_path) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    file_ref = "telegram-file:AgADTEST-HISTORICAL-FILE-ID-001"
    raw_id = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=file_ref,
        body=b"HIST-TELEGRAM-FILE-BYTES-001",
    )
    files_root = Path(settings.files_dir)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 1
    assert plan.candidates[0].raw_id == raw_id
    assert plan.candidates[0].source_ref == file_ref
    assert plan.candidates[0].kind == "file"
    assert plan.disk_verified is True
    assert plan.plan_sha256

    public = plan.public_summary(mode="preview")
    public_text = json.dumps(public, ensure_ascii=True)
    assert raw_id not in public_text
    assert file_ref not in public_text
    assert plan.candidates[0].content_hash not in public_text
    assert "doc.bin" not in public_text

    claim_path = _claim(tmp_path, plan)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    applied, extra = apply_plan(
        Path(settings.database_path),
        tenant_id=tenant,
        owner_id=tenant,
        uploader_id=tenant,
        files_root=files_root,
        claim_manifest=claim_path,
        expected_count=plan.candidate_count,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backup_dir,
    )
    assert applied.candidate_count == 1
    assert extra["applied"] == 1
    row = storage.execute(
        "SELECT raw_object_id FROM file_source_aliases WHERE source_ref=? AND user_id=?",
        (file_ref, tenant),
    ).fetchone()
    assert row is not None
    assert str(row["raw_object_id"] if hasattr(row, "keys") else row[0]) == raw_id
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan2 = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan2.candidate_count == 0
    assert plan2.counts["already_bound"] >= 1


def test_conflict_foreign_and_deleted_produce_zero_planned_writes(settings, storage) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user("bob", preset_key="user")
    file_ref = "telegram-file:AgADTEST-CONFLICT-FILE-ID-002"
    files_root = Path(settings.files_dir)
    raw_a = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref="telegram-file:AgADTEST-OTHER-003",
        body=b"A-BYTES",
    )
    _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=file_ref,
        body=b"B-BYTES",
    )
    storage.bind_owned_file_source_ref_alias(tenant, tenant, file_ref, raw_a)
    _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader="bob",
        source_ref="telegram-file:AgADTEST-FOREIGN-004",
        body=b"FOREIGN",
    )
    deleted_id = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref="telegram-file:AgADTEST-DELETED-005",
        body=b"DELETED",
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET deleted_at=? WHERE id=?",
            ("2026-08-12T00:00:00+00:00", deleted_id),
        )

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.counts["conflicts"] >= 1
    assert all(c.source_ref != file_ref for c in plan.candidates)
    assert all(c.uploader_id == tenant for c in plan.candidates)
    assert all(c.source_ref != "telegram-file:AgADTEST-DELETED-005" for c in plan.candidates)
    assert all(c.source_ref != "telegram-file:AgADTEST-FOREIGN-004" for c in plan.candidates)


def test_ignored_upload_with_recoverable_ref_is_zero_candidate(settings, storage) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    file_ref = "telegram-file:AgADTEST-IGNORED-FILE-ID-010"
    raw_id = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=file_ref,
        body=b"IGNORED-BYTES",
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=tenant,
            raw_object_id=raw_id,
            status=InboxStatus.IGNORED,
        )
    )
    files_root = Path(settings.files_dir)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 0
    assert plan.counts["planned_inserts"] == 0
    assert plan.counts["rows_scanned"] == 0
    assert all(c.source_ref != file_ref for c in plan.candidates)


def test_plan_sha_uses_private_full_basis_not_public_tags(settings, storage) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    files_root = Path(settings.files_dir)
    ref_a = "telegram-file:AgADTEST-SHA-BASIS-A"
    ref_b = "telegram-file:AgADTEST-SHA-BASIS-B"
    _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=ref_a,
        body=b"SHA-BASIS-BODY-A",
    )
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_a = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    raw_b = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=ref_b,
        body=b"SHA-BASIS-BODY-B",
    )
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan_b = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert plan_a.candidate_count >= 1
    assert plan_b.candidate_count > plan_a.candidate_count
    assert plan_a.plan_sha256 != plan_b.plan_sha256

    public_b = plan_b.public_summary(mode="preview")
    public_text = json.dumps(public_b, ensure_ascii=True, sort_keys=True)
    assert ref_a not in public_text
    assert ref_b not in public_text
    assert raw_b not in public_text
    for cand in plan_b.candidates:
        assert cand.raw_id not in public_text
        assert cand.source_ref not in public_text
        assert cand.content_hash not in public_text
    # sample must not embed full content_hash field; only short tags/classes
    for sample in public_b["sample_candidate_tags"]:
        assert "content_hash" not in sample
        assert set(sample.keys()) <= {
            "evidence_class",
            "kind",
            "raw_tag",
            "registration_class",
            "source_ref_tag",
            "uploader_tag",
        }
        assert len(sample["raw_tag"]) == 16
        assert sample["raw_tag"] == _tag(
            next(c.raw_id for c in plan_b.candidates if _tag(c.raw_id) == sample["raw_tag"])
        )


def test_foreign_uploader_excluded_approved_jbl_plan_works(settings, storage) -> None:
    tenant = "alice"
    jbl = "jbl_user"
    foreign = "eve"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(jbl, preset_key="user")
    storage.ensure_user(foreign, preset_key="user")
    files_root = Path(settings.files_dir)

    owner_ref = "telegram-file:AgADTEST-OWNER-SCOPE-020"
    jbl_ref = "telegram-file:AgADTEST-JBL-SCOPE-021"
    foreign_ref = "telegram-file:AgADTEST-FOREIGN-SCOPE-022"
    _insert_authorized_file(
        settings, storage, tenant=tenant, uploader=tenant, source_ref=owner_ref, body=b"OWNER-SCOPE"
    )
    jbl_raw = _insert_authorized_file(
        settings, storage, tenant=tenant, uploader=jbl, source_ref=jbl_ref, body=b"JBL-SCOPE"
    )
    _insert_authorized_file(
        settings, storage, tenant=tenant, uploader=foreign, source_ref=foreign_ref, body=b"FOREIGN-SCOPE"
    )

    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        owner_plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
        jbl_plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=jbl,
            files_root=files_root,
        )
        foreign_plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=foreign,
            files_root=files_root,
        )
    finally:
        conn.close()

    assert owner_plan.candidate_count == 1
    assert owner_plan.candidates[0].source_ref == owner_ref
    assert all(c.source_ref != jbl_ref for c in owner_plan.candidates)
    assert all(c.source_ref != foreign_ref for c in owner_plan.candidates)

    assert jbl_plan.candidate_count == 1
    assert jbl_plan.uploader_id == jbl
    assert jbl_plan.owner_id == tenant
    assert jbl_plan.candidates[0].raw_id == jbl_raw
    assert jbl_plan.candidates[0].source_ref == jbl_ref
    assert jbl_plan.plan_sha256 != owner_plan.plan_sha256

    assert foreign_plan.candidate_count == 1  # foreign is active user, exact-scoped
    # Control: foreign is NOT mixed into owner plan
    assert all(c.uploader_id == tenant for c in owner_plan.candidates)
    assert all(c.uploader_id == jbl for c in jbl_plan.candidates)


def test_legacy_and_invalid_registration_zero_candidate(settings, storage) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    files_root = Path(settings.files_dir)
    _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref="telegram-file:AgADTEST-LEGACY-030",
        body=b"LEGACY-BODY",
        legacy=True,
    )
    _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref="telegram-file:AgADTEST-INVALID-031",
        body=b"INVALID-BODY",
        corrupt_hash=True,
    )
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    assert plan.candidate_count == 0
    assert plan.counts["refused_registration_legacy"] >= 1
    assert plan.counts["refused_registration_invalid"] >= 1


def test_apply_rolls_back_when_postcondition_fails(settings, storage, tmp_path, monkeypatch) -> None:
    tenant = "alice"
    storage.ensure_user(tenant, preset_key="owner")
    file_ref = "telegram-file:AgADTEST-APPLY-FILE-ID-005"
    raw_id = _insert_authorized_file(
        settings,
        storage,
        tenant=tenant,
        uploader=tenant,
        source_ref=file_ref,
        body=b"APPLY-BYTES",
    )
    files_root = Path(settings.files_dir)
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        plan = build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
        )
    finally:
        conn.close()
    claim_path = _claim(tmp_path, plan)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)

    import tools.backfill_telegram_file_aliases as mod

    original = mod.build_plan
    state = {"i": 0}

    def wrapped(conn, **kw):  # noqa: ANN001, ANN003
        state["i"] += 1
        result = original(conn, **kw)
        if state["i"] == 3:
            alias_visible = conn.execute(
                "SELECT 1 FROM file_source_aliases WHERE source_ref=? AND raw_object_id=?",
                (file_ref, raw_id),
            ).fetchone()
            assert alias_visible is not None
            audit_visible = conn.execute(
                """SELECT 1 FROM audit_log
                    WHERE action=? AND target_type='raw_object'
                      AND target_id=? AND user_id=?""",
                (AUDIT_ACTION, raw_id, tenant),
            ).fetchone()
            assert audit_visible is not None
            from tools.backfill_telegram_file_aliases import AliasCandidate

            fake = AliasCandidate(
                raw_id=raw_id,
                tenant_id=tenant,
                uploader_id=tenant,
                source_ref=file_ref + "-ghost",
                kind="file",
                evidence_class="test",
                content_hash="0" * 64,
                registration_class="modern_valid_disk",
            )
            return type(result)(
                tenant_id=result.tenant_id,
                owner_id=result.owner_id,
                uploader_id=result.uploader_id,
                candidates=(fake,),
                plan_sha256=result.plan_sha256,
                counts=result.counts,
                disk_verified=result.disk_verified,
            )
        return result

    monkeypatch.setattr(mod, "build_plan", wrapped)
    with pytest.raises(ContractError):
        mod.apply_plan(
            Path(settings.database_path),
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=tenant,
            files_root=files_root,
            claim_manifest=claim_path,
            expected_count=plan.candidate_count,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backup_dir,
        )

    assert state["i"] == 3
    row = storage.execute(
        "SELECT 1 FROM file_source_aliases WHERE source_ref=? AND raw_object_id=?",
        (file_ref, raw_id),
    ).fetchone()
    assert row is None
    audit_n = storage.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action=?",
        (AUDIT_ACTION,),
    ).fetchone()
    n = int(audit_n["n"] if hasattr(audit_n, "keys") else audit_n[0])
    assert n == 0


def test_missing_files_root_fails_closed() -> None:
    with pytest.raises(ContractError, match="files root"):
        from tools.backfill_telegram_file_aliases import _resolve_files_root

        _resolve_files_root(None)
