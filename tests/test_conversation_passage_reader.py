from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Any

import pytest

from friday.conversation_passages.contract import (
    ConversationPassageProjectionRead,
)
from friday.conversation_passages.schema import (
    conversation_passage_anchor_locator_sha256,
    conversation_passage_content_sha256,
    conversation_passage_message_revision_sha256,
    conversation_passage_prefix_sha256,
)
from friday.storage._conversation_passages import (
    ConversationPassageStorageError,
    select_authorized_conversation_passage_projection_in_transaction,
)

_PROJECTED_AT = "2026-08-29T12:00:00Z"
_SOURCE_TIME = "2026-08-29T09:00:00+00:00"
_SIDECAR_PREFIXES = (
    "conversation_passage_",
    "conversation_passages",
)


def _message_id(number: int) -> str:
    return f"msg_{number:016x}"


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    conversation_id: str,
    principal_id: str,
    role: str,
    content: str,
    created_at: str = _SOURCE_TIME,
) -> None:
    conn.execute(
        """INSERT INTO messages(
               id,conversation_id,user_id,role,content,
               metadata_json,reply_to,created_at
           ) VALUES(?,?,?,?,?,'{}',NULL,?)""",
        (
            message_id,
            conversation_id,
            principal_id,
            role,
            content,
            created_at,
        ),
    )


def _tamper_message_content(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    content: str,
) -> None:
    """Test-only corruption below the immutable message-row guard."""

    trigger = conn.execute(
        """SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='messages_are_never_rewritten'"""
    ).fetchone()
    assert trigger is not None and isinstance(trigger["sql"], str)
    conn.execute("DROP TRIGGER messages_are_never_rewritten")
    try:
        conn.execute("UPDATE messages SET content=? WHERE id=?", (content, message_id))
    finally:
        conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL


