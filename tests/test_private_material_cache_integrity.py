"""Exact privacy-authority, legacy-identity and startup migration regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from friday.storage import FridayStorage
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    KnowledgeObject,
    RawObject,
    utc_now,
)


def _insert_entity(
    storage: FridayStorage,
    entity_id: str,
    *,
    name: str,
    aliases_json: str = "[]",
    description: str = "",
    version: int = 1,
) -> None:
    now = utc_now()
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entities(
                   id,user_id,name,normalized_name,entity_type,aliases_json,
                   description,metadata_json,version,created_at,updated_at)
               VALUES(?, 'alice', ?, ?, 'event', ?, ?, '{}', ?, ?, ?)""",
            (entity_id, name, name.casefold(), aliases_json, description, version, now, now),
        )


def _authenticated_snapshot(
    entity_id: str,
    *,
    name: str,
    aliases_json: str,
    version: int = 1,
) -> str:
    return json.dumps(
        {
            "id": entity_id,
            "user_id": "alice",
            "name": name,
            "description": "",
            "aliases_json": aliases_json,
            "metadata_json": "{}",
            "version": version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_nested_encoded_and_malformed_identity_tokens_quarantine_current_and_history(
    storage: FridayStorage,
) -> None:
    storage.ensure_user("alice")
    variants = [
        (
            "nested",
            "Nested current source",
            json.dumps([{"legacy_alias": "PRIVATE-NESTED-ALIAS-71C9"}]),
            "PRIVATE-NESTED-ALIAS-71C9",
        ),
        (
            "encoded-object",
            "Encoded object current source",
            json.dumps([json.dumps({"legacy_alias": "PRIVATE-ENCODED-OBJECT-82D0"})]),
            "PRIVATE-ENCODED-OBJECT-82D0",
        ),
        (
            "double-encoded",
            "Double encoded current source",
            json.dumps([json.dumps("PRIVATE-DOUBLE-ENCODED-93E1")]),
            "PRIVATE-DOUBLE-ENCODED-93E1",
        ),
        (
            "json-name",
            json.dumps({"legacy_name": "PRIVATE-JSON-NAME-A4F2"}),
            "[]",
            "PRIVATE-JSON-NAME-A4F2",
        ),
        (
            "malformed",
            "Malformed alias current source",
            '["PRIVATE-MALFORMED-ALIAS-B503"',
            "PRIVATE-MALFORMED-ALIAS-B503",
        ),
    ]

    current_ids: list[tuple[str, str, str]] = []
    history_ids: list[tuple[str, str, str]] = []
    for label, private_name, aliases, secret in variants:
        source_id = f"ent-current-identity-{label}"
        carrier_id = f"ent-current-carrier-{label}"
        _insert_entity(storage, source_id, name=private_name, aliases_json=aliases)
        _insert_entity(
            storage,
            carrier_id,
            name=f"Current carrier {label}",
            description=secret,
        )
        current_ids.append((source_id, carrier_id, secret))

        history_source_id = f"ent-history-identity-{label}"
        history_carrier_id = f"ent-history-carrier-{label}"
        _insert_entity(
            storage,
            history_source_id,
            name=f"Clean current identity {label}",
            version=2,
        )
        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO entity_versions(
                       id,user_id,entity_id,version,snapshot_json,created_at)
                   VALUES(?, 'alice', ?, 1, ?, ?)""",
                (
                    f"entv-history-identity-{label}",
                    history_source_id,
                    _authenticated_snapshot(
                        history_source_id,
                        name=private_name,
                        aliases_json=aliases,
                    ),
                    utc_now(),
                ),
            )
        _insert_entity(
            storage,
            history_carrier_id,
            name=f"History carrier {label}",
            description=secret,
        )
        history_ids.append((history_source_id, history_carrier_id, secret))

    with storage.transaction() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            [(entity_id, utc_now()) for entity_id, _, _ in current_ids + history_ids],
        )

    cached = {
        str(row[0])
        for row in storage.execute("SELECT entity_id FROM private_entity_material_cache").fetchall()
    }
    for source_id, carrier_id, secret in current_ids + history_ids:
        tokens = {
            str(row[0])
            for row in storage.execute(
                "SELECT name FROM private_entity_identity_tokens WHERE id=?",
                (source_id,),
            ).fetchall()
        }
        assert secret in tokens
        assert {source_id, carrier_id} <= cached
        assert storage.get_entity(source_id, "alice") is None
        assert storage.get_entity(carrier_id, "alice") is None


def test_private_identity_match_is_unicode_exact_without_numeric_prefix_false_positive(
    storage: FridayStorage,
) -> None:
    """Current/history aliases close exactly, but ``дело 1`` is not ``дело 10``."""

    storage.ensure_user("alice")
    current_alias = unicodedata.normalize("NFD", "ТАЙНА ЁЛКА")
    history_alias = unicodedata.normalize("NFD", "ИСТОРИЯ ЁЖ")
    source_id = "ent-private-token-boundary-source"
    _insert_entity(
        storage,
        source_id,
        name="дело 1",
        aliases_json=json.dumps([current_alias], ensure_ascii=False),
        version=2,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_versions(
                   id,user_id,entity_id,version,snapshot_json,created_at)
               VALUES('entv-private-token-boundary-source', 'alice', ?, 1, ?, ?)""",
            (
                source_id,
                _authenticated_snapshot(
                    source_id,
                    name="Старое точное имя",
                    aliases_json=json.dumps([history_alias], ensure_ascii=False),
                ),
                utc_now(),
            ),
        )
    prefix_id = "ent-private-token-boundary-prefix"
    current_carrier_id = "ent-private-token-boundary-current"
    history_carrier_id = "ent-private-token-boundary-history"
    _insert_entity(storage, prefix_id, name="дело 10")
    _insert_entity(storage, current_carrier_id, name="Current exact carrier", description="тайна ёлка")
    _insert_entity(
        storage,
        history_carrier_id,
        name="History exact carrier",
        description=unicodedata.normalize("NFC", history_alias).casefold(),
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (source_id, utc_now()),
        )

    cached = {
        str(row[0])
        for row in storage.execute("SELECT entity_id FROM private_entity_material_cache").fetchall()
    }
    assert {source_id, current_carrier_id, history_carrier_id} <= cached
    assert prefix_id not in cached
    assert storage.get_entity(prefix_id, "alice") is not None
    assert storage.get_entity(current_carrier_id, "alice") is None
    assert storage.get_entity(history_carrier_id, "alice") is None


def test_sparse_private_raw_lookup_uses_only_the_id_authority(
    storage: FridayStorage,
) -> None:
    """A point Raw read must scale with hidden IDs, not every public entity state."""

    from friday.storage._core import _private_identity_tokens_json
    from friday.storage._privacy import _not_private_raw_dependency

    storage.ensure_user("alice")
    historical_alias = unicodedata.normalize("NFD", "ИСТОРИЯ ЁЛКА 71C9")
    current_alias = unicodedata.normalize("NFD", "ТЕКУЩАЯ ЁЛКА 82D0")
    private = storage.create_entity(
        Entity(
            id="ent-sparse-token-private",
            user_id="alice",
            name="Sparse private identity",
            aliases_json=[historical_alias],
            entity_type=EntityType.EVENT,
        )
    )
    private.aliases_json = [current_alias]
    storage.update_entity(private)
    raw = RawObject(
        id="raw-sparse-token-public",
        user_id="alice",
        source="test",
        source_ref="sparse-token-public",
        raw_content="Independent public body",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id="ko-sparse-token-public",
            user_id="alice",
            raw_object_id=raw.id,
            content="Independent public knowledge",
            content_type="text",
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, utc_now()),
        )

    cached_tokens = {
        str(row[0])
        for row in storage.execute(
            "SELECT name FROM private_entity_material_cached_closure WHERE id=?",
            (private.id,),
        ).fetchall()
    }
    assert {"", historical_alias, current_alias} <= cached_tokens

    calls = 0

    def counted_identity_tokens(name: object, aliases: object) -> str:
        nonlocal calls
        calls += 1
        return _private_identity_tokens_json(name, aliases)

    storage.conn.create_function(
        "jericho_private_identity_tokens",
        2,
        counted_identity_tokens,
        deterministic=True,
    )
    assert storage.get_raw_object(raw.id, "alice") is not None

    assert calls == 0
    direct_tokens = {
        str(row[0])
        for row in storage.execute(
            "SELECT name FROM private_entity_material_cached_closure WHERE id=?",
            (private.id,),
        ).fetchall()
    }
    sparse_closure_calls = calls
    assert {"", historical_alias, current_alias} <= direct_tokens
    assert sparse_closure_calls > 0

    # These current rows and their authenticated version snapshots used to be
    # globally materialized once for every nested Raw -> KO -> Inbox predicate.
    # A cache-first plan must not call the identity UDF for any of them.
    for number in range(24):
        storage.create_entity(
            Entity(
                id=f"ent-sparse-token-decoy-{number}",
                user_id="alice",
                name=f"Independent public decoy {number}",
                entity_type=EntityType.CONCEPT,
            )
        )
    calls = 0
    assert storage.get_raw_object(raw.id, "alice") is not None
    assert calls == 0
    storage.execute(
        "SELECT name FROM private_entity_material_cached_closure WHERE id=?",
        (private.id,),
    ).fetchall()
    assert calls == sparse_closure_calls

    point_query = f"""SELECT r.id FROM raw_objects r
                       WHERE r.id=? AND r.user_id=?
                         AND {_not_private_raw_dependency("r")}"""  # nosec B608
    plan = [
        str(row["detail"])
        for row in storage.execute(
            "EXPLAIN QUERY PLAN " + point_query,
            (raw.id, "alice"),
        ).fetchall()
    ]
    assert not any("MATERIALIZE private_entity_identity_tokens" in detail for detail in plan)
    assert any(
        "derivative_cache" in detail and ("PRIMARY KEY" in detail or "SEARCH" in detail) for detail in plan
    )
    closure_plan = [
        str(row["detail"])
        for row in storage.execute(
            """EXPLAIN QUERY PLAN
               SELECT name FROM private_entity_material_cached_closure WHERE id=?""",
            (private.id,),
        ).fetchall()
    ]
    assert any("SEARCH v USING" in detail and "entity_id=?" in detail for detail in closure_plan)


