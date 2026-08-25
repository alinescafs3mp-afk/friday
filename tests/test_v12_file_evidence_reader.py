from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.file_delivery import FileRecordUnavailable, read_authorized_file_in_transaction
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PinnedFileEvidenceReference,
    historical_file_selection_token,
    prepare_current_turn_file_evidence,
    prepare_pinned_file_evidence,
    prepared_file_evidence_is_process_owned,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration import ReadOnlyAttachmentReference
from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import raw_source_identity_sha256
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id


def _actor(*, tenant: str = "alice", person: str = "") -> ActorContext:
    return ActorContext(
        user_id=tenant,
        preset_key="owner",
        source="v12-file-reader-test",
        shared_tenant=bool(person),
        person_id=person,
    )


def _text_digest(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


def _register(
    storage: Any,
    settings: Any,
    *,
    user_id: str = "alice",
    uploaded_by: str = "alice",
    text: str = "SOURCE-V12",
    filename: str = "source.txt",
    mime_type: str = "text/plain",
    source_bytes: bytes | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[RawObject, ReadOnlyAttachmentReference]:
    storage.ensure_user(user_id, preset_key="owner")
    storage.ensure_user(uploaded_by, preset_key="owner")
    content = source_bytes if source_bytes is not None else text.encode()
    digest = hashlib.sha256(content).hexdigest()
    relative = f"{user_id}/{digest[:2]}/{digest}.bin"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    metadata = {
        "filename": filename,
        "mime_type": mime_type,
        "stored_path": relative,
        "sha256": digest,
        "size_bytes": len(content),
        "uploaded_by": uploaded_by,
        "extraction_receipt_version": 1,
        "extraction_success": True,
        "extraction_error": "",
        "text_extraction_success": bool(text.strip()),
        "text_sha256": _text_digest(text),
        "extraction_chars": len(text),
        "text_truncated": False,
        "archive_truncated": False,
        "source_truncated_for_parse": False,
        "parse_deadline_reached": False,
        "parse_pages_read": 0,
        "parse_pages_truncated": False,
        "parse_total_pages": 0,
        "vision_pages_total": 0,
        "vision_pages_read": 0,
        "archive_files": 0,
        "archive_files_read": 0,
        "vision_used": False,
        "vision_review_required": False,
        "unsupported_format": False,
        **(extra or {}),
    }
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        content_hash=digest,
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    row = storage.execute(
        """SELECT id, user_id, source, source_ref, content_type, received_at,
                  content_hash, raw_content AS _raw_content,
                  metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=?""",
        (raw.id,),
    ).fetchone()
    assert row is not None
    reference = ReadOnlyAttachmentReference(
        ordinal=1,
        raw_object_id=raw.id,
        source_identity_sha256=raw_source_identity_sha256(dict(row)),
        name=filename,
        media_type=mime_type,
    )
    return raw, reference


def _prepare(storage: Any, settings: Any, references: tuple[ReadOnlyAttachmentReference, ...]):
    normalized = tuple(replace(reference, ordinal=index) for index, reference in enumerate(references, 1))
    return prepare_current_turn_file_evidence(
        storage,
        AuthorizationService(storage),
        settings.files_dir,
        _actor(),
        normalized,
        max_bytes=settings.max_upload_bytes,
    )


async def _ingest_reference(
    storage: Any,
    settings: Any,
    *,
    content: bytes,
    filename: str,
    mime_type: str,
) -> ReadOnlyAttachmentReference:
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        "alice",
        None,
        content,
        filename=filename,
        mime_type=mime_type,
        metadata={"uploaded_by": "alice"},
        source_ref=f"v12-real-ingest:{filename}",
    )
    raw_id = str(result["raw_object_id"])
    row = storage.execute(
        """SELECT id, user_id, source, source_ref, content_type, received_at,
                  content_hash, raw_content AS _raw_content,
                  metadata_json AS _raw_metadata
             FROM raw_objects WHERE id=?""",
        (raw_id,),
    ).fetchone()
    assert row is not None
    return ReadOnlyAttachmentReference(
        ordinal=1,
        raw_object_id=raw_id,
        source_identity_sha256=raw_source_identity_sha256(dict(row)),
        name=filename,
        media_type=mime_type,
    )


def test_current_turn_native_files_form_one_process_owned_bundle(settings, storage) -> None:
    _, first = _register(
        storage,
        settings,
        text="Первый документ: ALPHA-V12.",
        filename="alpha.txt",
    )
    _, second = _register(
        storage,
        settings,
        text="Второй документ: BETA-V12.",
        filename="beta.txt",
    )

    prepared = _prepare(storage, settings, (first, second))

    assert prepared_file_evidence_is_process_owned(prepared)
    assert prepared.raw_ids == (first.raw_object_id, second.raw_object_id)
    assert prepared.file_evidence_set.verification_complete is True
    assert [part.label for part in prepared.bundle.parts] == ["A1", "A2"]
    assert [part.text for part in prepared.bundle.parts] == [
        "Первый документ: ALPHA-V12.",
        "Второй документ: BETA-V12.",
    ]
    assert prepared.bundle.citation_labels == ("A1", "A2")
    assert prepared.identity_sha256 == prepared.bundle.identity_sha256()

    with storage.transaction() as conn:
        assert reauthorize_prepared_file_evidence_in_transaction(
            conn,
            AuthorizationService(storage),
            settings.files_dir,
            _actor(),
            prepared,
            max_bytes=settings.max_upload_bytes,
        )


def test_durable_raw_pin_is_reprepared_after_process_boundary(settings, storage) -> None:
    raw, current = _register(
        storage,
        settings,
        user_id="shared-tenant",
        uploaded_by="person-a",
        text="Durable comparison document.",
        filename="durable.txt",
    )
    reference = PinnedFileEvidenceReference(
        raw_object_id=raw.id,
        source_identity_sha256=current.source_identity_sha256,
        content_sha256=hashlib.sha256(b"Durable comparison document.").hexdigest(),
    )

    prepared = prepare_pinned_file_evidence(
        storage,
        AuthorizationService(storage),
        settings.files_dir,
        _actor(tenant="shared-tenant", person="person-a"),
        uploaded_by="person-a",
        reference=reference,
        max_bytes=settings.max_upload_bytes,
    )

    assert prepared_file_evidence_is_process_owned(prepared)
    assert prepared.raw_ids == (raw.id,)
    assert prepared.bundle.parts[0].text == "Durable comparison document."
    assert prepared.historical_selection is None
    with storage.transaction() as conn:
        assert reauthorize_prepared_file_evidence_in_transaction(
            conn,
            AuthorizationService(storage),
            settings.files_dir,
            _actor(tenant="shared-tenant", person="person-a"),
            prepared,
            max_bytes=settings.max_upload_bytes,
        )


def test_durable_raw_pin_rejects_identity_content_and_uploader_drift(settings, storage) -> None:
    raw, current = _register(storage, settings, text="Pinned source.", filename="pin.txt")
    good = PinnedFileEvidenceReference(
        raw_object_id=raw.id,
        source_identity_sha256=current.source_identity_sha256,
        content_sha256=hashlib.sha256(b"Pinned source.").hexdigest(),
    )
    actor = _actor()
    authorization = AuthorizationService(storage)

    with pytest.raises(FileEvidenceUnavailable):
        prepare_pinned_file_evidence(
            storage,
            authorization,
            settings.files_dir,
            actor,
            uploaded_by="alice",
            reference=replace(good, source_identity_sha256="f" * 64),
            max_bytes=settings.max_upload_bytes,
        )
    with pytest.raises(FileEvidenceUnavailable, match="pinned_source_changed"):
        prepare_pinned_file_evidence(
            storage,
            authorization,
            settings.files_dir,
            actor,
            uploaded_by="alice",
            reference=replace(good, content_sha256="f" * 64),
            max_bytes=settings.max_upload_bytes,
        )
    storage.ensure_user("mallory", preset_key="owner")
    with pytest.raises(FileEvidenceUnavailable):
        prepare_pinned_file_evidence(
            storage,
            authorization,
            settings.files_dir,
            actor,
            uploaded_by="mallory",
            reference=good,
            max_bytes=settings.max_upload_bytes,
        )

    storage.execute(
        "UPDATE raw_objects SET raw_content=? WHERE id=?",
        ("Changed source.", raw.id),
    )
    with pytest.raises(FileEvidenceUnavailable):
        prepare_pinned_file_evidence(
            storage,
            authorization,
            settings.files_dir,
            actor,
            uploaded_by="alice",
            reference=good,
            max_bytes=settings.max_upload_bytes,
        )


@pytest.mark.asyncio
async def test_reader_contract_matches_real_ingestion_projections(settings, storage) -> None:
    plain = await _ingest_reference(
        storage,
        settings,
        content="Полный UTF-8 документ: SOURCE-REAL-V12".encode(),
        filename="real.txt",
        mime_type="text/plain",
    )
    assert _prepare(storage, settings, (plain,)).bundle.parts[0].text.endswith("SOURCE-REAL-V12")

    cp1251_text = "Документ CP1251"
    cp1251 = await _ingest_reference(
        storage,
        settings,
        content=cp1251_text.encode("cp1251"),
        filename="legacy-cp1251.txt",
        mime_type="text/plain",
    )
    with pytest.raises(FileEvidenceUnavailable, match="registered_bytes_are_not_exact_utf8_text"):
        _prepare(storage, settings, (cp1251,))

    empty = await _ingest_reference(
        storage,
        settings,
        content=b"",
        filename="empty-real.txt",
        mime_type="text/plain",
    )
    with pytest.raises(FileEvidenceUnavailable):
        _prepare(storage, settings, (empty,))

    from reportlab.pdfgen.canvas import Canvas

    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(72, 720, "Native PDF source remains legacy owned")
    canvas.save()
    pdf = await _ingest_reference(
        storage,
        settings,
        content=stream.getvalue(),
        filename="native.pdf",
        mime_type="application/pdf",
    )
    with pytest.raises(FileEvidenceUnavailable):
        _prepare(storage, settings, (pdf,))


def test_prepared_authority_rejects_cross_source_splicing(settings, storage) -> None:
    _, first = _register(storage, settings, text="SOURCE-A", filename="a.txt")
    _, second = _register(storage, settings, text="SOURCE-B", filename="b.txt")
    prepared_a = _prepare(storage, settings, (first,))
    prepared_b = _prepare(storage, settings, (second,))

    with pytest.raises(ValueError, match="identities disagree"):
        replace(
            prepared_a,
            raw_ids=prepared_b.raw_ids,
            snapshot_tokens=prepared_b.snapshot_tokens,
        )

    object.__setattr__(prepared_a, "_identity_sha256", "0" * 64)
    assert not prepared_file_evidence_is_process_owned(prepared_a)


def test_empty_and_visual_sources_remain_legacy_owned(settings, storage) -> None:
    _, empty = _register(storage, settings, text="", filename="empty.txt")
    with pytest.raises(FileEvidenceUnavailable, match="native_text_unavailable"):
        _prepare(storage, settings, (empty,))

    _, scan = _register(
        storage,
        settings,
        text="",
        filename="scan.pdf",
        mime_type="application/pdf",
    )
    with pytest.raises(FileEvidenceUnavailable):
        _prepare(storage, settings, (scan,))


@pytest.mark.parametrize(
    ("extra", "text", "reason"),
    [
        ({"extraction_receipt_version": 0}, "BODY", "extraction_receipt_unattested"),
        ({"extraction_receipt_version": True}, "BODY", "extraction_receipt_unattested"),
        ({"parse_deadline_reached": True}, "BODY", "native_extraction_incomplete_or_advisory"),
        ({"parse_pages_truncated": True}, "BODY", "native_extraction_incomplete_or_advisory"),
        ({"text_truncated": True}, "BODY", "native_extraction_incomplete_or_advisory"),
        ({"vision_review_required": True}, "BODY", "native_extraction_incomplete_or_advisory"),
        ({"transcription": {"model": "local"}}, "BODY", "native_extraction_incomplete_or_advisory"),
        ({"text_extraction_success": False}, "BODY", "native_text_not_attested"),
        ({"extraction_chars": 3}, "BODY", "native_extraction_length_mismatch"),
        ({"text_sha256": "0" * 64}, "BODY", "native_extraction_digest_mismatch"),
        ({}, "[File: source.bin]", "body_is_provenance_stub"),
    ],
)
def test_partial_advisory_or_ambiguous_extraction_falls_closed(
    settings,
    storage,
    extra: dict[str, Any],
    text: str,
    reason: str,
) -> None:
    _, reference = _register(storage, settings, text=text, extra=extra)
    with pytest.raises(FileEvidenceUnavailable, match=reason):
        _prepare(storage, settings, (reference,))


def test_receipt_requires_every_closed_flag_and_counter(settings, storage) -> None:
    for mutation in (
        "missing_flag",
        "string_flag",
        "missing_counter",
        "float_counter",
        "archive_count",
    ):
        _, reference = _register(
            storage,
            settings,
            text=f"STRICT-RECEIPT-{mutation}",
            filename=f"{mutation}.txt",
        )
        row = storage.get_raw_object(reference.raw_object_id, "alice")
        assert row is not None
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata = dict(metadata)
        if mutation == "missing_flag":
            metadata.pop("text_truncated")
        elif mutation == "string_flag":
            metadata["text_truncated"] = "false"
        elif mutation == "missing_counter":
            metadata.pop("parse_pages_read")
        elif mutation == "float_counter":
            metadata["parse_pages_read"] = 0.0
        else:
            metadata["archive_files"] = 1
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), reference.raw_object_id),
            )
        with pytest.raises(FileEvidenceUnavailable):
            _prepare(storage, settings, (reference,))