def _publish_conversation(storage: Any, conversation_id: str) -> tuple[str, ...]:
    """Test-only exact publication through the schema's guarded append seam."""

    with storage.transaction() as conn:
        rows = tuple(
            dict(row)
            for row in conn.execute(
                """SELECT id,conversation_id,user_id,role,content,created_at
                     FROM messages
                    WHERE conversation_id=? AND role IN ('user','assistant')
                    ORDER BY rowid ASC""",
                (conversation_id,),
            ).fetchall()
        )
        assert rows
        next_rowid = int(
            conn.execute("SELECT COALESCE(MAX(passage_rowid),0)+1 FROM conversation_passages").fetchone()[0]
        )
        prefix: str | None = None
        message_ids: list[str] = []
        for ordinal, source in enumerate(rows):
            message_id = str(source["id"])
            principal_id = str(source["user_id"])
            content = str(source["content"])
            revision = conversation_passage_message_revision_sha256(
                message_id=message_id,
                conversation_id=conversation_id,
                principal_id=principal_id,
                role=str(source["role"]),
                content=content,
                created_at=str(source["created_at"]),
            )
            content_digest = conversation_passage_content_sha256(content)
            locator = conversation_passage_anchor_locator_sha256(
                conversation_id=conversation_id,
                anchor_message_id=message_id,
                anchor_ordinal=ordinal,
            )
            prefix = conversation_passage_prefix_sha256(prefix, ordinal, revision)
            conn.execute(
                """INSERT INTO conversation_passages(
                       passage_rowid,conversation_id,anchor_message_id,
                       anchor_ordinal,anchor_message_revision_sha256,
                       anchor_content_sha256,anchor_locator_sha256,
                       conversation_prefix_sha256
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    next_rowid + ordinal,
                    conversation_id,
                    message_id,
                    ordinal,
                    revision,
                    content_digest,
                    locator,
                    prefix,
                ),
            )
            message_ids.append(message_id)
    return tuple(message_ids)


def _conversation(storage: Any, principal_id: str) -> str:
    return str(storage.create_conversation(principal_id)["id"])


def _read(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    boundary_conversation_id: str,
    boundary_message_id: str,
    conversation_id: str,
    anchor_offset: int = 0,
    limit: int = 256,
) -> ConversationPassageProjectionRead | None:
    return select_authorized_conversation_passage_projection_in_transaction(
        conn,
        principal_id=principal_id,
        boundary_conversation_id=boundary_conversation_id,
        origin_boundary_user_message_id=boundary_message_id,
        conversation_id=conversation_id,
        anchor_offset=anchor_offset,
        limit=limit,
    )


def test_reader_requires_a_caller_owned_transaction(storage: Any) -> None:
    owner = "conversation-reader-owner"
    boundary_conversation = _conversation(storage, owner)
    boundary = storage.store_message(
        boundary_conversation,
        owner,
        "user",
        "accepted turn",
    )

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        _read(
            storage.conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=str(boundary["id"]),
            conversation_id=boundary_conversation,
        )


def test_foreign_inactive_and_wrong_target_stop_before_sidecar(storage: Any) -> None:
    owner = "conversation-reader-owner"
    foreign = "conversation-reader-foreign"
    inactive = "conversation-reader-inactive"
    owner_conversation = _conversation(storage, owner)
    foreign_conversation = _conversation(storage, foreign)
    inactive_conversation = _conversation(storage, inactive)
    owner_boundary = storage.store_message(
        owner_conversation,
        owner,
        "user",
        "owner boundary",
    )
    foreign_boundary = storage.store_message(
        foreign_conversation,
        foreign,
        "user",
        "foreign boundary",
    )
    inactive_boundary = storage.store_message(
        inactive_conversation,
        inactive,
        "user",
        "inactive boundary",
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE users SET status='inactive' WHERE id=?", (inactive,))

    denied: list[str] = []

    def deny_sidecar(
        action: int,
        arg1: str | None,
        _arg2: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        name = str(arg1 or "")
        if action == sqlite3.SQLITE_READ and name.startswith(_SIDECAR_PREFIXES):
            denied.append(name)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with storage.transaction() as conn:
        conn.set_authorizer(deny_sidecar)
        try:
            assert (
                _read(
                    conn,
                    principal_id=foreign,
                    boundary_conversation_id=owner_conversation,
                    boundary_message_id=str(owner_boundary["id"]),
                    conversation_id=owner_conversation,
                )
                is None
            )
            assert (
                _read(
                    conn,
                    principal_id=owner,
                    boundary_conversation_id=foreign_conversation,
                    boundary_message_id=str(foreign_boundary["id"]),
                    conversation_id=owner_conversation,
                )
                is None
            )
            assert (
                _read(
                    conn,
                    principal_id=owner,
                    boundary_conversation_id=owner_conversation,
                    boundary_message_id=str(owner_boundary["id"]),
                    conversation_id=foreign_conversation,
                )
                is None
            )
            assert (
                _read(
                    conn,
                    principal_id=inactive,
                    boundary_conversation_id=inactive_conversation,
                    boundary_message_id=str(inactive_boundary["id"]),
                    conversation_id=inactive_conversation,
                )
                is None
            )
        finally:
            conn.set_authorizer(None)
    assert denied == []


def test_backfill_pending_zero_child_reader_does_not_read_target_bodies(
    storage: Any,
) -> None:
    owner = "conversation-reader-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    boundary_id = _message_id(0x80)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=_message_id(0x70),
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="pre-boundary target body must not be read",
            created_at="2026-08-29T08:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="the sole authorized boundary body read",
            created_at="2026-08-29T09:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=_message_id(0x90),
            conversation_id=target,
            principal_id=owner,
            role="user",
            content="post-boundary target body must not be read",
            created_at="2026-08-29T10:00:00+00:00",
        )

    content_reads: list[tuple[str, str]] = []

    def observe_content_reads(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and table == "messages" and column == "content":
            content_reads.append((table, column))
        return sqlite3.SQLITE_OK

    with storage.transaction() as conn:
        conn.set_authorizer(observe_content_reads)
        try:
            page = _read(
                conn,
                principal_id=owner,
                boundary_conversation_id=boundary_conversation,
                boundary_message_id=boundary_id,
                conversation_id=target,
            )
        finally:
            conn.set_authorizer(None)

    assert page is not None
    assert page.authorized_message_count == 1
    assert page.authorized_projected_count == 0
    assert page.authorized_projection_complete is False
    assert page.anchors == ()
    assert content_reads == [("messages", "content")]


def test_owned_boundary_reads_current_and_other_owned_target(storage: Any) -> None:
    owner = "conversation-reader-owner"
    boundary_conversation = _conversation(storage, owner)
    other_conversation = _conversation(storage, owner)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=_message_id(0x100),
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="assistant",
            content="current conversation history",
        )
        _insert_message(
            conn,
            message_id=_message_id(0x200),
            conversation_id=other_conversation,
            principal_id=owner,
            role="user",
            content="other source first",
        )
        _insert_message(
            conn,
            message_id=_message_id(0x201),
            conversation_id=other_conversation,
            principal_id=owner,
            role="assistant",
            content="other source second",
        )
        boundary_message_id = _message_id(0x300)
        _insert_message(
            conn,
            message_id=boundary_message_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted user turn",
        )
    _publish_conversation(storage, boundary_conversation)
    _publish_conversation(storage, other_conversation)

    with storage.transaction() as conn:
        current = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_message_id,
            conversation_id=boundary_conversation,
        )
        other = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_message_id,
            conversation_id=other_conversation,
        )

    assert current is not None and other is not None
    assert current.authorized_projection_complete is True
    assert current.authorized_message_count == 1
    assert [item.anchor_message_id for item in current.anchors] == [_message_id(0x100)]
    assert other.authorized_projection_complete is True
    assert other.authorized_message_count == 2
    assert [item.anchor_message_id for item in other.anchors] == [
        _message_id(0x200),
        _message_id(0x201),
    ]
    assert current.boundary_identity_sha256 == other.boundary_identity_sha256


def test_reader_pages_at_256_and_rejects_malformed_controls(storage: Any) -> None:
    owner = "conversation-reader-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    with storage.transaction() as conn:
        for ordinal in range(257):
            _insert_message(
                conn,
                message_id=_message_id(0x1000 + ordinal),
                conversation_id=target,
                principal_id=owner,
                role="user" if ordinal % 2 == 0 else "assistant",
                content=f"bounded source {ordinal}",
            )
        boundary_message_id = _message_id(0x2000)
        _insert_message(
            conn,
            message_id=boundary_message_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted page boundary",
        )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        first = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_message_id,
            conversation_id=target,
            limit=256,
        )
        final = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_message_id,
            conversation_id=target,
            anchor_offset=256,
            limit=256,
        )
        empty = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_message_id,
            conversation_id=target,
            anchor_offset=257,
            limit=1,
        )
        for controls in (
            {"anchor_offset": -1},
            {"anchor_offset": True},
            {"anchor_offset": 258},
            {"limit": 0},
            {"limit": 257},
            {"limit": True},
        ):
            with pytest.raises(ConversationPassageStorageError):
                _read(
                    conn,
                    principal_id=owner,
                    boundary_conversation_id=boundary_conversation,
                    boundary_message_id=boundary_message_id,
                    conversation_id=target,
                    **controls,
                )

    assert first is not None and final is not None and empty is not None
    assert len(first.anchors) == 256 and first.has_more is True
    assert len(final.anchors) == 1 and final.has_more is False
    assert final.authorized_indexed_through_message_id == final.anchors[0].anchor_message_id
    assert empty.anchors == () and empty.has_more is False


def test_reader_message_plan_and_cost_ignore_unrelated_global_rows(storage: Any) -> None:
    owner = "conversation-reader-plan-owner"
    unrelated_owner = "conversation-reader-plan-unrelated"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    unrelated = _conversation(storage, unrelated_owner)
    target_message_id = _message_id(0x6000)
    boundary_message_id = _message_id(0x6001)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=target_message_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="indexed target source",
        )
        _insert_message(
            conn,
            message_id=boundary_message_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted plan boundary",
        )
    _publish_conversation(storage, target)

    traced: list[str] = []
    with storage.transaction() as conn:
        conn.set_trace_callback(traced.append)
        try:
            projection = _read(
                conn,
                principal_id=owner,
                boundary_conversation_id=boundary_conversation,
                boundary_message_id=boundary_message_id,
                conversation_id=target,
                limit=1,
            )
        finally:
            conn.set_trace_callback(None)
        parent_sql = next(statement for statement in traced if "target_source AS MATERIALIZED" in statement)
        plan = tuple(str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {parent_sql}").fetchall())
    assert projection is not None and projection.authorized_projected_count == 1
    assert any(
        "SEARCH source USING INDEX idx_messages_conversation (user_id=? AND conversation_id=?)" in detail
        for detail in plan
    )

    def measured_instruction_blocks() -> int:
        instruction_blocks = 0

        def progress() -> int:
            nonlocal instruction_blocks
            instruction_blocks += 1
            return 0

        with storage.transaction() as conn:
            conn.set_progress_handler(progress, 100)
            try:
                measured = _read(
                    conn,
                    principal_id=owner,
                    boundary_conversation_id=boundary_conversation,
                    boundary_message_id=boundary_message_id,
                    conversation_id=target,
                    limit=1,
                )
            finally:
                conn.set_progress_handler(None, 0)
        assert measured is not None
        return instruction_blocks

    baseline = measured_instruction_blocks()
    with storage.transaction() as conn:
        for ordinal in range(4_096):
            _insert_message(
                conn,
                message_id=_message_id(0x100000 + ordinal),
                conversation_id=unrelated,
                principal_id=unrelated_owner,
                role="user" if ordinal % 2 == 0 else "assistant",
                content=f"unrelated global source {ordinal}",
            )
    with_unrelated_rows = measured_instruction_blocks()

    # Each callback represents 100 SQLite VM instructions.  Tree-depth growth
    # is allowed; a global messages scan would exceed this fixed delta by a
    # wide margin for the 4,096 unrelated rows above.
    assert with_unrelated_rows <= baseline + 100


def test_reader_never_returns_post_boundary_identity_or_message_body(storage: Any) -> None:
    owner = "conversation-reader-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    pre_body = "PREBOUNDARY-PRIVATE-BODY"
    boundary_body = "BOUNDARY-PRIVATE-BODY"
    post_body = "POSTBOUNDARY-PRIVATE-BODY"
    pre_id = _message_id(0x3000)
    boundary_id = _message_id(0x3001)
    post_id = _message_id(0x3002)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=pre_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content=pre_body,
            created_at="2026-08-29T08:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content=boundary_body,
            created_at="2026-08-29T09:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=post_id,
            conversation_id=target,
            principal_id=owner,
            role="user",
            content=post_body,
            created_at="2026-08-29T10:00:00+00:00",
        )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        page = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_id,
            conversation_id=target,
        )

    assert page is not None
    assert [item.anchor_message_id for item in page.anchors] == [pre_id]
    assert page.authorized_indexed_through_message_id == pre_id
    private_projection = json.dumps(asdict(page), ensure_ascii=True, sort_keys=True)
    assert post_id not in private_projection
    assert pre_body not in private_projection
    assert boundary_body not in private_projection
    assert post_body not in private_projection
    rendered = repr(page) + "".join(repr(item) for item in page.anchors)
    assert pre_body not in rendered
    assert boundary_body not in rendered
    assert post_body not in rendered


@pytest.mark.parametrize("future_change", ("body", "anchor", "absent", "rebuild"))
def test_future_anchor_state_cannot_gate_an_accepted_prefix(
    storage: Any,
    future_change: str,
) -> None:
    owner = "conversation-reader-prefix-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    pre_id = _message_id(0x3800)
    old_boundary_id = _message_id(0x3801)
    future_id = _message_id(0x3802)
    full_boundary_id = _message_id(0x3803)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=pre_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="immutable accepted prefix",
            created_at="2026-08-29T08:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=old_boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted old boundary",
            created_at="2026-08-29T09:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=future_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="future body must be irrelevant to the old proof",
            created_at="2026-08-29T10:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=full_boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted full boundary",
            created_at="2026-08-29T11:00:00+00:00",
        )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        baseline = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=old_boundary_id,
            conversation_id=target,
        )
        full_baseline = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=full_boundary_id,
            conversation_id=target,
        )
    assert baseline is not None and full_baseline is not None
    assert [item.anchor_message_id for item in baseline.anchors] == [pre_id]
    assert [item.anchor_message_id for item in full_baseline.anchors] == [pre_id, future_id]

    with storage.transaction() as conn:
        if future_change == "body":
            _tamper_message_content(
                conn,
                message_id=future_id,
                content="corrupt future body",
            )
        elif future_change == "absent":
            trigger = conn.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger' AND name='conversation_passage_bd_validate'"""
            ).fetchone()
            assert trigger is not None and isinstance(trigger["sql"], str)
            conn.execute("DROP TRIGGER conversation_passage_bd_validate")
            try:
                conn.execute(
                    "DELETE FROM conversation_passages WHERE anchor_message_id=?",
                    (future_id,),
                )
            finally:
                conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL
        else:
            trigger = conn.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger' AND name='conversation_passage_bu_validate'"""
            ).fetchone()
            assert trigger is not None and isinstance(trigger["sql"], str)
            conn.execute("DROP TRIGGER conversation_passage_bu_validate")
            try:
                if future_change == "anchor":
                    conn.execute(
                        """UPDATE conversation_passages
                              SET anchor_content_sha256=?
                            WHERE anchor_message_id=?""",
                        ("f" * 64, future_id),
                    )
                else:
                    next_rowid = int(
                        conn.execute(
                            "SELECT COALESCE(MAX(passage_rowid),0)+1 FROM conversation_passages"
                        ).fetchone()[0]
                    )
                    conn.execute(
                        "UPDATE conversation_passages SET passage_rowid=? WHERE anchor_message_id=?",
                        (next_rowid, future_id),
                    )
            finally:
                conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL

    with storage.transaction() as conn:
        after = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=old_boundary_id,
            conversation_id=target,
        )
        assert after == baseline

        if future_change in {"body", "anchor", "absent"}:
            with pytest.raises(ConversationPassageStorageError):
                _read(
                    conn,
                    principal_id=owner,
                    boundary_conversation_id=boundary_conversation,
                    boundary_message_id=full_boundary_id,
                    conversation_id=target,
                )
        else:
            later = _read(
                conn,
                principal_id=owner,
                boundary_conversation_id=boundary_conversation,
                boundary_message_id=full_boundary_id,
                conversation_id=target,
            )
            assert later is not None
            assert later.authorized_projection_complete is True
            assert [item.anchor_message_id for item in later.anchors] == [pre_id, future_id]


