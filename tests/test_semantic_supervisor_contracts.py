from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    manifest_binding_snapshot_sha256,
    operational_capability_snapshot,
)
from friday.orchestration.capability_manifest import bounded_capability_manifest
from friday.orchestration.contracts import TurnInput
from friday.orchestration.execution_plan import ValidatedExecutionPlan
from friday.orchestration.policy_kernel import (
    PolicyAdmissionContext,
    PolicyReason,
    admit_supervisor_proposal,
    risk_hints_cannot_downgrade_effect,
)
from friday.orchestration.semantic_supervisor import (
    SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES,
    build_supervisor_input,
    build_supervisor_messages,
    build_supervisor_request,
    classify_supervisor_task,
    supervisor_eligibility,
    supervisor_mode_from_settings,
)
from friday.orchestration.supervisor_contracts import (
    ARCHIVE_SEARCH_ID,
    FILE_CURRENT_READ_ID,
    HOST_SCAN_LOCAL_ID,
    PRIMARY_SYNTHESIS_ID,
    SUPERVISOR_PROPOSAL_SCHEMA,
    WEB_SEARCH_CURRENT_ID,
    CapabilityAvailability,
    SupervisorContractError,
    SupervisorMode,
    SupervisorProposal,
    TaskClass,
    canonical_sha256,
)
from friday.orchestration.supervisor_observation import SupervisorSkipReason
from friday.orchestration.transient_web_comparison import (
    TRANSIENT_WEB_ADAPTER_ID,
    TRANSIENT_WEB_SECURITY_ID,
)
from friday.secondary_brain.gpt_oss import GptOssProtocolAdapter


def _actor() -> SimpleNamespace:
    return SimpleNamespace(is_owner=True, shared_tenant=False)


def _turn(
    message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    conversation_id: str | None = "conv-1",
    enable_tools: bool = True,
) -> TurnInput:
    return TurnInput.from_chat(
        message=message,
        actor=_actor(),
        conversation_id=conversation_id,
        attachments=attachments,
        enable_tools=enable_tools,
        synthetic_document_notice=False,
        mode="dialogue",
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )


def _compare_turn() -> TurnInput:
    return _turn(
        "Сравни этот договор с текущими публичными правилами в интернете.",
        attachments=[{"mime_type": "text/plain", "text": "clause one"}],
    )


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "semantic_supervisor_mode": "shadow",
        "semantic_supervisor_tasks": ("compare_current_file_with_current_web",),
        "semantic_supervisor_max_steps": 6,
        "semantic_supervisor_max_review_rounds": 1,
        "semantic_supervisor_timeout_sec": 12.0,
        "secondary_llm_profile": "profile-test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _compare_steps() -> list[dict[str, Any]]:
    return [
        {
            "step_id": "s1",
            "kind": "capability",
            "target_id": FILE_CURRENT_READ_ID,
            "purpose": "Read the current attachment.",
            "depends_on": [],
            "parallel_group": "evidence",
            "input": {"attachment_ordinal": 1},
            "expected_outcome": "complete_source_evidence",
        },
        {
            "step_id": "s2",
            "kind": "capability",
            "target_id": WEB_SEARCH_CURRENT_ID,
            "purpose": "Find current public rules.",
            "depends_on": [],
            "parallel_group": "evidence",
            "input": {"query_intent": "current public rules for the supplied document"},
            "expected_outcome": "verified_current_sources",
        },
        {
            "step_id": "s3",
            "kind": "model",
            "target_id": PRIMARY_SYNTHESIS_ID,
            "purpose": "Compare admitted evidence.",
            "depends_on": ["s1", "s2"],
            "parallel_group": None,
            "input": {},
            "expected_outcome": "cited_comparison",
        },
    ]


def _proposal_payload(supervisor_input: Any, **overrides: Any) -> dict[str, Any]:
    payload = {
        "schema": SUPERVISOR_PROPOSAL_SCHEMA,
        "manifest_id": supervisor_input.manifest.manifest_id,
        "task_class": "compare_current_file_with_current_web",
        "goal": "Compare the supplied document with current public rules.",
        "continuation_decision": "new_task",
        "risk_hints": ["external_read", "multi_source"],
        "steps": _compare_steps(),
        "completion_criteria": [
            "current_attachment_evidence_present",
            "current_public_evidence_has_coverage",
            "material_differences_source_bound",
        ],
        "review_mode": "none",
        "fallback": "primary_only",
    }
    payload.update(overrides)
    return payload


def test_unknown_supervisor_mode_fails_closed_to_off() -> None:
    assert SupervisorMode.fail_closed("typo-assist") is SupervisorMode.OFF
    assert (
        supervisor_mode_from_settings(SimpleNamespace(semantic_supervisor_mode="nope")) is SupervisorMode.OFF
    )


