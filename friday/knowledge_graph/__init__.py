"""First-class knowledge graph and conservative entity resolution."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any, NamedTuple

from friday.entity_phrases import mention_phrase_candidates
from friday.mentions import (
    inflected_mention_occurrences,
    inflected_mention_signature,
    inflected_token_context,
)
from friday.storage import FridayStorage
from friday.storage._graph import (
    _assert_entities_existed_at_boundary,
    _bounded_entity_by_id,
    _bounded_entity_listing_rows,
    _bounded_entity_relation_rows,
    _bounded_resolution_candidate_by_id,
    _bounded_resolution_candidate_rows,
    _bounded_visible_timeline_event_rows,
    _count_visible_relations,
    _count_visible_timeline_events,
    _current_entity_relations_for_traversal,
    _graph_entity_for_traversal,
    _historical_entity_relations,
    _iter_entities_for_graph_search,
    _relation_revision_watermark,
)
from friday.storage._privacy import (
    _not_private_entity_material_dependency,
    _not_private_relation_dependency,
)
from friday.storage.models import (
    Entity,
    EntityResolutionCandidate,
    EntityType,
    InboxStatus,
    Relation,
    RelationHistorySnapshotError,
    RelationType,
    ResolutionStatus,
    new_id,
    normalize_known_at,
    utc_now,
)

# Timeline semantics: an event entity "occurred_at" a normalized ISO date/range.
EVENT_TIME_RELATION = RelationType.OCCURRED_AT.value
# Entity types that act as user-curated containers (browse/organization layer).
CONTAINER_ENTITY_TYPES = frozenset({EntityType.PROJECT.value, EntityType.COLLECTION.value})


def build_user_model(storage: FridayStorage, user_id: str) -> dict[str, Any]:
    """Deterministic user model derived from the graph (no LLM needed).

    Recurring people/organizations (by accepted knowledge links), active
    projects, standing interests (tags), and capture rhythm. This is a computed
    REFLECTION of the knowledge — never a stored artifact — so it is always
    current and is edited by editing the underlying material. Consumed by the
    agent's chat context (personalization) and the profile organ's endpoint.
    """
    people = storage.list_entities_by_activity(user_id, types=("person",), limit=5)
    organizations = storage.list_entities_by_activity(user_id, types=("organization",), limit=5)
    projects = [
        c
        for c in storage.list_container_entities(user_id, tuple(sorted(CONTAINER_ENTITY_TYPES)))
        if c.get("knowledge_count")
    ]
    projects.sort(key=lambda c: int(c.get("knowledge_count") or 0), reverse=True)
    interests = storage.list_knowledge_tags(user_id, limit=8)

    knowledge_total = storage.count_knowledge_objects(user_id)
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    # `len(... limit=200)` насыщалось на двухстах и дальше молчало: у активного
    # человека «за 30 дней» навсегда становилось ровно 200.
    recent_count = storage.count_recent_knowledge(user_id, since_iso=since)

    return {
        "knowledge_total": knowledge_total,
        "recent_30d": recent_count,
        "people": [
            {"name": str(e.get("name") or "")[:240], "knowledge_count": int(e.get("knowledge_count") or 0)}
            for e in people[:5]
        ],
        "organizations": [
            {"name": str(e.get("name") or "")[:240], "knowledge_count": int(e.get("knowledge_count") or 0)}
            for e in organizations[:5]
        ],
        "projects": [
            {
                "name": str(c.get("name") or "")[:240],
                "kind": str(c.get("entity_type") or "")[:80],
                "knowledge_count": int(c.get("knowledge_count") or 0),
            }
            for c in projects[:5]
        ],
        "interests": [
            {"tag": str(t.get("tag") or "")[:120], "count": int(t.get("count") or 0)} for t in interests[:8]
        ],
    }


# Hard safety ceiling for graph traversal; the effective depth is set by config
# (graph_max_depth) but can never exceed this, to bound work on a large graph.
_MAX_TRAVERSAL_DEPTH = 4
# How much a document's SECOND and further shared entities add on top of its best
# one: `1 - (1-best) * prod(1 - damping * s_i)` over the other distinct entities.
# 0.0 is exactly the old max-over-entities. See `context_for_query` for the
# measured rationale; the value itself is measured on the 342-document stand.
_GRAPH_CORROBORATION_DAMPING = 0.5
# How much the LAST seed document's entities lose against the first one's. 0.0 is
# the old flat weight, 1.0 would make the last seed worth nothing at all.
#
# MEASURED on the 342-document stand, 198 queries built from the documents' own
# words, embeddings off so the graph channel is visible (recall@10 / MRR / share
# of returned graph scores tied with another result / results returned for ten
# nonsense queries, where fewer is better):
#
#     decay   recall@10   MRR     tied   nonsense
#      0.0     131/198    0.545   0.93      92
#      0.4     134/198    0.556   0.87      92
#      0.6     137/198    0.561   0.86      92
#      0.8     146/198    0.571   0.87      90
#      0.9     149/198    0.577   0.87      82
#      1.0     150/198    0.584   0.86      74
#
# Monotone in every column: the further down the seed list an entity came from,
# the less its vouching is worth. 1.0 measured marginally better still and is not
# taken — a weight of exactly zero makes the last seed's presence meaningless and
# quietly ties the result to how many seeds retrieval happens to pass.
_GRAPH_SEED_RANK_DECAY = 0.9
_MAX_PUBLISHED_GRAPH_PATHS = 10
_MAX_CONTEXT_GRAPH_ENTITIES = 256
_MAX_CONTEXT_GRAPH_RELATIONS = 512
_MAX_CONTEXT_RELATION_PAGE = _MAX_CONTEXT_GRAPH_RELATIONS + 1
_MAX_PUBLIC_ENTITY_RELATIONS = 200
_MAX_PUBLIC_ENTITY_GRAPH_EDGES = 800
_MAX_PUBLIC_ENTITY_GRAPH_NODES = _MAX_PUBLIC_ENTITY_GRAPH_EDGES * 2 + 1
_MAX_PUBLIC_GRAPH_COUNT = 1_000_000_000
_PUBLIC_KNOWLEDGE_TAG_LIMIT = 20
_PUBLIC_KNOWLEDGE_TAG_MAX_CHARS = 120
_REVIEW_PROVENANCE_KEYS = (
    "source",
    "candidate_id",
    "confidence",
)
_PUBLIC_RELATION_TEXT_LIMITS = {
    "id": 160,
    "source_entity_id": 160,
    "target_entity_id": 160,
    "source_name": 240,
    "target_name": 240,
    "relation_type": 80,
    "valid_from": 64,
    "valid_to": 64,
    "created_at": 64,
    "invalidated_at": 64,
    "superseded_by": 160,
    # Род ребра — закрытый перечень из двух значений, и он обязан доехать до
    # рисующей стороны: подтверждённая связь рисуется сплошной линией, а
    # совместная встречаемость пунктиром. Без этого поля наблюдение выглядело бы
    # на экране объявленным фактом.
    "kind": 16,
}
_PUBLIC_RELATION_FIELDS = tuple(_PUBLIC_RELATION_TEXT_LIMITS)
_PUBLIC_GRAPH_NODE_TEXT_LIMITS = {
    "id": 160,
    "name": 240,
    "entity_type": 80,
}
_PUBLIC_GRAPH_NUMBER_LIMIT = 1_000_000_000.0
_ISO_FULL_RE = re.compile(r"\b(\d{4})[-./](\d{1,2})[-./](\d{1,2})\b")
_DAY_FIRST_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_YEAR_MONTH_RE = re.compile(r"\b(\d{4})[-./](\d{1,2})\b")


class _GraphPathState(NamedTuple):
    """The score and the route that earned it, moved through BFS as one value."""

    root: str
    current: str
    score: float
    query_grounded: bool
    entity_ids: tuple[str, ...]
    edges: tuple[dict[str, Any], ...]


class _BoundedRelationList(list[dict[str, Any]]):
    """List-compatible relation page carrying honest public cap metadata."""

    def __init__(
        self,
        values: list[dict[str, Any]],
        *,
        matched_at_least: int,
        truncated: bool,
    ) -> None:
        super().__init__(values)
        self.matched_at_least = max(len(values), int(matched_at_least))
        self.truncated = bool(truncated or self.matched_at_least > len(values))


class _BoundedEntityList(list[dict[str, Any]]):
    """List-compatible entity page with an honest lower bound."""

    def __init__(
        self,
        values: list[dict[str, Any]],
        *,
        matched_at_least: int,
        truncated: bool,
    ) -> None:
        super().__init__(values)
        self.matched_at_least = max(len(values), int(matched_at_least))
        self.truncated = bool(truncated or self.matched_at_least > len(values))


def _relation_provenance(relation: dict[str, Any]) -> dict[str, Any]:
    """Compact non-secret provenance for a path step.

    Relation metadata may contain excerpts and arbitrary caller fields.  A graph
    route needs the review lineage, not that unbounded payload.  The Knowledge
    Object anchor is intentionally recovered from the nested review evidence,
    where accepted relation candidates store it.
    """

    metadata = _json_dict(relation.get("metadata_json"))
    if not metadata:
        # `get_entity_relations` is also the traversal seam.  Its public
        # projection has already reduced raw metadata to this allowlisted shape;
        # keep that safe provenance usable for path grounding without ever
        # reintroducing the original metadata row.
        projected = relation.get("provenance")
        if isinstance(projected, Mapping):
            output: dict[str, Any] = {}
            for key in ("origin", *_REVIEW_PROVENANCE_KEYS, "knowledge_object_id"):
                value = projected.get(key)
                if isinstance(value, str) and value.strip():
                    output[key] = value.strip()[:160]
                elif key == "confidence" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric = float(value)
                    if math.isfinite(numeric):
                        output[key] = max(0.0, min(numeric, 1.0))
            if projected.get("reviewed") is True:
                output["reviewed"] = True
            return output
    provenance: dict[str, Any] = {}
    origin = str(metadata.get("origin") or "").strip()[:80]
    if origin:
        provenance["origin"] = origin
    # Only the storage review path can mint evidence-backed provenance.  The
    # public relation API stamps another origin and may carry arbitrary user
    # metadata; accepting an evidence-looking nested object from that path would
    # let a caller turn any existing Knowledge Object ID into grounded=True.
    trusted_review = (
        origin == "review"
        and metadata.get("source") == "reviewed_relation_candidate"
        and isinstance(metadata.get("candidate_id"), str)
        and bool(str(metadata.get("candidate_id") or "").strip())
        and isinstance(metadata.get("reviewed_by"), str)
        and bool(str(metadata.get("reviewed_by") or "").strip())
    )
    if not trusted_review:
        return provenance
    provenance["reviewed"] = True

    for key in _REVIEW_PROVENANCE_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            provenance[key] = value.strip()[:160]
        elif key == "confidence" and isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if math.isfinite(numeric):
                provenance[key] = max(0.0, min(numeric, 1.0))
    nested_evidence = metadata.get("evidence")
    if isinstance(nested_evidence, dict):
        nested_knowledge_id = nested_evidence.get("knowledge_object_id")
        knowledge_object_id = (
            nested_knowledge_id.strip()[:160] if isinstance(nested_knowledge_id, str) else ""
        )
    else:
        knowledge_object_id = ""
    if not knowledge_object_id:
        direct_knowledge_id = metadata.get("knowledge_object_id")
        knowledge_object_id = (
            direct_knowledge_id.strip()[:160] if isinstance(direct_knowledge_id, str) else ""
        )
    if knowledge_object_id:
        provenance["knowledge_object_id"] = knowledge_object_id
    return provenance


def _public_relation(relation: Mapping[str, Any]) -> dict[str, Any]:
    """One bounded allowlisted relation shape for every KG/public graph seam.

    Storage rows deliberately retain full revision metadata for trusted internal
    consumers and tenant export.  Relation metadata is arbitrary and may contain
    excerpts, credentials, or very large caller fields, so it must never be
    copied wholesale into direct entity or neighbourhood responses.
    """

    projected: dict[str, Any] = {}
    for field in _PUBLIC_RELATION_FIELDS:
        if field not in relation:
            continue
        value = relation[field]
        if value is None:
            projected[field] = None
        elif isinstance(value, str):
            projected[field] = value[: _PUBLIC_RELATION_TEXT_LIMITS[field]]
    weight = relation.get("weight")
    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
        numeric_weight = float(weight)
        if math.isfinite(numeric_weight):
            projected["weight"] = max(
                -_PUBLIC_GRAPH_NUMBER_LIMIT,
                min(numeric_weight, _PUBLIC_GRAPH_NUMBER_LIMIT),
            )
    # `implicit` — булево, а не строка, поэтому мимо текстового allowlist оно
    # прошло бы молча. Пометка обязательна: без неё выведенное соседство
    # неотличимо от объявленной человеком связи.
    implicit = relation.get("implicit")
    if isinstance(implicit, bool):
        projected["implicit"] = implicit
    provenance = _relation_provenance(dict(relation))
    if provenance:
        projected["provenance"] = provenance
    return projected


def _validated_history_snapshot_status(
    status: Mapping[str, Any],
    *,
    requested_known_at: str,
) -> dict[str, Any]:
    """Canonical complete provenance for one transaction-time graph snapshot."""

    required = ("known_at", "known_at_floor", "history_complete", "identity_basis")
    if any(field not in status for field in required):
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


def _public_graph_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded allowlist for graph nodes; arbitrary entity fields stay internal."""

    projected: dict[str, Any] = {
        field: str(node.get(field) or "")[:limit] for field, limit in _PUBLIC_GRAPH_NODE_TEXT_LIMITS.items()
    }
    raw_count = node.get("knowledge_count")
    if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool):
        numeric = float(raw_count)
        if math.isfinite(numeric):
            projected["knowledge_count"] = max(
                0,
                min(int(numeric), _MAX_PUBLIC_GRAPH_COUNT),
            )
        else:
            projected["knowledge_count"] = 0
    else:
        projected["knowledge_count"] = 0
    return projected


def _is_live_graph_entity(entity: Mapping[str, Any] | None) -> bool:
    """One public definition of an entity which may anchor a graph response."""

    return bool(
        entity
        and not entity.get("deleted_at")
        and bool(entity.get("canonical", 1))
        and not entity.get("merged_into_id")
    )