def test_backdated_future_full_rebuild_preserves_the_exact_accepted_proof(
    storage: Any,
) -> None:
    owner = "conversation-reader-rebuild-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    pre_id = _message_id(0x3900)
    old_boundary_id = _message_id(0x3901)
    future_id = _message_id(0x3902)
    full_boundary_id = _message_id(0x3903)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=pre_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="stable durable-ingress prefix",
            created_at="2026-08-29T10:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=old_boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted before future ingress",
            created_at="2026-08-29T11:00:00+00:00",
        )
    _publish_conversation(storage, target)
    with storage.transaction() as conn:
        baseline = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=old_boundary_id,
            conversation_id=target,
        )
    assert baseline is not None

    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=future_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="later ingress with an earlier display timestamp",
            created_at="2026-08-29T09:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=full_boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted after future ingress",
            created_at="2026-08-29T12:00:00+00:00",
        )
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_message_count=0,
                      indexed_through_message_id=NULL,
                      indexed_conversation_revision_sha256=NULL,
                      passage_set_sha256=NULL,
                      projection_status='incomplete',
                      incomplete_reason='source_changed',
                      passage_count=0,
                      projected_at=?
                WHERE conversation_id=?""",
            (_PROJECTED_AT, target),
        )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        after = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=old_boundary_id,
            conversation_id=target,
        )
        full = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=full_boundary_id,
            conversation_id=target,
        )

    assert after == baseline
    assert full is not None
    assert [item.anchor_message_id for item in full.anchors] == [pre_id, future_id]


def test_retained_source_changed_prefix_is_complete_at_accepted_boundary(storage: Any) -> None:
    owner = "conversation-reader-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    pre_id = _message_id(0x4000)
    boundary_id = _message_id(0x4001)
    post_id = _message_id(0x4002)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=pre_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="retained prefix",
            created_at="2026-02-01T00:00:00+00:00",
        )
        _insert_message(
            conn,
            message_id=boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted much later turn",
            created_at="2026-12-01T00:00:00+00:00",
        )
    _publish_conversation(storage, target)
    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master
                WHERE type='trigger'
                  AND name='conversation_passage_message_ai_invalidate'"""
        ).fetchone()
        assert trigger is not None and isinstance(trigger["sql"], str)
        conn.execute("DROP TRIGGER conversation_passage_message_ai_invalidate")
        try:
            _insert_message(
                conn,
                message_id=post_id,
                conversation_id=target,
                principal_id=owner,
                role="user",
                content="inserted after the boundary but ordered before the prefix",
                created_at="2026-01-01T00:00:00+00:00",
            )
        finally:
            conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET projection_status='incomplete',
                      incomplete_reason='source_changed',
                      projected_at=?
                WHERE conversation_id=?""",
            (_PROJECTED_AT, target),
        )
        ordered_ids = tuple(
            str(row[0])
            for row in conn.execute(
                """SELECT id FROM messages
                    WHERE conversation_id=? AND role IN ('user','assistant')
                    ORDER BY created_at ASC,id ASC""",
                (target,),
            )
        )
        assert ordered_ids == (post_id, pre_id)

    with storage.transaction() as conn:
        page = _read(
            conn,
            principal_id=owner,
            boundary_conversation_id=boundary_conversation,
            boundary_message_id=boundary_id,
            conversation_id=target,
        )

    assert page is not None
    assert page.authorized_message_count == 1
    assert page.authorized_projected_count == 1
    assert page.authorized_projection_complete is True
    assert [item.anchor_message_id for item in page.anchors] == [pre_id]
    assert post_id not in {item.anchor_message_id for item in page.anchors}


@pytest.mark.parametrize("tamper", ("schema", "data"))
def test_target_schema_or_data_tamper_fails_closed(storage: Any, tamper: str) -> None:
    owner = "conversation-reader-owner"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    source_id = _message_id(0x5000)
    boundary_id = _message_id(0x5001)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=source_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="authenticated source",
        )
        _insert_message(
            conn,
            message_id=boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="accepted boundary",
        )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        if tamper == "schema":
            conn.execute("DROP INDEX idx_conversation_passage_anchor_revision")
        else:
            trigger = conn.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger'
                      AND name='conversation_passage_projection_bu_validate'"""
            ).fetchone()
            assert trigger is not None and isinstance(trigger["sql"], str)
            conn.execute("DROP TRIGGER conversation_passage_projection_bu_validate")
            try:
                conn.execute(
                    """UPDATE conversation_passage_projections
                          SET indexed_through_message_id='msg_ffffffffffffffff'
                        WHERE conversation_id=?""",
                    (target,),
                )
            finally:
                conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL
        with pytest.raises(ConversationPassageStorageError):
            _read(
                conn,
                principal_id=owner,
                boundary_conversation_id=boundary_conversation,
                boundary_message_id=boundary_id,
                conversation_id=target,
            )