def test_capability_manifest_is_bounded_and_digest_stable() -> None:
    turn = _compare_turn()
    manifest = bounded_capability_manifest(turn)
    again = bounded_capability_manifest(turn)
    assert manifest.manifest_id == again.manifest_id
    assert manifest.manifest_id.startswith("sha256:")
    ids = {item.id for item in manifest.capabilities}
    assert FILE_CURRENT_READ_ID in ids
    assert WEB_SEARCH_CURRENT_ID in ids
    assert HOST_SCAN_LOCAL_ID not in ids
    assert all(item.effect_class.value == "read" for item in manifest.capabilities)


def test_manifest_binds_real_permission_and_adapter_registry_without_exposing_it() -> None:
    turn = _compare_turn()
    snapshot = operational_capability_snapshot()
    manifest = bounded_capability_manifest(turn, binding_snapshot=snapshot)
    file_binding = snapshot.binding_for(FILE_CURRENT_READ_ID)
    archive_binding = snapshot.binding_for(ARCHIVE_SEARCH_ID)
    web_binding = snapshot.binding_for(WEB_SEARCH_CURRENT_ID)

    assert file_binding is not None and file_binding.available is True
    assert file_binding.security_id == "files.read"
    assert file_binding.tool_id == "file_read"
    assert file_binding.adapter_id == "friday.orchestration.file_read.V12FileReadHandler"
    assert archive_binding is not None and archive_binding.available is True
    assert archive_binding.security_id == "search.use"
    assert archive_binding.tool_id == "archive_search"
    assert archive_binding.adapter_id == "friday.execution_kernel.ExecutionKernel._archive_search"
    assert web_binding is not None and web_binding.available is True
    assert web_binding.security_id == TRANSIENT_WEB_SECURITY_ID
    assert web_binding.adapter_id == TRANSIENT_WEB_ADAPTER_ID
    assert manifest_binding_snapshot_sha256(manifest) == snapshot.digest_hex()

    public = json.dumps(manifest.payload(), sort_keys=True)
    assert "files.read" not in public
    assert TRANSIENT_WEB_SECURITY_ID not in public
    assert TRANSIENT_WEB_ADAPTER_ID not in public


def test_manifest_availability_requires_registered_permission_and_adapter() -> None:
    turn = _compare_turn()
    snapshot = operational_capability_snapshot()
    web_binding = snapshot.binding_for(WEB_SEARCH_CURRENT_ID)
    assert web_binding is not None
    assert turn.enable_tools is True
    for registration_field in ("permission_registered", "adapter_registered"):
        unavailable = replace(
            snapshot,
            bindings=tuple(
                replace(item, **{registration_field: False})
                if item.supervisor_capability_id == WEB_SEARCH_CURRENT_ID
                else item
                for item in snapshot.bindings
            ),
        )

        manifest = bounded_capability_manifest(turn, binding_snapshot=unavailable)
        web = manifest.capability_by_id()[WEB_SEARCH_CURRENT_ID]

        assert web.availability is CapabilityAvailability.UNAVAILABLE


def test_supervisor_input_is_secret_free_and_closed() -> None:
    turn = _turn(
        "Сравни документ с публичными правилами в интернете.",
        attachments=[{"mime_type": "text/plain", "name": "/private/secret.docx", "text": "ok"}],
    )
    supervisor_input = build_supervisor_input(turn, _settings())
    serialized = json.dumps(supervisor_input.payload(), ensure_ascii=False)
    assert "/private/" not in serialized
    assert "secret.docx" not in serialized
    assert "attachment-1" not in serialized or supervisor_input.turn.attachments[0].ordinal == 1
    rebuilt = type(supervisor_input).parse(supervisor_input.payload())
    assert rebuilt.canonical_sha256() == supervisor_input.canonical_sha256()


def test_proposal_rejects_extra_keys_duplicate_keys_and_surrounding_prose() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    payload = _proposal_payload(supervisor_input)
    with pytest.raises(SupervisorContractError, match="extra"):
        SupervisorProposal.parse({**payload, "execute_now": True})
    with pytest.raises(SupervisorContractError, match="without surrounding text"):
        SupervisorProposal.parse("```json\n" + json.dumps(payload) + "\n```")
    with pytest.raises(SupervisorContractError, match="duplicate key"):
        SupervisorProposal.parse(
            json.dumps(payload).replace(
                '"task_class": "compare_current_file_with_current_web"',
                '"task_class": "compare_current_file_with_current_web", "task_class": "unknown"',
            )
        )
    with pytest.raises(SupervisorContractError, match="invalid number"):
        SupervisorProposal.parse(json.dumps(payload).replace('"new_task"', "Infinity"))


def test_proposal_rejects_malformed_utf8_oversized_fields_and_too_many_steps() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    payload = _proposal_payload(supervisor_input)

    with pytest.raises(SupervisorContractError, match="valid UTF-8"):
        SupervisorProposal.parse(json.dumps(payload) + "\ud800")
    with pytest.raises(SupervisorContractError, match="goal exceeds 240"):
        SupervisorProposal.parse({**payload, "goal": "g" * 241})

    oversized_purpose = _compare_steps()
    oversized_purpose[0]["purpose"] = "p" * 161
    with pytest.raises(SupervisorContractError, match="purpose exceeds 160"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=oversized_purpose))

    too_many = _compare_steps()
    too_many.extend(
        {
            **too_many[0],
            "step_id": f"s{ordinal}",
        }
        for ordinal in range(4, 8)
    )
    with pytest.raises(SupervisorContractError, match="steps must contain 1 to 6"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=too_many))


