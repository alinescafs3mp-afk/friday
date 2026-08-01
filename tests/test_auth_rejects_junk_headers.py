"""A header full of junk is a failed authentication, not a server error.

`hmac.compare_digest` raises TypeError when a str argument holds a character above
U+00FF, and an HTTP header value may: both parsers uvicorn can use accept obs-text
bytes (0x80-0xFF), which decode into exactly that range. The TypeError escaped the
middleware's handlers, so the caller got a 500 with a traceback where a 401 belonged
— and because the request never reached the failure path, the attempt was written to
no audit log and counted against no failed-authentication budget. Twenty such
requests produced twenty 500s and zero audit rows; twenty ASCII ones produced 401s
and then a 429.

Neither value could ever have been correct — a signature and a token are ASCII — so
this was never a way to guess faster. It was a blind spot in the log and a client
that could not be rate-limited.
"""

from __future__ import annotations

import hmac
import time

import pytest

from friday.security import sign_bridge_request, verify_bridge_request

SECRET = "s" * 48
FIELDS = {
    "method": "POST",
    "path": "/api/telegram/message",
    "external_user_id": "42",
    "chat_id": "42",
    "nonce": "a" * 32,
    "body": b"{}",
}

JUNK = [
    "\x80" * 64,  # obs-text: the shape that produced the 500
    "ÿ" * 64,
    "é" * 64,
    "€" * 64,
    "деадбиф" * 8,
    "not-hex-at-all",
    "",
]


@pytest.mark.parametrize("signature", JUNK)
def test_junk_signatures_are_rejected_not_crashed(signature):
    now = int(time.time())
    with pytest.raises(ValueError):
        verify_bridge_request(
            SECRET,
            timestamp=str(now),
            signature=signature,
            max_age_sec=90,
            now=now,
            **FIELDS,
        )


def test_a_real_signature_still_verifies():
    now = int(time.time())
    signature = sign_bridge_request(SECRET, timestamp=now, **FIELDS)
    identity = verify_bridge_request(
        SECRET, timestamp=str(now), signature=signature, max_age_sec=90, now=now, **FIELDS
    )
    assert identity.external_user_id == "42"
    # Case-insensitivity of the hex digest is deliberate and must survive the fix.
    upper = verify_bridge_request(
        SECRET, timestamp=str(now), signature=signature.upper(), max_age_sec=90, now=now, **FIELDS
    )
    assert upper.chat_id == "42"


@pytest.mark.parametrize("token", JUNK)
def test_a_junk_bearer_token_compares_without_raising(token):
    """The same comparison guards the API token path in `server.py`."""
    configured = "t" * 48
    assert hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8")) is False
