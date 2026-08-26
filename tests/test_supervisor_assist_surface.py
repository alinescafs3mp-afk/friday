from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.file_evidence import stamp_current_turn_file_reference
from friday.orchestration.capability_binding import operational_capability_snapshot
from friday.orchestration.policy_kernel import PolicyAdmissionContext, admit_supervisor_proposal
from friday.orchestration.semantic_supervisor import build_supervisor_input
from friday.orchestration.supervisor_assist_ingress import SupervisorAssistIngressBindingV1
from friday.orchestration.supervisor_assist_surface import (
    CurrentFileWebAssistSurface,
    bind_assist_plan_to_surface,
    prepare_current_file_web_assist_surface,
)
from friday.orchestration.supervisor_contracts import (
    SUPERVISOR_PROPOSAL_SCHEMA,
    SupervisorProposal,
)
from friday.orchestration.supervisor_plan_authority import (
    PlanAuthorityScope,
    PlanSourceBinding,
    attest_plan_authority,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext


class _Carrier(dict[str, Any]):
    pass


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        semantic_supervisor_mode="assist",
        semantic_supervisor_tasks=("compare_current_file_with_current_web",),
        semantic_supervisor_max_steps=6,
        semantic_supervisor_max_review_rounds=0,
        semantic_supervisor_timeout_sec=12.0,
        secondary_llm_profile=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
    )


def _actor() -> ActorContext:
    return ActorContext("local:alice", "owner", "test")


def _message(query: str = "актуальные публичные правила 2026") -> str:
    return (
        "Сравни текущий файл с текущими данными в интернете.\n"
        f"Публичный веб-запрос: «{query}»"
    )


def _attachment() -> _Carrier:
    raw_id = "raw_1234567890abcdef"
    carrier = _Carrier(
        raw_object_id=raw_id,
        persisted=True,
        current_turn_only=True,
        mime_type="text/plain",
        transient_text="private body",
        extraction_success=True,
    )
    raw = {
        "id": raw_id,
        "source": "upload",
        "source_ref": "sha256:" + "1" * 64,
        "content_type": "text/plain",
        "received_at": "2026-08-26T00:00:00+00:00",
        "content_hash": "2" * 64,
        "raw_content": "private body",
        "metadata_json": "{}",
    }
    stamp_current_turn_file_reference(carrier, raw)
    return carrier


def _surface_kwargs(**overrides: Any) -> dict[str, Any]:
    actor = _actor()
    values: dict[str, Any] = {
        "user_id": actor.user_id,
        "message": _message(),
        "actor": actor,
        "conversation_id": "conv_1234567890abcdef",
        "attachments": [_attachment()],
        "enable_tools": True,
        "ingestion_result": {
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
            "category": "web_request",
            "reason": "explicit public web request",
        },
        "synthetic_document_notice": False,
        "replay_source_message_id": None,
        "mode": None,
        "explicit_mode_requested": False,
        "answer_with_voice": False,
        "reply_to": None,
        "quoted_attachment_reference": False,
        "reply_assistant_reference": False,
        "reply_assistant_message_id": None,
        "turn_policy": None,
        "pending_durable_admission": None,
        "ingress_binding": SupervisorAssistIngressBindingV1.from_claimed_request(
            source_ref="assist-surface:1",
            request_fingerprint_sha256="f" * 64,
        ),
        "conversation_is_dialogue": lambda person_id, conversation_id: (
            person_id == actor.own_id and conversation_id == "conv_1234567890abcdef"
        ),
    }
    values.update(overrides)
    return values


def _surface(**overrides: Any) -> CurrentFileWebAssistSurface:
    surface = prepare_current_file_web_assist_surface(
        _settings(),
        **_surface_kwargs(**overrides),
    )
    assert isinstance(surface, CurrentFileWebAssistSurface)
    return surface