def test_proposal_rejects_duplicate_step_ids_and_unsupported_model_role() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    duplicate = _compare_steps()
    duplicate[1]["step_id"] = duplicate[0]["step_id"]
    with pytest.raises(SupervisorContractError, match="step IDs must be unique"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=duplicate))

    unsupported = _compare_steps()
    unsupported[2]["target_id"] = "primary.unsupported"
    with pytest.raises(SupervisorContractError, match="not in the closed input catalog"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=unsupported))


def test_proposal_rejects_cycles_unknown_ids_and_shell_smuggling() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    cyclic = _compare_steps()
    cyclic[0]["depends_on"] = ["s3"]
    with pytest.raises(SupervisorContractError, match="acyclic"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=cyclic))
    missing = _compare_steps()
    missing[2]["depends_on"] = ["s9"]
    with pytest.raises(SupervisorContractError, match="unknown step"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=missing))
    smuggled = _compare_steps()
    smuggled[1]["input"] = {"query_intent": "rules; $(cat /etc/passwd)"}
    with pytest.raises(SupervisorContractError, match="natural-language"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=smuggled))
    path_command = _compare_steps()
    path_command[1]["input"] = {"query_intent": "C:\\Windows\\System32\\cmd.exe"}
    with pytest.raises(SupervisorContractError, match="natural-language"):
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=path_command))


@pytest.mark.parametrize(
    ("field", "carrier"),
    (
        ("goal", "$(cat /etc/passwd)"),
        ("goal", "Read /home/alice/private.docx before comparing."),
        ("goal", "Use $HOME/private.docx as the source."),
        ("purpose", "run `rm -rf /tmp/private`"),
        ("purpose", r"Read C:\Users\alice\private.docx"),
        ("purpose", "Set FRIDAY_TOKEN=private before reading."),
    ),
)
def test_proposal_rejects_shell_path_and_environment_carriers_in_control_text(
    field: str,
    carrier: str,
) -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    payload = _proposal_payload(supervisor_input)
    if field == "goal":
        payload["goal"] = carrier
    else:
        payload["steps"][0]["purpose"] = carrier

    with pytest.raises(SupervisorContractError, match="closed advisory text"):
        SupervisorProposal.parse(payload)


def test_proposal_control_text_allows_public_urls_and_natural_slashes() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    payload = _proposal_payload(
        supervisor_input,
        goal="Compare law/regulation guidance at https://example.com/public/rules.",
    )
    payload["steps"][1]["purpose"] = "Compare A/B public guidance."

    proposal = SupervisorProposal.parse(payload)

    assert proposal.goal.endswith("/public/rules.")
    assert proposal.steps[1].purpose == "Compare A/B public guidance."


@pytest.mark.parametrize(
    ("field", "carrier"),
    (
        ("goal", "Read /home/alice/private.docx."),
        ("purpose", "run `rm -rf /tmp/private`"),
    ),
)
def test_policy_kernel_rechecks_control_text_on_parser_bypassed_typed_objects(
    field: str,
    carrier: str,
) -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    proposal = SupervisorProposal.parse(_proposal_payload(supervisor_input))
    if field == "goal":
        bypassed = replace(proposal, goal=carrier)
    else:
        bypassed_step = replace(proposal.steps[0], purpose=carrier)
        bypassed = replace(proposal, steps=(bypassed_step, *proposal.steps[1:]))

    decision = admit_supervisor_proposal(
        bypassed,
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )

    assert decision.admitted is False
    assert decision.reason is PolicyReason.CONTROL_TEXT_NOT_ADMITTED
    assert decision.plan is None


def test_proposal_cannot_construct_validated_execution_plan() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    proposal = SupervisorProposal.parse(_proposal_payload(supervisor_input))
    with pytest.raises(Exception, match="cannot be parsed"):
        ValidatedExecutionPlan.parse(proposal.payload())
    with pytest.raises(Exception, match="cannot be constructed"):
        ValidatedExecutionPlan(
            proposal_digest=proposal.canonical_sha256(),
            manifest_digest=supervisor_input.manifest.digest_hex(),
            binding_snapshot_sha256="f" * 64,
            policy_version="x",
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
            effect_classes=(),
            confirmation_required=False,
            confirmation_present=False,
            fallback_owner="primary_only",
            publication_owner="primary",
            steps=(),
            _seal=object(),
        )


