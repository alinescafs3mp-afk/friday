"""Storage methods for entities, relations, aliases and resolution candidates.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from jericho.storage._base import (
    LOGGER,
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

    # Поля, которые ОПИСЫВАЮТ сущность и потому подлежат откату. Намеренно без
    # `canonical`/`merged_into_id`: слияние — отдельное решение со своей историей
    # и своим обратным ходом (`unmerge_entities`), и молча разъединять его откатом
    # правки имени значило бы делать за человека то, о чём он не просил.
    _RESTORABLE_ENTITY_FIELDS = ("name", "entity_type", "aliases_json", "description", "metadata_json")

    def merge_version_floor(self, entity_id: str, user_id: str) -> int:
        """Ниже какой версии откат правки уже не «правка», а разрушение слияния.

        Слияние правит ЦЕЛЬ (переносит имя источника в её алиасы) и пишет это
        обычной новой версией — то есть в истории объекта появляется правка,
        которую человек не делал. Откат «на одну назад» после слияния стирал
        алиас-мост, а сама слитая сущность оставалась надгробием: слияние
        распадалось наполовину и молча — поиск по прежнему имени переставал
        находить объект, а очередь слияний считала пару решённой.

        Слияние отменяется своим обратным ходом (`unmerge_entities`), и только им.
        Поэтому версии, созданные ЖИВЫМИ (неотменёнными) слияниями, для отката
        правки закрыты — возвращается наибольшая такая версия.
        """
        rows = self.execute(
            """SELECT target_after_json FROM entity_merge_history
               WHERE user_id=? AND target_entity_id=? AND undone_at IS NULL""",
            (user_id, entity_id),
        ).fetchall()
        floor = 0
        for row in rows:
            snapshot = _json_load(row["target_after_json"], {})
            if isinstance(snapshot, dict):
                floor = max(floor, int(snapshot.get("version") or 0))
        return floor

    def restore_entity_version(
        self, entity_id: str, user_id: str, version: int, *, reviewed_by: str | None = None
    ) -> dict[str, Any] | None:
        """Вернуть сущность к состоянию из снимка — новой версией, не перемоткой.

        Спека v3 §2 требует, чтобы исправление сущности было обратимым
        («correction... reversible without editing the Raw Object»), и снимки для
        этого уже писались при каждой правке — не было только обратного хода. У
        знаний он давно есть (`restore_knowledge_version`), у сущностей не было.

        Это не косметика на корпусе, где 4349 узлов-людей и 149 войсковых частей
        заведены автоматическими правилами: первая же правка не того узла (или
        правка, сделанная по ошибочной догадке) иначе необратима.

        Откат идёт обычной правкой, поэтому создаёт версию N+1 и ничего не
        стирает: откатившийся по ошибке может откатиться назад.
        """
        rows = [
            row
            for row in self.list_entity_versions(entity_id, user_id)
            if int(row.get("version") or 0) == int(version)
        ]
        if not rows:
            raise LookupError(f"Version {version} not found for {entity_id}")
        try:
            snapshot = json.loads(str(rows[0].get("snapshot_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("Version snapshot is not readable") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("Version snapshot is not an object")
        current = self.get_entity(entity_id, user_id)
        if not current or current.get("deleted_at"):
            return None
        floor = self.merge_version_floor(entity_id, user_id)
        if floor and int(version) < floor:
            raise ValueError(
                "Эта версия объекта относится к слиянию — откатывать его надо разъединением, "
                "иначе слитая сущность останется надгробием, а мост-алиас исчезнет"
            )
        fields = {name: snapshot[name] for name in self._RESTORABLE_ENTITY_FIELDS if name in snapshot}
        if not fields:
            raise ValueError("Version snapshot carries no restorable fields")
        raw_metadata = _json_load(fields.get("metadata_json"), {})
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        if reviewed_by:
            # Кто откатил и к чему — на самой сущности, а не только в аудите:
            # тот же принцип, что у знаний.
            metadata["restored_from_version"] = int(version)
            metadata["restored_by"] = str(reviewed_by)
        fields["metadata_json"] = metadata
        entity = Entity(
            id=str(current["id"]),
            user_id=user_id,
            name=str(fields.get("name") or current["name"]),
            entity_type=EntityType(str(fields.get("entity_type") or current.get("entity_type") or "other")),
            aliases_json=[str(item) for item in _json_load(fields.get("aliases_json"), []) or []],
            description=str(fields.get("description") or ""),
            metadata_json=metadata,
            canonical=bool(current.get("canonical", 1)),
            merged_into_id=current.get("merged_into_id"),
            version=int(current.get("version", 1)),
            created_at=str(current.get("created_at") or utc_now()),
            updated_at=utc_now(),
            deleted_at=current.get("deleted_at"),
        )
        self.update_entity(entity)
        return self.get_entity(entity_id, user_id)

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

    def graph_overview(self, user_id: str, *, limit: int = 120) -> dict[str, Any]:
        """Связная картина графа целиком, а не окрестность одного узла.

        Рисовать по `relations` бессмысленно: на живой установке их ноль и всегда
        было ноль — связь сущность↔сущность появляется только после подтверждения
        человеком. Зато `knowledge_entity_links` живут: две сущности, встреченные в
        одном документе, — это наблюдаемый факт, а не догадка, и именно он даёт
        связную картину на реальных данных.

        Поэтому рёбер два вида, и они НЕ смешиваются: `relation` — утверждение,
        которое кто-то подтвердил, `cooccurrence` — просто совместная встречаемость,
        с числом общих документов. Показывать их одинаково значило бы выдавать
        наблюдение за утверждение.

        Узлы отбираются по числу связанных документов и ограничены `limit`; сколько
        осталось за кадром, возвращается отдельно — картинка, молча показывающая
        часть графа, хуже отсутствующей.
        """
        bounded = max(1, min(int(limit), 500))
        rows = self.execute(
            """SELECT e.id, e.name, e.entity_type, COUNT(l.knowledge_object_id) AS knowledge_count
               FROM entities e
               JOIN knowledge_entity_links l
                 ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status = 'accepted'
               WHERE e.user_id = ? AND e.deleted_at IS NULL AND e.merged_into_id IS NULL
               GROUP BY e.id
               ORDER BY knowledge_count DESC, e.name COLLATE NOCASE, e.id
               LIMIT ?""",
            (user_id, bounded),
        ).fetchall()
        nodes = [dict(row) for row in rows]
        ids = [str(node["id"]) for node in nodes]
        if not ids:
            return {"nodes": [], "edges": [], "shown": 0, "total": self.count_entities(user_id)}

        placeholders = ",".join("?" * len(ids))
        # Совместная встречаемость считается ТОЛЬКО между показанными узлами: ребро в
        # невидимый узел рисовать некуда, а считать его в статистику — врать.
        cooccurrence = self.execute(
            f"""SELECT a.entity_id AS source, b.entity_id AS target,
                       COUNT(DISTINCT a.knowledge_object_id) AS weight
                FROM knowledge_entity_links a
                JOIN knowledge_entity_links b
                  ON b.knowledge_object_id = a.knowledge_object_id
                 AND b.user_id = a.user_id AND b.entity_id > a.entity_id
                WHERE a.user_id = ? AND a.status = 'accepted' AND b.status = 'accepted'
                  AND a.entity_id IN ({placeholders}) AND b.entity_id IN ({placeholders})
                GROUP BY a.entity_id, b.entity_id
                ORDER BY weight DESC
                LIMIT 800""",  # nosec B608
            (user_id, *ids, *ids),
        ).fetchall()
        relations = self.execute(
            f"""SELECT source_entity_id AS source, target_entity_id AS target, relation_type
                FROM relations
                WHERE user_id = ? AND source_entity_id IN ({placeholders})
                  AND target_entity_id IN ({placeholders})
                LIMIT 800""",  # nosec B608
            (user_id, *ids, *ids),
        ).fetchall()
        edges = [{**dict(row), "kind": "cooccurrence"} for row in cooccurrence]
        edges.extend({**dict(row), "kind": "relation", "weight": 1} for row in relations)
        return {
            "nodes": nodes,
            "edges": edges,
            "shown": len(nodes),
            "total": self.count_entities(user_id),
        }

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
        bounded = max(1, min(limit, 5000))
        params.extend([bounded, max(0, offset)])
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
        if len(rows) == bounded and offset <= 0:
            # Обрез был ТИХИМ, и это худший из возможных способов не справиться.
            # Проверено исполнением на 8001 сущности: прямой поиск по имени находит
            # запись, а `search_entities` и `match_mentions` возвращают ноль — они
            # строят своё представление из этого списка. Отрезается всегда один и
            # тот же хвост (`ORDER BY name`), то есть конец алфавита исчезает из
            # графа навсегда и молча.
            #
            # Настоящее лечение — не поднять потолок, а перестать строить работу с
            # графом на полной выборке; пока этого нет, обрез обязан быть слышен.
            total = self.count_entities(user_id, entity_type)
            if total > bounded:
                LOGGER.warning(
                    "list_entities returned %d of %d entities for tenant %s — the tail is "
                    "invisible to entity matching and graph expansion",
                    bounded,
                    total,
                    user_id,
                )
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
        """Entities whose alias normalises to ``alias``. No full-graph page cap.

        The previous path walked ``list_entities(limit=5000)`` and therefore lost
        every alias that lived past the alphabetical ceiling — the same silent
        blindness as ``match_mentions``. Only rows that actually carry aliases are
        loaded; empty ``[]`` is the common case and is filtered in SQL.
        """
        normalized = normalize_entity_name(alias)
        if not normalized:
            return []
        results: list[dict[str, Any]] = []
        rows = self.execute(
            """SELECT * FROM entities
               WHERE user_id=? AND deleted_at IS NULL AND canonical=1
                 AND aliases_json NOT IN ('[]', '', 'null')
               ORDER BY name COLLATE NOCASE, id""",
            (user_id,),
        ).fetchall()
        for row in rows:
            item = dict(row)
            aliases = _json_load(item.get("aliases_json"), [])
            if any(normalize_entity_name(alias) == normalized for alias in aliases):
                results.append(item)
        return results

    def find_entities_by_normalized_names(
        self,
        user_id: str,
        names: Sequence[str],
        *,
        include_aliases: bool = True,
    ) -> list[dict[str, Any]]:
        """Canonical entities matching any of the given names (or their aliases).

        Callers hand terms extracted from text; this method never lists the whole
        graph. A graph past the ``list_entities`` ceiling of 5000 stays fully
        addressable — the lookup is keyed on ``normalized_name`` (and, optionally,
        alias JSON for the minority of nodes that carry one).
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in names:
            key = normalize_entity_name(str(raw or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            wanted.append(key)
        if not wanted:
            return []

        by_id: dict[str, dict[str, Any]] = {}
        # SQLite caps host parameters; stay well under the common 999 limit.
        chunk_size = 400
        for start in range(0, len(wanted), chunk_size):
            chunk = wanted[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self.execute(
                f"""SELECT * FROM entities
                    WHERE user_id=? AND deleted_at IS NULL AND canonical=1
                      AND normalized_name IN ({placeholders})""",  # nosec B608
                (user_id, *chunk),
            ).fetchall()
            for row in rows:
                by_id[str(row["id"])] = dict(row)

        if include_aliases:
            alias_rows = self.execute(
                """SELECT * FROM entities
                   WHERE user_id=? AND deleted_at IS NULL AND canonical=1
                     AND aliases_json NOT IN ('[]', '', 'null')""",
                (user_id,),
            ).fetchall()
            wanted_set = set(wanted)
            for row in alias_rows:
                item = dict(row)
                entity_id = str(item["id"])
                if entity_id in by_id:
                    continue
                aliases = _json_load(item.get("aliases_json"), [])
                if any(normalize_entity_name(str(alias)) in wanted_set for alias in aliases):
                    by_id[entity_id] = item

        return list(by_id.values())

    def iter_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        page_size: int = 1000,
        include_merged: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Walk every matching entity. No silent alphabetical ceiling.

        ``list_entities`` is a page with a hard cap of 5000 and a warning when the
        page fills. Callers that truly need the whole graph (token-overlap search)
        must page explicitly — otherwise the tail of the alphabet stops existing.
        """
        where, params = self._entity_filter(user_id, entity_type, include_merged=include_merged)
        bounded = max(1, min(int(page_size), 5000))
        offset = 0
        while True:
            rows = self.execute(
                f"SELECT * FROM entities WHERE {where} "  # nosec B608
                "ORDER BY name COLLATE NOCASE, id LIMIT ? OFFSET ?",
                (*params, bounded, offset),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                yield dict(row)
            if len(rows) < bounded:
                break
            offset += len(rows)

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

    def undelete_entity(self, entity_id: str, user_id: str) -> dict[str, Any] | None:
        """Вернуть мягко удалённую сущность в граф — новой версией, не перемоткой.

        Удаление называлось мягким и было мягким по букве (строка с `deleted_at`
        остаётся), но обратного хода не существовало НИ ОДНОГО: `restore` отвечал
        404 (сущность считается несуществующей), `PATCH` — 200 с `entity: null`,
        карточка по имени не открывалась. То есть узел с его связями выпадал из
        графа до ручной правки SQLite, а кнопка в чате обещала обратимость.

        Надгробие СЛИЯНИЯ этим путём не воскрешается: у него есть свой обратный
        ход (`unmerge_entities`), и поднять его отдельно значило бы получить две
        живые сущности там, где человек попросил одну.
        """
        with self.transaction():
            current = self.get_entity(entity_id, user_id)
            if not current or not current.get("deleted_at"):
                return None
            if current.get("merged_into_id"):
                raise ValueError("Это след слияния, а не удалённый объект: возвращают его разъединением")
            entity = Entity(
                id=str(current["id"]),
                user_id=str(current["user_id"]),
                name=str(current.get("name") or ""),
                entity_type=EntityType(str(current.get("entity_type") or EntityType.OTHER.value)),
                aliases_json=_json_load(current.get("aliases_json"), []),
                description=str(current.get("description") or ""),
                metadata_json=_json_load(current.get("metadata_json"), {}),
                canonical=True,
                merged_into_id=None,
                version=int(current.get("version", 1)),
                created_at=str(current.get("created_at") or utc_now()),
                updated_at=utc_now(),
                deleted_at=None,
            )
            self.update_entity(entity)
        return self.get_entity(entity_id, user_id)

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

    def count_relation_candidates_for_entity(
        self, user_id: str, entity_id: str, *, status: str | None = "suggested"
    ) -> int:
        """How many relation candidates (either side) touch this one entity.

        Reuses `_relation_candidate_filter`/`_RELATION_CANDIDATE_FROM` — same
        query shape as `count_relation_candidates`, one extra predicate. Backs
        the object-view "N connections awaiting review" line: a profile that
        only showed CONFIRMED relations would silently hide a queue the owner
        might not know exists for this specific entity.
        """
        clauses, params = self._relation_candidate_filter(user_id, status)
        clauses.append("(c.source_entity_id=? OR c.target_entity_id=?)")
        params.extend([entity_id, entity_id])
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
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Кандидатуры на слияние — страницей, а не всей таблицей.

        Лимита не было вовсе: ни параметра, ни клампа. Фоновый обход дедупа копит
        кандидатуры сам, и на 5000 сущностях один проход дал 4012 строк — все они
        поднимались в память и уходили в ответ восьмимегабайтным JSON. Хвост
        `created_at, id` обязателен по той же причине, что и везде здесь: уверенность
        и отметка времени у пачки совпадают, и без него страницы разъезжаются.
        """
        bounded = max(1, min(int(limit), 1000))
        if status:
            rows = self.execute(
                """SELECT * FROM entity_resolution_candidates WHERE user_id=? AND status=?
                   ORDER BY confidence DESC, created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (user_id, enum_value(status), bounded, max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                """SELECT * FROM entity_resolution_candidates WHERE user_id=?
                   ORDER BY confidence DESC, created_at DESC, id DESC LIMIT ? OFFSET ?""",
                (user_id, bounded, max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_resolution_candidates(self, user_id: str, status: ResolutionStatus | None = None) -> int:
        """Сколько их всего — чтобы страница не выдавалась за полный объём."""
        if status:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM entity_resolution_candidates WHERE user_id=? AND status=?",
                (user_id, enum_value(status)),
            ).fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM entity_resolution_candidates WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

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

            recorded_merge_id = ""
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

            # Record every transferred link BEFORE INSERT OR IGNORE collapses
            # overlaps: a document already linked to the target would leave one
            # row and no way to know the source also had it.
            source_links = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                    (user_id, source_id),
                ).fetchall()
            ]
            target_ko_ids = {
                str(row["knowledge_object_id"])
                for row in conn.execute(
                    "SELECT knowledge_object_id FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                    (user_id, target_id),
                ).fetchall()
            }
            links_moved: list[dict[str, Any]] = []
            links_suppressed: list[dict[str, Any]] = []
            for link in source_links:
                ko_id = str(link["knowledge_object_id"])
                if ko_id in target_ko_ids:
                    links_suppressed.append(link)
                    continue
                new_link_id = new_id("kel")
                conn.execute(
                    """INSERT INTO knowledge_entity_links
                       (id, user_id, knowledge_object_id, entity_id, status, confidence,
                        evidence_json, created_at, reviewed_at, reviewed_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_link_id,
                        user_id,
                        ko_id,
                        target_id,
                        link["status"],
                        link["confidence"],
                        link["evidence_json"],
                        link["created_at"],
                        link["reviewed_at"],
                        link["reviewed_by"],
                    ),
                )
                links_moved.append({**link, "target_link_id": new_link_id})
            conn.execute(
                "DELETE FROM knowledge_entity_links WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            )

            primary_rows = conn.execute(
                "SELECT id FROM knowledge_objects WHERE user_id=? AND entity_id=?",
                (user_id, source_id),
            ).fetchall()
            primary_moved = [str(row["id"]) for row in primary_rows]
            if primary_moved:
                conn.execute(
                    "UPDATE knowledge_objects SET entity_id=?, updated_at=? WHERE user_id=? AND entity_id=?",
                    (target_id, now, user_id, source_id),
                )

            relations = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM relations WHERE user_id=? AND deleted_at IS NULL
                       AND (source_entity_id=? OR target_entity_id=?)""",
                    (user_id, source_id, source_id),
                ).fetchall()
            ]
            relations_transfer: list[dict[str, Any]] = []
            for relation in relations:
                conn.execute("DELETE FROM relations WHERE id=?", (relation["id"],))
                new_source = (
                    target_id if relation["source_entity_id"] == source_id else relation["source_entity_id"]
                )
                new_target = (
                    target_id if relation["target_entity_id"] == source_id else relation["target_entity_id"]
                )
                if new_source == new_target:
                    relations_transfer.append({"original": relation, "fate": "self_loop_dropped"})
                    continue
                existing = conn.execute(
                    """SELECT id FROM relations
                       WHERE user_id=? AND source_entity_id=? AND target_entity_id=?
                         AND relation_type=? AND deleted_at IS NULL LIMIT 1""",
                    (user_id, new_source, new_target, relation["relation_type"]),
                ).fetchone()
                if existing:
                    relations_transfer.append({"original": relation, "fate": "suppressed_duplicate"})
                    continue
                conn.execute(
                    """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
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
                relations_transfer.append(
                    {
                        "original": relation,
                        "fate": "moved",
                        "rewritten": {
                            "id": relation["id"],
                            "source_entity_id": new_source,
                            "target_entity_id": new_target,
                        },
                    }
                )

            conn.execute(
                """UPDATE entities SET merged_into_id=?, canonical=0, deleted_at=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (target_id, now, now, source_id, user_id),
            )
            # Какие именно строки очереди закрывает это слияние — записывается ДО
            # апдейта, иначе откатывать нечего: без списка `unmerge` оставлял пару
            # в статусе 'merged' навсегда. А `store_resolution_candidate` по
            # правилу «решённое человеком не возвращается в очередь» отдаёт
            # существующую строку не трогая, поэтому та же пара больше не
            # предлагалась НИКОГДА и слить её заново было нечем: прямого «слей вот
            # эти две» в системе нет, все пути идут через кандидатуру.
            closed_candidates = [
                str(item["id"])
                for item in conn.execute(
                    """SELECT id FROM entity_resolution_candidates
                       WHERE user_id=? AND status='suggested'
                         AND (entity_a_id=? OR entity_b_id=?)""",
                    (user_id, source_id, source_id),
                ).fetchall()
            ]
            conn.execute(
                """UPDATE entity_resolution_candidates SET status='merged', resolved_at=?, resolved_by=?
                   WHERE user_id=? AND status='suggested'
                     AND (entity_a_id=? OR entity_b_id=?)""",
                (now, merged_by or user_id, user_id, source_id, source_id),
            )
            transfer = {
                "links_moved": links_moved,
                "links_suppressed": links_suppressed,
                "primary_moved": primary_moved,
                "relations": relations_transfer,
                "closed_candidates": closed_candidates,
            }
            merge_id = new_id("merge")
            conn.execute(
                """INSERT INTO entity_merge_history(id, user_id, source_entity_id, target_entity_id,
                   source_snapshot_json, target_before_json, target_after_json, transfer_json,
                   merged_by, created_at, undone_at, undone_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (
                    merge_id,
                    user_id,
                    source_id,
                    target_id,
                    _snapshot(source),
                    _snapshot(target),
                    _snapshot(target_after),
                    json.dumps(transfer, ensure_ascii=False),
                    merged_by or user_id,
                    now,
                ),
            )
            recorded_merge_id = merge_id
        result = self.get_entity(target_id, user_id) or {}
        result["_merge_id"] = recorded_merge_id
        return result

    def get_merge_history(self, merge_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM entity_merge_history WHERE id=? AND user_id=?",
            (merge_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_merge_history(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM entity_merge_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [dict(row) for row in rows]

    def unmerge_entities(
        self,
        user_id: str,
        merge_id: str,
        *,
        undone_by: str | None = None,
    ) -> dict[str, Any]:
        """Reverse one accepted merge using the transfer set recorded at merge time.

        Snapshots alone are not enough: links moved with INSERT OR IGNORE, so a
        document both sides already shared becomes a single target row and loses
        its source origin. ``transfer_json`` records every moved, suppressed and
        rewritten edge; without it undo would invent ownership.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM entity_merge_history WHERE id=? AND user_id=?",
                (merge_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Merge history entry not found")
            history = dict(row)
            if history.get("undone_at"):
                raise ValueError("Merge has already been undone")
            transfer = _json_load(history.get("transfer_json"), {})
            if not isinstance(transfer, dict) or not transfer:
                raise ValueError(
                    "Merge has no transfer record and cannot be undone honestly "
                    "(merged before transfer_json existed)"
                )

            source_id = str(history["source_entity_id"])
            target_id = str(history["target_entity_id"])
            source_snap = _json_load(history.get("source_snapshot_json"), {})
            target_before = _json_load(history.get("target_before_json"), {})
            if not source_snap or not target_before:
                raise ValueError("Merge snapshots are incomplete")

            source_now = self.get_entity(source_id, user_id)
            target_now = self.get_entity(target_id, user_id)
            if not source_now or not target_now:
                raise ValueError("Merged entities are no longer present")
            if str(source_now.get("merged_into_id") or "") != target_id:
                raise ValueError("Source entity is no longer recorded as merged into this target")
            if target_now.get("deleted_at"):
                raise ValueError("Target entity has been deleted; refuse to unmerge onto a tombstone")

            now = utc_now()

            # 1. Restore source as a live canonical node from its pre-merge snapshot.
            conn.execute(
                """UPDATE entities SET name=?, normalized_name=?, entity_type=?, aliases_json=?,
                   description=?, metadata_json=?, canonical=1, merged_into_id=NULL,
                   version=?, updated_at=?, deleted_at=NULL
                   WHERE id=? AND user_id=?""",
                (
                    source_snap.get("name") or source_now.get("name"),
                    normalize_entity_name(str(source_snap.get("name") or source_now.get("name") or "")),
                    source_snap.get("entity_type") or source_now.get("entity_type"),
                    source_snap.get("aliases_json")
                    if isinstance(source_snap.get("aliases_json"), str)
                    else json.dumps(_json_load(source_snap.get("aliases_json"), []), ensure_ascii=False),
                    source_snap.get("description") or "",
                    source_snap.get("metadata_json")
                    if isinstance(source_snap.get("metadata_json"), str)
                    else json.dumps(_json_load(source_snap.get("metadata_json"), {}), ensure_ascii=False),
                    int(source_snap.get("version") or source_now.get("version") or 1),
                    now,
                    source_id,
                    user_id,
                ),
            )

            # 2. Restore target aliases/description to the pre-merge state. Version
            # advances: the undo is a new edit, not a silent rewrite of history.
            restored_aliases = target_before.get("aliases_json")
            if not isinstance(restored_aliases, str):
                restored_aliases = json.dumps(_json_load(restored_aliases, []), ensure_ascii=False)
            restored_meta = target_before.get("metadata_json")
            if not isinstance(restored_meta, str):
                restored_meta = json.dumps(_json_load(restored_meta, {}), ensure_ascii=False)
            target_version = int(target_now.get("version") or 1) + 1
            conn.execute(
                """UPDATE entities SET aliases_json=?, description=?, metadata_json=?,
                   version=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (
                    restored_aliases,
                    target_before.get("description") or "",
                    restored_meta,
                    target_version,
                    now,
                    target_id,
                    user_id,
                ),
            )
            target_restored = dict(target_now)
            target_restored["aliases_json"] = restored_aliases
            target_restored["description"] = target_before.get("description") or ""
            target_restored["metadata_json"] = restored_meta
            target_restored["version"] = target_version
            target_restored["updated_at"] = now
            self._store_entity_version(conn, target_restored)

            # 3. Links that were newly created on the target for the source's
            # exclusive documents: remove from target, put back on source.
            for link in transfer.get("links_moved") or []:
                if not isinstance(link, dict):
                    continue
                target_link_id = link.get("target_link_id")
                if target_link_id:
                    conn.execute(
                        "DELETE FROM knowledge_entity_links WHERE id=? AND user_id=? AND entity_id=?",
                        (target_link_id, user_id, target_id),
                    )
                else:
                    conn.execute(
                        """DELETE FROM knowledge_entity_links
                           WHERE user_id=? AND entity_id=? AND knowledge_object_id=?""",
                        (user_id, target_id, link.get("knowledge_object_id")),
                    )
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links
                       (id, user_id, knowledge_object_id, entity_id, status, confidence,
                        evidence_json, created_at, reviewed_at, reviewed_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        link.get("id") or new_id("kel"),
                        user_id,
                        link["knowledge_object_id"],
                        source_id,
                        link.get("status") or "accepted",
                        link.get("confidence") if link.get("confidence") is not None else 1.0,
                        link.get("evidence_json") or "{}",
                        link.get("created_at") or now,
                        link.get("reviewed_at"),
                        link.get("reviewed_by"),
                    ),
                )

            # 4. Overlapping documents: target kept its own row; only the source
            # side is missing and must be restored from the recorded original.
            for link in transfer.get("links_suppressed") or []:
                if not isinstance(link, dict):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links
                       (id, user_id, knowledge_object_id, entity_id, status, confidence,
                        evidence_json, created_at, reviewed_at, reviewed_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        link.get("id") or new_id("kel"),
                        user_id,
                        link["knowledge_object_id"],
                        source_id,
                        link.get("status") or "accepted",
                        link.get("confidence") if link.get("confidence") is not None else 1.0,
                        link.get("evidence_json") or "{}",
                        link.get("created_at") or now,
                        link.get("reviewed_at"),
                        link.get("reviewed_by"),
                    ),
                )

            # 5. Primary knowledge_objects.entity_id pointer, if any.
            for knowledge_id in transfer.get("primary_moved") or []:
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                       WHERE id=? AND user_id=? AND entity_id=?""",
                    (source_id, now, knowledge_id, user_id, target_id),
                )

            # 6. Relations: reverse each recorded fate.
            for item in transfer.get("relations") or []:
                if not isinstance(item, dict):
                    continue
                original = item.get("original") or {}
                if not isinstance(original, dict) or not original.get("id"):
                    continue
                fate = str(item.get("fate") or "")
                if fate == "moved":
                    rewritten = item.get("rewritten") or {}
                    conn.execute(
                        "DELETE FROM relations WHERE id=? AND user_id=?",
                        (rewritten.get("id") or original["id"], user_id),
                    )
                # self_loop_dropped / suppressed_duplicate: nothing on target to remove
                conn.execute(
                    """INSERT OR IGNORE INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        original["id"],
                        user_id,
                        original["source_entity_id"],
                        original["target_entity_id"],
                        original["relation_type"],
                        original.get("weight") if original.get("weight") is not None else 1.0,
                        original.get("metadata_json") or "{}",
                        original.get("created_at") or now,
                    ),
                )

            # 7. Очередь: пары, которые закрыло это слияние, возвращаются на
            # разбор. Иначе откат хоронил их навсегда — строка оставалась
            # 'merged', повторное предложение той же пары гасилось правилом
            # «решённое человеком durable», а другого пути слить две сущности в
            # системе нет. Возвращаются ТОЛЬКО те строки, что закрыло именно это
            # слияние, и только если человек не решил по ним что-то ещё позже.
            # 7. Очередь: пары, которые закрыло это слияние, возвращаются на
            # разбор. Иначе откат хоронил их навсегда — строка оставалась
            # 'merged', повторное предложение той же пары гасилось правилом
            # «решённое человеком durable», а другого пути слить две сущности в
            # системе нет. Возвращаются ТОЛЬКО те строки, что закрыло именно это
            # слияние.
            for candidate_id in transfer.get("closed_candidates") or []:
                conn.execute(
                    """UPDATE entity_resolution_candidates
                       SET status='suggested', resolved_at=NULL, resolved_by=NULL
                       WHERE id=? AND user_id=? AND status='merged'""",
                    (str(candidate_id), user_id),
                )

            conn.execute(
                """UPDATE entity_merge_history SET undone_at=?, undone_by=?
                   WHERE id=? AND user_id=? AND undone_at IS NULL""",
                (now, undone_by or user_id, merge_id, user_id),
            )

        return {
            "merge_id": merge_id,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "source": self.get_entity(source_id, user_id),
            "target": self.get_entity(target_id, user_id),
            "undone_at": now,
        }