def test_full_boundary_rejects_an_extra_foreign_sidecar_child(storage: Any) -> None:
    owner = "conversation-reader-extra-child-owner"
    foreign = "conversation-reader-extra-child-foreign"
    target = _conversation(storage, owner)
    boundary_conversation = _conversation(storage, owner)
    foreign_conversation = _conversation(storage, foreign)
    source_id = _message_id(0x5100)
    boundary_id = _message_id(0x5101)
    with storage.transaction() as conn:
        _insert_message(
            conn,
            message_id=source_id,
            conversation_id=target,
            principal_id=owner,
            role="assistant",
            content="authorized source",
        )
        _insert_message(
            conn,
            message_id=boundary_id,
            conversation_id=boundary_conversation,
            principal_id=owner,
            role="user",
            content="full boundary",
        )
    foreign_message = storage.store_message(
        foreign_conversation,
        foreign,
        "user",
        "foreign child source",
    )
    _publish_conversation(storage, target)

    with storage.transaction() as conn:
        triggers = conn.execute(
            """SELECT name,sql FROM sqlite_master
                WHERE type='trigger'
                  AND name IN (
                      'conversation_passage_bi_validate',
                      'conversation_passage_ai_parent_cas'
                  )
                ORDER BY name ASC"""
        ).fetchall()
        assert len(triggers) == 2
        assert all(isinstance(trigger["sql"], str) for trigger in triggers)
        for trigger in triggers:
            conn.execute(f"DROP TRIGGER {trigger['name']}")  # nosec B608 - fixed names above
        try:
            next_rowid = int(
                conn.execute("SELECT COALESCE(MAX(passage_rowid),0)+1 FROM conversation_passages").fetchone()[
                    0
                ]
            )
            conn.execute(
                """INSERT INTO conversation_passages(
                       passage_rowid,conversation_id,anchor_message_id,anchor_ordinal,
                       anchor_message_revision_sha256,anchor_content_sha256,
                       anchor_locator_sha256,conversation_prefix_sha256
                   ) VALUES(?,?,?,1,?,?,?,?)""",
                (
                    next_rowid,
                    target,
                    foreign_message["id"],
                    "0" * 64,
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
            )
        finally:
            for trigger in triggers:
                conn.execute(trigger["sql"])  # nosec B608 - exact SQLite-owned canonical DDL

        with pytest.raises(ConversationPassageStorageError):
            _read(
                conn,
                principal_id=owner,
                boundary_conversation_id=boundary_conversation,
                boundary_message_id=boundary_id,
                conversation_id=target,
            )