def test_pdf_remains_legacy_owned_until_per_page_native_coverage_exists(settings, storage) -> None:
    _, complete = _register(
        storage,
        settings,
        text="PDF-TEXT-LAYER",
        filename="complete.pdf",
        mime_type="application/pdf",
        extra={"parse_pages_read": 2, "parse_total_pages": 2},
    )
    with pytest.raises(FileEvidenceUnavailable, match="source_kind_not_in_native_text_canary"):
        _prepare(storage, settings, (complete,))

    _, partial = _register(
        storage,
        settings,
        text="PARTIAL-PDF-TEXT",
        filename="partial.pdf",
        mime_type="application/pdf",
        extra={"parse_pages_read": 1, "parse_total_pages": 2},
    )
    with pytest.raises(FileEvidenceUnavailable, match="source_kind_not_in_native_text_canary"):
        _prepare(storage, settings, (partial,))


@pytest.mark.parametrize(
    "binary_text",
    [
        "%PDF-1.7\n1 0 obj\nstream\nRASTER-PAGE-HIDDEN\nendstream",
        "PK\x03\x04renamed-office-container",
        "plain-prefix\x00binary-tail",
    ],
)
def test_renamed_binary_container_cannot_enter_native_text_canary(
    settings,
    storage,
    binary_text: str,
) -> None:
    _, reference = _register(
        storage,
        settings,
        text=binary_text,
        filename="renamed.txt",
        mime_type="text/plain",
    )
    with pytest.raises(FileEvidenceUnavailable, match="registered_bytes_are_not_exact_utf8_text"):
        _prepare(storage, settings, (reference,))