def test_policy_kernel_admits_compare_and_rejects_stale_unknown_and_effects() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    context = PolicyAdmissionContext(actor_binding_sha256="a" * 64, conversation_binding_sha256="b" * 64)
    admitted = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input)),
        supervisor_input,
        context,
    )
    assert admitted.admitted is True
    assert admitted.plan is not None
    assert admitted.plan.publication_owner == "primary"
    assert admitted.plan.fallback_owner == "primary_only"
    assert all(effect.value == "read" for effect in admitted.plan.effect_classes)
    assert admitted.plan.binding_snapshot_sha256 == context.capability_bindings.digest_hex()
    admitted_steps = {step.step_id: step for step in admitted.plan.steps}
    assert admitted_steps["s1"].resolved_security_id == "files.read"
    assert admitted_steps["s1"].resolved_adapter_id == ("friday.orchestration.file_read.V12FileReadHandler")
    assert admitted_steps["s2"].resolved_security_id == TRANSIENT_WEB_SECURITY_ID
    assert admitted_steps["s2"].resolved_adapter_id == TRANSIENT_WEB_ADAPTER_ID
    assert admitted_steps["s3"].resolved_security_id is None
    assert admitted_steps["s3"].resolved_adapter_id is None

    stale = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input, manifest_id="sha256:" + "c" * 64)),
        supervisor_input,
        context,
    )
    assert stale.reason is PolicyReason.STALE_MANIFEST

    injected = _compare_steps()
    injected[1]["target_id"] = HOST_SCAN_LOCAL_ID
    injected[1]["input"] = {}
    unknown = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=injected)),
        supervisor_input,
        context,
    )
    assert unknown.reason is PolicyReason.UNKNOWN_CAPABILITY

    self_review = _compare_steps()
    self_review[2]["target_id"] = "secondary.supervisor"
    rejected = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=self_review)),
        supervisor_input,
        context,
    )
    assert rejected.reason is PolicyReason.SELF_APPROVAL


def test_policy_kernel_rejects_private_binding_registry_drift() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    snapshot = operational_capability_snapshot()
    drifted = CapabilityBindingSnapshot(
        bindings=tuple(
            replace(
                item,
                adapter_id="transient_web_comparison.v2",
                adapter_identity_sha256="d" * 64,
            )
            if item.supervisor_capability_id == WEB_SEARCH_CURRENT_ID
            else item
            for item in snapshot.bindings
        )
    )
    decision = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input)),
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
            capability_bindings=drifted,
        ),
    )

    assert decision.admitted is False
    assert decision.reason is PolicyReason.REGISTRY_DRIFT
    assert decision.plan is None


def test_policy_kernel_rejects_manifest_reparsed_without_private_binding_witness() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    reparsed = type(supervisor_input).parse(supervisor_input.payload())
    proposal = SupervisorProposal.parse(_proposal_payload(reparsed))

    decision = admit_supervisor_proposal(
        proposal,
        reparsed,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )

    assert decision.reason is PolicyReason.REGISTRY_DRIFT


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        (
            lambda steps: steps.__setitem__(
                0,
                {**steps[0], "expected_outcome": "verified_current_sources"},
            ),
            PolicyReason.EXPECTED_OUTCOME_MISMATCH,
        ),
        (
            lambda steps: steps.__setitem__(
                2,
                {**steps[2], "depends_on": ["s1"]},
            ),
            PolicyReason.DEPENDENCY_SHAPE_MISMATCH,
        ),
    ),
)
def test_policy_kernel_recomputes_step_semantics(
    mutation: Any,
    expected_reason: PolicyReason,
) -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    steps = _compare_steps()
    mutation(steps)
    decision = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(supervisor_input, steps=steps)),
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )
    assert decision.reason is expected_reason


def test_policy_kernel_binds_proposal_task_to_code_owned_turn_class() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    steps = _compare_steps()
    steps[0] = {
        **steps[0],
        "target_id": ARCHIVE_SEARCH_ID,
        "input": {"query_intent": "archived document relevant to this comparison"},
        "expected_outcome": "archive_evidence",
    }
    decision = admit_supervisor_proposal(
        SupervisorProposal.parse(
            _proposal_payload(
                supervisor_input,
                task_class="compare_archive_with_current_web",
                steps=steps,
                completion_criteria=[
                    "archive_evidence_present",
                    "current_public_evidence_has_coverage",
                    "material_differences_source_bound",
                ],
            )
        ),
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )
    assert decision.reason is PolicyReason.TASK_CLASS_MISMATCH


def test_policy_kernel_requires_complete_capability_and_code_owned_completion_shape() -> None:
    partial_turn = _turn(
        "Сравни этот договор с текущими публичными правилами в интернете.",
        attachments=[{"mime_type": "application/pdf"}],
    )
    partial_input = build_supervisor_input(partial_turn, _settings())
    context = PolicyAdmissionContext(
        actor_binding_sha256="a" * 64,
        conversation_binding_sha256="b" * 64,
    )
    partial = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(partial_input)),
        partial_input,
        context,
    )
    assert partial.reason is PolicyReason.PARTIAL_CAPABILITY

    mixed_turn = _turn(
        "Сравни документы с текущими публичными правилами в интернете.",
        attachments=[
            {"mime_type": "application/pdf"},
            {"mime_type": "text/plain", "text": "readable"},
        ],
    )
    mixed_input = build_supervisor_input(mixed_turn, _settings())
    unreadable_ordinal = admit_supervisor_proposal(
        SupervisorProposal.parse(_proposal_payload(mixed_input)),
        mixed_input,
        context,
    )
    assert unreadable_ordinal.reason is PolicyReason.INPUT_NOT_IN_PROJECTION

    complete_input = build_supervisor_input(_compare_turn(), _settings())
    wrong_criteria = admit_supervisor_proposal(
        SupervisorProposal.parse(
            _proposal_payload(
                complete_input,
                completion_criteria=["current_attachment_evidence_present"],
            )
        ),
        complete_input,
        context,
    )
    assert wrong_criteria.reason is PolicyReason.COMPLETION_CRITERIA_MISMATCH

    premature_review = admit_supervisor_proposal(
        SupervisorProposal.parse(
            _proposal_payload(
                complete_input,
                review_mode="secondary_after_deterministic_checks",
            )
        ),
        complete_input,
        context,
    )
    assert premature_review.reason is PolicyReason.REVIEW_NOT_ADMITTED


