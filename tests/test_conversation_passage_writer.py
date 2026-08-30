from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import friday.conversation_passages.schema as schema_module
import friday.conversation_passages.writer as writer_module
from friday.conversation_passages.contract import (
    CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
)
from friday.conversation_passages.writer import (
    backfill_conversation_passages_in_transaction,
)
from friday.storage import init_storage


def _conversation(storage: Any, owner: str, bodies: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    conversation_id = str(storage.create_conversation(owner)["id"])
    message_ids = tuple(
        str(
            storage.store_message(
                conversation_id,
                owner,
                "user" if index % 2 == 0 else "assistant",
                body,
            )["id"]
        )
        for index, body in enumerate(bodies)
    )
    return conversation_id, message_ids


def _parent(storage: Any, conversation_id: str) -> dict[str, Any]:
    row = storage.execute(
        "SELECT * FROM conversation_passage_projections WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _child_count(storage: Any, conversation_id: str) -> int:
    row = storage.execute(
        "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_writer_advances_exactly_one_anchor_per_work_item_and_is_idempotent(storage: Any) -> None:
    owner = "conversation-writer-bounded"
    conversation_id, message_ids = _conversation(storage, owner, ("first", "second", "third"))

    for expected_count in (1, 2, 3):
        report = storage.backfill_conversation_passages(owner, limit=1)
        parent = _parent(storage, conversation_id)
        assert report["examined"] == 1
        assert report["anchors_written"] == 1
        assert report["message_bytes_examined"] == len(("first", "second", "third")[expected_count - 1])
        assert parent["passage_count"] == expected_count
        assert parent["indexed_message_count"] == expected_count
        assert parent["indexed_through_message_id"] == message_ids[expected_count - 1]
        assert _child_count(storage, conversation_id) == expected_count

    assert _parent(storage, conversation_id)["projection_status"] == "current"
    before = tuple(
        tuple(row)
        for row in storage.execute(
            "SELECT * FROM conversation_passages WHERE conversation_id=? ORDER BY anchor_ordinal",
            (conversation_id,),
        ).fetchall()
    )
    replay = storage.backfill_conversation_passages(owner, limit=64)
    after = tuple(
        tuple(row)
        for row in storage.execute(
            "SELECT * FROM conversation_passages WHERE conversation_id=? ORDER BY anchor_ordinal",
            (conversation_id,),
        ).fetchall()
    )
    assert replay == {
        "examined": 0,
        "anchors_written": 0,
        "current": 0,
        "explicit_incomplete": 0,
        "has_more": False,
        "next_resume_conversation_id": None,
        "message_bytes_examined": 0,
        "message_byte_budget": writer_module.CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES,
        "byte_budget_reached": False,
    }
    assert after == before


def test_zero_byte_message_is_counted_as_a_written_anchor(storage: Any) -> None:
    owner = "conversation-writer-empty"
    conversation_id, _message_ids = _conversation(storage, owner, ("",))

    report = storage.backfill_conversation_passages(owner, limit=1)

    assert report["examined"] == 1
    assert report["anchors_written"] == 1
    assert report["message_bytes_examined"] == 0
    assert report["current"] == 1
    assert _child_count(storage, conversation_id) == 1
    assert _parent(storage, conversation_id)["projection_status"] == "current"


def test_writer_fair_shares_conversations_and_its_returned_cursor_replays(storage: Any) -> None:
    owner = "conversation-writer-fair"
    first, _ = _conversation(storage, owner, ("a1", "a2", "a3"))
    second, _ = _conversation(storage, owner, ("b1", "b2", "b3"))

    first_page = storage.backfill_conversation_passages(owner, limit=2)

    assert first_page["examined"] == 2
    assert _parent(storage, first)["passage_count"] == 1
    assert _parent(storage, second)["passage_count"] == 1
    cursor = first_page["next_resume_conversation_id"]
    assert isinstance(cursor, str) and cursor.startswith("cpw2:")

    second_page = storage.backfill_conversation_passages(
        owner,
        resume_at_conversation_id=cursor,
        limit=2,
    )

    assert second_page["examined"] == 2
    assert _parent(storage, first)["passage_count"] == 2
    assert _parent(storage, second)["passage_count"] == 2


def test_committed_prefix_survives_reopen_and_resumes_without_duplicates(settings: Any) -> None:
    owner = "conversation-writer-restart"
    first = init_storage(settings)
    try:
        conversation_id, message_ids = _conversation(first, owner, ("one", "two", "three"))
        report = first.backfill_conversation_passages(owner, limit=1)
        cursor = report["next_resume_conversation_id"]
        assert _child_count(first, conversation_id) == 1
    finally:
        first.close()

    reopened = init_storage(settings)
    try:
        resumed = reopened.backfill_conversation_passages(
            owner,
            resume_at_conversation_id=cursor,
            limit=2,
        )
        assert resumed["anchors_written"] == 2
        assert _child_count(reopened, conversation_id) == 3
        assert _parent(reopened, conversation_id)["indexed_through_message_id"] == message_ids[-1]
        assert _parent(reopened, conversation_id)["projection_status"] == "current"
        assert reopened.backfill_conversation_passages(owner, limit=2)["examined"] == 0
    finally:
        reopened.close()


def test_restart_without_ephemeral_tie_cursor_resumes_from_durable_parent(settings: Any) -> None:
    owner = "conversation-writer-lost-cursor"
    first = init_storage(settings)
    try:
        conversation_id, message_ids = _conversation(first, owner, ("one", "two", "three"))
        first_page = first.backfill_conversation_passages(owner, limit=1)
        assert first_page["next_resume_conversation_id"] is not None
        assert _child_count(first, conversation_id) == 1
    finally:
        first.close()

    reopened = init_storage(settings)
    try:
        resumed = reopened.backfill_conversation_passages(
            owner,
            resume_at_conversation_id=None,
            limit=2,
        )
        assert resumed["anchors_written"] == 2
        assert _child_count(reopened, conversation_id) == 3
        assert _parent(reopened, conversation_id)["indexed_through_message_id"] == message_ids[-1]
        assert _parent(reopened, conversation_id)["projection_status"] == "current"
    finally:
        reopened.close()


def test_more_than_active_window_converges_with_the_returned_scan_cursor(
    storage: Any,
) -> None:
    owner = "conversation-writer-wide-fairness"
    conversations = [_conversation(storage, owner, (f"body-{index}",))[0] for index in range(40)]

    first = storage.backfill_conversation_passages(owner, limit=64)
    assert first["examined"] == first["anchors_written"] == first["current"] == 32
    assert sum(_parent(storage, item)["projection_status"] == "current" for item in conversations) == 32

    second = storage.backfill_conversation_passages(
        owner,
        resume_at_conversation_id=first["next_resume_conversation_id"],
        limit=64,
    )
    assert second["examined"] == second["anchors_written"] == second["current"] == 8
    assert second["has_more"] is False
    assert all(_parent(storage, item)["projection_status"] == "current" for item in conversations)


def test_33_conversations_limit_one_terminates_on_the_33rd_tick(storage: Any) -> None:
    owner = "conversation-writer-exact-page-terminal"
    conversations = [_conversation(storage, owner, (f"body-{index}",))[0] for index in range(33)]
    cursor: str | None = None

    for tick in range(33):
        report = storage.backfill_conversation_passages(
            owner,
            resume_at_conversation_id=cursor,
            limit=1,
        )
        assert report["examined"] == report["anchors_written"] == report["current"] == 1
        if tick < 32:
            assert report["has_more"] is True
            cursor = report["next_resume_conversation_id"]
            assert isinstance(cursor, str)
        else:
            assert report["has_more"] is False
            assert report["next_resume_conversation_id"] is None

    assert all(_parent(storage, item)["projection_status"] == "current" for item in conversations)


def test_reason_scan_fails_closed_when_an_owner_projection_is_missing(storage: Any) -> None:
    owner = "conversation-writer-missing-projection"
    conversation_id, _ = _conversation(storage, owner, ("body",))
    with storage.transaction() as conn:
        guard = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_projection_bd_validate'"""
        ).fetchone()
        assert guard is not None and type(guard["sql"]) is str
        conn.execute("DROP TRIGGER conversation_passage_projection_bd_validate")
        conn.execute(
            "DELETE FROM conversation_passage_projections WHERE conversation_id=?",
            (conversation_id,),
        )
        conn.execute(str(guard["sql"]))  # nosec B608 - exact canonical SQLite DDL

    with pytest.raises(sqlite3.DatabaseError, match="reason scan projection"):
        storage.backfill_conversation_passages(owner, limit=1)


def test_reason_scan_fails_closed_on_a_malformed_retry_discriminator(storage: Any) -> None:
    owner = "conversation-writer-malformed-discriminator"
    conversation_id, _ = _conversation(storage, owner, ("body",))
    with storage.transaction() as conn:
        guard = conn.execute(
            """SELECT sql FROM sqlite_master WHERE type='trigger'
                 AND name='conversation_passage_projection_bu_validate'"""
        ).fetchone()
        assert guard is not None and type(guard["sql"]) is str
        conn.execute("DROP TRIGGER conversation_passage_projection_bu_validate")
        conn.execute("PRAGMA ignore_check_constraints=ON")
        try:
            conn.execute(
                """UPDATE conversation_passage_projections
                      SET passage_index_revision='malformed-revision'
                    WHERE conversation_id=?""",
                (conversation_id,),
            )
        finally:
            conn.execute("PRAGMA ignore_check_constraints=OFF")
            conn.execute(str(guard["sql"]))  # nosec B608 - exact canonical SQLite DDL

    with pytest.raises(sqlite3.DatabaseError, match="reason scan projection"):
        storage.backfill_conversation_passages(owner, limit=1)


@pytest.mark.parametrize("settled_count", (100, 5_000))
def test_reason_scan_vm_work_is_bounded_before_filtering_settled_siblings(
    storage: Any,
    settled_count: int,
) -> None:
    owner = f"conversation-writer-reason-bound-{settled_count}"
    storage.ensure_user(owner)
    timestamp = "2026-08-30T00:00:00+00:00"
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES(?,?,'settled',?,?)""",
            ((f"conv_{index:016x}", owner, timestamp, timestamp) for index in range(settled_count)),
        )
        conn.execute(
            """UPDATE conversation_passage_projections
                  SET indexed_conversation_revision_sha256=?,passage_set_sha256=?,
                      projection_status='current',incomplete_reason=NULL
                WHERE conversation_id IN (
                      SELECT id FROM conversations WHERE user_id=?
                )""",
            (
                CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
                CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
                owner,
            ),
        )
        conn.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES('conv_ffffffffffffffff',?,'retryable',?,?)""",
            (owner, timestamp, timestamp),
        )
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(
                   'msg_ffffffffffffffff','conv_ffffffffffffffff',?,'user',
                   'retryable tail','{}',NULL,?
               )""",
            (owner, timestamp),
        )

    instruction_blocks = 0

    def progress() -> int:
        nonlocal instruction_blocks
        instruction_blocks += 1
        return 0

    storage.conn.set_progress_handler(progress, 100)
    try:
        report = storage.backfill_conversation_passages(owner, limit=1)
    finally:
        storage.conn.set_progress_handler(None, 0)

    assert report["examined"] == report["anchors_written"] == 0
    assert report["has_more"] is True
    assert instruction_blocks < 150


def test_live_source_changed_tail_and_historical_backfill_share_the_same_batch(
    storage: Any,
) -> None:
    owner = "conversation-writer-two-lane-fairness"
    live = "conv_0000000000000000"
    timestamp = "2026-08-30T00:00:00+00:00"
    storage.ensure_user(owner)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at)
               VALUES(?,?,'live',?,?)""",
            (live, owner, timestamp, timestamp),
        )
    for body in ("live-0", "live-1", "live-2"):
        storage.store_message(live, owner, "user", body)
    assert storage.backfill_conversation_passages(owner, limit=3)["current"] == 1
    storage.store_message(live, owner, "assistant", "live-tail")
    historical = [_conversation(storage, owner, (f"history-{index}",))[0] for index in range(40)]

    report = storage.backfill_conversation_passages(owner, limit=2)

    assert report["examined"] == report["anchors_written"] == report["current"] == 2
    assert _parent(storage, live)["projection_status"] == "current"
    assert sum(_parent(storage, item)["projection_status"] == "current" for item in historical) == 1


