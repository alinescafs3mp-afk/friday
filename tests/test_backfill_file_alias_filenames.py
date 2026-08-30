"""Focused contract for the historical deduplicated-upload filename repair."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

import tools.backfill_file_alias_filenames as operator
import tools.immutable_release_operator as release_operator
from friday.diagnostics.runtime_lease import ProcessLease
from friday.storage import FridayStorage, UnsupportedSchemaVersionError
from friday.storage.models import RawObject, new_id
from tools.backfill_file_alias_filenames import (
    CLAIM_SCHEMA,
    CLAIM_SCOPE,
    ContractError,
    _connect,
    _write_private_json,
    apply_plan,
    apply_plan_under_held_leases,
    build_plan,
)


def _external_cutover_receipt(settings, tmp_path: Path):
    inbox = settings.state_dir / "telegram-inbox.sqlite3"
    connection = sqlite3.connect(inbox)
    connection.execute("CREATE TABLE updates(update_id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    inbox.chmod(0o600)
    backup_dir = tmp_path / "cutover-backups"
    backup_dir.mkdir(mode=0o700)
    health_ca = tmp_path / "unused-ca.pem"
    health_ca.write_text("unused", encoding="ascii")
    health_ca.chmod(0o600)
    config = release_operator.SystemdConfig(
        anchor=tmp_path / "anchor",
        env_file=tmp_path / "env",
        env_file_sha256="0" * 64,
        friday_home=tmp_path,
        unit_dir=tmp_path,
        database=Path(settings.database_path),
        inbox_database=inbox,
        backup_dir=backup_dir,
        state_dir=settings.state_dir,
        health_ca=health_ca,
        health_ca_sha256=hashlib.sha256(health_ca.read_bytes()).hexdigest(),
    )
    backup = release_operator._exact_sqlite_backup(config)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    live_identity, live_sha256 = release_operator._private_file_attestation(  # noqa: SLF001
        Path(settings.database_path)
    )
    return operator.ExternalBackupReceipt(
        schema=operator.EXTERNAL_BACKUP_SCHEMA,
        manifest_path=payload.directory / "manifest.json",
        manifest_sha256=hashlib.sha256((payload.directory / "manifest.json").read_bytes()).hexdigest(),
        database_files_sha256=backup.receipt_sha256,
        inbox_files_sha256=backup.inbox_receipt_sha256,
        live_database_identity=live_identity,
        live_database_sha256=live_sha256,
    )


def _raw(
    storage,
    *,
    tenant: str = "alice",
    uploader: str = "alice",
    content_hash: str = "",
) -> str:
    storage.ensure_user(tenant, preset_key="owner")
    if uploader != tenant:
        storage.ensure_user(uploader, preset_key="user")
    raw_id = new_id("raw")
    raw = RawObject(
        id=raw_id,
        user_id=tenant,
        source="upload",
        source_ref=f"uploader:canonical:telegram-file:{raw_id}",
        raw_content="canonical body",
        content_type="file",
        metadata_json={
            "filename": "7849.odt",
            "mime_type": "application/vnd.oasis.opendocument.text",
            "uploaded_by": uploader,
        },
        content_hash=content_hash,
    )
    storage.store_raw_object(raw)
    return raw.id


def _notice_and_aliases(
    storage,
    raw_id: str,
    *,
    filename: str,
    timestamp: str,
    suffix: str,
    tenant: str = "alice",
    uploader: str = "alice",
    attached_ids: list[str] | None = None,
    uploaded_ids: list[str] | None = None,
    supplied_filename: str = "",
    extra_file_alias: bool = False,
    file_ref_override: str = "",
    message_ref_override: str = "",
    alias_timestamp: str = "",
) -> tuple[str, str]:
    conversation = storage.create_conversation(tenant)
    attached = [raw_id] if attached_ids is None else attached_ids
    uploaded = [raw_id] if uploaded_ids is None else uploaded_ids
    message = storage.store_message(
        conversation["id"],
        tenant,
        "user",
        f"Загружен документ: {filename}",
        metadata={
            "synthetic_document_notice": True,
            "conversation_attachment_raw_ids": attached,
            "conversation_uploaded_raw_ids": uploaded,
        },
    )
    file_ref = file_ref_override or f"telegram-file:FILE-{suffix}"
    aliases = [
        (file_ref, supplied_filename),
        (message_ref_override or f"telegram-message:42:{1000 + int(suffix)}", ""),
        (f"telegram-unique:UNIQUE-{suffix}", ""),
    ]
    if extra_file_alias:
        aliases.append((f"telegram-file:FILE-EXTRA-{suffix}", ""))
    with storage.transaction() as conn:
        conn.execute("UPDATE messages SET created_at=? WHERE id=?", (timestamp, message["id"]))
        for source_ref, alias_name in aliases:
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (tenant, uploader, source_ref, raw_id, alias_name, alias_timestamp or timestamp),
            )
    return str(message["id"]), file_ref


def _plan(settings, *, tenant: str = "alice", uploader: str = "alice"):
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        return build_plan(
            conn,
            tenant_id=tenant,
            owner_id=tenant,
            uploader_id=uploader,
        )
    finally:
        conn.close()


def _claim(tmp_path: Path, plan) -> Path:
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "claim.json"
    _write_private_json(
        path,
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
    return path


def test_unique_synthetic_notice_repairs_alias_but_not_canonical_raw(
    settings,
    storage,
    tmp_path,
) -> None:
    raw_id = _raw(storage)
    _message_id, file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 1
    assert plan.counts["planned_updates"] == 1
    public = json.dumps(plan.public_summary(mode="preview"), ensure_ascii=False)
    assert "666.odt" not in public
    assert file_ref not in public
    assert raw_id not in public

    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    applied, evidence = apply_plan(
        Path(settings.database_path),
        tenant_id="alice",
        owner_id="alice",
        uploader_id="alice",
        claim_manifest=claim,
        expected_count=1,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backups,
    )
    assert applied.candidate_count == evidence["applied_count"] == 1
    assert len(evidence["backup_manifest_sha256"]) == 64
    assert len(evidence["backup_database_sha256"]) == 64
    assert len(evidence["writer_quiescence_sha256"]) == 64
    exact = storage.find_owned_files_by_filename("alice", "alice", "666.odt")
    assert [(row["id"], row["filename"]) for row in exact] == [(raw_id, "666.odt")]
    canonical = storage.get_raw_object(raw_id, "alice")
    assert canonical is not None
    assert json.loads(canonical["metadata_json"])["filename"] == "7849.odt"
    assert _plan(settings).candidate_count == 0


def test_repair_succeeds_when_exact_target_ranks_after_public_ambiguity_page(
    settings,
    storage,
    tmp_path,
) -> None:
    older = _raw(storage)
    middle = _raw(storage)
    target = _raw(storage)
    with storage.transaction() as conn:
        for raw_id, received_at in (
            (older, "2026-08-17T10:00:00+00:00"),
            (middle, "2026-08-18T10:00:00+00:00"),
            (target, "2026-08-19T10:00:00+00:00"),
        ):
            row = conn.execute(
                "SELECT metadata_json FROM raw_objects WHERE id=?",
                (raw_id,),
            ).fetchone()
            assert row is not None
            metadata = json.loads(row["metadata_json"])
            if raw_id != target:
                metadata["filename"] = "shared-name.odt"
            conn.execute(
                "UPDATE raw_objects SET metadata_json=?,received_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), received_at, raw_id),
            )
    _notice_and_aliases(
        storage,
        target,
        filename="shared-name.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    before = storage.find_owned_files_by_filename("alice", "alice", "shared-name.odt")
    assert [row["id"] for row in before] == [older, middle]

    plan = _plan(settings)
    assert plan.candidate_count == 1
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    applied, evidence = apply_plan(
        Path(settings.database_path),
        tenant_id="alice",
        owner_id="alice",
        uploader_id="alice",
        claim_manifest=claim,
        expected_count=1,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backups,
    )

    assert applied.candidate_count == evidence["applied_count"] == 1
    bounded = storage.find_owned_files_by_filename("alice", "alice", "shared-name.odt")
    assert [row["id"] for row in bounded] == [older, middle]
    alias = storage.execute(
        """SELECT supplied_filename FROM file_source_aliases
            WHERE raw_object_id=? AND source_ref=?""",
        (target, plan.candidates[0].source_ref),
    ).fetchone()
    assert alias is not None and alias["supplied_filename"] == "shared-name.odt"
    assert _plan(settings).candidate_count == 0


def test_release_operator_can_apply_exact_claim_under_same_two_held_leases(
    settings,
    storage,
    tmp_path,
) -> None:
    raw_id = _raw(storage)
    _message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    external = _external_cutover_receipt(settings, tmp_path)
    backend = ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1")
    bridge = ProcessLease(
        settings.state_dir / "telegram-inbox.sqlite3.lock",
        protocol="friday.telegram-bridge.v1",
    )
    with backend, bridge:
        applied, evidence = apply_plan_under_held_leases(
            Path(settings.database_path),
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backend_lease=backend,
            bridge_lease=bridge,
            verified_backup_receipt=external,
        )
    assert applied.candidate_count == evidence["applied_count"] == 1
    assert evidence["applied_plan_sha256"] == plan.plan_sha256
    assert storage.find_owned_files_by_filename("alice", "alice", "666.odt")[0]["id"] == raw_id


def test_under_held_apply_rejects_missing_exact_sibling_lease_before_mutation(
    settings,
    storage,
    tmp_path,
) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    external = _external_cutover_receipt(settings, tmp_path)
    backend = ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1")
    bridge = ProcessLease(
        settings.state_dir / "telegram-inbox.sqlite3.lock",
        protocol="friday.telegram-bridge.v1",
    )
    with backend, pytest.raises(ContractError, match="both exact writer leases"):
        apply_plan_under_held_leases(
            Path(settings.database_path),
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backend_lease=backend,
            bridge_lease=bridge,
            verified_backup_receipt=external,
        )
    assert _plan(settings).candidate_count == 1


def test_distinct_reuploads_keep_multiple_names_for_one_deduplicated_raw(settings, storage) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="78.odt",
        timestamp="2026-08-17T13:21:02+00:00",
        suffix="1476",
    )
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 2
    assert {candidate.filename for candidate in plan.candidates} == {"78.odt", "666.odt"}
    assert len({candidate.source_ref for candidate in plan.candidates}) == 2


@pytest.mark.parametrize(
    ("attached", "uploaded"),
    [([], []), (["raw_other"], ["raw_other"]), (None, ["raw_other"])],
)
def test_missing_or_mismatched_singleton_lineage_is_never_a_candidate(
    settings,
    storage,
    attached,
    uploaded,
) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
        attached_ids=attached,
        uploaded_ids=uploaded,
    )
    assert _plan(settings).candidate_count == 0


@pytest.mark.parametrize("filename", ["bad/name.odt", "bad\\name.odt", "bad\nname.odt"])
def test_malformed_notice_basename_is_never_a_candidate(settings, storage, filename: str) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename=filename,
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    assert _plan(settings).candidate_count == 0


def test_telegram_carrier_shape_and_nearby_notices_cannot_cross_bind(settings, storage) -> None:
    raw_id = _raw(storage)
    first_message, _first_file = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
        extra_file_alias=True,
    )
    plan = _plan(settings)
    assert plan.candidate_count == 1
    assert plan.candidates[0].source_ref == f"friday-message-name:{first_message}"

    second_message, _second_file = _notice_and_aliases(
        storage,
        raw_id,
        filename="other.odt",
        timestamp="2026-08-19T17:46:41+00:00",
        suffix="1963",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 2
    assert {
        (candidate.message_id, candidate.filename, candidate.source_ref) for candidate in plan.candidates
    } == {
        (first_message, "666.odt", f"friday-message-name:{first_message}"),
        (second_message, "other.odt", f"friday-message-name:{second_message}"),
    }


def test_crossed_partial_persistence_inserts_message_alias_never_guessed_carrier(
    settings,
    storage,
    tmp_path,
) -> None:
    raw_id = _raw(storage)
    message_id, guessed_file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="notice-B.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    # Simulate crossed partial persistence: B retained only its exact notice;
    # the sole Telegram carrier set at the same time belongs to upload A.
    with storage.transaction() as conn:
        conn.execute("DELETE FROM file_source_aliases WHERE user_id='alice' AND uploaded_by='alice'")
        for source_ref in (
            "telegram-file:CARRIER-A",
            "telegram-message:42:1111",
            "telegram-unique:UNIQUE-A",
        ):
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
                   ) VALUES('alice','alice',?,?, '',?)""",
                (source_ref, raw_id, "2026-08-19T17:46:40+00:00"),
            )
    plan = _plan(settings)
    assert plan.candidate_count == 1
    assert plan.candidates[0].source_ref == f"friday-message-name:{message_id}"
    assert plan.candidates[0].source_ref != guessed_file_ref

    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    apply_plan(
        Path(settings.database_path),
        tenant_id="alice",
        owner_id="alice",
        uploader_id="alice",
        claim_manifest=claim,
        expected_count=1,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backups,
    )
    carrier = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref='telegram-file:CARRIER-A'"
    ).fetchone()
    derived = storage.execute(
        "SELECT raw_object_id,supplied_filename FROM file_source_aliases WHERE source_ref=?",
        (f"friday-message-name:{message_id}",),
    ).fetchone()
    assert carrier is not None and carrier["supplied_filename"] == ""
    assert derived is not None
    assert (derived["raw_object_id"], derived["supplied_filename"]) == (raw_id, "notice-B.odt")


