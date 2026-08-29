"""Closed contract tests for Proposal 86's durable document passage identity."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json

import pytest

from friday.document_catalog.passage_projection import (
    DOCUMENT_PASSAGE_INDEX_REVISION,
    DOCUMENT_PASSAGE_MAX_COUNT,
    DocumentPassageIncompleteReason,
    DocumentPassageProjection,
    DocumentPassageProjectionStatus,
    DocumentPassageSpan,
)
from friday.retrieval import chunk_spans
from friday.retrieval._contract_utils import RetrievalContractError

_RAW_ID = "raw_0123456789abcdef"
_SOURCE_SHA256 = "a" * 64


def _projection(text: str = "# Точный заголовок\nТекст документа") -> DocumentPassageProjection:
    return DocumentPassageProjection.from_complete_text(
        raw_object_id=_RAW_ID,
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text=text,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_current_projection_is_exact_body_free_and_canonical() -> None:
    body = "Секретный текст договора № 9917\nВторая строка."
    projection = _projection(body)

    assert (
        DocumentPassageProjection.parse_private(
            projection.to_private_json(),
            source_version=7,
            source_content_sha256=_SOURCE_SHA256,
            extracted_text=body,
        )
        == projection
    )
    assert projection.status is DocumentPassageProjectionStatus.CURRENT
    assert projection.incomplete_reason is None
    assert projection.extracted_text_sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert projection.source_char_count == len(body)
    assert projection.passages == (
        DocumentPassageSpan(0, 0, len(body), hashlib.sha256(body.encode()).hexdigest()),
    )
    assert body not in projection.to_private_json()
    assert body not in repr(projection)
    assert _RAW_ID not in repr(projection)
    assert not (
        _all_keys(projection.to_private_payload())
        & {
            "body",
            "excerpt",
            "filename",
            "path",
            "prompt",
            "query",
            "model_output",
            "tenant_id",
            "uploader_id",
            "canonical",
            "evidence_authority",
        }
    )


@pytest.mark.parametrize(
    "body",
    (
        "слово " * 40_000,
        "x" * 180_000,
    ),
    ids=("natural-boundaries", "unbroken-blob"),
)
def test_long_text_and_unbroken_blob_are_fully_covered_without_silent_tail_loss(
    body: str,
) -> None:
    projection = _projection(body)
    passages = projection.passages

    assert 1 < len(passages) <= DOCUMENT_PASSAGE_MAX_COUNT
    assert passages[0].start_char == 0
    assert passages[-1].end_char == len(body)
    assert [item.chunk_index for item in passages] == list(range(len(passages)))
    for left, right in zip(passages, passages[1:], strict=False):
        assert left.start_char < right.start_char <= left.end_char < right.end_char
    for passage in passages:
        exact_slice = body[passage.start_char : passage.end_char]
        assert passage.content_sha256 == hashlib.sha256(exact_slice.encode()).hexdigest()


def test_unicode_offsets_and_digests_bind_exact_python_codepoint_slices() -> None:
    body = ("А😀Б ёжик.\n" * 900) + "КОНЕЦ"
    projection = _projection(body)

    assert projection.source_char_count == len(body)
    assert projection.matches_exact_source_projection(
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text=body,
    )
    for passage in projection.passages:
        exact_slice = body[passage.start_char : passage.end_char]
        assert passage.content_sha256 == hashlib.sha256(exact_slice.encode("utf-8")).hexdigest()


def test_source_text_version_hash_and_policy_drift_fail_exact_revalidation() -> None:
    body = "Акт приёмки оборудования\n" * 200
    projection = _projection(body)

    assert not projection.matches_exact_source_projection(
        source_version=8,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text=body,
    )
    assert not projection.matches_exact_source_projection(
        source_version=7,
        source_content_sha256="b" * 64,
        extracted_text=body,
    )
    assert not projection.matches_exact_source_projection(
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text=body + "изменение",
    )
    with pytest.raises(RetrievalContractError, match="revision"):
        dataclasses.replace(projection, passage_index_revision="document-char-v2")
    tampered = dataclasses.replace(projection, extracted_text_sha256="f" * 64)
    assert not tampered.matches_exact_source_projection(
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text=body,
    )


@pytest.mark.parametrize(
    "reason",
    tuple(
        reason
        for reason in DocumentPassageIncompleteReason
        if reason is not DocumentPassageIncompleteReason.SOURCE_UNAVAILABLE
    ),
)
def test_every_bound_incomplete_reason_has_zero_passages(
    reason: DocumentPassageIncompleteReason,
) -> None:
    projection = DocumentPassageProjection.incomplete(
        raw_object_id=_RAW_ID,
        reason=reason,
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
    )

    assert projection.status is DocumentPassageProjectionStatus.INCOMPLETE
    assert projection.incomplete_reason is reason
    assert projection.passages == ()
    assert projection.extracted_text_sha256 is None
    assert projection.source_char_count is None
    assert not projection.matches_exact_source_projection(
        source_version=7,
        source_content_sha256=_SOURCE_SHA256,
        extracted_text="доступный позднее текст",
    )
    assert DocumentPassageProjection.parse_private(projection.to_private_json()) == projection


def test_source_unavailable_requires_an_incomplete_source_binding() -> None:
    unavailable = DocumentPassageProjection.incomplete(
        raw_object_id=_RAW_ID,
        reason=DocumentPassageIncompleteReason.SOURCE_UNAVAILABLE,
        source_version=7,
        source_content_sha256=None,
    )
    assert unavailable.passages == ()

    with pytest.raises(RetrievalContractError, match="complete source binding"):
        DocumentPassageProjection.incomplete(
            raw_object_id=_RAW_ID,
            reason=DocumentPassageIncompleteReason.SOURCE_UNAVAILABLE,
            source_version=7,
            source_content_sha256=_SOURCE_SHA256,
        )
    with pytest.raises(RetrievalContractError, match="lacks source binding"):
        DocumentPassageProjection.incomplete(
            raw_object_id=_RAW_ID,
            reason=DocumentPassageIncompleteReason.BACKFILL_PENDING,
            source_version=None,
            source_content_sha256=None,
        )


def test_projection_does_not_encode_or_infer_pending_or_canonical_authority() -> None:
    projection = _projection()
    keys = _all_keys(projection.to_private_payload())

    assert not (keys & {"review_state", "pending", "canonical", "authority", "tenant"})
    assert not hasattr(projection, "authorize")
    assert not hasattr(projection, "promote")
    assert not hasattr(projection, "evidence_authority")


def test_passage_policy_is_code_owned_and_embedding_independent() -> None:
    signature = inspect.signature(DocumentPassageProjection.from_complete_text)

    assert tuple(signature.parameters) == (
        "raw_object_id",
        "source_version",
        "source_content_sha256",
        "extracted_text",
    )
    assert DOCUMENT_PASSAGE_INDEX_REVISION == "document-char-v1:chunk-spans-v3:1200:200:64"
    assert not ({"settings", "model", "embedding", "endpoint"} & set(signature.parameters))


def test_passage_policy_revision_pins_the_exact_current_span_algorithm() -> None:
    projection = _projection("слово " * 40_000)
    coordinates = [(item.start_char, item.end_char) for item in projection.passages]
    fingerprint = hashlib.sha256(json.dumps(coordinates, separators=(",", ":")).encode("ascii")).hexdigest()

    assert len(coordinates) == DOCUMENT_PASSAGE_MAX_COUNT
    assert fingerprint == "e11d972ced5bb01e749ddfd8f19b16446707441f54fc8bf9de6fda8ef2397ba3"


def test_v3_filters_only_document_nonprogress_spans_without_changing_global_chunking() -> None:
    body = "Prelude sentence. " + ("x" * 1_400)
    released_v2 = chunk_spans(body, max_chars=1_200, overlap_chars=200, max_chunks=64)

    projection = _projection(body)

    assert released_v2 == [(0, 18), (8, 18), (18, 1_218), (1_018, len(body))]
    assert [(item.start_char, item.end_char) for item in projection.passages] == [
        (0, 18),
        (18, 1_218),
        (1_018, len(body)),
    ]
    assert [item.chunk_index for item in projection.passages] == [0, 1, 2]
    assert chunk_spans(body, max_chars=1_200, overlap_chars=200, max_chunks=64) == released_v2


def test_current_parser_requires_source_and_rejects_forged_policy_or_slice_digest() -> None:
    body = "x" * 2_000
    projection = _projection(body)

    with pytest.raises(RetrievalContractError, match="requires exact source revalidation"):
        DocumentPassageProjection.parse_private(projection.to_private_json())

    forged_policy = dataclasses.replace(
        projection,
        passages=(DocumentPassageSpan(0, 0, len(body), "b" * 64),),
    )
    with pytest.raises(RetrievalContractError, match="does not match its exact source"):
        DocumentPassageProjection.parse_private(
            forged_policy.to_private_json(),
            source_version=7,
            source_content_sha256=_SOURCE_SHA256,
            extracted_text=body,
        )

    forged_digest = dataclasses.replace(
        projection,
        passages=(dataclasses.replace(projection.passages[0], content_sha256="c" * 64),)
        + projection.passages[1:],
    )
    with pytest.raises(RetrievalContractError, match="does not match its exact source"):
        DocumentPassageProjection.from_private_payload(
            forged_digest.to_private_payload(),
            source_version=7,
            source_content_sha256=_SOURCE_SHA256,
            extracted_text=body,
        )

    with pytest.raises(RetrievalContractError, match="does not match its exact source"):
        DocumentPassageProjection.parse_private(
            projection.to_private_json(),
            source_version=8,
            source_content_sha256=_SOURCE_SHA256,
            extracted_text=body,
        )


def test_closed_parser_rejects_extra_duplicate_and_noncanonical_json() -> None:
    projection = _projection()
    payload = projection.to_private_payload()
    payload["extra"] = "forged"
    with pytest.raises(RetrievalContractError, match="keys"):
        DocumentPassageProjection.from_private_payload(payload)

    canonical = projection.to_private_json()
    duplicate = canonical.replace(
        '"schema":',
        '"schema":"friday.document-passage-projection.private.v1","schema":',
        1,
    )
    with pytest.raises(RetrievalContractError, match="duplicate"):
        DocumentPassageProjection.parse_private(duplicate)

    pretty = json.dumps(projection.to_private_payload(), ensure_ascii=False, indent=2)
    with pytest.raises(RetrievalContractError, match="canonical"):
        DocumentPassageProjection.parse_private(pretty)

    oversized = projection.to_private_payload()
    oversized["passages"] = [projection.passages[0].to_private_payload()] * (DOCUMENT_PASSAGE_MAX_COUNT + 1)
    with pytest.raises(RetrievalContractError, match="passages"):
        DocumentPassageProjection.from_private_payload(oversized)


def test_current_projection_rejects_empty_invalid_and_gapped_sources() -> None:
    for text in ("", " \n\t"):
        with pytest.raises(RetrievalContractError, match="non-empty"):
            _projection(text)

    with pytest.raises(RetrievalContractError, match="gap"):
        dataclasses.replace(
            _projection("x" * 2_000),
            passages=(
                DocumentPassageSpan(0, 0, 900, "a" * 64),
                DocumentPassageSpan(1, 1_000, 2_000, "b" * 64),
            ),
        )