def _bounded_graph_count(value: Any, fallback: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return max(fallback, min(numeric, _MAX_PUBLIC_GRAPH_COUNT))


def _safe_merge_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Small merge mutation result safe for HTTP and model tool boundaries."""

    output: dict[str, Any] = {
        "merge_id": str(raw.get("merge_id") or raw.get("_merge_id") or "")[:160],
        "source_entity_id": str(raw.get("source_entity_id") or "")[:160],
        "target_entity_id": str(raw.get("target_entity_id") or "")[:160],
    }
    for field in ("merged_into", "source", "target"):
        entity = raw.get(field)
        if isinstance(entity, Mapping):
            output[field] = _public_graph_node(entity)
    if raw.get("undone_at"):
        output["undone_at"] = str(raw.get("undone_at") or "")[:64]
    return output


def _bounded_public_number(value: Any, *, maximum: float = 1.0) -> float:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return max(0.0, min(numeric, maximum))


def _bounded_public_count(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), _MAX_PUBLIC_GRAPH_COUNT))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_knowledge_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Small document card shared by HTTP profiles and the model tool."""

    try:
        decoded = json.loads(str(raw.get("tags_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = []
    tags: list[str] = []
    if isinstance(decoded, list):
        for item in decoded:
            if not isinstance(item, str) or not item.strip():
                continue
            tags.append(item[:_PUBLIC_KNOWLEDGE_TAG_MAX_CHARS])
            if len(tags) >= _PUBLIC_KNOWLEDGE_TAG_LIMIT:
                break
    card: dict[str, Any] = {
        "id": str(raw.get("id") or "")[:160],
        "title": str(raw.get("title") or "")[:240],
        "summary": str(raw.get("summary") or "")[:500],
        "tags": tags,
        # Compatibility for clients which still decode the storage-shaped field.
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "importance": _bounded_public_number(raw.get("importance")),
        "quality_score": _bounded_public_number(raw.get("quality_score")),
        "document_date": str(raw.get("document_date") or "")[:64],
        "lifecycle_stage": str(raw.get("lifecycle_stage") or "")[:80],
        "knowledge_kind": str(raw.get("knowledge_kind") or "")[:80],
        "created_at": str(raw.get("created_at") or "")[:64],
        "updated_at": str(raw.get("updated_at") or "")[:64],
    }
    if "_link_confidence" in raw:
        card["_link_confidence"] = _bounded_public_number(raw.get("_link_confidence"))
    return card


def _safe_conflict_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded conflict review shape; evidence and notes stay in SQLite."""

    text_limits = {
        "id": 160,
        "knowledge_a_id": 160,
        "knowledge_b_id": 160,
        "conflict_type": 80,
        "status": 40,
        "created_at": 64,
        "reviewed_at": 64,
        "knowledge_a_title": 240,
        "knowledge_a_summary": 500,
        "knowledge_a_stage": 80,
        "knowledge_a_superseded_by": 160,
        "knowledge_b_title": 240,
        "knowledge_b_summary": 500,
        "knowledge_b_stage": 80,
        "knowledge_b_superseded_by": 160,
    }
    card: dict[str, Any] = {field: str(raw.get(field) or "")[:limit] for field, limit in text_limits.items()}
    card["confidence"] = _bounded_public_number(raw.get("confidence"))
    raw_evidence = str(raw.get("evidence_json") or "")
    evidence_present = (
        bool(raw.get("evidence_present"))
        if "evidence_present" in raw
        else raw_evidence not in {"", "{}", "[]", "null"}
    )
    evidence_bytes = (
        raw.get("evidence_bytes")
        if "evidence_bytes" in raw
        else len(raw_evidence.encode("utf-8", errors="replace"))
    )
    card["evidence"] = {
        "present": evidence_present,
        "bytes": _bounded_public_count(evidence_bytes),
    }
    note_chars = (
        raw.get("resolution_note_chars")
        if "resolution_note_chars" in raw
        else len(str(raw.get("resolution_note") or ""))
    )
    card["resolution_note_chars"] = _bounded_public_count(note_chars)
    triage = raw.get("triage")
    if isinstance(triage, Mapping):
        card["triage"] = {
            "hint": str(triage.get("hint") or "")[:40],
            "label_ru": str(triage.get("label_ru") or "")[:120],
            "jaccard": _bounded_public_number(triage.get("jaccard")),
            "length_ratio": _bounded_public_number(triage.get("length_ratio")),
            "data_diff_share": _bounded_public_number(triage.get("data_diff_share")),
        }
    return card


def _safe_conflict_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Mutation result without conflict evidence, resolution prose, or bodies."""

    nested = raw.get("conflict")
    if isinstance(nested, Mapping):
        output: dict[str, Any] = {"conflict": _safe_conflict_card(nested)}
        for field in ("winner_id", "deprecated_id"):
            if field in raw:
                output[field] = str(raw.get(field) or "")[:160]
        return output
    return _safe_conflict_card(raw)


def _public_event_source(value: Any) -> str:
    source = str(value or "")
    if source.startswith("reminder:"):
        return "reminder"
    if source in {"user", "ingestion"}:
        return source
    return "other" if source else ""


def _safe_event_time(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        "entity_id": str(raw.get("entity_id") or "")[:160],
        "occurred_at": str(raw.get("occurred_at") or "")[:64],
        "occurred_end": str(raw.get("occurred_end") or "")[:64] or None,
        "precision": str(raw.get("precision") or "")[:40],
        "source": _public_event_source(raw.get("source")),
        "updated_at": str(raw.get("updated_at") or "")[:64],
        "relation": EVENT_TIME_RELATION,
    }


def _safe_timeline_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlisted timeline row with bounded text and no reminder identity."""

    if raw.get("kind") == "event":
        event_time = _safe_event_time(raw) or {}
        return {
            "kind": "event",
            "entity_id": str(raw.get("entity_id") or "")[:160],
            "name": str(raw.get("name") or "")[:240],
            "entity_type": str(raw.get("entity_type") or "")[:80],
            "description": str(raw.get("description") or "")[:500],
            **event_time,
            "at": str(raw.get("at") or raw.get("occurred_at") or "")[:64],
            "boundary": EVENT_TIME_RELATION,
        }
    raw_source = raw.get("source")
    raw_target = raw.get("target")
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    target: Mapping[str, Any] = raw_target if isinstance(raw_target, Mapping) else {}
    return {
        "kind": "relation",
        "at": str(raw.get("at") or "")[:64],
        "boundary": str(raw.get("boundary") or "")[:40],
        "relation_id": str(raw.get("relation_id") or "")[:160],
        "relation_type": str(raw.get("relation_type") or "")[:80],
        "source": {
            "id": str(source.get("id") or "")[:160],
            "name": str(source.get("name") or "")[:240],
        },
        "target": {
            "id": str(target.get("id") or "")[:160],
            "name": str(target.get("name") or "")[:240],
        },
        "valid_from": str(raw.get("valid_from") or "")[:64],
        "valid_to": str(raw.get("valid_to") or "")[:64] or None,
        "created_at": str(raw.get("created_at") or "")[:64],
        "invalidated_at": str(raw.get("invalidated_at") or "")[:64] or None,
        "superseded_by": str(raw.get("superseded_by") or "")[:160] or None,
    }


def _graph_path_id(state: _GraphPathState) -> str:
    """Stable opaque ID for exactly one root/edge route."""

    route = [state.root, *state.entity_ids, *(str(edge["id"]) for edge in state.edges)]
    digest = hashlib.sha256("\x1f".join(route).encode()).hexdigest()[:20]
    return f"gpath_{digest}"


def _path_state_is_better(candidate: _GraphPathState, current: _GraphPathState | None) -> bool:
    """Total deterministic order: score, then shorter path, then stable IDs."""

    if current is None:
        return True
    if candidate.score != current.score:
        return candidate.score > current.score
    if len(candidate.edges) != len(current.edges):
        return len(candidate.edges) < len(current.edges)
    candidate_route = (candidate.entity_ids, tuple(str(edge["id"]) for edge in candidate.edges))
    current_route = (current.entity_ids, tuple(str(edge["id"]) for edge in current.edges))
    return candidate_route < current_route


def _valid_iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_event_date(text: str) -> tuple[str, str] | None:
    """First absolute date in ``text`` as (ISO ``YYYY-MM-DD``, precision), else None.

    Relative expressions (today/tomorrow/weekday) are intentionally ignored: they
    cannot be anchored deterministically and are left for the user to set explicitly.
    """
    if not text:
        return None
    for match in _ISO_FULL_RE.finditer(text):
        iso = _valid_iso(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso:
            return iso, "day"
    for match in _DAY_FIRST_RE.finditer(text):
        iso = _valid_iso(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        if iso:
            return iso, "day"
    for match in _YEAR_MONTH_RE.finditer(text):
        iso = _valid_iso(int(match.group(1)), int(match.group(2)), 1)
        if iso:
            return iso, "month"
    return None


def normalize_event_date(value: str) -> tuple[str, str]:
    """Normalize a user date (``YYYY`` / ``YYYY-MM`` / ``YYYY-MM-DD``) to (ISO, precision).

    Raises ``ValueError`` on anything that is not a valid calendar date.
    """
    cleaned = (value or "").strip()
    parts = re.split(r"[-./]", cleaned)
    try:
        nums = [int(part) for part in parts]
        if len(nums) == 1 and len(parts[0]) == 4:
            return date(nums[0], 1, 1).isoformat(), "year"
        if len(nums) == 2:
            return date(nums[0], nums[1], 1).isoformat(), "month"
        if len(nums) == 3:
            return date(nums[0], nums[1], nums[2]).isoformat(), "day"
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid date: {value!r}") from exc
    raise ValueError(f"Invalid date: {value!r}")


def _timeline_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """One deterministic order shared by event and relation timeline rows."""

    at = str(item.get("at") or "")
    if item.get("kind") == "event":
        return (
            at,
            0,
            0,
            str(item.get("name") or ""),
            "",
            str(item.get("entity_id") or ""),
        )
    raw_source = item.get("source")
    raw_target = item.get("target")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    boundary_rank = 0 if item.get("boundary") == "confirmed" else 1
    return (
        at,
        1,
        boundary_rank,
        str(item.get("relation_type") or ""),
        str(source.get("name") or ""),
        str(target.get("name") or ""),
        str(item.get("relation_id") or ""),
    )


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(item) for item in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_entity_terms(name: str, aliases: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    output = [(name.strip(), "canonical_name")]
    output.extend((alias.strip(), "alias") for alias in aliases if alias.strip())
    unique: dict[str, tuple[str, str]] = {}
    for value, source in output:
        normalized = value.casefold()
        if len(normalized) < 2:
            continue
        current = unique.get(normalized)
        if current is None or source == "canonical_name":
            unique[normalized] = (value, source)
    return tuple(sorted(unique.values(), key=lambda item: len(item[0]), reverse=True))


@lru_cache(maxsize=8192)
def _entity_terms_cached(name: str, aliases_json: str) -> tuple[tuple[str, str], ...]:
    """Keyed by the stored strings themselves, so an edited entity gets a fresh key."""
    return _build_entity_terms(name, tuple(_json_list(aliases_json)))


def _entity_terms(entity: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return canonical name and aliases without broad or morphological matching."""
    name = str(entity.get("name") or "")
    aliases = entity.get("aliases_json")
    if isinstance(aliases, str):
        # Every entity in the graph is walked on every query, and re-parsing the
        # same alias JSON each time was the largest single cost in the graph
        # channel: 20 of its 68 ms per query on the 342-document stand.
        return _entity_terms_cached(name, aliases)
    return _build_entity_terms(name, tuple(_json_list(aliases)))


_TOKEN_RE = re.compile(r"(?u)\b[\w.+#/-]{2,}\b")


@lru_cache(maxsize=8192)
def _overlap_tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.casefold()))


def _token_overlap(query: str, value: str) -> float:
    left = _overlap_tokens(query)
    right = _overlap_tokens(value)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# Насколько далеко друг от друга могут стоять два упоминания, чтобы фраза между
# ними считалась утверждением о связи. Замер — в `suggest_relations_for_knowledge`.
_RELATION_SPAN_CHARS = 400

#: Сколько знаков допускается между родственным словом и следующим именем.
#: Ровно столько, чтобы пройти знаки препинания и дату («Отец: Горбунов»,
#: «дочь – Чикачева»), но не перечисление полей бланка.
_FAMILY_WORD_TO_NAME_CHARS = 12

# Четвёртое поле — `reversed`: чья сторона встречается в тексте первой. У «X
# управляет Y» подлежащее (X, руководитель) стоит ДО глагола — совпадает с
# порядком хранения (source=left, target=right), reversed=False. У «X
# подчиняется Y» подлежащее (X, подчинённый) тоже стоит до глагола, но это тот
# же MANAGES наоборот: Y руководит X. Без разворота связь легла бы задом
# наперёд — начальник значился бы подчинённым своего подчинённого. Найдено и
# исправлено ДО применения, состязательным ревью перед демо для команды.
_RELATION_PHRASES: tuple[tuple[re.Pattern[str], RelationType, float, bool], ...] = (
    (
        re.compile(r"\b(?:использует|используют|uses?|runs?\s+on|работает\s+на)\b", re.I),
        RelationType.USES,
        0.90,
        False,
    ),
    (
        re.compile(
            r"\b(?:управляет|администрирует|руководит|тренирует|manages?|administers?|leads?|coaches?)\b",
            re.I,
        ),
        RelationType.MANAGES,
        0.88,
        False,
    ),
    (
        re.compile(r"\b(?:работает\s+над|отвечает\s+за|works?\s+on|is\s+responsible\s+for)\b", re.I),
        RelationType.WORKS_ON,
        0.88,
        False,
    ),
    (re.compile(r"\b(?:зависит\s+от|depends?\s+on)\b", re.I), RelationType.DEPENDS_ON, 0.90, False),
    # Голое слово «часть» из этой записи УБРАНО, и это замер, а не вкус: в корпусе
    # владельца 13 394 вхождения «часть/части», из них 9758 (72.9%) — «войсковая
    # часть» и «в/ч», то есть название организационной единицы, а не утверждение
    # «X является частью Y». Собственный разбор очереди (TASKS.md, #47) показал ту
    # же картину с другой стороны: все 70 кандидатов были part_of, и не меньше 29
    # из них — этот самый ложный друг. Улика — объявляющее слово, а не близость
    # двух имён к слову «часть».
    (
        re.compile(r"\b(?:входит\s+в\s+состав|входит\s+в|является\s+частью|part\s+of)\b", re.I),
        RelationType.PART_OF,
        0.82,
        False,
    ),
    (re.compile(r"\b(?:член|участник|состоит\s+в|member\s+of)\b", re.I), RelationType.MEMBER_OF, 0.82, False),
    # Иерархия, подчинённый упомянут первым: «Иванов подчиняется Смирновой»,
    # «Петров отчитывается перед Кузнецовым», «Кузнецов подотчётен директору».
    # Один из явно названных пробелов для сценария «4 начальника + 3
    # подчинённых» — найдено состязательным ревью на синтетике перед демо.
    (
        re.compile(
            r"\b(?:подчиняется|подотчётен|подотчетен|отчитывается\s+перед|"
            r"reports?\s+to|accountable\s+to)\b",
            re.I,
        ),
        RelationType.MANAGES,
        0.85,
        True,
    ),
    # Сотрудничество и координация — связь симметричная, разворот не нужен:
    # порядок упоминания сторон не меняет смысла «X координирует с Y».
    (
        re.compile(
            r"\b(?:сотрудничает\s+с|координирует\s+с|консультируется\s+с|встречается\s+с|"
            r"coordinates?\s+with|collaborates?\s+with|consults?\s+with|meets?\s+(?:\w+\s+)?with)\b",
            re.I,
        ),
        RelationType.RELATED_TO,
        0.75,
        False,
    ),
    # Межотраслевой пробел, найденный на синтетике за пределами военного архива
    # владельца — состязательное ревью перед демо, тема содержимого команды
    # заранее непредсказуема («разной тематики»). Технические/деловые/
    # административные/медицинские глаголы, каждый субъект-первый (X делает
    # действие Y), разворот не нужен. RELATED_TO намеренно, а не более точный
    # тип: «кто кого поставляет/уведомляет/лечит» не описан существующими
    # RelationType, и утверждать точный тип значило бы гадать вместо честной
    # нижней границы «эти двое как-то связаны».
    (
        re.compile(
            r"\b(?:интегрируется\s+с|взаимодействует\s+с|поставляет|"
            r"направил[аи]?|уведомил[аи]?|диагностировал[аи]?|"
            r"подписал[аи]?\s+(?:контракт|договор)|заключил[аи]?\s+договор|"
            r"integrates?\s+with|interacts?\s+with|supplies?|forwarded|notified|"
            r"diagnosed|signed\s+a\s+contract)\b",
            re.I,
        ),
        RelationType.RELATED_TO,
        0.72,
        False,
    ),
    # Родство. Замерено на архиве владельца 2026-08-03, когда обратный проход по
    # всем 1533 документам дал НОЛЬ кандидатов и надо было понять, почему:
    #
    #   объявляющее слово (любое из прежних)      52 док.   3%
    #   «назначить на должность»                   8 док.   0%
    #   «состоит в должности»                      0 док.   0%
    #   «зачислить в списки»                      32 док.   2%
    #   «супруга/сын/дочь/отец/мать/брат/…»      186 док.  12%   <- вот это
    #
    # То есть в корпусе из личных дел, приказов и списков служебные отношения
    # словом почти не объявляются — а родственные объявляются, и чаще всего
    # прочего. Это единственный класс, где здесь есть настоящая улика.
    #
    # Соседство уликой по-прежнему не считается: слово обязано стоять МЕЖДУ
    # двумя именами в пределах абзаца, как и для всех остальных. Ошибка «двое в
    # одном документе — значит связаны» на этом проекте ловилась трижды за один
    # день (графовый канал, очередь слияний 20 -> 45 061 пара, извлечение
    # связей), и повторять её нельзя тем более на списках по 50+ человек.
    #
    # Симметрично и без разворота: «Иванов, супруга Иванова» и «Иванова, муж
    # Иванов» описывают одну и ту же пару. Точную роль (кто кому сын) отсюда не
    # вывести — для этого нужен разбор падежей, — а `family_of` честно говорит
    # ровно то, что доказано: эти двое родня.
    (
        re.compile(
            r"\b(?:супруг|супруга|супруги|жена|жены|муж|мужа|"
            r"сын|сына|сыновья|дочь|дочери|дочка|"
            r"отец|отца|мать|матери|мама|папа|"
            r"брат|брата|сестра|сестры|сестрёнка|"
            # «Внук», «племянник» и прочая дальняя родня из словаря УБРАНЫ, и это
            # замер, а не вкус: все четыре кандидата, которые дало слово «внук»,
            # пришли из одного списка личного состава — «Рядовой Нечипоренко
            # Алексей Юрьевич (АВ-689922) Внук, Рядовой Азанов Иван Борисович
            # (АБ-745975) Горный». Это позывные, а не родство.
            #
            # В анкетах таких полей нет вовсе: бланк спрашивает супругу, детей,
            # родителей, брата с сестрой. Слово, которое в этом корпусе даёт
            # только ложные срабатывания, в словаре не нужно.
            r"spouse|wife|husband|son|daughter|father|mother|brother|sister)\b",
            re.I,
        ),
        RelationType.FAMILY_OF,
        0.80,
        False,
    ),
)

_CONFLICT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "uses",
        re.compile(
            r"(?P<subject>[\wА-ЯЁа-яё.+#/@:-][\wА-ЯЁа-яё .+#/@:-]{1,80}?)\s+"
            r"(?:использует|uses|работает\s+на|runs?\s+on)\s+"
            r"(?P<value>[\wА-ЯЁа-яё.+#/@:-]+(?:\s+\d+(?:\.\d+)*)?)",
            re.I,
        ),
    ),
    (
        "address",
        re.compile(
            r"(?P<subject>[\wА-ЯЁа-яё.+#/@:-][\wА-ЯЁа-яё .+#/@:-]{1,80}?)\s+"
            r"(?:имеет\s+IP|IP(?:-адрес)?|has\s+IP)\s*[:=—-]?\s*"
            r"(?P<value>(?:\d{1,3}\.){3}\d{1,3})",
            re.I,
        ),
    ),
    (
        "quoted_value",
        re.compile(
            r"(?P<subject>[A-Za-zА-ЯЁ0-9][A-Za-zА-ЯЁа-яё0-9._+#/@:-]{1,63})\s*=\s*"
            r"(?P<value>[-+]?\d[\d\s.,]*(?:\s*[A-ZА-ЯЁ]{2,8})?)",
            re.I,
        ),
    ),
    (
        "scheduled_date",
        re.compile(
            r"(?P<subject>[A-Za-zА-ЯЁ0-9«\"][\wА-ЯЁа-яё .«»\"+#/@:-]{1,80}?)\s+"
            r"(?:состоится|пройдёт|пройдет|запланирован\w*|назначен\w*|"
            r"scheduled\s+(?:for|on)|will\s+be\s+held\s+on)\s+"
            r"(?P<value>\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{4})",
            re.I,
        ),
    ),
)


