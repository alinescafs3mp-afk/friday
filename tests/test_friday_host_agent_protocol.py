from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.host_control.contracts import (
    PROTOCOL_VERSION,
    ContractError,
    canonical_json_bytes,
)
from friday.host_control.contracts import (
    WireRequest as BackendWireRequest,
)
from friday.host_control.contracts import (
    body_sha256 as backend_body_sha256,
)
from friday_host_agent.authentication import HMACAuthenticator, ReplayGuard
from friday_host_agent.protocol import ProtocolError, WireRequest, body_sha256, canonical_json

_KEY = b"k" * 32
_DIGEST = "a" * 64


def _request(*, now: int = 1_000, sequence: int = 1, body: dict | None = None):
    payload = {"probe": "данные", "count": 3} if body is None else body
    auth = HMACAuthenticator(_KEY, agent_id="host-agent:one")
    envelope = auth.create_envelope(
        request_id=f"request:{sequence}",
        sequence=sequence,
        issued_at=now,
        expires_at=now + 30,
        method="Health",
        job_id=f"job:{sequence}",
        actor_id="actor:one",
        own_id="owner:one",
        idempotency_key=f"idempotency:{sequence}",
        plan_digest=_DIGEST,
        body=payload,
    )
    return auth, BackendWireRequest.create(envelope, payload)


def test_agent_and_backend_use_one_canonical_golden_vector() -> None:
    body = {"z": [1, True, None], "unicode": "Привет", "nested": {"a": "b"}}
    expected = '{"nested":{"a":"b"},"unicode":"Привет","z":[1,true,null]}'.encode()
    assert canonical_json(body) == canonical_json_bytes(body) == expected
    assert body_sha256(body) == backend_body_sha256(body)
    _, request = _request(body=body)
    assert WireRequest.decode(request.encode()) == request


def test_hmac_rejects_body_tamper_identity_expiry_and_unknown_version() -> None:
    auth, request = _request()
    auth.verify(request.envelope, request.body, now=1_001)

    with pytest.raises(ProtocolError, match="body"):
        auth.verify(request.envelope, {"probe": "changed"}, now=1_001)
    with pytest.raises(ProtocolError, match="different host agent"):
        other = HMACAuthenticator(_KEY, agent_id="host-agent:other")
        other.verify(request.envelope, request.body, now=1_001)
    with pytest.raises(ProtocolError, match="expired"):
        auth.verify(request.envelope, request.body, now=1_030)
    with pytest.raises(ContractError, match="unsupported"):
        replace(request.envelope, protocol_version="2.0")


def test_hmac_rejects_signature_and_signed_body_hash_tampering() -> None:
    auth, request = _request()
    forged_signature = replace(request.envelope, signature="0" * 64)
    with pytest.raises(ProtocolError, match="signature"):
        auth.verify(forged_signature, request.body, now=1_001)

    changed_hash = replace(request.envelope, body_sha256="b" * 64)
    with pytest.raises(ProtocolError, match="body"):
        auth.verify(changed_hash, request.body, now=1_001)


def test_replay_guard_is_durable_for_request_and_sequence(tmp_path) -> None:
    _auth, first = _request(sequence=7)
    database = tmp_path / "replay.sqlite3"
    guard = ReplayGuard(database)
    guard.admit(first.envelope, now=1_001)
    guard.close()

    reopened = ReplayGuard(database)
    with pytest.raises(ProtocolError, match="already admitted"):
        reopened.admit(first.envelope, now=1_001)
    _auth, same_sequence = _request(sequence=7)
    same_sequence = BackendWireRequest.create(
        replace(same_sequence.envelope, request_id="request:different"), same_sequence.body
    )
    with pytest.raises(ProtocolError, match="already admitted"):
        reopened.admit(same_sequence.envelope, now=1_001)
    reopened.close()


def test_wire_decoder_rejects_non_object_and_oversized_body() -> None:
    with pytest.raises(ContractError):
        WireRequest.decode(json.dumps({"body": [], "envelope": {}}).encode())
    with pytest.raises(ContractError):
        body_sha256({"value": "x" * (512 * 1024)})


def test_request_protocol_version_is_shared() -> None:
    _auth, request = _request()
    assert request.envelope.protocol_version == PROTOCOL_VERSION
