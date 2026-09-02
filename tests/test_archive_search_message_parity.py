from __future__ import annotations

from typing import Any

from friday.retrieval.contracts import LifecycleState, MessageRole
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    ArchiveMessageSearchPage,
    select_authorized_archive_message_page_in_transaction,
)

_OWNER = "archive-message-parity-owner"
_FOREIGN = "archive-message-parity-foreign"
_MISTYPED_QUERY = "Uhfabr lt;ehcnd"
_REPAIRED_TEXT = "График дежурств"
_MISTYPED_TOPIC_QUERY = "фдзрф иуеф"


def _archive_page(
    storage: Any,
    *,
    conversation_id: str,
    boundary_user_message_id: str,
) -> ArchiveMessageSearchPage:
    with storage.transaction() as conn:
        page = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=_OWNER,
            query=_MISTYPED_QUERY,
            scope=ArchiveMessageScope.CURRENT,
            conversation_id=conversation_id,
            boundary_user_message_id=boundary_user_message_id,
        )
    assert page is not None
    return page


def _legacy_ids(
    storage: Any,
    *,
    conversation_id: str,
    boundary_user_message_id: str,
) -> list[str]:
    return [
        str(row["id"])
        for row in storage.search_messages(
            _OWNER,
            _MISTYPED_QUERY,
            conversation_id=conversation_id,
            before_message_id=boundary_user_message_id,
        )
    ]


def test_archive_selector_matches_legacy_keyboard_layout_recall(storage: Any) -> None:
    storage.ensure_user(_OWNER)
    conversation = storage.create_conversation(_OWNER, "message parity corpus")
    intended = storage.store_message(
        conversation["id"],
        _OWNER,
        "assistant",
        _REPAIRED_TEXT,
    )
    boundary = storage.store_message(
        conversation["id"],
        _OWNER,
        "user",
        "current archive request",
    )

    legacy_ids = _legacy_ids(
        storage,
        conversation_id=conversation["id"],
        boundary_user_message_id=boundary["id"],
    )
    page = _archive_page(
        storage,
        conversation_id=conversation["id"],
        boundary_user_message_id=boundary["id"],
    )

    assert legacy_ids == [intended["id"]]
    assert [hit.message.message_id for hit in page.hits] == legacy_ids
    assert page.query == _MISTYPED_QUERY
    assert page.total == 1


def test_original_authorized_hit_suppresses_keyboard_layout_retry(storage: Any) -> None:
    storage.ensure_user(_OWNER)
    conversation = storage.create_conversation(_OWNER, "message parity precedence")
    repaired = storage.store_message(
        conversation["id"],
        _OWNER,
        "assistant",
        _REPAIRED_TEXT,
    )
    original = storage.store_message(
        conversation["id"],
        _OWNER,
        "assistant",
        f"literal {_MISTYPED_QUERY}",
    )
    boundary = storage.store_message(
        conversation["id"],
        _OWNER,
        "user",
        "current archive request",
    )

    legacy_ids = _legacy_ids(
        storage,
        conversation_id=conversation["id"],
        boundary_user_message_id=boundary["id"],
    )
    page = _archive_page(
        storage,
        conversation_id=conversation["id"],
        boundary_user_message_id=boundary["id"],
    )

    assert legacy_ids == [original["id"]]
    assert [hit.message.message_id for hit in page.hits] == legacy_ids
    assert repaired["id"] not in legacy_ids
    assert page.total == 1


def test_foreign_only_layout_repair_cannot_affect_owner_result(storage: Any) -> None:
    storage.ensure_user(_OWNER)
    storage.ensure_user(_FOREIGN)
    owner_conversation = storage.create_conversation(_OWNER, "owner message boundary")
    storage.store_message(
        owner_conversation["id"],
        _OWNER,
        "assistant",
        "unrelated authorized history",
    )
    foreign_conversation = storage.create_conversation(_FOREIGN, "foreign private history")
    storage.store_message(
        foreign_conversation["id"],
        _FOREIGN,
        "assistant",
        _REPAIRED_TEXT,
    )
    boundary = storage.store_message(
        owner_conversation["id"],
        _OWNER,
        "user",
        "current archive request",
    )

    legacy_ids = _legacy_ids(
        storage,
        conversation_id=owner_conversation["id"],
        boundary_user_message_id=boundary["id"],
    )
    page = _archive_page(
        storage,
        conversation_id=owner_conversation["id"],
        boundary_user_message_id=boundary["id"],
    )

    assert legacy_ids == []
    assert page.hits == ()
    assert page.total == 0
    assert page.examined == 1
    assert page.query == _MISTYPED_QUERY


