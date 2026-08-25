"""Synthetic contract and mutation tests for Proposal 86's first audit slice."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from friday.storage import SCHEMA_VERSION
from tools.audit_document_catalog import (
    ContractError,
    _canonical_json,
    audit_document_catalog,
    main,
    open_offline_database,
    validate_report,
)

SECRET = "Сверхсекретный текст договора № 9917"


def _make_database(path: Path, *, lexical: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE users(id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE raw_objects(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_ref TEXT NOT NULL DEFAULT '',
            raw_content TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT 'text',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            received_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE TABLE knowledge_objects(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, raw_object_id TEXT);
        CREATE TABLE inbox(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            raw_object_id TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE private_entity_material_cache_state(
            singleton INTEGER PRIMARY KEY,
            valid INTEGER NOT NULL,
            prior_valid INTEGER NOT NULL
        );
        CREATE TABLE private_entity_material_derivative_state(
            singleton INTEGER PRIMARY KEY,
            valid INTEGER NOT NULL,
            prior_valid INTEGER NOT NULL
        );
        CREATE TABLE private_entity_material_derivative_cache(
            material_kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY(material_kind, object_id, user_id)
        ) WITHOUT ROWID;
        """
    )
    if lexical:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE raw_fts USING fts5(
                raw_content,
                content=raw_objects,
                content_rowid=rowid,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
    conn.execute(
        "INSERT INTO schema_meta VALUES('schema_version', ?, '2026-08-20T00:00:00Z')",
        (str(SCHEMA_VERSION),),
    )
    conn.executemany(
        "INSERT INTO users(id,status) VALUES(?,?)",
        (("alice", "active"), ("bob", "active"), ("mallory", "active"), ("eve", "active")),
    )
    conn.execute("INSERT INTO private_entity_material_cache_state VALUES(1,1,1)")
    conn.execute("INSERT INTO private_entity_material_derivative_state VALUES(1,1,1)")
    conn.commit()
    return conn


def _raw(
    conn: sqlite3.Connection,
    raw_id: str,
    *,
    tenant: str = "alice",
    uploader: str = "bob",
    text: str = SECRET,
    deleted: bool = False,
    audio: bool = False,
    public: bool = True,
    inbox_status: str | None = None,
) -> None:
    metadata = {
        "filename": f"{raw_id}-{SECRET}.pdf",
        "uploaded_by": uploader,
        "mime_type": "audio/ogg" if audio else "application/pdf",
        "media_kind": "audio" if audio else "document",
    }
    conn.execute(
        """INSERT INTO raw_objects(
               id,user_id,source,source_ref,raw_content,content_type,metadata_json,
               content_hash,version,received_at,created_at,deleted_at
           ) VALUES(?,?, 'upload', ?, ?, 'file', ?, ?, 1,
                    '2026-08-20T00:00:00Z','2026-08-20T00:00:00Z',?)""",
        (
            raw_id,
            tenant,
            f"transport-{raw_id}-{SECRET}",
            text,
            json.dumps(metadata, ensure_ascii=False),
            hashlib.sha256(f"bytes-{raw_id}".encode()).hexdigest(),
            "2026-08-20T01:00:00Z" if deleted else None,
        ),
    )
    if public:
        conn.execute(
            "INSERT INTO private_entity_material_derivative_cache VALUES('raw',?,?)",
            (raw_id, tenant),
        )
    if inbox_status is not None:
        inbox_id = f"inbox-{raw_id}"
        conn.execute(
            "INSERT INTO inbox(id,user_id,raw_object_id,status) VALUES(?,?,?,?)",
            (inbox_id, tenant, raw_id, inbox_status),
        )
        if public and inbox_status != "ignored":
            conn.execute(
                "INSERT INTO private_entity_material_derivative_cache VALUES('inbox',?,?)",
                (inbox_id, tenant),
            )


def _seed_scope(conn: sqlite3.Connection, *, lexical: bool = True) -> None:
    _raw(conn, "eligible")
    _raw(conn, "pending", inbox_status="pending")
    _raw(conn, "deleted", deleted=True)
    _raw(conn, "ignored", inbox_status="ignored")
    _raw(conn, "private", public=False)
    _raw(conn, "audio", audio=True)
    _raw(conn, "other-uploader", uploader="mallory")
    _raw(conn, "empty-ocr", text="")
    _raw(conn, "foreign", tenant="eve")
    if lexical:
        conn.execute("INSERT INTO raw_fts(rowid,raw_content) SELECT rowid,raw_content FROM raw_objects")
    conn.commit()


def _add_exact_catalog(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE document_catalog(
            raw_object_id TEXT PRIMARY KEY NOT NULL
                REFERENCES raw_objects(id) ON DELETE CASCADE,
            source_version INTEGER NOT NULL,
            source_content_sha256 TEXT NOT NULL,
            extracted_text_sha256 TEXT,
            semantic_title TEXT,
            title_authority TEXT NOT NULL CHECK(title_authority='navigation_only'),
            enrichment_revision INTEGER NOT NULL,
            enrichment_status TEXT NOT NULL CHECK(enrichment_status IN ('current','incomplete')),
            incomplete_reason TEXT,
            enriched_at TEXT NOT NULL,
            CHECK(
                (enrichment_status='current' AND extracted_text_sha256 IS NOT NULL
                 AND incomplete_reason IS NULL)
                OR
                (enrichment_status='incomplete' AND extracted_text_sha256 IS NULL
                 AND semantic_title IS NULL AND incomplete_reason IS NOT NULL)
            )
        );
        """
    )
    eligible = conn.execute(
        "SELECT version,content_hash,raw_content FROM raw_objects WHERE id='eligible'"
    ).fetchone()
    pending = conn.execute(
        "SELECT version,content_hash FROM raw_objects WHERE id='pending'"
    ).fetchone()
    conn.execute(
        """INSERT INTO document_catalog VALUES(
               'eligible',?,?,?,'Договор','navigation_only',1,
               'current',NULL,'2026-08-20T00:00:00+00:00'
           )""",
        (
            eligible["version"],
            eligible["content_hash"],
            hashlib.sha256(str(eligible["raw_content"]).encode()).hexdigest(),
        ),
    )
    conn.execute(
        """INSERT INTO document_catalog VALUES(
               'pending',?,?,NULL,NULL,'navigation_only',1,
               'incomplete','backfill_pending','2026-08-20T00:00:00+00:00'
           )""",
        (pending["version"], pending["content_hash"]),
    )
    conn.commit()


def _resign(report: dict[str, object]) -> dict[str, object]:
    mutated = copy.deepcopy(report)
    mutated.pop("report_sha256", None)
    mutated["report_sha256"] = hashlib.sha256(_canonical_json(mutated)).hexdigest()
    return mutated


def _audit_path(
    path: Path,
    *,
    tenant: str = "alice",
    uploader: str | None = "bob",
    max_rows: int = 100_000,
) -> dict[str, object]:
    os.chmod(path, 0o600)
    conn = open_offline_database(path)
    try:
        return audit_document_catalog(
            conn,
            tenant_id=tenant,
            uploader=uploader,
            max_rows=max_rows,
        )
    finally:
        conn.close()


def test_counts_authority_lifecycle_lexical_and_future_gaps_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.close()
    report = _audit_path(path, max_rows=20)

    assert report["counts"] == {
        "tenant_file_rows_examined": 8,
        "registered_authorized_live_text_bearing_files": 2,
        "pending_registered_files": 1,
        "catalogued_files": 0,
        "lexically_searchable_files": 2,
        "files_with_semantic_title": 0,
        "files_with_passages": 0,
        "files_with_current_embeddings": 0,
        "files_with_typed_dates": 0,
        "pending_files_with_semantic_index": 0,
        "files_with_stale_enrichment_revision": 0,
        "files_with_incomplete_catalog": 0,
        "files_with_index_incomplete_reason": 3,
        "files_excluded_by_policy": 5,
        "files_without_extracted_text": 1,
    }
    assert report["excluded_by_policy"] == {
        "deleted_lifecycle": 1,
        "ignored_lifecycle": 1,
        "private_authority": 1,
        "audio_document": 1,
        "uploader_authority": 1,
    }
    assert report["projections"]["lexical_source_index"]["status"] == "available"
    assert report["projections"]["document_catalog"]["status"] == "not_available"
    assert report["incomplete_reasons"]["catalog_projection_not_available"] == 2
    assert report["incomplete_reasons"]["pending_semantic_projection_not_available"] == 1
    assert report["completeness"]["status"] == "incomplete"
    validate_report(report)

    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in (SECRET, "eligible", "empty-ocr", str(tmp_path)):
        assert forbidden not in serialized


def test_missing_lexical_row_is_explicit_and_never_called_complete(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    row = conn.execute("SELECT rowid,raw_content FROM raw_objects WHERE id='pending'").fetchone()
    conn.execute(
        "INSERT INTO raw_fts(raw_fts,rowid,raw_content) VALUES('delete',?,?)",
        (row["rowid"], row["raw_content"]),
    )
    conn.commit()
    conn.close()
    report = _audit_path(path)
    assert report["counts"]["lexically_searchable_files"] == 1
    assert report["incomplete_reasons"]["lexical_row_missing"] == 1
    assert report["completeness"]["lexical_complete"] is False


def test_absent_lexical_projection_is_zero_not_available_not_complete(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path, lexical=False)
    _seed_scope(conn, lexical=False)
    conn.close()
    report = _audit_path(path)
    assert report["projections"]["lexical_source_index"] == {
        "status": "not_available",
        "revision_sha256": None,
    }
    assert report["counts"]["lexically_searchable_files"] == 0
    assert report["incomplete_reasons"]["lexical_projection_not_available"] == 2
    assert report["completeness"]["lexical_complete"] is False


def test_same_named_future_table_is_unsupported_not_guessed_as_coverage(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.execute("CREATE TABLE document_catalog(opaque_future_shape TEXT)")
    conn.commit()
    conn.close()
    report = _audit_path(path)
    projection = report["projections"]["document_catalog"]
    assert projection["status"] == "unsupported"
    assert len(projection["revision_sha256"]) == 64
    assert report["counts"]["catalogued_files"] == 0
    assert report["incomplete_reasons"]["catalog_projection_not_available"] == 2
    assert report["completeness"]["catalog_complete"] is False


def test_exact_catalog_separates_current_explicit_incomplete_and_semantic_coverage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    _add_exact_catalog(conn)
    conn.close()

    report = _audit_path(path)
    assert report["projections"]["document_catalog"]["status"] == "available"
    assert report["projections"]["semantic_title"]["status"] == "available"
    assert report["counts"]["catalogued_files"] == 2
    assert report["counts"]["files_with_semantic_title"] == 1
    assert report["counts"]["files_with_incomplete_catalog"] == 1
    assert report["incomplete_reasons"]["catalog_row_missing"] == 0
    assert report["incomplete_reasons"]["catalog_row_stale"] == 0
    assert report["incomplete_reasons"]["catalog_row_incomplete"] == 1
    assert report["incomplete_reasons"]["semantic_title_missing"] == 1
    assert report["completeness"]["catalog_complete"] is True
    assert report["completeness"]["semantic_complete"] is False
    validate_report(report)


def test_catalog_source_drift_is_stale_not_current_or_missing(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    _add_exact_catalog(conn)
    conn.execute("UPDATE document_catalog SET source_version=99 WHERE raw_object_id='pending'")
    conn.commit()
    conn.close()

    report = _audit_path(path)
    assert report["counts"]["catalogued_files"] == 1
    assert report["counts"]["files_with_stale_enrichment_revision"] == 1
    assert report["counts"]["files_with_incomplete_catalog"] == 0
    assert report["incomplete_reasons"]["catalog_row_missing"] == 0
    assert report["incomplete_reasons"]["catalog_row_stale"] == 1
    assert report["completeness"]["catalog_complete"] is False


def test_foreign_tenant_mutation_cannot_change_scoped_report(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.close()
    before = _audit_path(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _raw(conn, "foreign-second", tenant="eve", text=f"{SECRET} foreign mutation")
    foreign = conn.execute("SELECT rowid,raw_content FROM raw_objects WHERE id='foreign-second'").fetchone()
    conn.execute(
        "INSERT INTO raw_fts(rowid,raw_content) VALUES(?,?)",
        (foreign["rowid"], foreign["raw_content"]),
    )
    conn.commit()
    conn.close()
    after = _audit_path(path)
    assert after == before


@pytest.mark.parametrize(
    "mutation",
    (
        lambda r: r["counts"].__setitem__("catalogued_files", 1),
        lambda r: r["counts"].__setitem__("files_with_passages", 1),
        lambda r: r["counts"].__setitem__("pending_files_with_semantic_index", 1),
        lambda r: r["counts"].__setitem__("files_with_current_embeddings", 1),
        lambda r: r["counts"].__setitem__("files_with_typed_dates", 1),
        lambda r: r["counts"].__setitem__("files_with_stale_enrichment_revision", 1),
        lambda r: r["counts"].__setitem__("files_with_incomplete_catalog", 1),
        lambda r: r["incomplete_reasons"].__setitem__("catalog_row_missing", 1),
        lambda r: r["incomplete_reasons"].__setitem__("passage_projection_not_available", 0),
        lambda r: r["completeness"].__setitem__("status", "complete"),
        lambda r: r["completeness"].__setitem__("uncapped", False),
        lambda r: r["excluded_by_policy"].__setitem__("private_authority", 0),
    ),
)
def test_mutations_cannot_claim_missing_projection_stale_date_or_cap_complete(
    tmp_path: Path, mutation
) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.close()
    report = _audit_path(path)
    mutation(report)
    with pytest.raises(ContractError):
        validate_report(_resign(report))


def test_scan_bound_fails_closed_instead_of_emitting_partial_counts(tmp_path: Path) -> None:
    path = tmp_path / "synthetic.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.close()
    with pytest.raises(ContractError, match="row bound"):
        _audit_path(path, max_rows=7)


def test_offline_open_requires_private_quiescent_copy_and_is_query_only(tmp_path: Path) -> None:
    path = tmp_path / "offline.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    with pytest.raises(ContractError, match="explicit offline copy"):
        audit_document_catalog(conn, tenant_id="alice", uploader="bob")
    conn.close()
    os.chmod(path, 0o600)

    opened = open_offline_database(path)
    try:
        assert opened.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            opened.execute("DELETE FROM raw_objects")
        assert (
            audit_document_catalog(opened, tenant_id="alice", uploader="bob")["counts"][
                "registered_authorized_live_text_bearing_files"
            ]
            == 2
        )
    finally:
        opened.close()

    os.chmod(path, 0o644)
    with pytest.raises(ContractError, match="permissions"):
        open_offline_database(path)
    os.chmod(path, 0o600)
    sidecar = Path(f"{path}-wal")
    sidecar.write_bytes(b"")
    with pytest.raises(ContractError, match="sidecars"):
        open_offline_database(path)


def test_cli_requires_explicit_offline_attestation_and_emits_only_exact_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / f"{SECRET}.sqlite3"
    conn = _make_database(path)
    _seed_scope(conn)
    conn.close()
    os.chmod(path, 0o600)

    with pytest.raises(SystemExit):
        main(["--database", str(path), "--tenant", "alice"])
    capsys.readouterr()
    assert (
        main(
            [
                "--database",
                str(path),
                "--offline-copy",
                "--tenant",
                "alice",
                "--uploader",
                "bob",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    validate_report(payload)
    assert SECRET not in captured.out
    assert str(path) not in captured.out