def test_owner_writer_never_advances_a_foreign_principal(storage: Any) -> None:
    owner = "conversation-writer-owned"
    foreign = "conversation-writer-foreign"
    owned, _ = _conversation(storage, owner, ("owned",))
    foreign_conversation, _ = _conversation(storage, foreign, ("foreign",))

    report = storage.backfill_conversation_passages(owner, limit=64)

    assert report["examined"] == report["anchors_written"] == report["current"] == 1
    assert _parent(storage, owned)["projection_status"] == "current"
    assert _parent(storage, foreign_conversation)["projection_status"] == "incomplete"
    assert _child_count(storage, foreign_conversation) == 0


def test_maximum_batch_is_bounded_at_256_representative_synthetic_anchors(storage: Any) -> None:
    owner = "conversation-writer-representative"
    bodies = tuple(f"representative-{index:04d}-" + ("x" * 96) for index in range(256))
    conversation_id, _ = _conversation(storage, owner, bodies)
    instruction_blocks = 0

    def progress() -> int:
        nonlocal instruction_blocks
        instruction_blocks += 1
        return 0

    storage.conn.set_progress_handler(progress, 1_000)
    try:
        report = storage.backfill_conversation_passages(owner, limit=256)
    finally:
        storage.conn.set_progress_handler(None, 0)

    assert report["examined"] == report["anchors_written"] == 256
    assert report["current"] == 1
    assert report["has_more"] is False
    assert report["message_bytes_examined"] == sum(len(item.encode()) for item in bodies)
    assert _child_count(storage, conversation_id) == 256
    # Every callback is 1,000 SQLite VM instructions. This fixed ceiling is
    # deliberately generous for loaded CI while still detecting an accidental
    # corpus-scale or non-terminating writer contour at the released max page.
    assert instruction_blocks < 250_000


