from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from friday.host_control.network_approval import (
    NETWORK_APPROVAL_DOMAIN,
    NetworkApprovalError,
    NetworkApprovalLedger,
    NetworkApprovalSigner,
    NetworkApprovalVerifier,
    assert_network_approval_binding,
)


def _proof(*, signer: NetworkApprovalSigner | None = None, **overrides):  # noqa: ANN003, ANN202
    selected = signer or NetworkApprovalSigner(b"N" * 32)
    claims = {
        "host_agent_id": "host-agent:test",
        "approval_receipt_id": "approval:network:exact",
        "approval_payload_digest": "a" * 64,
        "plan_id": "plan:network:exact",
        "plan_digest": "b" * 64,
        "job_id": "hjob_0123456789abcdef0123456789abcdef",
        "execution_idempotency_key": "idempotency:network:exact",
        "actor_user_id": "actor:test",
        "actor_own_id": "owner:test",
        "issued_at": 1_000,
        "expires_at": 1_120,
    }
    claims.update(overrides)
    return selected.issue(**claims)


def _assert_binding(proof) -> None:  # noqa: ANN001
    assert_network_approval_binding(
        proof,
        host_agent_id="host-agent:test",
        approval_receipt_id="approval:network:exact",
        approval_payload_digest="a" * 64,
        plan_id="plan:network:exact",
        plan_digest="b" * 64,
        plan_created_at=900,
        plan_expires_at=1_200,
        job_id="hjob_0123456789abcdef0123456789abcdef",
        execution_idempotency_key="idempotency:network:exact",
        actor_user_id="actor:test",
        actor_own_id="owner:test",
    )


def test_network_approval_signature_uses_a_separate_domain_and_exact_times() -> None:
    signer = NetworkApprovalSigner(b"N" * 32)
    verifier = NetworkApprovalVerifier(signer.public_key_bytes)
    proof = _proof(signer=signer)

    verifier.verify(proof, now=1_001)
    _assert_binding(proof)
    assert NETWORK_APPROVAL_DOMAIN == b"friday-public-network-action-approval-v1"
    assert len(proof.signature) == 128
    assert len(proof.digest) == 64

    with pytest.raises(NetworkApprovalError, match="network_approval_expired"):
        verifier.verify(proof, now=1_120)
    with pytest.raises(NetworkApprovalError, match="network_approval_from_future"):
        verifier.verify(proof, now=900)
    with pytest.raises(NetworkApprovalError, match="network_approval_signature_invalid"):
        NetworkApprovalVerifier(NetworkApprovalSigner(b"F" * 32).public_key_bytes).verify(
            proof,
            now=1_001,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"host_agent_id": "host-agent:other"},
        {"approval_receipt_id": "approval:network:other"},
        {"approval_payload_digest": "c" * 64},
        {"plan_id": "plan:network:other"},
        {"plan_digest": "c" * 64},
        {"job_id": "hjob_fedcba9876543210fedcba9876543210"},
        {"execution_idempotency_key": "idempotency:network:other"},
        {"actor_user_id": "actor:other"},
        {"actor_own_id": "owner:other"},
        {"issued_at": 800, "expires_at": 920},
        {"expires_at": 1_300},
    ],
)
def test_network_approval_binding_rejects_every_changed_claim(overrides: dict) -> None:
    proof = _proof(**overrides)
    with pytest.raises(NetworkApprovalError, match="network_approval_binding_mismatch"):
        _assert_binding(proof)


def test_network_approval_ledger_is_full_sync_immutable_and_restart_safe(tmp_path: Path) -> None:
    database = tmp_path / "network-approvals.sqlite3"
    proof = _proof()
    ledger = NetworkApprovalLedger(database)
    assert ledger._connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # noqa: SLF001
    assert ledger.claim(proof, now=1_001) is True
    assert ledger.claim(proof, now=1_002) is False
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        ledger._connection.execute(  # noqa: SLF001
            "UPDATE consumed_network_approvals SET expires_at=? WHERE proof_id=?",
            (proof.expires_at + 1, proof.proof_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        ledger._connection.execute(  # noqa: SLF001
            "DELETE FROM consumed_network_approvals WHERE proof_id=?",
            (proof.proof_id,),
        )
    ledger.close()

    restarted = NetworkApprovalLedger(database)
    try:
        assert restarted.claim(proof, now=1_003) is False
        with pytest.raises(NetworkApprovalError, match="network_approval_replayed"):
            restarted.claim(_proof(), now=1_004)
        restarted.assert_claimed(proof)
    finally:
        restarted.close()
