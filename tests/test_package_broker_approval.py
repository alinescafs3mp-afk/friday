from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace

import pytest

from friday_package_broker.approval import (
    ApprovalProofError,
    PackageApprovalSigner,
    PackageApprovalVerifier,
)


def _proof(signer: PackageApprovalSigner):  # noqa: ANN202
    return signer.issue(
        broker_id="test-broker",
        approval_receipt_id="apr_0123456789abcdef",
        approval_payload_digest="a" * 64,
        plan_id="aptplan_0123456789abcdef0123456789abcdef",
        plan_digest="b" * 64,
        actor_user_id="owner",
        actor_own_id="own-1",
        continuation_work_item_id="hjob_0123456789abcdef0123456789abcdef",
        execution_idempotency_key="install:0123456789abcdef",
        issued_at=1_000,
        expires_at=1_120,
    )


def test_backend_proof_signature_is_domain_owned_and_tamper_evident() -> None:
    signer = PackageApprovalSigner(b"A" * 32)
    verifier = PackageApprovalVerifier(signer.public_key_bytes)
    proof = _proof(signer)
    verifier.verify(proof, now=1_001)

    with pytest.raises(ApprovalProofError, match="approval_signature_invalid"):
        verifier.verify(replace(proof, plan_digest="c" * 64), now=1_001)
    with pytest.raises(ApprovalProofError, match="approval_expired"):
        verifier.verify(proof, now=proof.expires_at)


def test_private_seed_loader_turns_dumpability_off_before_read_and_closes_fd(tmp_path) -> None:
    key = tmp_path / "backend-approval-signing.key"
    key.write_bytes(b"A" * 32)
    key.chmod(0o600)
    script = """
import ctypes
import os
import pathlib
import sys
from friday_package_broker.approval import load_backend_approval_signing_key

path = pathlib.Path(sys.argv[1])
assert load_backend_approval_signing_key(path) == b"A" * 32
libc = ctypes.CDLL(None)
assert libc.prctl(3, 0, 0, 0, 0) == 0
for entry in pathlib.Path("/proc/self/fd").iterdir():
    try:
        assert pathlib.Path(os.readlink(entry)) != path
    except FileNotFoundError:
        pass
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(key)],
        check=False,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.getcwd()},
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
