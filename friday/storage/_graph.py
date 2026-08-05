"""Storage methods for entities, relations, aliases and resolution candidates.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import date

from friday.storage._base import (
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

_GRAPH_DATE_RE = re.compile(r"^\d{4}(?:[-./]\d{1,2}(?:[-./]\d{1,2})?)?$")


def _normalize_graph_date(value: str, field: str, *, allow_empty: bool = True) -> str:
    """One comparable calendar representation for relation valid-time boundaries."""
    cleaned = str(value or "").strip()
    if not cleaned and allow_empty:
        return ""
    if not _GRAPH_DATE_RE.fullmatch(cleaned):
        raise ValueError(f"{field}: нужна календарная дата ГГГГ, ГГГГ-ММ или ГГГГ-ММ-ДД")
    parts = re.split(r"[-./]", cleaned)
    try:
        numbers = [int(part) for part in parts]
        if len(numbers) == 1:
            return date(numbers[0], 1, 1).isoformat()
        if len(numbers) == 2:
            return date(numbers[0], numbers[1], 1).isoformat()
        return date(numbers[0], numbers[1], numbers[2]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field}: такой календарной даты нет") from exc


class GraphMixin(StorageShared):
    def list_part_of_relations(self, user_id: str) -> list[dict[str, Any]]:
        """Active PART_OF edges; source is the child, target the parent."""
        rows = self.execute(
            "SELECT source_entity_id, target_entity_id, weight FROM relations"
            " WHERE user_id=? AND relation_type=? AND deleted_at IS NULL AND valid_to IS NULL"
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

    def graph_overview(
        self,
        user_id: str,
        *,
        limit: int = 120,
        entity_types: Sequence[str] | None = None,
        relation_types: Sequence[str] | None = None,
        only_relations: bool = False,
        min_weight: int = 1,
        min_confidence: float = 0.0,
        as_of: str = "",
        search: str = "",
        hide_isolates: bool = False,
    ) -> dict[str, Any]:
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

        Фильтры сужают ОТБОР УЗЛОВ, а не только рисование: обрезав картинку в
        браузере, мы показали бы «сто самых связанных сущностей, из которых нужного
        типа оказалось три», выдавая это за три сущности этого типа. `total` при
        этом продолжает считать весь граф — это свойство архива, а не запроса.
        """
        bounded = max(1, min(int(limit), 500))
        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        conditions = ["e.user_id = ?", "e.deleted_at IS NULL", "e.merged_into_id IS NULL"]
        parameters: list[Any] = [user_id]
        wanted_types = [str(item).strip() for item in (entity_types or []) if str(item).strip()]
        if wanted_types:
            conditions.append(f"e.entity_type IN ({','.join('?' * len(wanted_types))})")
            parameters.extend(wanted_types)
        needle = str(search or "").strip()
        if needle:
            conditions.append("e.name LIKE ? ESCAPE '\\'")
            escaped = needle.replace("%", r"\%").replace("_", r"\_")
            parameters.append(f"%{escaped}%")
        rows = self.execute(
            f"""SELECT e.id, e.name, e.entity_type, COUNT(l.knowledge_object_id) AS knowledge_count
               FROM entities e
               JOIN knowledge_entity_links l
                 ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status = 'accepted'
               WHERE {" AND ".join(conditions)}
               GROUP BY e.id
               ORDER BY knowledge_count DESC, e.name COLLATE NOCASE, e.id
               LIMIT ?""",  # nosec B608 — условия собраны из литералов, значения связаны
            (*parameters, bounded),
        ).fetchall()
        nodes = [dict(row) for row in rows]
        ids = [str(node["id"]) for node in nodes]
        if not ids:
            return {"nodes": [], "edges": [], "shown": 0, "total": self.count_entities(user_id)}

        placeholders = ",".join("?" * len(ids))
        # Совместная встречаемость считается ТОЛЬКО между показанными узлами: ребро в
        # невидимый узел рисовать некуда, а считать его в статистику — врать.
        cooccurrence: list[Any] = []
        if not only_relations:
            floor = max(1, int(min_weight))
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
                    HAVING weight >= ?
                    ORDER BY weight DESC
                    LIMIT 800""",  # nosec B608
                (user_id, *ids, *ids, floor),
            ).fetchall()
        relation_conditions = [
            "user_id = ?",
            f"source_entity_id IN ({placeholders})",
            f"target_entity_id IN ({placeholders})",
            "deleted_at IS NULL",
        ]
        relation_parameters: list[Any] = [user_id, *ids, *ids]
        if as_of:
            # «А как было тогда»: связь берётся, если на ту дату она уже началась
            # и ещё не кончилась. Пустое начало не исключает — «неизвестно, когда
            # началось» это не «началось позже». То же правило, что в обходе
            # окрестности узла: две картины одной даты обязаны совпадать.
            relation_conditions.append("(valid_from = '' OR valid_from <= ?)")
            relation_parameters.append(as_of)
            relation_conditions.append("(valid_to IS NULL OR valid_to > ?)")
            relation_parameters.append(as_of)
        else:
            # Отменённая связь на общей картине не рисуется: «служит в в/ч А» и
            # «служит в в/ч Б» рядом читаются как одновременные.
            relation_conditions.append("valid_to IS NULL")
        floor_confidence = max(0.0, min(float(min_confidence), 1.0))
        if floor_confidence > 0:
            relation_conditions.append("weight >= ?")
            relation_parameters.append(floor_confidence)
        wanted_relations = [str(item).strip() for item in (relation_types or []) if str(item).strip()]
        if wanted_relations:
            relation_conditions.append(f"relation_type IN ({','.join('?' * len(wanted_relations))})")
            relation_parameters.extend(wanted_relations)
        relations = self.execute(
            f"""SELECT source_entity_id AS source, target_entity_id AS target, relation_type, weight
                FROM relations
                WHERE {" AND ".join(relation_conditions)}
                LIMIT 800""",  # nosec B608
            tuple(relation_parameters),
        ).fetchall()
        edges = [{**dict(row), "kind": "cooccurrence"} for row in cooccurrence]
        edges.extend({**dict(row), "kind": "relation"} for row in relations)
        if hide_isolates:
            # Узел без единого ребра занимает место и ничего не рассказывает. Убирать
            # его — решение ЗРИТЕЛЯ, поэтому по умолчанию он на месте, а `shown`
            # ниже считается после отсева, чтобы подпись не расходилась с картинкой.
            connected = {str(edge["source"]) for edge in edges} | {str(edge["target"]) for edge in edges}
            nodes = [node for node in nodes if str(node["id"]) in connected]
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

    def count_entities_by_type(self, user_id: str, *, include_merged: bool = False) -> dict[str, int]:
        """Сколько сущностей каждого вида — агрегатом, а не подсчётом страницы.

        «Здоровье графа» считало это питоном по выборке с потолком 5000: на
        большем корпусе разбивка застывала и продолжала выглядеть точной, а
        `entity_count` рядом с ней считался честным агрегатом — два числа в одной
        панели расходились молча.

        Условия те же, что у `count_entities`, из одного помощника: иначе «всего»
        и сумма по видам разойдутся при первой же правке фильтра.
        """
        where, params = self._entity_filter(user_id, None, include_merged=include_merged)
        rows = self.execute(
            f"SELECT entity_type, COUNT(*) AS count FROM entities WHERE {where} "  # nosec B608
            "GROUP BY entity_type",
            tuple(params),
        ).fetchall()
        return {str(row["entity_type"] or ""): int(row["count"] or 0) for row in rows}

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

    def people_whose_name_starts_with(
        self, user_id: str, stems: Sequence[str], *, limit: int = 5
    ) -> list[str]:
        """Имена людей графа, начинающиеся с любой из этих основ.

        Существует ради одного вопроса, у которого цена ошибки высокая: не уходит
        ли в чужой поисковик фамилия сотрудника. Прежняя проверка звала
        `search_entities` и находила ТОЛЬКО точную форму — замерено на стенде:
        «Хасанов» находился, «Хасанова», «Хасанову», «Хасановым», «Маратовича»
        не находились ни одна. А спрашивают как раз «что известно про Хасанова».

        Поэтому сравнивается ОСНОВА: у русских фамилий меняется окончание, а
        начало стоит на месте. `LIKE 'основа%'` по индексу
        `(user_id, entity_type, normalized_name)` — префиксный поиск, который
        этот индекс и обслуживает.

        Ошибка в сторону «нашли лишнее» здесь дешевле: человек увидит отказ сразу
        и переспросит, а ушедшую фамилию не вернуть — в журнале остаётся хеш.
        """
        wanted = [normalize_entity_name(str(item or "")) for item in stems]
        clean = [item for item in dict.fromkeys(wanted) if len(item) >= 4]
        if not clean:
            return []
        conditions = " OR ".join("normalized_name LIKE ?" for _ in clean)
        rows = self.execute(
            "SELECT name FROM entities WHERE user_id=? AND entity_type='person' "
            f"AND deleted_at IS NULL AND canonical=1 AND ({conditions}) "  # nosec B608
            "LIMIT ?",
            (user_id, *[f"{item}%" for item in clean], max(1, min(int(limit), 50))),
        ).fetchall()
        return [str(row["name"] or "") for row in rows]

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
            "ORDER BY t.occurred_at ASC, e.name ASC, e.id ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_events_in_range(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        mine: str = "",
    ) -> int:
        """Сколько событий попадает в окно — независимо от размера страницы.

        `len(items)` при выборке с потолком отвечает «сколько я попросил». В
        планах человека это особенно дорого: «на неделю запланировано 100» при
        потолке ровно 100 читается как факт о его календаре.

        `mine` — учётка человека; при ней считаются его напоминания плюс события
        без автора (они из документов и общие). Условия повторяют
        `list_events_in_range`, включая отбор чужих напоминаний, который тот
        делает уже в питоне: два числа обязаны отвечать на один вопрос.
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
        if mine:
            clauses.append("(COALESCE(t.source,'') NOT LIKE 'reminder:%' OR COALESCE(t.source,'') = ?)")
            params.append(f"reminder:{mine}")
        row = self.execute(
            "SELECT COUNT(*) AS count FROM entity_time t JOIN entities e ON e.id=t.entity_id "
            f"WHERE {' AND '.join(clauses)}",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def list_relation_changes_in_range(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Valid-time boundary changes for one tenant, without relation metadata.

        A relation may contribute two immutable timeline rows: ``confirmed`` at a
        known ``valid_from`` and ``ended`` at ``valid_to``.  Transaction timestamps
        are evidence carried by a row, never a substitute for either boundary.
        """

        bounded_limit = max(1, min(int(limit), 2000))
        rows = self.execute(
            """WITH relation_changes AS (
                   SELECT r.id AS relation_id, r.relation_type AS relation_type,
                          r.source_entity_id AS source_entity_id, s.name AS source_name,
                          r.target_entity_id AS target_entity_id, t.name AS target_name,
                          r.valid_from AS valid_from, r.valid_to AS valid_to,
                          r.created_at AS created_at, r.invalidated_at AS invalidated_at,
                          r.superseded_by AS superseded_by, r.valid_from AS at,
                          'confirmed' AS boundary
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND r.deleted_at IS NULL AND r.valid_from <> ''
                     AND (? IS NULL OR r.valid_from >= ?)
                     AND (? IS NULL OR r.valid_from <= ?)
                   UNION ALL
                   SELECT r.id AS relation_id, r.relation_type AS relation_type,
                          r.source_entity_id AS source_entity_id, s.name AS source_name,
                          r.target_entity_id AS target_entity_id, t.name AS target_name,
                          r.valid_from AS valid_from, r.valid_to AS valid_to,
                          r.created_at AS created_at, r.invalidated_at AS invalidated_at,
                          r.superseded_by AS superseded_by, r.valid_to AS at,
                          'ended' AS boundary
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND r.valid_to IS NOT NULL AND r.valid_to <> ''
                     AND (? IS NULL OR r.valid_to >= ?)
                     AND (? IS NULL OR r.valid_to <= ?)
               )
               SELECT relation_id, relation_type, source_entity_id, source_name,
                      target_entity_id, target_name, valid_from, valid_to, created_at,
                      invalidated_at, superseded_by, at, boundary
               FROM relation_changes
               ORDER BY at ASC,
                        CASE boundary WHEN 'confirmed' THEN 0 ELSE 1 END ASC,
                        relation_type ASC, source_name ASC, target_name ASC, relation_id ASC
               LIMIT ?""",
            (
                user_id,
                start,
                start,
                end,
                end,
                user_id,
                start,
                start,
                end,
                end,
                bounded_limit,
            ),
        ).fetchall()
        changes: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            changes.append(
                {
                    "kind": "relation",
                    "at": item["at"],
                    "boundary": item["boundary"],
                    "relation_id": item["relation_id"],
                    "relation_type": item["relation_type"],
                    "source": {
                        "id": item["source_entity_id"],
                        "name": item["source_name"],
                    },
                    "target": {
                        "id": item["target_entity_id"],
                        "name": item["target_name"],
                    },
                    "valid_from": item["valid_from"],
                    "valid_to": item["valid_to"],
                    "created_at": item["created_at"],
                    "invalidated_at": item["invalidated_at"],
                    "superseded_by": item["superseded_by"],
                }
            )
        return changes

    def count_relation_changes_in_range(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> int:
        """Exact number of relation valid-time boundaries in the same window."""

        row = self.execute(
            """SELECT COUNT(*) AS count
               FROM (
                   SELECT r.id
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND r.deleted_at IS NULL AND r.valid_from <> ''
                     AND (? IS NULL OR r.valid_from >= ?)
                     AND (? IS NULL OR r.valid_from <= ?)
                   UNION ALL
                   SELECT r.id
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND r.valid_to IS NOT NULL AND r.valid_to <> ''
                     AND (? IS NULL OR r.valid_to >= ?)
                     AND (? IS NULL OR r.valid_to <= ?)
               )""",
            (
                user_id,
                start,
                start,
                end,
                end,
                user_id,
                start,
                start,
                end,
                end,
            ),
        ).fetchone()
        return int(row["count"] if row else 0)

    def create_relation(self, relation: Relation) -> Relation:
        if relation.source_entity_id == relation.target_entity_id:
            raise ValueError("Self-relations are not allowed")
        relation_weight = float(relation.weight)
        if not math.isfinite(relation_weight) or not 0.0 <= relation_weight <= 1.5:
            raise ValueError("Relation weight must be a finite number between 0 and 1.5")
        relation.weight = relation_weight
        relation.valid_from = _normalize_graph_date(relation.valid_from, "valid_from")
        if relation.valid_to:
            relation.valid_to = _normalize_graph_date(relation.valid_to, "valid_to")
            if relation.valid_from and relation.valid_to < relation.valid_from:
                raise ValueError("valid_to не может предшествовать valid_from")
        source = self.get_entity(relation.source_entity_id, relation.user_id)
        target = self.get_entity(relation.target_entity_id, relation.user_id)
        if not source or not target or source.get("deleted_at") or target.get("deleted_at"):
            raise ValueError("Both entities must belong to the same user")
        with self.transaction() as conn:
            try:
                conn.execute(
                    """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at,
                       valid_from, valid_to, invalidated_at, superseded_by)
                       VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                       :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                       :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
                    relation.to_row(),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """SELECT id FROM relations WHERE user_id=? AND source_entity_id=?
                       AND target_entity_id=? AND relation_type=?
                       AND deleted_at IS NULL AND valid_to IS NULL""",
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

    def invalidate_relation(
        self,
        user_id: str,
        relation_id: str,
        *,
        valid_to: str = "",
        superseded_by: str = "",
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Объявить связь недействующей, не стирая её.

        Два времени, и они разные. `valid_to` — КОГДА ПЕРЕСТАЛО БЫТЬ ПРАВДОЙ
        (человек переведён в другую часть первого марта); `invalidated_at` —
        КОГДА МЫ ЭТО ЗАПИСАЛИ. Без второго нельзя ответить на вопрос «что система
        считала верным на прошлой неделе», а именно им проверяют, почему она
        тогда так ответила.

        Связь остаётся в таблице. Мягкое удаление говорит «этого не было»,
        а здесь сказано «это было и кончилось» — разные утверждения, и второе
        нельзя выразить первым.
        """

        now = utc_now()
        normalized_valid_to = _normalize_graph_date(valid_to or now[:10], "valid_to", allow_empty=False)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM relations WHERE id=? AND user_id=? AND deleted_at IS NULL",
                (relation_id, user_id),
            ).fetchone()
            if not row:
                return None
            if row["invalidated_at"]:
                # Решение терминально, как у кандидатов: повторная отмена молча
                # переписала бы дату, по которой потом восстанавливают картину.
                raise ValueError("Связь уже объявлена недействующей")
            normalized_valid_from = _normalize_graph_date(str(row["valid_from"] or ""), "valid_from")
            if normalized_valid_from and normalized_valid_to < normalized_valid_from:
                raise ValueError("valid_to не может предшествовать valid_from")
            if superseded_by:
                replacement = conn.execute(
                    "SELECT 1 FROM relations WHERE id=? AND user_id=? AND deleted_at IS NULL",
                    (superseded_by, user_id),
                ).fetchone()
                if not replacement:
                    raise ValueError("Связь-замена не найдена")
            metadata = _json_load(row["metadata_json"], {})
            if reason:
                metadata["invalidation_reason"] = str(reason)[:400]
            conn.execute(
                """UPDATE relations
                   SET valid_to=?, invalidated_at=?, superseded_by=?, metadata_json=?
                   WHERE id=? AND user_id=?""",
                (
                    normalized_valid_to,
                    now,
                    superseded_by or None,
                    json.dumps(metadata, ensure_ascii=False),
                    relation_id,
                    user_id,
                ),
            )
            updated = conn.execute("SELECT * FROM relations WHERE id=?", (relation_id,)).fetchone()
        return dict(updated) if updated else None

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

    def get_entity_relations(
        self,
        entity_id: str,
        user_id: str | None = None,
        *,
        include_invalidated: bool = False,
        as_of: str = "",
    ) -> list[dict[str, Any]]:
        """Связи узла. По умолчанию — только ДЕЙСТВУЮЩИЕ.

        Отменённая связь остаётся в таблице (она была правдой и перестала ею
        быть), но в обычный обход графа не попадает: иначе «служит в в/ч А» и
        «служит в в/ч Б» покажутся одновременными, и картина соврёт.

        `as_of` отвечает на вопрос «а как было тогда»: связь берётся, если на ту
        дату она уже началась и ещё не кончилась. Пустой `valid_from` («начало
        неизвестно») не исключает связь из ответа — неизвестное начало это не
        «началось позже», а отсутствие сведений.
        """

        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        params: list[Any] = [entity_id, entity_id]
        clauses = ["r.deleted_at IS NULL"]
        if user_id is not None:
            clauses.append("r.user_id=?")
            params.append(user_id)
        if as_of:
            clauses.append("(r.valid_from = '' OR r.valid_from <= ?)")
            params.append(as_of)
            clauses.append("(r.valid_to IS NULL OR r.valid_to > ?)")
            params.append(as_of)
        elif not include_invalidated:
            clauses.append("r.valid_to IS NULL")
        # Все предикаты — литералы, значения связаны параметрами.
        query = f"""SELECT r.*, s.name AS source_name, t.name AS target_name
                FROM relations r
                JOIN entities s ON s.id=r.source_entity_id
                JOIN entities t ON t.id=r.target_entity_id
                WHERE (r.source_entity_id=? OR r.target_entity_id=?)
                  AND {" AND ".join(clauses)}
                ORDER BY r.created_at DESC"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_entity_graph(
        self,
        user_id: str,
        entity_id: str,
        depth: int = 2,
        *,
        as_of: str = "",
        entity_types: Sequence[str] = (),
        relation_types: Sequence[str] = (),
        min_weight: float = 0.0,
        min_confidence: float = 0.0,
    ) -> dict[str, Any]:
        """Окрестность узла. `as_of` — «как это выглядело на ту дату».

        Без этого параметра bi-temporal половина схемы 27 остаётся внутренним
        свойством хранилища: поля есть, а спросить «кто командовал в 2024» не
        может ни человек, ни агент — обход всегда идёт по сегодняшней картине.

        Отменённая связь при заданной дате возвращается в картину, если на ту
        дату она была верна: в этом и смысл отличия «кончилось» от «не было».
        """

        root = self.get_entity(entity_id, user_id)
        if not root or root.get("deleted_at"):
            return {"nodes": [], "edges": [], "root": entity_id}
        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        max_depth = max(0, min(depth, 5))
        # Фильтры сужают ОБХОД, а не рисование: отсеяв рёбра после обхода, вид
        # показал бы соседей второго круга, добытых через связь, которую человек
        # только что выключил. До этой правки локальный вид не получал фильтров
        # ВООБЩЕ — человек выбирал «только люди», переключался на окрестность
        # узла и молча получал всё.
        wanted_entities = {str(item).strip() for item in entity_types if str(item).strip()}
        wanted_relations = {str(item).strip() for item in relation_types if str(item).strip()}
        # Порог у связи один — её вес, — но имён у него исторически два. `weight`
        # здесь и есть уверенность связи, а `min_weight` в общей картине означает
        # ДРУГОЕ: число общих документов у совместной встречаемости. Панель звала
        # оба одним органом управления и делила число на 50, чтобы попасть в
        # диапазон 0..1 — то есть человек двигал «общих документов», а получал
        # порог уверенности. Здесь принимаются оба имени, берётся строгое из них.
        floor = max(0.0, float(min_weight), float(min_confidence))
        seen = {entity_id}
        frontier = {entity_id}
        nodes: dict[str, dict[str, Any]] = {entity_id: root}
        edges: dict[str, dict[str, Any]] = {}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for current in frontier:
                for relation in self.get_entity_relations(current, user_id, as_of=as_of):
                    if wanted_relations and str(relation.get("relation_type") or "") not in wanted_relations:
                        continue
                    if floor and float(relation.get("weight") or 0.0) < floor:
                        continue
                    neighbours = []
                    for candidate in (relation["source_entity_id"], relation["target_entity_id"]):
                        if candidate in seen:
                            continue
                        entity = self.get_entity(candidate, user_id)
                        if not entity or entity.get("deleted_at"):
                            continue
                        if wanted_entities and str(entity.get("entity_type") or "") not in wanted_entities:
                            # Узел отсеян — значит и ребро к нему рисовать нечем.
                            continue
                        neighbours.append((candidate, entity))
                    both_known = all(
                        side in seen for side in (relation["source_entity_id"], relation["target_entity_id"])
                    )
                    if not neighbours and not both_known:
                        continue
                    edges[relation["id"]] = relation
                    for candidate, entity in neighbours:
                        seen.add(candidate)
                        nodes[candidate] = entity
                        next_frontier.add(candidate)
            frontier = next_frontier
            if not frontier:
                break
        # `entities` не хранит числа документов — это агрегат по
        # `knowledge_entity_links`. Без него карточка узла в панели показывала
        # «Документов: —», хотя ровно это число стоит в подсказке кружка и задаёт
        # его радиус: два экрана об одной сущности говорили разное.
        counts = self._knowledge_counts_for(user_id, list(nodes))
        enriched = [
            {**node, "knowledge_count": counts.get(str(node.get("id")), 0)} for node in nodes.values()
        ]
        # Дата названа В ОТВЕТЕ: картина «на 2024» и картина «сегодня» выглядят
        # одинаково, и потребитель обязан видеть, какую из двух он получил.
        return {"root": entity_id, "nodes": enriched, "edges": list(edges.values()), "as_of": as_of}

    def _knowledge_counts_for(self, user_id: str, entity_ids: list[str]) -> dict[str, int]:
        """Сколько документов связано с каждой из названных сущностей."""
        if not entity_ids:
            return {}
        holders = ", ".join("?" * len(entity_ids))
        # Условие ровно то же, что в `graph_overview`: только подтверждённые
        # связи и без повторов. Иначе карточка узла и кружок, который её открыл,
        # снова назвали бы разные числа — а правка затевалась именно против
        # этого. Предложенные и отклонённые связи в счёт не идут: подтверждённых
        # 32 189 против 30 прочих, но верность числа от размера не зависит.
        rows = self.execute(
            f"""SELECT entity_id, COUNT(DISTINCT knowledge_object_id) AS total
                FROM knowledge_entity_links
                WHERE user_id = ? AND status = 'accepted' AND entity_id IN ({holders})
                GROUP BY entity_id""",  # noqa: S608
            (user_id, *entity_ids),
        ).fetchall()
        return {str(row["entity_id"]): int(row["total"]) for row in rows}

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
    # Концы связи обязаны быть ЖИВЫ. Проверка стояла при создании кандидата, но
    # между созданием и решением человека проходит время — за него сущность
    # успевают удалить или слить. Воспроизведено: конец удалён, кандидат принят,
    # ребро в никуда создано, а решение терминально и не откатывается.
    #
    # Условие живости стоит в JOIN, а не в WHERE, потому что этот же фрагмент
    # используют и счётчик, и выборка: разъехавшись, они дали бы «17 предложений»
    # над списком из пятнадцати.
    _RELATION_CANDIDATE_FROM = """FROM relation_candidates c
                JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                    AND s.deleted_at IS NULL
                JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                    AND t.deleted_at IS NULL"""

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
                if status == "accepted":
                    # Живость концов проверяется ЗДЕСЬ, а не только при создании
                    # кандидата: между предложением и решением человека проходит
                    # время, и за него сущность успевают удалить или слить.
                    #
                    # Отказ, а не молчаливый пропуск: решение терминально, и, приняв
                    # такую пару, человек получил бы ребро в никуда без возможности
                    # передумать. Кандидат при этом остаётся нерешённым — но из
                    # очереди он уже исчез (см. `_RELATION_CANDIDATE_FROM`), так что
                    # мозолить глаза не будет.
                    for column, side in (("source_entity_id", "начало"), ("target_entity_id", "конец")):
                        alive = conn.execute(
                            "SELECT 1 FROM entities WHERE id=? AND user_id=? AND deleted_at IS NULL",
                            (str(row[column]), user_id),
                        ).fetchone()
                        if not alive:
                            raise ValueError(
                                f"Принять связь нельзя: её {side} больше не существует "
                                "(сущность удалена или слита)"
                            )
                now = utc_now()
                conn.execute(
                    """UPDATE relation_candidates SET status=?, reviewed_at=?, reviewed_by=?
                       WHERE id=? AND user_id=?""",
                    (status, now, reviewed_by, candidate_id, user_id),
                )
                if status == "accepted":
                    evidence = _json_load(row["evidence_json"], {})
                    # С какой даты связь ПОДТВЕРЖДЕНА: собственная дата документа,
                    # который её объявил. Не «началось тогда», а «на эту дату уже
                    # было правдой» — рапорт от 15.03.2024 не утверждает, что
                    # раньше человек в части не служил.
                    #
                    # Без этого вся временная половина схемы 27 остаётся
                    # украшением: замерено на живом графе — 192 связи, и у ВСЕХ
                    # 192 `valid_from` пуст, то есть вопрос «как было в 2024»
                    # отвечать нечем.
                    #
                    # Дата загрузки сюда не годится: архив загружен разом, и
                    # `created_at` полутора тысяч документов говорит о дне
                    # импорта, а не о том, когда это было правдой.
                    valid_from = ""
                    knowledge_id = str(evidence.get("knowledge_object_id") or "")
                    if knowledge_id:
                        source_row = conn.execute(
                            "SELECT json_extract(metadata_json,'$.document_date') AS on_paper "
                            "FROM knowledge_objects WHERE id=? AND user_id=?",
                            (knowledge_id, user_id),
                        ).fetchone()
                        if source_row is not None:
                            valid_from = str(source_row["on_paper"] or "")
                    relation = Relation(
                        id=new_id("rel"),
                        user_id=user_id,
                        source_entity_id=str(row["source_entity_id"]),
                        target_entity_id=str(row["target_entity_id"]),
                        relation_type=str(row["relation_type"]),
                        weight=max(0.1, min(1.0, float(row["confidence"] or 0.5))),
                        valid_from=valid_from,
                        metadata_json={
                            "origin": "review",
                            "source": "reviewed_relation_candidate",
                            "candidate_id": candidate_id,
                            "reviewed_by": reviewed_by,
                            # weight is clamped for ranking; the extractor's raw
                            # confidence stays available as edge provenance.
                            "confidence": float(row["confidence"] or 0.5),
                            "evidence": evidence,
                        },
                    )
                    # Accepting an already represented relation remains idempotent.
                    with suppress(sqlite3.IntegrityError):
                        conn.execute(
                            """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                               relation_type, weight, metadata_json, created_at, deleted_at,
                               valid_from, valid_to, invalidated_at, superseded_by)
                               VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                               :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                               :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
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
        # Состоявшееся слияние не переписывается отказом.
        #
        # Замерено 2026-08-04: у пары в состоянии `merged` вызов с `rejected`
        # менял состояние на «не дубликат», при том что сущности в графе уже
        # слиты. Дальше пара не всплывёт нигде — она решена, — а записанное
        # решение противоречит тому, что произошло на самом деле.
        #
        # Хуже всего дорога: `entity_merge_decide(decision='reject')` НЕ требует
        # подтверждения человеком, в отличие от accept, — то есть переписать
        # состоявшееся слияние могла сама модель.
        #
        # Возврат в очередь (`suggested`) разрешён: это откат слияния, у него своя
        # дорога и свой смысл — пара снова ждёт решения. Разрешён и обратный ход
        # «отказал, потом передумал и слил»: там человек действует осознанно.
        if status is ResolutionStatus.REJECTED:
            query += " AND status <> 'merged'"
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
            aliases = {item.strip() for item in target_aliases if item and item.strip()}
            aliases.update(
                item.strip()
                for item in [*source_aliases, source["name"]]
                if item
                and item.strip()
                and normalize_entity_name(item) != normalize_entity_name(target["name"])
            )
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
            relation_replacements: dict[str, str | None] = {}
            for relation in relations:
                relation_id = str(relation["id"])
                new_source = (
                    target_id if relation["source_entity_id"] == source_id else relation["source_entity_id"]
                )
                new_target = (
                    target_id if relation["target_entity_id"] == source_id else relation["target_entity_id"]
                )
                if new_source == new_target:
                    conn.execute("DELETE FROM relations WHERE id=? AND user_id=?", (relation_id, user_id))
                    relations_transfer.append({"original": relation, "fate": "self_loop_dropped"})
                    relation_replacements[relation_id] = None
                    continue
                # Only two CURRENT intervals conflict. Historical intervals are
                # separate facts and the schema-30 unique index deliberately
                # permits them to share endpoints/type.
                existing = None
                if relation["valid_to"] is None:
                    existing = conn.execute(
                        """SELECT id FROM relations
                           WHERE user_id=? AND source_entity_id=? AND target_entity_id=?
                             AND relation_type=? AND deleted_at IS NULL AND valid_to IS NULL
                             AND id<>? LIMIT 1""",
                        (user_id, new_source, new_target, relation["relation_type"], relation_id),
                    ).fetchone()
                if existing:
                    kept_relation_id = str(existing["id"])
                    conn.execute("DELETE FROM relations WHERE id=? AND user_id=?", (relation_id, user_id))
                    relations_transfer.append(
                        {
                            "original": relation,
                            "fate": "suppressed_duplicate",
                            "kept_relation_id": kept_relation_id,
                        }
                    )
                    relation_replacements[relation_id] = kept_relation_id
                    continue
                # Preserve the row rather than reconstructing it from a column
                # list. Besides keeping both times today, this automatically keeps
                # future provenance columns and lets unmerge retain decisions made
                # after the merge.
                conn.execute(
                    """UPDATE relations SET source_entity_id=?, target_entity_id=?
                       WHERE id=? AND user_id=?""",
                    (new_source, new_target, relation_id, user_id),
                )
                relations_transfer.append(
                    {
                        "original": relation,
                        "fate": "moved",
                        "rewritten": {
                            "id": relation_id,
                            "source_entity_id": new_source,
                            "target_entity_id": new_target,
                        },
                    }
                )
                relation_replacements[relation_id] = relation_id

            # `superseded_by` is an edge between RELATION rows. If a replacement
            # collapsed into a target duplicate (or into a self-loop), preserving
            # its old id would leave a dangling reference. Record every rewrite so
            # unmerge can put the original relation graph back without guessing.
            relation_reference_rewrites: list[dict[str, Any]] = []
            for old_relation_id, replacement_relation_id in relation_replacements.items():
                if old_relation_id == replacement_relation_id:
                    continue
                references = conn.execute(
                    "SELECT id FROM relations WHERE user_id=? AND superseded_by=?",
                    (user_id, old_relation_id),
                ).fetchall()
                for reference in references:
                    reference_id = str(reference["id"])
                    conn.execute(
                        "UPDATE relations SET superseded_by=? WHERE id=? AND user_id=?",
                        (replacement_relation_id, reference_id, user_id),
                    )
                    relation_reference_rewrites.append(
                        {
                            "relation_id": reference_id,
                            "before": old_relation_id,
                            "after": replacement_relation_id,
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
            # Время события переезжает на цель вместе со всем остальным.
            #
            # Замерено 2026-08-04: слияние переносило алиасы, ссылки на документы,
            # связи и кандидатуры — и не трогало `entity_time`. Строка оставалась
            # на слитой сущности, которую читатель ленты уже не видит, и у события
            # просто пропадала дата: «Совещание 12 августа», слитое с дубликатом,
            # переставало напоминать о себе вовсе.
            #
            # Время цели при этом НЕ затирается: если у неё своя дата, она
            # правильнее — это тот узел, который человек оставил. Перенос идёт
            # только в пустое место.
            time_moved = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM entity_time WHERE user_id=? AND entity_id=?",
                    (user_id, source_id),
                ).fetchall()
            ]
            if time_moved:
                conn.execute(
                    """INSERT OR IGNORE INTO entity_time(
                           user_id, entity_id, occurred_at, occurred_end, precision, source, updated_at)
                       SELECT user_id, ?, occurred_at, occurred_end, precision, source, ?
                         FROM entity_time WHERE user_id=? AND entity_id=?""",
                    (target_id, now, user_id, source_id),
                )
                conn.execute(
                    "DELETE FROM entity_time WHERE user_id=? AND entity_id=?",
                    (user_id, source_id),
                )
            transfer = {
                # v2 moves a surviving relation by endpoint-only UPDATE. Older
                # histories reconstructed the row and may therefore carry the
                # temporal defaults that buggy merge wrote at the time.
                "relation_transfer_version": 2,
                "links_moved": links_moved,
                "links_suppressed": links_suppressed,
                "primary_moved": primary_moved,
                "relations": relations_transfer,
                "relation_reference_rewrites": relation_reference_rewrites,
                "closed_candidates": closed_candidates,
                "time_moved": time_moved,
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
            target_after = _json_load(history.get("target_after_json"), {})
            if not source_snap or not target_before or not target_after:
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

            # 2. Reverse only the alias delta introduced by THIS merge.  A target
            # can be edited after merging; restoring the complete before-snapshot
            # used to erase those later aliases, its description and its metadata.
            # If the alias list itself was untouched, preserve the exact old JSON
            # ordering. Otherwise this is a three-way inverse patch: later additions
            # and removals win, while aliases contributed by the merged source leave.
            before_aliases = [
                str(item) for item in _json_load(target_before.get("aliases_json"), []) if str(item).strip()
            ]
            after_aliases = [
                str(item) for item in _json_load(target_after.get("aliases_json"), []) if str(item).strip()
            ]
            current_aliases = [
                str(item) for item in _json_load(target_now.get("aliases_json"), []) if str(item).strip()
            ]
            if current_aliases == after_aliases:
                restored_alias_items = before_aliases
            else:
                merge_added_aliases = set(after_aliases) - set(before_aliases)
                merge_removed_aliases = set(before_aliases) - set(after_aliases)
                restored_alias_items = sorted(
                    (set(current_aliases) - merge_added_aliases) | merge_removed_aliases,
                    key=str.casefold,
                )

            # A later merge may borrow an alias first added by this one.  Its own
            # before/after delta is then empty for that spelling, so removing the
            # earlier bridge out of order would make the still-merged source
            # unreachable.  Refuse that dependency rather than inventing alias
            # ownership or silently breaking the other live merge.
            current_coverage = {
                normalize_entity_name(item)
                for item in [str(target_now.get("name") or ""), *current_aliases]
                if normalize_entity_name(item)
            }
            restored_coverage = {
                normalize_entity_name(item)
                for item in [str(target_now.get("name") or ""), *restored_alias_items]
                if normalize_entity_name(item)
            }
            lost_coverage = current_coverage - restored_coverage
            if lost_coverage:
                other_live_merges = conn.execute(
                    """SELECT id, source_snapshot_json FROM entity_merge_history
                       WHERE user_id=? AND target_entity_id=? AND undone_at IS NULL AND id<>?""",
                    (user_id, target_id, merge_id),
                ).fetchall()
                for other_merge in other_live_merges:
                    other_source = _json_load(other_merge["source_snapshot_json"], {})
                    if not isinstance(other_source, dict):
                        raise ValueError("Another live merge has an invalid source snapshot")
                    other_aliases = _json_load(other_source.get("aliases_json"), [])
                    required_coverage = {
                        normalize_entity_name(str(item))
                        for item in [other_source.get("name") or "", *other_aliases]
                        if normalize_entity_name(str(item))
                    }
                    if lost_coverage & required_coverage:
                        raise ValueError(
                            "Another live merge depends on an alias introduced by this merge; "
                            "undo the dependent merge first"
                        )
            restored_aliases = json.dumps(restored_alias_items, ensure_ascii=False)
            target_version = int(target_now.get("version") or 1) + 1
            conn.execute(
                """UPDATE entities SET aliases_json=?, version=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (
                    restored_aliases,
                    target_version,
                    now,
                    target_id,
                    user_id,
                ),
            )
            target_restored = dict(target_now)
            target_restored["aliases_json"] = restored_aliases
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
            try:
                relation_transfer_version = int(transfer.get("relation_transfer_version") or 1)
            except (TypeError, ValueError):
                relation_transfer_version = 1
            for item in transfer.get("relations") or []:
                if not isinstance(item, dict):
                    continue
                original = item.get("original") or {}
                if not isinstance(original, dict) or not original.get("id"):
                    continue
                fate = str(item.get("fate") or "")
                if fate == "moved":
                    rewritten = item.get("rewritten") or {}
                    relation_id = str(rewritten.get("id") or original["id"])
                    current = conn.execute(
                        """SELECT source_entity_id, target_entity_id, valid_from, valid_to,
                                  invalidated_at, superseded_by
                           FROM relations
                           WHERE id=? AND user_id=?""",
                        (relation_id, user_id),
                    ).fetchone()
                    if not current:
                        raise ValueError("A moved relation is missing; refuse to resurrect it on unmerge")
                    if str(current["source_entity_id"]) != str(
                        rewritten.get("source_entity_id") or ""
                    ) or str(current["target_entity_id"]) != str(rewritten.get("target_entity_id") or ""):
                        raise ValueError("A moved relation changed endpoints; refuse an unsafe unmerge")
                    # Only undo the endpoint rewrite. A human may have ended or
                    # otherwise annotated the relation after merge; reconstructing
                    # the pre-merge row would silently undo that later decision.
                    if relation_transfer_version >= 2:
                        conn.execute(
                            """UPDATE relations SET source_entity_id=?, target_entity_id=?
                               WHERE id=? AND user_id=?""",
                            (
                                original["source_entity_id"],
                                original["target_entity_id"],
                                relation_id,
                                user_id,
                            ),
                        )
                    else:
                        # Legacy merge rebuilt the row without temporal columns.
                        # Restore only values that are STILL the legacy defaults;
                        # a later relation_end decision must win.
                        conn.execute(
                            """UPDATE relations
                               SET source_entity_id=?, target_entity_id=?,
                                   valid_from=?, valid_to=?, invalidated_at=?, superseded_by=?
                               WHERE id=? AND user_id=?""",
                            (
                                original["source_entity_id"],
                                original["target_entity_id"],
                                current["valid_from"] or original.get("valid_from") or "",
                                current["valid_to"]
                                if current["valid_to"] is not None
                                else original.get("valid_to"),
                                current["invalidated_at"]
                                if current["invalidated_at"] is not None
                                else original.get("invalidated_at"),
                                current["superseded_by"]
                                if current["superseded_by"] is not None
                                else original.get("superseded_by"),
                                relation_id,
                                user_id,
                            ),
                        )
                    continue
                # self_loop_dropped / suppressed_duplicate: nothing on target to remove
                conn.execute(
                    """INSERT OR IGNORE INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at,
                       valid_from, valid_to, invalidated_at, superseded_by)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                    (
                        original["id"],
                        user_id,
                        original["source_entity_id"],
                        original["target_entity_id"],
                        original["relation_type"],
                        original.get("weight") if original.get("weight") is not None else 1.0,
                        original.get("metadata_json") or "{}",
                        original.get("created_at") or now,
                        original.get("valid_from") or "",
                        original.get("valid_to"),
                        original.get("invalidated_at"),
                        original.get("superseded_by"),
                    ),
                )

            # Restore only reference rewrites that still have the merge-produced
            # value. A later human decision wins; the guarded predicate prevents
            # unmerge from overwriting it.
            for rewrite in transfer.get("relation_reference_rewrites") or []:
                if not isinstance(rewrite, dict) or not rewrite.get("relation_id"):
                    continue
                after = rewrite.get("after")
                if after is None:
                    conn.execute(
                        """UPDATE relations SET superseded_by=?
                           WHERE id=? AND user_id=? AND superseded_by IS NULL""",
                        (rewrite.get("before"), str(rewrite["relation_id"]), user_id),
                    )
                else:
                    conn.execute(
                        """UPDATE relations SET superseded_by=?
                           WHERE id=? AND user_id=? AND superseded_by=?""",
                        (
                            rewrite.get("before"),
                            str(rewrite["relation_id"]),
                            user_id,
                            str(after),
                        ),
                    )

            # 7. Очередь: пары, которые закрыло это слияние, возвращаются на
            # разбор. Иначе откат хоронил их навсегда — строка оставалась
            # 'merged', повторное предложение той же пары гасилось правилом
            # «решённое человеком durable», а другого пути слить две сущности в
            # системе нет. Возвращаются ТОЛЬКО те строки, что закрыло именно это
            # слияние, и только если человек не решил по ним что-то ещё позже.
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