def test_existing_telegram_name_is_immutable_but_does_not_own_message_name(settings, storage) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
        supplied_filename="already.odt",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 1
    assert plan.candidates[0].source_ref.startswith("friday-message-name:msg_")
    existing = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref='telegram-file:FILE-1962'"
    ).fetchone()
    assert existing is not None and existing["supplied_filename"] == "already.odt"


def test_ignored_raw_is_denied_and_shared_tenant_uploader_is_exact(settings, storage) -> None:
    ignored = _raw(storage)
    _notice_and_aliases(
        storage,
        ignored,
        filename="ignored.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO inbox(
                   id,user_id,raw_object_id,status,suggested_tags_json,suggestions_json,
                   suggested_action,promotion_score,quality_score,classification_notes,created_at
               ) VALUES(?,?,?,'ignored','[]','{}','review',0,0,'',?)""",
            (new_id("inbox"), "alice", ignored, "2026-08-19T17:46:40+00:00"),
        )
    foreign = _raw(storage, uploader="bob")
    _notice_and_aliases(
        storage,
        foreign,
        filename="foreign.odt",
        timestamp="2026-08-19T18:46:40+00:00",
        suffix="1963",
        uploader="bob",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 0
    assert plan.counts["refused_raw_authority"] == 2
    shared = _plan(settings, uploader="bob")
    assert shared.candidate_count == 1
    assert shared.candidates[0].raw_id == foreign
    assert shared.candidates[0].uploader_id == "bob"


def test_shared_tenant_uploader_apply_is_message_and_raw_bound(settings, storage, tmp_path) -> None:
    raw_id = _raw(storage, uploader="bob")
    message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="actor-document.odt",
        timestamp="2026-08-19T18:46:40+00:00",
        suffix="1963",
        uploader="bob",
    )
    plan = _plan(settings, uploader="bob")
    assert plan.candidate_count == 1
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    applied, _evidence = apply_plan(
        Path(settings.database_path),
        tenant_id="alice",
        owner_id="alice",
        uploader_id="bob",
        claim_manifest=claim,
        expected_count=1,
        expected_plan_sha256=plan.plan_sha256,
        backup_dir=backups,
    )
    assert applied.candidate_count == 1
    derived = storage.execute(
        """SELECT user_id,uploaded_by,raw_object_id,supplied_filename
             FROM file_source_aliases WHERE source_ref=?""",
        (f"friday-message-name:{message_id}",),
    ).fetchone()
    assert derived is not None
    assert tuple(derived) == ("alice", "bob", raw_id, "actor-document.odt")
    raw_row = storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_id,)).fetchone()
    assert raw_row is not None
    metadata = json.loads(raw_row["metadata_json"])
    metadata["uploaded_by"] = "alice"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), raw_id),
        )
    with pytest.raises(UnsupportedSchemaVersionError, match="data violates"):
        FridayStorage._validate_file_source_alias_schema(storage.conn)  # noqa: SLF001


def test_shared_uploader_cannot_cross_tenant_raw_or_message_authority(settings, storage) -> None:
    foreign_raw = _raw(storage, tenant="carol", uploader="bob")
    storage.ensure_user("alice", preset_key="owner")
    message_id, _file_ref = _notice_and_aliases(
        storage,
        foreign_raw,
        filename="foreign-tenant.odt",
        timestamp="2026-08-19T19:46:40+00:00",
        suffix="1964",
        tenant="alice",
        uploader="bob",
    )
    plan = _plan(settings, uploader="bob")
    assert plan.candidate_count == 0
    assert plan.counts["refused_raw_authority"] == 1
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','bob',?,?,?,'2026-08-19T19:46:40+00:00')""",
            (f"friday-message-name:{message_id}", foreign_raw, "foreign-tenant.odt"),
        )


