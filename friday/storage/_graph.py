"""Storage methods for entities, relations, aliases and resolution candidates.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import cast

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
    contextmanager,
    enum_value,
    json,
    math,
    new_id,
    normalize_entity_name,
    sqlite3,
    suppress,
    utc_now,
)
from friday.storage._privacy import (
    _not_disallowed_private_material_for_person,
    _not_private_bounded_json_dependency,
    _not_private_entity_material_dependency,
    _not_private_knowledge_dependency,
    _not_private_relation_candidate_dependency,
    _not_private_relation_dependency,
    _not_private_reminder_entity,
    _not_private_resolution_candidate_dependency,
)
from friday.storage.models import RelationHistorySnapshotError, normalize_known_at

_GRAPH_DATE_RE = re.compile(r"^\d{4}(?:[-./]\d{1,2}(?:[-./]\d{1,2})?)?$")
_ENTITY_GRAPH_EDGE_BUDGET = 801
_ENTITY_GRAPH_PAGE_SIZE = _ENTITY_GRAPH_EDGE_BUDGET + 1
_GRAPH_RELATION_METADATA_MAX_BYTES = 8_192
_GRAPH_ENTITY_NAME_MAX_CHARS = 240
_GRAPH_ENTITY_DESCRIPTION_MAX_CHARS = 500
_GRAPH_ENTITY_ALIASES_MAX_BYTES = 8_192
_RELATION_CANDIDATE_PAGE_MAX = 501
_MERGE_HISTORY_JSON_MAX_BYTES = 1_048_576
_MERGE_HISTORY_TEXT_BUDGET = 4 * _MERGE_HISTORY_JSON_MAX_BYTES
_MERGE_HISTORY_NESTED_DEPTH = 8
_PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES = 1_048_576
_PUBLIC_ENTITY_VERSION_NESTED_DEPTH = 8
_PUBLIC_ENTITY_VERSION_TEXT_BUDGET = 4 * 1_048_576


def _visible_superseding_relation_id(alias: str = "r") -> str:
    """SQL expression exposing a replacement ID only while both ends stay public."""

    return f"""CASE WHEN EXISTS (
        SELECT 1 FROM relations replacement
        JOIN entities replacement_source
          ON replacement_source.id=replacement.source_entity_id
         AND replacement_source.user_id=replacement.user_id
         AND {_not_private_entity_material_dependency("replacement_source")}
        JOIN entities replacement_target
          ON replacement_target.id=replacement.target_entity_id
         AND replacement_target.user_id=replacement.user_id
         AND {_not_private_entity_material_dependency("replacement_target")}
        WHERE replacement.id={alias}.superseded_by
          AND replacement.user_id={alias}.user_id
          AND {_not_private_relation_dependency("replacement")}
    ) THEN substr({alias}.superseded_by,1,160) ELSE NULL END"""


def _is_private_reminder_entity_id(
    storage: StorageShared,
    entity_id: str,
    user_id: str | None = None,
) -> bool:
    """Whether an entity is a personal reminder, including deleted rows."""

    tenant_clause = " AND user_id=?" if user_id is not None else ""
    params: tuple[Any, ...] = (entity_id, user_id) if user_id is not None else (entity_id,)
    row = storage.execute(
        f"""SELECT 1 FROM entities e
             WHERE e.id=?{tenant_clause.replace("user_id", "e.user_id")}
               AND (EXISTS (SELECT 1 FROM private_entity_owners private_owner
                            WHERE private_owner.entity_id=e.id)
                    OR EXISTS (SELECT 1 FROM entity_time private_time
                               WHERE private_time.entity_id=e.id
                                 AND private_time.source LIKE 'reminder:%'))
             LIMIT 1""",  # nosec B608
        params,
    ).fetchone()
    return row is not None


def _public_entity_version_snapshot(
    storage: StorageShared,
    raw_snapshot: Any,
    *,
    entity_id: str,
    user_id: str,
    version: int,
) -> str | None:
    """Authenticate and deeply inspect an entity history snapshot.

    Entity rows store ``aliases_json`` and ``metadata_json`` as JSON strings
    inside the outer version JSON.  A SQL ``json_tree`` sees those as opaque
    text, and an escaped/non-NFC private name can otherwise be restored after
    its source entity is quarantined.  Decode bounded JSON-in-string recursively
    and fail closed on JSON-shaped corruption.
    """

    snapshot_text = str(raw_snapshot or "")
    if (
        not snapshot_text
        or len(snapshot_text.encode("utf-8", errors="replace")) > _PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES
    ):
        return None
    try:
        snapshot = json.loads(snapshot_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    try:
        snapshot_version = int(cast(Any, snapshot.get("version")))
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        str(snapshot.get("id") or "") != entity_id
        or str(snapshot.get("user_id") or "") != user_id
        or snapshot_version != int(version)
    ):
        return None

    texts: list[str] = []
    seen_nested: set[str] = set()
    used = 0

    def walk(value: Any, depth: int) -> bool:
        nonlocal used
        if isinstance(value, Mapping):
            return any(walk(str(key), depth) or walk(item, depth) for key, item in value.items())
        if isinstance(value, list):
            return any(walk(item, depth) for item in value)
        if not isinstance(value, str):
            return False
        used += len(value)
        if used > _PUBLIC_ENTITY_VERSION_TEXT_BUDGET:
            return True
        texts.append(value)
        stripped = value.lstrip()
        if not stripped or stripped[0] not in {"{", "[", '"'} or value in seen_nested:
            return False
        if depth >= _PUBLIC_ENTITY_VERSION_NESTED_DEPTH:
            return True
        try:
            nested = json.loads(value)
        except (TypeError, ValueError):
            return True
        seen_nested.add(value)
        return walk(nested, depth + 1)

    if walk(snapshot, 0):
        return None
    haystack = "\0".join(texts)
    folded_haystack = unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", haystack).casefold(),
    )
    private_rows = storage.execute("SELECT id, name FROM private_entity_material_closure").fetchall()
    for row in private_rows:
        private_id = str(row["id"] or "")
        private_name = str(row["name"] or "")
        folded_name = unicodedata.normalize(
            "NFC",
            unicodedata.normalize("NFC", private_name).casefold(),
        )
        if (private_id and private_id in haystack) or (folded_name and folded_name in folded_haystack):
            return None

    merged_into_id = str(snapshot.get("merged_into_id") or "")
    if merged_into_id:
        visible_target = storage.execute(
            f"""SELECT 1 FROM entities merge_target
                  WHERE merge_target.id=? AND merge_target.user_id=?
                    AND {_not_private_entity_material_dependency("merge_target")}
                  LIMIT 1""",  # nosec B608
            (merged_into_id, user_id),
        ).fetchone()
        if visible_target is None:
            return None
    return snapshot_text


def _bounded_visible_timeline_event_rows(
    storage: StorageShared,
    shared_user_id: str,
    person_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Shared document events plus exactly this person's private reminders."""

    clauses = [
        "e.entity_type='event'",
        "e.canonical=1",
        "e.deleted_at IS NULL",
        _not_disallowed_private_material_for_person("e", "?"),
        "((e.user_id=? AND COALESCE(t.source,'') NOT LIKE 'reminder:%' "
        "AND NOT EXISTS (SELECT 1 FROM private_entity_owners shared_private "
        "WHERE shared_private.entity_id=e.id)) "
        "OR (COALESCE(t.source,'')=? AND e.user_id IN (?,?) "
        "AND EXISTS (SELECT 1 FROM private_entity_owners private_owner "
        "WHERE private_owner.entity_id=e.id AND private_owner.person_id=?)))",
    ]
    reminder_source = f"reminder:{person_id}"
    params: list[Any] = [
        person_id,
        shared_user_id,
        reminder_source,
        person_id,
        shared_user_id,
        person_id,
    ]
    if start:
        clauses.append("t.occurred_at>=?")
        params.append(start)
    if end:
        clauses.append("t.occurred_at<=?")
        params.append(end)
    params.append(max(1, min(int(limit), 2_000)))
    rows = storage.execute(
        f"""SELECT substr(e.id,1,160) AS entity_id,
                   substr(e.name,1,240) AS name,
                   substr(e.entity_type,1,80) AS entity_type,
                   substr(e.description,1,500) AS description,
                   substr(t.occurred_at,1,64) AS occurred_at,
                   substr(t.occurred_end,1,64) AS occurred_end,
                   substr(COALESCE(t.precision,''),1,40) AS precision,
                   substr(COALESCE(t.source,''),1,256) AS source,
                   substr(COALESCE(t.updated_at,''),1,64) AS updated_at
              FROM entity_time t
              JOIN entities e ON e.id=t.entity_id AND e.user_id=t.user_id
             WHERE {" AND ".join(clauses)}
             ORDER BY t.occurred_at, e.name COLLATE NOCASE, e.id LIMIT ?""",  # nosec B608
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _count_visible_timeline_events(
    storage: StorageShared,
    shared_user_id: str,
    person_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> int:
    clauses = [
        "e.entity_type='event'",
        "e.canonical=1",
        "e.deleted_at IS NULL",
        _not_disallowed_private_material_for_person("e", "?"),
        "((e.user_id=? AND COALESCE(t.source,'') NOT LIKE 'reminder:%' "
        "AND NOT EXISTS (SELECT 1 FROM private_entity_owners shared_private "
        "WHERE shared_private.entity_id=e.id)) "
        "OR (COALESCE(t.source,'')=? AND e.user_id IN (?,?) "
        "AND EXISTS (SELECT 1 FROM private_entity_owners private_owner "
        "WHERE private_owner.entity_id=e.id AND private_owner.person_id=?)))",
    ]
    params: list[Any] = [
        person_id,
        shared_user_id,
        f"reminder:{person_id}",
        person_id,
        shared_user_id,
        person_id,
    ]
    if start:
        clauses.append("t.occurred_at>=?")
        params.append(start)
    if end:
        clauses.append("t.occurred_at<=?")
        params.append(end)
    row = storage.execute(
        f"""SELECT COUNT(*) AS count FROM entity_time t
              JOIN entities e ON e.id=t.entity_id AND e.user_id=t.user_id
             WHERE {" AND ".join(clauses)}""",  # nosec B608
        tuple(params),
    ).fetchone()
    return int(row["count"] if row else 0)


def _count_visible_relations(storage: StorageShared, user_id: str) -> int:
    """Exact current relation count with the same endpoint privacy boundary as browse."""

    row = storage.execute(
        f"""SELECT COUNT(*) AS count FROM relations r
              JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                   AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("s")}
             JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                   AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("t")}
             WHERE r.user_id=? AND r.deleted_at IS NULL
               AND {_not_private_relation_dependency("r")}""",  # nosec B608
        (user_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


def _entity_search_projection(alias: str = "e") -> str:
    """Bounded row shape for name/alias search; never selects entity metadata."""

    return f"""{alias}.id, {alias}.user_id,
               substr({alias}.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS name,
               substr({alias}.entity_type, 1, 80) AS entity_type,
               CASE WHEN length(CAST({alias}.aliases_json AS BLOB))
                              <= {_GRAPH_ENTITY_ALIASES_MAX_BYTES}
                    THEN {alias}.aliases_json ELSE '[]' END AS aliases_json,
               substr({alias}.description, 1, {_GRAPH_ENTITY_DESCRIPTION_MAX_CHARS}) AS description,
               '{{}}' AS metadata_json, {alias}.canonical, {alias}.merged_into_id,
               {alias}.version, {alias}.created_at, {alias}.updated_at, {alias}.deleted_at"""


def _bounded_entity_listing_rows(
    storage: StorageShared,
    user_id: str,
    *,
    entity_types: Sequence[str] = (),
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Bounded public browse rows without materializing arbitrary entity blobs."""

    clauses = [
        "e.user_id=?",
        "e.deleted_at IS NULL",
        "e.canonical=1",
        "e.merged_into_id IS NULL",
        _not_private_entity_material_dependency("e"),
    ]
    params: list[Any] = [user_id]
    wanted_types = [str(item).strip() for item in entity_types if str(item).strip()]
    if wanted_types:
        clauses.append(f"e.entity_type IN ({','.join('?' * len(wanted_types))})")
        params.extend(wanted_types)
    bounded = max(1, min(int(limit), 201))
    rows = storage.execute(
        f"""SELECT {_entity_search_projection()}
              FROM entities e WHERE {" AND ".join(clauses)}
             ORDER BY e.name COLLATE NOCASE, e.id LIMIT ? OFFSET ?""",  # nosec B608
        (*params, bounded, max(0, int(offset))),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_entity_by_id(
    storage: StorageShared,
    entity_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    """One bounded identity row; arbitrary metadata never crosses into Python."""

    row = storage.execute(
        f"""SELECT {_entity_search_projection()} FROM entities e
            WHERE e.id=? AND e.user_id=?
              AND {_not_private_entity_material_dependency("e")} LIMIT 1""",  # nosec B608
        (entity_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _bounded_relation_candidate_rows(
    storage: StorageShared,
    user_id: str,
    *,
    status: str | None = "suggested",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Public review cards without materializing arbitrary evidence JSON."""

    clauses = ["c.user_id=?"]
    params: list[Any] = [user_id]
    if status:
        if status not in {"suggested", "accepted", "rejected"}:
            raise ValueError("Invalid relation candidate status")
        clauses.append("c.status=?")
        params.append(status)
    bounded = max(1, min(int(limit), _RELATION_CANDIDATE_PAGE_MAX))
    params.extend((bounded, max(0, int(offset))))
    rows = storage.execute(
        f"""SELECT substr(c.id, 1, 160) AS id,
                   substr(c.source_entity_id, 1, 160) AS source_entity_id,
                   substr(c.target_entity_id, 1, 160) AS target_entity_id,
                   substr(c.relation_type, 1, 80) AS relation_type,
                   c.confidence,
                   substr(c.status, 1, 40) AS status,
                   substr(c.created_at, 1, 64) AS created_at,
                   substr(COALESCE(c.reviewed_at, ''), 1, 64) AS reviewed_at,
                   substr(s.name, 1, 240) AS source_name,
                   substr(s.entity_type, 1, 80) AS source_type,
                   substr(t.name, 1, 240) AS target_name,
                   substr(t.entity_type, 1, 80) AS target_type,
                   CASE WHEN COALESCE(c.evidence_json, '') NOT IN ('', '{{}}', '[]', 'null')
                        THEN 1 ELSE 0 END AS evidence_present,
                   MIN(length(CAST(COALESCE(c.evidence_json, '') AS BLOB)), 1000000000)
                       AS evidence_bytes
              FROM relation_candidates c
              JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                   AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("s")}
              JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                   AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("t")}
             WHERE {_not_private_relation_candidate_dependency("c")}
               AND {" AND ".join(clauses)}
             ORDER BY c.confidence DESC, c.created_at DESC, c.id
             LIMIT ? OFFSET ?""",  # nosec B608
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_relation_candidate_by_id(
    storage: StorageShared,
    user_id: str,
    candidate_id: str,
) -> dict[str, Any] | None:
    """One live, visible relation-review card without its evidence body."""

    row = storage.execute(
        f"""SELECT substr(c.id, 1, 160) AS id,
                   substr(c.source_entity_id, 1, 160) AS source_entity_id,
                   substr(c.target_entity_id, 1, 160) AS target_entity_id,
                   substr(c.relation_type, 1, 80) AS relation_type,
                   c.confidence, substr(c.status, 1, 40) AS status,
                   substr(c.created_at, 1, 64) AS created_at,
                   substr(COALESCE(c.reviewed_at, ''), 1, 64) AS reviewed_at,
                   substr(s.name, 1, 240) AS source_name,
                   substr(s.entity_type, 1, 80) AS source_type,
                   substr(t.name, 1, 240) AS target_name,
                   substr(t.entity_type, 1, 80) AS target_type,
                   CASE WHEN COALESCE(c.evidence_json, '') NOT IN ('', '{{}}', '[]', 'null')
                        THEN 1 ELSE 0 END AS evidence_present,
                   MIN(length(CAST(COALESCE(c.evidence_json, '') AS BLOB)), 1000000000)
                       AS evidence_bytes
              FROM relation_candidates c
              JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                   AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("s")}
              JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                   AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("t")}
             WHERE c.id=? AND c.user_id=?
               AND {_not_private_relation_candidate_dependency("c")}
             LIMIT 1""",  # nosec B608
        (candidate_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _bounded_resolution_candidate_rows(
    storage: StorageShared,
    user_id: str,
    status: ResolutionStatus | None = None,
    *,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Candidate structure only; review evidence never enters browse payloads."""

    clauses = [
        "user_id=?",
        "EXISTS (SELECT 1 FROM entities visible_a WHERE visible_a.id=entity_a_id "
        "AND visible_a.user_id=entity_resolution_candidates.user_id "
        f"AND {_not_private_entity_material_dependency('visible_a')})",
        "EXISTS (SELECT 1 FROM entities visible_b WHERE visible_b.id=entity_b_id "
        "AND visible_b.user_id=entity_resolution_candidates.user_id "
        f"AND {_not_private_entity_material_dependency('visible_b')})",
        _not_private_resolution_candidate_dependency("entity_resolution_candidates"),
    ]
    params: list[Any] = [user_id]
    if status is not None:
        clauses.append("status=?")
        params.append(enum_value(status))
    bounded = max(1, min(int(limit), 501))
    params.extend((bounded, max(0, int(offset))))
    rows = storage.execute(
        f"""SELECT id, entity_a_id, entity_b_id, confidence,
                   substr(resolution_method, 1, 80) AS resolution_method,
                   substr(status, 1, 40) AS status,
                   substr(created_at, 1, 64) AS created_at,
                   substr(COALESCE(resolved_at, ''), 1, 64) AS resolved_at
              FROM entity_resolution_candidates
             WHERE {" AND ".join(clauses)}
             ORDER BY confidence DESC, created_at DESC, id DESC
             LIMIT ? OFFSET ?""",  # nosec B608
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_resolution_candidate_by_id(
    storage: StorageShared,
    candidate_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    row = storage.execute(
        f"""SELECT id, entity_a_id, entity_b_id, confidence,
                  substr(resolution_method, 1, 80) AS resolution_method,
                  substr(status, 1, 40) AS status,
                  substr(created_at, 1, 64) AS created_at,
                  substr(COALESCE(resolved_at, ''), 1, 64) AS resolved_at
             FROM entity_resolution_candidates
            WHERE id=? AND user_id=?
              AND EXISTS (SELECT 1 FROM entities visible_a
                           WHERE visible_a.id=entity_a_id
                             AND visible_a.user_id=entity_resolution_candidates.user_id
                             AND {_not_private_entity_material_dependency("visible_a")})
              AND EXISTS (SELECT 1 FROM entities visible_b
                           WHERE visible_b.id=entity_b_id
                             AND visible_b.user_id=entity_resolution_candidates.user_id
                             AND {_not_private_entity_material_dependency("visible_b")})
              AND {_not_private_resolution_candidate_dependency("entity_resolution_candidates")}
            LIMIT 1""",
        (candidate_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _merge_history_privacy_context(
    storage: StorageShared,
    user_id: str,
) -> dict[str, Any]:
    """Resolve one tenant's durable merge dependencies under the caller's lock.

    History contains copies, not merely foreign keys: link evidence, relation
    metadata and entity JSON columns are encoded again inside the outer transfer
    object.  The context is deliberately built once per list/count operation so
    every row is checked against the same privacy snapshot without an N+1 query
    for each embedded id.
    """

    visible_entity_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT e.id FROM entities e WHERE e.user_id=?
                  AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    hidden_entities = storage.execute(
        f"""SELECT e.id, e.name FROM entities e
             WHERE NOT ({_not_private_entity_material_dependency("e")})"""  # nosec B608
    ).fetchall()
    visible_knowledge_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT k.id FROM knowledge_objects k WHERE k.user_id=?
                  AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    hidden_knowledge_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT k.id FROM knowledge_objects k
                 WHERE NOT ({_not_private_knowledge_dependency("k")})"""  # nosec B608
        ).fetchall()
    }
    visible_link_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT l.id FROM knowledge_entity_links l
                  JOIN entities link_entity
                    ON link_entity.id=l.entity_id AND link_entity.user_id=l.user_id
                   AND {_not_private_entity_material_dependency("link_entity")}
                  JOIN knowledge_objects link_knowledge
                    ON link_knowledge.id=l.knowledge_object_id
                   AND link_knowledge.user_id=l.user_id
                   AND {_not_private_knowledge_dependency("link_knowledge")}
                 WHERE l.user_id=?
                   AND {
                _not_private_bounded_json_dependency(
                    "l.evidence_json",
                    "l.user_id",
                    max_bytes=_MERGE_HISTORY_JSON_MAX_BYTES,
                    reject_nested_json=True,
                )
            }""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    all_link_ids = {
        str(row["id"]) for row in storage.execute("SELECT id FROM knowledge_entity_links").fetchall()
    }
    hidden_link_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT l.id FROM knowledge_entity_links l
                 WHERE NOT EXISTS (
                           SELECT 1 FROM entities hidden_link_entity
                            WHERE hidden_link_entity.id=l.entity_id
                              AND hidden_link_entity.user_id=l.user_id
                              AND {_not_private_entity_material_dependency("hidden_link_entity")}
                       )
                    OR NOT EXISTS (
                           SELECT 1 FROM knowledge_objects hidden_link_knowledge
                            WHERE hidden_link_knowledge.id=l.knowledge_object_id
                              AND hidden_link_knowledge.user_id=l.user_id
                              AND {_not_private_knowledge_dependency("hidden_link_knowledge")}
                       )
                    OR NOT ({
                _not_private_bounded_json_dependency(
                    "l.evidence_json",
                    "l.user_id",
                    max_bytes=_MERGE_HISTORY_JSON_MAX_BYTES,
                    reject_nested_json=True,
                )
            })"""  # nosec B608
        ).fetchall()
    }
    visible_relation_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT r.id FROM relations r
                  JOIN entities relation_source
                    ON relation_source.id=r.source_entity_id
                   AND relation_source.user_id=r.user_id
                   AND {_not_private_entity_material_dependency("relation_source")}
                  JOIN entities relation_target
                    ON relation_target.id=r.target_entity_id
                   AND relation_target.user_id=r.user_id
                   AND {_not_private_entity_material_dependency("relation_target")}
                 WHERE r.user_id=? AND {_not_private_relation_dependency("r")}""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    all_relation_ids = {str(row["id"]) for row in storage.execute("SELECT id FROM relations").fetchall()}
    hidden_relation_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT r.id FROM relations r
                 WHERE NOT EXISTS (
                           SELECT 1 FROM entities hidden_relation_source
                            WHERE hidden_relation_source.id=r.source_entity_id
                              AND hidden_relation_source.user_id=r.user_id
                              AND {_not_private_entity_material_dependency("hidden_relation_source")}
                       )
                    OR NOT EXISTS (
                           SELECT 1 FROM entities hidden_relation_target
                            WHERE hidden_relation_target.id=r.target_entity_id
                              AND hidden_relation_target.user_id=r.user_id
                              AND {_not_private_entity_material_dependency("hidden_relation_target")}
                       )
                    OR NOT ({_not_private_relation_dependency("r")})"""  # nosec B608
        ).fetchall()
    }
    visible_relation_candidate_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT c.id FROM relation_candidates c
                  JOIN entities relation_candidate_source
                    ON relation_candidate_source.id=c.source_entity_id
                   AND relation_candidate_source.user_id=c.user_id
                   AND {_not_private_entity_material_dependency("relation_candidate_source")}
                  JOIN entities relation_candidate_target
                    ON relation_candidate_target.id=c.target_entity_id
                   AND relation_candidate_target.user_id=c.user_id
                   AND {_not_private_entity_material_dependency("relation_candidate_target")}
                 WHERE c.user_id=?
                   AND {_not_private_relation_candidate_dependency("c")}""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    hidden_relation_candidate_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT c.id FROM relation_candidates c
                 WHERE NOT EXISTS (
                           SELECT 1 FROM entities hidden_relation_candidate_source
                            WHERE hidden_relation_candidate_source.id=c.source_entity_id
                              AND hidden_relation_candidate_source.user_id=c.user_id
                              AND {
                _not_private_entity_material_dependency("hidden_relation_candidate_source")
            }
                       )
                    OR NOT EXISTS (
                           SELECT 1 FROM entities hidden_relation_candidate_target
                            WHERE hidden_relation_candidate_target.id=c.target_entity_id
                              AND hidden_relation_candidate_target.user_id=c.user_id
                              AND {
                _not_private_entity_material_dependency("hidden_relation_candidate_target")
            }
                       )
                    OR NOT ({_not_private_relation_candidate_dependency("c")})"""  # nosec B608
        ).fetchall()
    }
    visible_candidate_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT c.id FROM entity_resolution_candidates c
                  JOIN entities candidate_a
                    ON candidate_a.id=c.entity_a_id AND candidate_a.user_id=c.user_id
                   AND {_not_private_entity_material_dependency("candidate_a")}
                  JOIN entities candidate_b
                    ON candidate_b.id=c.entity_b_id AND candidate_b.user_id=c.user_id
                   AND {_not_private_entity_material_dependency("candidate_b")}
                 WHERE c.user_id=?
                   AND {_not_private_resolution_candidate_dependency("c")}""",  # nosec B608
            (user_id,),
        ).fetchall()
    }
    hidden_candidate_ids = {
        str(row["id"])
        for row in storage.execute(
            f"""SELECT c.id FROM entity_resolution_candidates c
                 WHERE NOT EXISTS (
                           SELECT 1 FROM entities hidden_candidate_a
                            WHERE hidden_candidate_a.id=c.entity_a_id
                              AND hidden_candidate_a.user_id=c.user_id
                              AND {_not_private_entity_material_dependency("hidden_candidate_a")}
                       )
                    OR NOT EXISTS (
                           SELECT 1 FROM entities hidden_candidate_b
                            WHERE hidden_candidate_b.id=c.entity_b_id
                              AND hidden_candidate_b.user_id=c.user_id
                              AND {_not_private_entity_material_dependency("hidden_candidate_b")}
                       )
                    OR NOT ({_not_private_resolution_candidate_dependency("c")})"""  # nosec B608
        ).fetchall()
    }
    return {
        "user_id": user_id,
        "visible_entity_ids": visible_entity_ids,
        "visible_knowledge_ids": visible_knowledge_ids,
        "visible_link_ids": visible_link_ids,
        "all_link_ids": all_link_ids,
        "visible_relation_ids": visible_relation_ids,
        "all_relation_ids": all_relation_ids,
        "visible_relation_candidate_ids": visible_relation_candidate_ids,
        "visible_candidate_ids": visible_candidate_ids,
        "hidden_ids": {
            *(str(row["id"] or "") for row in hidden_entities),
            *hidden_knowledge_ids,
            *hidden_link_ids,
            *hidden_relation_ids,
            *hidden_relation_candidate_ids,
            *hidden_candidate_ids,
        }
        - {""},
        "hidden_names": {
            unicodedata.normalize(
                "NFC",
                unicodedata.normalize("NFC", str(row["name"] or "")).casefold(),
            )
            for row in hidden_entities
            if str(row["name"] or "")
        },
    }


@contextmanager
def _merge_history_read_snapshot(storage: StorageShared) -> Iterator[None]:
    """Hold one WAL read snapshot without advancing the relation write clock."""

    connection = storage.conn
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()


def _decode_merge_history_objects(row: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    parsed: dict[str, dict[str, Any]] = {}
    for field in (
        "source_snapshot_json",
        "target_before_json",
        "target_after_json",
        "transfer_json",
    ):
        raw = row.get(field)
        if not isinstance(raw, str):
            return None
        if len(raw.encode("utf-8", errors="replace")) > _MERGE_HISTORY_JSON_MAX_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        parsed[field] = value
    return parsed


def _merge_transfer_recorded_ids(transfer: Mapping[str, Any]) -> tuple[set[str], set[str]] | None:
    """Validate the replay grammar and return ids allowed to be absent post-move."""

    array_fields = (
        "links_moved",
        "links_suppressed",
        "primary_moved",
        "relations",
        "relation_reference_rewrites",
        "closed_candidates",
        "time_moved",
    )
    if any(field in transfer and not isinstance(transfer[field], list) for field in array_fields):
        return None

    recorded_links: set[str] = set()
    for field in ("links_moved", "links_suppressed"):
        for item in transfer.get(field) or []:
            if not isinstance(item, dict):
                return None
            link_required = ("id", "user_id", "knowledge_object_id", "evidence_json")
            if any(not isinstance(item.get(key), str) or not str(item[key]) for key in link_required):
                return None
            recorded_links.add(str(item["id"]))
            if field == "links_moved":
                target_link_id = item.get("target_link_id")
                if not isinstance(target_link_id, str) or not target_link_id:
                    return None
                recorded_links.add(target_link_id)

    if any(not isinstance(item, str) or not item for item in transfer.get("primary_moved") or []):
        return None

    recorded_relations: set[str] = set()
    for item in transfer.get("relations") or []:
        if not isinstance(item, dict) or not isinstance(item.get("original"), dict):
            return None
        original = item["original"]
        relation_required = (
            "id",
            "user_id",
            "source_entity_id",
            "target_entity_id",
            "metadata_json",
        )
        if any(not isinstance(original.get(key), str) or not str(original[key]) for key in relation_required):
            return None
        recorded_relations.add(str(original["id"]))
        fate = str(item.get("fate") or "")
        if fate not in {"moved", "self_loop_dropped", "suppressed_duplicate"}:
            return None
        if fate == "moved":
            rewritten = item.get("rewritten")
            if not isinstance(rewritten, dict) or any(
                not isinstance(rewritten.get(key), str) or not str(rewritten[key])
                for key in ("id", "source_entity_id", "target_entity_id")
            ):
                return None
            recorded_relations.add(str(rewritten["id"]))
        if fate == "suppressed_duplicate":
            kept_id = item.get("kept_relation_id")
            if not isinstance(kept_id, str) or not kept_id:
                return None
            recorded_relations.add(kept_id)

    for item in transfer.get("relation_reference_rewrites") or []:
        if not isinstance(item, dict):
            return None
        if not isinstance(item.get("relation_id"), str) or not item["relation_id"]:
            return None
        if not isinstance(item.get("before"), str) or not item["before"]:
            return None
        if item.get("after") is not None and (not isinstance(item.get("after"), str) or not item["after"]):
            return None

    if any(not isinstance(item, str) or not item for item in transfer.get("closed_candidates") or []):
        return None
    for item in transfer.get("time_moved") or []:
        if not isinstance(item, dict):
            return None
        if any(
            not isinstance(item.get(key), str) or not str(item[key])
            for key in ("entity_id", "user_id", "occurred_at", "precision", "updated_at")
        ):
            return None
        if not isinstance(item.get("source"), str):
            return None
        if item.get("occurred_end") is not None and not isinstance(item.get("occurred_end"), str):
            return None
    if "time_target_created" in transfer and not isinstance(transfer["time_target_created"], bool):
        return None
    return recorded_links, recorded_relations


def _merge_history_row_is_visible(
    row: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[bool, dict[str, dict[str, Any]] | None]:
    """One fail-closed rule shared by get/list/count/cards and transactional undo."""

    parsed = _decode_merge_history_objects(row)
    if parsed is None:
        return False, None
    user_id = str(context["user_id"])
    source_id = str(row.get("source_entity_id") or "")
    target_id = str(row.get("target_entity_id") or "")
    visible_entities = context["visible_entity_ids"]
    if source_id not in visible_entities or target_id not in visible_entities:
        return False, None
    source_snapshot = parsed["source_snapshot_json"]
    target_before = parsed["target_before_json"]
    target_after = parsed["target_after_json"]
    if (
        str(source_snapshot.get("id") or "") != source_id
        or str(source_snapshot.get("user_id") or "") != user_id
    ):
        return False, None
    if any(
        str(snapshot.get("id") or "") != target_id or str(snapshot.get("user_id") or "") != user_id
        for snapshot in (target_before, target_after)
    ):
        return False, None

    recorded = _merge_transfer_recorded_ids(parsed["transfer_json"])
    if recorded is None:
        return False, None
    recorded_links, recorded_relations = recorded
    visible_knowledge = context["visible_knowledge_ids"]
    visible_links = context["visible_link_ids"]
    visible_relations = context["visible_relation_ids"]
    if recorded_links & (context["all_link_ids"] - visible_links):
        return False, None
    if recorded_relations & (context["all_relation_ids"] - visible_relations):
        return False, None
    visible_relation_candidates = context["visible_relation_candidate_ids"]
    visible_candidates = context["visible_candidate_ids"]
    texts: list[str] = []
    seen_nested: set[str] = set()
    used = 0

    def walk(value: Any, key: str = "", depth: int = 0) -> bool:
        nonlocal used
        if isinstance(value, Mapping):
            return all(
                walk(str(item_key), depth=depth) and walk(item, key=str(item_key), depth=depth)
                for item_key, item in value.items()
            )
        if isinstance(value, list):
            return all(walk(item, key=key, depth=depth) for item in value)
        if value in (None, ""):
            return True
        if not isinstance(value, str):
            return True
        used += len(value.encode("utf-8", errors="replace"))
        if used > _MERGE_HISTORY_TEXT_BUDGET:
            return False
        texts.append(value)
        if (
            key
            in {
                "entity_id",
                "source_entity_id",
                "target_entity_id",
                "entity_a_id",
                "entity_b_id",
                "merged_into_id",
            }
            and value not in visible_entities
        ):
            return False
        if key == "knowledge_object_id" and value not in visible_knowledge:
            return False
        if key == "primary_moved" and value not in visible_knowledge:
            return False
        if key == "target_link_id" and value not in visible_links and value not in recorded_links:
            return False
        if (
            key in {"kept_relation_id", "relation_id", "before", "after", "superseded_by"}
            and value not in visible_relations
            and value not in recorded_relations
        ):
            return False
        if key == "closed_candidates" and value not in visible_candidates:
            return False
        if (
            key == "candidate_id"
            and value not in visible_relation_candidates
            and value not in visible_candidates
        ):
            return False
        if key == "user_id" and value != user_id:
            return False
        if key == "source" and value.startswith("reminder:"):
            return False
        stripped = value.lstrip()
        if not stripped or stripped[0] not in {"{", "[", '"'} or value in seen_nested:
            return True
        if depth >= _MERGE_HISTORY_NESTED_DEPTH:
            return False
        try:
            nested = json.loads(value)
        except (TypeError, ValueError):
            return False
        seen_nested.add(value)
        return walk(nested, key=key, depth=depth + 1)

    if not all(walk(value) for value in parsed.values()):
        return False, None
    if not all(
        walk(row.get(field), key=field)
        for field in ("id", "source_entity_id", "target_entity_id", "merged_by", "undone_by")
    ):
        return False, None
    haystack = "\0".join(texts)
    folded_haystack = unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", haystack).casefold(),
    )
    if any(token in haystack for token in context["hidden_ids"]):
        return False, None
    if any(name in folded_haystack for name in context["hidden_names"]):
        return False, None
    return True, parsed


def _iter_visible_merge_history(
    storage: StorageShared,
    user_id: str,
    context: Mapping[str, Any],
) -> Iterator[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
    cursor = storage.execute(
        """SELECT * FROM entity_merge_history WHERE user_id=?
           ORDER BY created_at DESC, id DESC""",
        (user_id,),
    )
    while True:
        rows = cursor.fetchmany(128)
        if not rows:
            break
        for raw_row in rows:
            row = dict(raw_row)
            visible, parsed = _merge_history_row_is_visible(row, context)
            if visible and parsed is not None:
                yield row, parsed


def _merge_history_card(
    row: Mapping[str, Any],
    parsed: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    transfer_text = str(row.get("transfer_json") or "")
    transfer = parsed["transfer_json"]
    transfer_bytes = len(transfer_text.encode("utf-8", errors="replace"))

    def count(field: str) -> int:
        value = transfer.get(field)
        return min(len(value), 1_000_000_000) if isinstance(value, list) else 0

    countable = transfer_bytes <= _GRAPH_RELATION_METADATA_MAX_BYTES
    return {
        "id": str(row.get("id") or "")[:160],
        "source_entity_id": str(row.get("source_entity_id") or "")[:160],
        "target_entity_id": str(row.get("target_entity_id") or "")[:160],
        "created_at": str(row.get("created_at") or "")[:64],
        "undone_at": str(row.get("undone_at") or "")[:64],
        "undoable": int(bool(transfer) and not row.get("undone_at")),
        "transfer_bytes": min(transfer_bytes, 1_000_000_000),
        "links_moved_count": count("links_moved") if countable else 0,
        "links_suppressed_count": count("links_suppressed") if countable else 0,
        "relations_count": count("relations") if countable else 0,
        "candidates_closed_count": count("closed_candidates") if countable else 0,
    }


def _bounded_merge_history_rows(
    storage: StorageShared,
    user_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Structural merge history after the complete snapshot/transfer closure."""

    bounded = max(1, min(int(limit), 201))
    wanted_offset = max(0, int(offset))
    result: list[dict[str, Any]] = []
    with _merge_history_read_snapshot(storage):
        context = _merge_history_privacy_context(storage, user_id)
        visible_offset = 0
        for row, parsed in _iter_visible_merge_history(storage, user_id, context):
            if visible_offset < wanted_offset:
                visible_offset += 1
                continue
            result.append(_merge_history_card(row, parsed))
            if len(result) >= bounded:
                break
    return result


def _count_merge_history(storage: StorageShared, user_id: str) -> int:
    with _merge_history_read_snapshot(storage):
        context = _merge_history_privacy_context(storage, user_id)
        return sum(1 for _row in _iter_visible_merge_history(storage, user_id, context))


def _iter_entities_for_graph_search(
    storage: StorageShared,
    user_id: str,
    *,
    page_size: int = 1_000,
) -> Iterator[dict[str, Any]]:
    """Walk the graph-search corpus in bounded cards, never full entity rows."""

    bounded = max(1, min(int(page_size), 1_000))
    last_id = ""
    while True:
        rows = storage.execute(
            f"""SELECT {_entity_search_projection()}
                 FROM entities e
                 WHERE e.user_id=? AND e.deleted_at IS NULL AND e.canonical=1
                   AND e.merged_into_id IS NULL
                   AND {_not_private_entity_material_dependency("e")} AND e.id>?
                 ORDER BY e.id LIMIT ?""",  # nosec B608
            (user_id, last_id, bounded),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            yield dict(row)
        if len(rows) < bounded:
            break
        last_id = str(rows[-1]["id"])


def _iter_alias_entities(
    storage: StorageShared,
    user_id: str,
    *,
    page_size: int = 256,
) -> Iterator[dict[str, Any]]:
    """Page the minority alias corpus without an O(tenant) ``fetchall`` peak."""

    bounded = max(1, min(int(page_size), 1_000))
    last_id = ""
    while True:
        rows = storage.execute(
            f"""SELECT {_entity_search_projection()} FROM entities e
                WHERE e.user_id=? AND e.deleted_at IS NULL AND e.canonical=1
                  AND e.merged_into_id IS NULL
                  AND {_not_private_entity_material_dependency("e")} AND e.id>?
                  AND e.aliases_json NOT IN ('[]', '', 'null')
                ORDER BY e.id LIMIT ?""",  # nosec B608
            (user_id, last_id, bounded),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            yield dict(row)
        if len(rows) < bounded:
            break
        last_id = str(rows[-1]["id"])


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


def _relation_batch_timestamp(conn: sqlite3.Connection) -> str:
    """The exact transaction timestamp shared by relation and identity history."""

    row = conn.execute("SELECT recorded_at FROM relation_revision_context WHERE singleton=1").fetchone()
    recorded_at = str(row["recorded_at"] if row else "")
    if not recorded_at:
        raise RuntimeError("relation revision transaction context is missing")
    return normalize_known_at(recorded_at, reject_future=False)


_RELATION_HISTORY_STATUS_FIELDS = (
    "known_at",
    "known_at_floor",
    "history_complete",
    "identity_basis",
)


def _canonical_relation_history_status(
    status: Mapping[str, Any],
    *,
    requested_known_at: str,
) -> dict[str, Any]:
    """Require the complete storage provenance tuple, without truthy fallbacks."""

    if any(field not in status for field in _RELATION_HISTORY_STATUS_FIELDS):
        raise RelationHistorySnapshotError("relation history status is incomplete")
    expected = normalize_known_at(requested_known_at)
    returned = str(status["known_at"] or "")
    try:
        canonical_returned = normalize_known_at(returned, reject_future=False)
    except ValueError as exc:
        raise RelationHistorySnapshotError(
            "relation history returned an unreadable known_at boundary"
        ) from exc
    if returned != canonical_returned or canonical_returned != expected:
        raise RelationHistorySnapshotError("relation history changed the requested known_at boundary")

    raw_floor = str(status["known_at_floor"] or "")
    try:
        floor = normalize_known_at(raw_floor, reject_future=False)
    except ValueError as exc:
        raise RelationHistorySnapshotError("relation history completeness floor is unreadable") from exc
    if not raw_floor or raw_floor != floor or floor > expected:
        raise RelationHistorySnapshotError("relation history completeness floor is inconsistent")
    if status["history_complete"] is not True:
        raise RelationHistorySnapshotError("relation history is not complete for the requested boundary")
    if status["identity_basis"] != "current_names":
        raise RelationHistorySnapshotError("relation history identity basis is unsupported")
    return {
        "known_at": expected,
        "known_at_floor": floor,
        "history_complete": True,
        "identity_basis": "current_names",
    }


def _assert_no_later_entity_topology_change(
    storage: StorageShared,
    user_id: str,
    boundary: str,
) -> set[str]:
    """Refuse current entity topology after a historical relation boundary.

    Names, aliases, descriptions and metadata deliberately remain current.  Only
    fields which decide whether an endpoint exists and where it resolves are
    compared.  SQL extracts those fields before Python sees the rows so neither
    diagnostics nor accidental exception rendering can include entity content.
    """

    rows = storage.execute(
        f"""SELECT v.entity_id, v.version, v.created_at,
                  json_valid(v.snapshot_json) AS snapshot_valid,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_type(v.snapshot_json, '$.canonical') END AS canonical_type,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_extract(v.snapshot_json, '$.canonical') END AS canonical_value,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_type(v.snapshot_json, '$.merged_into_id') END AS merged_type,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_extract(v.snapshot_json, '$.merged_into_id') END AS merged_value,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_type(v.snapshot_json, '$.deleted_at') END AS deleted_type,
                  CASE WHEN json_valid(v.snapshot_json)
                       THEN json_extract(v.snapshot_json, '$.deleted_at') END AS deleted_value
             FROM entity_versions v
             JOIN entities e ON e.id=v.entity_id AND e.user_id=v.user_id
            WHERE v.user_id=? AND {_not_private_entity_material_dependency("e")}
            ORDER BY v.entity_id, v.version, v.created_at, v.id""",  # nosec B608
        (user_id,),
    ).fetchall()
    witnessed_at_boundary = {
        str(row["entity_id"])
        for row in storage.execute(
            f"""SELECT rr.source_entity_id AS entity_id
                  FROM relation_revisions rr
                  JOIN entities s ON s.id=rr.source_entity_id AND s.user_id=rr.user_id
                       AND {_not_private_entity_material_dependency("s")}
             JOIN entities t ON t.id=rr.target_entity_id AND t.user_id=rr.user_id
                       AND {_not_private_entity_material_dependency("t")}
                 WHERE rr.user_id=? AND rr.recorded_at<=?
                   AND {_not_private_relation_dependency("rr")}
               UNION
                SELECT rr.target_entity_id AS entity_id
                  FROM relation_revisions rr
                  JOIN entities s ON s.id=rr.source_entity_id AND s.user_id=rr.user_id
                       AND {_not_private_entity_material_dependency("s")}
                  JOIN entities t ON t.id=rr.target_entity_id AND t.user_id=rr.user_id
                       AND {_not_private_entity_material_dependency("t")}
                 WHERE rr.user_id=? AND rr.recorded_at<=?
                   AND {_not_private_relation_dependency("rr")}""",  # nosec B608
            (user_id, boundary, user_id, boundary),
        ).fetchall()
    }
    previous: dict[str, tuple[bool, str, bool]] = {}
    versioned_entity_ids: set[str] = set()
    later_entity_ids: set[str] = set()
    for row in rows:
        entity_id = str(row["entity_id"])
        first_version = entity_id not in versioned_entity_ids
        versioned_entity_ids.add(entity_id)
        raw_recorded_at = str(row["created_at"] or "")
        try:
            recorded_at = normalize_known_at(raw_recorded_at, reject_future=False)
        except ValueError as exc:
            raise RelationHistorySnapshotError(
                "entity topology history contains an unreadable timestamp"
            ) from exc
        coarse_same_second = (
            not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", raw_recorded_at) and recorded_at[:19] == boundary[:19]
        )
        if (
            first_version
            and entity_id not in witnessed_at_boundary
            and (recorded_at > boundary or coarse_same_second)
        ):
            # This identity did not yet exist in the requested snapshot. Its
            # later deletion/merge is equally irrelevant and must not globally
            # poison otherwise reproducible roots; selecting it is rejected by
            # `_assert_entities_existed_at_boundary` instead.
            later_entity_ids.add(entity_id)
        if entity_id in later_entity_ids:
            continue
        if int(row["snapshot_valid"] or 0) != 1:
            raise RelationHistorySnapshotError("entity topology history contains an unreadable snapshot")
        if row["canonical_type"] not in {"true", "false", "integer"}:
            raise RelationHistorySnapshotError("entity topology history is incomplete")
        if row["merged_type"] not in {"null", "text"}:
            raise RelationHistorySnapshotError("entity topology history is incomplete")
        if row["deleted_type"] not in {"null", "text"}:
            raise RelationHistorySnapshotError("entity topology history is incomplete")
        topology = (
            bool(row["canonical_value"]),
            str(row["merged_value"] or ""),
            row["deleted_value"] is not None,
        )
        earlier = previous.get(entity_id)
        # Legacy/entity version timestamps have second precision. If the relation
        # boundary lies inside that same second, ordering is unknowable; fail
        # closed instead of pretending `.000000` proves the topology came first.
        if earlier is not None and topology != earlier and (recorded_at > boundary or coarse_same_second):
            raise RelationHistorySnapshotError(
                "known_at crosses a later entity topology change; historical identity is unavailable"
            )
        previous[entity_id] = topology

    # Version history is useful only if its tail still describes the current
    # topology. Legacy imports, manual repair, or corruption can bypass the
    # version writer; accepting that drift would silently use today's tombstone
    # state while claiming the historical boundary was reproducible. Select only
    # topology fields here so diagnostics can never carry names or metadata.
    current_rows = storage.execute(
        f"""SELECT e.id, e.canonical, COALESCE(e.merged_into_id, '') AS merged_into_id,
                  e.deleted_at IS NOT NULL AS deleted
             FROM entities e
            WHERE e.user_id=? AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
        (user_id,),
    ).fetchall()
    current_by_id = {str(row["id"]): row for row in current_rows}
    if any(entity_id not in versioned_entity_ids for entity_id in current_by_id):
        raise RelationHistorySnapshotError("entity topology history is incomplete")
    active_merges = {
        (str(row["source_entity_id"]), str(row["target_entity_id"]))
        for row in storage.execute(
            """SELECT source_entity_id, target_entity_id
                 FROM entity_merge_history
                WHERE user_id=? AND undone_at IS NULL""",
            (user_id,),
        ).fetchall()
    }
    for entity_id, recorded in previous.items():
        row = current_by_id.get(entity_id)
        if row is None:
            raise RelationHistorySnapshotError("entity topology history is incomplete")
        current = (
            bool(row["canonical"]),
            str(row["merged_into_id"] or ""),
            bool(row["deleted"]),
        )
        recorded_merge = (
            not current[0] and bool(current[1]) and current[2] and (entity_id, current[1]) in active_merges
        )
        if recorded != current and not recorded_merge:
            raise RelationHistorySnapshotError(
                "current entity topology differs from its recorded history; "
                "merge or unmerge reconstruction is unavailable"
            )
    return set(previous)


def _assert_entities_existed_at_boundary(
    storage: StorageShared,
    user_id: str,
    entity_ids: Sequence[str],
    boundary: str,
) -> None:
    """Refuse selected current identities which did not yet exist at ``boundary``.

    This is intentionally scoped to identities the caller is about to publish.
    A new, unrelated entity must not invalidate every historical graph snapshot
    for the tenant, while a newly-created requested root must never appear in a
    transaction snapshot from before its first recorded version.
    """

    wanted = list(dict.fromkeys(str(item) for item in entity_ids if str(item)))
    for offset in range(0, len(wanted), 400):
        batch = wanted[offset : offset + 400]
        placeholders = ",".join("?" * len(batch))
        rows = storage.execute(
            f"""SELECT e.id,
                       (SELECT v.created_at
                          FROM entity_versions v
                         WHERE v.user_id=e.user_id AND v.entity_id=e.id
                         ORDER BY v.version, v.created_at, v.id
                         LIMIT 1) AS first_recorded_at,
                       EXISTS(
                           SELECT 1
                             FROM relation_revisions rr
                             JOIN entities rs
                               ON rs.id=rr.source_entity_id AND rs.user_id=rr.user_id
                              AND {_not_private_entity_material_dependency("rs")}
                             JOIN entities rt
                               ON rt.id=rr.target_entity_id AND rt.user_id=rr.user_id
                              AND {_not_private_entity_material_dependency("rt")}
                            WHERE rr.user_id=e.user_id AND rr.recorded_at<=?
                              AND {_not_private_relation_dependency("rr")}
                              AND (rr.source_entity_id=e.id OR rr.target_entity_id=e.id)
                       ) AS witnessed_at_boundary
                  FROM entities e
                 WHERE e.user_id=? AND e.id IN ({placeholders})
                   AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
            (boundary, user_id, *batch),
        ).fetchall()
        returned = {str(row["id"]) for row in rows}
        if returned != set(batch):
            raise RelationHistorySnapshotError(
                "recorded existence history is incomplete for the requested snapshot"
            )
        for row in rows:
            raw_recorded_at = str(row["first_recorded_at"] or "")
            if not raw_recorded_at:
                raise RelationHistorySnapshotError(
                    "recorded existence history is incomplete for the requested snapshot"
                )
            try:
                recorded_at = normalize_known_at(raw_recorded_at, reject_future=False)
            except ValueError as exc:
                raise RelationHistorySnapshotError(
                    "entity existence history contains an unreadable timestamp"
                ) from exc
            # Entity-version history predates microsecond transaction stamps.
            # When both values occupy the same coarse second we cannot prove the
            # entity existed first, so the only honest result is a refusal.
            coarse_same_second = (
                not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", raw_recorded_at)
                and recorded_at[:19] == boundary[:19]
            )
            # A relation revision at/before the boundary is a durable witness
            # that its FK endpoint already existed, even if the endpoint's own
            # old timestamp only names the containing second.
            witnessed = bool(row["witnessed_at_boundary"])
            if not witnessed and (recorded_at > boundary or coarse_same_second):
                raise RelationHistorySnapshotError(
                    "known_at precedes a selected entity's recorded existence; "
                    "historical identity is unavailable"
                )


def _relation_revision_watermark(
    storage: StorageShared,
    user_id: str,
    boundary: str,
) -> int:
    """Monotonic token for all tenant revisions eligible at one boundary."""

    row = storage.execute(
        f"""SELECT COALESCE(MAX(rr.event_seq), 0) AS watermark
              FROM relation_revisions rr
              JOIN entities s ON s.id=rr.source_entity_id AND s.user_id=rr.user_id
                   AND {_not_private_entity_material_dependency("s")}
              JOIN entities t ON t.id=rr.target_entity_id AND t.user_id=rr.user_id
                   AND {_not_private_entity_material_dependency("t")}
             WHERE rr.user_id=? AND rr.recorded_at<=?
               AND {_not_private_relation_dependency("rr")}""",  # nosec B608
        (user_id, boundary),
    ).fetchone()
    return int(row["watermark"] if row else 0)


def _graph_entity_for_traversal(
    storage: StorageShared,
    entity_id: str,
    user_id: str,
) -> dict[str, Any] | None:
    """Read only bounded fields needed by graph traversal and publication."""

    row = storage.execute(
        f"""SELECT id, user_id,
                   substr(name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS name,
                   entity_type, '[]' AS aliases_json,
                   substr(description, 1, {_GRAPH_ENTITY_DESCRIPTION_MAX_CHARS}) AS description,
                   '{{}}' AS metadata_json, canonical, merged_into_id, version,
                   created_at, updated_at, deleted_at
              FROM entities e WHERE id=? AND user_id=?
                AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
        (entity_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _historical_entity_relations(
    storage: StorageShared,
    entity_id: str,
    user_id: str,
    *,
    include_invalidated: bool,
    as_of: str,
    known_at: str,
    require_live_endpoints: bool = True,
    relation_types: Sequence[str] = (),
    entity_types: Sequence[str] = (),
    min_weight: float = 0.0,
    root_entity_id: str = "",
    row_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read one node from a previously validated transaction snapshot."""

    clauses = [
        "r.deleted_at IS NULL",
        "r.user_id=?",
        _not_private_relation_dependency("r"),
    ]
    params: list[Any] = [user_id, known_at, entity_id, entity_id, user_id]
    if as_of:
        clauses.append("(r.valid_from = '' OR r.valid_from <= ?)")
        params.append(as_of)
        clauses.append("(r.valid_to IS NULL OR r.valid_to > ?)")
        params.append(as_of)
    elif not include_invalidated:
        clauses.append("r.valid_to IS NULL")
    wanted_relations = [str(item).strip() for item in relation_types if str(item).strip()]
    if wanted_relations:
        clauses.append(f"r.relation_type IN ({','.join('?' * len(wanted_relations))})")
        params.extend(wanted_relations)
    if min_weight > 0:
        clauses.append("r.weight>=?")
        params.append(float(min_weight))
    wanted_entities = [str(item).strip() for item in entity_types if str(item).strip()]
    if wanted_entities:
        placeholders = ",".join("?" * len(wanted_entities))
        clauses.append(
            f"""((r.source_entity_id=? AND (t.entity_type IN ({placeholders}) OR t.id=?))
                  OR (r.target_entity_id=? AND (s.entity_type IN ({placeholders}) OR s.id=?)))"""
        )
        params.extend(
            [
                entity_id,
                *wanted_entities,
                root_entity_id,
                entity_id,
                *wanted_entities,
                root_entity_id,
            ]
        )
    # Latest revision is fixed before endpoint/valid/delete filters. `user_id` is
    # the only safe ranked prefilter because schema 31 makes relation ownership and
    # id immutable; the repeated outer predicate remains tenant defence-in-depth.
    # Moving any mutable filter into `ranked` would resurrect an older matching row
    # after an endpoint change, invalidation, soft-delete or physical tombstone.
    source_live = " AND " + _not_private_entity_material_dependency("s")
    target_live = " AND " + _not_private_entity_material_dependency("t")
    if require_live_endpoints:
        source_live += " AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL"
        target_live += " AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL"
    limit_clause = ""
    order_clause = "r.created_at DESC, r.relation_id"
    if row_limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(1, min(int(row_limit), _ENTITY_GRAPH_PAGE_SIZE)))
        order_clause = (
            "r.weight DESC, r.relation_type COLLATE NOCASE, "
            "r.source_entity_id, r.target_entity_id, r.relation_id"
        )
    query = f"""WITH ranked AS (
            SELECT rr.event_seq, rr.relation_id, rr.recorded_at, rr.present,
                   ROW_NUMBER() OVER (
                       PARTITION BY rr.relation_id
                       ORDER BY rr.recorded_at DESC, rr.event_seq DESC
                   ) AS snapshot_rank
            FROM relation_revisions rr
            WHERE rr.user_id=? AND rr.recorded_at<=?
        ), selected AS (
            SELECT event_seq FROM ranked WHERE snapshot_rank=1 AND present=1
        )
        SELECT r.relation_id AS id, r.user_id, r.source_entity_id,
               r.target_entity_id, r.relation_type, r.weight,
               CASE WHEN length(CAST(r.metadata_json AS BLOB))
                              <= {_GRAPH_RELATION_METADATA_MAX_BYTES}
                    THEN r.metadata_json ELSE '{{}}' END AS metadata_json,
               r.created_at, r.deleted_at, r.valid_from,
               r.valid_to, r.invalidated_at,
               {_visible_superseding_relation_id("r")} AS superseded_by,
               substr(s.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS source_name,
               substr(t.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS target_name
        FROM selected latest
        JOIN relation_revisions r ON r.event_seq=latest.event_seq
        JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id{source_live}
        JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id{target_live}
        WHERE (r.source_entity_id=? OR r.target_entity_id=?)
          AND {" AND ".join(clauses)}
        ORDER BY {order_clause}
        {limit_clause}"""  # nosec B608
    rows = storage.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def _current_entity_relations_for_traversal(
    storage: StorageShared,
    entity_id: str,
    user_id: str,
    *,
    as_of: str,
    require_live_endpoints: bool = False,
    relation_types: Sequence[str] = (),
    entity_types: Sequence[str] = (),
    min_weight: float = 0.0,
    root_entity_id: str = "",
    row_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Tenant-safe raw current rows for canonicalizing legacy merge endpoints.

    Public direct reads require both endpoints to be live. Query-context
    traversal is different: it follows an old endpoint through its current
    ``merged_into_id`` and publishes only the resolved live canonical entity.
    Keeping that seam explicit prevents a direct API from exposing tombstones
    while retaining the compatibility bridge for legacy relations.
    """

    clauses = [
        "r.deleted_at IS NULL",
        "r.user_id=?",
        _not_private_relation_dependency("r"),
    ]
    params: list[Any] = [entity_id, entity_id, user_id]
    if as_of:
        clauses.extend(
            [
                "(r.valid_from = '' OR r.valid_from <= ?)",
                "(r.valid_to IS NULL OR r.valid_to > ?)",
            ]
        )
        params.extend([as_of, as_of])
    else:
        clauses.append("r.valid_to IS NULL")
    wanted_relations = [str(item).strip() for item in relation_types if str(item).strip()]
    if wanted_relations:
        clauses.append(f"r.relation_type IN ({','.join('?' * len(wanted_relations))})")
        params.extend(wanted_relations)
    if min_weight > 0:
        clauses.append("r.weight>=?")
        params.append(float(min_weight))
    wanted_entities = [str(item).strip() for item in entity_types if str(item).strip()]
    if wanted_entities:
        placeholders = ",".join("?" * len(wanted_entities))
        clauses.append(
            f"""((r.source_entity_id=? AND (t.entity_type IN ({placeholders}) OR t.id=?))
                  OR (r.target_entity_id=? AND (s.entity_type IN ({placeholders}) OR s.id=?)))"""
        )
        params.extend(
            [
                entity_id,
                *wanted_entities,
                root_entity_id,
                entity_id,
                *wanted_entities,
                root_entity_id,
            ]
        )
    source_state = " AND " + _not_private_entity_material_dependency("s")
    target_state = " AND " + _not_private_entity_material_dependency("t")
    if require_live_endpoints:
        source_state += " AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL"
        target_state += " AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL"
    limit_clause = ""
    order_clause = "r.created_at DESC, r.id"
    if row_limit is not None:
        limit_clause = " LIMIT ?"
        params.append(max(1, min(int(row_limit), _ENTITY_GRAPH_PAGE_SIZE)))
        order_clause = (
            "r.weight DESC, r.relation_type COLLATE NOCASE, r.source_entity_id, r.target_entity_id, r.id"
        )
    rows = storage.execute(
        f"""SELECT r.id, r.user_id, r.source_entity_id, r.target_entity_id,
                    r.relation_type, r.weight,
                    CASE WHEN length(CAST(r.metadata_json AS BLOB))
                                   <= {_GRAPH_RELATION_METADATA_MAX_BYTES}
                         THEN r.metadata_json ELSE '{{}}' END AS metadata_json,
                    r.created_at, r.deleted_at, r.valid_from, r.valid_to,
                    r.invalidated_at,
                    {_visible_superseding_relation_id("r")} AS superseded_by,
                    substr(s.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS source_name,
                    substr(t.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS target_name
              FROM relations r
              JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id{source_state}
              JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id{target_state}
             WHERE (r.source_entity_id=? OR r.target_entity_id=?)
               AND {" AND ".join(clauses)}
             ORDER BY {order_clause}
             {limit_clause}""",  # nosec B608
        tuple(params),
    ).fetchall()
    return [dict(row) for row in rows]


def _bounded_entity_relation_rows(
    storage: StorageShared,
    entity_id: str,
    user_id: str,
    *,
    as_of: str,
    known_at: str,
    history_status: Mapping[str, Any] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Read one bounded public relation page and an honest lower bound."""

    bounded = max(1, min(int(limit), _ENTITY_GRAPH_EDGE_BUDGET))
    row_limit = bounded + 1
    if history_status:
        _assert_entities_existed_at_boundary(storage, user_id, [entity_id], known_at)
        rows = _historical_entity_relations(
            storage,
            entity_id,
            user_id,
            include_invalidated=False,
            as_of=as_of,
            known_at=known_at,
            row_limit=row_limit,
        )
        _assert_entities_existed_at_boundary(
            storage,
            user_id,
            [
                str(endpoint)
                for row in rows
                for endpoint in (row["source_entity_id"], row["target_entity_id"])
            ],
            known_at,
        )
    else:
        rows = _current_entity_relations_for_traversal(
            storage,
            entity_id,
            user_id,
            as_of=as_of,
            require_live_endpoints=True,
            row_limit=row_limit,
        )
    matched_at_least = len(rows)
    return rows[:bounded], matched_at_least, len(rows) > bounded


def _temporal_graph_overview(
    storage: StorageShared,
    user_id: str,
    *,
    limit: int,
    entity_types: Sequence[str],
    relation_types: Sequence[str],
    min_confidence: float,
    as_of: str,
    history_status: Mapping[str, Any] | None,
    search: str,
    hide_isolates: bool,
) -> dict[str, Any]:
    """Build an explicit temporal picture solely from the relation projection."""

    history_watermark = (
        _relation_revision_watermark(storage, user_id, str(history_status["known_at"]))
        if history_status
        else None
    )

    relation_conditions = [
        "r.user_id=?",
        "r.deleted_at IS NULL",
        _not_private_entity_material_dependency("s"),
        _not_private_entity_material_dependency("t"),
        _not_private_relation_dependency("r"),
    ]
    if history_status is None:
        cte = """WITH relation_projection AS (
                     SELECT r.id, r.source_entity_id AS source, r.target_entity_id AS target,
                            r.relation_type, r.weight
                       FROM relations r
                       JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                            AND s.deleted_at IS NULL AND s.canonical=1
                            AND s.merged_into_id IS NULL
                       JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                            AND t.deleted_at IS NULL AND t.canonical=1
                            AND t.merged_into_id IS NULL
                      WHERE {conditions}
                 )"""
        relation_parameters: list[Any] = [user_id]
    else:
        # Latest revision wins before every mutable predicate.  Endpoint, delete,
        # valid-time, confidence and relation-kind filters below therefore cannot
        # resurrect an older row which happened to match them.
        cte = """WITH ranked AS (
                     SELECT rr.event_seq, rr.relation_id, rr.recorded_at,
                            rr.present, rr.user_id, rr.source_entity_id,
                            rr.target_entity_id, rr.relation_type, rr.weight,
                            rr.metadata_json, rr.deleted_at, rr.valid_from, rr.valid_to,
                            ROW_NUMBER() OVER (
                                PARTITION BY rr.relation_id
                                ORDER BY rr.recorded_at DESC, rr.event_seq DESC
                            ) AS snapshot_rank
                       FROM relation_revisions rr
                      WHERE rr.user_id=? AND rr.recorded_at<=?
                 ), relation_projection AS (
                     SELECT r.relation_id AS id, r.source_entity_id AS source,
                            r.target_entity_id AS target, r.relation_type, r.weight
                       FROM ranked r
                       JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                            AND s.deleted_at IS NULL AND s.canonical=1
                            AND s.merged_into_id IS NULL
                       JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                            AND t.deleted_at IS NULL AND t.canonical=1
                            AND t.merged_into_id IS NULL
                      WHERE r.snapshot_rank=1 AND r.present=1 AND {conditions}
                 )"""
        relation_parameters = [user_id, history_status["known_at"], user_id]
    if as_of:
        relation_conditions.extend(
            [
                "(r.valid_from = '' OR r.valid_from <= ?)",
                "(r.valid_to IS NULL OR r.valid_to > ?)",
            ]
        )
        relation_parameters.extend([as_of, as_of])
    else:
        relation_conditions.append("r.valid_to IS NULL")
    confidence_floor = max(0.0, min(float(min_confidence), 1.0))
    if confidence_floor:
        relation_conditions.append("r.weight>=?")
        relation_parameters.append(confidence_floor)
    wanted_relations = [str(item).strip() for item in relation_types if str(item).strip()]
    if wanted_relations:
        relation_conditions.append(f"r.relation_type IN ({','.join('?' * len(wanted_relations))})")
        relation_parameters.extend(wanted_relations)
    relation_cte = cte.format(conditions=" AND ".join(relation_conditions))

    node_conditions = [
        "e.user_id=?",
        "e.deleted_at IS NULL",
        "e.merged_into_id IS NULL",
        _not_private_entity_material_dependency("e"),
    ]
    node_parameters: list[Any] = [user_id]
    wanted_entities = [str(item).strip() for item in entity_types if str(item).strip()]
    if wanted_entities:
        node_conditions.append(f"e.entity_type IN ({','.join('?' * len(wanted_entities))})")
        node_parameters.extend(wanted_entities)
    needle = str(search or "").strip()
    if needle:
        node_conditions.append("e.name LIKE ? ESCAPE '\\'")
        escaped = needle.replace("%", r"\%").replace("_", r"\_")
        node_parameters.append(f"%{escaped}%")
    bounded = max(1, min(int(limit), 500))
    node_rows = storage.execute(
        f"""{relation_cte}, endpoints AS (
                 SELECT source AS id FROM relation_projection
                 UNION
                 SELECT target AS id FROM relation_projection
             ), candidate_nodes AS (
                 SELECT e.id,
                        substr(e.name, 1, {_GRAPH_ENTITY_NAME_MAX_CHARS}) AS name,
                        e.entity_type,
                        (SELECT COUNT(*) FROM relation_projection p
                          WHERE p.source=e.id OR p.target=e.id) AS relation_count
                   FROM endpoints x
                   JOIN entities e ON e.id=x.id
                  WHERE {" AND ".join(node_conditions)}
             )
             SELECT id, name, entity_type, 0 AS knowledge_count,
                    COUNT(*) OVER () AS nodes_matched
               FROM candidate_nodes
              ORDER BY relation_count DESC, name COLLATE NOCASE, id
              LIMIT ?""",  # nosec B608 -- clauses are fixed; values are bound
        (*relation_parameters, *node_parameters, bounded),
    ).fetchall()
    nodes = [
        {
            "id": row["id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            # Link history does not exist.  Zero is an explicit neutral display
            # value, never an eligibility/ranking signal from today's links.
            "knowledge_count": 0,
        }
        for row in node_rows
    ]
    nodes_matched = int(node_rows[0]["nodes_matched"] or 0) if node_rows else 0
    ids = [str(node["id"]) for node in nodes]
    edges: list[dict[str, Any]] = []
    edges_matched = 0
    if ids:
        placeholders = ",".join("?" * len(ids))
        edge_rows = storage.execute(
            f"""{relation_cte}
                 SELECT id, source, target, relation_type, weight,
                        COUNT(*) OVER () AS edges_matched
                   FROM relation_projection
                  WHERE source IN ({placeholders}) AND target IN ({placeholders})
                  ORDER BY weight DESC, relation_type COLLATE NOCASE, source, target, id
                  LIMIT 801""",  # nosec B608 -- placeholders only
            (*relation_parameters, *ids, *ids),
        ).fetchall()
        edges_matched = int(edge_rows[0]["edges_matched"] or 0) if edge_rows else 0
        edges = [
            {
                "id": row["id"],
                "source": row["source"],
                "target": row["target"],
                "relation_type": row["relation_type"],
                "weight": row["weight"],
                "kind": "relation",
            }
            for row in edge_rows[:800]
        ]
    if hide_isolates:
        connected = {str(edge["source"]) for edge in edges} | {str(edge["target"]) for edge in edges}
        nodes = [node for node in nodes if str(node["id"]) in connected]
    result: dict[str, Any] = {
        "nodes": nodes,
        "edges": edges,
        "shown": len(nodes),
        "total": nodes_matched,
        "nodes_matched_at_least": nodes_matched,
        "nodes_truncated": nodes_matched > len(nodes),
        "edges_matched_at_least": edges_matched,
        "edges_truncated": edges_matched > len(edges),
        "as_of": as_of,
        "known_at": str((history_status or {}).get("known_at") or ""),
        "identity_basis": "current_names",
        "temporal_basis": "bitemporal" if history_status else "valid_time",
    }
    if history_status:
        if (
            _relation_revision_watermark(storage, user_id, str(history_status["known_at"]))
            != history_watermark
        ):
            raise RelationHistorySnapshotError("relation history changed while building the graph overview")
        result.update(history_status)
    return result


class GraphMixin(StorageShared):
    def list_part_of_relations(self, user_id: str) -> list[dict[str, Any]]:
        """Active PART_OF edges; source is the child, target the parent."""
        rows = self.execute(
            f"""SELECT r.source_entity_id, r.target_entity_id, r.weight FROM relations r
                JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                     AND {_not_private_entity_material_dependency("s")}
                JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                     AND {_not_private_entity_material_dependency("t")}
                WHERE r.user_id=? AND r.relation_type=? AND r.deleted_at IS NULL
                  AND {_not_private_relation_dependency("r")}
                  AND r.valid_to IS NULL ORDER BY r.weight DESC""",  # nosec B608
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
                _relation_batch_timestamp(conn),
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
            visible = conn.execute(
                f"""SELECT 1 FROM entities e WHERE e.id=? AND e.user_id=?
                     AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity.id, entity.user_id),
            ).fetchone()
            if visible is None:
                raise ValueError("Entity fields reference private graph material")
            self._store_entity_version(conn, row)
        return entity

    def update_entity(self, entity: Entity) -> Entity:
        # Same shape as `update_knowledge_object`, same fix: the version is read
        # inside the transaction that writes it. Read-then-lock let two editors both
        # see version 1, both compute 2, and the loser's UPDATE disappear together
        # with its snapshot — `_store_entity_version` is INSERT OR IGNORE on
        # (entity, version), so the duplicate is dropped without a word.
        with self.transaction() as conn:
            existing_row = conn.execute(
                f"""SELECT e.* FROM entities e WHERE e.id=? AND e.user_id=?
                     AND {_not_private_reminder_entity("e")}""",  # nosec B608
                (entity.id, entity.user_id),
            ).fetchone()
            existing = dict(existing_row) if existing_row else None
            if not existing:
                raise ValueError("Entity not found for user")
            entity.version = max(int(existing.get("version", 1)) + 1, int(entity.version))
            entity.updated_at = utc_now()
            row = entity.to_row()
            row["normalized_name"] = normalize_entity_name(entity.name)
            try:
                conn.execute(
                    """UPDATE entities SET name=:name, normalized_name=:normalized_name,
                       entity_type=:entity_type, aliases_json=:aliases_json,
                       description=:description, metadata_json=:metadata_json,
                       canonical=:canonical, merged_into_id=:merged_into_id,
                       version=:version, updated_at=:updated_at, deleted_at=:deleted_at
                       WHERE id=:id AND user_id=:user_id""",
                    row,
                )
            except sqlite3.IntegrityError as exc:
                if "private entity material is immutable" not in str(exc):
                    raise
                raise ValueError("Entity fields reference private graph material") from exc
            visible = conn.execute(
                f"""SELECT 1 FROM entities e WHERE e.id=? AND e.user_id=?
                     AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity.id, entity.user_id),
            ).fetchone()
            if visible is None:
                raise ValueError("Entity fields reference private graph material")
            self._store_entity_version(conn, row)
        return entity

    def get_entity(self, entity_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute(
                f"""SELECT e.* FROM entities e WHERE e.id=?
                     AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity_id,),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT e.* FROM entities e WHERE e.id=? AND e.user_id=?
                     AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_entity_versions(self, entity_id: str, user_id: str) -> list[dict[str, Any]]:
        if self.get_entity(entity_id, user_id) is None:
            return []
        rows = self.execute(
            f"""SELECT v.* FROM entity_versions v
                  JOIN entities current_entity
                    ON current_entity.id=v.entity_id AND current_entity.user_id=v.user_id
                   AND {_not_private_entity_material_dependency("current_entity")}
                 WHERE v.entity_id=? AND v.user_id=?
                   AND {
                _not_private_bounded_json_dependency(
                    "v.snapshot_json",
                    "v.user_id",
                    max_bytes=_PUBLIC_ENTITY_VERSION_SNAPSHOT_MAX_BYTES,
                )
            }
                 ORDER BY v.version DESC, v.id DESC""",  # nosec B608
            (entity_id, user_id),
        ).fetchall()
        versions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            snapshot = _public_entity_version_snapshot(
                self,
                item.get("snapshot_json"),
                entity_id=entity_id,
                user_id=user_id,
                version=int(item.get("version") or 0),
            )
            if snapshot is None:
                continue
            item["snapshot_json"] = snapshot
            versions.append(item)
        return versions

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
        where = "user_id=? AND " + _not_private_entity_material_dependency("entities")
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
        known_at: str = "",
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

        В текущей картине узлы отбираются по числу связанных документов. При
        явном `as_of`/`known_at` у ссылок на документы нет нужной истории, поэтому
        они не используются вообще: узлы берутся из выбранной relation-проекции и
        ранжируются по числу её рёбер. Оба пути ограничены `limit`; сколько осталось
        за кадром, temporal-ответ называет отдельными matched/truncated полями.

        Фильтры сужают ОТБОР УЗЛОВ, а не только рисование: обрезав картинку в
        браузере, мы показали бы «сто самых связанных сущностей, из которых нужного
        типа оказалось три», выдавая это за три сущности этого типа. У текущего
        пути `total` продолжает считать весь архив; у temporal-пути — все
        relation-достижимые узлы после его фильтров.
        """
        # Both caller-owned boundaries are parsed before any entity/link read.
        # In particular, a missing graph must not turn malformed `as_of` into an
        # ordinary empty result.
        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        normalized_known_at = normalize_known_at(known_at) if str(known_at or "").strip() else ""
        history_status: dict[str, Any] | None = None
        if normalized_known_at:
            history_status = _canonical_relation_history_status(
                self.relation_history_status(user_id, normalized_known_at),
                requested_known_at=normalized_known_at,
            )
        if as_of or history_status:
            result = _temporal_graph_overview(
                self,
                user_id,
                limit=limit,
                entity_types=entity_types or (),
                relation_types=relation_types or (),
                min_confidence=min_confidence,
                as_of=as_of,
                history_status=history_status,
                search=search,
                hide_isolates=hide_isolates,
            )
            if history_status:
                confirmed = _canonical_relation_history_status(
                    self.relation_history_status(user_id, normalized_known_at),
                    requested_known_at=normalized_known_at,
                )
                if confirmed != history_status:
                    raise RelationHistorySnapshotError(
                        "relation history status changed while building the graph overview"
                    )
                result.update(confirmed)
            return result

        bounded = max(1, min(int(limit), 500))
        conditions = [
            "e.user_id = ?",
            "e.deleted_at IS NULL",
            "e.canonical=1",
            "e.merged_into_id IS NULL",
            _not_private_entity_material_dependency("e"),
        ]
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
            f"""SELECT substr(e.id,1,160) AS id, substr(e.name,1,240) AS name,
                       substr(e.entity_type,1,80) AS entity_type,
                       COUNT(l.knowledge_object_id) AS knowledge_count,
                       COUNT(*) OVER () AS nodes_matched
               FROM entities e
               JOIN knowledge_entity_links l
                 ON l.entity_id = e.id AND l.user_id = e.user_id AND l.status = 'accepted'
               JOIN knowledge_objects k
                 ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                AND k.deleted_at IS NULL
                AND {_not_private_knowledge_dependency("k")}
               WHERE {" AND ".join(conditions)}
               GROUP BY e.id
               ORDER BY knowledge_count DESC, e.name COLLATE NOCASE, e.id
               LIMIT ?""",  # nosec B608 — условия собраны из литералов, значения связаны
            (*parameters, bounded),
        ).fetchall()
        nodes = [
            {
                "id": row["id"],
                "name": row["name"],
                "entity_type": row["entity_type"],
                "knowledge_count": row["knowledge_count"],
            }
            for row in rows
        ]
        nodes_matched = int(rows[0]["nodes_matched"] or 0) if rows else 0
        ids = [str(node["id"]) for node in nodes]
        if not ids:
            return {
                "nodes": [],
                "edges": [],
                "shown": 0,
                "total": self.count_entities(user_id),
                "nodes_matched_at_least": 0,
                "nodes_truncated": False,
                "edges_matched_at_least": 0,
                "edges_truncated": False,
                "as_of": "",
                "known_at": "",
                "identity_basis": "current_names",
                "temporal_basis": "valid_time",
            }

        placeholders = ",".join("?" * len(ids))
        # Совместная встречаемость считается ТОЛЬКО между показанными узлами: ребро в
        # невидимый узел рисовать некуда, а считать его в статистику — врать.
        cooccurrence: list[Any] = []
        # Co-occurrence is a timeless derivative of TODAY'S accepted document
        # links. Mixing it into an explicit valid-time or transaction snapshot
        # would make an old picture depend on links accepted after that boundary.
        if not only_relations and history_status is None and not as_of:
            floor = max(1, int(min_weight))
            cooccurrence = self.execute(
                f"""SELECT a.entity_id AS source, b.entity_id AS target,
                           COUNT(DISTINCT a.knowledge_object_id) AS weight,
                           COUNT(*) OVER () AS edges_matched
                    FROM knowledge_entity_links a
                    JOIN knowledge_entity_links b
                      ON b.knowledge_object_id = a.knowledge_object_id
                     AND b.user_id = a.user_id AND b.entity_id > a.entity_id
                    JOIN knowledge_objects k
                      ON k.id=a.knowledge_object_id AND k.user_id=a.user_id
                     AND k.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("k")}
                    WHERE a.user_id = ? AND a.status = 'accepted' AND b.status = 'accepted'
                      AND a.entity_id IN ({placeholders}) AND b.entity_id IN ({placeholders})
                    GROUP BY a.entity_id, b.entity_id
                    HAVING weight >= ?
                    ORDER BY weight DESC, source, target
                    LIMIT 800""",  # nosec B608
                (user_id, *ids, *ids, floor),
            ).fetchall()
        relation_conditions = [
            "r.user_id = ?",
            f"r.source_entity_id IN ({placeholders})",
            f"r.target_entity_id IN ({placeholders})",
            "r.deleted_at IS NULL",
            _not_private_relation_dependency("r"),
        ]
        relation_parameters: list[Any]
        relation_source: str
        relation_source = ""
        relation_parameters = [user_id, *ids, *ids]
        relation_table = "relations r"
        # Отменённая связь на общей картине не рисуется: «служит в в/ч А» и
        # «служит в в/ч Б» рядом читаются как одновременные.
        relation_conditions.append("r.valid_to IS NULL")
        floor_confidence = max(0.0, min(float(min_confidence), 1.0))
        if floor_confidence > 0:
            relation_conditions.append("r.weight >= ?")
            relation_parameters.append(floor_confidence)
        wanted_relations = [str(item).strip() for item in (relation_types or []) if str(item).strip()]
        if wanted_relations:
            relation_conditions.append(f"r.relation_type IN ({','.join('?' * len(wanted_relations))})")
            relation_parameters.extend(wanted_relations)
        relations = self.execute(
            f"""{relation_source}
                SELECT r.id, r.source_entity_id AS source, r.target_entity_id AS target,
                       r.relation_type, r.weight, COUNT(*) OVER () AS edges_matched
                FROM {relation_table}
                WHERE {" AND ".join(relation_conditions)}
                ORDER BY r.weight DESC, r.relation_type COLLATE NOCASE,
                         r.source_entity_id, r.target_entity_id, r.id
                LIMIT 800""",  # nosec B608
            tuple(relation_parameters),
        ).fetchall()
        cooccurrence_matched = int(cooccurrence[0]["edges_matched"] or 0) if cooccurrence else 0
        relation_edges_matched = int(relations[0]["edges_matched"] or 0) if relations else 0
        edges = [
            {
                "source": row["source"],
                "target": row["target"],
                "weight": row["weight"],
                "kind": "cooccurrence",
            }
            for row in cooccurrence
        ]
        edges.extend(
            {
                "id": row["id"],
                "source": row["source"],
                "target": row["target"],
                "relation_type": row["relation_type"],
                "weight": row["weight"],
                "kind": "relation",
            }
            for row in relations
        )
        edges_matched = cooccurrence_matched + relation_edges_matched
        if hide_isolates:
            # Узел без единого ребра занимает место и ничего не рассказывает. Убирать
            # его — решение ЗРИТЕЛЯ, поэтому по умолчанию он на месте, а `shown`
            # ниже считается после отсева, чтобы подпись не расходилась с картинкой.
            connected = {str(edge["source"]) for edge in edges} | {str(edge["target"]) for edge in edges}
            nodes = [node for node in nodes if str(node["id"]) in connected]
        result = {
            "nodes": nodes,
            "edges": edges,
            "shown": len(nodes),
            "total": self.count_entities(user_id),
            "nodes_matched_at_least": nodes_matched,
            "nodes_truncated": nodes_matched > len(nodes),
            "edges_matched_at_least": edges_matched,
            "edges_truncated": edges_matched > len(edges),
            "as_of": "",
            "known_at": "",
            "identity_basis": "current_names",
            "temporal_basis": "valid_time",
        }
        return result

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
                    "list_entities returned %d of %d entities — the tail is "
                    "invisible to entity matching and graph expansion",
                    bounded,
                    total,
                )
        return [dict(row) for row in rows]

    def find_entity_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        normalized = normalize_entity_name(name)
        row = self.execute(
            f"""SELECT {_entity_search_projection()} FROM entities e
                WHERE e.user_id=? AND e.normalized_name=?
                  AND e.deleted_at IS NULL AND e.canonical=1
                  AND e.merged_into_id IS NULL
                  AND {_not_private_entity_material_dependency("e")}
                ORDER BY e.updated_at DESC LIMIT 1""",  # nosec B608
            (user_id, normalized),
        ).fetchone()
        return dict(row) if row else None

    def find_entity_by_alias(
        self,
        user_id: str,
        alias: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Entities whose alias normalises to ``alias``. No full-graph page cap.

        The previous path walked ``list_entities(limit=5000)`` and therefore lost
        every alias that lived past the alphabetical ceiling — the same silent
        blindness as ``match_mentions``. Only rows that actually carry aliases are
        loaded; empty ``[]`` is the common case and is filtered in SQL.
        """
        normalized = normalize_entity_name(alias)
        if not normalized:
            return []
        bounded = max(1, min(int(limit), 200))
        results: list[dict[str, Any]] = []
        for item in _iter_alias_entities(self, user_id):
            aliases = _json_load(item.get("aliases_json"), [])
            if any(normalize_entity_name(alias) == normalized for alias in aliases):
                results.append(item)
                if len(results) >= bounded:
                    break
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
            f"AND deleted_at IS NULL AND canonical=1 "
            f"AND {_not_private_entity_material_dependency('entities')} AND ({conditions}) "  # nosec B608
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
        limit: int = 800,
    ) -> list[dict[str, Any]]:
        """Canonical entities matching any of the given names (or their aliases).

        Callers hand terms extracted from text; this method never lists the whole
        graph. A graph past the ``list_entities`` ceiling of 5000 stays fully
        addressable — the lookup is keyed on ``normalized_name`` (and, optionally,
        alias JSON for the minority of nodes that carry one).
        """
        bounded = max(1, min(int(limit), 800))
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in names:
            key = normalize_entity_name(str(raw or ""))
            if not key or key in seen:
                continue
            seen.add(key)
            wanted.append(key)
            if len(wanted) >= bounded:
                break
        if not wanted:
            return []

        by_id: dict[str, dict[str, Any]] = {}
        # SQLite caps host parameters; stay well under the common 999 limit.
        chunk_size = 400
        for start in range(0, len(wanted), chunk_size):
            chunk = wanted[start : start + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = self.execute(
                f"""SELECT {_entity_search_projection()} FROM entities e
                    WHERE e.user_id=? AND e.deleted_at IS NULL AND e.canonical=1
                      AND e.merged_into_id IS NULL
                      AND {_not_private_entity_material_dependency("e")}
                      AND e.normalized_name IN ({placeholders})""",  # nosec B608
                (user_id, *chunk),
            ).fetchall()
            for row in rows:
                by_id[str(row["id"])] = dict(row)
                if len(by_id) >= bounded:
                    break
            if len(by_id) >= bounded:
                break

        if include_aliases and len(by_id) < bounded:
            wanted_set = set(wanted)
            for item in _iter_alias_entities(self, user_id):
                entity_id = str(item["id"])
                if entity_id in by_id:
                    continue
                aliases = _json_load(item.get("aliases_json"), [])
                if any(normalize_entity_name(str(alias)) in wanted_set for alias in aliases):
                    by_id[entity_id] = item
                    if len(by_id) >= bounded:
                        break

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
        if len(source) > 256 or len(precision) > 40:
            raise ValueError("Event time provenance is too large")
        reminder_person = source[len("reminder:") :] if source.startswith("reminder:") else ""
        if source.startswith("reminder:") and (not reminder_person or reminder_person != user_id):
            raise ValueError("Reminder owner does not match the event owner")
        record: dict[str, Any] = {}
        with self.transaction() as conn:
            visible_event = conn.execute(
                f"""SELECT 1 FROM entities e
                     WHERE e.id=? AND e.user_id=? AND e.entity_type='event'
                       AND e.deleted_at IS NULL AND e.canonical=1
                       AND e.merged_into_id IS NULL
                       AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
                (entity_id, user_id),
            ).fetchone()
            if visible_event is None:
                raise ValueError("Event entity not found")
            conn.execute(
                """INSERT INTO entity_time(entity_id, user_id, occurred_at, occurred_end,
                   precision, source, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     user_id=excluded.user_id,
                     occurred_at=excluded.occurred_at,
                     occurred_end=excluded.occurred_end,
                     precision=excluded.precision,
                     source=excluded.source,
                     updated_at=excluded.updated_at""",
                (entity_id, user_id, occurred_at, occurred_end, precision, source, utc_now()),
            )
            if reminder_person:
                conn.execute(
                    """INSERT INTO private_entity_owners(
                           entity_id, person_id, privacy_kind, created_at)
                       VALUES(?, ?, 'reminder', ?)
                       ON CONFLICT(entity_id) DO NOTHING""",
                    (entity_id, reminder_person, utc_now()),
                )
            stored = conn.execute(
                "SELECT * FROM entity_time WHERE entity_id=? AND user_id=?",
                (entity_id, user_id),
            ).fetchone()
            record = dict(stored) if stored else {}
        return record

    def get_entity_time(self, entity_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT t.* FROM entity_time t
                  JOIN entities e ON e.id=t.entity_id AND e.user_id=t.user_id
                 WHERE t.entity_id=? AND t.user_id=?
                   AND {_not_private_entity_material_dependency("e")}""",  # nosec B608
            (entity_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def delete_entity_time(self, entity_id: str, user_id: str | None = None) -> bool:
        clause = " AND t.user_id=?" if user_id else ""
        params: tuple[Any, ...] = (entity_id, user_id) if user_id else (entity_id,)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""DELETE FROM entity_time AS t
                      WHERE t.entity_id=?{clause}
                        AND EXISTS (
                            SELECT 1 FROM entities e
                             WHERE e.id=t.entity_id AND e.user_id=t.user_id
                               AND {_not_private_entity_material_dependency("e")}
                        )""",  # nosec B608
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
            _not_disallowed_private_material_for_person("e"),
            "((COALESCE(t.source,'') NOT LIKE 'reminder:%' "
            "AND NOT EXISTS (SELECT 1 FROM private_entity_owners shared_private "
            "WHERE shared_private.entity_id=e.id)) "
            "OR (COALESCE(t.source,'')=? "
            "AND EXISTS (SELECT 1 FROM private_entity_owners private_owner "
            "WHERE private_owner.entity_id=e.id AND private_owner.person_id=?)))",
        ]
        params: list[Any] = [user_id, f"reminder:{user_id}", user_id]
        if start:
            clauses.append("t.occurred_at >= ?")
            params.append(start)
        if end:
            clauses.append("t.occurred_at <= ?")
            params.append(end)
        params.append(max(1, min(int(limit), 2000)))
        rows = self.execute(
            "SELECT substr(e.id, 1, 160) AS entity_id, "
            "substr(e.name, 1, 240) AS name, "
            "substr(e.entity_type, 1, 80) AS entity_type, "
            "substr(e.description, 1, 500) AS description, "
            "substr(t.occurred_at, 1, 64) AS occurred_at, "
            "substr(COALESCE(t.occurred_end, ''), 1, 64) AS occurred_end, "
            "substr(COALESCE(t.precision, ''), 1, 40) AS precision, "
            "substr(COALESCE(t.source, ''), 1, 256) AS source "
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
        if mine:
            return _count_visible_timeline_events(
                self,
                user_id,
                mine,
                start=start,
                end=end,
            )
        clauses = [
            "e.user_id=?",
            "e.entity_type='event'",
            "e.canonical=1",
            "e.deleted_at IS NULL",
            _not_disallowed_private_material_for_person("e"),
            "((COALESCE(t.source,'') NOT LIKE 'reminder:%' "
            "AND NOT EXISTS (SELECT 1 FROM private_entity_owners shared_private "
            "WHERE shared_private.entity_id=e.id)) "
            "OR (COALESCE(t.source,'')=? "
            "AND EXISTS (SELECT 1 FROM private_entity_owners private_owner "
            "WHERE private_owner.entity_id=e.id AND private_owner.person_id=?)))",
        ]
        params: list[Any] = [user_id, f"reminder:{user_id}", user_id]
        if start:
            clauses.append("t.occurred_at >= ?")
            params.append(start)
        if end:
            clauses.append("t.occurred_at <= ?")
            params.append(end)
        row = self.execute(
            "SELECT COUNT(*) AS count FROM entity_time t "
            "JOIN entities e ON e.id=t.entity_id "
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
        visible_source = _not_private_entity_material_dependency("s")
        visible_target = _not_private_entity_material_dependency("t")
        rows = self.execute(
            f"""WITH relation_changes AS (
                   SELECT substr(r.id, 1, 160) AS relation_id,
                          substr(r.relation_type, 1, 80) AS relation_type,
                          substr(r.source_entity_id, 1, 160) AS source_entity_id,
                          substr(s.name, 1, 240) AS source_name,
                          substr(r.target_entity_id, 1, 160) AS target_entity_id,
                          substr(t.name, 1, 240) AS target_name,
                          substr(r.valid_from, 1, 64) AS valid_from,
                          substr(COALESCE(r.valid_to, ''), 1, 64) AS valid_to,
                          substr(r.created_at, 1, 64) AS created_at,
                          substr(COALESCE(r.invalidated_at, ''), 1, 64) AS invalidated_at,
                          {_visible_superseding_relation_id("r")} AS superseded_by,
                          substr(r.valid_from, 1, 64) AS at,
                          'confirmed' AS boundary
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                        AND {visible_source}
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                        AND {visible_target}
                   WHERE r.user_id=? AND r.deleted_at IS NULL AND r.valid_from <> ''
                     AND {_not_private_relation_dependency("r")}
                     AND (? IS NULL OR r.valid_from >= ?)
                     AND (? IS NULL OR r.valid_from <= ?)
                   UNION ALL
                   SELECT substr(r.id, 1, 160) AS relation_id,
                          substr(r.relation_type, 1, 80) AS relation_type,
                          substr(r.source_entity_id, 1, 160) AS source_entity_id,
                          substr(s.name, 1, 240) AS source_name,
                          substr(r.target_entity_id, 1, 160) AS target_entity_id,
                          substr(t.name, 1, 240) AS target_name,
                          substr(r.valid_from, 1, 64) AS valid_from,
                          substr(COALESCE(r.valid_to, ''), 1, 64) AS valid_to,
                          substr(r.created_at, 1, 64) AS created_at,
                          substr(COALESCE(r.invalidated_at, ''), 1, 64) AS invalidated_at,
                          {_visible_superseding_relation_id("r")} AS superseded_by,
                          substr(r.valid_to, 1, 64) AS at,
                          'ended' AS boundary
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                        AND {visible_source}
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                        AND {visible_target}
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND {_not_private_relation_dependency("r")}
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
               LIMIT ?""",  # nosec B608
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

        visible_source = _not_private_entity_material_dependency("s")
        visible_target = _not_private_entity_material_dependency("t")
        row = self.execute(
            f"""SELECT COUNT(*) AS count
               FROM (
                   SELECT r.id
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                       AND {visible_source}
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                       AND {visible_target}
                   WHERE r.user_id=? AND r.deleted_at IS NULL AND r.valid_from <> ''
                     AND {_not_private_relation_dependency("r")}
                     AND (? IS NULL OR r.valid_from >= ?)
                     AND (? IS NULL OR r.valid_from <= ?)
                   UNION ALL
                   SELECT r.id
                   FROM relations r
                   JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                       AND {visible_source}
                   JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                       AND {visible_target}
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND {_not_private_relation_dependency("r")}
                     AND r.valid_to IS NOT NULL AND r.valid_to <> ''
                     AND (? IS NULL OR r.valid_to >= ?)
                     AND (? IS NULL OR r.valid_to <= ?)
               )""",  # nosec B608
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
        if _is_private_reminder_entity_id(
            self, relation.source_entity_id, relation.user_id
        ) or _is_private_reminder_entity_id(self, relation.target_entity_id, relation.user_id):
            raise ValueError("Personal reminders cannot be linked into the shared knowledge graph")
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
        if (
            not source
            or not target
            or source.get("deleted_at")
            or target.get("deleted_at")
            or not bool(source.get("canonical", 1))
            or not bool(target.get("canonical", 1))
            or bool(source.get("merged_into_id"))
            or bool(target.get("merged_into_id"))
        ):
            raise ValueError("Both entities must belong to the same user")
        created = True
        persisted_row: sqlite3.Row | None = None
        with self.transaction() as conn:
            live_endpoints = conn.execute(
                f"""SELECT COUNT(*) AS count FROM entities
                    WHERE user_id=? AND id IN (?, ?) AND deleted_at IS NULL
                      AND canonical=1 AND merged_into_id IS NULL
                      AND {_not_private_entity_material_dependency("entities")}""",  # nosec B608
                (relation.user_id, relation.source_entity_id, relation.target_entity_id),
            ).fetchone()
            if not live_endpoints or int(live_endpoints["count"] or 0) != 2:
                raise ValueError("Both entities must belong to the same user")
            try:
                cursor = conn.execute(
                    """INSERT INTO relations(id, user_id, source_entity_id, target_entity_id,
                       relation_type, weight, metadata_json, created_at, deleted_at,
                       valid_from, valid_to, invalidated_at, superseded_by)
                       VALUES(:id, :user_id, :source_entity_id, :target_entity_id,
                       :relation_type, :weight, :metadata_json, :created_at, :deleted_at,
                       :valid_from, :valid_to, :invalidated_at, :superseded_by)""",
                    relation.to_row(),
                )
                # The schema-32 INSERT guard deliberately turns an exact
                # tombstone resurrection into a no-op when a later active row
                # already owns that tuple. Unlike a UNIQUE violation, SQLite's
                # RAISE(IGNORE) raises no exception; returning the caller's old ID
                # here would publish a relation which does not exist. Resolve the
                # same tenant-scoped active row as the ordinary idempotent path.
                if cursor.rowcount == 0:
                    created = False
                    row = conn.execute(
                        """SELECT * FROM relations WHERE user_id=? AND source_entity_id=?
                           AND target_entity_id=? AND relation_type=?
                           AND deleted_at IS NULL AND valid_to IS NULL""",
                        (
                            relation.user_id,
                            relation.source_entity_id,
                            relation.target_entity_id,
                            enum_value(relation.relation_type),
                        ),
                    ).fetchone()
                    if not row:
                        raise RuntimeError("relation insert was ignored without an active relation")
                    relation.id = str(row["id"])
                    persisted_row = row
            except sqlite3.IntegrityError:
                created = False
                row = conn.execute(
                    """SELECT * FROM relations WHERE user_id=? AND source_entity_id=?
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
                    persisted_row = row
                else:
                    raise
            if persisted_row is None:
                persisted_row = conn.execute(
                    "SELECT * FROM relations WHERE id=? AND user_id=?",
                    (relation.id, relation.user_id),
                ).fetchone()
            if persisted_row is None:
                raise RuntimeError("relation write completed without a persisted row")
            visible_persisted = conn.execute(
                f"""SELECT 1 FROM relations r
                     WHERE r.id=? AND r.user_id=?
                       AND {_not_private_relation_dependency("r")}""",  # nosec B608
                (relation.id, relation.user_id),
            ).fetchone()
            if visible_persisted is None:
                raise ValueError("Relation metadata references private knowledge")
        persisted_metadata = _json_load(persisted_row["metadata_json"], {})
        persisted = Relation(
            id=str(persisted_row["id"]),
            user_id=str(persisted_row["user_id"]),
            source_entity_id=str(persisted_row["source_entity_id"]),
            target_entity_id=str(persisted_row["target_entity_id"]),
            relation_type=str(persisted_row["relation_type"]),
            weight=float(persisted_row["weight"]),
            metadata_json=persisted_metadata if isinstance(persisted_metadata, dict) else {},
            created_at=str(persisted_row["created_at"]),
            deleted_at=persisted_row["deleted_at"],
            valid_from=str(persisted_row["valid_from"] or ""),
            valid_to=persisted_row["valid_to"],
            invalidated_at=persisted_row["invalidated_at"],
            superseded_by=persisted_row["superseded_by"],
        )
        # Relation is intentionally not extended with a persisted API field.
        # The route still needs to distinguish a write from an idempotent replay
        # so its append-only audit log does not claim a mutation that never ran.
        setattr(persisted, "_idempotent_replay", not created)  # noqa: B010
        return persisted

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
        visible_relation_from = f"""FROM relations r
            JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                 AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                 AND {_not_private_entity_material_dependency("s")}
            JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                 AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                 AND {_not_private_entity_material_dependency("t")}
            JOIN users relation_privacy_owner ON relation_privacy_owner.id=r.user_id
                 AND {_not_private_relation_dependency("r")}"""
        safe_relation_columns = f"""r.id, r.user_id, r.source_entity_id, r.target_entity_id,
            r.relation_type, r.weight, r.created_at, r.deleted_at, r.valid_from, r.valid_to,
            r.invalidated_at, {_visible_superseding_relation_id("r")} AS superseded_by,
            MIN(length(CAST(COALESCE(r.metadata_json,'') AS BLOB)),1000000000) AS metadata_bytes,
            CASE WHEN length(CAST(COALESCE(r.metadata_json,'') AS BLOB))
                           <={_GRAPH_RELATION_METADATA_MAX_BYTES}
                 THEN CASE WHEN json_valid(r.metadata_json)
                           THEN CASE WHEN json_type(r.metadata_json)='object'
                                     THEN r.metadata_json ELSE '{{}}' END
                           ELSE '{{}}' END
                 ELSE '{{}}' END AS metadata_json"""
        with self.transaction() as conn:
            row = conn.execute(
                f"""SELECT {safe_relation_columns} {visible_relation_from}
                     WHERE r.id=? AND r.user_id=? AND r.deleted_at IS NULL""",  # nosec B608
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
                    f"""SELECT 1 {visible_relation_from}
                         WHERE r.id=? AND r.user_id=? AND r.deleted_at IS NULL""",  # nosec B608
                    (superseded_by, user_id),
                ).fetchone()
                if not replacement:
                    raise ValueError("Связь-замена не найдена")
            metadata = _json_load(row["metadata_json"], {})
            if reason:
                if int(row["metadata_bytes"] or 0) > _GRAPH_RELATION_METADATA_MAX_BYTES:
                    raise ValueError("Relation metadata is too large to amend safely")
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
            else:
                conn.execute(
                    """UPDATE relations SET valid_to=?, invalidated_at=?, superseded_by=?
                       WHERE id=? AND user_id=?""",
                    (normalized_valid_to, now, superseded_by or None, relation_id, user_id),
                )
            updated = conn.execute(
                f"""SELECT {safe_relation_columns} {visible_relation_from}
                     WHERE r.id=? AND r.user_id=?""",  # nosec B608
                (relation_id, user_id),
            ).fetchone()
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
            f"""SELECT COUNT(*) AS count FROM relations r
                JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                     AND {_not_private_entity_material_dependency("s")}
                JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                     AND {_not_private_entity_material_dependency("t")}
                WHERE (r.source_entity_id=? OR r.target_entity_id=?)
                {user_clause.replace("user_id", "r.user_id")} AND r.deleted_at IS NULL
                  AND {_not_private_relation_dependency("r")}""",  # nosec B608
            tuple(params),
        ).fetchone()
        return int(row["count"] if row else 0)

    def relation_history_status(self, user_id: str, known_at: str = "") -> dict[str, Any]:
        """Validate one reproducible relation snapshot and name its provenance.

        Schema 31 can only promise complete history from its immutable migration
        floor. Entity names deliberately remain current, but current merge topology
        is safe only when no merge/unmerge happened after the requested boundary.
        Those conditions are checked here once before a multi-hop traversal.
        """

        # Caller input is parsed before any database read. A malformed boundary
        # never degrades into a floor/topology diagnostic about unrelated data.
        boundary = normalize_known_at(known_at) if str(known_at or "").strip() else ""
        marker = self.execute(
            "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()
        if marker is None or not str(marker["value"] or "").strip():
            raise RelationHistorySnapshotError("relation history completeness floor is missing")
        floor = normalize_known_at(str(marker["value"]), reject_future=False)
        if not boundary:
            return {
                "known_at": "",
                "known_at_floor": floor,
                "history_complete": True,
                "identity_basis": "current_names",
            }
        if boundary < floor:
            raise RelationHistorySnapshotError(
                f"known_at precedes complete relation history; earliest boundary is {floor}"
            )

        # Persist the promise before reading any mutable projection. A cutoff may
        # be later than the current event tail; without this durable watermark a
        # subsequent wall-clock rewind could timestamp a new commit inside the
        # snapshot we have already returned to the caller.
        self._observe_relation_history_boundary(boundary)

        # Ordinary entity edits may change names and metadata after the boundary;
        # those are explicitly published as current. Existence/canonical topology
        # is different: using it from today would add or remove historical
        # endpoints, so version history must prove it did not change later.
        entities_at_boundary = _assert_no_later_entity_topology_change(self, user_id, boundary)

        # Relation revisions recover historical endpoint IDs, not historical
        # entity merge topology. A current entity lookup after any later merge or
        # unmerge could silently map that old endpoint through today's topology,
        # so this slice is rejected instead of guessed. Product merge timestamps
        # use the same transaction-context instant as their relation revisions.
        merge_rows = self.execute(
            """SELECT source_entity_id, created_at, undone_at
                 FROM entity_merge_history WHERE user_id=?""",
            (user_id,),
        ).fetchall()
        for row in merge_rows:
            if str(row["source_entity_id"]) not in entities_at_boundary:
                continue
            for raw_timestamp in (row["created_at"], row["undone_at"]):
                if not raw_timestamp:
                    continue
                try:
                    identity_change = normalize_known_at(str(raw_timestamp), reject_future=False)
                except ValueError as exc:
                    raise RelationHistorySnapshotError(
                        "entity merge history has an unreadable transaction timestamp"
                    ) from exc
                if identity_change > boundary:
                    raise RelationHistorySnapshotError(
                        "known_at crosses a later entity merge or unmerge; "
                        "historical identity topology is unavailable"
                    )
        return {
            "known_at": boundary,
            "known_at_floor": floor,
            "history_complete": True,
            "identity_basis": "current_names",
        }

    def get_entity_relations(
        self,
        entity_id: str,
        user_id: str | None = None,
        *,
        include_invalidated: bool = False,
        as_of: str = "",
        known_at: str = "",
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

        # Parse both axes before any relation/entity read. Direct storage callers
        # deserve the same missing-entity behaviour as HTTP/KG callers.
        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        normalized_known_at = normalize_known_at(known_at) if str(known_at or "").strip() else ""
        history_status: dict[str, Any] | None = None
        if normalized_known_at:
            if user_id is None:
                raise ValueError("user_id is required with known_at")
            history_status = _canonical_relation_history_status(
                self.relation_history_status(user_id, normalized_known_at),
                requested_known_at=normalized_known_at,
            )
        if history_status:
            # A transaction boundary for one tenant must not turn a foreign or
            # guessed entity id into a history-completeness oracle.
            if self.get_entity(entity_id, str(user_id)) is None:
                return []
            _assert_entities_existed_at_boundary(
                self,
                str(user_id),
                [entity_id],
                normalized_known_at,
            )
            rows = _historical_entity_relations(
                self,
                entity_id,
                str(user_id),
                include_invalidated=include_invalidated,
                as_of=as_of,
                known_at=str(history_status["known_at"]),
            )
            _assert_entities_existed_at_boundary(
                self,
                str(user_id),
                [
                    str(endpoint)
                    for row in rows
                    for endpoint in (row["source_entity_id"], row["target_entity_id"])
                ],
                normalized_known_at,
            )
            confirmed = _canonical_relation_history_status(
                self.relation_history_status(str(user_id), normalized_known_at),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while reading entity relations"
                )
            return rows
        params: list[Any] = [entity_id, entity_id]
        clauses = ["r.deleted_at IS NULL", _not_private_relation_dependency("r")]
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
        # Все предикаты — литералы, значения связаны параметрами. This exact
        # current-projection fast path remains free of window/history work.
        visible_source = _not_private_entity_material_dependency("s")
        visible_target = _not_private_entity_material_dependency("t")
        query = f"""SELECT r.*, s.name AS source_name, t.name AS target_name
                FROM relations r
                JOIN entities s ON s.id=r.source_entity_id AND s.user_id=r.user_id
                                   AND s.deleted_at IS NULL AND s.canonical=1
                                   AND s.merged_into_id IS NULL
                                   AND {visible_source}
                JOIN entities t ON t.id=r.target_entity_id AND t.user_id=r.user_id
                                   AND t.deleted_at IS NULL AND t.canonical=1
                                   AND t.merged_into_id IS NULL
                                   AND {visible_target}
                WHERE (r.source_entity_id=? OR r.target_entity_id=?)
                  AND {" AND ".join(clauses)}
                ORDER BY r.created_at DESC, r.id"""  # nosec B608
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
        known_at: str = "",
    ) -> dict[str, Any]:
        """Окрестность узла. `as_of` — «как это выглядело на ту дату».

        Без этого параметра bi-temporal половина схемы 27 остаётся внутренним
        свойством хранилища: поля есть, а спросить «кто командовал в 2024» не
        может ни человек, ни агент — обход всегда идёт по сегодняшней картине.

        Отменённая связь при заданной дате возвращается в картину, если на ту
        дату она была верна: в этом и смысл отличия «кончилось» от «не было».
        """

        # Valid-time must be rejected before even a missing-root lookup; then the
        # transaction boundary receives the same strict normalization/metadata
        # contract as the direct relation reader.
        as_of = _normalize_graph_date(as_of, "as_of") if as_of else ""
        normalized_known_at = normalize_known_at(known_at) if str(known_at or "").strip() else ""
        history_status: dict[str, Any] | None = None
        history_watermark: int | None = None
        if normalized_known_at:
            history_status = _canonical_relation_history_status(
                self.relation_history_status(user_id, normalized_known_at),
                requested_known_at=normalized_known_at,
            )
            history_watermark = _relation_revision_watermark(
                self,
                user_id,
                normalized_known_at,
            )
            _assert_entities_existed_at_boundary(
                self,
                user_id,
                [entity_id],
                normalized_known_at,
            )
        root = _graph_entity_for_traversal(self, entity_id, user_id)
        if (
            not root
            or root.get("deleted_at")
            or not bool(root.get("canonical", 1))
            or bool(root.get("merged_into_id"))
        ):
            result: dict[str, Any] = {
                "nodes": [],
                "edges": [],
                "root": entity_id,
                "as_of": as_of,
                "known_at": normalized_known_at,
                "temporal_basis": "bitemporal" if history_status else "valid_time",
            }
            if history_status:
                if _relation_revision_watermark(self, user_id, normalized_known_at) != history_watermark:
                    raise RelationHistorySnapshotError(
                        "relation history changed while reading the graph root"
                    )
                confirmed = _canonical_relation_history_status(
                    self.relation_history_status(user_id, normalized_known_at),
                    requested_known_at=normalized_known_at,
                )
                if confirmed != history_status:
                    raise RelationHistorySnapshotError(
                        "relation history status changed while reading the graph root"
                    )
                result.update(confirmed)
            return result
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
        edges_matched_at_least = 0
        edges_truncated = False
        omitted_node_ids: set[str] = set()
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for current in sorted(frontier):
                if history_status:
                    relation_rows = _historical_entity_relations(
                        self,
                        current,
                        user_id,
                        include_invalidated=False,
                        as_of=as_of,
                        known_at=str(history_status["known_at"]),
                        relation_types=tuple(wanted_relations),
                        entity_types=tuple(wanted_entities),
                        min_weight=floor,
                        root_entity_id=entity_id,
                        row_limit=_ENTITY_GRAPH_PAGE_SIZE,
                    )
                else:
                    relation_rows = _current_entity_relations_for_traversal(
                        self,
                        current,
                        user_id,
                        as_of=as_of,
                        require_live_endpoints=True,
                        relation_types=tuple(wanted_relations),
                        entity_types=tuple(wanted_entities),
                        min_weight=floor,
                        root_entity_id=entity_id,
                        row_limit=_ENTITY_GRAPH_PAGE_SIZE,
                    )
                for relation in relation_rows:
                    relation_id = str(relation["id"])
                    if relation_id in edges:
                        continue
                    if wanted_relations and str(relation.get("relation_type") or "") not in wanted_relations:
                        continue
                    if floor and float(relation.get("weight") or 0.0) < floor:
                        continue
                    neighbours = []
                    for candidate in (relation["source_entity_id"], relation["target_entity_id"]):
                        if candidate in seen:
                            continue
                        if history_status:
                            _assert_entities_existed_at_boundary(
                                self,
                                user_id,
                                [str(candidate)],
                                normalized_known_at,
                            )
                        entity = _graph_entity_for_traversal(self, str(candidate), user_id)
                        if (
                            not entity
                            or entity.get("deleted_at")
                            or not bool(entity.get("canonical", 1))
                            or bool(entity.get("merged_into_id"))
                        ):
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
                    if len(edges) >= _ENTITY_GRAPH_EDGE_BUDGET:
                        # We have observed one additional fully-qualified edge,
                        # so the lower bound and truncation flag are facts, not a
                        # guess based on merely filling the page.
                        edges_truncated = True
                        edges_matched_at_least = len(edges) + 1
                        omitted_node_ids.update(candidate for candidate, _entity in neighbours)
                        break
                    edges[relation_id] = relation
                    for candidate, entity in neighbours:
                        seen.add(candidate)
                        nodes[candidate] = entity
                        next_frontier.add(candidate)
                if edges_truncated:
                    break
            if edges_truncated:
                break
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
        payload: dict[str, Any] = {
            "root": entity_id,
            "nodes": enriched,
            "edges": list(edges.values()),
            "nodes_matched_at_least": len(nodes) + len(omitted_node_ids - set(nodes)),
            "nodes_truncated": bool(omitted_node_ids - set(nodes)),
            "edges_matched_at_least": max(len(edges), edges_matched_at_least),
            "edges_truncated": edges_truncated,
            "as_of": as_of,
            "known_at": normalized_known_at,
            "temporal_basis": "bitemporal" if history_status else "valid_time",
        }
        if history_status:
            if _relation_revision_watermark(self, user_id, normalized_known_at) != history_watermark:
                raise RelationHistorySnapshotError("relation history changed while building the entity graph")
            confirmed = _canonical_relation_history_status(
                self.relation_history_status(user_id, normalized_known_at),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while building the entity graph"
                )
            payload.update(confirmed)
        return payload

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
            f"""SELECT l.entity_id, COUNT(DISTINCT l.knowledge_object_id) AS total
                FROM knowledge_entity_links l
                JOIN knowledge_objects k
                  ON k.id=l.knowledge_object_id AND k.user_id=l.user_id
                 AND k.deleted_at IS NULL
                 AND {_not_private_knowledge_dependency("k")}
                WHERE l.user_id = ? AND l.status = 'accepted'
                  AND l.entity_id IN ({holders})
                GROUP BY l.entity_id""",  # noqa: S608
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
        if _is_private_reminder_entity_id(self, source_entity_id, user_id) or _is_private_reminder_entity_id(
            self, target_entity_id, user_id
        ):
            raise ValueError("Personal reminders cannot enter the shared relation review queue")
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
        serialized_evidence = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)
        if len(serialized_evidence.encode("utf-8")) > _GRAPH_RELATION_METADATA_MAX_BYTES:
            raise ValueError("Relation candidate evidence is too large")
        with self.transaction() as conn:
            live_endpoints = conn.execute(
                f"""SELECT COUNT(*) AS count FROM entities
                     WHERE user_id=? AND id IN (?, ?) AND deleted_at IS NULL
                       AND canonical=1 AND merged_into_id IS NULL
                       AND {_not_private_entity_material_dependency("entities")}""",  # nosec B608
                (user_id, source_entity_id, target_entity_id),
            ).fetchone()
            if not live_endpoints or int(live_endpoints["count"] or 0) != 2:
                raise ValueError("Both candidate entities must belong to the same user")
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
                    serialized_evidence,
                    now,
                ),
            )
            visible_candidate = conn.execute(
                f"""SELECT 1 FROM relation_candidates c
                     WHERE c.user_id=? AND c.source_entity_id=? AND c.target_entity_id=?
                       AND c.relation_type=?
                       AND {_not_private_relation_candidate_dependency("c")}""",  # nosec B608
                (user_id, source_entity_id, target_entity_id, relation_type),
            ).fetchone()
            if visible_candidate is None:
                raise ValueError("Relation candidate evidence references private knowledge")
        row = self.execute(
            f"""SELECT c.*, substr(s.name,1,240) AS source_name,
                       substr(t.name,1,240) AS target_name
                {self._RELATION_CANDIDATE_FROM}
                WHERE c.user_id=? AND c.source_entity_id=? AND c.target_entity_id=?
                  AND c.relation_type=?""",  # nosec B608
            (user_id, source_entity_id, target_entity_id, relation_type),
        ).fetchone()
        return dict(row) if row else {}

    def get_relation_candidate(self, user_id: str, candidate_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT c.id, c.user_id, c.source_entity_id, c.target_entity_id,
                       substr(c.relation_type,1,80) AS relation_type, c.confidence,
                       CASE WHEN length(CAST(COALESCE(c.evidence_json,'') AS BLOB))
                                          <={_GRAPH_RELATION_METADATA_MAX_BYTES}
                                  AND json_valid(c.evidence_json)
                                  AND json_type(c.evidence_json)='object'
                            THEN c.evidence_json ELSE '{{}}' END AS evidence_json,
                       substr(c.status,1,40) AS status,
                       substr(c.created_at,1,64) AS created_at,
                       substr(COALESCE(c.reviewed_at,''),1,64) AS reviewed_at,
                       substr(COALESCE(c.reviewed_by,''),1,160) AS reviewed_by,
                       substr(s.name,1,240) AS source_name,
                       substr(s.entity_type,1,80) AS source_type,
                       substr(t.name,1,240) AS target_name,
                       substr(t.entity_type,1,80) AS target_type
                {self._RELATION_CANDIDATE_FROM}
                WHERE c.id=? AND c.user_id=?""",  # nosec B608
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
    _RELATION_CANDIDATE_FROM = f"""FROM relation_candidates c
                JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                    AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                    AND {_not_private_entity_material_dependency("s")}
                JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                    AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                    AND {_not_private_entity_material_dependency("t")}
                JOIN users candidate_privacy_owner ON candidate_privacy_owner.id=c.user_id
                    AND {_not_private_relation_candidate_dependency("c")}"""

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
        query = f"""SELECT c.id, c.user_id, c.source_entity_id, c.target_entity_id,
                       substr(c.relation_type,1,80) AS relation_type, c.confidence,
                       CASE WHEN length(CAST(COALESCE(c.evidence_json,'') AS BLOB))
                                          <={_GRAPH_RELATION_METADATA_MAX_BYTES}
                                  AND json_valid(c.evidence_json)
                                  AND json_type(c.evidence_json)='object'
                            THEN c.evidence_json ELSE '{{}}' END AS evidence_json,
                       substr(c.status,1,40) AS status,
                       substr(c.created_at,1,64) AS created_at,
                       substr(COALESCE(c.reviewed_at,''),1,64) AS reviewed_at,
                       substr(COALESCE(c.reviewed_by,''),1,160) AS reviewed_by,
                       substr(s.name,1,240) AS source_name,
                       substr(s.entity_type,1,80) AS source_type,
                       substr(t.name,1,240) AS target_name,
                       substr(t.entity_type,1,80) AS target_type
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
                f"""SELECT c.id, c.user_id, c.source_entity_id, c.target_entity_id,
                            c.relation_type, c.confidence, c.status, c.created_at,
                            c.reviewed_at, c.reviewed_by,
                            substr(s.name,1,240) AS source_name,
                            substr(s.entity_type,1,80) AS source_type,
                            substr(t.name,1,240) AS target_name,
                            substr(t.entity_type,1,80) AS target_type,
                            CASE WHEN COALESCE(c.evidence_json,'')
                                           NOT IN ('', '{{}}', '[]', 'null')
                                 THEN 1 ELSE 0 END AS evidence_present,
                            MIN(length(CAST(COALESCE(c.evidence_json,'') AS BLOB)),
                                1000000000) AS evidence_bytes,
                            CASE WHEN length(CAST(COALESCE(c.evidence_json,'') AS BLOB))
                                           <={_GRAPH_RELATION_METADATA_MAX_BYTES}
                                 THEN CASE WHEN json_valid(c.evidence_json)
                                           THEN CASE WHEN json_type(c.evidence_json)='object'
                                                     THEN c.evidence_json ELSE '{{}}' END
                                           ELSE '{{}}' END
                                 ELSE '{{}}' END AS evidence_json
                     FROM relation_candidates c
                     JOIN entities s ON s.id=c.source_entity_id AND s.user_id=c.user_id
                          AND s.deleted_at IS NULL AND s.canonical=1 AND s.merged_into_id IS NULL
                          AND {_not_private_entity_material_dependency("s")}
                     JOIN entities t ON t.id=c.target_entity_id AND t.user_id=c.user_id
                          AND t.deleted_at IS NULL AND t.canonical=1 AND t.merged_into_id IS NULL
                          AND {_not_private_entity_material_dependency("t")}
                     WHERE c.id=? AND c.user_id=?
                       AND {_not_private_relation_candidate_dependency("c")}""",  # nosec B608
                (candidate_id, user_id),
            ).fetchone()
            if not row:
                return None
            current_status = str(row["status"] or "suggested")
            reviewed_at = str(row["reviewed_at"] or "")
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
                            f"""SELECT 1 FROM entities
                               WHERE id=? AND user_id=? AND deleted_at IS NULL
                                 AND canonical=1 AND merged_into_id IS NULL
                                 AND {_not_private_entity_material_dependency("entities")}""",  # nosec B608
                            (str(row[column]), user_id),
                        ).fetchone()
                        if not alive:
                            raise ValueError(
                                f"Принять связь нельзя: её {side} больше не существует "
                                "(сущность удалена или слита)"
                            )
                now = utc_now()
                reviewed_at = now
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
                            f"""SELECT CASE
                                   WHEN length(CAST(COALESCE(metadata_json,'') AS BLOB))
                                          <={_GRAPH_RELATION_METADATA_MAX_BYTES}
                                   THEN CASE WHEN json_valid(metadata_json)
                                             THEN CASE
                                               WHEN json_type(metadata_json,'$.document_date')='text'
                                               THEN substr(json_extract(
                                                   metadata_json,'$.document_date'),1,64)
                                               ELSE '' END
                                             ELSE '' END
                                   ELSE '' END AS on_paper
                                FROM knowledge_objects anchored_source
                               WHERE anchored_source.id=? AND anchored_source.user_id=?
                                 AND anchored_source.deleted_at IS NULL
                                 AND {_not_private_knowledge_dependency("anchored_source")}""",  # nosec B608
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
            # The decision and this bounded response are one authority snapshot.
            # Re-reading after COMMIT used to open a race: an endpoint could be
            # deleted in that gap, making a successful mutation return 404 and
            # skip its audit record.  Arbitrary evidence and reviewer identity do
            # not enter the snapshot; only their content-free shape survives.
            result = {
                "id": str(row["id"] or "")[:160],
                "source_entity_id": str(row["source_entity_id"] or "")[:160],
                "target_entity_id": str(row["target_entity_id"] or "")[:160],
                "relation_type": str(row["relation_type"] or "")[:80],
                "confidence": float(row["confidence"] or 0.0),
                "status": status,
                "created_at": str(row["created_at"] or "")[:64],
                "reviewed_at": reviewed_at[:64],
                "source_name": str(row["source_name"] or "")[:240],
                "source_type": str(row["source_type"] or "")[:80],
                "target_name": str(row["target_name"] or "")[:240],
                "target_type": str(row["target_type"] or "")[:80],
                "evidence_present": bool(row["evidence_present"]),
                "evidence_bytes": int(row["evidence_bytes"] or 0),
            }
        return result

    def store_resolution_candidate(self, candidate: EntityResolutionCandidate) -> EntityResolutionCandidate:
        if candidate.entity_a_id == candidate.entity_b_id:
            raise ValueError("A resolution candidate must contain two distinct entities")
        left = self.get_entity(candidate.entity_a_id, candidate.user_id)
        right = self.get_entity(candidate.entity_b_id, candidate.user_id)
        if not left or not right:
            raise ValueError("Resolution entities must belong to the same user")
        row = candidate.to_row()
        with self.transaction() as conn:
            live_endpoints = conn.execute(
                f"""SELECT COUNT(*) AS count FROM entities
                     WHERE user_id=? AND id IN (?, ?) AND deleted_at IS NULL
                       AND canonical=1 AND merged_into_id IS NULL
                       AND {_not_private_entity_material_dependency("entities")}""",  # nosec B608
                (candidate.user_id, candidate.entity_a_id, candidate.entity_b_id),
            ).fetchone()
            if not live_endpoints or int(live_endpoints["count"] or 0) != 2:
                raise ValueError("Resolution entities must belong to the same user")
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
                        f"""SELECT * FROM entity_resolution_candidates c
                             WHERE c.id=? AND c.user_id=?
                               AND {_not_private_resolution_candidate_dependency("c")}""",  # nosec B608
                        (existing_candidate.id, candidate.user_id),
                    ).fetchone()
                    if refreshed is None:
                        raise ValueError("Resolution evidence references private graph material")
                    return self._resolution_from_row(refreshed)
                visible_existing = conn.execute(
                    f"""SELECT 1 FROM entity_resolution_candidates c
                         WHERE c.id=? AND c.user_id=?
                           AND {_not_private_resolution_candidate_dependency("c")}""",  # nosec B608
                    (existing_candidate.id, candidate.user_id),
                ).fetchone()
                if visible_existing is None:
                    raise ValueError("Resolution evidence references private graph material")
                return existing_candidate
            conn.execute(
                """INSERT INTO entity_resolution_candidates(id, user_id, entity_a_id, entity_b_id,
                   pair_key, confidence, resolution_method, evidence_json, status, resolved_by,
                   created_at, resolved_at)
                   VALUES(:id, :user_id, :entity_a_id, :entity_b_id, :pair_key, :confidence,
                   :resolution_method, :evidence_json, :status, :resolved_by, :created_at, :resolved_at)""",
                row,
            )
            visible_candidate = conn.execute(
                f"""SELECT 1 FROM entity_resolution_candidates c
                     WHERE c.id=? AND c.user_id=?
                       AND {_not_private_resolution_candidate_dependency("c")}""",  # nosec B608
                (candidate.id, candidate.user_id),
            ).fetchone()
            if visible_candidate is None:
                raise ValueError("Resolution evidence references private graph material")
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
            f"""SELECT * FROM entity_resolution_candidates c WHERE c.id=? AND c.user_id=?
                 AND {_not_private_resolution_candidate_dependency("c")}""",  # nosec B608
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
        clauses = [
            "c.user_id=?",
            "EXISTS (SELECT 1 FROM entities visible_a WHERE visible_a.id=c.entity_a_id "
            "AND visible_a.user_id=c.user_id "
            f"AND {_not_private_entity_material_dependency('visible_a')})",
            "EXISTS (SELECT 1 FROM entities visible_b WHERE visible_b.id=c.entity_b_id "
            "AND visible_b.user_id=c.user_id "
            f"AND {_not_private_entity_material_dependency('visible_b')})",
            _not_private_resolution_candidate_dependency("c"),
        ]
        params: list[Any] = [user_id]
        if status:
            clauses.append("c.status=?")
            params.append(enum_value(status))
        rows = self.execute(
            f"""SELECT c.* FROM entity_resolution_candidates c
                 WHERE {" AND ".join(clauses)}
                 ORDER BY c.confidence DESC, c.created_at DESC, c.id DESC
                 LIMIT ? OFFSET ?""",  # nosec B608
            (*params, bounded, max(0, offset)),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_resolution_candidates(self, user_id: str, status: ResolutionStatus | None = None) -> int:
        """Сколько их всего — чтобы страница не выдавалась за полный объём."""
        clauses = [
            "c.user_id=?",
            "EXISTS (SELECT 1 FROM entities visible_a WHERE visible_a.id=c.entity_a_id "
            "AND visible_a.user_id=c.user_id "
            f"AND {_not_private_entity_material_dependency('visible_a')})",
            "EXISTS (SELECT 1 FROM entities visible_b WHERE visible_b.id=c.entity_b_id "
            "AND visible_b.user_id=c.user_id "
            f"AND {_not_private_entity_material_dependency('visible_b')})",
            _not_private_resolution_candidate_dependency("c"),
        ]
        params: list[Any] = [user_id]
        if status:
            clauses.append("c.status=?")
            params.append(enum_value(status))
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM entity_resolution_candidates c
                 WHERE {" AND ".join(clauses)}""",  # nosec B608
            tuple(params),
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
        query = f"""UPDATE entity_resolution_candidates
                       SET status=?, resolved_at=?, resolved_by=?
                     WHERE id=?
                       AND EXISTS (
                           SELECT 1 FROM entities visible_a
                            WHERE visible_a.id=entity_resolution_candidates.entity_a_id
                              AND visible_a.user_id=entity_resolution_candidates.user_id
                              AND {_not_private_entity_material_dependency("visible_a")}
                       )
                       AND EXISTS (
                           SELECT 1 FROM entities visible_b
                            WHERE visible_b.id=entity_resolution_candidates.entity_b_id
                              AND visible_b.user_id=entity_resolution_candidates.user_id
                              AND {_not_private_entity_material_dependency("visible_b")}
                       )
                       AND {_not_private_resolution_candidate_dependency("entity_resolution_candidates")}"""  # nosec B608
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
            # The identity event and every relation rewrite below belong to the
            # same transaction-time snapshot. Second-resolution `utc_now()` could
            # otherwise sort a later merge before an earlier microsecond boundary.
            now = _relation_batch_timestamp(conn)
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
                    f"""SELECT l.* FROM knowledge_entity_links l
                          JOIN knowledge_objects linked_knowledge
                            ON linked_knowledge.id=l.knowledge_object_id
                           AND linked_knowledge.user_id=l.user_id
                         WHERE l.user_id=? AND l.entity_id=?
                           AND {_not_private_knowledge_dependency("linked_knowledge")}
                           AND {
                        _not_private_bounded_json_dependency(
                            "l.evidence_json",
                            "l.user_id",
                            max_bytes=_MERGE_HISTORY_JSON_MAX_BYTES,
                            reject_nested_json=True,
                        )
                    }""",  # nosec B608
                    (user_id, source_id),
                ).fetchall()
            ]
            target_ko_ids = {
                str(row["knowledge_object_id"])
                for row in conn.execute(
                    f"""SELECT l.knowledge_object_id FROM knowledge_entity_links l
                          JOIN knowledge_objects target_link_knowledge
                            ON target_link_knowledge.id=l.knowledge_object_id
                           AND target_link_knowledge.user_id=l.user_id
                         WHERE l.user_id=? AND l.entity_id=?
                           AND {_not_private_knowledge_dependency("target_link_knowledge")}""",  # nosec B608
                    (user_id, target_id),
                ).fetchall()
            }
            links_moved: list[dict[str, Any]] = []
            links_suppressed: list[dict[str, Any]] = []
            for link in source_links:
                ko_id = str(link["knowledge_object_id"])
                if ko_id in target_ko_ids:
                    links_suppressed.append(link)
                    conn.execute(
                        "DELETE FROM knowledge_entity_links WHERE id=? AND user_id=? AND entity_id=?",
                        (link["id"], user_id, source_id),
                    )
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
                    "DELETE FROM knowledge_entity_links WHERE id=? AND user_id=? AND entity_id=?",
                    (link["id"], user_id, source_id),
                )

            primary_rows = conn.execute(
                f"""SELECT k.id FROM knowledge_objects k
                     WHERE k.user_id=? AND k.entity_id=?
                       AND {_not_private_knowledge_dependency("k")}""",  # nosec B608
                (user_id, source_id),
            ).fetchall()
            primary_moved = [str(row["id"]) for row in primary_rows]
            for knowledge_id in primary_moved:
                conn.execute(
                    """UPDATE knowledge_objects SET entity_id=?, updated_at=?
                       WHERE id=? AND user_id=? AND entity_id=?""",
                    (target_id, now, knowledge_id, user_id, source_id),
                )

            relations = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT r.* FROM relations r
                       JOIN entities merge_relation_source
                         ON merge_relation_source.id=r.source_entity_id
                        AND merge_relation_source.user_id=r.user_id
                        AND {_not_private_entity_material_dependency("merge_relation_source")}
                       JOIN entities merge_relation_target
                         ON merge_relation_target.id=r.target_entity_id
                        AND merge_relation_target.user_id=r.user_id
                        AND {_not_private_entity_material_dependency("merge_relation_target")}
                       WHERE r.user_id=? AND r.deleted_at IS NULL
                       AND (r.source_entity_id=? OR r.target_entity_id=?)
                       AND {_not_private_relation_dependency("r")}""",  # nosec B608
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
                        f"""SELECT existing_relation.id FROM relations existing_relation
                           JOIN entities existing_relation_source
                             ON existing_relation_source.id=existing_relation.source_entity_id
                            AND existing_relation_source.user_id=existing_relation.user_id
                            AND {_not_private_entity_material_dependency("existing_relation_source")}
                           JOIN entities existing_relation_target
                             ON existing_relation_target.id=existing_relation.target_entity_id
                            AND existing_relation_target.user_id=existing_relation.user_id
                            AND {_not_private_entity_material_dependency("existing_relation_target")}
                           WHERE existing_relation.user_id=?
                             AND existing_relation.source_entity_id=?
                             AND existing_relation.target_entity_id=?
                             AND existing_relation.relation_type=?
                             AND existing_relation.deleted_at IS NULL
                             AND existing_relation.valid_to IS NULL
                             AND existing_relation.id<>?
                             AND {_not_private_relation_dependency("existing_relation")}
                           LIMIT 1""",  # nosec B608
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
                    f"""SELECT reference_relation.id FROM relations reference_relation
                       JOIN entities reference_source
                         ON reference_source.id=reference_relation.source_entity_id
                        AND reference_source.user_id=reference_relation.user_id
                        AND {_not_private_entity_material_dependency("reference_source")}
                       JOIN entities reference_target
                         ON reference_target.id=reference_relation.target_entity_id
                        AND reference_target.user_id=reference_relation.user_id
                        AND {_not_private_entity_material_dependency("reference_target")}
                       WHERE reference_relation.user_id=?
                         AND reference_relation.superseded_by=?
                         AND {_not_private_relation_dependency("reference_relation")}""",  # nosec B608
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
                    f"""SELECT resolution_candidate.id
                       FROM entity_resolution_candidates resolution_candidate
                       JOIN entities resolution_a
                         ON resolution_a.id=resolution_candidate.entity_a_id
                        AND resolution_a.user_id=resolution_candidate.user_id
                        AND {_not_private_entity_material_dependency("resolution_a")}
                       JOIN entities resolution_b
                         ON resolution_b.id=resolution_candidate.entity_b_id
                        AND resolution_b.user_id=resolution_candidate.user_id
                        AND {_not_private_entity_material_dependency("resolution_b")}
                       WHERE resolution_candidate.user_id=?
                         AND resolution_candidate.status='suggested'
                         AND (resolution_candidate.entity_a_id=?
                              OR resolution_candidate.entity_b_id=?)
                         AND {_not_private_resolution_candidate_dependency("resolution_candidate")}""",  # nosec B608
                    (user_id, source_id, source_id),
                ).fetchall()
            ]
            for candidate_id in closed_candidates:
                conn.execute(
                    """UPDATE entity_resolution_candidates
                       SET status='merged', resolved_at=?, resolved_by=?
                       WHERE id=? AND user_id=? AND status='suggested'""",
                    (now, merged_by or user_id, candidate_id, user_id),
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
            target_time_was_present = conn.execute(
                "SELECT 1 FROM entity_time WHERE user_id=? AND entity_id=?",
                (user_id, target_id),
            ).fetchone()
            time_moved = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM entity_time
                       WHERE user_id=? AND entity_id=? AND source NOT LIKE 'reminder:%'""",
                    (user_id, source_id),
                ).fetchall()
            ]
            if time_moved:
                conn.execute(
                    """INSERT OR IGNORE INTO entity_time(
                           user_id, entity_id, occurred_at, occurred_end, precision, source, updated_at)
                       SELECT user_id, ?, occurred_at, occurred_end, precision, source, ?
                         FROM entity_time WHERE user_id=? AND entity_id=?
                           AND source NOT LIKE 'reminder:%'""",
                    (target_id, now, user_id, source_id),
                )
                conn.execute(
                    """DELETE FROM entity_time WHERE user_id=? AND entity_id=?
                       AND source NOT LIKE 'reminder:%'""",
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
                "time_target_created": bool(time_moved) and target_time_was_present is None,
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
        with _merge_history_read_snapshot(self):
            row = self.execute(
                "SELECT * FROM entity_merge_history WHERE id=? AND user_id=?",
                (merge_id, user_id),
            ).fetchone()
            if row is None:
                return None
            history = dict(row)
            context = _merge_history_privacy_context(self, user_id)
            visible, _parsed = _merge_history_row_is_visible(history, context)
            return history if visible else None

    def list_merge_history(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 1000))
        result: list[dict[str, Any]] = []
        with _merge_history_read_snapshot(self):
            context = _merge_history_privacy_context(self, user_id)
            for row, _parsed in _iter_visible_merge_history(self, user_id, context):
                result.append(row)
                if len(result) >= bounded:
                    break
        return result

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
            context = _merge_history_privacy_context(self, user_id)
            visible, parsed = _merge_history_row_is_visible(history, context)
            if not visible or parsed is None:
                raise ValueError("Merge history entry not found")
            if history.get("undone_at"):
                raise ValueError("Merge has already been undone")
            transfer = parsed["transfer_json"]
            if not isinstance(transfer, dict) or not transfer:
                raise ValueError(
                    "Merge has no transfer record and cannot be undone honestly "
                    "(merged before transfer_json existed)"
                )

            source_id = str(history["source_entity_id"])
            target_id = str(history["target_entity_id"])
            source_snap = parsed["source_snapshot_json"]
            target_before = parsed["target_before_json"]
            target_after = parsed["target_after_json"]
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

            # Same batch boundary as the relation revisions restored below; this
            # makes merge-crossing checks exact at the half-open boundary.
            now = _relation_batch_timestamp(conn)

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

            # 8. Restore the source event's temporal anchor. The target keeps a
            # pre-existing date and every later edit. A merge-created target row
            # is removed only while all semantic fields and its merge timestamp
            # still match, so undo cannot erase a post-merge human decision.
            for time_item in transfer.get("time_moved") or []:
                if not isinstance(time_item, dict):
                    continue
                if transfer.get("time_target_created") is not False:
                    conn.execute(
                        """DELETE FROM entity_time
                           WHERE entity_id=? AND user_id=? AND occurred_at=?
                             AND occurred_end IS ? AND precision=? AND source=?
                             AND updated_at=?""",
                        (
                            target_id,
                            user_id,
                            time_item["occurred_at"],
                            time_item.get("occurred_end"),
                            time_item["precision"],
                            time_item["source"],
                            history["created_at"],
                        ),
                    )
                conn.execute(
                    """INSERT OR IGNORE INTO entity_time(
                           entity_id, user_id, occurred_at, occurred_end,
                           precision, source, updated_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        user_id,
                        time_item["occurred_at"],
                        time_item.get("occurred_end"),
                        time_item["precision"],
                        time_item["source"],
                        time_item["updated_at"],
                    ),
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