def test_authenticated_alias_added_to_cached_private_history_rebuilds_every_derivative(
    storage: FridayStorage,
) -> None:
    """A hidden ID is unchanged, but its newly authenticated token is authority too."""

    storage.ensure_user("alice")
    hidden = storage.create_entity(
        Entity(
            id="ent-late-private-history-alias",
            user_id="alice",
            name="Original private identity",
            entity_type=EntityType.EVENT,
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (hidden.id, utc_now()),
        )

    late_alias = unicodedata.normalize("NFD", "ПОЗДНИЙ ИСТОРИЧЕСКИЙ ЁЖ D18A")
    raw = storage.store_raw_object(
        RawObject(
            id="raw-late-private-history-alias",
            user_id="alice",
            source="test",
            source_ref="late-private-history-alias",
            raw_content=late_alias,
            content_type="text",
        )
    )
    knowledge = storage.store_knowledge_object(
        KnowledgeObject(
            id="ko-late-private-history-alias",
            user_id="alice",
            raw_object_id=raw.id,
            content="Independent derived card",
            content_type="text",
        )
    )
    inbox = storage.store_inbox_item(
        InboxItem(
            id="inbox-late-private-history-alias",
            user_id="alice",
            raw_object_id=raw.id,
            knowledge_object_id=knowledge.id,
        )
    )
    assert storage.get_raw_object(raw.id, "alice") is not None

    assert storage.get_knowledge_object(knowledge.id, "alice") is not None
    assert storage.get_inbox_item(inbox.id, "alice") is not None

    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_versions(
                   id,user_id,entity_id,version,snapshot_json,created_at)
               VALUES('entv-late-private-history-alias', 'alice', ?, 41, ?, ?)""",
            (
                hidden.id,
                _authenticated_snapshot(
                    hidden.id,
                    name="Historical private identity",
                    aliases_json=json.dumps([late_alias], ensure_ascii=False),
                    version=41,
                ),
                utc_now(),
            ),
        )

    assert (
        storage.execute(
            "SELECT valid FROM private_entity_material_derivative_state WHERE singleton=1"
        ).fetchone()[0]
        == 1
    )
    assert storage.get_raw_object(raw.id, "alice") is None
    assert storage.get_knowledge_object(knowledge.id, "alice") is None
    assert storage.get_inbox_item(inbox.id, "alice") is None


def test_external_derivative_invalidation_is_fail_closed_and_a_new_thread_heals(
    storage: FridayStorage,
) -> None:
    """A schema-ready connection still rechecks MAIN state under a write lock."""

    storage.ensure_user("alice")
    raw = storage.store_raw_object(
        RawObject(
            id="raw-external-derivative-heal",
            user_id="alice",
            source="test",
            source_ref="before-external-write",
            raw_content="Independent public text",
            content_type="text",
        )
    )
    external = sqlite3.connect(storage.settings.database_path, timeout=10.0)
    try:
        external.execute(
            "UPDATE raw_objects SET source_ref='after-external-write' WHERE id=?",
            (raw.id,),
        )
        external.commit()
    finally:
        external.close()

    assert (
        storage.execute(
            "SELECT valid FROM private_entity_material_derivative_state WHERE singleton=1"
        ).fetchone()[0]
        == 0
    )
    assert storage.get_raw_object(raw.id, "alice") is None

    with ThreadPoolExecutor(max_workers=1) as executor:
        healed = executor.submit(storage.get_raw_object, raw.id, "alice").result(timeout=20)
    assert healed is not None
    assert healed["source_ref"] == "after-external-write"
    assert (
        storage.execute(
            "SELECT valid FROM private_entity_material_derivative_state WHERE singleton=1"
        ).fetchone()[0]
        == 1
    )
    assert storage.get_raw_object(raw.id, "alice") is not None

    external = sqlite3.connect(storage.settings.database_path, timeout=10.0)
    try:
        external.execute(
            "UPDATE raw_objects SET source_ref='healed-by-explicit-commit' WHERE id=?",
            (raw.id,),
        )
        external.commit()
    finally:
        external.close()
    assert (
        storage.execute(
            "SELECT valid FROM private_entity_material_derivative_state WHERE singleton=1"
        ).fetchone()[0]
        == 0
    )
    storage.commit()
    committed = storage.get_raw_object(raw.id, "alice")
    assert committed is not None and committed["source_ref"] == "healed-by-explicit-commit"

    external = sqlite3.connect(storage.settings.database_path, timeout=10.0)
    try:
        external.execute(
            "UPDATE raw_objects SET source_ref='healed-at-outer-commit' WHERE id=?",
            (raw.id,),
        )
        external.commit()
    finally:
        external.close()
    with storage.transaction():
        # The current writer owns a transaction, but ownership alone is never an
        # excuse to expose a stale allowlist. Publication happens at outer exit.
        assert storage.get_raw_object(raw.id, "alice") is None
    outer_healed = storage.get_raw_object(raw.id, "alice")
    assert outer_healed is not None and outer_healed["source_ref"] == "healed-at-outer-commit"


def test_nested_private_flip_rollback_restores_source_and_derivative_authority(
    storage: FridayStorage,
) -> None:
    storage.ensure_user("alice")
    private_name = "NESTED PRIVATE ROLLBACK 3E7D"
    hidden = storage.create_entity(
        Entity(
            id="ent-nested-private-rollback",
            user_id="alice",
            name=private_name,
            entity_type=EntityType.EVENT,
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (hidden.id, utc_now()),
        )
    raw = storage.store_raw_object(
        RawObject(
            id="raw-nested-private-rollback",
            user_id="alice",
            source="test",
            source_ref="nested-private-rollback",
            raw_content="Public before nested rollback",
            content_type="text",
        )
    )

    class _RollbackProbe(RuntimeError):
        pass

    with storage.transaction() as conn:
        with pytest.raises(_RollbackProbe), storage.transaction() as nested:
            nested.execute(
                "UPDATE raw_objects SET raw_content=? WHERE id=?",
                (private_name, raw.id),
            )
            assert (
                nested.execute("SELECT valid FROM private_entity_material_derivative_state").fetchone()[0]
                == 1
            )
            assert storage.get_raw_object(raw.id, "alice") is None
            raise _RollbackProbe
        restored = conn.execute(
            "SELECT raw_content FROM raw_objects WHERE id=?",
            (raw.id,),
        ).fetchone()
        assert restored is not None and restored[0] == "Public before nested rollback"
        assert storage.get_raw_object(raw.id, "alice") is not None
        assert conn.execute("SELECT valid FROM private_entity_material_derivative_state").fetchone()[0] == 1


def test_managed_ingest_updates_derivative_ids_without_per_row_global_rebuild(
    storage: FridayStorage,
) -> None:
    """Mutation volume is linear for the ordinary Raw -> KO -> Inbox path."""

    storage.ensure_user("alice")
    hidden = storage.create_entity(
        Entity(
            id="ent-managed-derivative-batch-private",
            user_id="alice",
            name="MANAGED DERIVATIVE BATCH PRIVATE 92F1",
            entity_type=EntityType.EVENT,
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (hidden.id, utc_now()),
        )

    changes_before = storage.conn.total_changes
    started = time.perf_counter()
    with storage.transaction():
        for number in range(100):
            raw = storage.store_raw_object(
                RawObject(
                    id=f"raw-managed-derivative-{number}",
                    user_id="alice",
                    source="test",
                    source_ref=f"managed-derivative-{number}",
                    raw_content=f"Independent managed document {number}",
                    content_type="text",
                )
            )
            knowledge = storage.store_knowledge_object(
                KnowledgeObject(
                    id=f"ko-managed-derivative-{number}",
                    user_id="alice",
                    raw_object_id=raw.id,
                    content=f"Independent managed knowledge {number}",
                    content_type="text",
                )
            )
            storage.store_inbox_item(
                InboxItem(
                    id=f"inbox-managed-derivative-{number}",
                    user_id="alice",
                    raw_object_id=raw.id,
                    knowledge_object_id=knowledge.id,
                )
            )
    elapsed = time.perf_counter() - started
    mutation_delta = storage.conn.total_changes - changes_before

    counts = {
        str(row[0]): int(row[1])
        for row in storage.execute(
            """SELECT material_kind, COUNT(*)
                 FROM private_entity_material_derivative_cache
                GROUP BY material_kind"""
        ).fetchall()
    }
    assert counts == {"inbox": 100, "knowledge": 100, "raw": 100}
    # A global rebuild after every row rewrites the growing allowlist and produces
    # O(N²) changes. Per-ID publication stays below this structural ceiling; the
    # loose time bound catches an accidental text cross-product under CI load.
    assert mutation_delta < 12_000
    assert elapsed < 10.0


def test_numbered_reminder_names_do_not_quarantine_the_next_number(
    storage: FridayStorage,
) -> None:
    """The eleventh reminder used to fail because ``дело 1`` matched ``дело 10``."""

    storage.ensure_user("alice")
    storage.ensure_user("bob")
    created_ids: list[str] = []
    for number in range(12):
        entity = storage.create_entity(
            Entity(
                id=f"ent-numbered-reminder-{number}",
                user_id="alice",
                name=f"дело {number}",
                entity_type=EntityType.EVENT,
            )
        )
        created_ids.append(entity.id)
        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id,person_id,privacy_kind,created_at)
                   VALUES(?, 'bob', 'reminder', ?)""",
                (entity.id, utc_now()),
            )

    cached = {
        str(row[0])
        for row in storage.execute("SELECT entity_id FROM private_entity_material_cache").fetchall()
    }
    assert set(created_ids) <= cached


