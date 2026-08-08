"""Tenant-integrity regressions for owner-facing event timeline reads."""

from __future__ import annotations

from typing import Any

from friday.storage.models import Entity, EntityType, utc_now


def _seed_foreign_entity_time_carrier(storage: Any) -> tuple[str, Entity, Entity]:
    owner_id = "event-owner"
    foreign_id = "foreign-time-writer"
    storage.ensure_user(owner_id)
    storage.ensure_user(foreign_id)

    legitimate = Entity(
        id="ent-owner-legitimate-event",
        user_id=owner_id,
        name="Legitimate owner event",
        entity_type=EntityType.EVENT,
    )
    poisoned = Entity(
        id="ent-owner-foreign-time-carrier",
        user_id=owner_id,
        name="FOREIGN ENTITY TIME SENTINEL",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(legitimate)
    storage.create_entity(poisoned)

    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, occurred_end, precision, source, updated_at)
               VALUES(?, ?, ?, NULL, 'day', 'document:tenant-integrity-test', ?)""",
            (
                (legitimate.id, owner_id, "2026-08-08", utc_now()),
                (poisoned.id, foreign_id, "2026-08-09", utc_now()),
            ),
        )

    # Prove that the fixture contains the synthetic cross-tenant corruption whose
    # propagation the public owner-facing methods must contain.
    corrupt = storage.execute(
        "SELECT user_id FROM entity_time WHERE entity_id=?",
        (poisoned.id,),
    ).fetchone()
    assert corrupt is not None and corrupt["user_id"] == foreign_id

    return owner_id, legitimate, poisoned


def test_owner_event_list_rejects_foreign_entity_time_carrier(storage):
    """A corrupt foreign time row cannot turn an owner's entity into a listed event."""

    owner_id, legitimate, poisoned = _seed_foreign_entity_time_carrier(storage)

    listed = storage.list_events_in_range(owner_id)

    assert [row["entity_id"] for row in listed] == [legitimate.id]
    assert all(row["name"] != poisoned.name for row in listed)


def test_owner_event_count_rejects_foreign_entity_time_carrier(storage):
    """A corrupt foreign time row cannot inflate an owner's exact event count."""

    owner_id, _, _ = _seed_foreign_entity_time_carrier(storage)

    assert storage.count_events_in_range(owner_id) == 1
