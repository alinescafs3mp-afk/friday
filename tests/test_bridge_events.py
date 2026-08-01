"""The bridge's own failures, in the journal the rest of the system already uses.

`runtime_events` records worker transitions and backups, but the bridge could not
contribute: it is a separate process with its own SQLite, reaching the backend over
signed HTTP. So the events most worth having were the ones missing. A tunnel outage on
this machine produced 295 consecutive long-polling failures across three days, and
diagnosing it meant grepping a log file by hand.

The write path is deliberately the narrowest thing that works: an allowlisted set of
event types, a bounded payload, bridge authentication. The journal is capped, so an
unconstrained writer could evict every real event simply by flooding it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from friday.api.events import BRIDGE_EVENT_TYPES, MAX_PAYLOAD_KEYS, MAX_VALUE_CHARS
from friday.server import create_app


def _signed(settings, body, *, user: str = "5001"):
    """Sign exactly as the bridge does; the endpoint accepts nothing else."""
    import json as _json
    import time
    import uuid

    from friday.security import sign_bridge_request

    payload = _json.dumps(body).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": user,
        "X-Friday-Chat": user,
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method="POST",
            path="/api/events",
            external_user_id=user,
            chat_id=user,
            nonce=nonce,
            body=payload,
        ),
    }
    return payload, headers


def test_a_bridge_event_lands_in_the_journal(settings, storage):
    with TestClient(create_app(settings)) as client:
        payload, headers = _signed(
            settings, {"event_type": "bridge.poll_failed", "payload": {"loop": "poll"}}
        )
        response = client.post("/api/events", content=payload, headers=headers)

    assert response.status_code == 200, response.text
    events = storage.list_events(event_type="bridge.poll_failed")
    assert len(events) == 1 and events[0]["payload"]["loop"] == "poll"


def test_an_unknown_event_type_is_refused(settings, storage):
    """The vocabulary is fixed: the journal is bounded and shared with the backend."""
    with TestClient(create_app(settings)) as client:
        payload, headers = _signed(settings, {"event_type": "bridge.anything_i_like"})
        response = client.post("/api/events", content=payload, headers=headers)

    assert response.status_code == 400
    assert storage.count_events() == 0


def test_the_endpoint_requires_bridge_authentication(settings, storage):
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/events", json={"event_type": "bridge.poll_failed"})

    assert response.status_code in {401, 403}
    assert storage.count_events() == 0


def test_the_payload_is_bounded_on_both_axes(settings, storage):
    """The bridge handles untrusted input from Telegram; what it forwards is untrusted too."""
    with TestClient(create_app(settings)) as client:
        payload, headers = _signed(
            settings,
            {
                "event_type": "bridge.dead_letter",
                "payload": {f"k{index}": "x" * 5000 for index in range(50)},
            },
        )
        response = client.post("/api/events", content=payload, headers=headers)

    assert response.status_code == 200
    stored = storage.list_events()[0]["payload"]
    assert len(stored) <= MAX_PAYLOAD_KEYS
    assert all(len(str(value)) <= MAX_VALUE_CHARS for value in stored.values())


@pytest.mark.parametrize("event_type", sorted(BRIDGE_EVENT_TYPES))
def test_every_allowed_type_is_accepted(settings, storage, event_type):
    with TestClient(create_app(settings)) as client:
        payload, headers = _signed(settings, {"event_type": event_type})
        assert client.post("/api/events", content=payload, headers=headers).status_code == 200


# --- the bridge side: transitions, not ticks ------------------------------


class _Bridge:
    """Only the parts of the bridge `_journal_transition` touches."""

    from friday.telegram_bridge import TelegramBridge

    _journal_transition = TelegramBridge._journal_transition
    # Real method, not a stub: the signer choice is exactly what this file's
    # "nothing is posted without a chat to sign as" test is about.
    _signer_chat_id = TelegramBridge._signer_chat_id

    def __init__(self, allowed=(5001,)):
        self.posted: list[dict] = []
        self._loop_failing: dict[str, bool] = {}

        class _Config:
            allowed_chat_ids = list(allowed)

        self.config = _Config()

    async def _backend_json(self, _backend, _method, _path, body, _user, _chat):
        self.posted.append(body)
        return {}


def _run(bridge, sequence):
    async def main():
        for failing in sequence:
            await bridge._journal_transition(None, "poll", failing=failing)

    asyncio.run(main())


def test_a_flapping_loop_reports_twice_not_two_hundred_times():
    """The measured case: 295 consecutive failures is one event, not 295."""
    bridge = _Bridge()
    _run(bridge, [False] + [True] * 295 + [False] * 50)

    assert [item["event_type"] for item in bridge.posted] == [
        "bridge.poll_failed",
        "bridge.poll_recovered",
    ]


def test_the_first_success_after_start_is_not_a_recovery():
    bridge = _Bridge()
    _run(bridge, [False, False])
    assert bridge.posted == []


def test_the_error_type_travels_but_never_the_message():
    """An httpx error carries the full URL, and a Telegram URL carries the bot token."""
    bridge = _Bridge()

    async def main():
        await bridge._journal_transition(None, "poll", failing=False)
        await bridge._journal_transition(
            None,
            "poll",
            failing=True,
            error=RuntimeError("https://api.telegram.org/bot123456:SECRET/getUpdates failed"),
        )

    asyncio.run(main())

    payload = bridge.posted[0]["payload"]
    assert payload["error_type"] == "RuntimeError"
    assert "SECRET" not in str(bridge.posted[0])


def test_journalling_never_breaks_the_loop_it_watches():
    bridge = _Bridge()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("backend unreachable")

    bridge._backend_json = explode

    async def main():
        await bridge._journal_transition(None, "poll", failing=False)
        await bridge._journal_transition(None, "poll", failing=True)

    asyncio.run(main())  # must not raise


def test_nothing_is_posted_without_an_allowlisted_chat_to_sign_as():
    bridge = _Bridge(allowed=())
    _run(bridge, [False, True])
    assert bridge.posted == []
