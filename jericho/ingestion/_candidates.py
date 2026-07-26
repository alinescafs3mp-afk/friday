"""Ingestion: queueing agent-proposed candidates for review.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.ingestion._base import (
    Any,
    EntityType,
    IdempotencyConflictError,
    PipelineShared,
    RawObject,
    _bounded_text,
    _coerce_score,
    hashlib,
    new_id,
    replace,
)


class CandidatesMixin(PipelineShared):
    async def queue_agent_candidate(
        self,
        user_id: str,
        content: str,
        *,
        source_ref: str,
        candidate_type: str,
        metadata: dict[str, Any] | None = None,
        suggestion_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue an agent-produced work product for explicit Inbox review.

        This is the single safe write boundary for model-authored synthesis.
        Research, knowledge-work summaries, memory proposals, and entity
        proposals may be useful, but they remain interpretations until a human
        promotes them.  No Knowledge Object or confirmed graph record is
        created here.
        """

        candidate_type = str(candidate_type or "").strip().casefold().replace("-", "_")
        policies = {
            "research": (
                "research",
                "research synthesis requires explicit review before long-term storage",
            ),
            "knowledge_work": (
                "knowledge_work",
                "knowledge-work result requires explicit review before long-term storage",
            ),
            "memory": (
                "agent_tool",
                "agent memory proposal requires explicit review before long-term storage",
            ),
            "entity": (
                "agent_tool",
                "agent entity proposal requires explicit review before graph mutation",
            ),
        }
        if candidate_type not in policies:
            raise ValueError("candidate_type must be research, knowledge_work, memory, or entity")
        source, review_reason = policies[candidate_type]
        content = (content or "").strip()
        if not content:
            raise ValueError(f"{candidate_type} content is required")
        if len(content) > self.settings.max_extracted_text_chars:
            raise ValueError(f"{candidate_type} content exceeds JERICHO_MAX_EXTRACTED_TEXT_CHARS")
        source_ref = str(source_ref or "").strip()[:500]
        if not source_ref:
            raise ValueError("source_ref is required for agent candidates")

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.storage.ensure_user(user_id, source=source)
        existing = self.storage.find_raw_by_source_ref(user_id, source, source_ref)
        if existing:
            existing_digest = str(existing.get("content_hash") or "")
            if existing_digest and existing_digest != digest:
                raise IdempotencyConflictError(
                    "source_ref is already bound to different agent-candidate content"
                )
            inbox = self.storage.find_inbox_by_raw(str(existing["id"]), user_id)
            return {
                "idempotent_replay": True,
                "promoted": False,
                "queued_for_review": bool(inbox),
                "action": "review",
                "candidate_type": candidate_type,
                "raw_object_id": existing["id"],
                "inbox_id": inbox.get("id") if inbox else None,
            }

        baseline = self.assess_text(content, force_knowledge=True)
        assessment = replace(
            baseline,
            action="review",
            confidence=min(0.9, baseline.confidence),
            promotion_score=min(0.78, baseline.promotion_score),
            reason=review_reason,
            signals=[*baseline.signals, f"{candidate_type}_candidate"],
            penalties=[*baseline.penalties, "agent_review_boundary"],
        )
        enrichment = self._enrich(content, assessment, user_id=user_id)
        overrides = dict(suggestion_overrides or {})
        if overrides:
            title = _bounded_text(overrides.get("title"), 200) or enrichment.title
            summary = _bounded_text(overrides.get("summary"), 2_000) or enrichment.summary
            tags_value = overrides.get("tags")
            tags = enrichment.tags
            if isinstance(tags_value, list):
                tags = list(
                    dict.fromkeys(
                        _bounded_text(item, 64).casefold() for item in tags_value if _bounded_text(item, 64)
                    )
                )[:16]
            knowledge_kind = _bounded_text(overrides.get("knowledge_kind"), 80) or enrichment.knowledge_kind
            entities = enrichment.entities
            if isinstance(overrides.get("entities"), list):
                valid_types = {item.value for item in EntityType}
                proposed_entities: list[dict[str, Any]] = []
                for candidate in overrides["entities"][:30]:
                    if not isinstance(candidate, dict):
                        continue
                    name = _bounded_text(candidate.get("name"), 160)
                    entity_type = str(candidate.get("entity_type") or EntityType.OTHER.value).casefold()
                    if not name or entity_type not in valid_types:
                        continue
                    proposed_entities.append(
                        {
                            "name": name,
                            "entity_type": entity_type,
                            "confidence": min(
                                0.79,
                                _coerce_score(candidate.get("confidence"), default=0.65),
                            ),
                            "method": "agent_proposal",
                            "evidence": _bounded_text(candidate.get("evidence"), 500)
                            or "agent-authored proposal; requires review",
                        }
                    )
                if proposed_entities:
                    entities = proposed_entities
            enrichment = replace(
                enrichment,
                title=title,
                summary=summary,
                tags=tags,
                importance=_coerce_score(overrides.get("importance"), default=enrichment.importance),
                knowledge_kind=knowledge_kind,
                entities=entities,
                metadata={
                    **enrichment.metadata,
                    "agent_candidate": {
                        "type": candidate_type,
                        "review_only": True,
                    },
                },
            )

        raw = RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source=source,
            source_ref=source_ref,
            raw_content=content,
            content_type="text",
            content_hash=digest,
            metadata_json={
                **(metadata or {}),
                "promotion_assessment": assessment.to_dict(),
                "agent_candidate": True,
                "candidate_type": candidate_type,
                "review_only": True,
            },
        )
        with self.storage.transaction():
            existing = self.storage.find_raw_by_source_ref(user_id, source, source_ref)
            if existing:
                if str(existing.get("content_hash") or "") not in {"", digest}:
                    raise IdempotencyConflictError(
                        "source_ref is already bound to different agent-candidate content"
                    )
                existing_inbox = self.storage.find_inbox_by_raw(str(existing["id"]), user_id)
                return {
                    "idempotent_replay": True,
                    "promoted": False,
                    "queued_for_review": bool(existing_inbox),
                    "action": "review",
                    "candidate_type": candidate_type,
                    "raw_object_id": existing["id"],
                    "inbox_id": existing_inbox.get("id") if existing_inbox else None,
                }
            raw = self.storage.store_raw_object(raw)
            review_item = self._store_review_inbox(raw, assessment, enrichment)
        return {
            "idempotent_replay": False,
            "promoted": False,
            "queued_for_review": True,
            "action": "review",
            "candidate_type": candidate_type,
            "reason": assessment.reason,
            "raw_object_id": raw.id,
            "inbox_id": review_item.id,
            "suggestions": enrichment.to_suggestions(),
        }

    async def queue_research_candidate(
        self,
        user_id: str,
        content: str,
        *,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the review-only research boundary."""

        return await self.queue_agent_candidate(
            user_id,
            content,
            source_ref=source_ref,
            candidate_type="research",
            metadata=metadata,
        )

    async def queue_knowledge_work_candidate(
        self,
        user_id: str,
        content: str,
        *,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue a knowledge-work result without silently modifying memory."""

        return await self.queue_agent_candidate(
            user_id,
            content,
            source_ref=source_ref,
            candidate_type="knowledge_work",
            metadata=metadata,
        )