def test_derived_alias_rejects_uploader_claim_not_owned_by_raw(storage) -> None:
    raw_id = _raw(storage)
    storage.ensure_user("bob", preset_key="user")
    message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="alice-only.odt",
        timestamp="2026-08-19T20:46:40+00:00",
        suffix="1966",
    )
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','bob',?,?,?,'2026-08-19T20:46:40+00:00')""",
            (f"friday-message-name:{message_id}", raw_id, "alice-only.odt"),
        )


def test_claim_is_cas_bound_and_backup_precedes_any_update(settings, storage, tmp_path) -> None:
    raw_id = _raw(storage)
    _message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    candidate = plan.candidates[0]
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','alice',?,?,?,?)""",
            (candidate.source_ref, raw_id, candidate.filename, candidate.created_at),
        )
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    with pytest.raises(ContractError, match="claim manifest/count/checksum"):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )
    assert list(backups.iterdir()) == []
    row = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref=?",
        (plan.candidates[0].source_ref,),
    ).fetchone()
    assert row["supplied_filename"] == "666.odt"


def test_standalone_apply_cannot_race_immutable_release_controller(settings, storage, tmp_path) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="locked.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1965",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    lock_path = settings.state_dir / "immutable-release-operator.v1.lock"
    with (
        release_operator.OperatorTransactionLock(lock_path),
        pytest.raises(ContractError, match="release operation is in progress"),
    ):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )
    assert list(backups.iterdir()) == []
    assert _plan(settings).plan_sha256 == plan.plan_sha256


