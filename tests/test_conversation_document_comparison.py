from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypedDict

import pytest

import friday.orchestration.conversation_document_comparison as comparison_module
from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart
from friday.file_evidence import FileBodyKind, FileEvidenceSet, FileEvidenceView, FileRegistrationKind
from friday.file_evidence_reader import (
    _PROCESS_AUTHORITY as _FILE_EVIDENCE_AUTHORITY,  # noqa: PLC2701
)
from friday.file_evidence_reader import (
    PreparedFileEvidence,
)
from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
)
from friday.interaction_control_plane.turn_trace import FailureReason, OutcomeStatus
from friday.model_profiles import ModelProfileLease, ModelRequirements
from friday.orchestration.conversation_document_comparison import (
    ConversationDocumentComparisonError,
    compare_conversation_with_document,
    conversation_document_comparison_is_process_owned,
    conversation_document_comparison_lease_is_current,
    conversation_document_comparison_plan_sha256,
    conversation_document_model_evidence_identity,
    conversation_document_model_requirements,
)
from friday.orchestration.turn_context import (
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayResult,
    _exact_result,  # noqa: PLC2701
)
from friday.retrieval.archive_evidence_snapshot import archive_selected_evidence_snapshot_sha256
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.archive_search_message_adapter import MESSAGE_PASSAGE_INDEX_VERSION
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
)
from friday.source_identity import tenant_authorized_file_snapshot_token


class _DurableEvidenceDigests(TypedDict):
    message_evidence_sha256: str
    document_evidence_sha256: str
    evidence_bundle_sha256: str


@dataclass(frozen=True)
class _ParentContext:
    inherited_budget: InheritedTurnBudget

    def canonical_sha256(self) -> str:
        return hashlib.sha256(repr(self.inherited_budget).encode("utf-8")).hexdigest()


def _install_parent_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_model_calls: int = 2,
    max_output_tokens: int = 768,
) -> tuple[_ParentContext, float]:
    deadline = time.monotonic() + 10
    parent = _ParentContext(
        InheritedTurnBudget(
            TurnSafetyDeadline(int(deadline * 1_000_000_000)),
            ModelAntiLoopBudget(max_model_calls, 0),
            TurnResourceBudget(0, 0, 0, max_output_tokens),
        )
    )

    def current(expected: object = None) -> _ParentContext:
        if expected is not None and expected is not parent:
            raise TurnContextError("test parent identity drifted")
        return parent

    monkeypatch.setattr(comparison_module, "AuthenticatedTurnContext", _ParentContext)
    monkeypatch.setattr(comparison_module, "current_primary_authenticated_turn_context", current)
    return parent, deadline