def test_source_changed_appends_from_the_durable_parent_prefix(storage: Any) -> None:
    owner = "conversation-writer-append"
    conversation_id, _ = _conversation(storage, owner, ("old",))
    assert storage.backfill_conversation_passages(owner, limit=1)["current"] == 1

    appended = storage.store_message(conversation_id, owner, "assistant", "new")
    invalidated = _parent(storage, conversation_id)
    assert invalidated["projection_status"] == "incomplete"
    assert invalidated["incomplete_reason"] == "source_changed"
    assert invalidated["passage_count"] == 1

    report = storage.backfill_conversation_passages(owner, limit=1)

    parent = _parent(storage, conversation_id)
    assert report["anchors_written"] == 1
    assert report["current"] == 1
    assert parent["passage_count"] == 2
    assert parent["indexed_through_message_id"] == appended["id"]
    assert parent["projection_status"] == "current"


def test_writer_rolls_back_child_and_parent_together(storage: Any) -> None:
    owner = "conversation-writer-rollback"
    conversation_id, _ = _conversation(storage, owner, ("rollback",))

    with pytest.raises(RuntimeError, match="force rollback"), storage.transaction() as conn:
        report = backfill_conversation_passages_in_transaction(
            conn,
            principal_id=owner,
            resume_at_conversation_id=None,
            limit=1,
        )
        assert report["anchors_written"] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            == 1
        )
        raise RuntimeError("force rollback")

    assert _child_count(storage, conversation_id) == 0
    parent = _parent(storage, conversation_id)
    assert parent["passage_count"] == 0
    assert parent["projection_status"] == "incomplete"