def test_layout_retry_reuses_frozen_one_shot_controls(storage: Any) -> None:
    storage.ensure_user(_OWNER)
    conversation = storage.create_conversation(_OWNER, "one-shot repair controls")
    intended = storage.store_message(conversation["id"], _OWNER, "assistant", _REPAIRED_TEXT)
    boundary = storage.store_message(conversation["id"], _OWNER, "user", "current request")

    with storage.transaction() as conn:
        page = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=_OWNER,
            query=_MISTYPED_QUERY,
            scope=ArchiveMessageScope.CURRENT,
            conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
            roles=(item for item in (MessageRole.ASSISTANT,)),
            lifecycle_states=(item for item in (LifecycleState.ACTIVE,)),
        )

    assert page is not None
    assert [hit.message.message_id for hit in page.hits] == [intended["id"]]
    assert page.roles == (MessageRole.ASSISTANT,)
    assert page.lifecycle_states == (LifecycleState.ACTIVE,)


def test_candidate_limit_is_applied_after_conversation_deduplication(storage: Any) -> None:
    query = "conversation candidate crowding needle"
    storage.ensure_user(_OWNER)
    target_conversation = storage.create_conversation(_OWNER, "older target")
    target = storage.store_message(target_conversation["id"], _OWNER, "assistant", query)
    crowded_conversation = storage.create_conversation(_OWNER, "newer crowded source")
    for ordinal in range(100):
        storage.store_message(
            crowded_conversation["id"],
            _OWNER,
            "assistant",
            f"{query} repeated {ordinal:03d}",
        )
    boundary_conversation = storage.create_conversation(_OWNER, "accepted boundary")
    boundary = storage.store_message(boundary_conversation["id"], _OWNER, "user", "current request")

    with storage.transaction() as conn:
        page = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=_OWNER,
            query=query,
            scope=ArchiveMessageScope.ALL,
            conversation_id=boundary_conversation["id"],
            boundary_user_message_id=boundary["id"],
            limit=2,
        )

    assert page is not None
    assert page.total == page.returned == 2
    assert page.has_more is False
    assert {hit.message.conversation_id for hit in page.hits} == {
        crowded_conversation["id"],
        target_conversation["id"],
    }
    assert any(hit.message.message_id == target["id"] for hit in page.hits)
    assert {hit.source_rank for hit in page.hits} == {1, 2}


def test_repaired_topic_order_matches_principal_local_legacy_relevance(storage: Any) -> None:
    storage.ensure_user(_OWNER)
    relevant_conversation = storage.create_conversation(_OWNER, "older compact topic")
    relevant = storage.store_message(
        relevant_conversation["id"],
        _OWNER,
        "assistant",
        "alpha alpha alpha beta beta",
    )
    recent_conversation = storage.create_conversation(_OWNER, "newer padded topic")
    recent = storage.store_message(
        recent_conversation["id"],
        _OWNER,
        "assistant",
        "alpha beta " + "context " * 40,
    )
    boundary_conversation = storage.create_conversation(_OWNER, "accepted topic boundary")
    boundary = storage.store_message(
        boundary_conversation["id"],
        _OWNER,
        "user",
        "current archive request",
    )
    storage.execute(
        "UPDATE messages SET created_at=? WHERE id=?",
        ("2026-05-05T08:00:00+00:00", relevant["id"]),
    )
    storage.execute(
        "UPDATE messages SET created_at=? WHERE id=?",
        ("2026-05-05T09:00:00+00:00", recent["id"]),
    )
    storage.execute(
        "UPDATE messages SET created_at=? WHERE id=?",
        ("2026-05-06T10:00:00+00:00", boundary["id"]),
    )
    storage.commit()

    legacy_ids = [
        str(row["id"])
        for row in storage.search_messages(
            _OWNER,
            _MISTYPED_TOPIC_QUERY,
            limit=2,
            before_message_id=boundary["id"],
        )
    ]
    with storage.transaction() as conn:
        page = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=_OWNER,
            query=_MISTYPED_TOPIC_QUERY,
            scope=ArchiveMessageScope.ALL,
            conversation_id=boundary_conversation["id"],
            boundary_user_message_id=boundary["id"],
            roles=(MessageRole.ASSISTANT,),
            limit=2,
        )

    assert page is not None
    assert legacy_ids == [relevant["id"], recent["id"]]
    assert [hit.message.message_id for hit in page.hits] == legacy_ids
    assert tuple(hit.source_rank for hit in page.hits) == (1, 2)