def _durable_evidence_digests(
    selected: SelectedArchiveEvidence,
    *,
    document_evidence_sha256: str = "d" * 64,
) -> _DurableEvidenceDigests:
    message_evidence_sha256 = hashlib.sha256(
        json.dumps(
            selected.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    evidence_bundle_sha256 = hashlib.sha256(
        json.dumps(
            {
                "document_evidence_sha256": document_evidence_sha256,
                "message_evidence_sha256": message_evidence_sha256,
                "schema": "friday.compare-conversation-document-evidence-bundle.v1",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    return {
        "message_evidence_sha256": message_evidence_sha256,
        "document_evidence_sha256": document_evidence_sha256,
        "evidence_bundle_sha256": evidence_bundle_sha256,
    }


def _comparison_plan_sha256(durable: _DurableEvidenceDigests) -> str:
    return conversation_document_comparison_plan_sha256(
        request="Сопоставь выбранные сообщения с этим документом",
        message_evidence_sha256=durable["message_evidence_sha256"],
        document_evidence_sha256=durable["document_evidence_sha256"],
        evidence_bundle_sha256=durable["evidence_bundle_sha256"],
        message_model_evidence_sha256="1" * 64,
        document_model_evidence_sha256="2" * 64,
        model_evidence_sha256="3" * 64,
    )


def _message_evidence(
    *,
    text: str = "В переписке решили оставить точный режим CUDA graphs.",
    coverage: ArchiveEvidenceReplayCoverageGrade = ArchiveEvidenceReplayCoverageGrade.COMPLETE,
) -> tuple[ArchiveEvidenceReplayResult, SelectedArchiveEvidence]:
    conversation_id = "conv_0123456789abcdef"
    source = SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        "person-main",
        CanonicalObjectKind.CONVERSATION,
        conversation_id,
    )
    representation = SourceRepresentation(RepresentationKind.CONVERSATION, conversation_id)
    revision = SourceRevision(
        representation,
        RevisionKind.MESSAGE_LEDGER_SHA256,
        "e" * 64,
    )
    resolved = ResolvedSource.create(
        source_ref=source,
        representations=(representation,),
        lifecycle=(LifecycleRef(representation, LifecycleState.ACTIVE),),
        revisions=(revision,),
        revalidation_targets=(RevalidationTarget(representation, AuthorityScope.PRINCIPAL),),
    )
    passage = PassageRef(
        source,
        revision,
        MessageWindowLocator(
            first_message_id="msg_1111111111111111",
            last_message_id="msg_2222222222222222",
            start_at="2026-08-20T08:00:00+00:00",
            end_at="2026-08-20T09:00:00+00:00",
            context_before=1,
            context_after=1,
        ),
        MESSAGE_PASSAGE_INDEX_VERSION,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    snapshot = archive_selected_evidence_snapshot_sha256(resolved, (passage,), (text,))
    replay = _exact_result(
        corpus=ArchiveSearchCorpus.MESSAGES,
        coverage_grade=coverage,
        resolved_source=resolved,
        passage_refs=(passage,),
        texts=(text,),
    )
    selected = SelectedArchiveEvidence(
        work_item_id="work_0123456789abcdef",
        corpus=SelectedArchiveCorpus.MESSAGES,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256=snapshot,
        coverage_sha256="c" * 64,
        coverage_grade=SelectedArchiveCoverageGrade(coverage.value),
        origin_boundary_user_message_id="msg_3333333333333333",
    )
    return replay, selected


def _prepared_document(
    *,
    text: str = "В документе зафиксирован обычный режим без CUDA graphs.",
    raw_id: str = "raw_0123456789abcdef",
) -> PreparedFileEvidence:
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw = {
        "id": raw_id,
        "user_id": "tenant-main",
        "source": "upload",
        "source_ref": "telegram-file:test",
        "content_type": "file",
        "received_at": "2026-08-25T01:00:00+00:00",
        "content_hash": content_sha256,
        "_raw_content": text,
        "_raw_metadata": '{"filename":"decision.txt"}',
    }
    token = tenant_authorized_file_snapshot_token(
        raw,
        content_sha256=content_sha256,
        tenant_id="tenant-main",
        storage_owner_id="tenant-main",
    )
    assert token is not None
    view = FileEvidenceView(
        raw_id=raw_id,
        source_identity_sha256=token.source.identity_sha256,
        registration=FileRegistrationKind.VALID,
        disk_verified=True,
        workspace_relative_path=None,
        workspace_sha256=None,
        workspace_source_sha256=None,
        body_kind=FileBodyKind.EXTRACTED,
        source_complete=True,
        projection_applied=False,
        projection_empty_no_match=False,
        source_readable=True,
        verification_eligible=True,
    )
    evidence_set = FileEvidenceSet(items=(view,), expected_count=1)
    part = EvidencePart(
        label="A1",
        display_name="decision.txt",
        media_type="text/plain",
        source_identity_sha256=token.source.identity_sha256,
        text=text,
    )
    bundle = EvidenceBundle(
        parts=(part,),
        citations=(CitationBinding("A1", token.source.identity_sha256),),
        file_evidence_set_sha256=evidence_set.identity_sha256(),
    )
    return PreparedFileEvidence(
        tenant_id="tenant-main",
        person_id="person-main",
        raw_ids=(raw_id,),
        snapshot_tokens=(token,),
        file_evidence_set=evidence_set,
        bundle=bundle,
        historical_selection=None,
        _process_authority=_FILE_EVIDENCE_AUTHORITY,
    )


class _ComparisonModel:
    def __init__(
        self,
        *,
        answer: str = ("В сообщениях выбран CUDA graphs [M1.1], а документ фиксирует обычный режим [D1]."),
        verifier_supported: bool = True,
        synthesis_tool_call: bool = False,
        verifier_tool_call: bool = False,
        verifier_finish_reason: str = "stop",
        lease_current: bool = True,
        lease_states: tuple[bool, ...] | None = None,
        leased_context_tokens: int | None = None,
        requirements_sha256: str | None = None,
        leased_tool_rounds: int = 0,
        leased_tool_calls: int = 0,
        after_verifier: Callable[[], None] | None = None,
        available_context_tokens: int | None = None,
        expected_message_text: str = "В переписке решили оставить точный режим CUDA graphs.",
    ) -> None:
        self.answer = answer
        self.verifier_supported = verifier_supported
        self.synthesis_tool_call = synthesis_tool_call
        self.verifier_tool_call = verifier_tool_call
        self.verifier_finish_reason = verifier_finish_reason
        self.lease_states = lease_states or (lease_current,)
        self.leased_context_tokens = leased_context_tokens
        self.requirements_sha256 = requirements_sha256
        self.leased_tool_rounds = leased_tool_rounds
        self.leased_tool_calls = leased_tool_calls
        self.lease_checks = 0
        self.process_lease_checks = 0
        self.after_verifier = after_verifier
        self._available_context_tokens = available_context_tokens
        self.expected_message_text = expected_message_text
        self.acquire_calls = 0
        self.lease: ModelProfileLease | None = None
        self.calls: list[list[dict[str, Any]]] = []
        self.call_kwargs: list[dict[str, Any]] = []
        self.requirements: ModelRequirements | None = None
        self.verifier_answer = ""

    def available_context_tokens(self) -> int:
        if self._available_context_tokens is None:
            return 8_192
        return self._available_context_tokens

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease:
        self.acquire_calls += 1
        assert absolute_deadline > time.monotonic()
        assert requirements.prepared_evidence_items == 2
        assert requirements.max_tool_steps == 0
        assert requirements.max_tool_rounds == 0
        assert requirements.max_tool_calls == 0
        assert requirements.verifier_required is True
        self.requirements = requirements
        self.lease = ModelProfileLease(
            profile_id="conversation-document-test:dispatcher",
            attestation_sha256="a" * 64,
            requirements_sha256=self.requirements_sha256 or requirements.canonical_sha256(),
            capabilities=requirements.capabilities,
            required_context_tokens=(
                requirements.required_context_tokens
                if self.leased_context_tokens is None
                else self.leased_context_tokens
            ),
            prepared_evidence_items=requirements.prepared_evidence_items,
            max_tool_steps=requirements.max_tool_steps,
            max_tool_rounds=self.leased_tool_rounds,
            max_tool_calls=self.leased_tool_calls,
            effect=requirements.effect,
            verifier_required=requirements.verifier_required,
            process_epoch_sha256="b" * 64,
            _gate_authority=self,
            _gate_generation=1,
        )
        return self.lease

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool:
        index = self.lease_checks
        self.lease_checks += 1
        return bool(
            self.lease_states[min(index, len(self.lease_states) - 1)]
            and absolute_deadline > time.monotonic()
            and lease is self.lease
            and requirements is self.requirements
        )

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool:
        self.process_lease_checks += 1
        return bool(
            lease is self.lease
            and requirements is self.requirements
            and self.lease is not None
            and self.lease.requirements_sha256 == requirements.canonical_sha256()
        )

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert lease is self.lease
        assert requirements is self.requirements
        assert kwargs["max_tokens"] > 0
        assert kwargs["temperature"] == 0.0
        assert "tools" not in kwargs
        self.calls.append(messages)
        self.call_kwargs.append(kwargs)
        if len(self.calls) == 1:
            payload = json.loads(str(messages[-1]["content"]))
            assert payload["evidence"]["messages"]["fragments"] == [
                {
                    "label": "M1.1",
                    "text": self.expected_message_text,
                }
            ]
            assert payload["evidence"]["document"]["label"] == "D1"
            if self.synthesis_tool_call:
                return {
                    "content": "",
                    "tool_calls": [{"id": "forbidden"}],
                    "finish_reason": "tool_calls",
                }
            return {"content": self.answer, "tool_calls": None, "finish_reason": "stop"}
        verifier_input = json.loads(str(messages[-1]["content"]))
        self.verifier_answer = str(verifier_input["answer"])
        assert self.verifier_answer.endswith(self.answer)
        assert verifier_input["evidence"]["document"]["label"] == "D1"
        if self.after_verifier is not None:
            self.after_verifier()
        if self.verifier_tool_call:
            return {
                "content": "",
                "tool_calls": [{"id": "forbidden-verifier"}],
                "finish_reason": "tool_calls",
            }
        return {
            "content": json.dumps(
                {
                    "schema": "friday.v12-file-verifier.v1",
                    "supported": self.verifier_supported,
                    "citation_labels": ["M1.1", "D1"],
                    "unsupported_claims": 0 if self.verifier_supported else 1,
                }
            ),
            "tool_calls": None,
            "finish_reason": self.verifier_finish_reason,
        }


@pytest.mark.asyncio
async def test_exact_two_source_comparison_uses_two_tools_disabled_calls_and_rechecks_lease() -> None:
    replay, selected = _message_evidence()
    document = _prepared_document()
    model = _ComparisonModel()
    durable_digests = _durable_evidence_digests(selected)

    comparison = await compare_conversation_with_document(
        model,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=document,
        **durable_digests,
        absolute_deadline=time.monotonic() + 10,
    )

    assert len(model.calls) == 2
    assert comparison.answer == model.answer
    assert comparison.citation_labels == ("M1.1", "D1")
    assert comparison.message_coverage_grade is ArchiveEvidenceReplayCoverageGrade.COMPLETE
    assert comparison.message_evidence_sha256 == durable_digests["message_evidence_sha256"]
    assert comparison.document_evidence_sha256 == durable_digests["document_evidence_sha256"]
    assert comparison.evidence_bundle_sha256 == durable_digests["evidence_bundle_sha256"]
    assert comparison.requirements is conversation_document_model_requirements()
    assert comparison.requirements.max_tool_steps == 0
    assert comparison.requirements.max_tool_rounds == 0
    assert comparison.requirements.max_tool_calls == 0
    assert model.lease_checks == 3
    assert conversation_document_model_evidence_identity(
        replay,
        selected,
        document,
    ) == (
        comparison.message_model_evidence_sha256,
        comparison.document_model_evidence_sha256,
        comparison.model_evidence_sha256,
    )
    assert await conversation_document_comparison_lease_is_current(
        model,
        comparison,
        absolute_deadline=time.monotonic() + 10,
    )
    assert model.lease_checks == 4


@pytest.mark.asyncio
async def test_conversation_comparison_adopts_q38_only_for_large_exact_payloads() -> None:
    replay, selected = _message_evidence()
    durable = _durable_evidence_digests(selected)
    small_q38 = _ComparisonModel(available_context_tokens=40_960)

    small = await compare_conversation_with_document(
        small_q38,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **durable,
        absolute_deadline=time.monotonic() + 10,
    )

    assert small.requirements is conversation_document_model_requirements(8_192)
    assert small_q38.acquire_calls == 1

    large_document = _prepared_document(text="D" * 6_000)
    q36 = _ComparisonModel(available_context_tokens=8_192)
    with pytest.raises(ConversationDocumentComparisonError, match="attested context"):
        await compare_conversation_with_document(
            q36,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=large_document,
            **durable,
            absolute_deadline=time.monotonic() + 10,
        )
    assert q36.acquire_calls == 0

    q38 = _ComparisonModel(available_context_tokens=40_960)
    large = await compare_conversation_with_document(
        q38,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=large_document,
        **durable,
        absolute_deadline=time.monotonic() + 10,
    )

    assert large.requirements is conversation_document_model_requirements(40_960)
    assert q38.acquire_calls == 1
    assert len(q38.calls) == 2
    assert large.plan_sha256 != conversation_document_comparison_plan_sha256(
        request="Сопоставь выбранные сообщения с этим документом",
        message_evidence_sha256=large.message_evidence_sha256,
        document_evidence_sha256=large.document_evidence_sha256,
        evidence_bundle_sha256=large.evidence_bundle_sha256,
        message_model_evidence_sha256=large.message_model_evidence_sha256,
        document_model_evidence_sha256=large.document_model_evidence_sha256,
        model_evidence_sha256=large.model_evidence_sha256,
        requirements=conversation_document_model_requirements(8_192),
    )
    with pytest.raises(ConversationDocumentComparisonError):
        replace(
            large,
            requirements=conversation_document_model_requirements(8_192),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expected_calls", "reason", "synthesis", "verification"),
    (
        (
            _ComparisonModel(answer="Нет нужной метки [D1]."),
            1,
            FailureReason.INVALID_CONTRACT,
            OutcomeStatus.FAILED,
            OutcomeStatus.NOT_STARTED,
        ),
        (
            _ComparisonModel(verifier_supported=False),
            2,
            FailureReason.VERIFICATION_REJECTED,
            OutcomeStatus.SUCCEEDED,
            OutcomeStatus.FAILED,
        ),
        (
            _ComparisonModel(synthesis_tool_call=True),
            1,
            FailureReason.INVALID_CONTRACT,
            OutcomeStatus.FAILED,
            OutcomeStatus.NOT_STARTED,
        ),
        (
            _ComparisonModel(verifier_tool_call=True),
            2,
            FailureReason.INVALID_CONTRACT,
            OutcomeStatus.SUCCEEDED,
            OutcomeStatus.FAILED,
        ),
        (
            _ComparisonModel(verifier_finish_reason="length"),
            2,
            FailureReason.INVALID_CONTRACT,
            OutcomeStatus.SUCCEEDED,
            OutcomeStatus.FAILED,
        ),
    ),
)
async def test_synthesis_and_verifier_fail_closed(
    model: _ComparisonModel,
    expected_calls: int,
    reason: FailureReason,
    synthesis: OutcomeStatus,
    verification: OutcomeStatus,
) -> None:
    replay, selected = _message_evidence()

    with pytest.raises(ConversationDocumentComparisonError) as captured:
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )

    assert len(model.calls) == expected_calls
    assert captured.value.model_calls == expected_calls
    assert captured.value.failure_reason is reason
    assert captured.value.synthesis_outcome is synthesis
    assert captured.value.verification_outcome is verification


@pytest.mark.asyncio
async def test_stale_lease_and_message_snapshot_are_rejected_before_model_dispatch() -> None:
    replay, selected = _message_evidence()
    stale_model = _ComparisonModel(lease_current=False)
    with pytest.raises(ConversationDocumentComparisonError) as stale:
        await compare_conversation_with_document(
            stale_model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )
    assert stale.value.failure_reason is FailureReason.STALE_STATE
    assert stale_model.calls == []

    drifted = replace(selected, source_snapshot_sha256="f" * 64)
    untouched_model = _ComparisonModel()
    with pytest.raises(ConversationDocumentComparisonError):
        await compare_conversation_with_document(
            untouched_model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=drifted,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(drifted),
            absolute_deadline=time.monotonic() + 10,
        )
    assert untouched_model.lease is None
    assert untouched_model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_states", "expected_calls", "expected_checks"),
    (
        ((False,), 0, 1),
        ((True, False), 1, 2),
        ((True, True, False), 2, 3),
    ),
)
async def test_lease_revocation_before_each_call_or_result_fails_closed(
    lease_states: tuple[bool, ...],
    expected_calls: int,
    expected_checks: int,
) -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel(lease_states=lease_states)
    with pytest.raises(ConversationDocumentComparisonError) as captured:
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )
    assert captured.value.failure_reason is FailureReason.STALE_STATE
    assert len(model.calls) == expected_calls
    assert model.lease_checks == expected_checks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lease_kwargs",
    (
        {"leased_context_tokens": 4_096},
        {"requirements_sha256": "c" * 64},
        {"leased_tool_rounds": 1, "leased_tool_calls": 1},
    ),
)
async def test_downgraded_or_drifted_lease_is_rejected_before_model_call(
    lease_kwargs: dict[str, object],
) -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel(**lease_kwargs)  # type: ignore[arg-type]
    with pytest.raises(ConversationDocumentComparisonError) as captured:
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )
    assert captured.value.failure_reason is FailureReason.STALE_STATE
    assert model.lease_checks == 0
    assert model.calls == []


