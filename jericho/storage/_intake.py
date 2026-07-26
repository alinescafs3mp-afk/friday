"""Storage methods for raw objects and the review-gated Inbox.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    InboxItem,
    InboxStatus,
    RawObject,
    SourceReferenceConflictError,
    StorageShared,
    _json_load,
    enum_value,
    hashlib,
    hmac,
    json,
    sqlite3,
    utc_now,
)


class IntakeMixin(StorageShared):
    def _raw_from_row(self, row: sqlite3.Row | dict[str, Any]) -> RawObject:
        data = dict(row)
        return RawObject(
            id=data["id"],
            user_id=data["user_id"],
            source=data["source"],
            source_ref=data.get("source_ref", ""),
            raw_content=data.get("raw_content", ""),
            content_type=data.get("content_type", "text"),
            metadata_json=_json_load(data.get("metadata_json"), {}),
            content_hash=data.get("content_hash", ""),
            version=int(data.get("version", 1)),
            received_at=data.get("received_at") or utc_now(),
            created_at=data.get("created_at") or utc_now(),
            deleted_at=data.get("deleted_at"),
        )

    def find_raw_by_source_ref(self, user_id: str, source: str, source_ref: str) -> dict[str, Any] | None:
        if not source_ref:
            return None
        row = self.execute(
            "SELECT * FROM raw_objects WHERE user_id=? AND source=? AND source_ref=? AND deleted_at IS NULL",
            (user_id, source, source_ref),
        ).fetchone()
        return dict(row) if row else None

    def store_raw_object(self, obj: RawObject) -> RawObject:
        self.ensure_user(obj.user_id)
        if not obj.content_hash:
            obj.content_hash = hashlib.sha256(obj.raw_content.encode("utf-8", errors="replace")).hexdigest()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO raw_objects(id, user_id, source, source_ref, raw_content,
                       content_type, metadata_json, content_hash, version, received_at, created_at, deleted_at)
                       VALUES(:id, :user_id, :source, :source_ref, :raw_content,
                       :content_type, :metadata_json, :content_hash, :version, :received_at, :created_at, :deleted_at)""",
                    obj.to_row(),
                )
            return obj
        except sqlite3.IntegrityError:
            existing = self.find_raw_by_source_ref(obj.user_id, obj.source, obj.source_ref)
            if existing:
                existing_hash = str(existing.get("content_hash") or "")
                if (
                    obj.content_hash
                    and existing_hash
                    and not hmac.compare_digest(obj.content_hash, existing_hash)
                ):
                    raise SourceReferenceConflictError(
                        "source_ref is already bound to different content"
                    ) from None
                return self._raw_from_row(existing)
            raise

    def get_raw_object(self, raw_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM raw_objects WHERE id=?", (raw_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM raw_objects WHERE id=? AND user_id=?", (raw_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def store_inbox_item(self, item: InboxItem) -> InboxItem:
        self.ensure_user(item.user_id)
        raw = self.get_raw_object(item.raw_object_id, item.user_id)
        if not raw:
            raise ValueError("Inbox item requires a RawObject owned by the same user")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
                   VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
                item.to_row(),
            )
        return item

    def get_inbox_item(self, inbox_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM inbox WHERE id=? AND user_id=?", (inbox_id, user_id)).fetchone()
        return dict(row) if row else None

    def get_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM inbox WHERE raw_object_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1",
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def find_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT * FROM inbox WHERE raw_object_id=? AND user_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_inbox(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.execute(
                """SELECT * FROM inbox WHERE user_id=? AND status=?
                   ORDER BY created_at DESC LIMIT ? OFFSET ?""",
                (user_id, enum_value(status), max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM inbox WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_inbox_detailed(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["i.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            clauses.append("i.status=?")
            params.append(enum_value(status))
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; all values remain bound parameters.
        query = f"""SELECT i.*, r.source, r.source_ref, r.raw_content, r.content_type AS raw_content_type,
                       r.metadata_json AS raw_metadata_json, r.received_at,
                       k.title AS knowledge_title, k.summary AS knowledge_summary,
                       k.knowledge_kind, k.importance, k.quality_score AS knowledge_quality_score,
                       k.promotion_score AS knowledge_promotion_score, k.lifecycle_stage
                FROM inbox i
                JOIN raw_objects r ON r.id=i.raw_object_id AND r.user_id=i.user_id
                LEFT JOIN knowledge_objects k
                  ON k.id=i.knowledge_object_id AND k.user_id=i.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END,
                         i.promotion_score DESC, i.created_at DESC
                LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_inbox_status(
        self,
        inbox_id: str,
        status: InboxStatus,
        reviewed_by: str | None = None,
        *,
        user_id: str | None = None,
        suggested_entity_id: str | None = None,
        suggested_tags: list[str] | None = None,
        suggestions: dict[str, Any] | None = None,
        suggested_action: str | None = None,
        knowledge_object_id: str | None = None,
        clear_knowledge_object_id: bool = False,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        notes: str | None = None,
    ) -> bool:
        updates = ["status=?", "reviewed_at=?", "reviewed_by=?"]
        values: list[Any] = [enum_value(status), utc_now(), reviewed_by]
        if suggested_entity_id is not None:
            updates.append("suggested_entity_id=?")
            values.append(suggested_entity_id)
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggestions is not None:
            updates.append("suggestions_json=?")
            values.append(json.dumps(suggestions, ensure_ascii=False, sort_keys=True))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action)[:32])
        if clear_knowledge_object_id:
            updates.append("knowledge_object_id=NULL")
        elif knowledge_object_id is not None:
            updates.append("knowledge_object_id=?")
            values.append(knowledge_object_id)
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if notes is not None:
            updates.append("classification_notes=?")
            values.append(notes)
        # Assignment fragments are selected from fixed fields in this method.
        query = f"UPDATE inbox SET {', '.join(updates)} WHERE id=?"  # nosec B608
        values.append(inbox_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
        with self.transaction() as conn:
            cursor = conn.execute(query, tuple(values))
        return cursor.rowcount > 0

    def claim_inbox_promotion(self, inbox_id: str, user_id: str, knowledge_object_id: str) -> bool:
        """Atomically reserve an Inbox item for promotion.

        Sets ``knowledge_object_id`` only if the item still has none, so exactly
        one of several concurrent approvals wins; the losers get ``False`` and
        must NOT create a second canonical Knowledge Object from one Raw Object
        (the "Inbox before canonical, exactly once" invariant).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbox SET knowledge_object_id=? "
                "WHERE id=? AND user_id=? AND knowledge_object_id IS NULL",
                (knowledge_object_id, inbox_id, user_id),
            )
        return cursor.rowcount == 1

    def update_inbox_suggestions(
        self,
        inbox_id: str,
        user_id: str,
        *,
        suggestions: dict[str, Any],
        suggested_tags: list[str] | None = None,
        suggested_action: str | None = None,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        classification_notes: str | None = None,
    ) -> bool:
        """Refresh machine-generated Inbox advice without marking it human-reviewed.

        Background enrichment must not change ``status``, ``reviewed_at``, or
        ``reviewed_by``.  Keeping this operation separate from
        :meth:`update_inbox_status` makes that policy explicit and prevents a
        model-generated suggestion from looking like an administrator decision.
        """

        updates = ["suggestions_json=?"]
        values: list[Any] = [json.dumps(suggestions, ensure_ascii=False, sort_keys=True)]
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action).strip().casefold()[:32] or "review")
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if classification_notes is not None:
            updates.append("classification_notes=?")
            values.append(str(classification_notes)[:4000])
        values.extend([inbox_id, user_id])
        with self.transaction() as conn:
            # Assignment fragments are selected from fixed fields in this method.
            cursor = conn.execute(
                f"UPDATE inbox SET {', '.join(updates)} WHERE id=? AND user_id=?",  # nosec B608
                tuple(values),
            )
        return cursor.rowcount > 0
