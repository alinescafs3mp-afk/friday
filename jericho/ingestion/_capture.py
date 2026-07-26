"""Ingestion: capturing text and replaying an already-seen source.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.ingestion._base import (
    Any,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    PipelineShared,
    RawObject,
    _json_dict,
    hashlib,
    new_id,
)


class CaptureMixin(PipelineShared):
    def _replay_text_source(self, user_id: str, existing_raw: dict[str, Any]) -> dict[str, Any]:
        existing_ko = self.storage.get_knowledge_by_raw(existing_raw["id"], user_id)
        existing_inbox = self.storage.find_inbox_by_raw(existing_raw["id"], user_id)
        raw_metadata = _json_dict(existing_raw.get("metadata_json"))
        action = str(raw_metadata.get("promotion_assessment", {}).get("action") or "unknown")
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
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > self.settings.max_extracted_text_chars:
            raise ValueError("text exceeds JERICHO_MAX_EXTRACTED_TEXT_CHARS")

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
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
            return self._replay_text_source(user_id, existing_raw)

        assessment = self.assess_text(content, force_knowledge=force_knowledge)
        assessment = self._apply_feedback_calibration(user_id, assessment)
        if "explicit_no_save" in assessment.penalties:
            # Explicitly private/transient text remains in the conversation layer
            # only. Do not create Raw Object, Inbox, Knowledge Object, entity
            # suggestions, or enrichment traces that would defeat the request.
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
                "promotion_assessment": assessment.to_dict(),
                "classification": assessment.category,
                "classification_confidence": assessment.confidence,
                "classification_reason": assessment.reason,
                **(metadata or {}),
            },
        )

        # Raw Object, Knowledge Object / Inbox, graph links and version evidence
        # form one logical ingestion unit. Holding a SQLite IMMEDIATE transaction
        # across the unit prevents another process from observing a half-promoted
        # Raw Object and removes the source_ref check-then-insert race.
        with self.storage.transaction():
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

            # Strict review honours the prompt's "Inbox before canonical" invariant:
            # heuristic auto-promotion of substantial material is downgraded to a
            # pending Inbox suggestion instead of creating a canonical Knowledge
            # Object without review. Explicit saves (/note, "запомни", force_knowledge)
            # keep their direct promotion — the user already decided.
            # ``force_review`` requests the downgrade per call regardless of intent:
            # bulk imports are an explicit ACTION, but the user has not seen the
            # individual items, so none may become canonical silently.
            explicit_intent = bool({"manual_promotion", "explicit_save"} & set(assessment.signals))
            if force_review or (self.settings.ingestion_strict_review and not explicit_intent):
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