def test_model_risk_hints_cannot_downgrade_code_owned_effects() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    proposal = SupervisorProposal.parse(_proposal_payload(supervisor_input, risk_hints=[]))
    projection = risk_hints_cannot_downgrade_effect(proposal, supervisor_input)
    assert projection["hints_are_advisory_only"] is True
    assert projection["code_owned_effects"] == ["read", "read"]


def test_routing_keeps_exact_lanes_and_small_talk_off_the_supervisor() -> None:
    settings = _settings()
    small = supervisor_eligibility(_turn("привет"), settings)
    assert small.eligible is False
    assert small.skip_reason is SupervisorSkipReason.SMALL_TALK

    dialogue = supervisor_eligibility(_turn("как дела, расскажи новость"), settings)
    assert dialogue.eligible is False
    assert dialogue.skip_reason is SupervisorSkipReason.ORDINARY_DIALOGUE

    file_only = supervisor_eligibility(
        _turn("кратко перескажи файл", attachments=[{"mime_type": "text/plain", "text": "body"}]),
        settings,
    )
    assert file_only.eligible is False
    assert file_only.skip_reason is SupervisorSkipReason.ESTABLISHED_FILE_READ

    cancel = supervisor_eligibility(_turn("отмена"), settings, pending_bound=True)
    assert cancel.skip_reason is SupervisorSkipReason.EXACT_LANE

    ordinal = supervisor_eligibility(_turn("2"), settings, pending_bound=True)
    assert ordinal.skip_reason is SupervisorSkipReason.EXACT_LANE

    compare = supervisor_eligibility(_compare_turn(), settings)
    assert compare.eligible is True
    assert compare.task_class is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    assert classify_supervisor_task(_compare_turn()) is TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB

    for unavailable in (
        [{"mime_type": "application/pdf"}],
        [
            {"mime_type": "text/plain", "text": "one"},
            {"mime_type": "text/plain", "text": "two"},
        ],
    ):
        closed = supervisor_eligibility(
            _turn(
                "Сравни документы с текущими публичными правилами в интернете.",
                attachments=unavailable,
            ),
            settings,
        )
        assert closed.eligible is False
        assert closed.skip_reason is SupervisorSkipReason.EVIDENCE_UNAVAILABLE


def test_archive_and_current_web_route_mints_exact_two_read_plan_shape() -> None:
    turn = _turn("Сравни переписку из архива с текущими данными в интернете.")
    settings = _settings(semantic_supervisor_tasks=("compare_archive_with_current_web",))

    assert classify_supervisor_task(turn) is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB
    eligibility = supervisor_eligibility(turn, settings)
    assert eligibility.eligible is True
    assert eligibility.task_class is TaskClass.COMPARE_ARCHIVE_WITH_CURRENT_WEB

    supervisor_input = build_supervisor_input(turn, settings)
    messages = build_supervisor_messages(supervisor_input)
    proposal = SupervisorProposal.parse(
        json.loads(messages[-1]["content"])["untrusted_payload"]["response_template"]
    )
    decision = admit_supervisor_proposal(
        proposal,
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )

    assert decision.admitted is True
    assert decision.plan is not None
    reads = decision.plan.steps[:2]
    assert tuple(step.capability_id for step in reads) == (ARCHIVE_SEARCH_ID, WEB_SEARCH_CURRENT_ID)
    assert all(step.parallel_group == "evidence" and not step.depends_on for step in reads)
    synthesis = decision.plan.steps[-1]
    assert synthesis.capability_id == PRIMARY_SYNTHESIS_ID
    assert synthesis.depends_on == tuple(step.step_id for step in reads)


def test_empty_task_allowlist_never_invokes_supervisor() -> None:
    eligibility = supervisor_eligibility(_compare_turn(), _settings(semantic_supervisor_tasks=()))
    assert eligibility.eligible is False
    assert eligibility.skip_reason is SupervisorSkipReason.TASK_NOT_ALLOWLISTED