def test_writer_savepoint_prevents_orphan_when_outer_transaction_catches(storage: Any) -> None:
    owner = "conversation-writer-savepoint"
    conversation_id, _ = _conversation(storage, owner, ("atomic",))

    with storage.transaction() as conn:
        conn.execute(
            """CREATE TEMP TRIGGER force_projection_failure
               BEFORE UPDATE ON conversation_passage_projections
               WHEN NEW.passage_count>OLD.passage_count
               BEGIN
                   SELECT RAISE(ABORT,'forced_projection_failure');
               END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced_projection_failure"):
            backfill_conversation_passages_in_transaction(
                conn,
                principal_id=owner,
                resume_at_conversation_id=None,
                limit=1,
            )
        # The caller deliberately catches and commits its outer transaction.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM conversation_passages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            == 0
        )
        conn.execute("DROP TRIGGER force_projection_failure")

    assert _child_count(storage, conversation_id) == 0
    assert _parent(storage, conversation_id)["passage_count"] == 0


def test_writer_enforces_a_hard_body_byte_budget_without_loading_oversized_source(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "conversation-writer-budget"
    byte_budget = writer_module.CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES
    first_body = "a" * (byte_budget - 1)
    conversation_id, _ = _conversation(storage, owner, (first_body, "tail"))

    first = storage.backfill_conversation_passages(owner, limit=2)

    assert first["examined"] == 2
    assert first["anchors_written"] == 1
    assert first["message_bytes_examined"] == byte_budget - 1
    assert first["message_byte_budget"] == byte_budget
    assert first["byte_budget_reached"] is True
    assert first["has_more"] is True
    assert _parent(storage, conversation_id)["passage_count"] == 1

    second = storage.backfill_conversation_passages(owner, limit=1)
    assert second["anchors_written"] == 1
    assert second["current"] == 1
    assert _parent(storage, conversation_id)["projection_status"] == "current"

    sole, _ = _conversation(storage, owner, ("x" * (byte_budget + 1),))
    assert _parent(storage, sole)["incomplete_reason"] == "source_unavailable"

    def forbidden_body_load(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("an oversized source body was selected")

    monkeypatch.setattr(writer_module, "_source_row", forbidden_body_load)
    oversized = storage.backfill_conversation_passages(
        owner,
        resume_at_conversation_id=sole,
        limit=1,
    )
    assert oversized["examined"] == oversized["explicit_incomplete"] == 0
    assert oversized["anchors_written"] == oversized["message_bytes_examined"] == 0
    assert oversized["message_byte_budget"] == byte_budget
    assert oversized["byte_budget_reached"] is False
    assert oversized["has_more"] is False


def test_late_oversized_source_preserves_a_large_authenticated_prefix_in_bounded_work(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "conversation-writer-late-oversized"
    bodies = tuple(f"accepted-prefix-{index:04d}-" + ("x" * 1_000) for index in range(256))
    conversation_id, _ = _conversation(storage, owner, bodies)
    assert storage.backfill_conversation_passages(owner, limit=256)["current"] == 1
    accepted = _parent(storage, conversation_id)
    storage.store_message(
        conversation_id,
        owner,
        "assistant",
        "x" * (writer_module.CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
    )

    def forbidden_body_load(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("a rejected oversized tail body was selected")

    monkeypatch.setattr(writer_module, "_source_row", forbidden_body_load)
    instruction_blocks = 0

    def progress() -> int:
        nonlocal instruction_blocks
        instruction_blocks += 1
        return 0

    storage.conn.set_progress_handler(progress, 100)
    try:
        report = storage.backfill_conversation_passages(owner, limit=1)
    finally:
        storage.conn.set_progress_handler(None, 0)

    current = _parent(storage, conversation_id)
    assert report["examined"] == 1 and report["anchors_written"] == 0
    assert report["message_bytes_examined"] == 0
    assert report["byte_budget_reached"] is False and report["has_more"] is True
    assert _child_count(storage, conversation_id) == 256
    assert current["passage_count"] == accepted["passage_count"] == 256
    assert current["indexed_conversation_revision_sha256"] == accepted["indexed_conversation_revision_sha256"]
    assert current["passage_set_sha256"] == accepted["passage_set_sha256"]
    assert current["incomplete_reason"] == "source_changed"
    assert instruction_blocks < 1_000


def test_permanently_oversized_tail_does_not_starve_a_fit_sibling(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "conversation-writer-oversized-sibling"
    blocked, _ = _conversation(storage, owner, ("seed",))
    assert storage.backfill_conversation_passages(owner, limit=1)["current"] == 1
    storage.store_message(
        blocked,
        owner,
        "assistant",
        "x" * (writer_module.CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES + 1),
    )
    sibling, _ = _conversation(storage, owner, ("ok",))

    report = storage.backfill_conversation_passages(owner, limit=2)

    assert report["examined"] == 2
    assert report["anchors_written"] == report["current"] == 1
    assert report["message_bytes_examined"] == 2
    assert _parent(storage, blocked)["incomplete_reason"] == "source_changed"
    assert _parent(storage, blocked)["passage_count"] == 1
    assert _parent(storage, sibling)["projection_status"] == "current"


def test_invalid_source_stays_retryable_without_partial_children(storage: Any) -> None:
    owner = "conversation-writer-unavailable"
    conversation_id, message_ids = _conversation(storage, owner, ("bad timestamp",))
    with storage.transaction() as conn:
        trigger_rows = conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE type='trigger' AND name IN (
                       'messages_are_never_rewritten',
                       'conversation_passage_message_au_reset'
                 ) ORDER BY name"""
        ).fetchall()
        trigger_sql = {str(row["name"]): str(row["sql"]) for row in trigger_rows}
        assert set(trigger_sql) == {
            "messages_are_never_rewritten",
            "conversation_passage_message_au_reset",
        }
        for trigger_name in trigger_sql:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')  # nosec B608 - SQLite-owned names
        try:
            conn.execute(
                "UPDATE messages SET created_at='not-a-timestamp' WHERE id=?",
                (message_ids[0],),
            )
        finally:
            for sql in trigger_sql.values():
                conn.execute(sql)  # nosec B608 - exact SQLite-owned canonical DDL

    report = storage.backfill_conversation_passages(owner, limit=1)

    parent = _parent(storage, conversation_id)
    assert report["examined"] == 1
    assert report["explicit_incomplete"] == 0
    assert report["anchors_written"] == 0
    assert parent["projection_status"] == "incomplete"
    assert parent["incomplete_reason"] == "backfill_pending"
    assert parent["passage_count"] == 0
    assert _child_count(storage, conversation_id) == 0
    assert report["has_more"] is True