def test_non_utf8_registered_text_cleanly_remains_legacy_owned(settings, storage) -> None:
    text = "Договор в кодировке CP1251"
    _, reference = _register(
        storage,
        settings,
        text=text,
        source_bytes=text.encode("cp1251"),
    )

    with pytest.raises(FileEvidenceUnavailable, match="registered_bytes_are_not_exact_utf8_text"):
        _prepare(storage, settings, (reference,))


def test_current_runtime_secret_rotation_never_enters_model_evidence(
    settings,
    storage,
    monkeypatch,
) -> None:
    future_secret = "sk-friday-rotation-proof-1234567890"
    _, reference = _register(storage, settings, text=f"ordinary before rotation {future_secret}")
    monkeypatch.setenv("FRIDAY_API_TOKEN", future_secret)
    with pytest.raises(FileEvidenceUnavailable, match="body_requires_secret_projection"):
        _prepare(storage, settings, (reference,))


@pytest.mark.parametrize(
    "secret",
    [
        "sk-friday-filename-secret-1234567890",
        "jrc_DO_NOT_FORWARD_THIS_FILENAME_CREDENTIAL_1234567890",
    ],
)
def test_secret_bearing_filename_never_enters_model_evidence(
    settings,
    storage,
    monkeypatch,
    secret: str,
) -> None:
    monkeypatch.setenv("FRIDAY_API_TOKEN", secret)
    _, reference = _register(
        storage,
        settings,
        text="SAFE-SOURCE-BODY",
        filename=f"contract-{secret}.txt",
    )

    with pytest.raises(FileEvidenceUnavailable, match="source_descriptor_requires_secret_projection"):
        _prepare(storage, settings, (reference,))


