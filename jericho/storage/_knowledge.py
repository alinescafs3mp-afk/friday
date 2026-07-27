"""Storage methods for Knowledge Objects, versions, usage and conflicts.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from jericho.storage._base import (
    _SEARCH_TEXT_LEN_SQL,
    LOGGER,
    UTC,
    Any,
    EntityResolutionCandidate,
    KnowledgeObject,
    LifecycleStage,
    SequenceMatcher,
    StorageShared,
    _is_entity_identifier,
    _json_load,
    _snapshot,
    datetime,
    json,
    math,
    new_id,
    normalize_entity_name,
    re,
    sqlite3,
    timedelta,
    utc_now,
)

# FTS MATCH accepts a bounded number of terms, and a natural-language question is
# mostly function words. Twelve is the budget; which twelve is the whole question.
_FTS_TERM_BUDGET = 12


# Below this length a name has too few characters for trigram blocking to be
# meaningful, so short names of the same type are compared against each other
# exhaustively. There are few of them and n² over few is nothing.
_SHORT_NAME_CHARS = 6
# Ceiling on evaluated pairs. Four SequenceMatcher calls per surviving pair put
# this at a few seconds — a scan reachable from an HTTP route needs an answer, not
# an eventual one. Reaching it is a WARNING, never silent.
_MAX_DUPLICATE_PAIRS = 200_000
# Evidence strength of the key that introduced a pair, strongest first. Used only
# to decide what the ceiling drops.
_KEY_RANK = {"variant": 0, "token": 1, "acronym": 2, "short": 3, "bigram": 4}


def _ratio_ceiling(left: str, right: str, left_counts: Counter[str], right_counts: Counter[str]) -> float:
    """Highest ``SequenceMatcher.ratio()`` these two strings can reach. Exact.

    ``ratio()`` is ``2·M/(len(a)+len(b))``, and the matched characters form a common
    subsequence, so ``M`` is bounded twice over: by the shorter string, and by the
    size of the two strings' character multiset intersection. The second bound is
    the one that pays — same-length names defeat the first entirely, while the
    multiset bound prunes them for a third of what one ``SequenceMatcher`` call
    costs. Both are upper bounds, so nothing that could have qualified is skipped.
    """
    total = len(left) + len(right)
    if not total:
        return 0.0
    shared = sum((left_counts & right_counts).values())
    return 2.0 * min(len(left), len(right), shared) / total


def _blocking_keys(entity_type: str, variants: Sequence[str]) -> set[tuple[str, ...]]:
    """Cheap keys such that any pair that could score ≥ ~0.5 shares at least one.

    Derived from the scoring below rather than guessed. Ignoring the ≤0.14 context
    boost, which cannot carry a pair on its own, a candidate needs one of:

    * a shared normalized variant           → ``exact_alias`` (0.995)
    * an identical token set                → 0.94, and it implies a shared token
    * ``token_jaccard ≥ 0.40``              → a shared token
    * matching acronyms                     → 0.82
    * ``name_similarity ≥ 0.51`` and friends → half the shorter name's characters
      match in order, which for a name of six characters or more forces a shared
      character trigram.

    The last one is the only approximation, and it is bounded: names under
    ``_SHORT_NAME_CHARS`` skip trigrams and land in one exhaustive per-type bucket.
    """
    keys: set[tuple[str, ...]] = set()
    for variant in variants:
        keys.add(("variant", entity_type, variant))
        for token in variant.split():
            keys.add(("token", entity_type, token))
    name = variants[0] if variants else ""
    tokens = [token for token in name.split() if token]
    if len(tokens) >= 2:
        keys.add(("acronym", entity_type, "".join(token[0] for token in tokens).casefold()))
    compact = name.replace(" ", "")
    if len(compact) < _SHORT_NAME_CHARS:
        # Too few characters for an n-gram to mean anything; these all meet.
        keys.add(("short", entity_type))
    for offset in range(len(compact) - 1):
        # Bigrams, not trigrams — measured, not assumed. Trigrams lost 2% of the
        # exhaustive scan's proposals: «Орион» vs «Орион2 1» (short and long names
        # landed in disjoint bucket families) and «ООО 24» vs «ОСОО 40» (similar
        # strings sharing no three consecutive characters). Bigrams recover every
        # one of those, and short names keep their exhaustive bucket *as well as*
        # their bigrams so they still meet longer neighbours.
        keys.add(("bigram", entity_type, compact[offset : offset + 2]))
    return keys


def _valid_lifecycle_stage(value: Any) -> str:
    """Reject a lifecycle stage that is not one of the four.

    The DDL constrains importance, quality_score and promotion_score with CHECK, but
    not this column, and ``update_knowledge_fields`` passed whatever PATCH supplied
    straight through. Both ``"Active"`` (wrong case) and ``"totally-bogus"``
    persisted: ``get_lifecycle_stats`` then reported a stage nobody defined, and the
    object matched no lifecycle filter, so it fell out of every governance scan while
    still answering searches. A typo in one request quietly removed an object from
    oversight.
    """
    stage = str(getattr(value, "value", value) or "").strip().casefold()
    allowed = {item.value for item in LifecycleStage}
    if stage not in allowed:
        raise ValueError(f"lifecycle_stage must be one of {sorted(allowed)}")
    return stage


def _fts_terms(text: str) -> list[str]:
    """Spend the term budget on the words that select a document, not the first typed.

    The rule was ``re.findall(...)[:12]`` over the raw text, so a question longer
    than twelve tokens lost its tail — and a Russian question front-loads «как»,
    «почему», «в», «на», words every document contains, while the identifier that
    actually names the answer comes last. A 14-term question containing
    ``autovacuum_vacuum_scale_factor`` never got that term to the index at all.

    Stopwords are dropped **only when the query is over budget**, and that
    restraint is measured, not stylistic: dropping them unconditionally moved
    ``tools/retrieval_bench.py`` from **0.583 to 0.458** (paraphrase 0.50→0.17,
    synonym 0.40→0.20). At this stage FTS is a recall stage, and for a paraphrase
    the common words are the *only* lexical bridge to the document. So a query
    within budget keeps every token it had; only one that must lose something
    loses the cheap words instead of the specific one. Text order is preserved —
    reordering by length scored the same 0.458 and buys nothing.

    Tokenisation goes through ``retrieval.tokens_of``: the fifth site still
    rolling its own regex, and the one that made a sentence-final identifier
    (``…scale_factor.``) a different string from the same identifier in a query.
    """
    from jericho.retrieval import _STOPWORDS, tokens_of

    unique = list(dict.fromkeys(token for token in tokens_of(text) if len(token) >= 2))
    if len(unique) <= _FTS_TERM_BUDGET:
        return unique
    chosen = [token for token in unique if token.casefold() not in _STOPWORDS][:_FTS_TERM_BUDGET]
    if len(chosen) < _FTS_TERM_BUDGET:
        # A long query that is mostly stopwords still gets a full budget.
        taken = set(chosen)
        chosen += [token for token in unique if token not in taken][: _FTS_TERM_BUDGET - len(chosen)]
    return chosen


class KnowledgeMixin(StorageShared):
    def get_knowledge_by_raw(self, raw_id: str, user_id: str) -> dict[str, Any] | None:
        # The LIVE Knowledge Object for a Raw Object: soft-deleted rows (e.g. a
        # promotion-race loser's orphan, or an ignored KO) are excluded so callers
        # never adopt a hidden object as the canonical one.
        row = self.execute(
            "SELECT * FROM knowledge_objects "
            "WHERE raw_object_id=? AND user_id=? AND deleted_at IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (raw_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _ko_snapshot(obj: KnowledgeObject | dict[str, Any]) -> dict[str, Any]:
        return obj.to_row() if isinstance(obj, KnowledgeObject) else dict(obj)

    def _store_ko_version(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO knowledge_object_versions
               (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                new_id("kov"),
                row["user_id"],
                row["id"],
                int(row.get("version", 1)),
                _snapshot(row),
                utc_now(),
            ),
        )

    def store_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        self.ensure_user(obj.user_id)
        raw = self.get_raw_object(obj.raw_object_id, obj.user_id)
        if not raw:
            raise ValueError("KnowledgeObject requires a RawObject owned by the same user")
        row = obj.to_row()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_objects(id, user_id, raw_object_id, entity_id, content,
                   content_type, title, summary, tags_json, metadata_json, knowledge_kind,
                   importance, quality_score, promotion_score, lifecycle_stage, version,
                   superseded_by_id, created_at, updated_at, deleted_at)
                   VALUES(:id, :user_id, :raw_object_id, :entity_id, :content,
                   :content_type, :title, :summary, :tags_json, :metadata_json, :knowledge_kind,
                   :importance, :quality_score, :promotion_score, :lifecycle_stage, :version,
                   :superseded_by_id, :created_at, :updated_at, :deleted_at)""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_object(self, obj: KnowledgeObject) -> KnowledgeObject:
        # The version is READ INSIDE the transaction that writes it. Reading first
        # and locking afterwards let two editors both see version 1, both compute 2,
        # and the loser's UPDATE vanish — along with its snapshot, since
        # `_store_ko_version` is INSERT OR IGNORE on (object, version).
        with self.transaction() as conn:
            existing = self.get_knowledge_object(obj.id, obj.user_id)
            if not existing:
                raise ValueError("Knowledge object not found for user")
            obj.version = max(int(existing.get("version", 1)) + 1, int(obj.version))
            obj.updated_at = utc_now()
            row = obj.to_row()
            conn.execute(
                """UPDATE knowledge_objects SET entity_id=:entity_id, content=:content,
                   content_type=:content_type, title=:title, summary=:summary, tags_json=:tags_json,
                   metadata_json=:metadata_json, knowledge_kind=:knowledge_kind,
                   importance=:importance, quality_score=:quality_score,
                   promotion_score=:promotion_score, lifecycle_stage=:lifecycle_stage, version=:version,
                   superseded_by_id=:superseded_by_id, updated_at=:updated_at, deleted_at=:deleted_at
                   WHERE id=:id AND user_id=:user_id""",
                row,
            )
            self._store_ko_version(conn, row)
        return obj

    def update_knowledge_fields(self, ko_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
        """Merge ``fields`` into a Knowledge Object and version the result.

        Read, merge and write happen inside ONE transaction. They used to be three
        separate steps with the lock taken only for the last one, which is a
        read-modify-write race in the plainest form: two editors both read version 1,
        both compute 2, and the second UPDATE overwrites the first. The snapshot is
        lost with it, because ``_store_ko_version`` is ``INSERT OR IGNORE`` on
        ``(knowledge_object_id, version)`` and the duplicate version is dropped in
        silence. Reproduced with six concurrent edits: final version **3 instead of
        7**, three snapshots instead of seven, no error raised anywhere — four edits
        and their history simply gone.

        ``transaction()`` is reentrant on the same thread, so the nested
        ``update_knowledge_object`` below does not deadlock.
        """
        with self.transaction():
            current = self.get_knowledge_object(ko_id, user_id)
            if not current:
                return None
            tags = fields.get("tags_json", _json_load(current.get("tags_json"), []))
            metadata = fields.get("metadata_json", _json_load(current.get("metadata_json"), {}))
            obj = KnowledgeObject(
                id=current["id"],
                user_id=current["user_id"],
                raw_object_id=current["raw_object_id"],
                entity_id=fields.get("entity_id", current.get("entity_id")),
                content=fields.get("content", current.get("content", "")),
                content_type=fields.get("content_type", current.get("content_type", "")),
                title=fields.get("title", current.get("title", "")),
                summary=fields.get("summary", current.get("summary", "")),
                tags_json=tags if isinstance(tags, list) else _json_load(tags, []),
                metadata_json=metadata if isinstance(metadata, dict) else _json_load(metadata, {}),
                knowledge_kind=str(fields.get("knowledge_kind", current.get("knowledge_kind", "note"))),
                importance=float(fields.get("importance", current.get("importance", 0.5))),
                quality_score=float(fields.get("quality_score", current.get("quality_score", 0.5))),
                promotion_score=float(fields.get("promotion_score", current.get("promotion_score", 0.5))),
                lifecycle_stage=_valid_lifecycle_stage(
                    fields.get("lifecycle_stage", current.get("lifecycle_stage", "active"))
                ),
                version=int(current.get("version", 1)),
                superseded_by_id=fields.get("superseded_by_id", current.get("superseded_by_id")),
                created_at=current.get("created_at", utc_now()),
                updated_at=current.get("updated_at", utc_now()),
                deleted_at=fields.get("deleted_at", current.get("deleted_at")),
            )
            self.update_knowledge_object(obj)
            return self.get_knowledge_object(ko_id, user_id)

    def get_knowledge_object(self, ko_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute("SELECT * FROM knowledge_objects WHERE id=?", (ko_id,)).fetchone()
        else:
            row = self.execute(
                "SELECT * FROM knowledge_objects WHERE id=? AND user_id=?",
                (ko_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_knowledge_versions(self, ko_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT * FROM knowledge_object_versions
               WHERE knowledge_object_id=? AND user_id=? ORDER BY version DESC""",
            (ko_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def diff_knowledge_versions(
        self,
        ko_id: str,
        user_id: str,
        *,
        from_version: int | None = None,
        to_version: int | None = None,
    ) -> dict[str, Any] | None:
        """Structured diff between two versions (default: the two most recent)."""
        from jericho.versions import diff_snapshots

        versions = self.list_knowledge_versions(ko_id, user_id)  # newest first
        if not versions:
            return None
        by_version = {int(row["version"]): _json_load(row.get("snapshot_json"), {}) for row in versions}
        available = sorted(by_version)
        newest = available[-1]
        target = to_version if to_version is not None else newest
        if target not in by_version:
            return None
        base = from_version
        if base is None:
            earlier = [v for v in available if v < target]
            base = earlier[-1] if earlier else target
        if base not in by_version:
            return None
        return {
            "knowledge_object_id": ko_id,
            "from_version": base,
            "to_version": target,
            "available_versions": available,
            "changes": diff_snapshots(by_version[base], by_version[target]),
        }

    def count_knowledge_objects(self, user_id: str) -> int:
        row = self.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL",
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_knowledge_objects(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [user_id]
        where = "user_id=? AND deleted_at IS NULL"
        if lifecycle_stage:
            where += " AND lifecycle_stage=?"
            params.append(lifecycle_stage)
        if tag:
            # ``tags_json`` is a canonical JSON array, so json_each enumerates
            # exact tags; jericho_casefold keeps the match case-insensitive for
            # Cyrillic as well (SQLite's lower() folds ASCII only).
            where += (
                " AND EXISTS (SELECT 1 FROM json_each(knowledge_objects.tags_json)"
                " WHERE jericho_casefold(json_each.value) = jericho_casefold(?))"
            )
            params.append(tag)
        if entity_id:
            # Browse-by-entity/container: only reviewer-accepted links count.
            where += (
                " AND EXISTS (SELECT 1 FROM knowledge_entity_links l"
                " WHERE l.knowledge_object_id = knowledge_objects.id"
                " AND l.entity_id=? AND l.user_id=? AND l.status='accepted')"
            )
            params.extend([entity_id, user_id])
        params.extend([max(1, min(limit, 5000)), max(0, offset)])
        # ``where`` contains only fixed clauses; all values remain bound parameters.
        rows = self.execute(
            f"SELECT * FROM knowledge_objects WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_tags(self, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Distinct tags with usage counts for browse-by-tag surfaces.

        Tags are stored canonically (deduped, casefold-sorted) per object, so
        one json_each pass yields exact values; grouping is case-insensitive
        with the first-seen spelling kept for display.
        """
        rows = self.execute(
            "SELECT json_each.value AS tag, COUNT(*) AS count"
            " FROM knowledge_objects, json_each(knowledge_objects.tags_json)"
            " WHERE knowledge_objects.user_id=? AND knowledge_objects.deleted_at IS NULL"
            " GROUP BY jericho_casefold(json_each.value)"
            " ORDER BY count DESC, jericho_casefold(json_each.value) ASC LIMIT ?",
            (user_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [{"tag": str(row["tag"]), "count": int(row["count"])} for row in rows]

    def list_container_entities(self, user_id: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Canonical container entities (projects/collections) with member counts."""
        if not types:
            return []
        placeholders = ",".join("?" for _ in types)
        rows = self.execute(
            "SELECT e.*, ("
            " SELECT COUNT(*) FROM knowledge_entity_links l"
            " JOIN knowledge_objects k ON k.id = l.knowledge_object_id"
            " WHERE l.entity_id = e.id AND l.user_id = e.user_id"
            " AND l.status='accepted' AND k.deleted_at IS NULL"
            ") AS knowledge_count"
            f" FROM entities e WHERE e.user_id=? AND e.entity_type IN ({placeholders})"  # nosec B608
            " AND e.deleted_at IS NULL AND e.canonical=1"
            " ORDER BY lower(e.name) ASC",
            (user_id, *types),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_entities_by_activity(
        self,
        user_id: str,
        *,
        types: tuple[str, ...] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Canonical entities ranked by how many live knowledge objects link them.

        The signal behind "recurring people/projects" in a user model: an entity
        the user keeps attaching material to. Only accepted links and non-deleted
        knowledge count.
        """
        clauses = ["e.user_id=?", "e.deleted_at IS NULL", "e.canonical=1"]
        params: list[Any] = [user_id]
        if types:
            placeholders = ",".join("?" for _ in types)
            clauses.append(f"e.entity_type IN ({placeholders})")  # nosec B608
            params.extend(types)
        params.append(max(1, min(int(limit), 100)))
        rows = self.execute(
            "SELECT e.id AS id, e.name AS name, e.entity_type AS entity_type,"
            " COUNT(l.id) AS knowledge_count"
            " FROM entities e"
            " JOIN knowledge_entity_links l"
            "   ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status='accepted'"
            " JOIN knowledge_objects k"
            "   ON k.id = l.knowledge_object_id AND k.deleted_at IS NULL"
            f" WHERE {' AND '.join(clauses)}"  # nosec B608
            " GROUP BY e.id ORDER BY knowledge_count DESC, lower(e.name) ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_knowledge(self, user_id: str, *, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        """Knowledge created at or after ``since_iso`` — the "what happened lately" window."""
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL AND created_at >= ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, since_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_on_this_day(
        self, user_id: str, *, month_day: str, before_iso: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Knowledge captured on the same calendar day (MM-DD) in an earlier year.

        ``created_at`` is an ISO string, so ``strftime('%m-%d', …)`` selects the
        anniversary and the ``created_at < before_iso`` bound keeps only the past.
        """
        rows = self.execute(
            "SELECT id, title, knowledge_kind, created_at FROM knowledge_objects"
            " WHERE user_id=? AND deleted_at IS NULL"
            " AND strftime('%m-%d', created_at) = ? AND created_at < ?"
            " ORDER BY created_at DESC LIMIT ?",
            (user_id, month_day, before_iso, max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete_knowledge_object(self, ko_id: str, user_id: str | None = None) -> bool:
        """Soft-delete an object while retaining a complete version snapshot."""
        current = self.get_knowledge_object(ko_id, user_id)
        if not current or current.get("deleted_at"):
            return False
        owner = str(current["user_id"])
        updated = self.update_knowledge_fields(
            ko_id,
            owner,
            lifecycle_stage=LifecycleStage.DELETED.value,
            deleted_at=utc_now(),
        )
        return updated is not None

    def search_knowledge(self, user_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = " ".join((query or "").split()).strip()
        if not text:
            return []
        rows: list[sqlite3.Row] = []
        terms = _fts_terms(text)
        if self._fts_available and terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            try:
                rows = self.execute(
                    """SELECT k.*, bm25(knowledge_fts, 1.0, 2.0, 1.5, 0.5) AS _rank
                       FROM knowledge_fts
                       JOIN knowledge_objects k ON k.rowid=knowledge_fts.rowid
                       WHERE k.user_id=? AND k.deleted_at IS NULL AND knowledge_fts MATCH ?
                       ORDER BY _rank ASC, k.importance DESC LIMIT ?""",
                    (user_id, match_query, max(1, min(limit, 200))),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            escaped = text.replace("%", r"\%").replace("_", r"\_")
            like = f"%{escaped}%"
            rows = self.execute(
                """SELECT * FROM knowledge_objects
                   WHERE user_id=? AND deleted_at IS NULL
                     AND (title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'
                          OR content LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\')
                   ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, like, like, like, like, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def eval_case_health(self, user_id: str, *, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """How much of the gold set can still be satisfied at all.

        A case whose expected objects were all deleted depresses recall forever and is
        indistinguishable, in the number alone, from search having got worse — so the
        report says which it is.
        """
        rows = self.list_eval_cases(user_id) if cases is None else cases
        wanted: set[str] = set()
        for case in rows:
            wanted.update(str(item) for item in case.get("expected_ids", []))
        live: set[str] = set()
        ordered = sorted(wanted)
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            found = self.execute(
                "SELECT id FROM knowledge_objects "  # nosec B608
                f"WHERE user_id=? AND deleted_at IS NULL AND id IN ({placeholders})",
                (user_id, *batch),
            ).fetchall()
            live.update(str(row["id"]) for row in found)
        dead_ids: list[str] = []
        stale_manual = 0
        for case in rows:
            expected = {str(item) for item in case.get("expected_ids", [])}
            if expected and not (expected & live):
                dead_ids.append(str(case["id"]))
                if str(case.get("source") or "") == "manual":
                    stale_manual += 1
        return {
            "cases": len(rows),
            "stale": len(dead_ids),
            "stale_manual": stale_manual,
            "stale_mined": len(dead_ids) - stale_manual,
            "dead_case_ids": dead_ids,
        }

    def link_knowledge_entity(
        self,
        user_id: str,
        knowledge_object_id: str,
        entity_id: str,
        *,
        status: str = "accepted",
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        ko = self.get_knowledge_object(knowledge_object_id, user_id)
        entity = self.get_entity(entity_id, user_id)
        if not ko or not entity or entity.get("deleted_at"):
            raise ValueError("Knowledge object and entity must belong to the same user")
        now = utc_now()
        link_id = new_id("kel")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_entity_links(id, user_id, knowledge_object_id, entity_id,
                   status, confidence, evidence_json, created_at, reviewed_at, reviewed_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, knowledge_object_id, entity_id) DO UPDATE SET
                     status=excluded.status, confidence=excluded.confidence,
                     evidence_json=excluded.evidence_json, reviewed_at=excluded.reviewed_at,
                     reviewed_by=excluded.reviewed_by""",
                (
                    link_id,
                    user_id,
                    knowledge_object_id,
                    entity_id,
                    status,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now if reviewed_by else None,
                    reviewed_by,
                ),
            )
            # Keep legacy primary link synchronized for older clients.
            if status == "accepted" and not ko.get("entity_id"):
                conn.execute(
                    "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE id=? AND user_id=?",
                    (entity_id, now, knowledge_object_id, user_id),
                )
        row = self.execute(
            """SELECT * FROM knowledge_entity_links
               WHERE user_id=? AND knowledge_object_id=? AND entity_id=?""",
            (user_id, knowledge_object_id, entity_id),
        ).fetchone()
        return dict(row) if row else {}

    def list_knowledge_entity_links(
        self,
        user_id: str,
        *,
        entity_id: str | None = None,
        knowledge_object_id: str | None = None,
        status: str | None = "accepted",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["l.user_id=?"]
        params: list[Any] = [user_id]
        if entity_id:
            clauses.append("l.entity_id=?")
            params.append(entity_id)
        if knowledge_object_id:
            clauses.append("l.knowledge_object_id=?")
            params.append(knowledge_object_id)
        if status:
            clauses.append("l.status=?")
            params.append(status)
        params.append(max(1, min(limit, 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT l.*, e.name AS entity_name, e.entity_type,
                       k.title AS knowledge_title, k.lifecycle_stage AS knowledge_lifecycle
                FROM knowledge_entity_links l
                JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE l.status WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                         l.confidence DESC, l.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def set_knowledge_entity_link_status(
        self,
        link_id: str,
        user_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        """Review a proposed link without losing its evidence or history."""

        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("status must be suggested, accepted, or rejected")
        link = self.execute(
            "SELECT * FROM knowledge_entity_links WHERE id=? AND user_id=?",
            (link_id, user_id),
        ).fetchone()
        if not link:
            return None
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE knowledge_entity_links
                   SET status=?, reviewed_at=?, reviewed_by=?
                   WHERE id=? AND user_id=?""",
                (status, now, reviewed_by, link_id, user_id),
            )
            if status == "accepted":
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=COALESCE(entity_id, ?), updated_at=?
                       WHERE id=? AND user_id=?""",
                    (link["entity_id"], now, link["knowledge_object_id"], user_id),
                )
            else:
                current = conn.execute(
                    """SELECT entity_id FROM knowledge_objects
                       WHERE id=? AND user_id=?""",
                    (link["knowledge_object_id"], user_id),
                ).fetchone()
                if current and current["entity_id"] == link["entity_id"]:
                    fallback = conn.execute(
                        """SELECT entity_id FROM knowledge_entity_links
                           WHERE user_id=? AND knowledge_object_id=? AND status='accepted'
                             AND id<>? ORDER BY confidence DESC, created_at ASC LIMIT 1""",
                        (user_id, link["knowledge_object_id"], link_id),
                    ).fetchone()
                    conn.execute(
                        """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                           WHERE id=? AND user_id=?""",
                        (
                            fallback["entity_id"] if fallback else None,
                            now,
                            link["knowledge_object_id"],
                            user_id,
                        ),
                    )
        row = self.execute(
            """SELECT l.*, e.name AS entity_name, e.entity_type, k.title AS knowledge_title
               FROM knowledge_entity_links l
               JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
               WHERE l.id=? AND l.user_id=?""",
            (link_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def count_entity_knowledge(self, user_id: str, entity_id: str) -> int:
        """How many accepted, live Knowledge Objects an entity carries.

        Exists because the graph layer answered this by loading up to a thousand
        full rows — bodies, summaries, tags — and calling ``len()`` on the list.
        """
        row = self.execute(
            """SELECT COUNT(*) AS count FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL""",
            (user_id, entity_id),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_entity_knowledge_refs(
        self, user_id: str, entity_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Ranking-relevant columns only, for callers that never read the body.

        ``context_for_query`` traverses the graph loading every linked object at up
        to 1000 rows per entity, for every entity the BFS dequeues — and the only
        fields it ever touches are the id, the link confidence and the two scores it
        sorts by. Everything else was megabytes of document text read from disk and
        thrown away.
        """
        rows = self.execute(
            """SELECT k.id, k.importance, k.quality_score, l.confidence AS _link_confidence
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                """SELECT id, importance, quality_score, 1.0 AS _link_confidence
                   FROM knowledge_objects WHERE user_id=? AND entity_id=?
                   AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_entity_knowledge(self, user_id: str, entity_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.execute(
            """SELECT k.*, l.confidence AS _link_confidence, l.evidence_json AS _link_evidence_json
               FROM knowledge_entity_links l
               JOIN knowledge_objects k ON k.id=l.knowledge_object_id
               WHERE l.user_id=? AND l.entity_id=? AND l.status='accepted' AND k.deleted_at IS NULL
               ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?""",
            (user_id, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        if not rows:
            rows = self.execute(
                """SELECT * FROM knowledge_objects WHERE user_id=? AND entity_id=?
                   AND deleted_at IS NULL ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (user_id, entity_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def conflict_pair_key(knowledge_a_id: str, knowledge_b_id: str) -> str:
        """Canonical key for an unordered pair — public so a detector can ask about a
        pair it has not stored yet."""
        return "|".join(sorted((knowledge_a_id, knowledge_b_id)))

    def store_knowledge_conflict(
        self,
        user_id: str,
        knowledge_a_id: str,
        knowledge_b_id: str,
        *,
        conflict_type: str = "potential_contradiction",
        confidence: float,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if knowledge_a_id == knowledge_b_id:
            raise ValueError("A knowledge object cannot conflict with itself")
        left = self.get_knowledge_object(knowledge_a_id, user_id)
        right = self.get_knowledge_object(knowledge_b_id, user_id)
        if not left or not right or left.get("deleted_at") or right.get("deleted_at"):
            raise ValueError("Both knowledge objects must belong to the same user")
        parsed_confidence = float(confidence)
        if not math.isfinite(parsed_confidence) or not 0.0 <= parsed_confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        pair_key = self.conflict_pair_key(knowledge_a_id, knowledge_b_id)
        # Bound once and reused for both the write and the read-back: the row is unique
        # on (user_id, pair_key, conflict_type), so reading by pair alone can return a
        # DIFFERENT conflict about the same pair.
        normalized_type = str(conflict_type or "potential_contradiction")[:80]
        conflict_id = new_id("conf")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO knowledge_conflicts(
                       id, user_id, knowledge_a_id, knowledge_b_id, pair_key,
                       conflict_type, confidence, evidence_json, status, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?)
                   ON CONFLICT(user_id, pair_key, conflict_type) DO UPDATE SET
                     confidence=MAX(knowledge_conflicts.confidence, excluded.confidence),
                     evidence_json=CASE
                       WHEN excluded.confidence >= knowledge_conflicts.confidence THEN excluded.evidence_json
                       ELSE knowledge_conflicts.evidence_json
                     END""",
                (
                    conflict_id,
                    user_id,
                    knowledge_a_id,
                    knowledge_b_id,
                    pair_key,
                    normalized_type,
                    parsed_confidence,
                    json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        return self.get_knowledge_conflict_by_pair(user_id, pair_key, normalized_type)

    # Projection shared with ``list_knowledge_conflicts`` so a conflict looks the same
    # whether it was just written or read back from a list.
    _CONFLICT_PROJECTION = """SELECT c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                       b.title AS knowledge_b_title, b.summary AS knowledge_b_summary
                FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id"""

    def get_knowledge_conflict_by_pair(
        self, user_id: str, pair_key: str, conflict_type: str
    ) -> dict[str, Any]:
        """Read the one conflict identified by its full unique key.

        ``store_knowledge_conflict`` used to answer this by listing up to 5000 conflicts
        and scanning them in Python — O(n) work, growing, on every write, while conflict
        detection runs per promoted object. It also matched on ``pair_key`` alone, and
        the row is unique on ``(user_id, pair_key, conflict_type)``: with two conflict
        types about the same pair it returned whichever had the higher confidence, not
        the one just written. The lookup uses the leftmost prefix of that UNIQUE index.
        """
        row = self.execute(
            f"{self._CONFLICT_PROJECTION} WHERE c.user_id=? AND c.pair_key=? AND c.conflict_type=?",  # nosec B608
            (user_id, pair_key, conflict_type),
        ).fetchone()
        return dict(row) if row else {}

    def list_knowledge_conflicts(
        self,
        user_id: str,
        *,
        status: str | None = "suggested",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        allowed = {"suggested", "confirmed", "dismissed", "resolved"}
        clauses = ["c.user_id=?"]
        params: list[Any] = [user_id]
        if status:
            if status not in allowed:
                raise ValueError("Invalid conflict status")
            clauses.append("c.status=?")
            params.append(status)
        params.append(max(1, min(int(limit), 5000)))
        # ``clauses`` contains only fixed predicates; values remain bound.
        query = f"""SELECT c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                       b.title AS knowledge_b_title, b.summary AS knowledge_b_summary
                FROM knowledge_conflicts c
                JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
                JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.confidence DESC, c.created_at DESC LIMIT ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_conflict_pair_statuses(self, user_id: str, conflict_type: str) -> dict[str, str]:
        """``pair_key -> status`` for one conflict type, in a single query.

        A detector needs to know which pairs a human has already settled BEFORE it
        proposes them again. Reading the whole map once per run also replaces the
        per-pair full re-listing ``store_knowledge_conflict`` does to return its row.
        """
        rows = self.execute(
            "SELECT pair_key, status FROM knowledge_conflicts WHERE user_id=? AND conflict_type=?",
            (user_id, str(conflict_type)),
        ).fetchall()
        return {str(row["pair_key"]): str(row["status"] or "suggested") for row in rows}

    def get_knowledge_conflict(self, user_id: str, conflict_id: str) -> dict[str, Any] | None:
        row = self.execute(
            """SELECT c.*, a.title AS knowledge_a_title, a.summary AS knowledge_a_summary,
                      b.title AS knowledge_b_title, b.summary AS knowledge_b_summary
               FROM knowledge_conflicts c
               JOIN knowledge_objects a ON a.id=c.knowledge_a_id AND a.user_id=c.user_id
               JOIN knowledge_objects b ON b.id=c.knowledge_b_id AND b.user_id=c.user_id
               WHERE c.id=? AND c.user_id=?""",
            (conflict_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def review_knowledge_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        if status not in {"confirmed", "dismissed", "resolved"}:
            raise ValueError("Invalid conflict review status")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM knowledge_conflicts WHERE id=? AND user_id=?",
                (conflict_id, user_id),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row["status"] or "suggested")
            if current_status == status:
                pass
            elif current_status == "suggested" or (current_status == "confirmed" and status == "resolved"):
                conn.execute(
                    """UPDATE knowledge_conflicts
                       SET status=?, reviewed_at=?, reviewed_by=?, resolution_note=?
                       WHERE id=? AND user_id=?""",
                    (status, utc_now(), reviewed_by, resolution_note[:2000], conflict_id, user_id),
                )
            else:
                raise ValueError(
                    f"Conflict is already {current_status}; only confirmed conflicts may advance to resolved"
                )
        return self.get_knowledge_conflict(user_id, conflict_id)

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a conflict by choosing a winner; the loser is deprecated.

        Detection and confirmation only flag a contradiction — this is the
        action that actually settles it: the losing Knowledge Object becomes
        ``deprecated`` and points at the winner (``superseded_by_id`` plus a
        ``deprecated_by_conflict`` metadata stamp), and the conflict is marked
        ``resolved``. Provenance is preserved: the loser is versioned, not
        deleted, and can be reactivated by editing it. Ordering (deprecate the
        loser, then flip the conflict) keeps a re-run after a crash idempotent.
        """
        conflict = self.get_knowledge_conflict(user_id, conflict_id)
        if conflict is None:
            return None
        knowledge_a = str(conflict["knowledge_a_id"])
        knowledge_b = str(conflict["knowledge_b_id"])
        if winner_id not in (knowledge_a, knowledge_b):
            raise ValueError("winner_id must be one of the conflicting knowledge objects")
        current_status = str(conflict.get("status") or "suggested")
        if current_status in {"dismissed", "resolved"}:
            raise ValueError(f"Conflict is already {current_status}")
        loser_id = knowledge_b if winner_id == knowledge_a else knowledge_a
        loser = self.get_knowledge_object(loser_id, user_id)
        if loser is None or loser.get("deleted_at"):
            raise ValueError("Losing knowledge object not found")

        metadata = _json_load(loser.get("metadata_json"), {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["deprecated_by_conflict"] = {
            "conflict_id": conflict_id,
            "superseded_by": winner_id,
            "at": utc_now(),
        }
        self.update_knowledge_fields(
            loser_id,
            user_id,
            lifecycle_stage=LifecycleStage.DEPRECATED.value,
            superseded_by_id=winner_id,
            metadata_json=metadata,
        )
        note = (resolution_note or f"kept {winner_id}; deprecated {loser_id}")[:2000]
        self.review_knowledge_conflict(
            user_id, conflict_id, "resolved", reviewed_by=reviewed_by, resolution_note=note
        )
        return {
            "conflict": self.get_knowledge_conflict(user_id, conflict_id),
            "winner_id": winner_id,
            "deprecated_id": loser_id,
        }

    def find_duplicate_candidates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.5,
    ) -> list[EntityResolutionCandidate]:
        """Generate conservative, context-aware duplicate proposals.

        Name similarity is only one signal.  Shared Knowledge Objects and graph neighbours raise
        confidence, while compact identifiers never use fuzzy or prefix matching.  The method only
        creates review candidates; it never performs a merge.
        """

        entities = self.list_entities(user_id, limit=5000)
        knowledge_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT entity_id, knowledge_object_id FROM knowledge_entity_links
               WHERE user_id=? AND status='accepted'""",
            (user_id,),
        ).fetchall():
            knowledge_by_entity.setdefault(str(row["entity_id"]), set()).add(str(row["knowledge_object_id"]))
        neighbours_by_entity: dict[str, set[str]] = {}
        for row in self.execute(
            """SELECT source_entity_id, target_entity_id FROM relations
               WHERE user_id=? AND deleted_at IS NULL""",
            (user_id,),
        ).fetchall():
            source = str(row["source_entity_id"])
            target = str(row["target_entity_id"])
            neighbours_by_entity.setdefault(source, set()).add(target)
            neighbours_by_entity.setdefault(target, set()).add(source)

        def variants(entity: dict[str, Any]) -> list[str]:
            values = [str(entity.get("name") or "")]
            values.extend(str(item) for item in _json_load(entity.get("aliases_json"), []))
            return [normalize_entity_name(value) for value in values if normalize_entity_name(value)]

        def acronym(name: str) -> str:
            tokens = [token for token in re.split(r"\s+", name) if token]
            return "".join(token[0] for token in tokens if token).casefold() if len(tokens) >= 2 else ""

        def overlap(left: set[str], right: set[str]) -> float:
            union = left | right
            return len(left & right) / len(union) if union else 0.0

        candidates: list[EntityResolutionCandidate] = []
        min_confidence = max(0.0, min(1.0, float(min_confidence)))
        prepared: list[dict[str, Any]] = [
            {
                "entity": entity,
                "variants": variants(entity),
                # Counted once per entity, not once per pair: the multiset bound
                # below is only cheap if this is not rebuilt 200 000 times.
                "counts": [Counter(variant) for variant in variants(entity)],
                "identifier": any(
                    _is_entity_identifier(str(value))
                    for value in [entity.get("name", ""), *_json_load(entity.get("aliases_json"), [])]
                ),
            }
            for entity in entities
        ]
        # Blocking, not all-pairs. The exhaustive scan is quadratic in entity count
        # with three-plus SequenceMatcher calls per surviving pair, and it runs from
        # an agent tool call and two HTTP routes as well as a worker. Measured on a
        # synthetic corpus of 2000 entities: **94 seconds**, on the event loop, with
        # `list_entities(limit=5000)` allowing well over twice that.
        blocks: dict[tuple[str, ...], list[int]] = {}
        for index, data in enumerate(prepared):
            for key in _blocking_keys(str(data["entity"]["entity_type"]), data["variants"]):
                blocks.setdefault(key, []).append(index)

        # No block is dropped for being large. Skipping a crowded bucket was the
        # obvious way to bound the work and it is the wrong one: a pair whose only
        # shared key lives in that bucket disappears from the proposals with nothing
        # to show for it — silent truncation, dressed as an optimisation. The pruning
        # is done instead by `_ratio_ceiling` below, which is exact.
        # Each pair remembers the STRONGEST key that introduced it, so the ceiling
        # below removes the flimsiest evidence first rather than whatever happens to
        # sort last. A shared alias outranks a shared word, which outranks two
        # adjacent characters.
        # Blocks are consumed strongest-key-first and enumeration stops at the
        # ceiling, so the ceiling bounds wall time and not merely the scoring: on a
        # corpus whose entity names share common words, 2000 entities produce 1.7
        # MILLION candidate pairs, and simply building that set costs more than
        # scoring the ones worth scoring.
        ordered: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()
        truncated = False
        for key in sorted(blocks, key=lambda item: (_KEY_RANK.get(item[0], len(_KEY_RANK)), item)):
            if truncated:
                break
            members = blocks[key]
            for position, left_index in enumerate(members):
                for right_index in members[position + 1 :]:
                    pair = (left_index, right_index)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    ordered.append(pair)
                if len(ordered) > _MAX_DUPLICATE_PAIRS:
                    truncated = True
                    break
        if truncated:
            # Said out loud. The scan is quadratic in entity count with several
            # SequenceMatcher calls per surviving pair; an exhaustive run over those
            # 2000 entities takes **166 seconds**. Returning a short list in silence
            # would let the reviewer believe there is nothing more to merge — so the
            # cheapest evidence (two adjacent characters) is what gets dropped, and
            # the fact that anything was dropped is a warning.
            LOGGER.warning(
                "duplicate detection stopped at %d candidate pairs for tenant %s — "
                "the proposal list is PARTIAL; weakest evidence was dropped first",
                len(ordered),
                user_id,
            )

        for left_index, right_index in ordered:
            left_data = prepared[left_index]
            right_data = prepared[right_index]
            left = left_data["entity"]
            right = right_data["entity"]
            left_variants = left_data["variants"]
            right_variants = right_data["variants"]
            left_name = left_variants[0] if left_variants else ""
            right_name = right_variants[0] if right_variants else ""
            if left["entity_type"] != right["entity_type"] or not left_name or not right_name:
                continue

            exact_alias = bool(set(left_variants) & set(right_variants))
            # Codes, tickers, contract identifiers, and versioned names are exact-match only.
            if (left_data["identifier"] or right_data["identifier"]) and not exact_alias:
                continue

            left_tokens = set(left_name.split())
            right_tokens = set(right_name.split())
            token_jaccard = overlap(left_tokens, right_tokens)
            acronym_match = bool(
                acronym(left_name)
                and acronym(left_name) == acronym(right_name)
                and len(left_tokens) >= 2
                and len(right_tokens) >= 2
            )
            if not exact_alias and not (left_tokens == right_tokens and len(left_tokens) >= 2):
                # Exact ceiling before spending three-plus SequenceMatcher calls.
                # `ratio()` is 2·M/(len(a)+len(b)) and matched characters cannot
                # exceed the shorter string, so `_ratio_ceiling` bounds it from above
                # for free — and `sorted_similarity` compares strings of the same
                # lengths, so the same bound holds. Adding the context boost's maximum
                # keeps this an over-estimate, so nothing that could have qualified is
                # skipped: the candidate set is unchanged, only the arithmetic is.
                left_counts = left_data["counts"]
                right_counts = right_data["counts"]
                name_ceiling = (
                    _ratio_ceiling(left_name, right_name, left_counts[0], right_counts[0])
                    if left_counts and right_counts
                    else 0.0
                )
                ceiling = max(
                    name_ceiling * 0.78,
                    token_jaccard * 0.90,
                    max(
                        (
                            _ratio_ceiling(left_variant, right_variant, left_count, right_count)
                            for left_variant, left_count in zip(left_variants, left_counts, strict=True)
                            for right_variant, right_count in zip(right_variants, right_counts, strict=True)
                        ),
                        default=0.0,
                    )
                    * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                if min(0.97, ceiling + 0.14) < min_confidence:
                    continue

            name_similarity = SequenceMatcher(None, left_name, right_name).ratio()
            sorted_similarity = SequenceMatcher(
                None,
                " ".join(sorted(left_tokens)),
                " ".join(sorted(right_tokens)),
            ).ratio()
            alias_similarity = max(
                (
                    SequenceMatcher(None, left_variant, right_variant).ratio()
                    for left_variant in left_variants
                    for right_variant in right_variants
                ),
                default=0.0,
            )
            shared_knowledge = overlap(
                knowledge_by_entity.get(str(left["id"]), set()),
                knowledge_by_entity.get(str(right["id"]), set()),
            )
            shared_neighbours = overlap(
                neighbours_by_entity.get(str(left["id"]), set()),
                neighbours_by_entity.get(str(right["id"]), set()),
            )

            if exact_alias:
                confidence = 0.995
                method = "exact_name_or_alias"
            elif left_tokens == right_tokens and len(left_tokens) >= 2:
                confidence = 0.94
                method = "same_tokens_different_order"
            else:
                confidence = max(
                    name_similarity * 0.70,
                    sorted_similarity * 0.78,
                    token_jaccard * 0.90,
                    alias_similarity * 0.76,
                    0.82 if acronym_match else 0.0,
                )
                context_boost = min(0.14, shared_knowledge * 0.09 + shared_neighbours * 0.07)
                confidence = min(0.97, confidence + context_boost)
                method = "name_alias_and_graph_evidence"

            # A single generic token needs very strong evidence; fuzzy short names create noise.
            if len(left_tokens) == len(right_tokens) == 1 and not exact_alias:
                if min(len(left_name), len(right_name)) < 5:
                    confidence *= 0.72
                if shared_knowledge == 0 and shared_neighbours == 0:
                    confidence *= 0.88
            if confidence < min_confidence:
                continue
            candidates.append(
                EntityResolutionCandidate(
                    id=new_id("er"),
                    user_id=user_id,
                    entity_a_id=left["id"],
                    entity_b_id=right["id"],
                    confidence=round(confidence, 6),
                    resolution_method=method,
                    evidence_json={
                        "left_name": left["name"],
                        "right_name": right["name"],
                        "name_similarity": round(name_similarity, 4),
                        "sorted_token_similarity": round(sorted_similarity, 4),
                        "token_jaccard": round(token_jaccard, 4),
                        "alias_similarity": round(alias_similarity, 4),
                        "exact_alias": exact_alias,
                        "acronym_match": acronym_match,
                        "shared_knowledge": round(shared_knowledge, 4),
                        "shared_graph_neighbours": round(shared_neighbours, 4),
                        "identifier_safe": not (left_data["identifier"] or right_data["identifier"]),
                    },
                )
            )
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        return candidates

    def record_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
        *,
        retrieved: bool = False,
        used_in_answer: bool = False,
    ) -> int:
        """Record bounded, tenant-checked usage signals for ranking and lifecycle.

        Counts are deliberately coarse.  They improve usefulness over time
        without creating an opaque behavioral profile or allowing another
        tenant to influence a user's ranking.
        """

        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        if not unique_ids or (not retrieved and not used_in_answer):
            return 0
        now = utc_now()
        changed = 0
        with self.transaction() as conn:
            for knowledge_id in unique_ids[:500]:
                exists = conn.execute(
                    "SELECT 1 FROM knowledge_objects WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (knowledge_id, user_id),
                ).fetchone()
                if not exists:
                    continue
                conn.execute(
                    """INSERT INTO knowledge_usage(
                           user_id, knowledge_object_id, retrieval_count, answer_count,
                           last_retrieved_at, last_used_at, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_object_id) DO UPDATE SET
                         retrieval_count=knowledge_usage.retrieval_count+excluded.retrieval_count,
                         answer_count=knowledge_usage.answer_count+excluded.answer_count,
                         last_retrieved_at=COALESCE(excluded.last_retrieved_at, knowledge_usage.last_retrieved_at),
                         last_used_at=COALESCE(excluded.last_used_at, knowledge_usage.last_used_at),
                         updated_at=excluded.updated_at""",
                    (
                        user_id,
                        knowledge_id,
                        1 if retrieved else 0,
                        1 if used_in_answer else 0,
                        now if retrieved else None,
                        now if used_in_answer else None,
                        now,
                    ),
                )
                changed += 1
        return changed

    def get_knowledge_usage(self, user_id: str, knowledge_object_ids: list[str]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        unique_ids = list(dict.fromkeys(str(item) for item in knowledge_object_ids if str(item).strip()))
        for start in range(0, len(unique_ids), 400):
            chunk = unique_ids[start : start + 400]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT * FROM knowledge_usage
                    WHERE user_id=? AND knowledge_object_id IN ({placeholders})"""  # nosec B608
            rows = self.execute(query, (user_id, *chunk)).fetchall()
            output.update({str(row["knowledge_object_id"]): dict(row) for row in rows})
        return output

    def list_knowledge_missing_embedding(
        self,
        model: str,
        *,
        limit: int = 64,
        chunk_scheme: str = "",
        chunk_threshold: int = 0,
    ) -> list[dict[str, Any]]:
        """Knowledge Objects whose stored vector is absent, from another model, or stale.

        Staleness is keyed on the Knowledge Object ``version``, which bumps on every
        content-affecting update, so a re-enriched note is re-embedded on the next
        index cycle while a lifecycle-only change is not.

        A change to the chunking configuration (``chunk_scheme``) re-stales ONLY the
        objects long enough to actually be split, so enabling passage-level recall
        does not rewrite the whole corpus of short notes. The join stays strictly 1:1
        against ``knowledge_embeddings``, so ``limit`` keeps counting objects.
        """
        bounded = max(1, min(int(limit), 1000))
        rows = self.execute(
            """SELECT k.id AS id, k.user_id AS user_id, k.version AS version,
                      k.title AS title, k.summary AS summary, k.content AS content,
                      k.tags_json AS tags_json, k.knowledge_kind AS knowledge_kind
               FROM knowledge_objects k
               LEFT JOIN knowledge_embeddings e ON e.knowledge_object_id = k.id
               WHERE k.deleted_at IS NULL
                 AND (e.knowledge_object_id IS NULL
                      OR e.model != ?
                      OR e.source_version != k.version
                      OR (e.chunk_scheme != ? AND """
            + _SEARCH_TEXT_LEN_SQL
            + """ > ?))
               ORDER BY k.updated_at DESC
               LIMIT ?""",
            (model, chunk_scheme, max(0, int(chunk_threshold)), bounded),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_user_chunk_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        object_limit: int | None = None,
        row_limit: int | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return (``ko_id#chunk_index``, packed vector) for a user's passage vectors.

        ``object_limit`` is the SAME object window ``get_user_embeddings`` uses, so a
        capped scan never covers a document only halfway; ``row_limit`` is a pure fuse
        on top of it for a corpus where the objects in that window are heavily split.
        Soft-deleted objects are excluded, mirroring the whole-object query — without
        that, deleted knowledge would resurrect through its chunks.
        """
        query = (
            "SELECT c.knowledge_object_id || '#' || c.chunk_index AS id, c.vector AS vector "
            "FROM knowledge_chunk_embeddings c "
            "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
            "WHERE c.user_id = ? AND c.model = ? AND c.dim = ? AND k.deleted_at IS NULL"
        )
        params: list[Any] = [user_id, model, int(dim)]
        if object_limit is not None and object_limit > 0:
            query += (
                " AND c.knowledge_object_id IN ("
                "SELECT id FROM knowledge_objects WHERE user_id = ? AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?)"
            )
            params.extend([user_id, int(object_limit)])
        query += " ORDER BY k.created_at DESC, c.knowledge_object_id, c.chunk_index"
        if row_limit is not None and row_limit > 0:
            query += " LIMIT ?"
            params.append(int(row_limit))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    def list_lifecycle_candidates(
        self,
        user_id: str,
        *,
        days_threshold: int = 90,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return explainable, review-only stale-knowledge candidates.

        This intentionally does not mutate lifecycle or importance. Manually
        reviewed, file-derived, recently used, or positively rated knowledge is
        protected from automated suggestions.
        """

        days = max(1, min(int(days_threshold), 36500))
        rows = self.execute(
            """SELECT k.*, u.retrieval_count, u.answer_count,
                      u.positive_feedback_count, u.negative_feedback_count,
                      u.last_retrieved_at, u.last_used_at, u.last_feedback_at
               FROM knowledge_objects k
               LEFT JOIN knowledge_usage u
                 ON u.user_id=k.user_id AND u.knowledge_object_id=k.id
               WHERE k.user_id=? AND k.lifecycle_stage='active' AND k.deleted_at IS NULL
                 AND datetime(k.updated_at) < datetime('now', ?)
               ORDER BY k.importance ASC, k.updated_at ASC LIMIT ?""",
            (user_id, f"-{days} days", max(1, min(int(limit), 5000))),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            metadata = _json_load(item.get("metadata_json"), {})
            assessment = metadata.get("promotion_assessment") if isinstance(metadata, dict) else {}
            assessment = assessment if isinstance(assessment, dict) else {}
            protected_reasons: list[str] = []
            if item.get("content_type") == "file" or metadata.get("source_filename"):
                protected_reasons.append("file-derived knowledge")
            if assessment.get("reason") in {"explicit save intent", "human review"}:
                protected_reasons.append("explicitly saved or reviewed")
            if int(item.get("positive_feedback_count") or 0) > int(item.get("negative_feedback_count") or 0):
                protected_reasons.append("positive user feedback")
            recent_cutoff = datetime.now(UTC) - timedelta(days=max(7, days // 3))
            for field, label in (
                ("last_used_at", "recently used in an answer"),
                ("last_retrieved_at", "recently retrieved"),
            ):
                timestamp = str(item.get(field) or "")
                if not timestamp:
                    continue
                try:
                    parsed = datetime.fromisoformat(timestamp)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    if parsed >= recent_cutoff:
                        protected_reasons.append(label)
                except ValueError:
                    pass
            if protected_reasons:
                continue

            importance = max(0.0, min(1.0, float(item.get("importance") or 0.5)))
            quality = max(0.0, min(1.0, float(item.get("quality_score") or 0.5)))
            promotion = max(0.0, min(1.0, float(item.get("promotion_score") or 0.5)))
            retrievals = int(item.get("retrieval_count") or 0)
            answers = int(item.get("answer_count") or 0)
            negative = int(item.get("negative_feedback_count") or 0)
            risk = (
                (1.0 - importance) * 0.38
                + (1.0 - quality) * 0.22
                + (1.0 - promotion) * 0.16
                + (0.12 if retrievals == 0 else 0.0)
                + (0.08 if answers == 0 else 0.0)
                + min(0.12, negative * 0.04)
            )
            if risk < 0.48:
                continue
            reasons = ["not updated within threshold"]
            if importance < 0.35:
                reasons.append("low importance")
            if quality < 0.4:
                reasons.append("low quality score")
            if promotion < 0.4:
                reasons.append("weak original promotion confidence")
            if retrievals == 0 and answers == 0:
                reasons.append("never used")
            if negative:
                reasons.append("negative feedback")
            output.append(
                {
                    "knowledge_object": item,
                    "risk_score": round(min(1.0, risk), 4),
                    "recommended_action": "review_for_archive" if risk >= 0.68 else "review_importance",
                    "suggested_importance": round(max(0.1, importance - min(0.2, risk * 0.15)), 3),
                    "reasons": reasons,
                    "protected": False,
                }
            )
        return sorted(output, key=lambda item: item["risk_score"], reverse=True)

    def deprecate_stale_knowledge(self, user_id: str, days_threshold: int = 90) -> int:
        """Archive low-value stale items, creating a version for every transition."""
        days = max(1, min(int(days_threshold), 36500))
        rows = self.execute(
            """SELECT id FROM knowledge_objects
               WHERE user_id=? AND lifecycle_stage='active' AND deleted_at IS NULL
                 AND importance < 0.3
                 AND datetime(updated_at) < datetime('now', ?)""",
            (user_id, f"-{days} days"),
        ).fetchall()
        changed = 0
        for row in rows:
            if self.update_knowledge_fields(
                str(row["id"]),
                user_id,
                lifecycle_stage=LifecycleStage.ARCHIVED.value,
            ):
                changed += 1
        return changed

    def get_lifecycle_stats(self, user_id: str) -> dict[str, int]:
        rows = self.execute(
            """SELECT lifecycle_stage, COUNT(*) AS count FROM knowledge_objects
               WHERE user_id=? GROUP BY lifecycle_stage""",
            (user_id,),
        ).fetchall()
        result = {stage.value: 0 for stage in LifecycleStage}
        result.update({row["lifecycle_stage"]: int(row["count"]) for row in rows})
        return result