def test_malformed_utf8_source_is_deferred_without_starving_a_valid_sibling(storage: Any) -> None:
    owner = "conversation-writer-malformed-utf8"
    poisoned = storage.create_conversation(owner)
    timestamp = "2026-08-30T00:00:00+00:00"
    with storage.transaction() as conn:
        trigger = conn.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='trigger'
                   AND name='conversation_passage_message_bi_identity_immutable'"""
        ).fetchone()
        assert trigger is not None and type(trigger["sql"]) is str
        conn.execute('DROP TRIGGER "conversation_passage_message_bi_identity_immutable"')
        try:
            conn.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                   ) VALUES(
                       'msg_eeeeeeeeeeeeeeee',?,?,'user',CAST(x'80' AS TEXT),'{}',NULL,?
                   )""",  # nosec B608 - fixed malformed UTF-8 regression literal
                (poisoned["id"], owner, timestamp),
            )
        finally:
            conn.execute(str(trigger["sql"]))  # nosec B608 - exact canonical SQLite DDL
    healthy, _ = _conversation(storage, owner, ("healthy sibling",))

    report = storage.backfill_conversation_passages(owner, limit=2)

    assert report["examined"] == 2
    assert report["anchors_written"] == report["current"] == 1
    assert report["has_more"] is True
    assert _parent(storage, poisoned["id"])["projection_status"] == "incomplete"
    assert _child_count(storage, poisoned["id"]) == 0
    assert _parent(storage, healthy)["projection_status"] == "current"


