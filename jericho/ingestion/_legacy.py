"""Ingestion: quality scans and cleanup of historical knowledge.

Moved verbatim out of the single 3564-line module: same names, signatures and
bodies. Mixed back into ``IngestionPipeline``, so every collaborator resolves
exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.ingestion._base import (
    _QUESTION_START,
    Any,
    LifecycleStage,
    PipelineShared,
    _clamp,
    _json_dict,
    utc_now,
)


class LegacyMixin(PipelineShared):
    def assess_existing_knowledge(
        self,
        user_id: str,
        knowledge: dict[str, Any] | str,
        *,
        threshold: float = 0.55,
        include_suggestion: bool = False,
    ) -> dict[str, Any]:
        """Conservatively assess whether an existing object looks like legacy chatter.

        The assessment is read-only and explainable.  User-reviewed objects and files are
        protected from heuristic cleanup unless an administrator explicitly acts on them.
        """

        current = (
            self.storage.get_knowledge_object(knowledge, user_id) if isinstance(knowledge, str) else knowledge
        )
        if not current or str(current.get("user_id") or "") != user_id:
            raise ValueError("Knowledge Object not found")

        content = str(current.get("content") or "")
        title = str(current.get("title") or "")
        metadata = _json_dict(current.get("metadata_json"))
        assessment = self.assess_text(content)
        stored_quality = _clamp(float(current.get("quality_score", 0.5) or 0.5))
        stored_promotion = _clamp(float(current.get("promotion_score", 0.5) or 0.5))
        protected_reasons: list[str] = []
        if str(current.get("content_type") or "") == "file":
            protected_reasons.append("file_object")
        if metadata.get("manually_promoted_from_inbox"):
            protected_reasons.append("manually_promoted")
        legacy_cleanup = metadata.get("legacy_cleanup")
        if isinstance(legacy_cleanup, dict) and legacy_cleanup.get("reviewed"):
            protected_reasons.append("previously_reviewed")

        reasons: list[str] = []
        risk = 0.0
        if assessment.action == "transient":
            risk += 0.64
            reasons.append(f"fresh_policy_{assessment.category}")
        elif assessment.action == "review":
            risk += 0.23
            reasons.append("fresh_policy_requires_review")
        if _QUESTION_START.search(content.strip()) or content.rstrip().endswith("?"):
            risk += 0.18
            reasons.append("question_like_content")
        if title.rstrip().endswith("?"):
            risk += 0.12
            reasons.append("question_title")
        if len(content.split()) < 6:
            risk += 0.08
            reasons.append("very_short")
        if stored_quality < 0.35:
            risk += min(0.18, (0.35 - stored_quality) * 0.65)
            reasons.append("low_stored_quality")
        if stored_promotion < 0.35:
            risk += min(0.18, (0.35 - stored_promotion) * 0.65)
            reasons.append("low_stored_promotion")
        if str(current.get("knowledge_kind") or "") in {"chatter", "question", "command"}:
            risk += 0.20
            reasons.append("transient_knowledge_kind")

        # Human decisions always win over a heuristic.  We still expose the assessment, but the
        # object is not marked as a cleanup candidate without an explicit override.
        protected = bool(protected_reasons)
        if protected:
            risk = min(risk, 0.30)
        risk = _clamp(risk)
        suspect = bool(not protected and risk >= _clamp(threshold))
        if assessment.action == "transient":
            recommended = "return_to_inbox"
        elif assessment.action == "review":
            recommended = "reclassify"
        else:
            recommended = "keep"
        result = {
            "knowledge_object": current,
            "suspect": suspect,
            "risk_score": round(risk, 4),
            "reasons": reasons or ["no_material_quality_risk"],
            "protected": protected,
            "protected_reasons": protected_reasons,
            "recommended_action": recommended,
            "assessment": assessment.to_dict(),
            "stored_quality_score": stored_quality,
            "stored_promotion_score": stored_promotion,
        }
        if include_suggestion:
            result["suggestion"] = self._enrich(
                content,
                assessment,
                user_id=user_id,
            ).to_suggestions()
        return result

    def scan_legacy_quality(
        self,
        user_id: str,
        *,
        limit: int = 250,
        threshold: float = 0.55,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Return likely legacy junk without modifying or auto-archiving anything."""

        output: list[dict[str, Any]] = []
        offset = 0
        hard_limit = max(1, min(limit, 2000))
        while len(output) < hard_limit:
            batch = self.storage.list_knowledge_objects(user_id, limit=500, offset=offset)
            if not batch:
                break
            for item in batch:
                if not include_archived and str(item.get("lifecycle_stage")) != LifecycleStage.ACTIVE.value:
                    continue
                result = self.assess_existing_knowledge(user_id, item, threshold=threshold)
                if result["suspect"]:
                    output.append(result)
                    if len(output) >= hard_limit:
                        break
            offset += len(batch)
            if len(batch) < 500:
                break
        output.sort(
            key=lambda item: (-float(item["risk_score"]), str(item["knowledge_object"].get("updated_at", "")))
        )
        return output[:hard_limit]

    def scan_legacy_low_quality(
        self,
        user_id: str,
        *,
        limit: int = 250,
        threshold: float = 0.48,
    ) -> list[dict[str, Any]]:
        """Find likely legacy chatter without mutating data.

        Recommendations are intentionally conservative.  Files and manually reviewed objects are
        never auto-flagged solely because they are short.
        """

        # Backward-compatible name retained for callers from 0.5.x.
        return self.scan_legacy_quality(user_id, limit=limit, threshold=threshold)

    def apply_legacy_cleanup(
        self,
        user_id: str,
        knowledge_object_id: str,
        *,
        action: str,
        reviewed_by: str,
        reason: str = "legacy quality cleanup",
    ) -> dict[str, Any]:
        current = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not current or (current.get("deleted_at") and action != "restore"):
            raise ValueError("Knowledge Object not found")
        metadata = _json_dict(current.get("metadata_json"))
        cleanup_history = metadata.get("legacy_cleanup_history")
        if not isinstance(cleanup_history, list):
            cleanup_history = []
        cleanup_history.append(
            {
                "action": action,
                "reviewed_by": reviewed_by,
                "reason": reason,
                "at": utc_now(),
                "previous_lifecycle": current.get("lifecycle_stage"),
                "previous_quality_score": current.get("quality_score"),
            }
        )
        metadata["legacy_cleanup"] = {
            "reviewed": True,
            "action": action,
            "reviewed_by": reviewed_by,
            "reason": reason,
            "at": utc_now(),
        }
        metadata["legacy_cleanup_history"] = cleanup_history[-20:]

        if action == "return_to_inbox":
            return self.return_knowledge_to_inbox(
                user_id,
                knowledge_object_id,
                reviewed_by=reviewed_by,
                reason=reason,
            )
        if action == "soft_delete":
            metadata["legacy_cleanup"]["soft_deleted"] = True
            self.storage.update_knowledge_fields(
                knowledge_object_id,
                user_id,
                metadata_json=metadata,
            )
            if not self.storage.soft_delete_knowledge_object(knowledge_object_id, user_id):
                raise ValueError("Knowledge Object could not be soft deleted")
            return {
                "knowledge_object_id": knowledge_object_id,
                "status": "soft_deleted",
            }
        if action == "archive":
            updated = self.storage.update_knowledge_fields(
                knowledge_object_id,
                user_id,
                lifecycle_stage=LifecycleStage.ARCHIVED.value,
                importance=min(float(current.get("importance", 0.5)), 0.1),
                quality_score=min(float(current.get("quality_score", 0.5)), 0.15),
                promotion_score=min(float(current.get("promotion_score", 0.5)), 0.15),
                metadata_json=metadata,
            )
        elif action == "reclassify":
            assessment = self.assess_text(str(current.get("content") or ""))
            if assessment.action == "transient":
                raise ValueError(
                    "Fresh policy still considers this object transient; use return_to_inbox or keep"
                )
            enrichment = self._enrich(str(current.get("content") or ""), assessment, user_id=user_id)
            metadata.update(enrichment.metadata)
            updated = self.storage.update_knowledge_fields(
                knowledge_object_id,
                user_id,
                title=enrichment.title,
                summary=enrichment.summary,
                tags_json=enrichment.tags,
                metadata_json=metadata,
                knowledge_kind=enrichment.knowledge_kind,
                importance=enrichment.importance,
                quality_score=enrichment.quality_score,
                promotion_score=max(assessment.promotion_score, 0.65),
            )
            self._link_entities(
                user_id,
                knowledge_object_id,
                current["raw_object_id"],
                enrichment.entities,
            )
        elif action == "keep":
            metadata["legacy_cleanup"]["kept_as_knowledge"] = True
            updated = self.storage.update_knowledge_fields(
                knowledge_object_id,
                user_id,
                quality_score=max(float(current.get("quality_score", 0.5)), 0.55),
                promotion_score=max(float(current.get("promotion_score", 0.5)), 0.65),
                metadata_json=metadata,
            )
        elif action == "restore":
            updated = self.storage.update_knowledge_fields(
                knowledge_object_id,
                user_id,
                lifecycle_stage=LifecycleStage.ACTIVE.value,
                deleted_at=None,
                metadata_json=metadata,
            )
        else:
            raise ValueError(
                "action must be return_to_inbox, archive, reclassify, keep, soft_delete, or restore"
            )
        if not updated:
            raise ValueError("Knowledge Object update failed")
        return updated