def test_mixed_or_duplicate_task_allowlist_fails_closed() -> None:
    for tasks in (
        ("compare_current_file_with_current_web", "not-admitted"),
        (
            "compare_current_file_with_current_web",
            "compare_current_file_with_current_web",
        ),
    ):
        eligibility = supervisor_eligibility(
            _compare_turn(),
            _settings(semantic_supervisor_tasks=tasks),
        )
        assert eligibility.eligible is False
        assert eligibility.skip_reason is SupervisorSkipReason.TASK_NOT_ALLOWLISTED


def test_deeply_nested_contract_input_fails_closed() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    payload = _proposal_payload(supervisor_input)
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(20):
        child: dict[str, Any] = {}
        cursor["nested"] = child
        cursor = child
    payload["steps"][0]["input"] = nested
    with pytest.raises(SupervisorContractError, match="nesting depth"):
        SupervisorProposal.parse(payload)


def test_supervisor_messages_keep_untrusted_user_text_out_of_policy() -> None:
    turn = _compare_turn()
    supervisor_input = build_supervisor_input(turn, _settings())
    system, user = build_supervisor_messages(supervisor_input)
    assert "advisory only" in system["content"].casefold()
    payload = json.loads(user["content"])
    assert payload["trusted_policy"]["tools_allowed"] is False
    assert payload["trusted_policy"]["publication_allowed"] is False
    assert payload["untrusted_turn"]["message"] == supervisor_input.turn.message
    assert "FRIDAY_" not in user["content"]
    compact_manifest = payload["untrusted_payload"]["capability_manifest"]
    assert compact_manifest["manifest_id"] == supervisor_input.manifest.manifest_id
    assert all(
        set(item) == {"id", "class", "availability", "input_schema_id"}
        for item in compact_manifest["capabilities"]
    )
    template = payload["untrusted_payload"]["response_template"]
    proposal = SupervisorProposal.parse(template)
    admitted = admit_supervisor_proposal(
        proposal,
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )
    assert admitted.admitted is True
    request = build_supervisor_request(
        supervisor_input,
        absolute_deadline_monotonic=1.0,
    )
    schema = request.structured_output_schema
    assert schema is not None
    assert schema["properties"]["task_class"]["enum"] == ["compare_current_file_with_current_web"]
    assert schema["properties"]["steps"]["minItems"] == 3
    assert schema["properties"]["steps"]["maxItems"] == 3
    assert sum(len(item["content"].encode("utf-8")) for item in request.messages) < 3_100
    payload = GptOssProtocolAdapter().build_payload(
        SimpleNamespace(
            max_context_tokens=4096,
            max_output_tokens=512,
            served_model_alias="accepted-profile-alias",
        ),
        request,
    )
    assert payload["max_tokens"] == 512
    assert payload["response_format"]["type"] == "json_schema"


def test_assist_proposal_transport_and_kernel_bind_the_distinct_v2_policy() -> None:
    supervisor_input = build_supervisor_input(
        _compare_turn(),
        _settings(semantic_supervisor_mode="assist"),
    )
    assert supervisor_input.budgets.max_review_rounds == 1
    system, user = build_supervisor_messages(supervisor_input)
    assert "advisory only" in system["content"].casefold()
    payload = json.loads(user["content"])
    trusted = payload["trusted_policy"]
    assert trusted["policy_id"] == semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
    assert trusted["policy_sha256"] == (semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256)
    assert payload["untrusted_payload"]["constraints"]["max_review_rounds"] == 1
    assert trusted["tools_allowed"] is False
    assert trusted["effects_allowed"] is False
    assert trusted["publication_allowed"] is False

    proposal = SupervisorProposal.parse(payload["untrusted_payload"]["response_template"])
    decision = admit_supervisor_proposal(
        proposal,
        supervisor_input,
        PolicyAdmissionContext(
            actor_binding_sha256="a" * 64,
            conversation_binding_sha256="b" * 64,
        ),
    )
    assert decision.admitted is True
    assert decision.plan is not None
    assert decision.plan.policy_version == (semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID)
    assert decision.plan.confirmation_required is False
    assert decision.plan.publication_owner == "primary"


def test_assist_v2_transport_rejects_the_broader_shadow_archive_journey() -> None:
    with pytest.raises(SupervisorContractError, match="accepted product policy"):
        build_supervisor_input(
            _turn("Сравни переписку из архива с текущими данными в интернете."),
            _settings(
                semantic_supervisor_mode="assist",
                semantic_supervisor_tasks=("compare_archive_with_current_web",),
            ),
        )


