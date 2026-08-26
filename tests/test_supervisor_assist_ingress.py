"""Closed body-free ingress identity for pending supervisor assist graphs."""

from __future__ import annotations

import hashlib

import pytest

from friday.orchestration.supervisor_assist_ingress import (
    SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA,
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding(label: str) -> SupervisorAssistIngressBindingV1:
    return SupervisorAssistIngressBindingV1.from_claimed_request(
        source_ref=f"api-request:{label}",
        request_fingerprint_sha256=_sha256(f"request:{label}"),
    )


def _pending() -> PendingDurableTurnAdmission:
    return PendingDurableTurnAdmission.owned(
        person_id="local:assist-owner",
        conversation_id="conv_0123456789abcdef",
        work_graph_id="graph_fedcba9876543210",
        revision=7,
    )


def test_claimed_request_binding_is_deterministic_body_free_and_closed() -> None:
    binding = _binding("root-private-text")

    assert binding.payload() == {
        "schema": SUPERVISOR_ASSIST_INGRESS_BINDING_SCHEMA,
        "source_ref_sha256": _sha256("api-request:root-private-text"),
        "request_fingerprint_sha256": _sha256("request:root-private-text"),
    }
    assert binding.canonical_sha256() == _binding("root-private-text").canonical_sha256()
    projected = repr(binding.payload())
    assert "api-request:root-private-text" not in projected
    assert "root-private-text" not in projected

    with pytest.raises(ValueError, match="source reference"):
        SupervisorAssistIngressBindingV1.from_claimed_request(
            source_ref=" request-key ",
            request_fingerprint_sha256=_sha256("request"),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        SupervisorAssistIngressBindingV1.from_claimed_request(
            source_ref="request-key",
            request_fingerprint_sha256="A" * 64,
        )


@pytest.mark.parametrize(
    ("relation", "current_label", "suppresses_ingestion", "permits_legacy"),
    (
        (SupervisorAssistPendingRelation.ROOT_REPLAY, "root", True, False),
        (SupervisorAssistPendingRelation.NEW_TURN, "new", False, True),
        (SupervisorAssistPendingRelation.EXPLICIT_CANCEL, "cancel", True, False),
    ),
)
def test_active_graph_decision_is_closed_and_exactly_scoped(
    relation: SupervisorAssistPendingRelation,
    current_label: str,
    suppresses_ingestion: bool,
    permits_legacy: bool,
) -> None:
    pending = _pending()
    root = _binding("root")
    decision = SupervisorAssistPendingDecision.for_graph(
        relation=relation,
        pending=pending,
        root_request_binding_sha256=root.canonical_sha256(),
        current=_binding(current_label),
    )

    assert decision.pending is pending
    assert decision.person_id == pending.person_id
    assert decision.conversation_id == pending.conversation_id
    assert decision.suppresses_ingestion is suppresses_ingestion
    assert decision.permits_legacy is permits_legacy
    assert decision.matches_message("cancel" if current_label == "cancel" else "обычный ход")
    assert not decision.matches_message("обычный ход" if current_label == "cancel" else "cancel")
    assert "graph_fedcba9876543210" not in repr(decision)


def test_uncertain_decision_suppresses_both_ingestion_and_legacy() -> None:
    decision = SupervisorAssistPendingDecision.uncertain(
        person_id="local:assist-owner",
        conversation_id="conv_0123456789abcdef",
        current=_binding("untrusted-current"),
    )

    assert decision.relation is SupervisorAssistPendingRelation.UNCERTAIN
    assert decision.pending is None
    assert decision.root_request_binding_sha256 is None
    assert decision.suppresses_ingestion is True
    assert decision.permits_legacy is False
    assert decision.matches_message("любой непустой ход")


def test_relation_claims_fail_closed_on_binding_or_scope_mismatch() -> None:
    pending = _pending()
    root = _binding("root").canonical_sha256()

    with pytest.raises(ValueError, match="root replay"):
        SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.ROOT_REPLAY,
            pending=pending,
            root_request_binding_sha256=root,
            current=_binding("new"),
        )
    with pytest.raises(ValueError, match="new turn"):
        SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.NEW_TURN,
            pending=pending,
            root_request_binding_sha256=root,
            current=_binding("root"),
        )
    with pytest.raises(ValueError, match="cancellation"):
        SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.EXPLICIT_CANCEL,
            pending=pending,
            root_request_binding_sha256=root,
            current=_binding("root"),
        )
    with pytest.raises(ValueError, match="graph binding"):
        SupervisorAssistPendingDecision(
            relation=SupervisorAssistPendingRelation.EXPLICIT_CANCEL,
            person_id="local:foreign-owner",
            conversation_id=pending.conversation_id,
            pending=pending,
            root_request_binding_sha256=root,
            current_request_binding_sha256=_binding("cancel").canonical_sha256(),
        )
    with pytest.raises(ValueError, match="graph binding"):
        SupervisorAssistPendingDecision.for_graph(
            relation=SupervisorAssistPendingRelation.ROOT_REPLAY,
            pending=PendingDurableTurnAdmission.owned(
                person_id=pending.person_id,
                conversation_id=pending.conversation_id,
                work_item_id="work_0123456789abcdef",
                revision=1,
            ),
            root_request_binding_sha256=root,
            current=_binding("root"),
        )
