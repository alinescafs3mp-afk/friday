from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday_package_broker.authentication import RESPONSE_DOMAIN
from friday_package_broker.client import (
    PackageBrokerClient,
    PackageBrokerUnavailable,
    PackageBrokerUnknownOutcome,
    _read_response,
)
from friday_package_broker.contracts import (
    BrokerWireResponse,
    PackagePostconditionState,
    PackageRef,
)
from friday_package_broker.store import BrokerStore
from tests.package_broker_fixtures import receipt as package_receipt
from tests.test_package_broker_daemon import Harness


def approval_proof(harness: Harness, planned: dict[str, Any], *, idempotency_key: str):
    return harness.approval_signer.issue(
        broker_id="test-broker",
        approval_receipt_id="approval-1",
        approval_payload_digest=harness.approval_payload_digest(planned["plan_id"]),
        plan_id=planned["plan_id"],
        plan_digest=planned["plan_digest"],
        actor_user_id="owner",
        actor_own_id="own-1",
        continuation_work_item_id="work-1",
        execution_idempotency_key=idempotency_key,
        issued_at=1_000,
        expires_at=1_100,
    )


class OneShotBroker:
    def __init__(
        self,
        path: Path,
        handler: Callable[[bytes], bytes | None],
    ) -> None:
        self.path = path
        self.handler = handler
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        assert self.ready.wait(2)

    def join(self) -> None:
        self.thread.join(2)
        assert not self.thread.is_alive()

    def _run(self) -> None:
        self.path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen(1)
            self.ready.set()
            channel, _address = listener.accept()
            with channel:
                request = b""
                while not request.endswith(b"\n"):
                    block = channel.recv(64 * 1024)
                    if not block:
                        return
                    request += block
                response = self.handler(request[:-1])
                if response is not None:
                    channel.sendall(response + b"\n")


def call_through(
    path: Path,
    handler: Callable[[bytes], bytes | None],
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    server = OneShotBroker(path, handler)
    server.start()
    try:
        return operation()
    finally:
        server.join()


def test_client_uses_hmac_requests_but_only_a_pinned_public_key_for_evidence(tmp_path) -> None:
    harness = Harness()
    socket_path = tmp_path / "broker.sock"
    client = PackageBrokerClient(
        socket_path=socket_path,
        broker_id="test-broker",
        request_key=b"K" * 32,
        pinned_public_key=harness.auth.public_key_bytes,
        clock=lambda: 1_000,
    )

    def handler(raw: bytes) -> bytes:
        return harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)

    try:
        planned = call_through(
            socket_path,
            handler,
            lambda: client.plan_install(
                requested=(PackageRef("nmap"),),
                original_task_ref="task-1",
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="plan-client-1",
            ),
        )
        executed = call_through(
            socket_path,
            handler,
            lambda: client.execute_install(
                plan_id=planned["plan_id"],
                approved_plan_digest=planned["plan_digest"],
                approval_receipt_id="approval-1",
                approval_proof=approval_proof(harness, planned, idempotency_key="execute-client-1"),
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="execute-client-1",
            ),
        )
        assert executed["status"] == "completed"
        assert executed["receipt"]["signature"]
        assert executed["transaction_id"] == executed["receipt"]["transaction_id"]
        with pytest.raises(ValueError, match="private"):
            client._authenticator.sign_bytes(b"forged", domain=b"receipt")
    finally:
        harness.close()


def test_effectful_disconnect_after_request_delivery_is_unknown_not_retried(tmp_path) -> None:
    harness = Harness()
    socket_path = tmp_path / "broker.sock"
    client = PackageBrokerClient(
        socket_path=socket_path,
        broker_id="test-broker",
        request_key=b"K" * 32,
        pinned_public_key=harness.auth.public_key_bytes,
        clock=lambda: 1_000,
    )

    def handler(raw: bytes) -> bytes:
        return harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)

    try:
        planned = call_through(
            socket_path,
            handler,
            lambda: client.plan_install(
                requested=(PackageRef("nmap"),),
                original_task_ref="task-1",
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="plan-client-1",
            ),
        )

        def execute_then_disconnect(raw: bytes) -> None:
            harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)
            return None

        server = OneShotBroker(socket_path, execute_then_disconnect)
        server.start()
        try:
            with pytest.raises(PackageBrokerUnknownOutcome) as raised:
                client.execute_install(
                    plan_id=planned["plan_id"],
                    approved_plan_digest=planned["plan_digest"],
                    approval_receipt_id="approval-1",
                    approval_proof=approval_proof(harness, planned, idempotency_key="execute-client-1"),
                    continuation_work_item_id="work-1",
                    actor_id="owner",
                    own_id="own-1",
                    idempotency_key="execute-client-1",
                )
            assert raised.value.plan_id == planned["plan_id"]
            assert harness.backend.execute_calls == 1
        finally:
            server.join()
    finally:
        harness.close()


