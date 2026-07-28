"""Storage methods for entities, relations, aliases and resolution candidates.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    Entity,
    EntityResolutionCandidate,
    EntityType,
    Relation,
    RelationType,
    ResolutionStatus,
    StorageShared,
    _json_load,
    _snapshot,
    enum_value,
    json,
    math,
    new_id,
    normalize_entity_name,
    sqlite3,
    suppress,
    utc_now,
)


class GraphMixin(StorageShared):
    def list_part_of_relations(self, user_id: str) -> list[dict[str, Any]]:
        """Active PART_OF edges; source is the child, target the parent."""
        rows = self.execute(
            "SELECT source_entity_id, target_entity_id, weight FROM relations"
            " WHERE user_id=? AND relation_type=? AND deleted_at IS NULL"
            " ORDER BY weight DESC",
            (user_id, RelationType.PART_OF.value),
        ).fetchall()
        return [dict(row) for row in rows]

    def _store_entity_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO entity_versions
               (id, user_id, entity_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("entv"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )

    def create_entity(self, entity: Entity) -> Entity:
        self.ensure_user(entity.user_id)
        row = entity.to_row()
        row["normalized_name"] = normalize_entity_name(entity.name)
        if not row["normalized_name"]:
            raise ValueError("Entity name is empty after normalization")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO entities(id, user_id, name, normalized_name, entity_type,
                   aliases_json, description, metadata_json, canonical, merged_into_id, version,
                   created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :name, :normalized_name, :entity_type,
                   :aliases_json, :description, :metadata_json, :canonical, :merged_into_id, :version,
                   :created_at, :updated_at, :deleted_at)""",
                row,
            )
            self._store_entity_version(conn, row)
        return entity

    def update_entity(self, entity: Entity) -> Entity:
        # Same shape as `update_knowledge_object`, same fix: the version is read
        # inside the transaction that writes it. Read-then-lock let two editors both
        # see version 1, both compute 2, and the loser's UPDATE disappear together
        # with its snapshot — `_store_entity_version` is INSERT OR IGNORE on
        # (entity, version), so the duplicate is dropped without a word.
        with self.transaction() as conn:
            existing = self.get_entity(entity.id, entity.user_id)
            if not existing:
                raise ValueError("Entity not found for user")
            entity.version = max(int(existing.get("version", 1)) + 1, int(entity.version))
            entity.updated_at = utc_now()
            row = entity.to_row()
            row["normalized_name"] = normalize_entity_name(entity.name)
            conn.execute(
                """UPDATE entities SET name=:name, normalized_name=:normalized_name,
                   entity_type=:entity_type, aliases_json=:aliases_json, description=:description,
                   metadata_json=:metadata_json, canonical=:canonical, merged_into_id=:merged_into_id,
                   version=:version, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            self._store_entity_version(conn, row)
        return entity

    def get_entity(self, entity_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM entities WHERE id=? AND user_id=?", (entity_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def list_entity_versions(self, entity_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM entity_versions WHERE entity_id=? AND user_id=? ORDER BY version DESC",
            (entity_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _entity_filter(
        self,
        user_id: str,
        entity_type: EntityType | None,
        *,
        include_merged: bool,
    ) -> tuple[str, list[Any]]:
        """The WHERE clause and its parameters, shared by the listing and its count.

        `deleted_at IS NULL AND canonical=1` is the pair most easily lost when a count
        is written by hand: a plain `COUNT(*) FROM entities` also counts tombstones and
        entities merged into another, so a pager built on it would never reach its own
        last page.
        """
        where = "user_id=?"
        params: list[Any] = [user_id]
        if not include_merged:
            where += " AND deleted_at IS NULL AND canonical=1"
        if entity_type:
            where += " AND entity_type=?"
            params.append(enum_value(entity_type))
        return where, params

    def count_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        include_merged: bool = False,
    ) -> int:
        where, params = self._entity_filter(user_id, entity_type, include_merged=include_merged)
        # ``where`` contains only fixed predicates; values remain bound.
        row = self.execute(
            f"SELECT COUNT(*) AS count FROM entities WHERE {where}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        where, params = self._entity_filter(user_id, entity_type, include_merged=include_merged)
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        # `, id` is what makes paging honest: names are not unique — namesakes are
        # normal for entities — and without a unique tail SQLite is free to order a
        # group of equal names differently between two page requests, so rows
        # duplicate on one boundary and vanish on another.
        # ``where`` contains only fixed predicates; values remain bound.
        rows = self.execute(
            f"SELECT * FROM entities WHERE {where} "  # nosec B608
            "ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_entity_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        normalized = normalize_entity_name(name)
        row = self.execute(
            """SELECT * FROM entities WHERE user_id=? AND normalized_name=?
               AND deleted_at IS NULL AND canonical=1 ORDER BY updated_at DESC LIMIT 1""",
            (user_id, normalized),
        ).fetchone()
        return dict(row) if row else None

    def find_entity_by_alias(self, user_id: str, alias: str) -> list[dict[str, Any]]:
        normalized = normalize_entity_name(alias)
        results: list[dict[str, Any]] = []
        for row in self.list_entities(user_id, limit=5000):
            aliases = _json_load(row.get("aliases_json"), [])
            if any(normalize_entity_name(item) == normalized for item in aliases):
                results.append(row)
        return results

    def soft_delete_entity(self, entity_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an entity and record the state as a new entity version.

        Read and write in ONE transaction. `update_entity` overwrites every field
        from the snapshot handed to it, so a read taken before the write lock
        means any merge or edit that commits in between is silently reverted on
        the tombstone — and the reverted state is then stored as a new
        `entity_versions` row, which is the record a reviewer would trust. Same
        shape as the read-modify-write races already fixed in `update_entity` and
        `merge_entities`; `transaction()` is reentrant, so nesting is safe.
        """
        with self.transaction():
            return self._soft_delete_entity_locked(entity_id, user_id)

    def _soft_delete_entity_locked(self, entity_id: str, user_id: str | None) -> bool:
        current = self.get_entity(entity_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        entity = Entity(
            id=str(current["id"]),
            user_id=str(current["user_id"]),
            name=str(current.get("name") or ""),
            entity_type=str(current.get("entity_type") or EntityType.OTHER.value),
            aliases_json=_json_load(current.get("aliases_json"), []),
            description=str(current.get("description") or ""),
            metadata_json=_json_load(current.get("metadata_json"), {}),
            canonical=False,
            merged_into_id=current.get("merged_into_id"),
            version=int(current.get("version", 1)),
            created_at=str(current.get("created_at") or utc_now()),
            updated_at=str(current.get("updated_at") or utc_now()),
            deleted_at=utc_now(),
        )
        self.update_entity(entity)
        return True

    def set_entity_time(
        self,
        entity_id: str,
        user_id: str,
        occurred_at: str,
        *,
        occurred_end: str | None = None,
        precision: str = "day",
        source: str = "",
    ) -> dict[str, Any]:
        """Record (or replace) the temporal anchor of an event entity."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO entity_time(entity_id, user_id, occurred_at, occurred_end,
                   precision, source, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     occurred_at=excluded.occurred_at,
                     occurred_end=excluded.occurred_end,
                     precision=excluded.precision,
                     source=excluded.source,
                     updated_at=excluded.updated_at""",
                (entity_id, user_id, occurred_at, occurred_end, precision, source, utc_now()),
            )
        return self.get_entity_time(entity_id, user_id) or {}

    def get_entity_time(self, entity_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM entity_time WHERE entity_id=? AND user_id=?",
            (entity_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def delete_entity_time(self, entity_id: str, user_id: str | None = None) -> bool:
        clause = " AND user_id=?" if user_id else ""
        params: tuple[Any, ...] = (entity_id, user_id) if user_id else (entity_id,)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"DELETE FROM entity_time WHERE entity_id=?{clause}",  # nosec B608
                params,
            )
        return cursor.rowcount > 0

    def list_events_in_range(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Ordered timeline of dated event entities, optionally bounded to a window.

        Only canonical, non-deleted event entities are returned; a merged or deleted
        event cannot resurface on the timeline through a stale temporal row.
        """
        clauses = [
            "e.user_id=?",
            "e.entity_type='event'",
            "e.canonical=1",
            "e.deleted_at IS NULL",
        ]
        params: list[Any] = [user_id]
        if start:
            clauses.append("t.occurred_at >= ?")
            params.append(start)
        if end:
            clauses.append("t.occurred_at <= ?")
            params.append(end)
        params.append(max(1, min(int(limit), 2000)))
        rows = self.execute(
            "SELECT e.id AS entity_id, e.name AS name, e.entity_type AS entity_type, "
            "e.description AS description, t.occurred_at AS occurred_at, "
            "t.occurred_end AS occurred_end, t.precision AS precision, t.source AS source "
            "FROM entity_time t JOIN entities e ON e.id=t.entity_id "
            f"WHERE {' AND '.join(clauses)} "  # nosec B608
            "ORDER BY t.occurred_at ASC, e.name ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_relation(self, relation: Relation) -> Relation:
        if relation.source_entity_id == relation.target_entity_id:
            raise ValueError("Self-relations are not allowed")
        relation_weight = float(relation.weight)
        if not math.isfinite(relation_weight) or not 0.0 <= relation_weight <= 1.5:
            raise ValueError("Relation weight must be a finite number between 0 and 1.5")
        relation.weight = relation_weight
        source = self.get_entity(relation.source_entity_id, relation.user_id)
        target = self.get_entity(relation.target_entity_id, relation.user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both entities must belong to the same user")
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at)
                       VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                       :relation_type, :weight, :metadata_json, :created_at, :deleted_at)""",
                    relation.to_row(),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """SELECT id FROM relations WHERE user_id=? AND source_entity_id=?
                       AND target_entity_id=? AND relation_type=? AND deleted_at IS NULL""",
                    (
                        relation.user_id,
                        relation.source_entity_id,
                        relation.target_entity_id,
                        enum_value(relation.relation_type),
                    ),
                ).fetchone()
                if row:
                    relation.id = row["id"]
                else:
                    raise
        return relation

    def count_entity_relations(self, entity_id: str, user_id: str | None = None) -> int:
        """Relation count without the two entity joins and the full rows.

        ``search_entities`` asked for this by materialising every relation of every
        returned entity, with both endpoint names, and calling ``len()``.
        """
        params: list[Any] = [entity_id, entity_id]
        user_clause = ""
        if user_id is not None:
            user_clause = " AND user_id=?"
            params.append(user_id)
        # ``user_clause`` is one fixed optional predicate; the value is bound.
        row = self.execute(
            "SELECT COUNT(*) AS count FROM relations "
            "WHERE (source_entity_id=? OR target_entity_id=?)"  # nosec B608
            f"{user_clause} AND deleted_at IS NULL",
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def get_entity_relations(self, entity_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [entity_id, entity_id]
        user_clause = ""
        if user_id is not None:
            user_clause = " AND r.user_id=?"
            params.append(user_id)
        # ``user_clause`` is one fixed optional predicate; the value is bound.
        query = f"""SELECT r.*, s.name AS source_name, t.name AS target_name
                FROM relations r
                JOIN entities s ON s.id=r.source_entity_id
                JOIN entities t ON t.id=r.target_entity_id
                WHERE (r.source_entity_id=? OR r.target_entity_id=?)
                  {user_clause} AND r.deleted_at IS NULL
                ORDER BY r.created_at DESC"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_entity_graph(self, user_id: str, entity_id: str, depth: int = 2) -> dict[str, Any]:
        root = self.get_entity(entity_id, user_id)
        if not root or root.get("deleted_at"):
            return {"nodes": [], "edges": [], "root": entity_id}
        max_depth = max(0, min(depth, 5))
        seen = {entity_id}
        frontier = {entity_id}
        nodes: dict[str, dict[str, Any]] = {entity_id: root}
        edges: dict[str, dict[str, Any]] = {}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for current in frontier:
                for relation in self.get_entity_relations(current, user_id):
                    edges[relation["id"]] = relation
                    for candidate in (relation["source_entity_id"], relation["target_entity_id"]):
                        if candidate in seen:
                            continue
                        entity = self.get_entity(candidate, user_id)
                        if entity and not entity.get("deleted_at"):
                            seen.add(candidate)
                            nodes[candidate] = entity
                            next_frontier.add(candidate)
            frontier = next_frontier
            if not frontier:
                break
        return {"root": entity_id, "nodes": list(nodes.values()), "edges": list(edges.values())}

    def store_relation_candidate(
        self,
        user_id: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        *,
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or refresh a review-only graph relation proposal."""

        if source_entity_id == target_entity_id:
            raise ValueError("Self-relation candidates are not allowed")
        source = self.get_entity(source_entity_id, user_id)
        target = self.get_entity(target_entity_id, user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both candidate entities must belong to the same user")
        relation_type = str(relation_type or "related_to").strip().casefold()
        allowed_types = {item.value for item in RelationType}
        if relation_type not in allowed_types:
            raise ValueError("Unsupported relation type")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        candidate_id = new_id("relc")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO relation_candidates(
                       id, user_id, source_entity_id, target_entity_id, relation_type,
                       confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, source_entity_id, target_entity_id, relation_type) DO UPDATE SET
                     confidence=MAX(relation_candidates.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= relation_candidates.confidence THEN excluded.evidence_json
                       ELSE relation_candidates.evidence_json
                     END,
                     status=CASE
                       WHEN relation_candidates.status IN ('accepted', 'rejected')
                         THEN relation_candidates.status
                       ELSE 'suggested'
                     END""",
                (
                    candidate_id,
                    user_id,
                    source_entity_id,
                    target_entity_id,
                    relation_type,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        row = self.execute(
            """SELECT c.*, s.name AS source_name, t.name AS target_name
               FROM relation_candidates c
               JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
               JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
               WHERE c.user_id=? AND c.source_entity_id=? AND c.target_entity_id=?
                 AND c.relation_type=?""",
            (user_id, source_entity_id, target_entity_id, relation_type),
        ).fetchone()
        return dict(row) if row else {}

    def get_relation_candidate(self, user_id: str, candidate_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT c.*, s.name AS source_name, s.entity_type AS source_type,
                      t.name AS target_name, t.entity_type AS target_type
               FROM relation_candidates c
               JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
               JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
               WHERE c.id=? AND c.user_id=?""",
            (candidate_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    # The two joins are FILTERS, not decoration: they are INNER, and matching
    # `user_id` on both endpoints drops a candidate whose entity belongs to another
    # account or no longer exists. A count that omits them counts rows the page
    # never shows.
    _RELATION_CANDIDATE_FROM = """FROM relation_candidates c
                JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id"""

    @staticmethod
    def _relation_candidate_filter(user_id: str, status: str | None) -> tuple[list[str], list[Any]]:
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in {"suggested", "accepted", "rejected"}:
                raise ValueError("Invalid relation candidate status")
            clauses.append("c.status=?")
            params.append(status)
        return clauses, params

    def count_relation_candidates(self, user_id: str, *, status: str | None = "suggested") -> int:
        clauses, params = self._relation_candidate_filter(user_id, status)
        # ``clauses`` contains only fixed predicates; values remain bound.
        row = self.execute(
            f"SELECT COUNT(*) AS count {self._RELATION_CANDIDATE_FROM} "  # nosec B608
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_relation_candidates(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = self._relation_candidate_filter(user_id, status)
        params.extend([max(1, min(int(limit), 5000)), max(0, offset)])
        # `, c.id` for the same reason as everywhere else here: `created_at` is written
        # to second precision, so one extractor run stamps a whole batch identically.
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT c.*, s.name AS source_name, s.entity_type AS source_type,
                       t.name AS target_name, t.entity_type AS target_type
                {self._RELATION_CANDIDATE_FROM}
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC, c.id LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def review_relation_candidate(
        self,
        user_id: str,
        candidate_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        if status not in {"accepted", "rejected"}:
            raise ValueError("status must be accepted or rejected")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM relation_candidates WHERE id=? AND user_id=?",
                (candidate_id, user_id),
            ).fetchone()
            if not row:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status in {"accepted", "rejected"}:
                if current_status != status:
                    raise ValueError(
                        f"Relation candidate is already {current_status}; reviewed decisions are terminal"
                    )
            else:
                now = utc_now()
                conn.execute(
                    """UPDATE relation_candidates SET status=?, reviewed_at=?, reviewed_by=?
                       WHERE id=? AND user_id=?""",
                    (status, now, reviewed_by, candidate_id, user_id),
                )
                if status == "accepted":
                    relation = Relation(
                        id=new_id("rel"),
                        user_id=user_id,
                        source_entity_id=str(row["source_entity_id"]),
                        target_entity_id=str(row["target_entity_id"]),
                        relation_type=str(row["relation_type"]),
                        weight=max(0.1, min(1.0, float(row["confidence"] or 0.5))),
                        metadata_json={
                            "origin": "review",
                            "source": "reviewed_relation_candidate",
                            "candidate_id": candidate_id,
                            "reviewed_by": reviewed_by,
                            # weight is clamped for ranking; the extractor's raw
                            # confidence stays available as edge provenance.
                            "confidence": float(row["confidence"] or 0.5),
                            "evidence": _json_load(row["evidence_json"], {}),
                        },
                    )
                    # Accepting an already represented relation remains idempotent.
                    with suppress(sqlite3.IntegrityError):
                        conn.execute(
                            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                               relation_type, weight, metadata_json, created_at, deleted_at)
                               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                               :relation_type, :weight, :metadata_json, :created_at, :deleted_at)""",
                            relation.to_row(),
                        )
        return self.get_relation_candidate(user_id, candidate_id)

    def store_resolution_candidate(self, candidate: EntityResolutionCandidate) -> EntityResolutionCandidate:
        if candidate.entity_a_id == candidate.entity_b_id:
            raise ValueError("A resolution candidate must contain two distinct entities")
        left = self.get_entity(candidate.entity_a_id, candidate.user_id)
        right = self.get_entity(candidate.entity_b_id, candidate.user_id)
        if not left or not right:
            raise ValueError("Resolution entities must belong to the same user")
        row = candidate.to_row()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM entity_resolution_candidates WHERE user_id=? AND pair_key=?",
                (candidate.user_id, candidate.pair_key),
            ).fetchone()
            if existing:
                # Rejections and completed decisions are durable.  Pending candidates may receive
                # stronger evidence over time without changing their identity or review state.
                existing_candidate = self._resolution_from_row(existing)
                if str(existing_candidate.status) == ResolutionStatus.SUGGESTED.value and float(
                    candidate.confidence
                ) > float(existing_candidate.confidence):
                    conn.execute(
                        """UPDATE entity_resolution_candidates
                           SET confidence=?, resolution_method=?, evidence_json=?
                           WHERE id=? AND user_id=? AND status='suggested'""",
                        (
                            max(0.0, min(1.0, float(candidate.confidence))),
                            candidate.resolution_method,
                            json.dumps(candidate.evidence_json, ensure_ascii=False, sort_keys=True),
                            existing_candidate.id,
                            candidate.user_id,
                        ),
                    )
                    refreshed = conn.execute(
                        "SELECT * FROM entity_resolution_candidates WHERE id=? AND user_id=?",
                        (existing_candidate.id, candidate.user_id),
                    ).fetchone()
                    return self._resolution_from_row(refreshed) if refreshed else existing_candidate
                return existing_candidate
            conn.execute(
                """INSERT INTO entity_resolution_candidates(id, user_id, entity_a_id, entity_b_id,
                   pair_key, confidence, resolution_method, evidence_json, status, resolved_by,
                   created_at, resolved_at)
                   VALUES(:id, :user_id, :entity_a_id, :entity_b_id, :pair_key, :confidence,
                   :resolution_method, :evidence_json, :status, :resolved_by, :created_at, :resolved_at)""",
                row,
            )
        return candidate

    @staticmethod
    def _resolution_from_row(row: sqlite3.Row | dict[str, Any]) -> EntityResolutionCandidate:
        data = dict(row)
        return EntityResolutionCandidate(
            id=data["id"],
            user_id=data["user_id"],
            entity_a_id=data["entity_a_id"],
            entity_b_id=data["entity_b_id"],
            confidence=float(data.get("confidence", 0.0)),
            resolution_method=data.get("resolution_method", "name_similarity"),
            evidence_json=_json_load(data.get("evidence_json"), {}),
            status=data.get("status", "suggested"),
            resolved_by=data.get("resolved_by"),
            created_at=data.get("created_at", utc_now()),
            resolved_at=data.get("resolved_at"),
        )

    def get_resolution_candidate(self, candidate_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM entity_resolution_candidates WHERE id=? AND user_id=?",
            (candidate_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_resolution_candidates(
        self,
        user_id: str,
        status: ResolutionStatus | None = None,
    ) -> list[dict[str, Any]]:
        if status:
            rows = self.execute(
                """SELECT * FROM entity_resolution_candidates WHERE user_id=? AND status=?
                   ORDER BY confidence DESC, created_at DESC""",
                (user_id, enum_value(status)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM entity_resolution_candidates WHERE user_id=? ORDER BY confidence DESC, created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_candidate(
        self,
        candidate_id: str,
        status: ResolutionStatus,
        resolved_by: str | None = None,
        *,
        user_id: str | None = None,
    ) -> bool:
        query = "UPDATE entity_resolution_candidates SET status=?, resolved_at=?, resolved_by=? WHERE id=?"
        params: tuple[Any, ...] = (
            enum_value(status),
            None if status == ResolutionStatus.SUGGESTED else utc_now(),
            resolved_by,
            candidate_id,
        )
        if user_id is not None:
            query += " AND user_id=?"
            params += (user_id,)
        with self.transaction() as conn:
            cursor = conn.execute(query, params)
        return cursor.rowcount > 0

    def merge_entities(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        *,
        merged_by: str | None = None,
    ) -> dict[str, Any]:
        if source_id == target_id:
            raise ValueError("Cannot merge an entity into itself")
        # Both entities are read INSIDE the transaction that merges them. Reading
        # first and locking afterwards meant two merges into the same target each
        # saw the pre-merge alias set and the pre-merge version: the second UPDATE
        # overwrote the first, so one merge's aliases were dropped and its snapshot
        # silently ignored by INSERT OR IGNORE. A merge moves links and relations,
        # which makes losing half of one considerably worse than losing an edit.
        with self.transaction() as conn:
            source = self.get_entity(source_id, user_id)
            target = self.get_entity(target_id, user_id)
            if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
                raise ValueError("Both canonical entities must belong to the same user")

            source_aliases = _json_load(source.get("aliases_json"), [])
            target_aliases = _json_load(target.get("aliases_json"), [])
            aliases = {
                item.strip()
                for item in [*source_aliases, *target_aliases, source["name"]]
                if item
                and item.strip()
                and normalize_entity_name(item) != normalize_entity_name(target["name"])
            }
            now = utc_now()
            target_after = dict(target)
            target_after["aliases_json"] = json.dumps(sorted(aliases, key=str.casefold), ensure_ascii=False)
            target_after["version"] = int(target.get("version", 1)) + 1
            target_after["updated_at"] = now

            conn.execute(
                """UPDATE entities SET aliases_json=?, version=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (target_after["aliases_json"], target_after["version"], now, target_id, user_id),
            )
            self._store_entity_version(conn, target_after)

            source_links = conn.execute(
                "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            ).fetchall()
            for link_row in source_links:
                link = dict(link_row)
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links
                       (id, user_id, knowledge_object_id, entity_id, status, confidence,
                        evidence_json, created_at, reviewed_at, reviewed_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("kel"),
                        user_id,
                        link["knowledge_object_id"],
                        target_id,
                        link["status"],
                        link["confidence"],
                        link["evidence_json"],
                        link["created_at"],
                        link["reviewed_at"],
                        link["reviewed_by"],
                    ),
                )
            conn.execute(
                "DELETE FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            )
            conn.execute(
                "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE user_id=? AND entity_id=?",
                (target_id, now, user_id, source_id),
            )

            relations = conn.execute(
                """SELECT * FROM relations WHERE user_id=? AND deleted_at IS NULL
                   AND (source_entity_id=? OR target_entity_id=?)""",
                (user_id, source_id, source_id),
            ).fetchall()
            for relation_row in relations:
                relation = dict(relation_row)
                conn.execute("DELETE FROM relations WHERE id=?", (relation["id"],))
                new_source = (
                    target_id if relation["source_entity_id"] == source_id else relation["source_entity_id"]
                )
                new_target = (
                    target_id if relation["target_entity_id"] == source_id else relation["target_entity_id"]
                )
                if new_source == new_target:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        relation["id"],
                        user_id,
                        new_source,
                        new_target,
                        relation["relation_type"],
                        relation["weight"],
                        relation["metadata_json"],
                        relation["created_at"],
                    ),
                )

            conn.execute(
                """UPDATE entities SET merged_into_id=?, canonical=0, deleted_at=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (target_id, now, now, source_id, user_id),
            )
            conn.execute(
                """UPDATE entity_resolution_candidates SET status='merged', resolved_at=?, resolved_by=?
                   WHERE user_id=? AND status='suggested'
                     AND (entity_a_id=? OR entity_b_id=?)""",
                (now, merged_by or user_id, user_id, source_id, source_id),
            )
            conn.execute(
                """INSERT INTO entity_merge_history(id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json, merged_by, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("merge"),
                    user_id,
                    source_id,
                    target_id,
                    _snapshot(source),
                    _snapshot(target),
                    _snapshot(target_after),
                    merged_by or user_id,
                    now,
                ),
            )
        return self.get_entity(target_id, user_id) or {}

    def list_merge_history(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM entity_merge_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]
