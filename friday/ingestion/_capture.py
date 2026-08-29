"""Ingestion: capturing text and replaying an already-seen source.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager

from friday.ingestion._base import (
    Any,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    PipelineShared,
    RawObject,
    _json_dict,
    hashlib,
    new_id,
)
from friday.orchestration.turn_context import AuthenticatedTurnContext, IngressKind, TurnContextError
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context

AUTHENTICATED_TURN_INGESTION_METADATA_KEY = "authenticated_turn_ingestion"
AUTHENTICATED_TURN_INGESTION_SCHEMA = "friday.authenticated-turn-ingestion.v1"


def _authenticated_turn_ingestion_metadata(
    metadata: dict[str, Any] | None,
    *,
    context: AuthenticatedTurnContext | None,
    user_id: str,
    content: str,
    source: str,
    source_ref: str,
) -> dict[str, Any] | None:
    if metadata is not None and AUTHENTICATED_TURN_INGESTION_METADATA_KEY in metadata:
        raise TurnContextError("authenticated turn ingestion metadata is reserved")
    if context is None:
        return metadata
    if context.authority.tenant_id != user_id:
        raise TurnContextError("authenticated turn ingestion tenant drifted")
    ingress_source = "telegram" if context.authority.ingress_kind is IngressKind.TELEGRAM else "api"
    ingress_reference_matches = (
        source_ref == context.authority.update_id
        if context.authority.ingress_kind is IngressKind.SIGNED_HTTP
        else source_ref == f"telegram-update:{context.authority.update_id}"
        or re.fullmatch(
            rf"telegram-album-v2:{re.escape(context.authority.update_id)}:[0-9a-f]{{64}}",
            source_ref,
        )
        is not None
    )
    accepted_ingress = bool(
        source == ingress_source and content == context.model_input.message and ingress_reference_matches
    )
    if source == ingress_source and not accepted_ingress:
        raise TurnContextError("authenticated turn ingestion source identity drifted")
    relation = "accepted_ingress" if accepted_ingress else "derived_effect"
    closed = dict(metadata or {})
    closed[AUTHENTICATED_TURN_INGESTION_METADATA_KEY] = {
        "schema": AUTHENTICATED_TURN_INGESTION_SCHEMA,
        "turn_id": context.turn_id,
        "context_authority_sha256": context.context_authority_sha256,
        "request_effect_binding_sha256": context.effect_fence.request_effect_binding_sha256,
        "relation": relation,
    }
    return closed


class CaptureMixin(PipelineShared):
    def _replay_text_source(self, user_id: str, existing_raw: dict[str, Any]) -> dict[str, Any]:
        existing_ko = self.storage.get_knowledge_by_raw(existing_raw["id"], user_id)
        existing_inbox = self.storage.find_inbox_by_raw(existing_raw["id"], user_id)
        raw_metadata = _json_dict(existing_raw.get("metadata_json"))
        # `_json_dict` and not `.get(..., {})`: the block is provenance written by this
        # pipeline, but a legacy row may hold anything, and a reader that assumes a
        # dict turns a bad row into an unhandled AttributeError on every retry.
        action = str(_json_dict(raw_metadata.get("promotion_assessment")).get("action") or "unknown")
        # A committed ingestion always leaves a terminal artifact: a promote leaves a
        # Knowledge Object, unless strict-review downgraded it to a pending Inbox item.
        # Only the genuine in-progress state (neither artifact yet) is retryable.
        if (action == "promote" and not existing_ko and not existing_inbox) or (
            action == "review" and not existing_inbox
        ):
            raise IdempotencyInProgressError("source_ref is already being promoted by another worker")
        return {
            "idempotent_replay": True,
            "promoted": bool(existing_ko),
            "action": action if existing_ko else "review" if existing_inbox else action,
            "raw_object_id": existing_raw["id"],
            "inbox_id": existing_inbox.get("id") if existing_inbox else None,
            "knowledge_object": existing_ko,
        }

    async def ingest_text(
        self,
        user_id: str,
        content: str,
        *,
        source: str = "telegram",
        source_ref: str = "",
        force_knowledge: bool = False,
        force_review: bool = False,
        metadata: dict[str, Any] | None = None,
        turn_deadline: float | None = None,
        _authenticated_turn_context: AuthenticatedTurnContext | None = None,
    ) -> dict[str, Any]:
        authenticated_context = current_primary_authenticated_turn_context(_authenticated_turn_context)
        if authenticated_context is not None:
            sealed_deadline = (
                authenticated_context.inherited_budget.safety_deadline.monotonic_ns / 1_000_000_000
            )
            if (
                turn_deadline is not None
                and int(turn_deadline * 1_000_000_000)
                != authenticated_context.inherited_budget.safety_deadline.monotonic_ns
            ):
                raise TurnContextError("authenticated turn ingestion deadline drifted")
            turn_deadline = sealed_deadline

        def require_authenticated_commit_authority() -> None:
            if authenticated_context is not None:
                current_primary_authenticated_turn_context(authenticated_context)
            if turn_deadline is not None and time.monotonic() >= turn_deadline:
                raise TimeoutError("request deadline expired before text ingestion commit")

        if turn_deadline is not None and time.monotonic() >= turn_deadline:
            raise TimeoutError("request deadline expired before text ingestion")
        content = (content or "").strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > self.settings.max_extracted_text_chars:
            raise ValueError("text exceeds FRIDAY_MAX_EXTRACTED_TEXT_CHARS")
        metadata = _authenticated_turn_ingestion_metadata(
            metadata,
            context=authenticated_context,
            user_id=user_id,
            content=content,
            source=source,
            source_ref=source_ref,
        )

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if authenticated_context is None:
            self.storage.ensure_user(user_id, source=source)
        existing_raw = (
            self.storage.find_raw_by_source_ref(user_id, source, source_ref) if source_ref else None
        )
        if existing_raw:
            existing_hash = str(existing_raw.get("content_hash") or "")
            if not existing_hash:
                existing_hash = hashlib.sha256(
                    str(existing_raw.get("raw_content") or "").encode("utf-8")
                ).hexdigest()
            if existing_hash != content_hash:
                raise IdempotencyConflictError("source_ref is already bound to different text content")
            require_authenticated_commit_authority()
            return self._replay_text_source(user_id, existing_raw)

        assessment = self.assess_text(content, force_knowledge=force_knowledge)
        assessment = self._apply_feedback_calibration(user_id, assessment)
        if "explicit_no_save" in assessment.penalties:
            # Explicitly private/transient text remains in the conversation layer
            # only. Do not create Raw Object, Inbox, Knowledge Object, entity
            # suggestions, or enrichment traces that would defeat the request.
            require_authenticated_commit_authority()
            return {
                "promoted": False,
                "queued_for_review": False,
                "persisted": False,
                "action": assessment.action,
                "category": assessment.category,
                "confidence": assessment.confidence,
                "promotion_score": assessment.promotion_score,
                "quality_score": assessment.quality_score,
                "reason": assessment.reason,
                "raw_object_id": None,
            }

        enrichment = self._enrich(content, assessment, user_id=user_id)
        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source=source,
            source_ref=source_ref,
            raw_content=content,
            content_type="text",
            content_hash=content_hash,
            metadata_json={
                # The caller's metadata goes FIRST. It used to go last, so anything
                # reaching `POST /api/ingest/text` could replace the pipeline's own
                # `promotion_assessment` — the provenance block `_replay_text_source`
                # reads back to decide whether an ingestion is still in flight. A
                # non-dict value made every later ingest of the same source_ref raise
                # AttributeError, and a forged `{"action": "promote"}` on transient
                # content wedged that source_ref as "in progress" for good.
                **(metadata or {}),
                "promotion_assessment": assessment.to_dict(),
                "classification": assessment.category,
                "classification_confidence": assessment.confidence,
                "classification_reason": assessment.reason,
            },
        )

        # Raw Object, Knowledge Object / Inbox, graph links and version evidence
        # form one logical ingestion unit. Holding a SQLite IMMEDIATE transaction
        # across the unit prevents another process from observing a half-promoted
        # Raw Object and removes the source_ref check-then-insert race.
        require_authenticated_commit_authority()

        @contextmanager
        def authenticated_ingestion_transaction():
            with self.storage.transaction() as conn:
                yield conn
                # This runs for every normal exit, including a return from any
                # branch below, while the complete ingestion unit is still
                # rollback-able and immediately before its outer commit path.
                require_authenticated_commit_authority()

        transaction = (
            authenticated_ingestion_transaction()
            if authenticated_context is not None
            else self.storage.transaction()
        )
        with transaction:
            existing_raw = (
                self.storage.find_raw_by_source_ref(user_id, source, source_ref) if source_ref else None
            )
            if existing_raw:
                existing_hash = str(existing_raw.get("content_hash") or "")
                if not existing_hash:
                    existing_hash = hashlib.sha256(
                        str(existing_raw.get("raw_content") or "").encode("utf-8")
                    ).hexdigest()
                if existing_hash != content_hash:
                    raise IdempotencyConflictError("source_ref is already bound to different text content")
                return self._replay_text_source(user_id, existing_raw)

            raw = self.storage.store_raw_object(raw)
            if assessment.action == "transient":
                return {
                    "promoted": False,
                    "queued_for_review": False,
                    "action": assessment.action,
                    "category": assessment.category,
                    "confidence": assessment.confidence,
                    "promotion_score": assessment.promotion_score,
                    "quality_score": assessment.quality_score,
                    "reason": assessment.reason,
                    "raw_object_id": raw.id,
                }

            if assessment.action == "review":
                review_item = self._store_review_inbox(raw, assessment, enrichment)
                return {
                    "promoted": False,
                    "queued_for_review": True,
                    "action": assessment.action,
                    "category": assessment.category,
                    "confidence": assessment.confidence,
                    "promotion_score": assessment.promotion_score,
                    "quality_score": enrichment.quality_score,
                    "reason": assessment.reason,
                    "raw_object_id": raw.id,
                    "inbox_id": review_item.id,
                    "suggestions": enrichment.to_suggestions(),
                    "extracted_entities": enrichment.entities,
                }

            # «Inbox before canonical»: heuristic auto-promotion of substantial
            # material is downgraded to a pending Inbox suggestion instead of
            # creating a canonical Knowledge Object nobody has seen. Explicit saves
            # (/note, «запомни», force_knowledge) keep their direct promotion — the
            # user already decided. ``force_review`` requests the downgrade per call
            # regardless of intent: a bulk import is one explicit ACTION, but the
            # user has not read the individual items.
            #
            # For text the signals ARE the intent: they come from the person's own
            # words or from a caller that said `force_knowledge`. The file path
            # cannot use them (see `review_required`), which is why the decision
            # itself lives there and only its input is computed here.
            explicit_intent = bool({"manual_promotion", "explicit_save"} & set(assessment.signals))
            if self.review_required(force_review=force_review, explicit_intent=explicit_intent):
                review_item = self._store_review_inbox(raw, assessment, enrichment)
                return {
                    "promoted": False,
                    "queued_for_review": True,
                    "action": "review",
                    "assessed_action": assessment.action,
                    "strict_review": True,
                    "category": assessment.category,
                    "confidence": assessment.confidence,
                    "promotion_score": assessment.promotion_score,
                    "quality_score": enrichment.quality_score,
                    "reason": assessment.reason,
                    "raw_object_id": raw.id,
                    "inbox_id": review_item.id,
                    "suggestions": enrichment.to_suggestions(),
                    "extracted_entities": enrichment.entities,
                }

            promoted = self._promote_raw(
                raw=raw,
                content=content,
                assessment=assessment,
                enrichment=enrichment,
            )
            return {
                "promoted": True,
                "queued_for_review": not promoted["auto_classified"],
                "action": assessment.action,
                "category": assessment.category,
                "confidence": assessment.confidence,
                "promotion_score": assessment.promotion_score,
                "quality_score": enrichment.quality_score,
                "reason": assessment.reason,
                "raw_object_id": raw.id,
                **promoted,
            }