def test_cancel_disconnect_after_request_delivery_is_unknown_and_reconcilable(tmp_path) -> None:
    harness = Harness()
    socket_path = tmp_path / "broker.sock"
    client = PackageBrokerClient(
        socket_path=socket_path,
        broker_id="test-broker",
        request_key=b"K" * 32,
        pinned_public_key=harness.auth.public_key_bytes,
        clock=lambda: 1_000,
    )

    def handler(raw: bytes) -> bytes:
        return harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)

    try:
        planned = call_through(
            socket_path,
            handler,
            lambda: client.plan_install(
                requested=(PackageRef("nmap"),),
                original_task_ref="task-1",
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="plan-client-cancel-1",
            ),
        )

        def cancel_then_disconnect(raw: bytes) -> None:
            harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)
            return None

        with pytest.raises(PackageBrokerUnknownOutcome) as raised:
            call_through(
                socket_path,
                cancel_then_disconnect,
                lambda: client.cancel_before_commit(
                    plan_id=planned["plan_id"],
                    continuation_work_item_id="work-1",
                    actor_id="owner",
                    own_id="own-1",
                    idempotency_key="cancel-client-1",
                ),
            )
        assert raised.value.plan_id == planned["plan_id"]
        assert harness.backend.execute_calls == 0

        reconciled = call_through(
            socket_path,
            handler,
            lambda: client.status(
                plan_id=planned["plan_id"],
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="status-client-cancel-1",
            ),
        )
        assert reconciled["status"] == "cancelled_before_commit"
    finally:
        harness.close()


def test_malformed_signed_execute_record_is_unknown_not_a_raw_type_error(tmp_path) -> None:
    harness = Harness()
    socket_path = tmp_path / "broker.sock"
    client = PackageBrokerClient(
        socket_path=socket_path,
        broker_id="test-broker",
        request_key=b"K" * 32,
        pinned_public_key=harness.auth.public_key_bytes,
        clock=lambda: 1_000,
    )

    def handler(raw: bytes) -> bytes:
        return harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)

    try:
        planned = call_through(
            socket_path,
            handler,
            lambda: client.plan_install(
                requested=(PackageRef("nmap"),),
                original_task_ref="task-1",
                continuation_work_item_id="work-1",
                actor_id="owner",
                own_id="own-1",
                idempotency_key="plan-client-1",
            ),
        )

        def execute_with_malformed_status(raw: bytes) -> bytes:
            valid = BrokerWireResponse.decode(harness.daemon.handle_request(raw, peer_uid=1000, now=1_000))
            result = valid.result
            result["status"] = []
            malformed = BrokerWireResponse.create(
                broker_id=valid.broker_id,
                build_id=valid.build_id,
                request_id=valid.request_id,
                server_time=valid.server_time,
                ok=True,
                result=result,
            )
            signature = harness.auth.sign_bytes(malformed.signing_bytes(), domain=RESPONSE_DOMAIN)
            return malformed.with_signature(signature).encode()

        with pytest.raises(PackageBrokerUnknownOutcome):
            call_through(
                socket_path,
                execute_with_malformed_status,
                lambda: client.execute_install(
                    plan_id=planned["plan_id"],
                    approved_plan_digest=planned["plan_digest"],
                    approval_receipt_id="approval-1",
                    approval_proof=approval_proof(harness, planned, idempotency_key="execute-client-1"),
                    continuation_work_item_id="work-1",
                    actor_id="owner",
                    own_id="own-1",
                    idempotency_key="execute-client-1",
                ),
            )
        assert harness.backend.execute_calls == 1
    finally:
        harness.close()