def _plan(surface: CurrentFileWebAssistSurface, *, query: str) -> Any:
    supervisor_input = build_supervisor_input(surface.turn, _settings())
    proposal = SupervisorProposal.parse(
        {
            "schema": SUPERVISOR_PROPOSAL_SCHEMA,
            "manifest_id": supervisor_input.manifest.manifest_id,
            "budget_sha256": supervisor_input.budgets.canonical_sha256(),
            "task_class": "compare_current_file_with_current_web",
            "goal": "Compare the supplied file with current public evidence.",
            "continuation_decision": "new_task",
            "risk_hints": ["external_read", "multi_source"],
            "steps": [
                {
                    "step_id": "s1",
                    "kind": "capability",
                    "target_id": "file.current.read",
                    "purpose": "Read the current file.",
                    "depends_on": [],
                    "parallel_group": "evidence",
                    "input": {"attachment_ordinal": 1},
                    "expected_outcome": "complete_source_evidence",
                },
                {
                    "step_id": "s2",
                    "kind": "capability",
                    "target_id": "web.search.current",
                    "purpose": "Read current public evidence.",
                    "depends_on": [],
                    "parallel_group": "evidence",
                    "input": {"query_intent": query},
                    "expected_outcome": "verified_current_sources",
                },
                {
                    "step_id": "s3",
                    "kind": "model",
                    "target_id": "primary.synthesis",
                    "purpose": "Compare admitted evidence with citations.",
                    "depends_on": ["s1", "s2"],
                    "parallel_group": None,
                    "input": {},
                    "expected_outcome": "cited_comparison",
                },
            ],
            "completion_criteria": [
                "current_attachment_evidence_present",
                "current_public_evidence_has_coverage",
                "material_differences_source_bound",
            ],
            "review_mode": "none",
            "fallback": "primary_only",
        }
    )
    decision = admit_supervisor_proposal(
        proposal,
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
            authority_scope=PlanAuthorityScope.ASSIST_EXECUTION,
            source_bindings=(
                PlanSourceBinding.current_raw_object(
                    raw_object_id=surface.attachment.raw_object_id,
                    source_identity_sha256=surface.attachment.source_identity_sha256,
                    content_sha256=surface.attachment_content_sha256,
                ),
            ),
            turn_deadline_monotonic_ns=(
                time.monotonic_ns() + supervisor_input.budgets.turn_deadline_ms * 1_000_000
            ),
            authority_attestor=lambda boundary: attest_plan_authority(
                boundary,
                witness_sha256="9" * 64,
            ),
            capability_bindings=operational_capability_snapshot(),
        ),
    )
    assert decision.admitted and decision.plan is not None
    return decision.plan


def test_exact_surface_mints_only_process_owned_file_and_public_query_pins() -> None:
    surface = _surface()
    assert surface.conversation_id == "conv_1234567890abcdef"
    assert surface.attachment.raw_object_id == "raw_1234567890abcdef"
    assert surface.attachment_content_sha256 == "2" * 64
    assert surface.web_plan.query_sha256
    assert "private body" not in repr(surface)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enable_tools", False),
        ("synthetic_document_notice", True),
        ("replay_source_message_id", "msg_1234567890abcdef"),
        ("mode", "engineer"),
        ("explicit_mode_requested", True),
        ("answer_with_voice", True),
        ("reply_to", "msg_1234567890abcdef"),
        ("quoted_attachment_reference", True),
        ("reply_assistant_reference", True),
        ("reply_assistant_message_id", "msg_1234567890abcdef"),
        (
            "pending_durable_admission",
            PendingDurableTurnAdmission.owned(
                person_id="local:alice",
                conversation_id="conv_1234567890abcdef",
            ),
        ),
        ("ingress_binding", None),
    ],
)
def test_special_or_already_owned_surfaces_never_enter_assist(field: str, value: object) -> None:
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(**{field: value}),
        )
        is None
    )


def test_surface_rejects_unstamped_historical_or_non_dialogue_inputs() -> None:
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(attachments=[dict(_attachment())]),
        )
        is None
    )
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(conversation_is_dialogue=lambda *_args: False),
        )
        is None
    )
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(message="Сравни файл с вебом без явного публичного запроса"),
        )
        is None
    )


def test_surface_keeps_tenant_and_conversation_owner_exact() -> None:
    foreign_person = ActorContext(
        "local:tenant",
        "owner",
        "test",
        shared_tenant=True,
        person_id="local:alice",
    )
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(
                user_id=foreign_person.user_id,
                actor=foreign_person,
                conversation_is_dialogue=lambda *_args: True,
            ),
        )
        is None
    )

    archive_owner = ActorContext(
        "local:tenant",
        "owner",
        "test",
        shared_tenant=True,
        person_id="local:tenant",
    )
    assert isinstance(
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(
                user_id=archive_owner.user_id,
                actor=archive_owner,
                conversation_is_dialogue=lambda person_id, _conversation_id: (
                    person_id == "local:tenant"
                ),
            ),
        ),
        CurrentFileWebAssistSurface,
    )


def test_surface_accepts_only_exact_transient_web_ingestion_shape() -> None:
    assert _surface(ingestion_result=None)
    malformed = dict(_surface_kwargs()["ingestion_result"])
    malformed["capture"] = True
    assert (
        prepare_current_file_web_assist_surface(
            _settings(),
            **_surface_kwargs(ingestion_result=malformed),
        )
        is None
    )


def test_plan_binding_requires_exact_sealed_outbound_query() -> None:
    query = "актуальные публичные правила 2026"
    surface = _surface(message=_message(query))
    matching = _plan(surface, query=query)
    bindings = bind_assist_plan_to_surface(matching, surface)
    assert bindings is not None
    assert [item.graph_step_id for item in bindings] == [
        "read_current_file",
        "read_current_web",
        "primary_synthesis",
    ]
    private_plan = json.dumps(matching.payload(), sort_keys=True)
    assert surface.attachment.raw_object_id not in private_plan
    assert surface.attachment.source_identity_sha256 in private_plan
    assert surface.attachment_content_sha256 in private_plan
    assert matching.budget_sha256 == matching.budgets.canonical_sha256()
    assert matching.budgets.turn_deadline_ms == 12_000

    mismatched = _plan(surface, query="другие публичные правила 2026")
    assert bind_assist_plan_to_surface(mismatched, surface) is None