def test_telegram_alias_timestamp_is_not_a_message_name_identity_surface(settings, storage) -> None:
    raw_id = _raw(storage)
    _message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
        alias_timestamp="2026-08-19T17:46:41+00:00",
    )
    plan = _plan(settings)
    assert plan.candidate_count == 1
    assert plan.candidates[0].created_at == "2026-08-19T17:46:40+00:00"


def test_exact_public_eligibility_failure_rolls_back_repair_after_verified_backup(
    settings,
    storage,
    tmp_path,
    monkeypatch,
) -> None:
    raw_id = _raw(storage)
    _message_id, file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "_exact_alias_is_publicly_eligible", lambda *_args: False)
    with pytest.raises(ContractError, match="exact public filename eligibility"):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )
    assert list(backups.glob("*.manifest.json"))
    row = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref=?",
        (file_ref,),
    ).fetchone()
    assert row["supplied_filename"] == ""


@pytest.mark.parametrize("tamper", ["content_hash", "message_id"])
def test_noncanonical_identity_surfaces_fail_closed(settings, storage, tamper: str) -> None:
    raw_id = _raw(storage, content_hash="0" * 63) if tamper == "content_hash" else _raw(storage)
    if tamper == "message_id":
        conversation = storage.create_conversation("alice")
        timestamp = "2026-08-19T17:46:40+00:00"
        metadata = json.dumps(
            {
                "synthetic_document_notice": True,
                "conversation_attachment_raw_ids": [raw_id],
                "conversation_uploaded_raw_ids": [raw_id],
            },
            sort_keys=True,
        )
        with storage.transaction() as conn:
            trigger = conn.execute(
                """SELECT sql FROM sqlite_master WHERE type='trigger'
                     AND name='conversation_passage_message_bi_identity_immutable'"""
            ).fetchone()
            assert trigger is not None and isinstance(trigger[0], str)
            conn.execute("DROP TRIGGER conversation_passage_message_bi_identity_immutable")
            conn.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,created_at
                   ) VALUES('message_not_canonical',?,'alice','user',?,?,?)""",
                (conversation["id"], "Загружен документ: 666.odt", metadata, timestamp),
            )
            conn.execute(str(trigger[0]))  # nosec B608 - exact authenticated SQLite DDL
            for source_ref in (
                "telegram-file:FILE-1962",
                "telegram-message:42:2962",
                "telegram-unique:UNIQUE-1962",
            ):
                conn.execute(
                    """INSERT INTO file_source_aliases(
                           user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
                       ) VALUES('alice','alice',?,?, '',?)""",
                    (source_ref, raw_id, timestamp),
                )
    else:
        _notice_and_aliases(
            storage,
            raw_id,
            filename="666.odt",
            timestamp="2026-08-19T17:46:40+00:00",
            suffix="1962",
        )
    assert _plan(settings).candidate_count == 0


def test_running_backend_lease_blocks_apply_before_backup(settings, storage, tmp_path) -> None:
    raw_id = _raw(storage)
    _message_id, file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    with (
        ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1"),
        pytest.raises(ContractError, match="not quiesced"),
    ):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )
    assert list(backups.iterdir()) == []
    row = storage.execute(
        "SELECT supplied_filename FROM file_source_aliases WHERE source_ref=?",
        (file_ref,),
    ).fetchone()
    assert row["supplied_filename"] == ""


def test_replaced_lease_identity_blocks_apply_and_releases_guard(
    settings,
    storage,
    tmp_path,
    monkeypatch,
) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    monkeypatch.setattr(operator, "_lease_is_exactly_held", lambda _lease: False)
    with pytest.raises(ContractError, match="identity is not exact"):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )
    assert list(backups.iterdir()) == []
    # The failed acquisition must not strand either process role.
    with ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1"):
        pass


def test_current_schema_with_missing_alias_guard_is_rejected(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="owner")
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER file_source_alias_filename_insert_guard")
    conn = _connect(Path(settings.database_path), read_only=True)
    try:
        with pytest.raises(ContractError, match="invariant"):
            build_plan(conn, tenant_id="alice", owner_id="alice", uploader_id="alice")
    finally:
        conn.close()


def test_apply_refuses_public_claim_file(settings, storage, tmp_path) -> None:
    raw_id = _raw(storage)
    _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    plan = _plan(settings)
    claim = _claim(tmp_path, plan)
    claim.chmod(0o644)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    with pytest.raises(ContractError, match="private regular"):
        apply_plan(
            Path(settings.database_path),
            tenant_id="alice",
            owner_id="alice",
            uploader_id="alice",
            claim_manifest=claim,
            expected_count=1,
            expected_plan_sha256=plan.plan_sha256,
            backup_dir=backups,
        )


def test_direct_sql_control_name_remains_guarded(storage) -> None:
    raw_id = _raw(storage)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','alice','telegram-file:BAD',?,?,'2026-08-19T00:00:00Z')""",
            (raw_id, "bad\rname.odt"),
        )