@pytest.mark.asyncio
async def test_publication_recheck_rejects_restart_after_result_seal() -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel(lease_states=(True, True, True, False))
    comparison = await compare_conversation_with_document(
        model,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **_durable_evidence_digests(selected),
        absolute_deadline=time.monotonic() + 10,
    )

    assert not await conversation_document_comparison_lease_is_current(
        model,
        comparison,
        absolute_deadline=time.monotonic() + 10,
    )
    assert model.lease_checks == 4


@pytest.mark.asyncio
async def test_authenticated_parent_narrows_output_deadline_and_zero_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay, selected = _message_evidence()
    _parent, parent_deadline = _install_parent_context(
        monkeypatch,
        max_output_tokens=384,
    )
    model = _ComparisonModel()
    comparison = await compare_conversation_with_document(
        model,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **_durable_evidence_digests(selected),
        absolute_deadline=parent_deadline + 30,
    )

    assert [item["max_tokens"] for item in model.call_kwargs] == [384, 256]
    assert all(float(item["absolute_deadline"]) <= parent_deadline for item in model.call_kwargs)
    assert comparison.requirements.max_tool_rounds == comparison.requirements.max_tool_calls == 0


@pytest.mark.asyncio
async def test_authenticated_parent_with_one_model_call_refuses_before_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay, selected = _message_evidence()
    _install_parent_context(monkeypatch, max_model_calls=1)
    model = _ComparisonModel()
    with pytest.raises(ConversationDocumentComparisonError) as captured:
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )
    assert captured.value.failure_reason is FailureReason.BUDGET_EXHAUSTED
    assert model.lease is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_verifier_max_answer_is_reserved_before_model_dispatch() -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel()

    with pytest.raises(ConversationDocumentComparisonError) as captured:
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(text="слово " * 50),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )

    assert str(captured.value) == "comparison evidence exceeds the attested context"
    assert model.lease is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_unsafe_model_projection_is_rejected_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel()
    monkeypatch.setattr(comparison_module, "model_messages_are_secret_free", lambda _messages: False)

    with pytest.raises(ConversationDocumentComparisonError):
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=time.monotonic() + 10,
        )

    assert model.lease is None
    assert model.calls == []


