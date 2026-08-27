from __future__ import annotations

import json
from pathlib import Path

from friday.account_deletion import (
    _mark_account_deletion_history_clean,
    delete_account,
    preflight_account_deletion,
)
from friday.interaction_control_plane.engineer_work_item_schema import ENGINEER_WORK_ITEM_SCHEMA

OWNER = "local:engineer-fence-owner"
ACTOR = "local:engineer-fence-admin"
IDEMPOTENCY_KEY = "ecmd-" + "a" * 64
WORK_ITEM_ID = "ewi_" + "b" * 32
SOURCE_BINDING = "c" * 64
COMMAND_DIGEST = "d" * 64
RETIRED_AT = "2026-08-27T10:00:00+00:00"


def _insert_retired_fence(storage) -> None:
    # A retired fence legitimately has no live Work Item.  Bypass only the
    # admission-time trigger to build that post-retirement state, then restore the
    # canonical DDL before exercising either public lifecycle.
    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER trg_engineer_work_item_command_fence_insert_guard")
        conn.execute(
            """INSERT INTO engineer_work_item_command_fences(
                   owner_id,idempotency_key,work_item_id,expected_revision,step_ordinal,
                   source_binding_sha256,command_digest,retired_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                OWNER,
                IDEMPOTENCY_KEY,
                WORK_ITEM_ID,
                1,
                1,
                SOURCE_BINDING,
                COMMAND_DIGEST,
                RETIRED_AT,
            ),
        )
    storage.conn.executescript(ENGINEER_WORK_ITEM_SCHEMA)


def test_retired_command_fence_exports_and_owner_cascades_with_exact_accounting(storage) -> None:
    storage.ensure_user(ACTOR, preset_key="admin")
    storage.ensure_user(OWNER)
    _insert_retired_fence(storage)

    exported = storage.export_user(OWNER)
    export_path = Path(str(exported["path"]))
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["engineer_work_item_command_fences"] == [
        {
            "owner_id": OWNER,
            "idempotency_key": IDEMPOTENCY_KEY,
            "work_item_id": WORK_ITEM_ID,
            "expected_revision": 1,
            "step_ordinal": 1,
            "source_binding_sha256": SOURCE_BINDING,
            "command_digest": COMMAND_DIGEST,
            "retired_at": RETIRED_AT,
        }
    ]
    # The export itself is deliberately an external-artifact blocker; remove the
    # test artifact before proving the independent account-erasure lifecycle.
    export_path.unlink()

    assert _mark_account_deletion_history_clean(storage, OWNER)
    storage.update_user(OWNER, status="disabled")
    plan = preflight_account_deletion(storage, OWNER, quiescence_available=True)
    assert plan["ready"] is True, plan
    assert plan["counts"]["engineer_work_item_command_fences"] == 1
    assert "engineer_work_item_command_fences.owner_id" not in plan["unknown_scopes"]

    outcome = delete_account(
        storage,
        OWNER,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=ACTOR,
        quiescence_verified=True,
    )

    assert outcome["deleted"]["engineer_work_item_command_fences"] == 1
    assert outcome["deleted_rows"] == plan["planned_delete_rows"]
    assert storage.execute(
        "SELECT COUNT(*) FROM engineer_work_item_command_fences WHERE owner_id=?",
        (OWNER,),
    ).fetchone()[0] == 0
    assert storage.get_user(OWNER) is None
    assert storage.get_user(ACTOR) is not None

