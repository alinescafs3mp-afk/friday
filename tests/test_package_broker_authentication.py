from __future__ import annotations

import pytest

from friday_package_broker.authentication import (
    REQUEST_DOMAIN,
    RESPONSE_DOMAIN,
    BrokerAuthenticationError,
    BrokerAuthenticator,
    ReplayLedger,
)
from friday_package_broker.daemon import EMPTY_PLAN_DIGEST


def envelope(authenticator: BrokerAuthenticator, *, ttl: int = 60):
    return authenticator.create_envelope(
        request_id="request-1",
        sequence=1,
        issued_at=1_000,
        expires_at=1_000 + ttl,
        method="Health",
        job_id="work-1",
        actor_id="owner",
        own_id="own-1",
        idempotency_key="health-1",
        plan_digest=EMPTY_PLAN_DIGEST,
        body={},
    )


def test_request_authentication_is_expiring_and_domain_separated() -> None:
    authenticator = BrokerAuthenticator(b"K" * 32, broker_id="test-broker", signing_private_key=b"S" * 32)
    signed = envelope(authenticator)
    authenticator.verify(signed, {}, now=1_001)

    with pytest.raises(BrokerAuthenticationError, match="request_expired"):
        authenticator.verify(signed, {}, now=1_060)
    assert authenticator.sign_bytes(b"same", domain=REQUEST_DOMAIN) != authenticator.sign_bytes(
        b"same", domain=RESPONSE_DOMAIN
    )


def test_replay_admission_survives_a_restart(tmp_path) -> None:
    authenticator = BrokerAuthenticator(b"K" * 32, broker_id="test-broker", signing_private_key=b"S" * 32)
    signed = envelope(authenticator)
    database = tmp_path / "replay.sqlite3"
    first = ReplayLedger(database)
    first.admit(signed, now=1_001)
    first.close()

    recovered = ReplayLedger(database)
    try:
        with pytest.raises(BrokerAuthenticationError, match="replayed_request"):
            recovered.admit(signed, now=1_002)
    finally:
        recovered.close()


def test_replay_memory_mode_requires_explicit_test_opt_in() -> None:
    with pytest.raises(ValueError, match="explicit"):
        ReplayLedger(":memory:")


def test_request_lifetime_cannot_exceed_broker_limit() -> None:
    authenticator = BrokerAuthenticator(b"K" * 32, broker_id="test-broker", signing_private_key=b"S" * 32)
    signed = envelope(authenticator, ttl=301)

    with pytest.raises(BrokerAuthenticationError, match="invalid_expiry"):
        authenticator.verify(signed, {}, now=1_001)
