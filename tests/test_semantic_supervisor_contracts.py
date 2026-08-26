from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

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
    build_supervisor_input,
    build_supervisor_messages,
    classify_supervisor_task,
    supervisor_eligibility,
    supervisor_mode_from_settings,
)
from friday.orchestration.supervisor_contracts import (
    FILE_CURRENT_READ_ID,
    HOST_SCAN_LOCAL_ID,
    PRIMARY_SYNTHESIS_ID,
    SUPERVISOR_PROPOSAL_SCHEMA,
    WEB_SEARCH_CURRENT_ID,
    SupervisorContractError,
    SupervisorMode,
    SupervisorProposal,
    TaskClass,
    canonical_sha256,
)
from friday.orchestration.supervisor_observation import SupervisorSkipReason


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


def test_proposal_cannot_construct_validated_execution_plan() -> None:
    supervisor_input = build_supervisor_input(_compare_turn(), _settings())
    proposal = SupervisorProposal.parse(_proposal_payload(supervisor_input))
    with pytest.raises(Exception, match="cannot be parsed"):
        ValidatedExecutionPlan.parse(proposal.payload())
    with pytest.raises(Exception, match="cannot be constructed"):
        ValidatedExecutionPlan(
            proposal_digest=proposal.canonical_sha256(),
            manifest_digest=supervisor_input.manifest.digest_hex(),
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


def test_empty_task_allowlist_never_invokes_supervisor() -> None:
    eligibility = supervisor_eligibility(_compare_turn(), _settings(semantic_supervisor_tasks=()))
    assert eligibility.eligible is False
    assert eligibility.skip_reason is SupervisorSkipReason.TASK_NOT_ALLOWLISTED


def test_supervisor_messages_keep_untrusted_user_text_out_of_policy() -> None:
    turn = _compare_turn()
    supervisor_input = build_supervisor_input(turn, _settings())
    system, user = build_supervisor_messages(supervisor_input)
    assert "do not authorize" in system["content"].casefold()
    payload = json.loads(user["content"])
    assert payload["trusted_policy"]["tools_allowed"] is False
    assert payload["trusted_policy"]["publication_allowed"] is False
    assert payload["untrusted_turn"]["message"] == supervisor_input.turn.message
    assert "FRIDAY_" not in user["content"]


def test_canonical_digest_is_order_independent() -> None:
    payload = {"b": 1, "a": True}
    assert canonical_sha256(payload) == canonical_sha256({"a": True, "b": 1})


def test_configuration_defaults_and_unknown_mode_stay_off(settings, monkeypatch) -> None:
    assert settings.semantic_supervisor_mode == "off"
    assert settings.semantic_supervisor_tasks == ()
    assert settings.public_dict()["semantic_supervisor"]["promotion_admitted"] is False
    monkeypatch.setenv("FRIDAY_SEMANTIC_SUPERVISOR_MODE", "please-assist")
    from friday.config import load_settings

    loaded = load_settings()
    assert loaded.semantic_supervisor_mode == "off"