@pytest.mark.parametrize(
    ("filler", "language_hint"),
    (("a", "en"), ("я", "ru")),
)
def test_supervisor_message_projection_exactly_fits_4k_adapter_budget(
    filler: str,
    language_hint: str,
) -> None:
    prefix = "compare current public web rules " if filler == "a" else "сравни публичные правила в интернете "
    source = prefix + filler * 1_200
    supervisor_input = build_supervisor_input(
        _turn(source, attachments=[{"mime_type": "text/plain", "text": "clause"}]),
        _settings(),
    )
    assert supervisor_input.turn.language_hint == language_hint
    assert supervisor_input.turn.message == source[: len(supervisor_input.turn.message)]
    assert len(supervisor_input.turn.message) < 1_200

    messages = build_supervisor_messages(supervisor_input)
    envelope_bytes = sum(len(item["content"].encode("utf-8")) for item in messages)
    assert envelope_bytes <= SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES
    request = build_supervisor_request(supervisor_input, absolute_deadline_monotonic=1.0)
    GptOssProtocolAdapter().build_payload(
        SimpleNamespace(
            max_context_tokens=4_096,
            max_output_tokens=512,
            served_model_alias="accepted-profile-alias",
        ),
        request,
    )

    next_character = source[len(supervisor_input.turn.message)]
    oversized = replace(
        supervisor_input,
        turn=replace(
            supervisor_input.turn,
            message=supervisor_input.turn.message + next_character,
        ),
    )
    oversized_bytes = sum(
        len(item["content"].encode("utf-8")) for item in build_supervisor_messages(oversized)
    )
    assert oversized_bytes > SUPERVISOR_ADAPTER_INPUT_BUDGET_BYTES
    with pytest.raises(SupervisorContractError, match="adapter input budget"):
        build_supervisor_request(oversized, absolute_deadline_monotonic=1.0)


@pytest.mark.parametrize(
    "private_suffix",
    (
        "/home/alice/private.docx",
        "Bearer " + "A" * 32,
        "https://example.com/?next=file:///home/alice/private.docx",
        "https://example.com/?next=/home/alice/private.docx",
        "https://example.com/?next=%2Fhome%2Falice%2Fprivate.docx",
        "https://example.com?next=%2Fhome%2Falice%2Fprivate.docx",
        "https://example.com#next=%2Fhome%2Falice%2Fprivate.docx",
        "https://example.com/?next=%2525252Fhome%2525252Falice%2525252Fprivate.docx",
        "https://example.com/?next=%ZZ",
        "https://exa%ZZmple.com/",
        "https://example.com/file:///home/alice/private.docx",
        "https://example.com/%66ile%3A%2F%2F%2Fhome/alice/private.docx",
        "https://file:%2F%2F%2Fhome@example.com/public",
        "https://example.com/C:%5CUsers%5Calice%5Csecret.txt",
        "https://example.com/%5C%5Cserver%5Cshare%5Csecret.txt",
        "https://example.com/redirect/..%2F..%2Fhome%2Falice%2Fsecret.txt",
        "https://example.com/?token=%73%6B%2D" + "%41" * 20,
        "https://example.com?token=%73%6B%2D" + "%41" * 20,
        "https://example.com/?authorization=Bearer+" + "A" * 32,
        "https://example.com/?authorization=Bearer%2B" + "A" * 32,
        "https://example.com/?id=%72%61%77%5F" + "%61" * 16,
    ),
)
def test_byte_projection_does_not_hide_a_private_suffix(private_suffix: str) -> None:
    source = "Сравни документ с публичными правилами в интернете. " + "я" * 1_200 + " " + private_suffix
    supervisor_input = build_supervisor_input(
        _turn(source, attachments=[{"mime_type": "text/plain", "text": "clause"}]),
        _settings(),
    )
    with pytest.raises(SupervisorContractError, match="private|secret material"):
        build_supervisor_request(supervisor_input, absolute_deadline_monotonic=1.0)


def test_canonical_digest_is_order_independent() -> None:
    payload = {"b": 1, "a": True}
    assert canonical_sha256(payload) == canonical_sha256({"a": True, "b": 1})


def test_configuration_defaults_and_unknown_mode_stay_off(settings, monkeypatch) -> None:
    assert settings.semantic_supervisor_mode == "off"
    assert settings.semantic_supervisor_tasks == ()
    public = settings.public_dict()["semantic_supervisor"]
    assert public["promotion_admitted"] is False
    assert public["promotion_config"] == {
        "operator_gate_enabled": False,
        "raw_settings_valid": True,
        "evidence_file_configured": False,
        "evidence_sha256_configured": False,
        "latency_budget_file_configured": False,
        "latency_budget_sha256_configured": False,
        "source_revision_configured": False,
        "registry_binding_configured": False,
        "canary_actor_binding_count": 0,
        "evidence_path_public": False,
        "latency_budget_path_public": False,
    }
    raw = settings.semantic_supervisor_promotion_activation_settings()
    assert raw.enabled is False
    assert raw.requested_mode == "off"
    assert raw.evidence_file == ""
    assert raw.latency_budget_file == ""
    assert raw.canary_actor_bindings == ()
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_MODE", "please-assist")
    from friday.config import load_settings

    loaded = load_settings()
    assert loaded.semantic_supervisor_mode == "off"


