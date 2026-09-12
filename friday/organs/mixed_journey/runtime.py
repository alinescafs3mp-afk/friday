"""Run a mixed read within the existing primary turn and storage transaction.

Ingress owns retries and final transport delivery. This seam acquires evidence,
uses the attested two-call comparison and commits the two conversation rows.
It does not own a second task ledger or mark a transport send as successful.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import asdict
from typing import Any

from friday.execution_kernel import (
    confirm_staged_request_effect,
    mark_request_effect_possible,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
)
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PreparedFileEvidence,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.model_input_hygiene import model_visible_text_is_secret_free
from friday.orchestration.mixed_file_archive_web_comparison import (
    MixedFileArchiveWebComparisonError,
    compare_current_file_archive_with_web,
    mixed_file_archive_web_comparison_process_lease_is_current,
    mixed_file_archive_web_request_is_admitted,
)
from friday.orchestration.mixed_file_archive_web_query import mixed_file_archive_web_turn_is_admitted
from friday.orchestration.transient_web_comparison import (
    TransientWebComparisonAdapter,
    TransientWebComparisonError,
    TransientWebEvidenceStatus,
    seal_compare_current_file_public_web_query,
)
from friday.orchestration.turn_context import TurnContextError
from friday.orchestration.turn_context_call_scope import require_current_authenticated_chat_call_scope
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.orchestration.web_research_consumption import WebResearchConsumptionState
from friday.organs.mixed_journey.consume import _chat_dict, _web_consumption
from friday.organs.mixed_journey.observe import MIXED_SOURCE_FACTS, MIXED_SOURCE_FACTS_SCHEMA
from friday.organs.mixed_journey.sources import prepare_mixed_file_archive_sources
from friday.permissions import ActorContext, AuthorizationService
from friday.storage import normalize_conversation_mode
from friday.storage._conversations import create_conversation_in_transaction, store_message_in_transaction
from friday.storage._core import guarded_storage_transaction
from friday.storage.models import new_id

MIXED_ANSWER_MODE = "mixed_file_archive_web"
MIXED_SOURCE_RECEIPT = "mixed_file_archive_web_sources_v1"
_PUBLICATION_RESERVE_SEC = 2.0
_NOT_ADMITTED = "Сравнение файла, архива и веба не принято: нужны один текущий файл, точная архивная ссылка и отдельная публичная тема."
_UNAVAILABLE = (
    "Сравнение сейчас недоступно: доказательства не подготовлены или доступ к источникам изменился."
)


def _source_pin(prepared: PreparedFileEvidence) -> dict[str, str]:
    token = prepared.snapshot_tokens[0]
    return {
        "raw_object_id": token.source.raw_id,
        "source_identity_sha256": token.source.identity_sha256,
        "content_sha256": token.content_sha256,
    }


async def _prepare_sources(**kwargs: Any) -> tuple[PreparedFileEvidence, PreparedFileEvidence]:
    # Keep ownership of this bounded read-only thread until it has closed its
    # file handles. Cancellation must not leave an unobserved child running.
    task = asyncio.create_task(asyncio.to_thread(prepare_mixed_file_archive_sources, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def execute_mixed_file_archive_web_turn(
    *,
    settings: Any,
    storage: Any,
    authorization: AuthorizationService | None,
    model: Any,
    web: Any,
    user_id: str,
    actor: ActorContext,
    message: str,
    conversation_id: str | None,
    attachments: list[dict[str, Any]] | None,
    turn_deadline: float | None,
) -> dict[str, Any]:
    """Acquire real evidence and atomically publish one primary answer."""

    if user_id != actor.own_id:
        raise PermissionError("mixed turn person does not match the authenticated actor")
    if not mixed_file_archive_web_turn_is_admitted(message, attachments=attachments):
        return _chat_dict(message=_NOT_ADMITTED, conversation_id=conversation_id or "")
    if (
        not mixed_file_archive_web_request_is_admitted(message)
        or model is None
        or not isinstance(authorization, AuthorizationService)
        or not callable(getattr(web, "research", None))
        or turn_deadline is None
        or type(turn_deadline) not in {int, float}
        or not math.isfinite(turn_deadline)
    ):
        return _chat_dict(message=_UNAVAILABLE, conversation_id=conversation_id or "")
    assert attachments is not None and turn_deadline is not None
    deadline = float(turn_deadline)
    try:
        parent = current_primary_authenticated_turn_context()
        if parent is not None:
            if parent.authority.actor != actor or parent.authority.conversation_id != conversation_id:
                raise TurnContextError("mixed turn authority changed")
            deadline = min(deadline, parent.inherited_budget.safety_deadline.monotonic_ns / 1e9)
        work_deadline = deadline - _PUBLICATION_RESERVE_SEC

        def require_live(*, publishing: bool = False) -> None:
            current = current_primary_authenticated_turn_context(parent)
            if current is not parent or time.monotonic() >= (deadline if publishing else work_deadline):
                raise TimeoutError("mixed turn deadline or authority expired")
            if parent is not None:
                scope = require_current_authenticated_chat_call_scope(parent)
                if scope.model_input.message != message or len(scope.attachment_carriers) != len(attachments):
                    raise TurnContextError("mixed turn input changed")
                if any(a is not b for a, b in zip(scope.attachment_carriers, attachments, strict=True)):
                    raise TurnContextError("mixed turn attachment authority changed")

        require_live()
        conversation = storage.get_conversation(conversation_id, actor.own_id) if conversation_id else None
        if conversation_id and conversation is None:
            raise FileEvidenceUnavailable("mixed_conversation_unavailable")
        mode = normalize_conversation_mode(str((conversation or {}).get("mode") or "dialogue"))
        if mode != "dialogue":
            raise FileEvidenceUnavailable("mixed_conversation_mode_unavailable")
        plan = seal_compare_current_file_public_web_query(
            current_user_message=message,
            actor=actor,
            conversation_id=conversation_id,
        )
        current, archive = await _prepare_sources(
            storage=storage,
            authorization=authorization,
            files_root=settings.files_dir,
            actor=actor,
            message=message,
            attachments=attachments,
            max_bytes=settings.max_upload_bytes,
            absolute_deadline=work_deadline,
        )
        require_live()
        # Reuse the ingress's durable uncertain fence before replay-sensitive
        # outbound work. A crash must not silently repeat a completed web/model call.
        if not mark_request_effect_possible():
            raise FileEvidenceUnavailable("mixed_effect_fence_unavailable")
        evidence = await TransientWebComparisonAdapter(authorization, web).research(
            plan=plan,
            actor=actor,
            conversation_id=conversation_id,
            current_user_message=message,
            absolute_deadline=work_deadline,
        )
        require_live()
        turn_id = parent.turn_id if parent is not None else str(new_id("turn"))
        consumption = _web_consumption(turn_id, evidence, consumption_id=turn_id)
        if (
            evidence.status is TransientWebEvidenceStatus.SOURCED
            and consumption.usability is WebResearchConsumptionState.UNAVAILABLE
        ):
            raise FileEvidenceUnavailable("mixed_web_provenance_unavailable")
        comparison = await compare_current_file_archive_with_web(
            model,
            request=message,
            accepted_plan_sha256=plan.canonical_sha256(),
            prepared_file=current,
            prepared_archive=archive,
            web_evidence=evidence,
            absolute_deadline=work_deadline,
        )

        def before_commit() -> None:
            require_live(publishing=True)
            if not mixed_file_archive_web_comparison_process_lease_is_current(model, comparison):
                raise FileEvidenceUnavailable("mixed_model_authority_changed")
            if not model_visible_text_is_secret_free(comparison.answer):
                raise FileEvidenceUnavailable("mixed_answer_projection_unavailable")

        before_commit()
        receipt = {
            "current": _source_pin(current),
            "archive": _source_pin(archive),
            "archive_filename": archive.historical_selection.filename if archive.historical_selection else "",
            "answer_sha256": hashlib.sha256(comparison.answer.encode("utf-8")).hexdigest(),
        }
        raw_ids = [*current.raw_ids, *archive.raw_ids]
        source_facts = {
            "schema": MIXED_SOURCE_FACTS_SCHEMA,
            "authenticated_turn_id": turn_id,
            "files": [
                {"file_id": str(current.raw_ids[0]), "sha256": current.snapshot_tokens[0].content_sha256}
            ],
            "archives": [
                {
                    "archive_id": str(archive.raw_ids[0]),
                    "sha256": archive.snapshot_tokens[0].content_sha256,
                    # Exact archive selection contains one authorized revision.
                    "member_count": len(archive.raw_ids),
                }
            ],
        }
        metadata = {
            "answer_mode": MIXED_ANSWER_MODE,
            "message_format": comparison.message_format,
            "interaction_mode": mode,
            "private_context_lineage": True,
            "conversation_attachment_raw_ids": raw_ids,
            "conversation_attachment_uploaders": {raw_id: actor.own_id for raw_id in raw_ids},
            MIXED_SOURCE_RECEIPT: receipt,
            MIXED_SOURCE_FACTS: source_facts,
            "web_research_consumption": asdict(consumption),
            "mixed_source_identity_sha256": comparison.source_evidence_sha256,
            "verification_status": "verified",
            "verified": True,
            "citation_check": {"status": "verified", "checked": len(comparison.citation_labels)},
        }
        with guarded_storage_transaction(
            storage,
            before_commit=before_commit,
            lock_timeout_sec=max(0.0, deadline - time.monotonic()),
            after_commit=confirm_staged_request_effect,
            after_rollback=rollback_staged_request_effect,
        ) as conn:
            for prepared in (current, archive):
                if not reauthorize_prepared_file_evidence_in_transaction(
                    conn,
                    authorization,
                    settings.files_dir,
                    actor,
                    prepared,
                    max_bytes=settings.max_upload_bytes,
                    storage=storage,
                ):
                    raise FileEvidenceUnavailable("mixed_source_authority_changed")
            before_commit()
            if conversation_id is None:
                if parent is not None:
                    raise TurnContextError("authenticated mixed turn cannot create a conversation")
                conversation_id = str(
                    create_conversation_in_transaction(
                        conn,
                        actor.own_id,
                        title=message[:80],
                        mode=mode,
                    )["id"]
                )
            else:
                row = conn.execute(
                    "SELECT mode FROM conversations WHERE id=? AND user_id=?",
                    (conversation_id, actor.own_id),
                ).fetchone()
                if row is None or normalize_conversation_mode(str(row["mode"] or "dialogue")) != mode:
                    raise FileEvidenceUnavailable("mixed_conversation_authority_changed")
            if not stage_request_effect_possible_in_transaction(
                conn,
                expected_request_binding_sha256=(
                    parent.effect_fence.request_effect_binding_sha256 if parent is not None else None
                ),
            ):
                raise FileEvidenceUnavailable("mixed_publication_fence_unavailable")
            user = store_message_in_transaction(
                conn,
                conversation_id,
                actor.own_id,
                "user",
                message,
                metadata={
                    "answer_mode": MIXED_ANSWER_MODE + "_request",
                    "private_context_lineage": True,
                    "conversation_uploaded_raw_ids": list(current.raw_ids),
                },
            )
            assistant = store_message_in_transaction(
                conn,
                conversation_id,
                actor.own_id,
                "assistant",
                comparison.answer,
                metadata=metadata,
                reply_to=str(user["id"]),
            )
        reply = _chat_dict(
            message=comparison.answer,
            message_format=comparison.message_format,
            conversation_id=conversation_id,
            message_id=str(assistant["id"]),
            citations=[{"label": label} for label in comparison.citation_labels],
            web_sources=[
                {"url": str(payload["url"]), "title": str(payload["title"])}
                for source in evidence.sources
                if (payload := source.synthesis_payload())
            ],
            web_evidence_status=evidence.status.value,
            context={
                "interaction_mode": mode,
                "answer_mode": MIXED_ANSWER_MODE,
                "comparison_status": comparison.status.value,
                "partial_reasons": [reason.value for reason in comparison.partial_reasons],
            },
            web_research_consumption=asdict(consumption),
        )
        reply["verified"] = True
        reply[MIXED_SOURCE_FACTS] = source_facts
        return reply
    except (
        FileEvidenceUnavailable,
        MixedFileArchiveWebComparisonError,
        TransientWebComparisonError,
        TurnContextError,
        PermissionError,
        TimeoutError,
    ):
        return _chat_dict(message=_UNAVAILABLE, conversation_id=conversation_id or "")