def test_plan_and_evidence_identities_are_body_sensitive_but_body_free() -> None:
    replay, selected = _message_evidence()
    first = conversation_document_model_evidence_identity(
        replay,
        selected,
        _prepared_document(text="Первая версия документа."),
    )
    second = conversation_document_model_evidence_identity(
        replay,
        selected,
        _prepared_document(text="Вторая версия документа."),
    )

    assert first != second
    encoded = json.dumps(first + second)
    assert "Первая" not in encoded
    assert "Вторая" not in encoded


def test_plan_binds_each_durable_evidence_digest() -> None:
    _replay, selected = _message_evidence()
    durable = _durable_evidence_digests(selected)
    baseline = _comparison_plan_sha256(durable)

    changed_message = durable.copy()
    changed_message["message_evidence_sha256"] = "a" * 64
    assert baseline != _comparison_plan_sha256(changed_message)

    changed_document = durable.copy()
    changed_document["document_evidence_sha256"] = "b" * 64
    assert baseline != _comparison_plan_sha256(changed_document)

    changed_bundle = durable.copy()
    changed_bundle["evidence_bundle_sha256"] = "c" * 64
    assert baseline != _comparison_plan_sha256(changed_bundle)


def test_plan_binds_the_exact_v2_requirements_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _replay, selected = _message_evidence()
    durable = _durable_evidence_digests(selected)
    baseline = _comparison_plan_sha256(durable)
    requirements = conversation_document_model_requirements()
    monkeypatch.setattr(
        comparison_module,
        "conversation_document_model_requirements",
        lambda: replace(requirements, max_tool_calls=1),
    )
    assert _comparison_plan_sha256(durable) != baseline