def _normalized_claims(text: str) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    for predicate, pattern in _CONFLICT_PATTERNS:
        for match in pattern.finditer(text or ""):
            subject = " ".join(match.group("subject").split()).strip(" .,:;—-")
            value = " ".join(match.group("value").split()).strip(" .,:;—-")
            if not subject or not value:
                continue
            if predicate == "scheduled_date":
                # Compare dates by their normalized ISO value, so the same day in a
                # different format is NOT flagged as a contradiction.
                parsed = parse_event_date(value)
                if parsed:
                    value = parsed[0]
            claims.append(
                {
                    "predicate": predicate,
                    "subject": subject,
                    "subject_key": subject.casefold(),
                    "value": value,
                    "value_key": value.casefold(),
                    "evidence": match.group(0)[:300],
                }
            )
    return claims


class EntityResolver:
    """Detect duplicates, but never merge an uncertain pair automatically."""

    def __init__(self, storage: FridayStorage) -> None:
        self.storage = storage

    def detect_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.55,
    ) -> list[EntityResolutionCandidate]:
        output: list[EntityResolutionCandidate] = []
        for candidate in self.storage.find_duplicate_candidates(
            user_id,
            min_confidence=max(0.0, min(1.0, min_confidence)),
        ):
            stored = self.storage.store_resolution_candidate(candidate)
            if str(stored.status) in {ResolutionStatus.SUGGESTED.value, str(ResolutionStatus.SUGGESTED)}:
                output.append(stored)
        # Deduplicate pairs when storage returned an already existing proposal.
        unique: dict[str, EntityResolutionCandidate] = {item.pair_key: item for item in output}
        return sorted(unique.values(), key=lambda item: item.confidence, reverse=True)

    def sweep_duplicates(
        self,
        user_id: str,
        *,
        min_confidence: float = 0.55,
        max_pairs: int = 50_000,
    ) -> dict[str, Any]:
        """One budgeted tick, and a report that admits what it has not looked at yet.

        `detect_duplicates` returns proposals; this returns the state of the walk.
        The difference matters to the reader: an empty proposal list with
        `keys_pending > 0` means «not looked at yet», and returning it as a bare
        empty list is how a reviewer concludes there is nothing left to merge.
        """
        candidates, report = self.storage.sweep_entity_duplicates(
            user_id,
            min_confidence=max(0.0, min(1.0, min_confidence)),
            max_pairs=max_pairs,
        )
        # The durable cursor has already been stored by storage. Its key can
        # contain a private name/alias token and must not cross into an HTTP or
        # model result; only structural progress survives this boundary.
        report = {
            key: value
            for key, value in report.items()
            if key
            in {
                "entities",
                "pairs_examined",
                "keys_total",
                "keys_examined",
                "keys_pending",
                "partial",
                "sweeps",
                "resumed",
                "complete",
                "candidates",
            }
        }
        stored_suggested = 0
        for candidate in candidates:
            stored = self.storage.store_resolution_candidate(candidate)
            if str(stored.status) in {ResolutionStatus.SUGGESTED.value, str(ResolutionStatus.SUGGESTED)}:
                stored_suggested += 1
        report["suggested"] = stored_suggested
        report["pending_total"] = self.storage.count_resolution_candidates(
            user_id,
            ResolutionStatus.SUGGESTED,
        )
        return report

    def get_resolutions(
        self,
        user_id: str,
        status: ResolutionStatus | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Bounded structural review cards, shared by owner and admin APIs.

        Обогащение стоит шесть запросов на кандидата, из них два — по объектам
        знаний. Без предела это множилось на всю таблицу: на 5000 сущностях фоновый
        обход накопил 4012 кандидатур, и один вызов занимал 317 секунд.
        Читателю нужны те, что сверху по уверенности, а не все.
        """
        bounded = max(1, min(int(limit), 500))
        candidates = _bounded_resolution_candidate_rows(
            self.storage,
            user_id,
            status,
            limit=bounded + 1,
            offset=max(0, int(offset)),
        )
        enriched: list[dict[str, Any]] = []
        for candidate in candidates[:bounded]:
            left = _bounded_entity_by_id(self.storage, candidate["entity_a_id"], user_id)
            right = _bounded_entity_by_id(self.storage, candidate["entity_b_id"], user_id)
            if not left or not right:
                continue
            try:
                confidence = float(candidate.get("confidence", 0.0))
            except (TypeError, ValueError, OverflowError):
                confidence = 0.0
            confidence = max(0.0, min(confidence, 1.0)) if math.isfinite(confidence) else 0.0
            if confidence >= 0.95:
                recommendation = "strong_merge_candidate"
            elif confidence >= 0.78:
                recommendation = "compare_context"
            else:
                recommendation = "manual_review"
            enriched.append(
                {
                    "id": str(candidate.get("id") or "")[:160],
                    "entity_a_id": str(candidate.get("entity_a_id") or "")[:160],
                    "entity_b_id": str(candidate.get("entity_b_id") or "")[:160],
                    "confidence": confidence,
                    "resolution_method": str(candidate.get("resolution_method") or "")[:80],
                    "status": str(candidate.get("status") or "")[:40],
                    "created_at": str(candidate.get("created_at") or "")[:64],
                    "resolved_at": str(candidate.get("resolved_at") or "")[:64],
                    "entity_a": {
                        "id": str(left.get("id") or "")[:160],
                        "name": str(left.get("name") or "")[:240],
                        "entity_type": str(left.get("entity_type") or "")[:80],
                        "knowledge_count": self.storage.count_entity_knowledge(user_id, left["id"]),
                        "relation_count": self.storage.count_entity_relations(left["id"], user_id),
                    },
                    "entity_b": {
                        "id": str(right.get("id") or "")[:160],
                        "name": str(right.get("name") or "")[:240],
                        "entity_type": str(right.get("entity_type") or "")[:80],
                        "knowledge_count": self.storage.count_entity_knowledge(user_id, right["id"]),
                        "relation_count": self.storage.count_entity_relations(right["id"], user_id),
                    },
                    "recommendation": recommendation,
                }
            )
        return _BoundedEntityList(
            enriched,
            matched_at_least=len(candidates),
            truncated=len(candidates) > bounded,
        )

    def get_pending_resolutions(self, user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.get_resolutions(user_id, ResolutionStatus.SUGGESTED, limit=limit)

    def accept_resolution(
        self,
        candidate_id: str,
        user_id: str,
        *,
        target_entity_id: str | None = None,
        resolved_by: str | None = None,
    ) -> dict[str, Any]:
        candidate = _bounded_resolution_candidate_by_id(self.storage, candidate_id, user_id)
        if not candidate or candidate["status"] != ResolutionStatus.SUGGESTED.value:
            raise ValueError("Resolution candidate was not found or is no longer pending")
        pair = {candidate["entity_a_id"], candidate["entity_b_id"]}
        if target_entity_id is not None and target_entity_id not in pair:
            raise ValueError("target_entity_id must be one of the proposed entities")

        if target_entity_id is None:
            left, right = candidate["entity_a_id"], candidate["entity_b_id"]
            left_relations = self.storage.count_entity_relations(left, user_id)
            right_relations = self.storage.count_entity_relations(right, user_id)
            left_knowledge = self.storage.count_entity_knowledge(user_id, left)
            right_knowledge = self.storage.count_entity_knowledge(user_id, right)
            # Stable tie-break: richer entity, then older record (candidate A).
            target_entity_id = (
                left if (left_relations + left_knowledge) >= (right_relations + right_knowledge) else right
            )
        source_entity_id = next(entity_id for entity_id in pair if entity_id != target_entity_id)
        merged = self.storage.merge_entities(
            user_id,
            source_entity_id,
            target_entity_id,
            merged_by=resolved_by or user_id,
        )
        self.storage.resolve_candidate(
            candidate_id,
            ResolutionStatus.MERGED,
            resolved_by or user_id,
            user_id=user_id,
        )
        return {
            "source_entity_id": source_entity_id,
            "target_entity_id": target_entity_id,
            "merged_into": merged,
            "merge_id": merged.get("_merge_id"),
        }

    def reject_resolution(self, candidate_id: str, user_id: str, *, resolved_by: str | None = None) -> bool:
        candidate = _bounded_resolution_candidate_by_id(self.storage, candidate_id, user_id)
        if not candidate or candidate["status"] == ResolutionStatus.MERGED.value:
            raise ValueError("Resolution candidate not found")
        if not self.storage.resolve_candidate(
            candidate_id,
            ResolutionStatus.REJECTED,
            resolved_by or user_id,
            user_id=user_id,
        ):
            raise ValueError("Resolution candidate not found")
        return True

    def unmerge(
        self,
        user_id: str,
        merge_id: str,
        *,
        undone_by: str | None = None,
    ) -> dict[str, Any]:
        """Undo one accepted merge. Requires the transfer set recorded at merge time."""
        return self.storage.unmerge_entities(user_id, merge_id, undone_by=undone_by or user_id)


@lru_cache(maxsize=4096)
def _mention_pattern(term: str) -> re.Pattern[str]:
    """Compiled once per distinct term and reused across queries and tenants."""
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE | re.UNICODE)


class KnowledgeGraph:
    def __init__(self, storage: FridayStorage) -> None:
        self.storage = storage
        self.resolver = EntityResolver(storage)

    def create_entity(
        self,
        user_id: str,
        name: str,
        entity_type: EntityType = EntityType.OTHER,
        *,
        aliases: list[str] | None = None,
        description: str = "",
        metadata: dict[str, Any] | None = None,
        deduplicate: bool = True,
    ) -> dict[str, Any]:
        clean_name = " ".join((name or "").split()).strip()
        if not clean_name:
            raise ValueError("Entity name is required")
        existing = self.find_entity(user_id, clean_name) if deduplicate else None
        if existing and existing.get("entity_type") == entity_type.value:
            return existing
        entity = Entity(
            id=new_id("ent"),
            user_id=user_id,
            name=clean_name,
            entity_type=entity_type,
            aliases_json=aliases or [],
            description=description,
            metadata_json=metadata or {},
        )
        self.storage.create_entity(entity)
        return self.storage.get_entity(entity.id, user_id) or {}

    def get_entity(self, entity_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self.storage.get_entity(entity_id, user_id)

    def set_event_time(
        self,
        user_id: str,
        entity_id: str,
        occurred_at: str,
        *,
        occurred_end: str | None = None,
        precision: str | None = None,
        source: str = "user",
    ) -> dict[str, Any]:
        """Give an event entity a temporal anchor (RelationType.OCCURRED_AT).

        Both ends are normalized to a valid ISO date; an end before the start is
        rejected. Only ``event`` entities may carry a time.
        """
        entity = _bounded_entity_by_id(self.storage, entity_id, user_id)
        if not entity or entity.get("deleted_at"):
            raise ValueError("Event entity not found")
        if str(entity.get("entity_type")) != EntityType.EVENT.value:
            raise ValueError("Only event entities can have an occurrence time")
        start_iso, start_precision = normalize_event_date(occurred_at)
        end_iso: str | None = None
        if occurred_end:
            end_iso, _ = normalize_event_date(occurred_end)
            if end_iso < start_iso:
                raise ValueError("occurred_end must not precede occurred_at")
        record = self.storage.set_entity_time(
            entity_id,
            user_id,
            start_iso,
            occurred_end=end_iso,
            precision=precision or start_precision,
            source=source,
        )
        record["relation"] = EVENT_TIME_RELATION
        return record

    def get_event_time(self, user_id: str, entity_id: str) -> dict[str, Any] | None:
        record = self.storage.get_entity_time(entity_id, user_id)
        if record:
            record["relation"] = EVENT_TIME_RELATION
        return record

    def record_event_time_from_text(
        self, user_id: str, entity_id: str, text: str, *, source: str = "ingestion"
    ) -> dict[str, Any] | None:
        """Best-effort: stamp an event with the first absolute date in its source text.

        Only fills a gap — an existing (e.g. user-set) time is never overwritten.
        """
        if self.storage.get_entity_time(entity_id, user_id):
            return None
        parsed = parse_event_date(text)
        if not parsed:
            return None
        occurred_at, precision = parsed
        return self.storage.set_entity_time(
            entity_id, user_id, occurred_at, precision=precision, source=source
        )

    def timeline(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
        person_id: str = "",
    ) -> list[dict[str, Any]]:
        """Backward-compatible item list from the exact unified timeline page."""

        return self.timeline_page(
            user_id,
            start=start,
            end=end,
            limit=limit,
            person_id=person_id,
        )["items"]

    def timeline_page(
        self,
        user_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 200,
        person_id: str = "",
    ) -> dict[str, Any]:
        """Events and relation valid-time changes under one stable global limit."""

        normalized_start = normalize_event_date(start)[0] if start else None
        normalized_end = normalize_event_date(end)[0] if end else None
        if normalized_start and normalized_end and normalized_end < normalized_start:
            raise ValueError("end не может предшествовать start")

        bounded_limit = max(1, min(int(limit), 2000))
        events = _bounded_visible_timeline_event_rows(
            self.storage,
            user_id,
            person_id,
            start=normalized_start,
            end=normalized_end,
            limit=bounded_limit,
        )
        relation_changes = self.storage.list_relation_changes_in_range(
            user_id,
            start=normalized_start,
            end=normalized_end,
            limit=bounded_limit,
        )

        items: list[dict[str, Any]] = []
        for raw_event in events:
            event = dict(raw_event)
            event["relation"] = EVENT_TIME_RELATION
            event["kind"] = "event"
            event["at"] = event.get("occurred_at")
            event["boundary"] = EVENT_TIME_RELATION
            items.append(event)
        items.extend(dict(change) for change in relation_changes)
        items.sort(key=_timeline_sort_key)
        shown = [_safe_timeline_item(item) for item in items[:bounded_limit]]

        total = _count_visible_timeline_events(
            self.storage,
            user_id,
            person_id,
            start=normalized_start,
            end=normalized_end,
        ) + self.storage.count_relation_changes_in_range(
            user_id,
            start=normalized_start,
            end=normalized_end,
        )
        return {
            "items": shown,
            "count": len(shown),
            "total": total,
            "truncated": total > len(shown),
            "start": normalized_start,
            "end": normalized_end,
        }

    def list_entities(
        self,
        user_id: str,
        entity_type: EntityType | None = None,
        *,
        limit: int = 100,
        include_merged: bool = False,
    ) -> list[dict[str, Any]]:
        return self.storage.list_entities(
            user_id,
            entity_type,
            limit=limit,
            include_merged=include_merged,
        )

    def find_entity(self, user_id: str, name: str) -> dict[str, Any] | None:
        direct = self.storage.find_entity_by_name(user_id, name)
        if direct:
            return direct
        aliases = self.storage.find_entity_by_alias(user_id, name, limit=1)
        return aliases[0] if aliases else None

    def match_mentions(
        self,
        user_id: str,
        text: str,
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Match existing canonical names and aliases conservatively in text.

        Names are matched literally with Unicode word boundaries.  This is
        deliberately not stemming or prefix matching: an identifier or person
        must actually occur in the input before it can influence graph links.

        Lookup is inverted: candidates come from the text, the database answers
        by ``normalized_name`` / alias. Walking ``list_entities(limit=5000)`` used
        to drop the alphabetical tail once the graph passed the ceiling — proven
        on 8001 entities where direct name lookup still worked and this method
        returned nothing.
        """
        if not text.strip():
            return []
        phrases = mention_phrase_candidates(text)
        bounded = max(1, min(int(limit), 200))
        entities = self.storage.find_entities_by_normalized_names(
            user_id,
            phrases,
            limit=min(800, bounded * 4),
        )
        matches: list[dict[str, Any]] = []
        occupied = bytearray(len(text))
        lowered = text.casefold()
        candidates: list[tuple[tuple[int, int, str, str], dict[str, Any], str | None, str]] = []
        for entity in entities:
            entity_id = str(entity["id"])
            for term, source in _entity_terms(entity):
                if term.casefold() not in lowered:
                    continue
                method_priority = 0 if source == "canonical_name" else 1
                candidates.append(
                    ((-len(term), method_priority, entity_id, term.casefold()), entity, term, source)
                )
            name = str(entity.get("name") or "")
            if inflected_mention_signature(name) is not None:
                candidates.append(
                    (
                        (-len(name), 2, entity_id, name.casefold()),
                        entity,
                        None,
                        "canonical_name_inflected",
                    )
                )
        # SQL intentionally returns a bounded set, not a semantic order.  Resolve
        # the actual canonical/alias/inflected matchers globally, longest label
        # first.  Ranking whole entity cards is insufficient: a card may have one
        # long inflected occurrence and a short alias elsewhere, and must not use
        # that long occurrence merely as priority for occupying the short alias.
        candidates.sort(key=lambda item: item[0])
        token_context = inflected_token_context(text) if any(item[2] is None for item in candidates) else []
        accepted: set[str] = set()
        for _priority, entity, candidate_term, candidate_source in candidates:
            entity_id = str(entity["id"])
            best: dict[str, Any] | None = None
            if candidate_term is not None:
                pattern = _mention_pattern(candidate_term)
                for hit in pattern.finditer(text):
                    if occupied.find(b"\x01", hit.start(), hit.end()) >= 0:
                        continue
                    occupied[hit.start() : hit.end()] = b"\x01" * (hit.end() - hit.start())
                    if best is None and entity_id not in accepted:
                        confidence = 0.99 if candidate_source == "canonical_name" else 0.96
                        best = {
                            "entity_id": entity["id"],
                            "name": entity["name"],
                            "entity_type": entity["entity_type"],
                            "matched_text": hit.group(0),
                            "span": [hit.start(), hit.end()],
                            "confidence": confidence,
                            "method": f"existing_{candidate_source}_exact",
                        }
            else:
                for start, end in inflected_mention_occurrences(
                    text,
                    str(entity.get("name") or ""),
                    token_context=token_context,
                ):
                    if occupied.find(b"\x01", start, end) >= 0:
                        continue
                    occupied[start:end] = b"\x01" * (end - start)
                    if best is None and entity_id not in accepted:
                        best = {
                            "entity_id": entity["id"],
                            "name": entity["name"],
                            "entity_type": entity["entity_type"],
                            "matched_text": text[start:end],
                            "span": [start, end],
                            # Ниже буквального совпадения: падеж сложен свёрткой, а
                            # свёртка — приближение, пусть и то же самое, которым
                            # определяется тождество самого узла.
                            "confidence": 0.97,
                            "method": "existing_canonical_name_inflected",
                        }
            if best:
                matches.append(best)
                accepted.add(entity_id)
        matches.sort(
            key=lambda item: (-float(item["confidence"]), item["span"][0], -len(item["matched_text"]))
        )
        return matches[:bounded]

    def search_entities(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 10,
        entity_type: EntityType | None = None,
    ) -> list[dict[str, Any]]:
        """Find graph entry points with a deterministic, bounded-memory top-k."""

        bounded = max(1, min(int(limit), 500))
        exact = {
            item["entity_id"]: item
            for item in self.match_mentions(user_id, query, limit=min(200, bounded * 2))
        }
        wanted_type = entity_type.value if entity_type is not None else ""
        # Entries stay in final rank order. Insertion is O(k), but k is capped at
        # 500 and the resident set never grows with tenant size. The former
        # append-then-sort retained every broad token match (including each 8-KB
        # alias card) until the full tenant scan had completed.
        ranked: list[tuple[tuple[float, str, str], dict[str, Any], str]] = []

        def offer(entity: dict[str, Any], score: float, method: str) -> None:
            rank = (
                -float(score),
                str(entity.get("name") or "").casefold(),
                str(entity.get("id") or ""),
            )
            if len(ranked) >= bounded and rank >= ranked[-1][0]:
                return
            low = 0
            high = len(ranked)
            while low < high:
                middle = (low + high) // 2
                if ranked[middle][0] < rank:
                    low = middle + 1
                else:
                    high = middle
            ranked.insert(low, (rank, entity, method))
            if len(ranked) > bounded:
                ranked.pop()

        # Token-overlap still needs every entity's terms: a short query can hit a
        # name that shares only one word with it, which phrase lookup alone would
        # miss. The iterator keyset-pages bounded cards without the silent 5000
        # ceiling or OFFSET's quadratic rescan cost.
        for entity in _iter_entities_for_graph_search(self.storage, user_id):
            if wanted_type and entity.get("entity_type") != wanted_type:
                continue
            if entity["id"] in exact:
                offer(
                    entity,
                    float(exact[entity["id"]]["confidence"]),
                    str(exact[entity["id"]]["method"]),
                )
                continue
            score = 0.0
            method = "token_overlap"
            for term, _source in _entity_terms(entity):
                score = max(score, _token_overlap(query, term))
            score = max(score, _token_overlap(query, str(entity.get("description") or "")) * 0.65)
            if score >= 0.30:
                offer(entity, min(0.85, score), method)
        return [
            {
                **entity,
                "_match_score": round(-rank[0], 4),
                "_match_method": method,
                # COUNT(*), not len(rows). This ran per returned entity and pulled
                # up to 1000 full Knowledge Objects — bodies and all — to produce a
                # number, plus every relation with both endpoint names.
                "_relation_count": self.storage.count_entity_relations(entity["id"], user_id),
                "_knowledge_count": self.storage.count_entity_knowledge(user_id, entity["id"]),
            }
            for rank, entity, method in ranked
        ]

    def context_for_query(
        self,
        user_id: str,
        query: str,
        *,
        depth: int = 1,
        entity_limit: int = 8,
        knowledge_limit: int = 30,
        seed_knowledge_ids: list[str] | None = None,
        as_of: str = "",
        known_at: str = "",
    ) -> dict[str, Any]:
        """Build a compact scored subgraph for retrieval and agent context.

        Query-matched entities are the primary roots. High-ranking Knowledge Objects may also
        seed entities through accepted links. In addition to explicit relations, the traversal
        can follow conservative *co-occurrence* edges between entities linked to the same
        Knowledge Object. Those implicit edges are labelled and never persisted as asserted facts.
        """

        # Validate both temporal axes before even looking up roots. A malformed
        # or incomplete transaction-time snapshot must fail explicitly even when
        # the graph happens to be empty; otherwise a merge-crossing/floor refusal
        # would silently turn into an ordinary "nothing found" answer.
        cleaned_as_of = str(as_of or "").strip()
        normalized_as_of = normalize_event_date(cleaned_as_of)[0] if cleaned_as_of else ""
        requested_known_at = str(known_at or "").strip()
        if requested_known_at:
            # Normalize independently, then require storage to echo that exact
            # boundary and every provenance field. `bool(...)`/`.get(...)`
            # fallbacks here would brand a current or incomplete graph as a
            # reproducible historical snapshot.
            normalized_requested_known_at = normalize_known_at(requested_known_at)
            history_status = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=requested_known_at,
                ),
                requested_known_at=normalized_requested_known_at,
            )
            history_watermark: int | None = _relation_revision_watermark(
                self.storage,
                user_id,
                normalized_requested_known_at,
            )
        else:
            # The current fast path predates relation history and must not become
            # dependent on a migration-floor read. Snapshot provenance is only a
            # fail-closed requirement when the caller explicitly asks for it.
            history_status = {
                "known_at": "",
                "known_at_floor": "",
                "history_complete": True,
                "identity_basis": "current_names",
            }
            history_watermark = None
        normalized_known_at = str(history_status["known_at"])

        def public_snapshot_metadata(status: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "known_at": str(status["known_at"]),
                "known_at_floor": str(status["known_at_floor"]),
                "history_complete": status["history_complete"],
                "identity_basis": str(status["identity_basis"]),
            }

        snapshot_metadata = public_snapshot_metadata(history_status)

        def confirm_history_snapshot() -> dict[str, Any]:
            """Recheck merge topology after the last current-identity read."""

            if not normalized_known_at:
                return snapshot_metadata
            if _relation_revision_watermark(self.storage, user_id, normalized_known_at) != history_watermark:
                raise RelationHistorySnapshotError("relation history changed while building query context")
            confirmed = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=normalized_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while building query context"
                )
            return public_snapshot_metadata(confirmed)

        temporal_basis = "bitemporal" if normalized_known_at else "valid_time"

        raw_entity_cache: dict[str, dict[str, Any] | None] = {}
        canonical_id_cache: dict[str, str | None] = {}

        def get_raw_entity(entity_id: str) -> dict[str, Any] | None:
            if entity_id not in raw_entity_cache:
                if normalized_known_at:
                    _assert_entities_existed_at_boundary(
                        self.storage,
                        user_id,
                        [entity_id],
                        normalized_known_at,
                    )
                raw_entity_cache[entity_id] = _graph_entity_for_traversal(
                    self.storage,
                    entity_id,
                    user_id,
                )
            return raw_entity_cache[entity_id]

        def canonical_entity_id(entity_id: str) -> str | None:
            """Follow a legacy merge tombstone to one live canonical endpoint."""

            if entity_id in canonical_id_cache:
                return canonical_id_cache[entity_id]
            visited: list[str] = []
            current_id = entity_id
            while current_id and current_id not in visited:
                visited.append(current_id)
                current = get_raw_entity(current_id)
                if not current:
                    current_id = ""
                    break
                merged_into = str(current.get("merged_into_id") or "")
                if merged_into:
                    current_id = merged_into
                    continue
                if current.get("deleted_at") or not bool(current.get("canonical", 1)):
                    current_id = ""
                break
            if current_id in visited[:-1]:
                current_id = ""
            resolved = current_id or None
            for visited_id in visited:
                canonical_id_cache[visited_id] = resolved
            return resolved

        def get_entity(entity_id: str) -> dict[str, Any] | None:
            canonical_id = canonical_entity_id(entity_id)
            return get_raw_entity(canonical_id) if canonical_id else None

        roots = self.search_entities(user_id, query, limit=entity_limit)
        root_scores: dict[str, float] = {}
        # Entities the QUERY itself matched. Only these corroborate a document
        # below: an entity discovered by traversal was very often discovered
        # THROUGH the document it would then vouch for, and letting that count
        # would pay a document for its own entity count rather than for agreeing
        # with the question.
        query_matched_ids: set[str] = set()
        # Entities whose presence traces back to the question — the query matched
        # them, or they are reachable from such a match through relations the
        # USER asserted. Co-occurrence edges and seed documents do NOT ground:
        # «Альфа зависит от Беты» is the owner's own claim and makes Beta's
        # document relevant to a question about Alpha, while «эти двое
        # встретились в одном документе» is an observation about that document.
        evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for root in roots:
            entity_id = canonical_entity_id(str(root["id"]))
            if not entity_id:
                continue
            score = float(root.get("_match_score", 0.0))
            root_scores[entity_id] = max(root_scores.get(entity_id, 0.0), score)
            query_matched_ids.add(entity_id)
            evidence[entity_id].append(
                {
                    "kind": "query_match",
                    "method": root.get("_match_method"),
                    "score": root.get("_match_score"),
                }
            )

        # Seeds arrive in RELEVANCE ORDER (retrieval passes its best FTS and
        # lexical hits, best first) and that order used to be discarded: every
        # seeded entity got a flat `0.72 * confidence`. Since most graph-scored
        # documents in production are reached this way, the channel handed a
        # near-identical constant to a whole cluster and could not order it —
        # measured on a real corpus, 93% of returned graph scores were tied with
        # another result of the same query. Decayed by position, the best seed
        # keeps its old weight and the tail is worth visibly less.
        seeds = list(seed_knowledge_ids or [])
        span = max(1, len(seeds) - 1)
        for position, knowledge_id in enumerate(seeds):
            rank_factor = 1.0 - _GRAPH_SEED_RANK_DECAY * (position / span)
            for link in self.storage.list_knowledge_entity_links(
                user_id,
                knowledge_object_id=knowledge_id,
                status="accepted",
                limit=30,
            ):
                entity_id = canonical_entity_id(str(link["entity_id"]))
                if not entity_id:
                    continue
                score = 0.72 * rank_factor * float(link.get("confidence", 1.0) or 1.0)
                root_scores[entity_id] = max(root_scores.get(entity_id, 0.0), score)
                evidence[entity_id].append(
                    {
                        "kind": "seed_knowledge_link",
                        "knowledge_object_id": knowledge_id,
                        "confidence": link.get("confidence"),
                    }
                )

        if not root_scores:
            # Root matching and canonicalization already read today's entity
            # names/topology. A concurrent merge must therefore be caught even
            # on this otherwise early empty-result path.
            snapshot_metadata = confirm_history_snapshot()
            return {
                "roots": [],
                "entities": [],
                "nodes": [],
                "relations": [],
                "knowledge": [],
                "knowledge_candidates": [],
                "paths": [],
                "paths_matched_at_least": 0,
                "paths_truncated": False,
                "temporal_basis": temporal_basis,
                "as_of": normalized_as_of,
                **snapshot_metadata,
            }

        max_depth = max(0, min(depth, _MAX_TRAVERSAL_DEPTH))
        traversal_truncated = False
        entity_knowledge_cache: dict[str, list[dict[str, Any]]] = {}
        entity_relation_cache: dict[str, list[dict[str, Any]]] = {}
        knowledge_links_cache: dict[str, list[dict[str, Any]]] = {}

        def get_entity_knowledge(entity_id: str) -> list[dict[str, Any]]:
            if entity_id not in entity_knowledge_cache:
                prefetch_entity_knowledge([entity_id])
            return entity_knowledge_cache[entity_id]

        def prefetch_entity_knowledge(entity_ids: list[str]) -> None:
            missing = list(
                dict.fromkeys(
                    entity_id for entity_id in entity_ids if entity_id not in entity_knowledge_cache
                )
            )
            if not missing:
                return
            # The traversal needs only this projection. Fetch a whole BFS frontier
            # at once so graph width no longer becomes SQL query count.
            entity_knowledge_cache.update(
                self.storage.list_entities_knowledge_refs(
                    user_id,
                    missing,
                    limit=max(100, min(1000, knowledge_limit * 4)),
                )
            )

        def get_entity_relations(entity_id: str) -> list[dict[str, Any]]:
            nonlocal traversal_truncated
            if entity_id not in entity_relation_cache:
                # The outer preflight is the validated snapshot token for this
                # whole traversal. Calling the public wrapper here used to run
                # KG + storage pre/post topology scans for every visited node.
                # Historical hops read their fixed revision projection directly;
                # one outer postflight still follows the final identity read.
                if normalized_known_at:
                    raw_relations = _historical_entity_relations(
                        self.storage,
                        entity_id,
                        user_id,
                        include_invalidated=False,
                        as_of=normalized_as_of,
                        known_at=normalized_known_at,
                        require_live_endpoints=False,
                        row_limit=_MAX_CONTEXT_RELATION_PAGE,
                    )
                else:
                    raw_relations = _current_entity_relations_for_traversal(
                        self.storage,
                        entity_id,
                        user_id,
                        as_of=normalized_as_of,
                        row_limit=_MAX_CONTEXT_RELATION_PAGE,
                    )
                if len(raw_relations) >= _MAX_CONTEXT_RELATION_PAGE:
                    traversal_truncated = True
                # Internal traversal needs legacy tombstones long enough to
                # resolve them to a current canonical endpoint, but neither the
                # cache nor the returned context may retain arbitrary metadata.
                entity_relation_cache[entity_id] = [_public_relation(relation) for relation in raw_relations]
            return entity_relation_cache[entity_id]

        def get_knowledge_links(knowledge_id: str) -> list[dict[str, Any]]:
            if knowledge_id not in knowledge_links_cache:
                knowledge_links_cache[knowledge_id] = self.storage.list_knowledge_entity_links(
                    user_id,
                    knowledge_object_id=knowledge_id,
                    status="accepted",
                    limit=30,
                )
            return knowledge_links_cache[knowledge_id]

        entities: dict[str, dict[str, Any]] = {}
        relations: dict[str, dict[str, Any]] = {}
        best_states: dict[str, _GraphPathState] = {}
        path_evidence: dict[str, dict[str, Any]] = {}
        ordered_root_ids = sorted(root_scores, key=lambda item: (-root_scores[item], item))
        if len(ordered_root_ids) > _MAX_CONTEXT_GRAPH_ENTITIES:
            traversal_truncated = True
        for entity_id in ordered_root_ids[:_MAX_CONTEXT_GRAPH_ENTITIES]:
            state = _GraphPathState(
                root=entity_id,
                current=entity_id,
                score=root_scores[entity_id],
                query_grounded=entity_id in query_matched_ids,
                entity_ids=(entity_id,),
                edges=(),
            )
            best_states[entity_id] = state
        queue: deque[_GraphPathState] = deque(best_states.values())

        def offer_neighbour(
            *,
            state: _GraphPathState,
            neighbour_id: str,
            propagated: float,
            edge: dict[str, Any],
            evidence_item: dict[str, Any],
            grounds: bool = False,
        ) -> None:
            nonlocal traversal_truncated
            if neighbour_id == state.current or neighbour_id in state.entity_ids or propagated < 0.12:
                return
            if neighbour_id not in best_states and len(best_states) >= _MAX_CONTEXT_GRAPH_ENTITIES:
                traversal_truncated = True
                return
            candidate = _GraphPathState(
                root=state.root,
                current=neighbour_id,
                score=propagated,
                # Query grounding belongs to the same immutable route as score
                # and edges.  Updating a detached set before accepting this state
                # let a rejected low-score A→B offer ground a stronger seed-root
                # B→C path that was not connected to the query at all.
                query_grounded=state.query_grounded and grounds,
                entity_ids=(*state.entity_ids, neighbour_id),
                edges=(*state.edges, edge),
            )
            if not _path_state_is_better(candidate, best_states.get(neighbour_id)):
                return
            best_states[neighbour_id] = candidate
            path_evidence[neighbour_id] = evidence_item
            queue.append(candidate)

        prefetched_depth: int | None = None
        stop_traversal = False
        while queue:
            frontier_depth = len(queue[0].edges)
            if frontier_depth != prefetched_depth:
                prefetch_entity_knowledge(
                    [
                        state.current
                        for state in queue
                        if len(state.edges) == frontier_depth and best_states.get(state.current) == state
                    ]
                )
                prefetched_depth = frontier_depth
            state = queue.popleft()
            if best_states.get(state.current) != state:
                continue
            entity_id = state.current
            current_depth = len(state.edges)
            entity = get_entity(entity_id)
            if not entity:
                continue
            linked_knowledge = get_entity_knowledge(entity_id)
            explicit_relations = get_entity_relations(entity_id)
            route_evidence = [*evidence[entity_id]]
            if entity_id in path_evidence:
                route_evidence.append(path_evidence[entity_id])
            entities[entity_id] = {
                **entity,
                "_graph_depth": current_depth,
                "_graph_score": round(state.score, 6),
                "_evidence": route_evidence,
                "_relation_count": len(explicit_relations),
                "_knowledge_count": len(linked_knowledge),
            }
            if current_depth >= max_depth:
                continue

            next_depth = current_depth + 1
            for relation in sorted(explicit_relations, key=lambda item: str(item.get("id") or "")):
                relation_id = str(relation["id"])
                source_id = canonical_entity_id(str(relation["source_entity_id"]))
                target_id = canonical_entity_id(str(relation["target_entity_id"]))
                if not source_id or not target_id or source_id == target_id:
                    continue
                if source_id == entity_id:
                    neighbour_id = target_id
                    direction = "forward"
                elif target_id == entity_id:
                    neighbour_id = source_id
                    direction = "reverse"
                else:
                    # A corrupt/legacy row that is not incident to this canonical
                    # endpoint cannot be used to construct a coherent route.
                    continue
                if relation_id not in relations and len(relations) >= _MAX_CONTEXT_GRAPH_RELATIONS:
                    traversal_truncated = True
                    stop_traversal = True
                    break
                source_entity = get_entity(source_id) or {}
                target_entity = get_entity(target_id) or {}
                relations[relation_id] = {
                    **relation,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "source_name": source_entity.get("name", ""),
                    "target_name": target_entity.get("name", ""),
                    "implicit": False,
                }
                relation_weight = max(0.0, min(1.5, float(relation.get("weight", 1.0))))
                propagated = state.score * 0.52 * relation_weight / next_depth
                provenance = _relation_provenance(relation)
                edge: dict[str, Any] = {
                    "id": relation_id,
                    "from": entity_id,
                    "to": neighbour_id,
                    "direction": direction,
                    "source": source_id,
                    "target": target_id,
                    "type": str(relation.get("relation_type") or "related_to"),
                    "weight": relation_weight,
                    "implicit": False,
                    "valid_from": str(relation.get("valid_from") or ""),
                    "valid_to": relation.get("valid_to"),
                    "created_at": str(relation.get("created_at") or ""),
                    "invalidated_at": relation.get("invalidated_at"),
                    "superseded_by": relation.get("superseded_by"),
                    "provenance": provenance,
                }
                knowledge_object_id = str(provenance.get("knowledge_object_id") or "")
                if knowledge_object_id:
                    edge["knowledge_object_id"] = knowledge_object_id
                offer_neighbour(
                    state=state,
                    neighbour_id=neighbour_id,
                    propagated=propagated,
                    edge=edge,
                    evidence_item={
                        "kind": "explicit_relation",
                        "from_entity_id": entity_id,
                        "relation_id": relation_id,
                        "relation_type": relation.get("relation_type"),
                        "depth": next_depth,
                    },
                    grounds=True,
                )

            if stop_traversal:
                break

            if normalized_as_of or normalized_known_at:
                # Entity links have neither valid-time nor append-only history.
                # Their present-day co-occurrence cannot answer a historical
                # valid-time OR transaction-time question.
                continue

            # Accepted links to one Knowledge Object provide useful graph structure without
            # asserting a semantic relation that the user never confirmed.
            for knowledge_item in linked_knowledge[: max(20, min(120, knowledge_limit * 2))]:
                knowledge_id = str(knowledge_item["id"])
                normalized_links: dict[str, dict[str, Any]] = {}
                for link in get_knowledge_links(knowledge_id):
                    linked_entity_id = canonical_entity_id(str(link["entity_id"]))
                    if not linked_entity_id:
                        continue
                    current_link = normalized_links.get(linked_entity_id)
                    link_rank = (float(link.get("confidence", 1.0) or 1.0), str(link.get("id") or ""))
                    current_rank = (
                        (
                            float(current_link.get("confidence", 1.0) or 1.0),
                            str(current_link.get("id") or ""),
                        )
                        if current_link
                        else (-1.0, "")
                    )
                    if link_rank > current_rank:
                        normalized_links[linked_entity_id] = link
                source_link = normalized_links.get(entity_id)
                if not source_link:
                    continue
                source_confidence = max(
                    0.0,
                    min(1.0, float(source_link.get("confidence", 1.0) or 1.0)),
                )
                for neighbour_id, link in sorted(normalized_links.items()):
                    if neighbour_id == entity_id:
                        continue
                    target_confidence = max(
                        0.0,
                        min(1.0, float(link.get("confidence", 1.0) or 1.0)),
                    )
                    pair = sorted((entity_id, neighbour_id))
                    relation_id = f"co:{knowledge_id}:{pair[0]}:{pair[1]}"
                    if relation_id not in relations and len(relations) >= _MAX_CONTEXT_GRAPH_RELATIONS:
                        traversal_truncated = True
                        stop_traversal = True
                        break
                    pair_source = get_entity(pair[0]) or {}
                    pair_target = get_entity(pair[1]) or {}
                    link_ids = sorted(
                        {
                            str(source_link.get("id") or ""),
                            str(link.get("id") or ""),
                        }
                        - {""}
                    )
                    weight = round(source_confidence * target_confidence, 6)
                    relations[relation_id] = {
                        "id": relation_id,
                        "user_id": user_id,
                        "source_entity_id": pair[0],
                        "target_entity_id": pair[1],
                        "source_name": pair_source.get("name", ""),
                        "target_name": pair_target.get("name", ""),
                        "relation_type": "co_occurs_in",
                        "weight": weight,
                        "implicit": True,
                        "knowledge_object_id": knowledge_id,
                        "knowledge_title": knowledge_item.get("title", ""),
                        "link_ids": link_ids,
                    }
                    propagated = (
                        state.score * 0.42 * (source_confidence * target_confidence) ** 0.5 / next_depth
                    )
                    edge = {
                        "id": relation_id,
                        "from": entity_id,
                        "to": neighbour_id,
                        "direction": "forward" if entity_id == pair[0] else "reverse",
                        "source": pair[0],
                        "target": pair[1],
                        "type": "co_occurs_in",
                        "weight": weight,
                        "implicit": True,
                        "valid_from": "",
                        "valid_to": None,
                        "created_at": max(
                            str(source_link.get("created_at") or ""),
                            str(link.get("created_at") or ""),
                        ),
                        "invalidated_at": None,
                        "superseded_by": None,
                        "provenance": {
                            "origin": "implicit_cooccurrence",
                            "source": "accepted_knowledge_links",
                            "knowledge_object_id": knowledge_id,
                        },
                        "knowledge_object_id": knowledge_id,
                        "link_ids": link_ids,
                    }
                    offer_neighbour(
                        state=state,
                        neighbour_id=neighbour_id,
                        propagated=propagated,
                        edge=edge,
                        evidence_item={
                            "kind": "shared_knowledge_object",
                            "from_entity_id": entity_id,
                            "knowledge_object_id": knowledge_id,
                            "knowledge_title": knowledge_item.get("title", ""),
                            "depth": next_depth,
                        },
                    )
                if stop_traversal:
                    break
            if stop_traversal:
                break

        path_states = sorted(
            (state for state in best_states.values() if state.edges),
            key=lambda state: (
                -state.score,
                len(state.edges),
                state.root,
                state.current,
                tuple(str(edge["id"]) for edge in state.edges),
            ),
        )
        published_states = path_states[:_MAX_PUBLISHED_GRAPH_PATHS]
        paths = [
            {
                "path_id": _graph_path_id(state),
                "root": state.root,
                "target": state.current,
                "score": round(state.score, 6),
                "entity_ids": list(state.entity_ids),
                "entities": [
                    {
                        "id": entity_id,
                        "name": str((entities.get(entity_id) or {}).get("name") or ""),
                        "entity_type": str((entities.get(entity_id) or {}).get("entity_type") or "other"),
                    }
                    for entity_id in state.entity_ids
                ],
                "edges": [dict(edge) for edge in state.edges],
            }
            for state in published_states
        ]
        published_path_by_target = {str(path["target"]): str(path["path_id"]) for path in paths}

        knowledge_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # Per-document contributions, one slot per DISTINCT entity: the strongest
        # link an entity offers a document. Collected first, combined below.
        contributions: dict[str, dict[str, float]] = defaultdict(dict)
        best_by_document: dict[str, tuple[float, str, dict[str, Any], dict[str, Any]]] = {}
        for entity_id, entity in entities.items():
            entity_score = float(entity.get("_graph_score", 0.0))
            for item in get_entity_knowledge(entity_id):
                document_id = str(item["id"])
                link_confidence = float(item.get("_link_confidence", 1.0) or 1.0)
                candidate_score = entity_score * max(0.0, min(1.0, link_confidence))
                knowledge_evidence[document_id].append(
                    {
                        "entity_id": entity_id,
                        "entity_name": entity.get("name", ""),
                        "link_confidence": link_confidence,
                        "entity_score": round(entity_score, 6),
                    }
                )
                if entity_id in query_matched_ids and candidate_score > contributions[document_id].get(
                    entity_id, 0.0
                ):
                    contributions[document_id][entity_id] = candidate_score
                best = best_by_document.get(document_id)
                if best is None or candidate_score > best[0]:
                    best_by_document[document_id] = (candidate_score, entity_id, item, entity)

        # Max-over-entities alone cannot rank: measured on a real corpus, 83% of
        # candidate scores collapsed to one of two values (0.677 / 0.276), and a
        # document sharing 16 entities with the query scored exactly the same as
        # one sharing a single hub. Every additional QUERY-MATCHED entity is
        # independent corroboration, so those fold in noisy-or fashion on top of
        # the best one — damped, because entities linked to one document co-occur
        # rather than testify independently, and the same number also feeds the
        # evidence gate in retrieval, where inflation would readmit noise.
        #
        # Corroboration from traversal-discovered entities was written first and
        # removed: a document linking A + three others is itself the edge by which
        # those three are reached from a query for A, so it corroborated ITSELF and
        # the score rose with entity count rather than with agreement. A test on a
        # one-entity query caught it.
        knowledge: dict[str, dict[str, Any]] = {}
        for document_id, (strongest, best_entity_id, item, entity) in best_by_document.items():
            remainder = 1.0
            for contributor_id, score in contributions[document_id].items():
                if contributor_id != best_entity_id:
                    remainder *= 1.0 - _GRAPH_CORROBORATION_DAMPING * max(0.0, min(1.0, score))
            combined = 1.0 - (1.0 - strongest) * remainder
            knowledge[document_id] = {
                **item,
                "_graph_score": round(combined, 6),
                "_graph_entity_id": best_entity_id,
                "_graph_entity_name": entity.get("name", ""),
                "_graph_depth": entity.get("_graph_depth", 0),
            }

        ordered_knowledge = sorted(
            knowledge.values(),
            key=lambda item: (
                -float(item.get("_graph_score", 0.0)),
                -float(item.get("quality_score", 0.5)),
                -float(item.get("importance", 0.5)),
            ),
        )[: max(1, min(knowledge_limit, 500))]
        knowledge_candidates: list[dict[str, Any]] = []
        grounded_ids = {entity_id for entity_id, state in best_states.items() if state.query_grounded}
        for item in ordered_knowledge:
            document_evidence = [dict(entry) for entry in knowledge_evidence[str(item["id"])]]
            # The scoring loop above handles many documents.  Never reuse its last
            # local ``best_entity_id`` here: each candidate must point only at the
            # route that earned this particular document's published score.
            best_entity_id = str(item.get("_graph_entity_id") or "")
            best_published_path = published_path_by_target.get(best_entity_id, "")
            if best_published_path:
                best_evidence = next(
                    (
                        entry
                        for entry in document_evidence
                        if str(entry.get("entity_id") or "") == best_entity_id
                    ),
                    None,
                )
                if best_evidence is not None:
                    best_evidence["path_id"] = best_published_path
            candidate: dict[str, Any] = {
                "knowledge_object_id": item["id"],
                "score": item.get("_graph_score", 0.0),
                "evidence": document_evidence,
                # The evidence gate must follow the SAME entity-state that earned
                # this candidate's score. Letting any weaker linked entity set the
                # flag mixed an ungrounded seed-root score with an unrelated lower
                # grounded route at document aggregation time.
                "query_matched": best_entity_id in grounded_ids,
            }
            if best_published_path:
                candidate["path_id"] = best_published_path
            knowledge_candidates.append(candidate)
        root_ids = set(root_scores)
        node_items = sorted(
            entities.values(),
            key=lambda item: (
                -float(item.get("_graph_score", 0.0)),
                int(item.get("_graph_depth", 0)),
                str(item.get("name", "")).casefold(),
            ),
        )
        root_items = [entity for entity in node_items if str(entity["id"]) in root_ids]
        relation_items = sorted(
            relations.values(),
            key=lambda item: (
                bool(item.get("implicit")),
                -float(item.get("weight", 1.0) or 1.0),
                str(item.get("relation_type", "")),
            ),
        )
        # Relation revision reads are historical, but entity names and merge
        # tombstones above are intentionally current. Recheck only after every
        # such lookup so a merge committed mid-build cannot escape as a mixed
        # identity snapshot.
        snapshot_metadata = confirm_history_snapshot()
        return {
            "roots": root_items,
            "entities": node_items,
            "nodes": node_items,
            "relations": relation_items,
            "knowledge": ordered_knowledge,
            "knowledge_candidates": knowledge_candidates,
            "paths": paths,
            "paths_matched_at_least": len(path_states),
            "paths_truncated": traversal_truncated or len(path_states) > _MAX_PUBLISHED_GRAPH_PATHS,
            "temporal_basis": temporal_basis,
            "as_of": normalized_as_of,
            **snapshot_metadata,
        }

    def update_entity(self, user_id: str, entity_id: str, **fields: Any) -> dict[str, Any] | None:
        current = self.storage.get_entity(entity_id, user_id)
        if not current or current.get("deleted_at"):
            return None
        aliases = fields.get("aliases", fields.get("aliases_json", _json_list(current.get("aliases_json"))))
        metadata = fields.get(
            "metadata", fields.get("metadata_json", _json_dict(current.get("metadata_json")))
        )
        entity_type = fields.get("entity_type", current.get("entity_type", EntityType.OTHER.value))
        entity = Entity(
            id=current["id"],
            user_id=user_id,
            name=fields.get("name", current["name"]),
            entity_type=EntityType(entity_type),
            aliases_json=_json_list(aliases),
            description=fields.get("description", current.get("description", "")),
            metadata_json=_json_dict(metadata),
            canonical=bool(current.get("canonical", 1)),
            merged_into_id=current.get("merged_into_id"),
            version=int(current.get("version", 1)),
            created_at=str(current.get("created_at") or utc_now()),
            updated_at=str(current.get("updated_at") or utc_now()),
            deleted_at=current.get("deleted_at"),
        )
        self.storage.update_entity(entity)
        return self.storage.get_entity(entity_id, user_id)

    def delete_entity(self, user_id: str, entity_id: str) -> bool:
        return self.storage.soft_delete_entity(entity_id, user_id)

    def restore_entity_version(
        self, user_id: str, entity_id: str, version: int, *, reviewed_by: str | None = None
    ) -> dict[str, Any] | None:
        return self.storage.restore_entity_version(entity_id, user_id, version, reviewed_by=reviewed_by)

    def create_relation(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        relation_type: RelationType = RelationType.RELATED_TO,
        *,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
        origin: str = "manual",
        valid_from: str = "",
    ) -> Relation:
        # Every edge carries a mandatory origin stamp; the parameter wins over
        # caller-supplied metadata so an API body cannot spoof provenance.
        relation = Relation(
            id=new_id("rel"),
            user_id=user_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type=relation_type,
            weight=weight,
            metadata_json={**(metadata or {}), "origin": str(origin)[:40]},
            valid_from=valid_from,
        )
        return self.storage.create_relation(relation)

    def relation_valid_from(self, user_id: str, knowledge_object_id: str) -> str:
        """С какой даты связь ПОДТВЕРЖДЕНА документом, который её объявил.

        Это не «началось тогда», а «на эту дату уже было правдой». Разница
        существенная и названа здесь вслух, потому что от неё зависит смысл
        вопроса «как было в 2024»: рапорт от 15.03.2024 о том, что Иванов служит
        в в/ч 30926, не утверждает, что раньше он там не служил, — он утверждает,
        что на 15 марта служил.

        Поэтому `as_of` читается как «что мы знали на эту дату», и связь с более
        поздним документом в такой ответ не попадает — честно, потому что на ту
        дату подтверждения не было.

        Берётся СОБСТВЕННАЯ дата документа (`metadata_json.document_date`,
        извлечённая из бумаги), а не дата загрузки: архив загружен разом, и
        `created_at` у полутора тысяч документов почти одинаков — он говорит о
        дне импорта, а не о том, когда это было правдой. На живом архиве своя
        дата известна у 1349 документов из 1536.
        """

        if not knowledge_object_id:
            return ""
        knowledge = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not knowledge:
            return ""
        metadata = knowledge.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata or "{}")
            except (ValueError, TypeError):
                metadata = {}
        if not isinstance(metadata, dict):
            return ""
        return str(metadata.get("document_date") or "")

    # ------------------------------------------------------------------
    # Containers: user-curated project/collection entities organizing
    # knowledge inside the graph itself (spec: "Knowledge Graph is central").
    # Membership reuses knowledge_entity_links; hierarchy reuses PART_OF.
    # ------------------------------------------------------------------

    def list_containers(
        self,
        user_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Container entities with member counts and PART_OF parent links.

        Returns a flat list; each row carries ``knowledge_count`` and
        ``parent_id`` (the strongest active PART_OF edge to another
        container, or None for roots) so callers can render a tree.
        """
        bounded = max(1, min(int(limit), 200))
        raw_containers = _bounded_entity_listing_rows(
            self.storage,
            user_id,
            entity_types=tuple(sorted(CONTAINER_ENTITY_TYPES)),
            limit=bounded + 1,
        )
        matched_at_least = len(raw_containers)
        containers = raw_containers[:bounded]
        container_ids = {str(row["id"]) for row in containers}
        parent_by_child: dict[str, str] = {}
        if container_ids:
            placeholders = ",".join("?" * len(container_ids))
            container_types = tuple(sorted(CONTAINER_ENTITY_TYPES))
            type_placeholders = ",".join("?" * len(container_types))
            parent_rows = self.storage.execute(
                f"""WITH ranked AS (
                         SELECT r.source_entity_id, r.target_entity_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY r.source_entity_id
                                    ORDER BY r.weight DESC, r.id
                                ) AS parent_rank
                           FROM relations r
                           JOIN entities parent
                             ON parent.id=r.target_entity_id AND parent.user_id=r.user_id
                          WHERE r.user_id=? AND r.relation_type=?
                            AND r.deleted_at IS NULL AND r.valid_to IS NULL
                            AND r.source_entity_id IN ({placeholders})
                            AND parent.deleted_at IS NULL AND parent.canonical=1
                            AND parent.merged_into_id IS NULL
                            AND {_not_private_entity_material_dependency("parent")}
                            AND {_not_private_relation_dependency("r")}
                            AND parent.entity_type IN ({type_placeholders})
                     )
                     SELECT source_entity_id, target_entity_id
                       FROM ranked WHERE parent_rank=1""",  # nosec B608
                (
                    user_id,
                    RelationType.PART_OF.value,
                    *sorted(container_ids),
                    *container_types,
                ),
            ).fetchall()
            parent_by_child = {
                str(row["source_entity_id"]): str(row["target_entity_id"]) for row in parent_rows
            }
        knowledge_counts = self.storage._knowledge_counts_for(  # noqa: SLF001
            user_id,
            sorted(container_ids),
        )
        for row in containers:
            row["parent_id"] = parent_by_child.get(str(row["id"]))
            row["knowledge_count"] = knowledge_counts.get(str(row["id"]), 0)
        return _BoundedEntityList(
            containers,
            matched_at_least=matched_at_least,
            truncated=len(raw_containers) > bounded,
        )

    def create_container(
        self,
        user_id: str,
        name: str,
        *,
        kind: str = EntityType.COLLECTION.value,
        parent_id: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create (or return the existing) container entity, optionally under a parent.

        An explicit user action creates the PART_OF edge directly — the review
        gate applies to system suggestions, not to the user's own decisions.
        """
        if kind not in CONTAINER_ENTITY_TYPES:
            allowed = ", ".join(sorted(CONTAINER_ENTITY_TYPES))
            raise ValueError(f"Container kind must be one of: {allowed}")
        entity = self.create_entity(
            user_id,
            name,
            EntityType(kind),
            description=description,
            metadata={"container": True, "origin": "user"},
        )
        entity_id = str(entity.get("id") or "")
        if parent_id:
            if parent_id == entity_id:
                raise ValueError("Container cannot be part of itself")
            parent = self.storage.get_entity(parent_id, user_id)
            if not parent or parent.get("deleted_at"):
                raise ValueError("Parent container not found")
            if str(parent.get("entity_type")) not in CONTAINER_ENTITY_TYPES:
                raise ValueError("Parent must be a project or collection entity")
            self.create_relation(
                user_id,
                entity_id,
                parent_id,
                RelationType.PART_OF,
                weight=1.0,
                origin="container",
            )
        return self.storage.get_entity(entity_id, user_id) or entity

    def suggest_relations_for_knowledge(
        self,
        user_id: str,
        knowledge_object_id: str,
    ) -> list[dict[str, Any]]:
        """Extract explicit, review-only relations from one Knowledge Object.

        Co-occurrence alone is never enough. Both entity mentions and an explicit
        relation phrase must occur in the same local span.

        «Local span» is a PARAGRAPH, and that was measured rather than chosen. On
        400 real documents from the owner's archive, with the extractor's own entity
        candidates standing in for links (median 8 per document):

            окно  вхождения  связей  документов
             160  первое          2           1     <- как было
             400  первое         25           7
             400  все            26           8     <- стало
            1000  все            58          20

        The 160-character window — not «first occurrence only» — was what made this
        return nothing: widening it alone multiplies the yield by twelve. A relation
        phrase appears at all in 141 of those 400 documents, so the vocabulary is
        not the problem either.

        1000 characters would double the yield again and is deliberately NOT taken:
        that is a page, not a span, and two entities a page apart with «использует»
        somewhere between them is not evidence of anything. Every suggestion costs a
        human decision, and this project already has 1605 of those waiting.

        Every occurrence of a name counts, not just the first: which mention happens
        to come first is an accident of how the document is written.
        """

        knowledge = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not knowledge or knowledge.get("deleted_at"):
            return []
        text = str(knowledge.get("content") or knowledge.get("summary") or "")
        links = self.storage.list_knowledge_entity_links(
            user_id,
            knowledge_object_id=knowledge_object_id,
            status="accepted",
            limit=100,
        )
        mentions: list[tuple[int, int, dict[str, Any]]] = []
        for link in links:
            name = str(link.get("entity_name") or "").strip()
            if not name:
                continue
            pattern = re.compile(re.escape(name), re.I)
            for match in pattern.finditer(text):
                mentions.append((match.start(), match.end(), link))
        mentions.sort(key=lambda item: item[0])

        suggestions: list[dict[str, Any]] = []
        for index, left in enumerate(mentions):
            for right in mentions[index + 1 :]:
                if right[0] - left[1] > _RELATION_SPAN_CHARS:
                    break
                if left[2]["entity_id"] == right[2]["entity_id"]:
                    # Одна и та же сущность, упомянутая дважды. Стало возможным ровно
                    # тогда, когда я разрешил считать ВСЕ вхождения имени: при одном
                    # вхождении пара из двух упоминаний всегда была двумя разными
                    # сущностями. «Атлас … использует … Атлас» — не связь, а
                    # предложение про один объект, и хранилище справедливо отвечает
                    # `Self-relation candidates are not allowed`, роняя весь разбор
                    # документа пятисоткой. Найдено на массовом продвижении: одно
                    # падение на сотню документов.
                    continue
                between = text[left[1] : right[0]]
                for phrase, relation_type, base_confidence, phrase_reversed in _RELATION_PHRASES:
                    phrase_match = phrase.search(between)
                    if not phrase_match:
                        continue
                    if relation_type is RelationType.FAMILY_OF and (
                        len(between) - phrase_match.end() > _FAMILY_WORD_TO_NAME_CHARS
                    ):
                        # Родственное слово должно стоять НЕПОСРЕДСТВЕННО перед
                        # именем: «Отец Горбунов Иван Алексеевич».
                        #
                        # Замерено на архиве владельца: документы — анкеты, где
                        # эти слова чаще всего стоят в ЗАГОЛОВКЕ поля («22.
                        # Родители (ФИО, дата рождения, где проживает…)»), и
                        # такой заголовок попадает между людьми из РАЗНЫХ анкет,
                        # сцепляя чужих друг другу людей. Отдельная ловушка —
                        # позывные: в списке личного состава «Рядовой Нечипоренко
                        # Алексей Юрьевич (АВ-689922) Внук» слово «Внук» это
                        # позывной, а не родство.
                        #
                        # Требование близости отсекает и то, и другое: между
                        # заголовком поля и следующей фамилией всегда стоит
                        # перечисление, а между позывным и следующим бойцом —
                        # звание и номер.
                        continue
                    if relation_type is RelationType.FAMILY_OF and not (
                        str(left[2].get("entity_type") or "") == "person"
                        and str(right[2].get("entity_type") or "") == "person"
                    ):
                        # Родня бывает только у людей.
                        #
                        # Замерено на архиве владельца 2026-08-03: без этой
                        # проверки проход дал 509 кандидатов, и в выборке из
                        # двадцати восемь оказались мусором вида «Изобильный ->
                        # Москва | слово: Брат» и «Курган -> Челябинск | Мать» —
                        # два города в родстве.
                        #
                        # Причина не в словаре, а в документах: это анкеты, где
                        # родственные слова стоят в ЗАГОЛОВКЕ поля («23. Брат,
                        # сестра, близкие (ФИО, дата рождения, где проживает…)»),
                        # а не в утверждении о паре. Заголовок бланка попадает
                        # между любыми двумя именами, включая названия городов.
                        #
                        # Тип сущности — улика структурная и потому надёжнее
                        # любого уточнения словаря: город не может быть ничьим
                        # братом, как бы ни был написан документ.
                        continue
                    confidence = base_confidence
                    span = text[max(0, left[0] - 30) : min(len(text), right[1] + 30)]
                    # `reversed` swaps which mention becomes source/target: "X
                    # подчиняется Y" has the subordinate (X) mentioned first in
                    # text, but the relation MANAGES is stored manager-first —
                    # so the SECOND mention (Y) is the source here, not the first.
                    source, target = (right, left) if phrase_reversed else (left, right)
                    candidate = self.storage.store_relation_candidate(
                        user_id,
                        str(source[2]["entity_id"]),
                        str(target[2]["entity_id"]),
                        relation_type.value,
                        confidence=confidence,
                        evidence={
                            "knowledge_object_id": knowledge_object_id,
                            "source_name": source[2].get("entity_name"),
                            "target_name": target[2].get("entity_name"),
                            # Found by adversarial review: `match` used to be the
                            # leftover loop variable from the EARLIER mention-collection
                            # loop above (`for match in pattern.finditer(text)`) — it was
                            # never bound to the relation-phrase match itself, so this
                            # showed the reviewer a stray entity name instead of the verb
                            # that actually justified the relation.
                            "phrase": phrase_match.group(0),
                            "excerpt": span[:500],
                            "method": "explicit_local_relation_phrase",
                        },
                    )
                    suggestions.append(candidate)
                    break
        unique = {str(item.get("id") or ""): item for item in suggestions if item.get("id")}
        return sorted(unique.values(), key=lambda item: float(item.get("confidence", 0.0)), reverse=True)

    async def suggest_relations_from_structure(
        self,
        user_id: str,
        knowledge_object_id: str,
        *,
        llm: Any,
        max_tokens: int = 1200,
        store: bool = True,
    ) -> dict[str, Any]:
        """Связи, объявленные ФОРМОЙ документа, — арбитром, а не фразой.

        Второй извлекатель рядом с `suggest_relations_for_knowledge`, а не
        замена ему: фразовый детерминирован и работает без модели, а этот
        читает то, чего в фразе нет вовсе, — поле анкеты, строку ведомости,
        адресата рапорта. Оба кладут кандидатов в одну очередь на review.
        См. `friday/knowledge_graph/_structure.py`.
        """

        from friday.knowledge_graph._structure import suggest_relations_from_structure

        return await suggest_relations_from_structure(
            self.storage,
            user_id,
            knowledge_object_id,
            llm=llm,
            max_tokens=max_tokens,
            store=store,
        )

    async def review_relation_candidates(
        self,
        user_id: str,
        *,
        llm: Any,
        limit: int = 0,
        apply: bool = False,
        reviewed_by: str = "arbiter",
        votes: int = 2,
        on_verdict: Any = None,
    ) -> dict[str, Any]:
        """Сверить очередь предложенных связей с документами-основаниями.

        Обратная сторона `suggest_relations_from_structure`: тот предлагает,
        этот спрашивает у документа, объявляет ли он предложенное. Умеет только
        подтвердить, отвергнуть или воздержаться — воздержание оставляет
        кандидата человеку. См. `friday/knowledge_graph/_review.py`.
        """

        from friday.knowledge_graph._review import review_relation_candidates

        return await review_relation_candidates(
            self.storage,
            user_id,
            llm=llm,
            limit=limit,
            apply=apply,
            reviewed_by=reviewed_by,
            votes=votes,
            on_verdict=on_verdict,
        )

    def invalidate_relation(
        self,
        user_id: str,
        relation_id: str,
        *,
        valid_to: str = "",
        superseded_by: str = "",
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Связь перестала быть верной — это не то же самое, что её не было.

        Мягкое удаление говорит «этого не было», отмена — «это было и кончилось».
        Второе первым не выразить, а именно оно нужно архиву: рапорт 2024 года о
        службе в в/ч 30926 остаётся фактом о 2024-м после перевода человека.
        """

        return self.storage.invalidate_relation(
            user_id,
            relation_id,
            valid_to=valid_to,
            superseded_by=superseded_by,
            reason=reason,
        )

    def review_relation_candidate(
        self,
        user_id: str,
        candidate_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        return self.storage.review_relation_candidate(
            user_id,
            candidate_id,
            status,
            reviewed_by=reviewed_by,
        )

    def detect_conflicts_for_knowledge(
        self,
        user_id: str,
        knowledge_object_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Propose potential contradictions without changing either claim."""

        current = self.storage.get_knowledge_object(knowledge_object_id, user_id)
        if not current or current.get("deleted_at"):
            return []
        current_claims = _normalized_claims(str(current.get("content") or ""))
        if not current_claims:
            return []

        linked_entities = self.storage.list_knowledge_entity_links(
            user_id,
            knowledge_object_id=knowledge_object_id,
            status="accepted",
            limit=100,
        )
        candidate_ids: set[str] = set()
        for link in linked_entities:
            for item in self.storage.get_entity_knowledge(
                user_id,
                str(link["entity_id"]),
                limit=250,
            ):
                candidate_id = str(item.get("id") or "")
                if candidate_id and candidate_id != knowledge_object_id:
                    candidate_ids.add(candidate_id)
        if not candidate_ids:
            # Bounded fallback for exact identifiers or properties that were
            # not linked by legacy ingestion.
            candidate_ids.update(
                str(item["id"])
                for item in self.storage.list_knowledge_objects(user_id, limit=300)
                if item.get("id") != knowledge_object_id
            )

        output: list[dict[str, Any]] = []
        for other_id in list(candidate_ids)[:500]:
            other = self.storage.get_knowledge_object(other_id, user_id)
            if not other or other.get("deleted_at"):
                continue
            other_claims = _normalized_claims(str(other.get("content") or ""))
            for left in current_claims:
                for right in other_claims:
                    if left["predicate"] != right["predicate"]:
                        continue
                    if left["subject_key"] != right["subject_key"]:
                        continue
                    if left["value_key"] == right["value_key"]:
                        continue
                    confidence = 0.92 if left["predicate"] in {"address", "quoted_value"} else 0.82
                    conflict = self.storage.store_knowledge_conflict(
                        user_id,
                        knowledge_object_id,
                        other_id,
                        conflict_type=f"{left['predicate']}_mismatch",
                        confidence=confidence,
                        evidence={
                            "subject": left["subject"],
                            "predicate": left["predicate"],
                            "new_value": left["value"],
                            "existing_value": right["value"],
                            "new_evidence": left["evidence"],
                            "existing_evidence": right["evidence"],
                            "method": "same_subject_predicate_different_value",
                        },
                    )
                    output.append(conflict)
                    if len(output) >= max(1, min(limit, 100)):
                        return output
        return output

    def review_conflict(
        self,
        user_id: str,
        conflict_id: str,
        status: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        return self.storage.review_knowledge_conflict(
            user_id,
            conflict_id,
            status,
            reviewed_by=reviewed_by,
            resolution_note=resolution_note,
        )

    def resolve_conflict(
        self,
        user_id: str,
        conflict_id: str,
        winner_id: str,
        *,
        reviewed_by: str,
        resolution_note: str = "",
    ) -> dict[str, Any] | None:
        return self.storage.resolve_conflict(
            user_id,
            conflict_id,
            winner_id,
            reviewed_by=reviewed_by,
            resolution_note=resolution_note,
        )

    def get_entity_relations(
        self,
        entity_id: str,
        user_id: str,
        *,
        as_of: str = "",
        known_at: str = "",
        limit: int = _MAX_PUBLIC_ENTITY_RELATIONS,
    ) -> list[dict[str, Any]]:
        """Связи узла по valid-time и, при запросе, transaction-time.

        Обёртка обязана пропускать дату дальше: без этого «как было тогда»
        работало бы через обход графа и не работало через прямой вызов, а
        разница между двумя дорогами к одному факту — это ровно тот случай,
        когда ворота стоят на одной из них.
        """

        cleaned_as_of = str(as_of or "").strip()
        normalized_as_of = normalize_event_date(cleaned_as_of)[0] if cleaned_as_of else ""
        requested_known_at = str(known_at or "").strip()
        normalized_known_at = ""
        history_status: dict[str, Any] | None = None
        history_watermark: int | None = None
        if requested_known_at:
            # Normalize, floor-check and validate identity history before the
            # first entity/current-projection read.  A missing entity must not
            # hide an invalid or incomplete transaction snapshot as ordinary [].
            normalized_known_at = normalize_known_at(requested_known_at)
            history_status = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=requested_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
            history_watermark = _relation_revision_watermark(
                self.storage,
                user_id,
                normalized_known_at,
            )
        entity = _bounded_entity_by_id(self.storage, entity_id, user_id)
        if not _is_live_graph_entity(entity):
            if history_status:
                # The missing-entity decision used current identity topology too;
                # catch a merge racing that read just like non-empty snapshots do.
                confirmed = _validated_history_snapshot_status(
                    self.storage.relation_history_status(
                        user_id,
                        known_at=normalized_known_at,
                    ),
                    requested_known_at=normalized_known_at,
                )
                if confirmed != history_status:
                    raise RelationHistorySnapshotError(
                        "relation history status changed while checking the graph entity"
                    )
                if (
                    _relation_revision_watermark(self.storage, user_id, normalized_known_at)
                    != history_watermark
                ):
                    raise RelationHistorySnapshotError(
                        "relation history changed while checking the graph entity"
                    )
            return _BoundedRelationList([], matched_at_least=0, truncated=False)
        rows, matched_at_least, truncated = _bounded_entity_relation_rows(
            self.storage,
            entity_id,
            user_id,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
            history_status=history_status,
            limit=limit,
        )
        if history_status:
            if _relation_revision_watermark(self.storage, user_id, normalized_known_at) != history_watermark:
                raise RelationHistorySnapshotError(
                    "relation history changed while publishing entity relations"
                )
            confirmed = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=normalized_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while publishing entity relations"
                )
        return _BoundedRelationList(
            [_public_relation(row) for row in rows],
            matched_at_least=matched_at_least,
            truncated=truncated,
        )

    def count_pending_relations(self, entity_id: str, user_id: str) -> int:
        if not _bounded_entity_by_id(self.storage, entity_id, user_id):
            return 0
        return self.storage.count_relation_candidates_for_entity(user_id, entity_id)

    def get_entity_graph(
        self,
        user_id: str,
        entity_id: str,
        depth: int = 2,
        *,
        as_of: str = "",
        known_at: str = "",
        entity_types: Any = (),
        relation_types: Any = (),
        min_weight: float = 0.0,
        min_confidence: float = 0.0,
        # Умолчание `False` — замер, а не осторожность. Этот метод читают ТРИ
        # дороги: агент (`entity_lookup`), публичный маршрут и админка. Соседство
        # в концентраторе уже признано не-уликой — штатное расписание на полсотни
        # имён делает «связанными» все пары этих людей, и именно этот канал
        # уполовинивал recall@10 (0.35 -> 0.15). Поэтому встречаемость включает
        # ровно та дорога, которой она нужна для РИСОВАНИЯ, и говорит об этом вслух.
        include_cooccurrence: bool = False,
    ) -> dict[str, Any]:
        cleaned_as_of = str(as_of or "").strip()
        normalized_as_of = normalize_event_date(cleaned_as_of)[0] if cleaned_as_of else ""
        requested_known_at = str(known_at or "").strip()
        normalized_known_at = normalize_known_at(requested_known_at) if requested_known_at else ""
        history_status: dict[str, Any] | None = None
        if normalized_known_at:
            history_status = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=requested_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
        raw_result = self.storage.get_entity_graph(
            user_id,
            entity_id,
            depth,
            as_of=normalized_as_of,
            known_at=normalized_known_at,
            entity_types=entity_types,
            relation_types=relation_types,
            min_weight=min_weight,
            min_confidence=min_confidence,
            include_cooccurrence=bool(include_cooccurrence),
        )
        if not isinstance(raw_result, Mapping):
            raise ValueError("entity graph result is not a mapping")
        if str(raw_result.get("as_of") or "") != normalized_as_of:
            raise ValueError("entity graph changed the normalized as_of boundary")
        expected_temporal_basis = "bitemporal" if history_status else "valid_time"
        if raw_result.get("temporal_basis") != expected_temporal_basis:
            raise RelationHistorySnapshotError("entity graph returned an inconsistent temporal basis")
        if history_status:
            result_status = _validated_history_snapshot_status(
                raw_result,
                requested_known_at=normalized_known_at,
            )
            if result_status != history_status:
                raise RelationHistorySnapshotError(
                    "entity graph returned a different relation-history status"
                )
        elif str(raw_result.get("known_at") or ""):
            raise RelationHistorySnapshotError(
                "current entity graph unexpectedly returned a historical boundary"
            )

        raw_nodes = raw_result.get("nodes")
        raw_edges = raw_result.get("edges")
        if not isinstance(raw_nodes, list) or not all(isinstance(node, Mapping) for node in raw_nodes):
            raise ValueError("entity graph nodes are not a list of mappings")
        if not isinstance(raw_edges, list) or not all(isinstance(edge, Mapping) for edge in raw_edges):
            raise ValueError("entity graph edges are not a list of mappings")

        nodes_by_id: dict[str, dict[str, Any]] = {}
        public_node_ids: set[str] = set()
        for raw_node in raw_nodes:
            raw_id = str(raw_node.get("id") or "")
            if not raw_id or raw_id in nodes_by_id:
                raise ValueError("entity graph contains a missing or duplicate node id")
            projected_node = _public_graph_node(raw_node)
            public_id = str(projected_node["id"])
            if not public_id or public_id in public_node_ids:
                raise ValueError("bounded entity graph node ids are not unique")
            public_node_ids.add(public_id)
            nodes_by_id[raw_id] = projected_node

        sortable_edges: list[tuple[dict[str, Any], str, str]] = []
        public_edge_ids: set[str] = set()
        for raw_edge in raw_edges:
            source_id = str(raw_edge.get("source_entity_id") or "")
            target_id = str(raw_edge.get("target_entity_id") or "")
            if source_id not in nodes_by_id or target_id not in nodes_by_id:
                raise ValueError("entity graph edge refers to an unpublished endpoint")
            projected = _public_relation(raw_edge)
            public_edge_id = str(projected.get("id") or "")
            if not public_edge_id or public_edge_id in public_edge_ids:
                raise ValueError("bounded entity graph edge ids are missing or not unique")
            public_edge_ids.add(public_edge_id)
            sortable_edges.append((projected, source_id, target_id))

        def edge_rank(item: tuple[dict[str, Any], str, str]) -> tuple[Any, ...]:
            # Подтверждённые связи идут ПЕРВЫМИ, и это не вкус. Вес у двух родов
            # рёбер меряется в разном: у связи это уверенность 0..1, у совместной
            # встречаемости — число общих документов. Сортируя их одним числом,
            # бюджет отдавался бы встречаемости целиком (3 общих документа больше
            # уверенности 0.9), и объявленные человеком связи вылетали бы из
            # картины первыми. Внутри каждого рода порядок прежний.
            return (
                0 if str(item[0].get("kind") or "relation") != "cooccurrence" else 1,
                -float(item[0].get("weight") or 0.0),
                str(item[0].get("relation_type") or "").casefold(),
                item[1],
                item[2],
                str(item[0].get("id") or ""),
            )

        sortable_edges.sort(key=edge_rank)
        root_id = str(raw_result.get("root") or entity_id)
        if nodes_by_id and root_id not in nodes_by_id:
            raise ValueError("entity graph root is absent from its node set")

        # A global weight slice can sever the only root→bridge edge and retain a
        # high-weight second-hop component. Grow a deterministic connected prefix
        # from the requested root instead: every published edge is reachable from
        # the root through edges which precede it in this same bounded response.
        adjacency: dict[str, list[int]] = defaultdict(list)
        for index, (_, source_id, target_id) in enumerate(sortable_edges):
            adjacency[source_id].append(index)
            adjacency[target_id].append(index)
        published_edge_items: list[tuple[dict[str, Any], str, str]] = []
        selected_node_ids = {root_id} if root_id in nodes_by_id else set()
        queued_edges: set[int] = set()
        edge_heap: list[tuple[tuple[Any, ...], int]] = []

        def queue_incident(node_id: str) -> None:
            for edge_index in adjacency.get(node_id, []):
                if edge_index in queued_edges:
                    continue
                queued_edges.add(edge_index)
                heapq.heappush(edge_heap, (edge_rank(sortable_edges[edge_index]), edge_index))

        if selected_node_ids:
            queue_incident(root_id)
        while edge_heap and len(published_edge_items) < _MAX_PUBLIC_ENTITY_GRAPH_EDGES:
            _, edge_index = heapq.heappop(edge_heap)
            item = sortable_edges[edge_index]
            _, source_id, target_id = item
            if source_id not in selected_node_ids and target_id not in selected_node_ids:
                continue
            published_edge_items.append(item)
            for endpoint_id in (source_id, target_id):
                if endpoint_id not in selected_node_ids:
                    selected_node_ids.add(endpoint_id)
                    queue_incident(endpoint_id)
        published_edges = [item[0] for item in published_edge_items]
        if len(selected_node_ids) > _MAX_PUBLIC_ENTITY_GRAPH_NODES:
            raise ValueError("bounded entity graph edge set exceeds its node budget")
        published_nodes = [nodes_by_id[item] for item in selected_node_ids if item in nodes_by_id]
        published_nodes.sort(
            key=lambda node: (
                str(node.get("id") or "") != root_id,
                str(node.get("name") or "").casefold(),
                str(node.get("id") or ""),
            )
        )

        nodes_matched = _bounded_graph_count(
            raw_result.get("nodes_matched_at_least"),
            len(raw_nodes),
        )
        edges_matched = _bounded_graph_count(
            raw_result.get("edges_matched_at_least"),
            len(raw_edges),
        )
        upstream_nodes_truncated = raw_result.get("nodes_truncated", False)
        upstream_edges_truncated = raw_result.get("edges_truncated", False)
        if not isinstance(upstream_nodes_truncated, bool) or not isinstance(upstream_edges_truncated, bool):
            raise ValueError("entity graph truncation metadata is not boolean")
        if upstream_nodes_truncated and nodes_matched <= len(raw_nodes):
            nodes_matched = min(len(raw_nodes) + 1, _MAX_PUBLIC_GRAPH_COUNT)
        if upstream_edges_truncated and edges_matched <= len(raw_edges):
            edges_matched = min(len(raw_edges) + 1, _MAX_PUBLIC_GRAPH_COUNT)
        result: dict[str, Any] = {
            "root": root_id[:160],
            "nodes": published_nodes,
            "edges": published_edges,
            "nodes_matched_at_least": nodes_matched,
            "nodes_truncated": upstream_nodes_truncated or nodes_matched > len(published_nodes),
            "edges_matched_at_least": edges_matched,
            "edges_truncated": upstream_edges_truncated or edges_matched > len(published_edges),
            "as_of": normalized_as_of,
            "known_at": normalized_known_at,
            "identity_basis": "current_names",
            "temporal_basis": expected_temporal_basis,
        }
        if history_status:
            confirmed = _validated_history_snapshot_status(
                self.storage.relation_history_status(
                    user_id,
                    known_at=normalized_known_at,
                ),
                requested_known_at=normalized_known_at,
            )
            if confirmed != history_status:
                raise RelationHistorySnapshotError(
                    "relation history status changed while publishing the entity graph"
                )
            result.update(confirmed)
        return result

    def find_relation_path(
        self,
        user_id: str,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 4,
    ) -> dict[str, Any]:
        """Кратчайшая цепочка ПОДТВЕРЖДЁННЫХ связей между двумя узлами.

        Это то, ради чего граф — граф, а не список соседей: «как связаны Иванов и
        проект Заря» нельзя ответить карточкой ни одного из них. В админке путь
        подсвечивается на картине с самого начала; в чате его не было, а чат по
        закону проекта (`sol/SOL.md` §1.6) — первый интерфейс.

        Встречаемость в цепочку НЕ входит. Соседство в концентраторе — замеренная
        не-улика: штатное расписание на полсотни имён связывает все пары этих
        людей, и путь через него означал бы «оба упомянуты в одном документе», а
        не «связаны». Молчание об отсутствии пути честнее выдуманной связи.

        Обход идёт по окрестности источника — то есть по тому же коду, который
        уже держит границу арендатора, мягкие удаления и слияния.
        """

        depth = max(1, min(int(max_depth), 5))
        neighbourhood = self.get_entity_graph(user_id, source_id, depth)
        nodes = {str(node["id"]): node for node in neighbourhood.get("nodes", [])}
        if source_id not in nodes or target_id not in nodes:
            return {"found": False, "path": [], "depth_searched": depth}
        adjacency: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        for edge in neighbourhood.get("edges", []):
            # Встречаемость сюда не попадает уже потому, что `include_cooccurrence`
            # по умолчанию выключен, — но проверка стоит и здесь: умолчание можно
            # сменить одной строкой, а цена ошибки тут не «лишнее ребро», а
            # выдуманная связь между людьми.
            if edge.get("kind") == "cooccurrence":
                continue
            left = str(edge.get("source_entity_id") or edge.get("source") or "")
            right = str(edge.get("target_entity_id") or edge.get("target") or "")
            if not left or not right:
                continue
            adjacency.setdefault(left, []).append((right, edge))
            adjacency.setdefault(right, []).append((left, edge))
        came_from: dict[str, tuple[str, Mapping[str, Any]] | None] = {source_id: None}
        queue: deque[str] = deque([source_id])
        while queue:
            current = queue.popleft()
            if current == target_id:
                break
            for neighbour, edge in adjacency.get(current, []):
                if neighbour in came_from:
                    continue
                came_from[neighbour] = (current, edge)
                queue.append(neighbour)
        if target_id not in came_from:
            return {"found": False, "path": [], "depth_searched": depth}
        steps: list[dict[str, Any]] = []
        cursor = target_id
        while came_from.get(cursor):
            previous, edge = came_from[cursor]  # type: ignore[misc]
            steps.append(
                {
                    "from": {"id": previous, "name": str(nodes.get(previous, {}).get("name") or "")},
                    "to": {"id": cursor, "name": str(nodes.get(cursor, {}).get("name") or "")},
                    "relation_type": str(edge.get("relation_type") or ""),
                    # Направление ребра — свойство утверждения, а не обхода: путь
                    # может идти против стрелки, и человеку это надо видеть.
                    "forward": str(edge.get("source_entity_id") or edge.get("source") or "") == previous,
                }
            )
            cursor = previous
        steps.reverse()
        return {"found": True, "path": steps, "depth_searched": depth}

    def link_knowledge_to_entity(
        self,
        ko_id: str,
        entity_id: str,
        user_id: str,
        *,
        confidence: float = 1.0,
        evidence: dict[str, Any] | None = None,
        status: str = "accepted",
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        return self.storage.link_knowledge_entity(
            user_id,
            ko_id,
            entity_id,
            confidence=confidence,
            evidence=evidence,
            status=status,
            reviewed_by=reviewed_by,
        )

    def get_entity_knowledge(
        self,
        entity_id: str,
        user_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not _bounded_entity_by_id(self.storage, entity_id, user_id):
            return []
        return self.storage.get_entity_knowledge(user_id, entity_id, limit=limit)

    def entity_profile(
        self,
        entity_id: str,
        user_id: str,
        *,
        knowledge_limit: int = 10,
        relation_limit: int = _MAX_PUBLIC_ENTITY_RELATIONS,
    ) -> dict[str, Any]:
        """Everything an object-view needs about one entity: confirmed relations,
        linked documents, and derived tags/date-range/pending-review count.

        Shared composition point — the agent's `entity_lookup` tool
        (`execution_kernel`) and the HTTP/Telegram `/profile` surface both call
        this, so they show exactly the same thing instead of two independently
        maintained versions of "what does this entity look like".

        `event_time` keeps THREE distinct temporal facts apart, per spec v3 §4
        ("distinguish when an event happened, when a source reported it, and
        when Friday learned it"): `occurred_at` (when the event itself took
        place, `entity_time`/`set_event_time` — only ever set for `event`
        entities) is a different fact from `profile.document_date_range`
        (when the SOURCE documents were dated) and from each document's own
        `created_at` (when Friday ingested it). Conflating any two of these
        is exactly the mistake `document_date` vs `updated_at` already
        guards against elsewhere in this method.
        """
        if not _bounded_entity_by_id(self.storage, entity_id, user_id):
            return {
                "relations": [],
                "pending_relations_count": 0,
                "knowledge_objects": [],
                "knowledge_objects_total": 0,
                "knowledge_objects_matched_at_least": 0,
                "knowledge_objects_truncated": False,
                "profile": {"tags": [], "document_date_range": None, "documents_without_own_date": 0},
                "event_time": None,
                "edits": {"versions": 0, "last_edited_at": None, "restorable_version": None},
            }
        # Карточка перечисляет документы, но не показывает их текст — поэтому
        # проекция без `content`: полный `k.*` давал замеренные 2.4–4.9 МБ на один
        # ответ, и та же тяжесть уходила модели через `entity_lookup`, где всё
        # равно обрезалась на 11 900 знаках.
        knowledge_objects = [
            _safe_knowledge_card(item)
            for item in self.storage.get_entity_knowledge_cards(
                user_id,
                entity_id,
                limit=knowledge_limit,
            )
        ]
        summary = self.storage.entity_knowledge_summary(user_id, entity_id)
        knowledge_total = int(summary.get("total") or 0)
        relations = self.get_entity_relations(entity_id, user_id, limit=relation_limit)
        # ПОРЯДОК КЛЮЧЕЙ ЗДЕСЬ — ЧАСТЬ КОНТРАКТА. Ответ инструмента агента режется
        # на 12 000 знаках (`ToolResult.to_llm_message`), а список документов —
        # самая длинная часть словаря. Пока сводка стояла ПОСЛЕ него, у трети
        # сущностей корпуса (замерено: 34%) модель не получала ни тегов, ни
        # диапазона дат, ни числа документов, ни пометки о производности — они
        # оставались за отсечкой. Факты о сущности идут первыми, список — последним.
        return {
            # Сводка НЕ выводится из показанного списка: список — страница
            # (`knowledge_limit`), а сводка посчитана по всем документам. Ровно это
            # и делало карточку неверной: диапазон дат десяти самых важных
            # документов подавался как диапазон сущности — замерено неверным у 93
            # из 200 самых широких сущностей боевой копии.
            "profile": summary,
            "knowledge_objects_total": knowledge_total,
            "knowledge_objects_matched_at_least": knowledge_total,
            "knowledge_objects_truncated": knowledge_total > len(knowledge_objects),
            # Спека v3 §2: «derived properties identify their source objects,
            # calculation version and freshness; a derived value is never
            # presented as a sourced fact». Теги, диапазон дат и число «без своей
            # даты» НЕ записаны на объекте — они вычислены из его документов прямо
            # сейчас. Без пометки человек (и модель) читает их как свойства
            # объекта: «у Иванова теги такие-то», хотя правильно — «в его
            # документах встречаются такие-то».
            "profile_provenance": {
                "derived": True,
                "derived_from": "linked knowledge objects",
                "source_count": knowledge_total,
                "computed_at": utc_now(),
                "calculation": "entity_knowledge_summary/1",
            },
            "relations": relations,
            "relations_matched_at_least": getattr(
                relations,
                "matched_at_least",
                len(relations),
            ),
            "relations_truncated": bool(getattr(relations, "truncated", False)),
            "pending_relations_count": self.count_pending_relations(entity_id, user_id),
            "event_time": _safe_event_time(self.get_event_time(user_id, entity_id)),
            # Четвёртый временной факт, теперь и для сущности: КОГДА ЕЁ ПРАВИЛИ —
            # отдельно от дат документов и от времени события (спека v3 §2).
            # `restorable_version` — та версия, к которой ведёт откат «отменить
            # последнюю правку»: предпоследняя, потому что последняя и есть
            # текущее состояние.
            "edits": self._entity_edit_history(user_id, entity_id),
            # Список идёт ПОСЛЕДНИМ: если ответ и обрежется, потеряется он, а не
            # факты о сущности.
            "knowledge_objects": knowledge_objects,
        }

    def _entity_edit_history(self, user_id: str, entity_id: str) -> dict[str, Any]:
        aggregate = self.storage.execute(
            """SELECT COUNT(DISTINCT version) AS versions, MAX(version) AS current_version
                 FROM entity_versions WHERE entity_id=? AND user_id=?""",
            (entity_id, user_id),
        ).fetchone()
        version_count = int(aggregate["versions"] or 0) if aggregate else 0
        if not version_count:
            return {"versions": 0, "last_edited_at": None, "restorable_version": None}
        current_version = int(aggregate["current_version"] or 0)
        latest = self.storage.execute(
            """SELECT created_at FROM entity_versions
                 WHERE entity_id=? AND user_id=?
                 ORDER BY version DESC, id DESC LIMIT 1""",
            (entity_id, user_id),
        ).fetchone()
        # Слияние тоже правит цель и тоже пишет версию — но откатывать его надо
        # разъединением, а не «отменой последней правки»: иначе алиас-мост со
        # старым именем исчезает, а слитая сущность остаётся надгробием. Версии,
        # созданные живым слиянием, для этой кнопки закрыты.
        floor = self.storage.merge_version_floor(entity_id, user_id)
        restorable = self.storage.execute(
            """SELECT MAX(version) AS version FROM entity_versions
                 WHERE entity_id=? AND user_id=? AND version<? AND version>=?""",
            (entity_id, user_id, current_version, floor),
        ).fetchone()
        return {
            "versions": version_count,
            "last_edited_at": str(latest["created_at"] or "") if latest else None,
            "restorable_version": (
                int(restorable["version"])
                if restorable is not None and restorable["version"] is not None
                else None
            ),
        }

    def review_knowledge_link(
        self,
        user_id: str,
        link_id: str,
        status: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any] | None:
        return self.storage.set_knowledge_entity_link_status(
            link_id,
            user_id,
            status,
            reviewed_by=reviewed_by,
        )

    def get_stats(self, user_id: str) -> dict[str, Any]:
        # One response is one SQLite WAL snapshot.  Separate autocommit SELECTs
        # can otherwise straddle an ingest commit and publish an impossible
        # combination (for example a promoted knowledge object with the old raw
        # count).  A deferred read transaction does not advance the relation
        # write clock and does not block concurrent WAL readers/writers.
        connection = self.storage.conn
        owns_snapshot = not connection.in_transaction
        if owns_snapshot:
            connection.execute("BEGIN")
        try:
            return {
                # Считается, а не меряется длиной выборки: `entities` взяты с потолком
                # 5000, и выше него это число застывало, продолжая выглядеть точным.
                # Замер: счётчик 0.9 мс против 16.6 мс у полной выборки — дешевле И честнее.
                "entity_count": self.storage.count_entities(user_id),
                "relation_count": _count_visible_relations(self.storage, user_id),
                "knowledge_object_count": self.storage.count_knowledge_objects(user_id),
                # Raw material and original files are distinct from promoted
                # Knowledge Objects. Both use the same quarantine-aware predicate
                # as their read surfaces, so hidden existence is not disclosed even
                # as an aggregate.
                "raw_object_count": self.storage.count_visible_raw_objects(user_id),
                "file_count": self.storage.count_visible_raw_objects(user_id, files_only=True),
                # Тем же агрегатом, что и `entity_count` выше, и по тем же условиям:
                # разбивка считалась питоном по странице в 5000 строк и на большем
                # корпусе застывала, стоя рядом с честным «всего».
                "entities_by_type": self.storage.count_entities_by_type(user_id),
                "pending_resolutions": self.storage.count_resolution_candidates(
                    user_id, ResolutionStatus.SUGGESTED
                ),
                "pending_inbox": self.storage.count_inbox(user_id, InboxStatus.PENDING),
                "pending_relation_candidates": self.storage.count_relation_candidates(
                    user_id, status="suggested"
                ),
                "pending_conflicts": self.storage.count_knowledge_conflicts(user_id, status="suggested"),
            }
        finally:
            if owns_snapshot and connection.in_transaction:
                connection.rollback()

    def is_empty(self, user_id: str) -> bool:
        return self.storage.count_knowledge_objects(user_id) == 0 and not self.storage.list_entities(
            user_id, limit=1
        )

    @staticmethod
    def get_bootstrap_suggestions(user_id: str) -> list[str]:
        del user_id
        return [
            "Отправьте заметку о проекте, человеке или решении — она появится во входящих.",
            "Загрузите PDF, DOCX, таблицу или обычный текстовый файл.",
            "Попросите сохранить факт явно: «Запомни: проект Альфа запускается в сентябре».",
        ]
