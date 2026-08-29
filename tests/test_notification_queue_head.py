"""An undeliverable notification must leave the queue, not sit at its head.

`/api/notifications/pending` refuses to hand the bridge a chat that is no longer
in the allow-list — correct — but it used to do that by SKIPPING the row: status
stayed `pending`, `attempts` stayed 0, nothing marked it and nothing could ever
clean it up. The queue is drained `ORDER BY created_at ASC LIMIT 20`, so twenty
such rows occupy every slot forever, and no later notification — reminder, «в
этот день», weekly digest, sentinel alert — is delivered again. Silently: no
error, no log, no alert, and the endpoint keeps answering `200 {"count": 0}`.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace

from fastapi.testclient import TestClient

from friday.security import sign_bridge_request
from friday.server import create_app


def _pending(client, settings, *, limit: int = 20) -> dict:
    path = f"/api/notifications/pending?limit={limit}"
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    signer = "5001"
    response = client.get(
        path,
        headers={
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": signer,
            "X-Friday-Chat": signer,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="GET",
                path=path,
                external_user_id=signer,
                chat_id=signer,
                nonce=nonce,
                body=b"",
            ),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_de_allowlisted_chat_does_not_block_the_queue(settings):
    tuned = replace(settings, telegram_allowed_chat_ids=[5001])
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        storage.ensure_user("alice")

        # Twenty rows for a chat that has since been removed from the allow-list,
        # queued BEFORE the one that matters — exactly the shape of a revoked chat
        # whose backlog was still queued.
        for index in range(20):
            assert storage.enqueue_notification(
                "alice", "999999", f"старое {index}", kind="reflection", dedup_key=f"old:{index}"
            )
        assert storage.enqueue_notification(
            "alice", "5001", "напоминание", kind="reflection", dedup_key="live:1"
        )

        first = _pending(client, tuned)
        # The first drain is allowed to return nothing — the window was full of
        # dead rows — but it must have retired them.
        second = _pending(client, tuned)
        assert second["count"] == 1, (
            f"the live notification is still unreachable (first drain: {first['count']})"
        )
        assert second["items"][0]["body"] == "напоминание"

        rows = {row["id"]: row for row in storage.execute("SELECT * FROM outbound_notifications")}
        dead = [row for row in rows.values() if row["chat_id"] == "999999"]
        assert dead and all(row["status"] == "failed" for row in dead)
        # Attempts are NOT spent on a row that was never handed to the bridge:
        # this is not a delivery failure, it is a row that cannot be delivered.
        assert all(int(row["attempts"]) == 0 for row in dead)
        # And the dedup key is released, so the organ may raise the matter again
        # if the chat is ever allowed back.
        assert all(row["dedup_key"] == "" for row in dead)


def test_an_allowed_chat_is_untouched(settings):
    """The retirement must not touch rows the bridge is about to deliver."""
    tuned = replace(settings, telegram_allowed_chat_ids=[5001])
    with TestClient(create_app(tuned)) as client:
        storage = client.app.state.storage
        storage.ensure_user("alice")
        assert storage.enqueue_notification("alice", "5001", "живое", dedup_key="live:2")

        assert _pending(client, tuned)["count"] == 1
        row = storage.execute("SELECT * FROM outbound_notifications").fetchone()
        assert row["status"] == "pending"
        assert row["dedup_key"] == "live:2"
        # Draining twice must keep returning it: it is still undelivered.
        assert _pending(client, tuned)["count"] == 1