def test_reconciliation_client_rejects_forged_and_legacy_inner_evidence(tmp_path) -> None:
    database = tmp_path / "broker.sqlite3"
    first = Harness(store=BrokerStore(database))
    planned_response = first.request(
        "PlanInstall", first.plan_body(), idempotency_key="plan-client-reconcile-1"
    )
    assert isinstance(planned_response, dict)
    planned = planned_response["result"]
    record = first.store.get(planned["plan_id"], actor_user_id="owner", actor_own_id="own-1")
    first.store.claim_execution(
        record.plan.plan_id,
        actor_user_id="owner",
        actor_own_id="own-1",
        plan_digest=record.plan.digest,
        execution_idempotency_key="execute-client-reconcile-1",
        transaction_id="apttxn_0123456789abcdef0123456789abcdef",
        approval_receipt_id="approval-1",
        approval_proof_id="approvalproof_0123456789abcdef0123456789abcdef",
        approval_proof_digest="d" * 64,
        now=1_000,
    )
    first.close()

    harness = Harness(store=BrokerStore(database))
    socket_path = tmp_path / "broker.sock"
    client = PackageBrokerClient(
        socket_path=socket_path,
        broker_id="test-broker",
        request_key=b"K" * 32,
        pinned_public_key=harness.auth.public_key_bytes,
        clock=lambda: 1_000,
    )

    def handler(raw: bytes) -> bytes:
        return harness.daemon.handle_request(raw, peer_uid=1000, now=1_000)

    def reconcile() -> dict[str, Any]:
        return client.reconcile_after_restart(
            plan_id=planned["plan_id"],
            plan_digest=planned["plan_digest"],
            continuation_work_item_id="work-1",
            actor_id="owner",
            own_id="own-1",
            idempotency_key="reconcile-client-1",
        )

    try:
        accepted = call_through(socket_path, handler, reconcile)
        assert accepted["reconciliation"]["postcondition_state"] == PackagePostconditionState.DESIRED.value

        def forged_inner(raw: bytes) -> bytes:
            valid = BrokerWireResponse.decode(handler(raw))
            evidence = {**valid.result["reconciliation"], "signature": "0" * 128}
            response = BrokerWireResponse.create(
                broker_id=valid.broker_id,
                build_id=valid.build_id,
                request_id=valid.request_id,
                server_time=valid.server_time,
                ok=True,
                result={**valid.result, "reconciliation": evidence},
            )
            return response.with_signature(
                harness.auth.sign_bytes(response.signing_bytes(), domain=RESPONSE_DOMAIN)
            ).encode()

        with pytest.raises(PackageBrokerUnavailable):
            call_through(socket_path, forged_inner, reconcile)

        current = package_receipt(record.plan)
        manifest = next(item for item in current.evidence_refs if item.kind == "apt_dpkg_transaction")
        legacy = replace(
            current,
            schema_version=2,
            stdout_total_size_bytes=None,
            stderr_total_size_bytes=None,
            stdout_total_size_complete=False,
            stderr_total_size_complete=False,
            evidence_refs=(manifest,),
        )

        def legacy_inner(raw: bytes) -> bytes:
            valid = BrokerWireResponse.decode(handler(raw))
            response = BrokerWireResponse.create(
                broker_id=valid.broker_id,
                build_id=valid.build_id,
                request_id=valid.request_id,
                server_time=valid.server_time,
                ok=True,
                result={**valid.result, "reconciliation": legacy.to_payload()},
            )
            return response.with_signature(
                harness.auth.sign_bytes(response.signing_bytes(), domain=RESPONSE_DOMAIN)
            ).encode()

        with pytest.raises(PackageBrokerUnavailable):
            call_through(socket_path, legacy_inner, reconcile)
    finally:
        harness.close()


def test_response_reader_enforces_one_monotonic_operation_deadline() -> None:
    class SlowChannel:
        def settimeout(self, _value: float) -> None:
            return None

        def recv(self, _maximum: int) -> bytes:
            time.sleep(0.02)
            return b"x"

    with pytest.raises(TimeoutError):
        _read_response(SlowChannel(), deadline=time.monotonic() + 0.01)  # type: ignore[arg-type]
