from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, replace

import pytest

import friday.conversation_passages.schema as passage_schema
from friday.conversation_passages.contract import (
    CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_INDEX_REVISION,
    CONVERSATION_PASSAGE_MAX_COUNT,
    ConversationPassageAnchor,
    ConversationPassageContractError,
    ConversationPassageProjectionRead,
)
from friday.storage import FridayStorage


def _empty_projection() -> ConversationPassageProjectionRead:
    return ConversationPassageProjectionRead(
        conversation_id="conversation-projected-at",
        passage_index_revision=CONVERSATION_PASSAGE_INDEX_REVISION,
        boundary_identity_sha256="0" * 64,
        authorized_message_count=0,
        authorized_projected_count=0,
        authorized_projection_complete=True,
        authorized_indexed_through_message_id=None,
        authorized_conversation_revision_sha256=CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
        authorized_passage_set_sha256=CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
        anchor_offset=0,
        anchors=(),
        has_more=False,
    )


def test_boundary_projection_contract_is_immutable() -> None:
    projection = _empty_projection()

    with pytest.raises(FrozenInstanceError):
        projection.authorized_message_count = 1  # type: ignore[misc]


def test_empty_boundary_projection_rejects_nonempty_proof() -> None:
    projection = _empty_projection()

    with pytest.raises(ConversationPassageContractError, match="empty authorized projection proof"):
        ConversationPassageProjectionRead(
            conversation_id=projection.conversation_id,
            passage_index_revision=projection.passage_index_revision,
            boundary_identity_sha256=projection.boundary_identity_sha256,
            authorized_message_count=0,
            authorized_projected_count=0,
            authorized_projection_complete=True,
            authorized_indexed_through_message_id=None,
            authorized_conversation_revision_sha256="1" * 64,
            authorized_passage_set_sha256=projection.authorized_passage_set_sha256,
            anchor_offset=0,
            anchors=(),
            has_more=False,
        )


def test_anchor_contract_rejects_impossible_maximum_ordinal() -> None:
    with pytest.raises(ConversationPassageContractError, match="anchor ordinal is invalid"):
        ConversationPassageAnchor(
            conversation_id="conversation-anchor-limit",
            anchor_message_id="msg_0000000000000001",
            anchor_ordinal=CONVERSATION_PASSAGE_MAX_COUNT,
            anchor_message_revision_sha256="1" * 64,
            anchor_content_sha256="2" * 64,
            anchor_locator_sha256="3" * 64,
            conversation_prefix_sha256="4" * 64,
        )


def test_anchor_contract_rejects_noncanonical_message_identity() -> None:
    with pytest.raises(ConversationPassageContractError, match="anchor message identity is invalid"):
        ConversationPassageAnchor(
            conversation_id="conversation-anchor-identity",
            anchor_message_id="private/path/secret.txt",
            anchor_ordinal=0,
            anchor_message_revision_sha256="1" * 64,
            anchor_content_sha256="2" * 64,
            anchor_locator_sha256="3" * 64,
            conversation_prefix_sha256="4" * 64,
        )


@pytest.mark.parametrize("anchors", (None, [], "not-a-tuple"))
def test_projection_contract_closes_malformed_anchor_containers(anchors: object) -> None:
    with pytest.raises(ConversationPassageContractError, match="projection anchors are invalid"):
        replace(_empty_projection(), anchors=anchors)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "projected_at",
    (
        "0000-01-01T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-02-29T00:00:00Z",
        "2026-04-31T00:00:00Z",
    ),
)
def test_projection_table_rejects_impossible_calendar_timestamp(
    storage: FridayStorage,
    projected_at: str,
) -> None:
    conversation = storage.create_conversation("schema49-projected-at-owner")

    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            "UPDATE conversation_passage_projections SET projected_at=? WHERE conversation_id=?",
            (projected_at, conversation["id"]),
        )


def _simulate_missing_fts5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = passage_schema._canonical_schema_objects

    def unavailable(*, include_fts: bool) -> dict[tuple[str, str], str]:
        if include_fts:
            raise sqlite3.OperationalError("no such module: fts5")
        return canonical(include_fts=False)

    monkeypatch.setattr(passage_schema, "_canonical_schema_objects", unavailable)


def test_authoritative_validation_uses_exact_manifest_when_fts5_is_unavailable(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _simulate_missing_fts5(monkeypatch)

    passage_schema.validate_conversation_passage_schema(
        storage.conn,
        validate_fts_data=False,
    )
    with pytest.raises(sqlite3.OperationalError, match="no such module: fts5"):
        passage_schema.validate_conversation_passage_fts_schema(storage.conn)


@pytest.mark.parametrize(
    ("tamper", "expected"),
    (
        ("DROP INDEX idx_conversation_passage_anchor_revision", "passage DDL"),
        ("DROP TRIGGER conversation_passage_fts_ai", "passage FTS DDL"),
    ),
)
def test_no_fts5_fallback_still_rejects_counterfeit_schema(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    expected: str,
) -> None:
    with storage.transaction() as conn:
        conn.execute(tamper)
    _simulate_missing_fts5(monkeypatch)

    with pytest.raises(sqlite3.DatabaseError, match=expected):
        passage_schema.validate_conversation_passage_schema(
            storage.conn,
            validate_fts_data=False,
        )
