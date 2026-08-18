"""The first V12 route: current-turn, complete, registered file evidence."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from friday.evidence_bundle import EvidenceBundle
from friday.execution_kernel import (
    confirm_staged_request_effect,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
)
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PreparedFileEvidence,
    prepare_current_turn_file_evidence,
    prepared_file_evidence_is_process_owned,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.model_input_hygiene import (
    model_messages_are_secret_free,
    model_visible_text_is_secret_free,
)
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration.contracts import RouteClass, ToolEffect, TurnInput, TurnPlan
from friday.orchestration.file_read_contract import (
    V12_FILE_SYNTHESIS_SYSTEM,
    V12_FILE_VERIFIER_SCHEMA,
    V12_FILE_VERIFIER_SYSTEM,
    build_file_synthesis_messages,
    build_file_verifier_messages,
    require_file_verifier_clear,
    validate_file_synthesis_answer,
)
from friday.orchestration.router import (
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
    ReadOnlyRouteResult,
)
from friday.permissions import AuthorizationService
from friday.storage import normalize_conversation_mode
from friday.storage._conversations import (
    create_conversation_in_transaction,
    store_message_in_transaction,
)
from friday.storage._core import guarded_storage_transaction

_PROCESS_AUTHORITY = object()
_MAX_CANARY_FILES = 2
_MAX_ANSWER_JSON_UTF8_BYTES = 2_048
_SYNTHESIS_MAX_TOKENS = 512
_PREPARATION_BUDGET_SEC = 4.5
_PUBLICATION_RESERVE_SEC = 2.0
_MAX_ATTESTED_INPUT_UTF8_BYTES = 5_500


class V12FileReadError(RuntimeError):
    """A selected V12 file turn could not be safely published."""


@dataclass(frozen=True, slots=True)
class _PreparedFileContext:
    evidence: PreparedFileEvidence
    conversation_id: str | None
    interaction_mode: str


@dataclass(frozen=True, slots=True)
class _PreparedFileTurn:
    evidence: PreparedFileEvidence
    conversation_id: str | None
    interaction_mode: str
    model_lease: ModelProfileLease = field(repr=False, compare=False)
    model_requirements: ModelRequirements = field(repr=False)
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or not prepared_file_evidence_is_process_owned(self.evidence)
            or type(self.model_lease) is not ModelProfileLease
            or not isinstance(self.model_requirements, ModelRequirements)
            or self.model_requirements.prepared_evidence_items != len(self.evidence.bundle.parts)
        ):
            raise ValueError("prepared V12 file turn is not process-owned")


class _AttestedFileModel(Protocol):
    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool: ...

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: Literal["foreground", "background"],
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]: ...


def _file_requirements(evidence_items: int) -> ModelRequirements:
    return ModelRequirements(
        capabilities=frozenset(
            {
                ModelCapability.PREPARED_EVIDENCE_2,
                ModelCapability.CONTEXT_8K,
                ModelCapability.REMOTE_CANCELLATION,
            }
        ),
        required_context_tokens=8_192,
        prepared_evidence_items=evidence_items,
        max_tool_steps=0,
        effect=ModelEffect.READ,
        verifier_required=True,
    )


def _messages_fit_attested_context(messages: list[dict[str, str]]) -> bool:
    return (
        len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= _MAX_ATTESTED_INPUT_UTF8_BYTES
    )


def _require_deadline(deadline: float, *, stage: str, reserve: float = 0.0) -> None:
    if deadline - time.monotonic() <= reserve:
        raise TimeoutError(f"V12 publication deadline expired {stage}")


async def _call_model_once(
    model: _AttestedFileModel,
    lease: ModelProfileLease,
    requirements: ModelRequirements,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    deadline: float,
    priority: Literal["foreground", "background"],
) -> dict[str, Any]:
    if not model_messages_are_secret_free(messages):
        raise V12FileReadError("model payload requires a secret projection")
    if not _messages_fit_attested_context(messages):
        raise V12FileReadError("model payload exceeds the attested context tier")
    remaining = deadline - time.monotonic()
    if remaining <= _PUBLICATION_RESERVE_SEC:
        raise TimeoutError("V12 file route has no model budget")
    response = await asyncio.wait_for(
        model.complete(
            lease,
            requirements,
            messages,
            max_tokens=max_tokens,
            priority=priority,
            absolute_deadline=deadline - _PUBLICATION_RESERVE_SEC,
            temperature=0.0,
        ),
        timeout=max(0.001, remaining - _PUBLICATION_RESERVE_SEC),
    )
    if not isinstance(response, dict):
        raise V12FileReadError("model returned a non-object response")
    if response.get("finish_reason") != "stop" or response.get("tool_calls") not in (None, []):
        raise V12FileReadError("model response was incomplete or effectful")
    content = response.get("content")
    if not isinstance(content, str):
        raise V12FileReadError("model response has no text")
    return response


class V12FileReadHandler:
    """Read, synthesize, verify and atomically publish one current-file turn."""

    route = RouteClass.FILE_READ
    effect = ToolEffect.READ

    def __init__(
        self,
        *,
        storage: Any,
        authorization: AuthorizationService,
        settings: Any,
        model: _AttestedFileModel,
    ) -> None:
        self._storage = storage
        self._authorization = authorization
        self._settings = settings
        self._model = model

    def _prepare_sync(
        self,
        request: ReadOnlyRouteRequest,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        if request.user_id != request.actor.user_id or not 1 <= len(request.attachments) <= _MAX_CANARY_FILES:
            return None
        try:
            evidence = prepare_current_turn_file_evidence(
                self._storage,
                self._authorization,
                self._settings.files_dir,
                request.actor,
                request.attachments,
                max_bytes=self._settings.max_upload_bytes,
                absolute_deadline=absolute_deadline,
            )
        except (FileEvidenceUnavailable, TimeoutError):
            return None

        conversation_id = request.conversation_id
        if conversation_id is not None:
            conversation = self._storage.get_conversation(conversation_id, request.actor.own_id)
            if not isinstance(conversation, dict):
                return None
            interaction_mode = normalize_conversation_mode(str(conversation.get("mode") or "dialogue"))
        else:
            interaction_mode = normalize_conversation_mode(request.conversation_mode or "dialogue")
        return _PreparedFileContext(
            evidence=evidence,
            conversation_id=conversation_id,
            interaction_mode=interaction_mode,
        )

    async def _prepare_context(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        """Strategy seam for another read-only route over the same evidence plane."""

        del turn, plan
        return await asyncio.to_thread(self._prepare_sync, request, absolute_deadline)

    async def prepare(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
    ) -> ReadOnlyRoutePreparation | None:
        preparation_deadline = time.monotonic() + _PREPARATION_BUDGET_SEC
        if request.turn_deadline is not None:
            preparation_deadline = min(preparation_deadline, request.turn_deadline)
        prepared = await self._prepare_context(
            request,
            turn,
            plan,
            preparation_deadline,
        )
        if prepared is None:
            return None
        synthesis_messages = self._synthesis_messages(turn, plan, prepared.evidence.bundle)
        verifier_messages = self._verifier_messages(turn, prepared.evidence.bundle, "")
        empty_answer_bytes = len(json.dumps("", ensure_ascii=False).encode("utf-8"))
        reserved_verifier_bytes = (
            len(json.dumps(verifier_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            # The answer is JSON-encoded once into the verifier's user
            # content, then that content is encoded again as a chat message.
            # Quotes/backslashes can therefore expand twice.  Reserve the
            # exact closed answer budget at the worst two-byte expansion so
            # every admitted answer can reach the verifier without truncation.
            + 2 * (_MAX_ANSWER_JSON_UTF8_BYTES - empty_answer_bytes)
        )
        if not (
            model_messages_are_secret_free(synthesis_messages)
            and _messages_fit_attested_context(synthesis_messages)
            and model_messages_are_secret_free(verifier_messages)
            and reserved_verifier_bytes <= _MAX_ATTESTED_INPUT_UTF8_BYTES
        ):
            return None
        requirements = _file_requirements(len(prepared.evidence.bundle.parts))
        lease = await self._model.acquire_lease(
            requirements,
            absolute_deadline=preparation_deadline,
        )
        if type(lease) is not ModelProfileLease:
            return None
        attested = _PreparedFileTurn(
            evidence=prepared.evidence,
            conversation_id=prepared.conversation_id,
            interaction_mode=prepared.interaction_mode,
            model_lease=lease,
            model_requirements=requirements,
            _process_authority=_PROCESS_AUTHORITY,
        )
        return ReadOnlyRoutePreparation(
            route=self.route,
            plan_sha256=plan.canonical_sha256(),
            evidence_identity_sha256=attested.evidence.identity_sha256,
            private_payload=attested,
        )

    def _prepared_matches(
        self,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> _PreparedFileTurn | None:
        prepared = preparation.private_payload
        if (
            type(prepared) is not _PreparedFileTurn
            or prepared._process_authority is not _PROCESS_AUTHORITY
            or not prepared_file_evidence_is_process_owned(prepared.evidence)
            or type(prepared.model_lease) is not ModelProfileLease
            or plan.route is not self.route
            or preparation.route is not self.route
            or preparation.plan_sha256 != plan.canonical_sha256()
            or preparation.evidence_identity_sha256 != prepared.evidence.identity_sha256
        ):
            return None
        return prepared

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        del turn
        prepared = self._prepared_matches(plan, preparation)
        if prepared is None:
            return False
        deadline = request.turn_deadline or (time.monotonic() + _PREPARATION_BUDGET_SEC)
        return await self._model.lease_is_current(
            prepared.model_lease,
            prepared.model_requirements,
            absolute_deadline=deadline,
        )

    @staticmethod
    def _synthesis_messages(
        turn: TurnInput,
        plan: TurnPlan,
        bundle: EvidenceBundle,
    ) -> list[dict[str, str]]:
        return build_file_synthesis_messages(turn, plan, bundle)

    async def _synthesize(
        self,
        turn: TurnInput,
        plan: TurnPlan,
        bundle: EvidenceBundle,
        lease: ModelProfileLease,
        requirements: ModelRequirements,
        *,
        deadline: float,
    ) -> str:
        response = await _call_model_once(
            self._model,
            lease,
            requirements,
            self._synthesis_messages(turn, plan, bundle),
            max_tokens=_SYNTHESIS_MAX_TOKENS,
            deadline=deadline,
            priority="foreground",
        )
        try:
            return validate_file_synthesis_answer(
                response["content"],
                bundle.citation_labels,
            )
        except ValueError:
            raise V12FileReadError("synthesis returned unsafe text") from None

    @staticmethod
    def _verifier_messages(
        turn: TurnInput,
        bundle: EvidenceBundle,
        answer: str,
    ) -> list[dict[str, str]]:
        return build_file_verifier_messages(turn, bundle, answer)

    async def _verify(
        self,
        turn: TurnInput,
        bundle: EvidenceBundle,
        answer: str,
        lease: ModelProfileLease,
        requirements: ModelRequirements,
        *,
        deadline: float,
    ) -> None:
        response = await _call_model_once(
            self._model,
            lease,
            requirements,
            self._verifier_messages(turn, bundle, answer),
            max_tokens=256,
            deadline=deadline,
            priority="foreground",
        )
        try:
            require_file_verifier_clear(response["content"], bundle.citation_labels)
        except ValueError:
            raise V12FileReadError("verifier rejected the answer") from None

    def _publish_sync(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        prepared: _PreparedFileTurn,
        answer: str,
        *,
        deadline: float,
    ) -> tuple[str, str, str]:
        _require_deadline(
            deadline,
            stage="before effect ownership",
            reserve=_PUBLICATION_RESERVE_SEC,
        )
        if not model_visible_text_is_secret_free(answer):
            raise V12FileReadError("publication output requires a secret projection")
        evidence = prepared.evidence

        def before_commit() -> None:
            _require_deadline(deadline, stage="before transaction commit")
            if not model_visible_text_is_secret_free(answer):
                raise V12FileReadError("publication output requires a secret projection")

        try:
            with guarded_storage_transaction(
                self._storage,
                before_commit=before_commit,
                lock_timeout_sec=max(
                    0.0,
                    deadline - time.monotonic() - _PUBLICATION_RESERVE_SEC,
                ),
                after_commit=confirm_staged_request_effect,
                after_rollback=rollback_staged_request_effect,
            ) as conn:
                if not reauthorize_prepared_file_evidence_in_transaction(
                    conn,
                    self._authorization,
                    self._settings.files_dir,
                    request.actor,
                    evidence,
                    max_bytes=self._settings.max_upload_bytes,
                    storage=self._storage,
                ):
                    raise V12FileReadError("file authority changed before publication")
                _require_deadline(deadline, stage="during final reauthorization")
                if not model_visible_text_is_secret_free(answer):
                    raise V12FileReadError("publication output requires a secret projection")
                if not stage_request_effect_possible_in_transaction(conn):
                    raise V12FileReadError("request effect fence could not be committed")

                conversation_id = prepared.conversation_id
                interaction_mode = prepared.interaction_mode
                if conversation_id is None:
                    conversation = create_conversation_in_transaction(
                        conn,
                        request.actor.own_id,
                        title=turn.message[:80],
                        mode=interaction_mode,
                    )
                    conversation_id = str(conversation.get("id") or "")
                else:
                    conversation_row = conn.execute(
                        "SELECT id, mode FROM conversations WHERE id=? AND user_id=?",
                        (conversation_id, request.actor.own_id),
                    ).fetchone()
                    if conversation_row is None:
                        raise V12FileReadError("conversation authority changed before publication")
                    interaction_mode = normalize_conversation_mode(
                        str(conversation_row["mode"] or "dialogue")
                    )

                route_mode = f"v12_{plan.route.value}"
                user_metadata = {
                    "answer_mode": f"{route_mode}_request",
                    "private_context_lineage": True,
                    "v12_plan_sha256": plan.canonical_sha256(),
                }
                if evidence.historical_selection is None:
                    user_metadata["conversation_uploaded_raw_ids"] = list(evidence.raw_ids)
                else:
                    user_metadata["conversation_attachment_raw_ids"] = list(evidence.raw_ids)
                    user_metadata["conversation_attachment_uploaders"] = {
                        raw_id: evidence.person_id for raw_id in evidence.raw_ids
                    }
                _require_deadline(deadline, stage="before durable messages")
                store_message_in_transaction(
                    conn,
                    conversation_id,
                    request.actor.own_id,
                    "user",
                    turn.message,
                    metadata=user_metadata,
                )
                assistant_metadata = {
                    "answer_mode": route_mode,
                    "attachment_context_used": True,
                    "attachment_context_expected_count": len(evidence.raw_ids),
                    "attachment_context_readable_count": len(evidence.raw_ids),
                    "attachment_coverage_complete": True,
                    "attachment_verification_complete": True,
                    "citation_check": {
                        "status": "verified",
                        "checked": len(evidence.bundle.citation_labels),
                    },
                    "conversation_attachment_raw_ids": list(evidence.raw_ids),
                    "conversation_attachment_uploaders": {
                        raw_id: evidence.person_id for raw_id in evidence.raw_ids
                    },
                    "evidence_identity_sha256": evidence.identity_sha256,
                    "interaction_mode": interaction_mode,
                    "knowledge_citations": {},
                    "private_context_lineage": True,
                    "tools_used": [],
                    "v12_plan_sha256": plan.canonical_sha256(),
                    "verification": {"status": "verified", "score": 1.0, "issues": []},
                    "verification_status": "verified",
                    "verified": True,
                }
                assistant = store_message_in_transaction(
                    conn,
                    conversation_id,
                    request.actor.own_id,
                    "assistant",
                    answer,
                    metadata=assistant_metadata,
                )
                message_id = str(assistant.get("id") or "")
                if not re.fullmatch(r"msg_[0-9a-f]{16}", message_id):
                    raise V12FileReadError("assistant publication has no durable identity")
                _require_deadline(deadline, stage="before transaction commit")
                publication = (conversation_id, message_id, interaction_mode)
        except BaseException:
            raise
        confirm_staged_request_effect()
        return publication

    async def handle(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> ReadOnlyRouteResult:
        prepared = self._prepared_matches(plan, preparation)
        if prepared is None:
            raise V12FileReadError("file preparation authority is invalid")
        deadline = request.turn_deadline or (time.monotonic() + 60.0)
        if not await self._model.lease_is_current(
            prepared.model_lease,
            prepared.model_requirements,
            absolute_deadline=deadline,
        ):
            raise V12FileReadError("file model authority changed before synthesis")
        answer = await self._synthesize(
            turn,
            plan,
            prepared.evidence.bundle,
            prepared.model_lease,
            prepared.model_requirements,
            deadline=deadline,
        )
        await self._verify(
            turn,
            prepared.evidence.bundle,
            answer,
            prepared.model_lease,
            prepared.model_requirements,
            deadline=deadline,
        )
        if not await self._model.lease_is_current(
            prepared.model_lease,
            prepared.model_requirements,
            absolute_deadline=deadline,
        ):
            raise V12FileReadError("file model authority changed before publication")
        # Publication is deliberately one short, synchronous SQLite critical
        # section.  Once the effect fence is crossed, task cancellation must
        # not detach a worker thread that can commit after the router reports a
        # timeout.  Canary sources are bounded to two small exact-text bodies,
        # so the final byte revalidation remains a bounded local operation.
        conversation_id, message_id, interaction_mode = self._publish_sync(
            request,
            turn,
            plan,
            prepared,
            answer,
            deadline=deadline,
        )
        return ReadOnlyRouteResult(
            message=answer,
            conversation_id=conversation_id,
            message_id=message_id,
            evidence_identity_sha256=prepared.evidence.identity_sha256,
            citation_labels=prepared.evidence.bundle.citation_labels,
            verified=True,
            message_format="markdown",
            interaction_mode=interaction_mode,
        )


__all__ = [
    "V12FileReadError",
    "V12FileReadHandler",
    "V12_FILE_SYNTHESIS_SYSTEM",
    "V12_FILE_VERIFIER_SCHEMA",
    "V12_FILE_VERIFIER_SYSTEM",
]
