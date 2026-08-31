from __future__ import annotations

import json
import sqlite3
from unittest import mock

import pytest

import friday.storage._archive_search_documents as document_storage
from friday.retrieval.archive_search_contract import (
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
)
from friday.retrieval.contracts import TemporalPrecision, TemporalRole, TemporalValueKind
from friday.storage._core import iso_date


def _metadata_projection(metadata_json: str) -> tuple[int, str, str | None]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.create_function("jericho_iso_date", 1, iso_date, deterministic=True)
        conn.execute("CREATE TABLE source(metadata_json TEXT NOT NULL)")
        conn.execute("INSERT INTO source(metadata_json) VALUES(?)", (metadata_json,))
        catalog_valid_sql = document_storage._catalog_metadata_valid(  # noqa: SLF001
            "source",
            require_format_ready=True,
        )
        format_sql = document_storage._format_expression("source")  # noqa: SLF001
        document_date_sql = document_storage._document_date_expression("source")  # noqa: SLF001
        row = conn.execute(
            f"""SELECT {catalog_valid_sql},
                       {format_sql},
                       {document_date_sql}
                  FROM source"""  # noqa: SLF001
        ).fetchone()
        assert row is not None
        return int(row[0]), str(row[1]), None if row[2] is None else str(row[2])
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
        ({"mime_type": "text/plain"}, "text/plain"),
        ({"mime": "application/pdf"}, "application/pdf"),
        ({"mime": "TEXT/PLAIN", "mime_type": "text/plain"}, "text/plain"),
        ({"mime_type": "application/vnd.api+json"}, "application/vnd.api+json"),
        ({"mime_type": "a/" + "b" * 198}, "a/" + "b" * 198),
    ),
)
def test_format_navigation_accepts_only_one_bounded_unambiguous_mime(
    metadata: dict[str, object],
    expected: str,
) -> None:
    catalog_valid, source_format, _document_date = _metadata_projection(
        json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
    )

    assert catalog_valid == 1
    assert source_format == expected


@pytest.mark.parametrize(
    "metadata_json",
    (
        '{"mime_type":" text/plain"}',
        '{"mime_type":"text/plain\\n"}',
        '{"mime_type":"text/\\tplain"}',
        '{"mime_type":"text pl/plain"}',
        json.dumps({"mime_type": "текст/plain"}, separators=(",", ":")),
        '{"mime_type":"textplain"}',
        '{"mime_type":"/plain"}',
        '{"mime_type":"text/"}',
        '{"mime_type":"text/plain/extra"}',
        '{"mime_type":"text/plain;charset=utf-8"}',
        '{"mime_type":"text@/plain"}',
        '{"mime_type":"text/*"}',
        json.dumps({"mime_type": "a/" + "b" * 199}, separators=(",", ":")),
        '{"mime":"application/pdf","mime_type":"text/plain"}',
        '{"mime_type":"text/plain","mime_type":"application/pdf"}',
        '{"mime_type":7}',
    ),
    ids=(
        "whitespace",
        "newline",
        "tab",
        "embedded-space",
        "unicode",
        "missing-slash",
        "empty-type",
        "empty-subtype",
        "multiple-slashes",
        "parameters",
        "forbidden-punctuation",
        "wildcard",
        "oversized",
        "conflict",
        "duplicate",
        "wrong-type",
    ),
)
def test_malformed_format_metadata_has_no_catalog_projection(
    metadata_json: str,
) -> None:
    catalog_valid, source_format, _document_date = _metadata_projection(metadata_json)

    assert catalog_valid == 0
    assert source_format == ""


@pytest.mark.parametrize(
    ("metadata_json", "expected"),
    (
        ('{"document_date":"2024-05-10"}', "2024-05-10"),
        ('{"document_date":"10.05.2024"}', "2024-05-10"),
        ("{}", None),
        ('{"document_date":"2024-02-31"}', None),
        ('{"document_date":17}', None),
        ('{"document_date":"2024-05-10","document_date":"2025-05-10"}', None),
    ),
    ids=("iso", "parser-normalized", "missing", "invalid", "wrong-type", "duplicate"),
)
def test_legacy_document_date_projection_is_canonical_or_unknown(
    metadata_json: str,
    expected: str | None,
) -> None:
    _catalog_valid, _source_format, document_date = _metadata_projection(metadata_json)

    assert document_date == expected


def _legacy_date_constraint(corpus: ArchiveSearchCorpus) -> ArchiveTemporalConstraint:
    return ArchiveTemporalConstraint(
        corpus=corpus,
        role=TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
        value_kind=TemporalValueKind.DATE_INTERVAL,
        precision=TemporalPrecision.DAY,
        start="2024-05-10",
        end="2024-05-11",
    )


def test_own_date_parser_is_request_gated_and_never_enabled_for_knowledge() -> None:
    ordinary = ArchiveSearchRequest.create(
        query="bounded probe",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    dated = ArchiveSearchRequest.create(
        query="bounded probe",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(_legacy_date_constraint(ArchiveSearchCorpus.DOCUMENTS),),
    )
    knowledge = ArchiveSearchRequest.create(
        query="bounded probe",
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
        temporal_constraints=(_legacy_date_constraint(ArchiveSearchCorpus.KNOWLEDGE),),
    )

    ordinary_sql, _ = document_storage._source_cte(  # noqa: SLF001
        ArchiveSearchCorpus.DOCUMENTS,
        ordinary,
        include_body=False,
    )
    dated_sql, _ = document_storage._source_cte(  # noqa: SLF001
        ArchiveSearchCorpus.DOCUMENTS,
        dated,
        include_body=False,
    )
    assert "jericho_iso_date" not in ordinary_sql
    assert "jericho_iso_date" in dated_sql
    assert document_storage._temporal_supported(dated, ArchiveSearchCorpus.DOCUMENTS) is True  # noqa: SLF001
    assert document_storage._temporal_supported(knowledge, ArchiveSearchCorpus.KNOWLEDGE) is False  # noqa: SLF001


def test_format_matching_does_not_widen_the_shared_knowledge_catalog() -> None:
    request = ArchiveSearchRequest.create(
        query="text/plain",
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
    )

    with mock.patch.object(
        document_storage,
        "_format_expression",
        side_effect=AssertionError("knowledge catalog consulted document MIME"),
    ):
        document_storage._catalog_sql(  # noqa: SLF001
            ArchiveSearchCorpus.KNOWLEDGE,
            request,
            "owner",
            document_catalog_available=False,
            enrichment_revision=0,
        )