def test_prepare_rejects_forged_identity_foreign_uploader_and_disk_swap(settings, storage) -> None:
    raw, reference = _register(storage, settings)
    forged = replace(reference, source_identity_sha256="0" * 64)
    with pytest.raises(FileEvidenceUnavailable, match="current_turn_source_changed"):
        _prepare(storage, settings, (forged,))

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=json_set(metadata_json, '$.uploaded_by', 'bob') WHERE id=?",
            (raw.id,),
        )
    storage.ensure_user("bob", preset_key="owner")
    with pytest.raises(FileEvidenceUnavailable, match="registered_file_unavailable"):
        _prepare(storage, settings, (reference,))

    _, fresh = _register(storage, settings, text="ORIGINAL-DISK")
    raw_row = storage.get_raw_object(fresh.raw_object_id, "alice")
    assert raw_row is not None
    metadata = raw_row["metadata_json"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    (Path(settings.files_dir) / metadata["stored_path"]).write_bytes(b"SWAPPED-DISK")
    with pytest.raises(FileEvidenceUnavailable, match="registered_file_unavailable"):
        _prepare(storage, settings, (fresh,))


@pytest.mark.parametrize("metadata_attack", ["duplicate_uploader", "oversized"])
def test_authorized_byte_primitive_rejects_ambiguous_uploader_metadata(
    settings,
    storage,
    metadata_attack: str,
) -> None:
    raw, _ = _register(storage, settings, text="PRIVATE-BYTES")
    row = storage.get_raw_object(raw.id, "alice")
    assert row is not None
    metadata = row["metadata_json"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    if metadata_attack == "duplicate_uploader":
        encoded = encoded.replace(
            '"uploaded_by":"alice"',
            '"uploaded_by":"alice","uploaded_by":"bob"',
            1,
        )
    else:
        metadata["padding"] = "x" * (129 * 1024)
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET metadata_json=? WHERE id=?", (encoded, raw.id))
    with storage.transaction() as conn, pytest.raises(FileRecordUnavailable):
        read_authorized_file_in_transaction(
            conn,
            settings.files_dir,
            raw.id,
            "alice",
            person_id="alice",
            max_bytes=settings.max_upload_bytes,
        )


@pytest.mark.parametrize(
    "mutation",
    ["deleted", "permission", "bytes", "uploader", "raw_content", "metadata", "suspended"],
)
def test_final_reauthorization_rejects_every_post_prepare_authority_change(
    settings,
    storage,
    mutation: str,
) -> None:
    raw, reference = _register(storage, settings, text="PREPARED-SOURCE")
    prepared = _prepare(storage, settings, (reference,))
    authorization = AuthorizationService(storage)
    if mutation == "deleted":
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET deleted_at='2026-08-18T00:00:00Z' WHERE id=?", (raw.id,))
    elif mutation == "permission":
        authorization.deny_permission("alice", "files.read")
    elif mutation == "bytes":
        row = storage.get_raw_object(raw.id, "alice")
        assert row is not None
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        (Path(settings.files_dir) / metadata["stored_path"]).write_bytes(b"POST-PREPARE-SWAP")
    elif mutation == "uploader":
        storage.ensure_user("bob", preset_key="owner")
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET metadata_json=json_set(metadata_json, '$.uploaded_by', 'bob') "
                "WHERE id=?",
                (raw.id,),
            )
    elif mutation == "raw_content":
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET raw_content='CHANGED' WHERE id=?", (raw.id,))
    elif mutation == "metadata":
        with storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET metadata_json=json_set(metadata_json, '$.filename', 'changed.txt') "
                "WHERE id=?",
                (raw.id,),
            )
    else:
        with storage.transaction() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id='alice'")

    with storage.transaction() as conn:
        assert not reauthorize_prepared_file_evidence_in_transaction(
            conn,
            authorization,
            settings.files_dir,
            _actor(),
            prepared,
            max_bytes=settings.max_upload_bytes,
        )