def test_promotion_config_retains_exact_private_bindings_but_redacts_them(
    settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_path = "/private/friday/promotion-evidence.json"
    evidence_sha256 = "a" * 64
    latency_budget_path = "/private/friday/latency-budget.json"
    latency_budget_sha256 = "f" * 64
    source_sha256 = "b" * 64
    registry_sha256 = "c" * 64
    actors = ("d" * 64, "e" * 64)
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_MODE", "canary")
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED", "1")
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE", evidence_path)
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256", evidence_sha256)
    monkeypatch.setenv(
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE",
        latency_budget_path,
    )
    monkeypatch.setenv(
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256",
        latency_budget_sha256,
    )
    monkeypatch.setenv(
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256",
        source_sha256,
    )
    monkeypatch.setenv(
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256",
        registry_sha256,
    )
    monkeypatch.setenv(
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS",
        ",".join(actors),
    )
    from friday.config import load_settings

    loaded = load_settings()
    raw = loaded.semantic_supervisor_promotion_activation_settings()
    assert raw.enabled is True
    assert raw.requested_mode == "canary"
    assert raw.evidence_file == evidence_path
    assert raw.evidence_sha256 == evidence_sha256
    assert raw.latency_budget_file == latency_budget_path
    assert raw.latency_budget_sha256 == latency_budget_sha256
    assert raw.source_revision_sha256 == source_sha256
    assert raw.registry_binding_sha256 == registry_sha256
    assert raw.canary_actor_bindings == actors
    semantic_public = loaded.public_dict()["semantic_supervisor"]
    assert isinstance(semantic_public, dict)
    public = semantic_public["promotion_config"]
    assert isinstance(public, dict)
    assert public == {
        "operator_gate_enabled": True,
        "raw_settings_valid": True,
        "evidence_file_configured": True,
        "evidence_sha256_configured": True,
        "latency_budget_file_configured": True,
        "latency_budget_sha256_configured": True,
        "source_revision_configured": True,
        "registry_binding_configured": True,
        "canary_actor_binding_count": 2,
        "evidence_path_public": False,
        "latency_budget_path_public": False,
    }
    serialized = json.dumps(public, sort_keys=True)
    for private_value in (
        evidence_path,
        evidence_sha256,
        latency_budget_path,
        latency_budget_sha256,
        source_sha256,
        registry_sha256,
        *actors,
    ):
        assert private_value not in serialized


@pytest.mark.parametrize(
    ("name", "value", "attribute"),
    (
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED",
            "true",
            "semantic_supervisor_promotion_enabled",
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS",
            f"{'e' * 64},{'d' * 64}",
            "semantic_supervisor_promotion_canary_actor_bindings",
        ),
    ),
)
def test_noncanonical_promotion_env_retains_a_closed_invalid_sentinel(
    settings: object,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    attribute: str,
) -> None:
    monkeypatch.setenv(name, value)
    from friday.config import load_settings

    loaded = load_settings()
    assert getattr(loaded, attribute) is None
    semantic_public = loaded.public_dict()["semantic_supervisor"]
    assert isinstance(semantic_public, dict)
    public = semantic_public["promotion_config"]
    assert isinstance(public, dict)
    assert public["operator_gate_enabled"] is False
    assert public["raw_settings_valid"] is False


@pytest.mark.parametrize(
    ("env_name", "raw", "attribute", "public_key", "expected"),
    (
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
            "999",
            "semantic_supervisor_max_steps",
            "max_steps",
            999,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
            "not-an-int",
            "semantic_supervisor_max_steps",
            "max_steps",
            0,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
            "-1",
            "semantic_supervisor_max_review_rounds",
            "max_review_rounds",
            -1,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
            "not-an-int",
            "semantic_supervisor_max_review_rounds",
            "max_review_rounds",
            -1,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "15.1",
            "semantic_supervisor_timeout_sec",
            "timeout_sec",
            15.1,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "nan",
            "semantic_supervisor_timeout_sec",
            "timeout_sec",
            0.0,
        ),
        (
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            "inf",
            "semantic_supervisor_timeout_sec",
            "timeout_sec",
            0.0,
        ),
    ),
)
def test_invalid_semantic_rollout_numeric_env_stays_closed(
    settings: object,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    raw: str,
    attribute: str,
    public_key: str,
    expected: int | float,
) -> None:
    monkeypatch.setenv(env_name, raw)
    from friday.config import load_settings

    loaded = load_settings()
    assert getattr(loaded, attribute) == expected
    projection = loaded.public_dict()["semantic_supervisor"]
    assert projection[public_key] == expected
    assert projection["promotion_admitted"] is False
    json.dumps(projection, allow_nan=False)


def test_semantic_supervisor_config_is_forwarded_by_operator_templates() -> None:
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    cli = (root / "friday" / "cli.py").read_text(encoding="utf-8")
    expected_defaults = (
        ("FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS", "1"),
        ("FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS", "6"),
        ("FRIDAY_SEMANTIC_SUPERVISOR_MODE", "off"),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED", "0"),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_TASKS", ""),
        ("FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC", "12"),
    )
    for key, value in expected_defaults:
        assert f"{key}={value}" in env_example
        assert f"{key}={value}" in cli
        assert f"{key}: ${{{key}:-{value}}}" in compose
    env_block = "\n".join(f"{key}={value}" for key, value in expected_defaults)
    compose_block = "\n".join(f"  {key}: ${{{key}:-{value}}}" for key, value in expected_defaults)
    assert env_example.rstrip().endswith(env_block)
    assert f'{env_block}\n"""' in cli
    assert compose_block in compose