def test_oversized_utf8_poison_is_body_free_and_cannot_starve_a_valid_sibling(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = "conversation-writer-oversized-utf8"
    poisoned = storage.create_conversation(owner)
    timestamp = "2026-08-30T00:00:00+00:00"
    with storage.transaction() as conn:
        guard_rows = conn.execute(
            """SELECT name,sql FROM sqlite_master
                 WHERE type='trigger' AND name IN (
                       'conversation_passage_message_bi_identity_immutable',
                       'conversation_passage_message_ai_invalidate'
                 ) ORDER BY name"""
        ).fetchall()
        guards = {str(row["name"]): str(row["sql"]) for row in guard_rows}
        assert set(guards) == {
            "conversation_passage_message_ai_invalidate",
            "conversation_passage_message_bi_identity_immutable",
        }
        for name in guards:
            conn.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - authenticated names
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(
                   'msg_ffffffffffffffff',?,?,'user',
                   CAST(x'80'||zeroblob(4194304) AS TEXT),'{}',NULL,?
               )""",
            (poisoned["id"], owner, timestamp),
        )
        poisoned_rowid = int(
            conn.execute("SELECT rowid FROM messages WHERE id='msg_ffffffffffffffff'").fetchone()[0]
        )
        for sql in guards.values():
            conn.execute(sql)  # nosec B608 - exact authenticated SQLite DDL
    healthy_body = "healthy sibling"
    healthy, _ = _conversation(storage, owner, (healthy_body,))

    source_row = writer_module._source_row
    utf8_sizes: list[int] = []
    utf8_valid = schema_module._utf8_valid

    def guarded_source_row(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["source_rowid"] != poisoned_rowid
        return source_row(*args, **kwargs)

    def bounded_utf8_valid(value: object) -> int:
        size = len(value) if type(value) is bytes else -1
        utf8_sizes.append(size)
        if size > writer_module.CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES:
            raise AssertionError("writer evaluated UTF-8 over an oversized body")
        return utf8_valid(value)

    monkeypatch.setattr(writer_module, "_source_row", guarded_source_row)
    monkeypatch.setattr(schema_module, "_utf8_valid", bounded_utf8_valid)
    report = storage.backfill_conversation_passages(owner, limit=2)

    assert report["examined"] == 2
    assert report["anchors_written"] == report["current"] == 1
    assert report["explicit_incomplete"] == 0
    assert report["message_bytes_examined"] == len(healthy_body.encode("utf-8"))
    assert report["has_more"] is True
    assert utf8_sizes and max(utf8_sizes) == len(healthy_body.encode("utf-8"))
    assert _parent(storage, poisoned["id"])["incomplete_reason"] == "backfill_pending"
    assert _child_count(storage, poisoned["id"]) == 0
    assert _parent(storage, healthy)["projection_status"] == "current"


def test_writer_requires_transaction_and_rejects_malformed_limits_and_cursors(storage: Any) -> None:
    owner = "conversation-writer-validation"
    _conversation(storage, owner, ("body",))

    with pytest.raises(RuntimeError, match="caller-owned transaction"):
        backfill_conversation_passages_in_transaction(
            storage.conn,
            principal_id=owner,
            resume_at_conversation_id=None,
        )
    for bad_limit in (True, 0, 257, 1.0):
        with pytest.raises(ValueError, match="limit"):
            storage.backfill_conversation_passages(owner, limit=bad_limit)
    for bad_cursor in ("", " padded", "line\nbreak", "x" * 201, 1):
        with pytest.raises(ValueError, match="cursor"):
            storage.backfill_conversation_passages(
                owner,
                resume_at_conversation_id=bad_cursor,
            )


def test_writer_populates_only_the_fts_derivative_with_message_terms(storage: Any) -> None:
    owner = "conversation-writer-fts"
    conversation_id, _ = _conversation(storage, owner, ("quasarneedle",))

    storage.backfill_conversation_passages(owner, limit=1)

    ordinary_columns = {
        str(row[1])
        for table in ("conversation_passage_projections", "conversation_passages")
        for row in storage.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608 - fixed table names
    }
    assert "content" not in ordinary_columns
    matches = storage.execute(
        "SELECT rowid FROM conversation_passages_fts WHERE conversation_passages_fts MATCH ?",
        ("quasarneedle",),
    ).fetchall()
    assert len(matches) == 1
    assert _parent(storage, conversation_id)["projection_status"] == "current"