def test_final_reauthorization_rejects_post_prepare_privacy_quarantine(settings, storage) -> None:
    raw, reference = _register(storage, settings, text="PREPARED-PUBLIC-SOURCE")
    prepared = _prepare(storage, settings, (reference,))
    entity = Entity(
        id=new_id("ent"),
        user_id="alice",
        name="Private dependency",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(entity)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content="PREPARED-PUBLIC-SOURCE",
        content_type="text",
        title="Prepared source",
    )
    storage.store_knowledge_object(knowledge)
    storage.link_knowledge_entity("alice", knowledge.id, entity.id, status="accepted")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   VALUES(?, 'bob', 'reminder', '2026-08-18T00:00:00Z')""",
            (entity.id,),
        )

    with storage.transaction() as conn:
        assert not reauthorize_prepared_file_evidence_in_transaction(
            conn,
            AuthorizationService(storage),
            settings.files_dir,
            _actor(),
            prepared,
            max_bytes=settings.max_upload_bytes,
        )


def test_shared_tenant_requires_the_exact_current_uploader(settings, storage) -> None:
    storage.ensure_user("shared", preset_key="owner")
    storage.ensure_user("alice", preset_key="owner")
    storage.ensure_user("bob", preset_key="owner")
    _, reference = _register(
        storage,
        settings,
        user_id="shared",
        uploaded_by="alice",
        text="ALICE-PRIVATE-CURRENT-TURN",
    )
    authorization = AuthorizationService(storage, shared_tenant="shared")
    prepared = prepare_current_turn_file_evidence(
        storage,
        authorization,
        settings.files_dir,
        _actor(tenant="shared", person="alice"),
        (reference,),
        max_bytes=settings.max_upload_bytes,
    )
    assert prepared.person_id == "alice"

    with pytest.raises(FileEvidenceUnavailable, match="registered_file_unavailable"):
        prepare_current_turn_file_evidence(
            storage,
            authorization,
            settings.files_dir,
            _actor(tenant="shared", person="bob"),
            (reference,),
            max_bytes=settings.max_upload_bytes,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_ids", ["raw_0123456789abcdef"]),
        ("latest_count", True),
        ("received_since", 123),
        ("filename", b"source.txt"),
    ],
)
def test_historical_selection_token_rejects_noncanonical_runtime_types(field: str, value: object) -> None:
    token = historical_file_selection_token(
        tenant_id="alice",
        uploaded_by="alice",
        kind="latest",
        raw_ids=("raw_0123456789abcdef",),
        latest_count=1,
    )

    with pytest.raises(ValueError, match="historical file selector"):
        replace(token, **{field: value})