def test_legacy_snapshot_dependency_match_uses_identity_boundaries_but_keeps_ids() -> None:
    from friday.storage._core import _snapshot_private_token_matches

    entity_id = "ent-private-snapshot-token-source"
    alias = unicodedata.normalize("NFD", "ТАЙНА ЁЛКА")
    owners = {
        entity_id: {entity_id},
        "дело 1": {entity_id},
        alias: {entity_id},
    }
    assert _snapshot_private_token_matches({"title": "дело 10"}, owners, {entity_id}) == set()
    assert _snapshot_private_token_matches({"title": "тайна ёлка"}, owners, {entity_id}) == {entity_id}
    # Entity ids are opaque references, not human names, and intentionally keep
    # conservative substring semantics inside packed legacy fields.
    assert _snapshot_private_token_matches(
        {"reference": f"prefix:{entity_id}:suffix"}, owners, {entity_id}
    ) == {entity_id}


def test_person_cache_predicate_shows_only_exact_uncontaminated_own_reminder(
    storage: FridayStorage,
) -> None:
    from friday.storage._privacy import _not_disallowed_private_material_for_person

    storage.ensure_user("alice")
    storage.ensure_user("bob")
    storage.ensure_user("shared")
    bob_private = storage.create_entity(
        Entity(
            id="ent-person-cache-bob-private",
            user_id="bob",
            name="BOB PRIVATE MATERIAL 71C9",
            entity_type=EntityType.EVENT,
        )
    )
    own = storage.create_entity(
        Entity(
            id="ent-person-cache-own",
            user_id="shared",
            name="Alice own reminder",
            entity_type=EntityType.EVENT,
        )
    )
    contaminated = storage.create_entity(
        Entity(
            id="ent-person-cache-contaminated",
            user_id="shared",
            name="Alice contaminated reminder",
            description=bob_private.name,
            entity_type=EntityType.EVENT,
        )
    )
    with storage.transaction() as conn:
        for entity, person in (
            (bob_private, "bob"),
            (own, "alice"),
            (contaminated, "alice"),
        ):
            conn.execute(
                """INSERT INTO entity_time(
                       entity_id,user_id,occurred_at,precision,source,updated_at)
                   VALUES(?, ?, '2026-08-07', 'day', ?, ?)""",
                (entity.id, entity.user_id, f"reminder:{person}", utc_now()),
            )
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id,person_id,privacy_kind,created_at)
                   VALUES(?, ?, 'reminder', ?)""",
                (entity.id, person, utc_now()),
            )

    predicate = _not_disallowed_private_material_for_person("e", "?")
    visible = {
        str(row[0])
        for row in storage.execute(
            f"""SELECT e.id FROM entities e
                 WHERE e.user_id IN ('alice','shared')
                   AND {predicate}""",  # nosec B608 - code-owned predicate
            ("alice",),
        ).fetchall()
    }
    assert own.id in visible
    assert contaminated.id not in visible


def test_runtime_authorizer_and_immutable_guards_preserve_hidden_identity(
    storage: FridayStorage,
) -> None:
    storage.ensure_user("alice")
    source = storage.create_entity(
        Entity(
            id="ent-authority-private-source",
            user_id="alice",
            name="PRIVATE AUTHORITY IMMUTABLE C61A",
            entity_type=EntityType.EVENT,
        )
    )
    carrier = storage.create_entity(
        Entity(
            id="ent-authority-private-carrier",
            user_id="alice",
            name="Authority carrier",
            description=source.name,
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(
                   entity_id,person_id,privacy_kind,created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (source.id, utc_now()),
        )

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("DELETE FROM private_entity_material_cache")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("DELETE FROM private_entity_material_work")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("UPDATE private_entity_material_cache_state SET valid=1")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("DELETE FROM private_entity_material_derivative_cache")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("DELETE FROM private_entity_material_derivative_work")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        storage.execute("UPDATE private_entity_material_derivative_state SET valid=1")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.execute(
            "UPDATE entities SET name='RENAMED' WHERE id=?",
            (source.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        storage.execute("DELETE FROM entities WHERE id=?", (source.id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        storage.execute(
            "UPDATE entity_versions SET snapshot_json='{}' WHERE entity_id=?",
            (source.id,),
        )
    assert storage.get_entity(source.id, "alice") is None
    assert storage.get_entity(carrier.id, "alice") is None


def test_invalid_cache_state_denies_all_generic_material_and_delivery(
    settings,
) -> None:
    storage = FridayStorage(settings)
    try:
        storage.ensure_user("alice")
        raw = RawObject(
            id="raw-cache-state-public",
            user_id="alice",
            source="test",
            source_ref="cache-state-public",
            raw_content="Unrelated public body",
            content_type="text",
            content_hash=hashlib.sha256(b"Unrelated public body").hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id="ko-cache-state-public",
            user_id="alice",
            raw_object_id=raw.id,
            content="Unrelated public knowledge",
            content_type="text",
            title="Public knowledge",
        )
        storage.store_knowledge_object(knowledge)
        inbox = InboxItem(
            id="inbox-cache-state-public",
            user_id="alice",
            raw_object_id=raw.id,
            knowledge_object_id=knowledge.id,
        )
        storage.store_inbox_item(inbox)
        assert storage.enqueue_notification("alice", "42", "Unrelated public notice")
        hidden = storage.create_entity(
            Entity(
                id="ent-cache-state-private-marker",
                user_id="alice",
                name="PRIVATE CACHE STATE MARKER D714",
                entity_type=EntityType.EVENT,
            )
        )
        with storage.transaction() as conn:
            conn.execute(
                """INSERT INTO private_entity_owners(
                       entity_id,person_id,privacy_kind,created_at)
                   VALUES(?, 'bob', 'reminder', ?)""",
                (hidden.id, utc_now()),
            )

        # Model an out-of-process write which has no connection-local authorizer.
        external = sqlite3.connect(Path(settings.database_path), timeout=10.0)
        try:
            external.execute("PRAGMA foreign_keys=ON")
            external.execute("DELETE FROM private_entity_material_cache")
            external.commit()
        finally:
            external.close()

        assert storage.execute("SELECT valid FROM private_entity_material_cache_state").fetchone()[0] == 0
        assert storage.get_raw_object(raw.id, "alice") is None
        assert storage.get_knowledge_object(knowledge.id, "alice") is None
        assert storage.get_inbox_item(inbox.id, "alice") is None
        assert storage.list_pending_notifications() == []
    finally:
        storage.close()


def test_persistent_privacy_schema_is_udf_free_and_offline_writes_fail_closed(
    settings,
    tmp_path: Path,
) -> None:
    database = tmp_path / "offline-privacy-schema.sqlite3"
    offline_settings = replace(settings, database_path=database)
    initial = FridayStorage(offline_settings)
    initial.ensure_user("alice")
    public = initial.create_entity(
        Entity(
            id="ent-offline-privacy-public",
            user_id="alice",
            name="Offline public before",
            entity_type=EntityType.EVENT,
        )
    )
    initial.close()

    raw = sqlite3.connect(database)
    try:
        udf_schema = raw.execute(
            """SELECT type, name FROM sqlite_master
                 WHERE sql IS NOT NULL AND instr(lower(sql), 'jericho_')>0"""
        ).fetchall()
        assert udf_schema == []
        # SQLite reparses every persistent view/trigger for ALTER.  This is the
        # exact offline path which UDF-backed persistent views used to brick.
        raw.execute("ALTER TABLE api_tokens ADD COLUMN offline_probe TEXT")
        raw.execute(
            "UPDATE entities SET name='Offline public after' WHERE id=?",
            (public.id,),
        )
        raw.commit()
        state = raw.execute("SELECT valid, prior_valid FROM private_entity_material_cache_state").fetchone()
        assert state == (0, 1)
    finally:
        raw.close()

    reopened = FridayStorage(offline_settings)
    try:
        assert reopened.execute("SELECT valid FROM private_entity_material_cache_state").fetchone()[0] == 1
        entity = reopened.get_entity(public.id, "alice")
        assert entity is not None and entity["name"] == "Offline public after"
    finally:
        reopened.close()


def test_current_schema_legacy_reminder_migrates_before_privacy_guards(
    settings,
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-reminder-current-schema.sqlite3"
    migrated_settings = replace(settings, database_path=database)
    initial = FridayStorage(migrated_settings)
    initial.ensure_user("shared")
    initial.ensure_user("bob")
    reminder = initial.create_entity(
        Entity(
            id="ent-current-schema-legacy-reminder",
            user_id="shared",
            name="Legacy reminder migration",
            entity_type=EntityType.EVENT,
        )
    )
    with initial.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id,user_id,occurred_at,precision,source,updated_at)
               VALUES(?, 'shared', '2026-08-05', 'day', 'reminder:bob', ?)""",
            (reminder.id, utc_now()),
        )
        conn.execute("DELETE FROM private_entity_owners WHERE entity_id=?", (reminder.id,))
    initial.close()

    reopened = FridayStorage(migrated_settings)
    try:
        for table, id_column in (
            ("entities", "id"),
            ("entity_versions", "entity_id"),
            ("entity_time", "entity_id"),
        ):
            owner = reopened.execute(
                f"SELECT user_id FROM {table} WHERE {id_column}=?",  # nosec B608 - fixed cases
                (reminder.id,),
            ).fetchone()
            assert owner is not None and owner[0] == "bob"
        marker = reopened.execute(
            "SELECT person_id FROM private_entity_owners WHERE entity_id=?",
            (reminder.id,),
        ).fetchone()
        assert marker is not None and marker[0] == "bob"
        assert reopened.execute("SELECT valid FROM private_entity_material_cache_state").fetchone()[0] == 1
    finally:
        reopened.close()