def test_derived_alias_requires_exact_synthetic_message_and_public_binder_stays_closed(storage) -> None:
    raw_id = _raw(storage)
    bogus_ref = "friday-message-name:msg_0000000000000000"
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','alice',?,?,?,'2026-08-19T00:00:00Z')""",
            (bogus_ref, raw_id, "bogus.odt"),
        )
    assert not storage.bind_owned_file_source_ref_alias(
        "alice",
        "alice",
        bogus_ref,
        raw_id,
        supplied_filename="bogus.odt",
    )
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','alice',?,?,'','2026-08-19T00:00:00Z')""",
            (bogus_ref, raw_id),
        )


def test_alias_identity_is_immutable_for_every_provenance_field(storage) -> None:
    raw_id = _raw(storage)
    other_raw_id = _raw(storage)
    storage.ensure_user("bob", preset_key="user")
    _message_id, source_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    mutations = (
        ("UPDATE file_source_aliases SET user_id='bob' WHERE source_ref=?", (source_ref,)),
        ("UPDATE file_source_aliases SET uploaded_by='bob' WHERE source_ref=?", (source_ref,)),
        (
            "UPDATE file_source_aliases SET source_ref='telegram-file:OTHER' WHERE source_ref=?",
            (source_ref,),
        ),
        (
            "UPDATE file_source_aliases SET raw_object_id=? WHERE source_ref=?",
            (other_raw_id, source_ref),
        ),
        (
            "UPDATE file_source_aliases SET created_at='2026-08-19T17:46:41+00:00' WHERE source_ref=?",
            (source_ref,),
        ),
    )
    for sql, params in mutations:
        with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
            conn.execute(sql, params)
    exact = storage.execute(
        """SELECT user_id,uploaded_by,source_ref,raw_object_id,created_at
             FROM file_source_aliases WHERE source_ref=?""",
        (source_ref,),
    ).fetchone()
    assert exact is not None
    assert tuple(exact) == (
        "alice",
        "alice",
        source_ref,
        raw_id,
        "2026-08-19T17:46:40+00:00",
    )


