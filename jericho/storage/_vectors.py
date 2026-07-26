"""Storage methods for embedding vectors, whole-object and passage-level.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    Any,
    Mapping,
    Sequence,
    StorageShared,
    utc_now,
)


class VectorsMixin(StorageShared):
    def get_user_embeddings(
        self, user_id: str, model: str, dim: int, *, limit: int | None = None
    ) -> list[tuple[str, bytes]]:
        """Return (knowledge_object_id, packed vector) for a user's live vectors.

        Only rows matching the active model and dimension are returned, and vectors
        whose Knowledge Object has been soft-deleted are excluded, so a stale row can
        never resurrect deleted knowledge into dense recall. ``limit`` caps the scan
        to the newest N objects (a latency guard on a large corpus).
        """
        query = (
            "SELECT e.knowledge_object_id AS id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects k ON k.id = e.knowledge_object_id "
            "WHERE e.user_id = ? AND e.model = ? AND e.dim = ? AND k.deleted_at IS NULL"
        )
        params: list[Any] = [user_id, model, int(dim)]
        if limit is not None and limit > 0:
            query += " ORDER BY k.created_at DESC LIMIT ?"
            params.append(int(limit))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), bytes(row["vector"])) for row in rows]

    def list_user_vectors_page(
        self,
        user_id: str,
        model: str,
        *,
        after: tuple[str, str] | None = None,
        before: tuple[str, str] | None = None,
        max_updated_at: str | None = None,
        descending: bool = False,
        limit: int = 2048,
    ) -> list[tuple[str, str, bytes]]:
        """One keyset page of ``(knowledge_object_id, updated_at, vector)``.

        Ordered by ``(updated_at, knowledge_object_id)`` — a TOTAL order, unlike the
        bare ``created_at`` the old capped scan used, where a bulk import sharing one
        second left it undefined which rows survived the LIMIT. ``after``/``before``
        are strict bounds so paging can neither repeat nor skip a row, and
        ``max_updated_at`` excludes the run's own second, whose rows are not
        necessarily all written yet.
        """
        clauses = ["e.user_id = ?", "e.model = ?", "k.deleted_at IS NULL"]
        params: list[Any] = [user_id, model]
        if after is not None:
            clauses.append("(e.updated_at, e.knowledge_object_id) > (?, ?)")
            params.extend([after[0], after[1]])
        if before is not None:
            clauses.append("(e.updated_at, e.knowledge_object_id) < (?, ?)")
            params.extend([before[0], before[1]])
        if max_updated_at is not None:
            clauses.append("e.updated_at < ?")
            params.append(max_updated_at)
        direction = "DESC" if descending else "ASC"
        query = (
            "SELECT e.knowledge_object_id AS id, e.updated_at AS updated_at, e.vector AS vector "  # nosec B608
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects k ON k.id = e.knowledge_object_id "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY e.updated_at {direction}, e.knowledge_object_id {direction} LIMIT ?"
        )
        params.append(max(1, min(int(limit), 10000)))
        rows = self.execute(query, tuple(params)).fetchall()
        return [(str(row["id"]), str(row["updated_at"]), bytes(row["vector"])) for row in rows]

    def count_user_vectors(self, user_id: str, model: str, *, before: tuple[str, str] | None = None) -> int:
        """Corpus size, or how many rows are still strictly below a backfill cursor."""
        clauses = ["e.user_id = ?", "e.model = ?", "k.deleted_at IS NULL"]
        params: list[Any] = [user_id, model]
        if before is not None:
            clauses.append("(e.updated_at, e.knowledge_object_id) < (?, ?)")
            params.extend([before[0], before[1]])
        row = self.execute(
            "SELECT COUNT(*) AS n FROM knowledge_embeddings e "  # nosec B608
            "JOIN knowledge_objects k ON k.id = e.knowledge_object_id "
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchone()
        return int(row["n"]) if row else 0

    def get_chunk_spans(
        self, user_id: str, model: str, keys: Sequence[tuple[str, int]]
    ) -> dict[tuple[str, int], tuple[int, int]]:
        """Character spans of specific chunks, so the answer can quote the passage
        that actually matched instead of the lexically best window.

        Only spans still valid for the object's CURRENT version are returned. Between
        an edit and the next index tick the stored offsets describe the previous
        revision; slicing today's content at them would quote an arbitrary window, so
        a stale row yields nothing and the caller falls back to the whole body.
        """
        spans: dict[tuple[str, int], tuple[int, int]] = {}
        wanted = {(str(ko_id), int(index)) for ko_id, index in keys}
        if not wanted:
            return spans
        ordered = sorted({ko_id for ko_id, _ in wanted})
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT c.knowledge_object_id AS knowledge_object_id, c.chunk_index AS chunk_index, "  # nosec B608
                "c.start_char AS start_char, c.end_char AS end_char "
                "FROM knowledge_chunk_embeddings c "
                "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
                "AND k.version = c.source_version "
                f"WHERE c.user_id = ? AND c.model = ? AND c.knowledge_object_id IN ({placeholders})",
                (user_id, model, *batch),
            ).fetchall()
            for row in rows:
                key = (str(row["knowledge_object_id"]), int(row["chunk_index"]))
                if key in wanted:
                    spans[key] = (int(row["start_char"]), int(row["end_char"]))
        return spans

    def get_reusable_vectors(
        self, knowledge_object_ids: Sequence[str], model: str
    ) -> dict[str, dict[str, bytes]]:
        """``{ko_id: {content_hash: packed vector}}`` across both vector tables.

        The same text embedded by the same model yields the same vector, so a re-index
        triggered by a lifecycle-only version bump or a chunking-config change costs no
        HTTP call at all for the parts whose text did not change.
        """
        reusable: dict[str, dict[str, bytes]] = {}
        ordered = sorted({str(value) for value in knowledge_object_ids})
        if not ordered:
            return reusable
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT knowledge_object_id, content_hash, vector "  # nosec B608
                "FROM knowledge_embeddings "
                f"WHERE model = ? AND knowledge_object_id IN ({placeholders}) "
                "UNION ALL "
                "SELECT knowledge_object_id, content_hash, vector "
                "FROM knowledge_chunk_embeddings "
                f"WHERE model = ? AND knowledge_object_id IN ({placeholders})",
                (model, *batch, model, *batch),
            ).fetchall()
            for row in rows:
                content_hash = str(row["content_hash"] or "")
                if not content_hash:
                    continue
                bucket = reusable.setdefault(str(row["knowledge_object_id"]), {})
                bucket[content_hash] = bytes(row["vector"])
        return reusable

    def upsert_knowledge_vectors(
        self,
        items: Sequence[dict[str, Any]],
        chunks: Mapping[str, Sequence[dict[str, Any]]] | None = None,
    ) -> dict[str, int]:
        """Write whole-object vectors and their passage vectors in ONE transaction.

        Atomicity is load-bearing: staleness is decided from the whole-object row
        alone, so a committed object row whose chunk rows were lost would look fresh
        forever. Chunk rows are deleted-then-inserted rather than upserted, so an
        object that shrank from nine chunks to three leaves no orphans behind — and an
        empty chunk list is how disabling chunking cleans itself up.
        """
        if not items and not chunks:
            return {"objects": 0, "chunks": 0}
        now = utc_now()
        written_objects = 0
        written_chunks = 0
        with self.transaction() as conn:
            for item in items:
                conn.execute(
                    """INSERT INTO knowledge_embeddings(
                           knowledge_object_id, user_id, model, dim,
                           source_version, content_hash, chunk_scheme, vector, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(knowledge_object_id) DO UPDATE SET
                           user_id=excluded.user_id,
                           model=excluded.model,
                           dim=excluded.dim,
                           source_version=excluded.source_version,
                           content_hash=excluded.content_hash,
                           chunk_scheme=excluded.chunk_scheme,
                           vector=excluded.vector,
                           updated_at=excluded.updated_at""",
                    (
                        str(item["knowledge_object_id"]),
                        str(item["user_id"]),
                        str(item["model"]),
                        int(item["dim"]),
                        int(item.get("source_version", 0)),
                        str(item.get("content_hash", "")),
                        str(item.get("chunk_scheme", "")),
                        bytes(item["vector"]),
                        now,
                    ),
                )
                written_objects += 1
            for knowledge_object_id, rows in (chunks or {}).items():
                conn.execute(
                    "DELETE FROM knowledge_chunk_embeddings WHERE knowledge_object_id=?",
                    (str(knowledge_object_id),),
                )
                for row in rows:
                    conn.execute(
                        """INSERT INTO knowledge_chunk_embeddings(
                               knowledge_object_id, chunk_index, user_id, model, dim,
                               source_version, chunk_scheme, start_char, end_char,
                               content_hash, vector, updated_at)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(knowledge_object_id),
                            int(row["chunk_index"]),
                            str(row["user_id"]),
                            str(row["model"]),
                            int(row["dim"]),
                            int(row.get("source_version", 0)),
                            str(row.get("chunk_scheme", "")),
                            int(row.get("start_char", 0)),
                            int(row.get("end_char", 0)),
                            str(row.get("content_hash", "")),
                            bytes(row["vector"]),
                            now,
                        ),
                    )
                    written_chunks += 1
        return {"objects": written_objects, "chunks": written_chunks}

    def upsert_knowledge_embeddings(self, items: Sequence[dict[str, Any]]) -> int:
        """Insert or replace a batch of whole-object vectors; return the count."""
        return self.upsert_knowledge_vectors(items)["objects"]

    def delete_knowledge_embedding(self, knowledge_object_id: str) -> None:
        """Drop an object's vectors — whole-object and passage-level together, so no
        half-deleted state can outlive the call."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM knowledge_chunk_embeddings WHERE knowledge_object_id=?",
                (knowledge_object_id,),
            )
            conn.execute(
                "DELETE FROM knowledge_embeddings WHERE knowledge_object_id=?",
                (knowledge_object_id,),
            )

    def count_knowledge_embeddings(self, user_id: str | None = None) -> int:
        if user_id is None:
            row = self.execute("SELECT COUNT(*) AS n FROM knowledge_embeddings").fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS n FROM knowledge_embeddings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_knowledge_chunk_embeddings(self, user_id: str | None = None) -> int:
        if user_id is None:
            row = self.execute("SELECT COUNT(*) AS n FROM knowledge_chunk_embeddings").fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS n FROM knowledge_chunk_embeddings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_chunked_knowledge_objects(self, user_id: str | None = None) -> int:
        """How many objects actually carry passage vectors (index-coverage signal)."""
        if user_id is None:
            row = self.execute(
                "SELECT COUNT(DISTINCT knowledge_object_id) AS n FROM knowledge_chunk_embeddings"
            ).fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(DISTINCT knowledge_object_id) AS n "
                "FROM knowledge_chunk_embeddings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["n"]) if row else 0
