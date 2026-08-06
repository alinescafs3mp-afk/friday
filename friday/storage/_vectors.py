"""Storage methods for embedding vectors, whole-object and passage-level.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from friday.storage._base import (
    Any,
    Mapping,
    Sequence,
    StorageShared,
    utc_now,
)
from friday.storage._privacy import (
    _exact_uploader_knowledge_dependency,
    _not_private_knowledge_dependency,
)


class VectorsMixin(StorageShared):
    def dense_vector_signature(self, user_id: str, model: str, dim: int) -> tuple[Any, ...]:
        """Дешёвая подпись состояния векторов арендатора — для резидентного кэша.

        Меняется от любого события, которое обязано его пересобрать: записи и
        перезаписи векторов (счётчики и MAX(updated_at) обеих таблиц), удаления
        строк (счётчики падают), мягкого удаления/восстановления объекта
        (MAX(updated_at) объектов берётся по ВСЕМ строкам, живой счётчик — по
        живым). Шесть скалярных подзапросов по индексированным колонкам.
        """
        row = self.execute(
            """SELECT
                 (SELECT COUNT(*) FROM knowledge_embeddings
                   WHERE user_id=? AND model=? AND dim=?) AS doc_count,
                 (SELECT COALESCE(MAX(updated_at), '') FROM knowledge_embeddings
                   WHERE user_id=? AND model=? AND dim=?) AS doc_updated,
                 (SELECT COUNT(*) FROM knowledge_chunk_embeddings
                   WHERE user_id=? AND model=? AND dim=?) AS chunk_count,
                 (SELECT COALESCE(MAX(updated_at), '') FROM knowledge_chunk_embeddings
                   WHERE user_id=? AND model=? AND dim=?) AS chunk_updated,
                 (SELECT COUNT(*) FROM knowledge_objects
                   WHERE user_id=? AND deleted_at IS NULL) AS live_objects,
                 (SELECT COALESCE(MAX(updated_at), '') FROM knowledge_objects
                   WHERE user_id=?) AS objects_updated""",
            (user_id, model, int(dim)) * 4 + (user_id, user_id),
        ).fetchone()
        return tuple(row)

    def get_user_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        limit: int | None = None,
        uploaded_by: str | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return (knowledge_object_id, packed vector) for a user's live vectors.

        Only rows matching the active model and dimension are returned, and vectors
        whose Knowledge Object has been soft-deleted are excluded, so a stale row can
        never resurrect deleted knowledge into dense recall. ``limit`` caps the scan
        to the newest N objects (a latency guard on a large corpus).
        """
        author = str(uploaded_by) if uploaded_by is not None else None
        if author is not None and not author.strip():
            return []
        params: list[Any] = [user_id, model, int(dim)]
        if limit is not None and limit > 0:
            # The window is chosen FIRST, by id, and only then are vectors fetched.
            #
            # Joining and sorting by ``k.created_at DESC LIMIT ?`` put the sort key on
            # the far side of the join, so the LIMIT could not short-circuit: every
            # vector BLOB belonging to the tenant was read and pushed through a temp
            # b-tree before N of them survived. Measured at 10k 1024-float vectors:
            # 469 ms, and the whole point of the cap was to avoid exactly that.
            # `idx_knowledge_chunk_scan_order` makes the inner select an index walk
            # that stops after N rows.  The id tail is load-bearing: equal import
            # timestamps must not let physical rowid/VACUUM choose the recall window.
            query = (
                "SELECT e.knowledge_object_id AS id, e.vector AS vector "
                "FROM knowledge_embeddings e "
                "WHERE e.user_id = ? AND e.model = ? AND e.dim = ? "
                "AND e.knowledge_object_id IN ("
                "  SELECT window_k.id FROM knowledge_objects window_k "
                "  INDEXED BY idx_knowledge_chunk_scan_order"
                "  WHERE window_k.user_id = ? AND window_k.deleted_at IS NULL "
                f" AND {_not_private_knowledge_dependency('window_k')}"  # nosec B608
            )
            params.append(user_id)
            if author is not None:
                query += f" AND {_exact_uploader_knowledge_dependency('window_k')}"
                params.append(author)
            query += " ORDER BY window_k.created_at DESC, window_k.id ASC LIMIT ?)"
            params.append(int(limit))
        else:
            query = (
                "SELECT e.knowledge_object_id AS id, e.vector AS vector "
                "FROM knowledge_embeddings e "
                "JOIN knowledge_objects k ON k.id = e.knowledge_object_id "
                "WHERE e.user_id = ? AND e.model = ? AND e.dim = ? "
                "AND k.user_id = ? AND k.deleted_at IS NULL "
                f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608
            )
            # Embedding owner is denormalised and can be malformed independently of
            # its KO.  Both sides are authority predicates, never one or the other.
            params.append(user_id)
            if author is not None:
                query += f" AND {_exact_uploader_knowledge_dependency('k')}"
                params.append(author)
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
        clauses = [
            "e.user_id = ?",
            "e.model = ?",
            "k.deleted_at IS NULL",
            _not_private_knowledge_dependency("k"),
        ]
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
        clauses = [
            "e.user_id = ?",
            "e.model = ?",
            "k.deleted_at IS NULL",
            _not_private_knowledge_dependency("k"),
        ]
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
        self,
        user_id: str,
        model: str,
        keys: Sequence[tuple[str, int]],
        *,
        uploaded_by: str | None = None,
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
        if not wanted or (uploaded_by is not None and not str(uploaded_by).strip()):
            return spans
        ordered = sorted({ko_id for ko_id, _ in wanted})
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            query = (
                "SELECT c.knowledge_object_id AS knowledge_object_id, c.chunk_index AS chunk_index, "
                "c.start_char AS start_char, c.end_char AS end_char "
                "FROM knowledge_chunk_embeddings c "
                "JOIN knowledge_objects k ON k.id = c.knowledge_object_id "
                "AND k.version = c.source_version "
                "WHERE c.user_id = ? AND c.model = ? AND k.user_id = ? "
                f"AND {_not_private_knowledge_dependency('k')} "  # nosec B608
            )
            params: list[Any] = [user_id, model, user_id]
            if uploaded_by is not None:
                query += f"AND {_exact_uploader_knowledge_dependency('k')} "
                params.append(str(uploaded_by))
            query += f"AND c.knowledge_object_id IN ({placeholders})"  # nosec B608
            params.extend(batch)
            rows = self.execute(query, tuple(params)).fetchall()
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

    def get_vectors_by_content_hash(self, content_hashes: Sequence[str], model: str) -> dict[str, bytes]:
        """``{content_hash: packed vector}`` for ANY object, not just one.

        `get_reusable_vectors` answers "has THIS object embedded this text before",
        which covers a re-index and nothing else. The same text embedded by the same
        model yields the same vector whoever owns it — the sibling method's own
        docstring says so — and a real archive is full of the same text twice: one
        folder of 342 documents held 13 groups of byte-identical files, 29 objects,
        each paying the model for a vector that already existed. Re-importing a folder
        that was imported before is the same case in the extreme, and it used to cost
        a full re-embedding of everything.
        """
        unique = sorted({str(value) for value in content_hashes if value})
        if not unique:
            return {}
        found: dict[str, bytes] = {}
        for start in range(0, len(unique), 400):
            batch = unique[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT content_hash, vector FROM knowledge_embeddings "  # nosec B608
                f"WHERE model = ? AND content_hash IN ({placeholders}) "
                "UNION ALL "
                "SELECT content_hash, vector FROM knowledge_chunk_embeddings "
                f"WHERE model = ? AND content_hash IN ({placeholders})",
                (model, *batch, model, *batch),
            ).fetchall()
            for row in rows:
                digest = str(row["content_hash"] or "")
                if digest and digest not in found:
                    found[digest] = bytes(row["vector"])
        return found

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

    def mark_embeddings_stale(self, *, user_id: str | None = None) -> int:
        """Пометить векторы устаревшими, НЕ удаляя их; вернуть число помеченных.

        Нужно, когда вектора посчитаны верно по форме, но по неверному тексту, и
        отличить такие от честных нельзя: хэш пишется от полного текста, а вектор
        мог быть посчитан по укороченному. Ровно это и случилось — отказ сервиса по
        длине лечился укорачиванием всех текстов пачки, и 32 из 50 проверенных
        векторов описывали другой текст.

        Пометка вместо удаления принципиальна. Устаревание определяется по
        `source_version != k.version` (см. `_missing_embedding_filter`), поэтому
        достаточно поставить в это поле значение, которым настоящая версия быть не
        может. Вектор остаётся на месте и продолжает отвечать на поиск, пока
        индексатор не заменит его правильным. Удаление оставило бы корпус без
        плотного поиска на всё время пересчёта.

        Одной пометки версии НЕ ХВАТАЕТ, и это выяснилось на живом пересчёте: 1208
        объектов из 1537 «пересчитались» за две минуты по нулю секунд на пачку.
        Никакого пересчёта не было — сработало переиспользование по хэшу текста
        (`get_vectors_by_content_hash`), и оно вернуло ТЕ ЖЕ негодные вектора. Так и
        должно быть по его правилам: хэш писался от полного текста, вектор считался
        по укороченному, и отличить одно от другого хранилище не может.

        Поэтому хэш тоже стирается — в обеих таблицах. Чанки помечаются вместе с
        объектом: их тексты укорачивались в тех же пачках.

        ⚠️ Пустой хэш закрывает переиспользование ТОЛЬКО у помеченных строк, и этого
        достаточно лишь при пометке всех арендаторов. Поиск по хэшу
        (`get_vectors_by_content_hash`) идёт по всей таблице без фильтра арендатора —
        в этом его польза при повторном импорте, — поэтому при `--user X` тот же
        текст у другого арендатора остаётся с прежним, возможно негодным, вектором,
        и индексатор нашёл бы его вместо пересчёта. Закрыто на стороне индексатора:
        он не спрашивает кэш по хэшу для объектов, чей собственный хэш стёрт.
        """
        with self.transaction() as conn:
            marked = 0
            for table in ("knowledge_embeddings", "knowledge_chunk_embeddings"):
                if user_id is None:
                    cursor = conn.execute(
                        f"UPDATE {table} SET source_version=-1, content_hash=''"  # nosec B608
                    )
                else:
                    cursor = conn.execute(
                        f"UPDATE {table} SET source_version=-1, content_hash='' WHERE user_id=?",  # nosec B608
                        (user_id,),
                    )
                if table == "knowledge_embeddings":
                    marked = int(cursor.rowcount or 0)
            return marked

    def list_forced_embedding_ids(self, knowledge_object_ids: Sequence[str]) -> list[str]:
        """Кто из этих объектов помечен на принудительный пересчёт ПРЯМО СЕЙЧАС.

        Признак тот же, что и в выборке устаревших: собственная строка объекта есть, а
        `content_hash` у неё пуст. Нужен потому, что пометка может прийти, пока пачка
        находится в сервисе: план построен до неё, часть входов разрешена из кэша, и
        запись такого плана стёрла бы пометку вектором, который никто не пересчитывал.
        """
        ordered = sorted({str(value) for value in knowledge_object_ids})
        if not ordered:
            return []
        found: list[str] = []
        for start in range(0, len(ordered), 400):
            batch = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self.execute(
                "SELECT knowledge_object_id FROM knowledge_embeddings "  # nosec B608
                f"WHERE knowledge_object_id IN ({placeholders}) AND COALESCE(content_hash, '') = ''",
                tuple(batch),
            ).fetchall()
            found.extend(str(row["knowledge_object_id"]) for row in rows)
        return found

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