@pytest.mark.asyncio
async def test_mismatched_durable_message_evidence_is_rejected_before_dispatch() -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel()
    durable = _durable_evidence_digests(selected)
    durable["message_evidence_sha256"] = "f" * 64

    with pytest.raises(ConversationDocumentComparisonError, match="durable selected message"):
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **durable,
            absolute_deadline=time.monotonic() + 10,
        )

    assert model.lease is None
    assert model.calls == []


@pytest.mark.asyncio
async def test_partial_message_coverage_is_visible_and_verified() -> None:
    replay, selected = _message_evidence(coverage=ArchiveEvidenceReplayCoverageGrade.PARTIAL)
    model = _ComparisonModel()

    comparison = await compare_conversation_with_document(
        model,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **_durable_evidence_digests(selected),
        absolute_deadline=time.monotonic() + 10,
    )

    assert comparison.message_coverage_grade is ArchiveEvidenceReplayCoverageGrade.PARTIAL
    assert comparison.answer.startswith("Охват выбранных сообщений неполный;")
    assert model.verifier_answer == comparison.answer


@pytest.mark.asyncio
async def test_accepted_value_revalidates_answer_contract_on_construction() -> None:
    replay, selected = _message_evidence()
    comparison = await compare_conversation_with_document(
        _ComparisonModel(),
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **_durable_evidence_digests(selected),
        absolute_deadline=time.monotonic() + 10,
    )

    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, answer="Подмена без сообщения [D1].")
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, answer="<tool>подмена</tool> [M1.1] [D1].")
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, _process_authority=object())
    with pytest.raises(ConversationDocumentComparisonError):
        replace(
            comparison,
            answer="Иной вывод из сообщений [M1.1], а документ подтверждает его [D1].",
        )
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, model_evidence_sha256="f" * 64)
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, message_evidence_sha256="f" * 64)
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, document_evidence_sha256="f" * 64)
    with pytest.raises(ConversationDocumentComparisonError):
        replace(comparison, evidence_bundle_sha256="f" * 64)
    with pytest.raises(ConversationDocumentComparisonError):
        replace(
            comparison,
            requirements=replace(comparison.requirements, required_context_tokens=4_096),
        )

    object.__setattr__(
        comparison,
        "answer",
        "Иной вывод из сообщений [M1.1], а документ подтверждает его [D1].",
    )
    assert not conversation_document_comparison_is_process_owned(comparison)
    with pytest.raises(TypeError):
        await conversation_document_comparison_lease_is_current(
            _ComparisonModel(),
            comparison,
            absolute_deadline=time.monotonic() + 10,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_deadline", [math.nan, math.inf, -math.inf, True])
async def test_nonfinite_or_boolean_deadline_is_rejected_before_model_use(
    invalid_deadline: object,
) -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel()

    with pytest.raises(ConversationDocumentComparisonError, match="deadline is exhausted"):
        await compare_conversation_with_document(
            model,
            request="Сопоставь выбранные сообщения с этим документом",
            message_replay=replay,
            selected_message_evidence=selected,
            prepared_document=_prepared_document(),
            **_durable_evidence_digests(selected),
            absolute_deadline=invalid_deadline,  # type: ignore[arg-type]
        )

    assert model.calls == []
    assert model.lease_checks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_deadline", [math.nan, math.inf, -math.inf, True])
async def test_publication_recheck_rejects_nonfinite_or_boolean_deadline(
    invalid_deadline: object,
) -> None:
    replay, selected = _message_evidence()
    model = _ComparisonModel()
    comparison = await compare_conversation_with_document(
        model,
        request="Сопоставь выбранные сообщения с этим документом",
        message_replay=replay,
        selected_message_evidence=selected,
        prepared_document=_prepared_document(),
        **_durable_evidence_digests(selected),
        absolute_deadline=time.monotonic() + 10,
    )
    checks_before_publication = model.lease_checks

    with pytest.raises(ConversationDocumentComparisonError, match="deadline is exhausted"):
        await conversation_document_comparison_lease_is_current(
            model,
            comparison,
            absolute_deadline=invalid_deadline,  # type: ignore[arg-type]
        )

    assert model.lease_checks == checks_before_publication