def test_current_schema_revalidates_existing_message_alias_linkage(storage) -> None:
    raw_id = _raw(storage)
    other_raw_id = _raw(storage)
    message_id, _file_ref = _notice_and_aliases(
        storage,
        raw_id,
        filename="666.odt",
        timestamp="2026-08-19T17:46:40+00:00",
        suffix="1962",
    )
    source_ref = f"friday-message-name:{message_id}"
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO file_source_aliases(
                   user_id,uploaded_by,source_ref,raw_object_id,supplied_filename,created_at
               ) VALUES('alice','alice',?,?,?,'2026-08-19T17:46:40+00:00')""",
            (source_ref, raw_id, "666.odt"),
        )
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='trigger' AND name='file_source_alias_identity_update_guard'"""
        ).fetchone()
        assert trigger is not None and trigger["sql"]
        conn.execute("DROP TRIGGER file_source_alias_identity_update_guard")
        conn.execute(
            "UPDATE file_source_aliases SET raw_object_id=? WHERE source_ref=?",
            (other_raw_id, source_ref),
        )
        conn.execute(str(trigger["sql"]))

    with pytest.raises(UnsupportedSchemaVersionError, match="data violates"):
        FridayStorage._validate_file_source_alias_schema(storage.conn)  # noqa: SLF001
