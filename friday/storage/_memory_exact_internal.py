"""Exact, transaction-local storage authority for ranked memory recall.

The existing :class:`~friday.retrieval.HybridSearcher` is a ranking provider,
not a source authority.  This private module closes its bounded proposal, then
reselects every publishable Knowledge/Raw row and every graph source under one
caller-owned SQLite transaction.  The public storage mixins and legacy
``memory_search`` path are intentionally unchanged.

Continuation tokens retain only keyed source handles, semantic revisions and
digests.  Plain queries, source identifiers, bodies and metadata never enter a
durable cursor.  Provider carriers and storage authorities are process-private
and deliberately have body-free representations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, NoReturn, SupportsIndex

from friday.audit_privacy import decode_audit_privacy_key
from friday.retrieval.memory_exact_contract import (
    MemoryExactCandidate,
    MemoryExactContinuation,
    MemoryExactContractError,
    MemoryExactDateWindowStatus,
    MemoryExactGraphCoverage,
    MemoryExactGraphDirection,
    MemoryExactGraphEdgeProjection,
    MemoryExactGraphEvidenceBasis,
    MemoryExactGraphNodeProjection,
    MemoryExactGraphPathProjection,
    MemoryExactGraphProjection,
    MemoryExactGraphRelationProjection,
    MemoryExactLifecycleStage,
    MemoryExactPage,
    MemoryExactRequest,
    MemoryExactTemporalStatus,
    _create_memory_exact_candidate,
    _create_memory_exact_page,
)
from friday.storage._privacy import (
    _exact_uploader_knowledge_dependency,
    _not_private_entity_material_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
    _not_private_relation_dependency,
)
from friday.storage.models import normalize_known_at

if TYPE_CHECKING:
    from friday.knowledge_graph import KnowledgeGraph

_AUTHORITY_FACTORY = object()
_PROVIDER_FACTORY = object()
_PROVIDER_SEAL_KEY = secrets.token_bytes(32)
_GLOBAL_ENTITY_MERGE_BOUND_PROOF = object()

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TURN_ID = re.compile(r"turn_[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")

_AUTHORITY_SCHEMA = "friday.memory-exact-storage-authority.v1"
_PROVIDER_SCHEMA = "friday.memory-exact-provider-snapshot.v1"
_PROVIDER_ROW_REVISION_SCHEMA = "friday.memory-exact-provider-row-revision.v1"
_CURSOR_SCHEMA = "friday.memory-exact-continuation.v1"
_SNAPSHOT_SCHEMA = "friday.memory-exact-storage-snapshot.v1"
_ROW_REVISION_SCHEMA = "friday.memory-exact-knowledge-revision.v1"
_RAW_REVISION_SCHEMA = "friday.memory-exact-raw-revision.v1"
_RELATION_REVISION_SCHEMA = "friday.memory-exact-relation-revision.v1"
_IMPLICIT_REVISION_SCHEMA = "friday.memory-exact-implicit-relation-revision.v1"
_LEDGER_SCHEMA = "friday.memory-exact-source-ledger.v1"
_GRAPH_SOURCE_SCHEMA = "friday.memory-exact-graph-source-set.v1"

# The lane must hash complete source material to make revalidation exact.  These
# ceilings are therefore deliberately below the generic 64 MiB document surface:
# an oversized source is unavailable here rather than being partly hashed or
# allowed to allocate without a preflight.
MEMORY_EXACT_MAX_PROVIDER_ROWS: Final = 50
MEMORY_EXACT_MAX_GRAPH_NODES: Final = 12
MEMORY_EXACT_MAX_GRAPH_RELATIONS: Final = 20
MEMORY_EXACT_MAX_GRAPH_PATHS: Final = 6
MEMORY_EXACT_MAX_PROVIDER_GRAPH_PATHS: Final = 10
MEMORY_EXACT_MAX_GRAPH_PATH_EDGES: Final = 4
MEMORY_EXACT_MAX_GRAPH_SOURCE_ROWS: Final = 80
MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS: Final = 512
MEMORY_EXACT_MAX_BODY_UTF8_BYTES: Final = 2 * 1024 * 1024
MEMORY_EXACT_MAX_METADATA_UTF8_BYTES: Final = 1024 * 1024
MEMORY_EXACT_MAX_FIELD_UTF8_BYTES: Final = 256 * 1024
MEMORY_EXACT_MAX_ROW_UTF8_BYTES: Final = 4 * 1024 * 1024
MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES: Final = 16 * 1024 * 1024
MEMORY_EXACT_MAX_PAGE_UTF8_BYTES: Final = 1_000_000
MEMORY_EXACT_MAX_CONTINUATION_BYTES: Final = 4_096
MEMORY_EXACT_MAX_EXCERPT_CHARS: Final = 600
MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS: Final = 4_096
MEMORY_EXACT_MAX_ENTITY_HISTORY_UTF8_BYTES: Final = 16 * 1024 * 1024
MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS: Final = 1_024
MEMORY_EXACT_MAX_ENTITY_MERGE_DEPTH: Final = 16
MEMORY_EXACT_MAX_IMPLICIT_LINK_ROWS: Final = 30
_MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS: Final = 400
_MEMORY_EXACT_MAX_HISTORICAL_RELATION_IDS: Final = 802
_MEMORY_EXACT_MAX_GLOBAL_ENTITY_MERGE_ROWS: Final = 4_096
_MAX_GRAPH_TEXT_BYTES = 1024
_MAX_QUERY_BYTES = 16_384
_FETCH_BATCH = 8


class MemoryExactStorageError(ValueError):
    """Body-free failure at the exact memory storage boundary."""


class MemoryExactStorageDrift(MemoryExactStorageError):
    """The exact provider or source snapshot changed before publication."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise MemoryExactStorageError("memory exact canonical value is invalid") from None


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bytes_sha256(value: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MemoryExactStorageError("memory exact stored text is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def _hmac(key: bytes, *, domain: str, material: bytes) -> str:
    return hmac.new(
        key,
        domain.encode("ascii", errors="strict") + b"\x00" + material,
        hashlib.sha256,
    ).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryExactStorageError("memory exact JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> NoReturn:
    raise MemoryExactStorageError(f"memory exact JSON constant {value!r} is invalid")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MemoryExactStorageError("memory exact JSON number is not finite")
    return parsed


def _strict_json(value: str, *, label: str) -> object:
    if not isinstance(value, str):
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        return json.loads(
            value,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
            parse_float=_finite_float,
        )
    except MemoryExactStorageError:
        raise
    except (UnicodeError, OverflowError, RecursionError, ValueError):
        raise MemoryExactStorageError(f"{label} is invalid") from None


def _scope(value: object, *, label: str, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MemoryExactStorageError(f"{label} is invalid") from None
    if len(encoded) > maximum or any(unicodedata.category(character).startswith("C") for character in value):
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = True,
    allow_controls: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MemoryExactStorageError(f"{label} is invalid") from None
    if len(encoded) > maximum:
        raise MemoryExactStorageError(f"{label} exceeds its byte limit")
    if not allow_controls and any(unicodedata.category(character).startswith("C") for character in value):
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _private_text(value: object, *, label: str, maximum: int) -> str:
    return _bounded_text(
        value,
        label=label,
        maximum=maximum,
        allow_empty=True,
        allow_controls=True,
    )


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _integer(value: object, *, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MemoryExactStorageError(f"{label} is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise MemoryExactStorageError(f"{label} is invalid")
    return result


def _enum_value(value: object, *, label: str) -> str:
    raw = getattr(value, "value", None)
    if not isinstance(raw, str) or not raw:
        raise MemoryExactStorageError(f"{label} is invalid")
    return raw


def _request_text(value: object, *, label: str) -> str:
    if value is None:
        return ""
    return _bounded_text(value, label=label, maximum=128, allow_controls=False)


def _utc(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 64:
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryExactStorageError(f"{label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryExactStorageError(f"{label} is invalid")
    normalized = parsed.astimezone(UTC).isoformat()
    if value != normalized:
        raise MemoryExactStorageError(f"{label} is not canonical UTC")
    return normalized


def _load_key(conn: sqlite3.Connection) -> bytes:
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
        return decode_audit_privacy_key(row[0] if row is not None else None)
    except Exception:  # noqa: BLE001 - absent storage authority fails closed
        raise MemoryExactStorageError("memory exact storage key is unavailable") from None


def _require_transaction(conn: sqlite3.Connection) -> None:
    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise RuntimeError("memory exact storage requires a caller-owned transaction")


def _request_identity(request: MemoryExactRequest) -> str:
    if type(request) is not MemoryExactRequest:
        raise MemoryExactStorageError("memory exact request is invalid")
    raw = request.to_identity_json()
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="strict")) > 32_768:
        raise MemoryExactStorageError("memory exact request identity is invalid")
    parsed = _strict_json(raw, label="memory exact request identity")
    canonical = _canonical_bytes(parsed)
    if canonical != raw.encode("ascii", errors="strict"):
        raise MemoryExactStorageError("memory exact request identity is not canonical")
    return hashlib.sha256(canonical).hexdigest()


def _selector_payload(request: MemoryExactRequest) -> dict[str, object]:
    tenant = _scope(request.tenant_id, label="request tenant identity")
    principal = _scope(request.principal_id, label="request principal identity")
    turn = request.active_turn_id
    if not isinstance(turn, str) or _TURN_ID.fullmatch(turn) is None:
        raise MemoryExactStorageError("request active turn identity is invalid")
    query = _private_text(request.query, label="request query", maximum=_MAX_QUERY_BYTES)
    if not query.strip():
        raise MemoryExactStorageError("request query is empty")
    stages = tuple(_enum_value(item, label="request lifecycle stage") for item in request.lifecycle_stages)
    if not stages or len(stages) != len(set(stages)):
        raise MemoryExactStorageError("request lifecycle stages are not closed")
    page_size = _integer(request.page_size, label="request page size", low=1, high=100)
    snapshot_limit = _integer(
        request.snapshot_limit,
        label="request snapshot limit",
        low=page_size,
        high=MEMORY_EXACT_MAX_PROVIDER_ROWS,
    )
    since = _request_text(request.since, label="request since")
    until = _request_text(request.until, label="request until")
    as_of = _request_text(request.as_of, label="request as_of")
    known_at = _request_text(request.known_at, label="request known_at")
    return {
        "schema": "friday.memory-exact-selector.v1",
        "tenant_sha256": hashlib.sha256(tenant.encode("utf-8")).hexdigest(),
        "principal_sha256": hashlib.sha256(principal.encode("utf-8")).hexdigest(),
        "active_turn_sha256": hashlib.sha256(turn.encode("ascii")).hexdigest(),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "since": since,
        "until": until,
        "as_of": as_of,
        "known_at": known_at,
        "lifecycle_stages": list(stages),
        "page_size": page_size,
        "snapshot_limit": snapshot_limit,
    }


def _sequence(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryExactStorageError(f"{label} is invalid")
    return value


def _copy_graph_type(value: object, *, label: str) -> str:
    """Match the released graph's 80-character model projection."""

    return _bounded_text(
        value,
        label=label,
        maximum=_MAX_GRAPH_TEXT_BYTES,
        allow_empty=False,
        allow_controls=False,
    )[:80]


def _copy_graph_node(value: object, *, label: str) -> dict[str, str]:
    item = _mapping(value, label=label)
    return {
        "id": _scope(item.get("id"), label=f"{label} identity", maximum=240),
        "name": _bounded_text(
            item.get("name", ""),
            label=f"{label} name",
            maximum=_MAX_GRAPH_TEXT_BYTES,
            allow_controls=False,
        ),
        "entity_type": _copy_graph_type(
            item.get("entity_type", "other"),
            label=f"{label} type",
        ),
    }


def _copy_graph_relation(value: object, *, label: str) -> dict[str, object]:
    item = _mapping(value, label=label)
    source = _scope(item.get("source_entity_id"), label=f"{label} source", maximum=240)
    target = _scope(item.get("target_entity_id"), label=f"{label} target", maximum=240)
    if source == target:
        raise MemoryExactStorageError(f"{label} is a self relation")
    implicit = item.get("implicit", False)
    if type(implicit) is not bool:
        raise MemoryExactStorageError(f"{label} implicit marker is invalid")
    copied: dict[str, object] = {
        "id": _scope(item.get("id"), label=f"{label} identity", maximum=768),
        "source_entity_id": source,
        "target_entity_id": target,
        "relation_type": _copy_graph_type(
            item.get("relation_type", "related_to"),
            label=f"{label} type",
        ),
        "implicit": implicit,
        "valid_from": _bounded_text(
            item.get("valid_from", "") or "",
            label=f"{label} valid_from",
            maximum=64,
            allow_controls=False,
        ),
        "valid_to": None,
    }
    valid_to = item.get("valid_to")
    if valid_to is not None:
        copied["valid_to"] = _bounded_text(
            valid_to,
            label=f"{label} valid_to",
            maximum=64,
            allow_empty=False,
            allow_controls=False,
        )
    for field in ("created_at", "updated_at", "invalidated_at"):
        raw = item.get(field)
        if raw is not None:
            copied[field] = _bounded_text(
                raw,
                label=f"{label} {field}",
                maximum=64,
                allow_controls=False,
            )
    for field in ("knowledge_object_id", "evidence_knowledge_object_id"):
        raw = item.get(field)
        if raw:
            copied[field] = _scope(raw, label=f"{label} {field}", maximum=240)
    for field in ("source_name", "target_name"):
        if field in item:
            copied[field] = _bounded_text(
                item[field],
                label=f"{label} {field}",
                maximum=_MAX_GRAPH_TEXT_BYTES,
                allow_controls=False,
            )
    if "weight" in item:
        copied["weight"] = _finite_number(item["weight"], label=f"{label} weight")
    return copied


def _copy_graph_edge(value: object, *, label: str) -> dict[str, object]:
    item = _mapping(value, label=label)
    traversal_from = _scope(item.get("from"), label=f"{label} from", maximum=240)
    traversal_to = _scope(item.get("to"), label=f"{label} to", maximum=240)
    source = _scope(item.get("source"), label=f"{label} source", maximum=240)
    target = _scope(item.get("target"), label=f"{label} target", maximum=240)
    direction = item.get("direction")
    if direction not in {"forward", "reverse"}:
        raise MemoryExactStorageError(f"{label} direction is invalid")
    expected = (traversal_from, traversal_to) if direction == "forward" else (traversal_to, traversal_from)
    if (source, target) != expected or traversal_from == traversal_to:
        raise MemoryExactStorageError(f"{label} endpoints are incoherent")
    implicit = item.get("implicit", False)
    if type(implicit) is not bool:
        raise MemoryExactStorageError(f"{label} implicit marker is invalid")
    copied: dict[str, object] = {
        "id": _scope(item.get("id"), label=f"{label} identity", maximum=768),
        "from": traversal_from,
        "to": traversal_to,
        "source": source,
        "target": target,
        "direction": direction,
        "type": _copy_graph_type(
            item.get("type", "related_to"),
            label=f"{label} type",
        ),
        "implicit": implicit,
        "valid_from": _bounded_text(
            item.get("valid_from", "") or "",
            label=f"{label} valid_from",
            maximum=64,
            allow_controls=False,
        ),
        "valid_to": None,
    }
    valid_to = item.get("valid_to")
    if valid_to is not None:
        copied["valid_to"] = _bounded_text(
            valid_to,
            label=f"{label} valid_to",
            maximum=64,
            allow_empty=False,
            allow_controls=False,
        )
    for field in ("created_at", "updated_at", "invalidated_at"):
        raw = item.get(field)
        if raw is not None:
            copied[field] = _bounded_text(
                raw,
                label=f"{label} {field}",
                maximum=64,
                allow_controls=False,
            )
    if "weight" in item:
        copied["weight"] = _finite_number(item["weight"], label=f"{label} weight")
    for field in ("knowledge_object_id", "evidence_knowledge_object_id"):
        raw = item.get(field)
        if raw:
            copied[field] = _scope(raw, label=f"{label} {field}", maximum=240)
    provenance = item.get("provenance")
    if provenance is not None:
        source_map = _mapping(provenance, label=f"{label} provenance")
        compact: dict[str, str] = {}
        for field in ("origin", "source", "kind"):
            raw = source_map.get(field)
            if raw:
                compact[field] = _bounded_text(
                    raw,
                    label=f"{label} provenance {field}",
                    maximum=160,
                    allow_controls=False,
                )
        raw_knowledge = source_map.get("knowledge_object_id")
        if raw_knowledge:
            compact["knowledge_object_id"] = _scope(
                raw_knowledge,
                label=f"{label} provenance knowledge",
                maximum=240,
            )
        copied["provenance"] = compact
    return copied


def _copy_graph_path(value: object, *, label: str) -> dict[str, object]:
    item = _mapping(value, label=label)
    edges_raw = _sequence(item.get("edges"), label=f"{label} edges")
    if not 1 <= len(edges_raw) <= MEMORY_EXACT_MAX_GRAPH_PATH_EDGES:
        raise MemoryExactStorageError(f"{label} edge count is invalid")
    entity_ids = tuple(
        _scope(raw, label=f"{label} entity", maximum=240)
        for raw in _sequence(item.get("entity_ids"), label=f"{label} entities")
    )
    if len(entity_ids) != len(edges_raw) + 1 or len(set(entity_ids)) != len(entity_ids):
        raise MemoryExactStorageError(f"{label} entity route is invalid")
    root = _scope(item.get("root"), label=f"{label} root", maximum=240)
    target = _scope(item.get("target"), label=f"{label} target", maximum=240)
    if root != entity_ids[0] or target != entity_ids[-1]:
        raise MemoryExactStorageError(f"{label} endpoints are invalid")
    edges: list[dict[str, object]] = []
    for index, raw in enumerate(edges_raw):
        edge = _copy_graph_edge(raw, label=f"{label} edge")
        if edge["from"] != entity_ids[index] or edge["to"] != entity_ids[index + 1]:
            raise MemoryExactStorageError(f"{label} edge route is invalid")
        edges.append(edge)
    entities_raw = item.get("entities", ())
    entities: list[dict[str, str]] = []
    if entities_raw:
        sequence = _sequence(entities_raw, label=f"{label} entity labels")
        if len(sequence) != len(entity_ids):
            raise MemoryExactStorageError(f"{label} entity labels are incomplete")
        for index, raw in enumerate(sequence):
            node = _copy_graph_node(raw, label=f"{label} entity label")
            if node["id"] != entity_ids[index]:
                raise MemoryExactStorageError(f"{label} entity label changed identity")
            entities.append(node)
    return {
        "path_id": _scope(item.get("path_id"), label=f"{label} identity", maximum=240),
        "root": root,
        "target": target,
        "entity_ids": list(entity_ids),
        "entities": entities,
        "edges": edges,
    }


def _copy_graph_context(
    request: MemoryExactRequest,
    raw: object,
    *,
    effective_query: str,
    temporal: Mapping[str, object],
    graph_saturated: bool,
) -> dict[str, object]:
    if type(graph_saturated) is not bool:
        raise MemoryExactStorageError("memory exact graph saturation binding is invalid")
    context = _mapping(raw, label="memory exact provider graph")
    if context.get("query") != effective_query:
        raise MemoryExactStorageError("memory exact provider graph changed its effective query")
    for field in (
        "as_of",
        "known_at",
        "known_at_floor",
        "history_complete",
        "identity_basis",
        "temporal_basis",
    ):
        if context.get(field) != temporal[field]:
            raise MemoryExactStorageError(f"memory exact provider graph changed {field}")
    expanded = context.get("expanded")
    if type(expanded) is not bool:
        raise MemoryExactStorageError("memory exact provider graph expansion marker is invalid")

    raw_nodes = _sequence(context.get("nodes", ()), label="memory exact provider graph nodes")
    if len(raw_nodes) > MEMORY_EXACT_MAX_GRAPH_NODES:
        raise MemoryExactStorageError("memory exact provider graph has too many nodes")
    nodes = [_copy_graph_node(item, label="memory exact provider graph node") for item in raw_nodes]
    node_ids = [str(item["id"]) for item in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise MemoryExactStorageError("memory exact provider graph has duplicate nodes")

    raw_roots = _sequence(context.get("roots", ()), label="memory exact provider graph roots")
    if len(raw_roots) > MEMORY_EXACT_MAX_GRAPH_NODES:
        raise MemoryExactStorageError("memory exact provider graph has too many roots")
    roots = [_copy_graph_node(item, label="memory exact provider graph root") for item in raw_roots]
    root_ids = [str(item["id"]) for item in roots]
    if len(root_ids) != len(set(root_ids)):
        raise MemoryExactStorageError("memory exact provider graph has duplicate roots")

    raw_relations = _sequence(context.get("relations", ()), label="memory exact provider graph relations")
    if len(raw_relations) > MEMORY_EXACT_MAX_GRAPH_RELATIONS:
        raise MemoryExactStorageError("memory exact provider graph has too many relations")
    relations = [
        _copy_graph_relation(item, label="memory exact provider graph relation") for item in raw_relations
    ]
    relation_ids = [str(item["id"]) for item in relations]
    if len(relation_ids) != len(set(relation_ids)):
        raise MemoryExactStorageError("memory exact provider graph has duplicate relations")

    raw_paths = _sequence(context.get("paths", ()), label="memory exact provider graph paths")
    if len(raw_paths) > MEMORY_EXACT_MAX_PROVIDER_GRAPH_PATHS:
        raise MemoryExactStorageError("memory exact provider graph has too many paths")
    paths = [_copy_graph_path(item, label="memory exact provider graph path") for item in raw_paths]
    path_ids = [str(item["path_id"]) for item in paths]
    if len(path_ids) != len(set(path_ids)):
        raise MemoryExactStorageError("memory exact provider graph has duplicate paths")
    if (temporal["as_of"] or temporal["known_at"]) and (
        any(bool(item["implicit"]) for item in relations)
        or any(bool(edge["implicit"]) for path in paths for edge in path["edges"])
    ):
        raise MemoryExactStorageError("memory exact temporal graph contains present-day co-occurrence")

    def matched(name: str, observed: int) -> int:
        raw_value = context.get(name, observed)
        return _integer(
            raw_value,
            label=f"memory exact provider graph {name}",
            low=observed,
            high=1_000_000_000,
        )

    if "nodes_matched_at_least" in context or "relations_matched_at_least" in context:
        # Released HybridSearcher exposes no authoritative node/relation total.
        # An injected count must not turn a saturated, unknowable cap into an
        # apparently complete graph.
        raise MemoryExactStorageError("memory exact provider invented graph coverage")
    nodes_matched = len(nodes)
    relations_matched = len(relations)
    paths_matched = matched("paths_matched_at_least", len(paths))
    paths_truncated = context.get("paths_truncated", False)
    if type(paths_truncated) is not bool or (paths_matched > len(paths) and not paths_truncated):
        raise MemoryExactStorageError("memory exact provider graph truncation is dishonest")
    if paths_truncated and paths_matched == len(paths):
        paths_matched += 1
    if not expanded and (relations or paths or paths_matched or paths_truncated):
        raise MemoryExactStorageError("memory exact unexpanded provider graph is invalid")
    if graph_saturated and (
        temporal["as_of"]
        or temporal["known_at"]
        or expanded
        or roots
        or nodes
        or relations
        or paths
        or paths_matched
        or paths_truncated
    ):
        raise MemoryExactStorageError("memory exact saturated graph projection is invalid")

    def upstream_coverage(*, observed: int, cap: int) -> str:
        if observed >= cap:
            return MemoryExactGraphCoverage.UNKNOWN.value
        return MemoryExactGraphCoverage.COMPLETE.value

    return {
        "expanded": expanded,
        "roots": roots,
        "nodes": nodes,
        "relations": relations,
        "paths": paths,
        "nodes_matched_at_least": nodes_matched,
        "relations_matched_at_least": relations_matched,
        "paths_matched_at_least": paths_matched,
        "nodes_coverage": upstream_coverage(
            observed=len(nodes),
            cap=MEMORY_EXACT_MAX_GRAPH_NODES,
        )
        if not graph_saturated
        else MemoryExactGraphCoverage.UNKNOWN.value,
        "relations_coverage": upstream_coverage(
            observed=len(relations),
            cap=MEMORY_EXACT_MAX_GRAPH_RELATIONS,
        ),
        "paths_coverage": (
            MemoryExactGraphCoverage.PARTIAL.value
            if paths_truncated or paths_matched > len(paths)
            else MemoryExactGraphCoverage.COMPLETE.value
        ),
        "paths_truncated": paths_truncated,
    }


class MemoryExactProviderSnapshot:
    """Process-private, sealed copy of one bounded HybridSearcher proposal."""

    _date_window_applied: bool
    _date_window_empty: bool
    _effective_query: str
    _graph_json: str
    _knowledge_ids: tuple[str, ...]
    _knowledge_revision_sha256s: tuple[str, ...]
    _matched_at_least: int
    _request: MemoryExactRequest
    _request_identity_sha256: str
    _seal: str
    _temporal_json: str

    __slots__ = (
        "_date_window_applied",
        "_date_window_empty",
        "_effective_query",
        "_graph_json",
        "_knowledge_ids",
        "_knowledge_revision_sha256s",
        "_matched_at_least",
        "_request",
        "_request_identity_sha256",
        "_seal",
        "_temporal_json",
    )

    def __init__(
        self,
        *,
        request: MemoryExactRequest,
        effective_query: str,
        knowledge_ids: tuple[str, ...],
        knowledge_revision_sha256s: tuple[str, ...],
        matched_at_least: int,
        date_window_applied: bool,
        date_window_empty: bool,
        temporal_json: str,
        graph_json: str,
        request_identity_sha256: str,
        seal: str,
        factory: object = None,
    ) -> None:
        if factory is not _PROVIDER_FACTORY:
            raise MemoryExactStorageError("memory exact provider snapshot is process-private")
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "_effective_query", effective_query)
        object.__setattr__(self, "_knowledge_ids", knowledge_ids)
        object.__setattr__(self, "_knowledge_revision_sha256s", knowledge_revision_sha256s)
        object.__setattr__(self, "_matched_at_least", matched_at_least)
        object.__setattr__(self, "_date_window_applied", date_window_applied)
        object.__setattr__(self, "_date_window_empty", date_window_empty)
        object.__setattr__(self, "_temporal_json", temporal_json)
        object.__setattr__(self, "_graph_json", graph_json)
        object.__setattr__(self, "_request_identity_sha256", request_identity_sha256)
        object.__setattr__(self, "_seal", seal)

    @property
    def request(self) -> MemoryExactRequest:
        return self._request

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("memory exact provider snapshot is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("memory exact provider snapshot is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory exact provider snapshot is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory exact provider snapshot is process-private")

    def __repr__(self) -> str:
        return "<MemoryExactProviderSnapshot sealed>"


def _provider_material(snapshot: MemoryExactProviderSnapshot) -> dict[str, object]:
    return {
        "schema": _PROVIDER_SCHEMA,
        "request_identity_sha256": snapshot._request_identity_sha256,
        "effective_query": snapshot._effective_query,
        "knowledge_ids": list(snapshot._knowledge_ids),
        "knowledge_revision_sha256s": list(snapshot._knowledge_revision_sha256s),
        "matched_at_least": snapshot._matched_at_least,
        "date_window_applied": snapshot._date_window_applied,
        "date_window_empty": snapshot._date_window_empty,
        "temporal": _strict_json(snapshot._temporal_json, label="provider temporal snapshot"),
        "graph": _strict_json(snapshot._graph_json, label="provider graph snapshot"),
    }


def _verify_provider_snapshot(
    snapshot: MemoryExactProviderSnapshot,
    request: MemoryExactRequest,
) -> tuple[dict[str, object], dict[str, object]]:
    if type(snapshot) is not MemoryExactProviderSnapshot or snapshot._request is not request:
        raise MemoryExactStorageError("memory exact provider snapshot is invalid")
    if _request_identity(request) != snapshot._request_identity_sha256:
        raise MemoryExactStorageError("memory exact provider request binding changed")
    material = _provider_material(snapshot)
    expected = _hmac(
        _PROVIDER_SEAL_KEY,
        domain=_PROVIDER_SCHEMA,
        material=_canonical_bytes(material),
    )
    if not hmac.compare_digest(expected, snapshot._seal):
        raise MemoryExactStorageError("memory exact provider snapshot seal is invalid")
    temporal = material["temporal"]
    graph = material["graph"]
    if not isinstance(temporal, dict) or not isinstance(graph, dict):
        raise MemoryExactStorageError("memory exact provider snapshot shape changed")
    return temporal, graph


def _provider_date_window_status(
    request: MemoryExactRequest,
    snapshot: MemoryExactProviderSnapshot,
) -> MemoryExactDateWindowStatus:
    try:
        return MemoryExactDateWindowStatus(
            since=request.since,
            until=request.until,
            applied=snapshot._date_window_applied,
            empty=snapshot._date_window_empty,
        )
    except MemoryExactContractError:
        raise MemoryExactStorageError("memory exact provider date-window status is invalid") from None


def _create_memory_exact_provider_snapshot(
    request: MemoryExactRequest,
    payload: Mapping[str, Any],
    provider_revisions: Mapping[str, tuple[str, str]],
    *,
    graph_saturated: bool = False,
) -> MemoryExactProviderSnapshot:
    """Close the HybridSearcher response before it reaches storage authority."""

    if type(graph_saturated) is not bool:
        raise MemoryExactStorageError("memory exact graph saturation binding is invalid")
    selector = _selector_payload(request)
    raw = _mapping(payload, label="memory exact provider response")
    effective_query = _private_text(
        raw.get("query"),
        label="memory exact provider effective query",
        maximum=_MAX_QUERY_BYTES,
    )
    if not effective_query.strip():
        raise MemoryExactStorageError("memory exact provider effective query is empty")
    results = _sequence(raw.get("results"), label="memory exact provider results")
    if len(results) > int(selector["snapshot_limit"]):
        raise MemoryExactStorageError("memory exact provider exceeded the requested snapshot limit")
    if not isinstance(provider_revisions, Mapping):
        raise MemoryExactStorageError("memory exact provider revision authority is invalid")
    knowledge_ids: list[str] = []
    knowledge_revision_sha256s: list[str] = []
    for item in results:
        row = _mapping(item, label="memory exact provider result")
        knowledge_id = _scope(row.get("id"), label="memory exact provider knowledge identity", maximum=240)
        revisions = provider_revisions.get(knowledge_id)
        if type(revisions) is not tuple or len(revisions) != 2:
            raise MemoryExactStorageError("memory exact provider result lacks bounded source authority")
        expected_revision, expected_row_revision = revisions
        if (
            not isinstance(expected_revision, str)
            or not _SHA256.fullmatch(expected_revision)
            or not isinstance(expected_row_revision, str)
            or not _SHA256.fullmatch(expected_row_revision)
        ):
            raise MemoryExactStorageError("memory exact provider revision authority is invalid")
        observed_row_revision = _provider_knowledge_revision(row)
        if not hmac.compare_digest(observed_row_revision, expected_row_revision):
            raise MemoryExactStorageError("memory exact provider result changed after bounded source read")
        knowledge_ids.append(knowledge_id)
        knowledge_revision_sha256s.append(expected_revision)
    if len(knowledge_ids) != len(set(knowledge_ids)):
        raise MemoryExactStorageError("memory exact provider returned duplicate knowledge identities")
    matched_at_least = _integer(
        raw.get("matched_at_least", len(knowledge_ids)),
        label="memory exact provider matched lower bound",
        low=len(knowledge_ids),
        high=1_000_000_000,
    )
    strategy = raw.get("strategy", {})
    if not isinstance(strategy, Mapping):
        raise MemoryExactStorageError("memory exact provider strategy is invalid")
    requested_window = request.since is not None or request.until is not None
    if requested_window:
        empty_marker = strategy.get("date_window_empty")
        if empty_marker is True:
            applied_marker = strategy.get("date_window_applied")
            if (
                strategy.get("date_window") is not True
                or (applied_marker is not None and applied_marker is not False)
                or knowledge_ids
                or matched_at_least != 0
            ):
                raise MemoryExactStorageError("memory exact provider date-window emptiness is invalid")
            date_window_applied = True
            date_window_empty = True
        else:
            if empty_marker is not None and empty_marker is not False:
                raise MemoryExactStorageError("memory exact provider date-window marker is invalid")
            marker = strategy.get("date_window_applied")
            if type(marker) is not bool or strategy.get("date_window") is not True:
                raise MemoryExactStorageError("memory exact provider omitted its date-window coverage")
            date_window_applied = marker
            date_window_empty = False
    else:
        for marker_name in ("date_window", "date_window_applied", "date_window_empty"):
            marker = strategy.get(marker_name)
            if marker is not None and marker is not False:
                raise MemoryExactStorageError("memory exact provider invented a date window")
        date_window_applied = False
        date_window_empty = False
    temporal: dict[str, object] = {
        "as_of": str(selector["as_of"]),
        "known_at": str(selector["known_at"]),
        "known_at_floor": _request_text(
            raw.get("known_at_floor", ""), label="memory exact provider known_at floor"
        ),
        "history_complete": raw.get("history_complete"),
        "identity_basis": raw.get("identity_basis"),
        "temporal_basis": raw.get("temporal_basis"),
    }
    if raw.get("as_of") != temporal["as_of"] or raw.get("known_at") != temporal["known_at"]:
        raise MemoryExactStorageError("memory exact provider changed a temporal boundary")
    expected_basis = "bitemporal" if temporal["known_at"] else "valid_time"
    if (
        temporal["history_complete"] is not True
        or temporal["identity_basis"] != "current_names"
        or temporal["temporal_basis"] != expected_basis
        or (temporal["known_at"] and not temporal["known_at_floor"])
        or (not temporal["known_at"] and temporal["known_at_floor"] != "")
    ):
        raise MemoryExactStorageError("memory exact provider temporal status is invalid")
    graph = _copy_graph_context(
        request,
        raw.get("graph_context"),
        effective_query=effective_query,
        temporal=temporal,
        graph_saturated=graph_saturated,
    )
    request_identity_sha256 = _request_identity(request)
    temporal_json = _canonical_bytes(temporal).decode("ascii")
    graph_json = _canonical_bytes(graph).decode("ascii")
    provisional = {
        "schema": _PROVIDER_SCHEMA,
        "request_identity_sha256": request_identity_sha256,
        "effective_query": effective_query,
        "knowledge_ids": knowledge_ids,
        "knowledge_revision_sha256s": knowledge_revision_sha256s,
        "matched_at_least": matched_at_least,
        "date_window_applied": date_window_applied,
        "date_window_empty": date_window_empty,
        "temporal": temporal,
        "graph": graph,
    }
    seal = _hmac(
        _PROVIDER_SEAL_KEY,
        domain=_PROVIDER_SCHEMA,
        material=_canonical_bytes(provisional),
    )
    return MemoryExactProviderSnapshot(
        request=request,
        effective_query=effective_query,
        knowledge_ids=tuple(knowledge_ids),
        knowledge_revision_sha256s=tuple(knowledge_revision_sha256s),
        matched_at_least=matched_at_least,
        date_window_applied=date_window_applied,
        date_window_empty=date_window_empty,
        temporal_json=temporal_json,
        graph_json=graph_json,
        request_identity_sha256=request_identity_sha256,
        seal=seal,
        factory=_PROVIDER_FACTORY,
    )


def _authorization_payload(
    value: tuple[tuple[str, str, str], ...],
    *,
    principal_id: str,
) -> list[dict[str, str]]:
    if type(value) is not tuple or len(value) != 2:
        raise MemoryExactStorageError("memory exact authorization bindings are invalid")
    expected = ("search.use", "knowledge.read")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not tuple or len(item) != 3:
            raise MemoryExactStorageError("memory exact authorization binding is invalid")
        security_id, user_id, preset_key = item
        if security_id != expected[index] or user_id != principal_id:
            raise MemoryExactStorageError("memory exact authorization escaped its principal")
        result.append(
            {
                "security_id": security_id,
                "user_id": _scope(user_id, label="authorization user identity"),
                "preset_key": _scope(preset_key, label="authorization preset identity"),
            }
        )
    return result


class MemoryExactStorageAuthority:
    """Database-keyed storage capability for one exact request and turn."""

    _adapter_binding_sha256: str
    _authority_context_sha256: str
    _authority_handle: str
    _authorization_binding_sha256: str
    _capability_binding_sha256: str
    _context_authority_sha256: str
    _person_binding_sha256: str
    _principal_id: str
    _request: MemoryExactRequest
    _request_identity_sha256: str
    _seal: str
    _selector_sha256: str
    _tenant_binding_sha256: str
    _tenant_id: str
    _turn_authority_sha256: str
    _turn_id_sha256: str

    __slots__ = (
        "_adapter_binding_sha256",
        "_authority_context_sha256",
        "_authority_handle",
        "_authorization_binding_sha256",
        "_capability_binding_sha256",
        "_context_authority_sha256",
        "_person_binding_sha256",
        "_principal_id",
        "_request",
        "_request_identity_sha256",
        "_seal",
        "_selector_sha256",
        "_tenant_binding_sha256",
        "_tenant_id",
        "_turn_authority_sha256",
        "_turn_id_sha256",
    )

    def __init__(
        self,
        *,
        request: MemoryExactRequest,
        tenant_id: str,
        principal_id: str,
        material: dict[str, object],
        seal: str,
        factory: object = None,
    ) -> None:
        if factory is not _AUTHORITY_FACTORY:
            raise MemoryExactStorageError("memory exact storage authority is process-private")
        object.__setattr__(self, "_request", request)
        object.__setattr__(self, "_tenant_id", tenant_id)
        object.__setattr__(self, "_principal_id", principal_id)
        for name in (
            "authority_handle",
            "authority_context_sha256",
            "request_identity_sha256",
            "selector_sha256",
            "turn_id_sha256",
            "turn_authority_sha256",
            "context_authority_sha256",
            "tenant_binding_sha256",
            "person_binding_sha256",
            "adapter_binding_sha256",
            "capability_binding_sha256",
            "authorization_binding_sha256",
        ):
            object.__setattr__(self, f"_{name}", _digest(material[name], label=name))
        object.__setattr__(self, "_seal", _digest(seal, label="storage authority seal"))

    @property
    def authority_handle(self) -> str:
        return self._authority_handle

    @property
    def request(self) -> MemoryExactRequest:
        return self._request

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("memory exact storage authority is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("memory exact storage authority is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory exact storage authority is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory exact storage authority is process-private")

    def __repr__(self) -> str:
        return "<MemoryExactStorageAuthority sealed>"


def _authority_material(authority: MemoryExactStorageAuthority) -> dict[str, object]:
    return {
        "schema": _AUTHORITY_SCHEMA,
        "authority_handle": authority._authority_handle,
        "authority_context_sha256": authority._authority_context_sha256,
        "request_identity_sha256": authority._request_identity_sha256,
        "selector_sha256": authority._selector_sha256,
        "turn_id_sha256": authority._turn_id_sha256,
        "turn_authority_sha256": authority._turn_authority_sha256,
        "context_authority_sha256": authority._context_authority_sha256,
        "tenant_binding_sha256": authority._tenant_binding_sha256,
        "person_binding_sha256": authority._person_binding_sha256,
        "adapter_binding_sha256": authority._adapter_binding_sha256,
        "capability_binding_sha256": authority._capability_binding_sha256,
        "authorization_binding_sha256": authority._authorization_binding_sha256,
        "tenant_sha256": hashlib.sha256(authority._tenant_id.encode("utf-8")).hexdigest(),
        "principal_sha256": hashlib.sha256(authority._principal_id.encode("utf-8")).hexdigest(),
    }


def _durable_authority_core(material: Mapping[str, object]) -> dict[str, object]:
    """Return the complete live-turn authority core used by restart tokens."""

    return {
        key: value
        for key, value in material.items()
        if key
        not in {
            "authority_context_sha256",
            "authority_handle",
        }
    }


def _expected_durable_authority(material: Mapping[str, object]) -> tuple[str, str]:
    core = _durable_authority_core(material)
    authority_context_sha256 = _sha256(
        {key: value for key, value in core.items() if key != "request_identity_sha256"}
    )
    authority_handle = _sha256({**core, "authority_context_sha256": authority_context_sha256})
    return authority_context_sha256, authority_handle


def _verify_authority(
    conn: sqlite3.Connection,
    authority: MemoryExactStorageAuthority,
    request: MemoryExactRequest,
) -> bytes:
    if type(authority) is not MemoryExactStorageAuthority or authority._request is not request:
        raise MemoryExactStorageError("memory exact storage authority is invalid")
    request_identity = _request_identity(request)
    selector_sha256 = _sha256(_selector_payload(request))
    if (
        request_identity != authority._request_identity_sha256
        or selector_sha256 != authority._selector_sha256
        or request.tenant_id != authority._tenant_id
        or request.principal_id != authority._principal_id
    ):
        raise MemoryExactStorageError("memory exact storage request binding changed")
    material = _authority_material(authority)
    expected_context, expected_handle = _expected_durable_authority(material)
    if (
        expected_context != authority._authority_context_sha256
        or expected_handle != authority._authority_handle
    ):
        raise MemoryExactStorageError("memory exact storage authority handle is invalid")
    key = _load_key(conn)
    expected_seal = _hmac(key, domain=_AUTHORITY_SCHEMA, material=_canonical_bytes(material))
    if not hmac.compare_digest(expected_seal, authority._seal):
        raise MemoryExactStorageError("memory exact storage authority seal is invalid")
    return key


def _issue_memory_exact_storage_authority_in_transaction(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    tenant_id: str,
    principal_id: str,
    turn_id: str,
    turn_authority_sha256: str,
    context_authority_sha256: str,
    tenant_binding_sha256: str,
    person_binding_sha256: str,
    adapter_binding_sha256: str,
    authorization_bindings: tuple[tuple[str, str, str], ...],
) -> MemoryExactStorageAuthority:
    """Issue storage authority only after the adapter's two fresh decisions."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="memory exact tenant identity")
    principal = _scope(principal_id, label="memory exact principal identity")
    selector = _selector_payload(request)
    if tenant != request.tenant_id or principal != request.principal_id:
        raise MemoryExactStorageError("memory exact issued scope differs from the request")
    if not isinstance(turn_id, str) or _TURN_ID.fullmatch(turn_id) is None:
        raise MemoryExactStorageError("memory exact turn identity is invalid")
    if turn_id != request.active_turn_id:
        raise MemoryExactStorageError("memory exact issued turn differs from the request")
    authorizations = _authorization_payload(authorization_bindings, principal_id=principal)
    authorization_binding_sha256 = _sha256(
        {"schema": "friday.memory-exact-authorization-bindings.v1", "items": authorizations}
    )
    capability_binding_sha256 = _sha256(
        {
            "schema": "friday.memory-exact-capability-binding.v1",
            "security_ids": [item["security_id"] for item in authorizations],
            "authorization_binding_sha256": authorization_binding_sha256,
        }
    )
    stable = {
        "schema": _AUTHORITY_SCHEMA,
        "request_identity_sha256": _request_identity(request),
        "selector_sha256": _sha256(selector),
        "turn_id_sha256": hashlib.sha256(turn_id.encode("ascii")).hexdigest(),
        "turn_authority_sha256": _digest(turn_authority_sha256, label="turn authority binding"),
        "context_authority_sha256": _digest(context_authority_sha256, label="context authority binding"),
        "tenant_binding_sha256": _digest(tenant_binding_sha256, label="tenant binding"),
        "person_binding_sha256": _digest(person_binding_sha256, label="person binding"),
        "adapter_binding_sha256": _digest(adapter_binding_sha256, label="adapter binding"),
        "capability_binding_sha256": capability_binding_sha256,
        "authorization_binding_sha256": authorization_binding_sha256,
        "tenant_sha256": hashlib.sha256(tenant.encode("utf-8")).hexdigest(),
        "principal_sha256": hashlib.sha256(principal.encode("utf-8")).hexdigest(),
    }
    authority_context_sha256, authority_handle = _expected_durable_authority(stable)
    material = {**stable, "authority_context_sha256": authority_context_sha256}
    sealed_material = {**material, "authority_handle": authority_handle}
    key = _load_key(conn)
    seal = _hmac(key, domain=_AUTHORITY_SCHEMA, material=_canonical_bytes(sealed_material))
    return MemoryExactStorageAuthority(
        request=request,
        tenant_id=tenant,
        principal_id=principal,
        material=sealed_material,
        seal=seal,
        factory=_AUTHORITY_FACTORY,
    )


def _record(cursor: sqlite3.Cursor, row: sqlite3.Row | tuple[object, ...]) -> dict[str, Any]:
    columns = tuple(str(item[0]) for item in (cursor.description or ()))
    return dict(zip(columns, tuple(row), strict=True))


def _probe_tenant(conn: sqlite3.Connection, *, tenant_id: str) -> None:
    """The first scoped database read; deliberately body- and count-free."""

    cursor = conn.execute(
        "SELECT 1 FROM users tenant WHERE tenant.id=? AND tenant.status='active' LIMIT 1",
        (tenant_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        raise MemoryExactStorageError("memory exact tenant scope is unavailable")


def _probe_provider_sources(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    provider_ids: tuple[str, ...],
) -> None:
    """ID-only exact-scope check before any candidate count or source body.

    Lifecycle/date exclusion is applied later.  A missing, deleted, foreign or
    quarantined ranked identity is source drift, not an ordinary filter miss.
    """

    if not provider_ids:
        return
    holders = ",".join("?" for _item in provider_ids)
    cursor = conn.execute(
        f"""SELECT knowledge.id
              FROM knowledge_objects knowledge
              JOIN raw_objects raw
                ON raw.id=knowledge.raw_object_id
               AND raw.user_id=knowledge.user_id
               AND raw.deleted_at IS NULL
               AND {_not_private_raw_dependency("raw")}
             WHERE knowledge.user_id=? AND knowledge.id IN ({holders})
               AND knowledge.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("knowledge")}""",  # nosec B608
        (tenant_id, *provider_ids),
    )
    rows = cursor.fetchall()
    cursor.close()
    returned = {str(row[0]) for row in rows}
    if returned != set(provider_ids):
        raise MemoryExactStorageError("memory exact ranked source is unavailable")


def _probe_graph_entities(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_ids: tuple[str, ...],
) -> None:
    """Resolve graph endpoint ownership before history counts or entity text."""

    if not entity_ids:
        return
    holders = ",".join("?" for _item in entity_ids)
    cursor = conn.execute(
        f"""SELECT entity.id
              FROM entities entity
             WHERE entity.user_id=? AND entity.id IN ({holders})
               AND entity.deleted_at IS NULL AND entity.canonical=1
               AND entity.merged_into_id IS NULL
               AND {_not_private_entity_material_dependency("entity")}""",  # nosec B608
        (tenant_id, *entity_ids),
    )
    rows = cursor.fetchall()
    cursor.close()
    if {str(row[0]) for row in rows} != set(entity_ids):
        raise MemoryExactStorageError("memory exact graph entity source is unavailable")


def _graph_knowledge_ids(graph: Mapping[str, object]) -> tuple[str, ...]:
    result: list[str] = []

    def add(value: object) -> None:
        if value:
            identity = _scope(value, label="memory exact graph knowledge identity", maximum=240)
            if identity not in result:
                result.append(identity)

    for relation in graph.get("relations", []):
        if not isinstance(relation, dict):
            raise MemoryExactStorageError("memory exact graph relation changed shape")
        add(relation.get("knowledge_object_id"))
        add(relation.get("evidence_knowledge_object_id"))
    for path in graph.get("paths", []):
        if not isinstance(path, dict):
            raise MemoryExactStorageError("memory exact graph path changed shape")
        for edge in path.get("edges", []):
            if not isinstance(edge, dict):
                raise MemoryExactStorageError("memory exact graph edge changed shape")
            add(edge.get("knowledge_object_id"))
            add(edge.get("evidence_knowledge_object_id"))
            provenance = edge.get("provenance")
            if isinstance(provenance, dict):
                add(provenance.get("knowledge_object_id"))
    if len(result) > MEMORY_EXACT_MAX_GRAPH_SOURCE_ROWS:
        raise MemoryExactStorageError("memory exact graph has too many knowledge sources")
    return tuple(result)


def _wanted_cte(
    provider_ids: tuple[str, ...],
    graph_ids: tuple[str, ...],
) -> tuple[str, tuple[object, ...]]:
    ordered: list[tuple[str, int, int]] = []
    positions: dict[str, int] = {}
    graph_set = set(graph_ids)
    for rank, identity in enumerate(provider_ids):
        positions[identity] = len(ordered)
        ordered.append((identity, rank, 1 if identity in graph_set else 0))
    for identity in graph_ids:
        if identity in positions:
            continue
        positions[identity] = len(ordered)
        ordered.append((identity, -1, 1))
    if not ordered:
        # A real one-row CTE avoids generating invalid ``VALUES`` syntax.  Its
        # impossible identity cannot match a stored source and is not a request ID.
        return "wanted(ordinal, object_id, candidate_rank, graph_source) AS (VALUES(0,'',-1,0))", ()
    holders = ",".join("(?,?,?,?)" for _item in ordered)
    parameters: list[object] = []
    for ordinal, (identity, rank, graph_source) in enumerate(ordered):
        parameters.extend((ordinal, identity, rank, graph_source))
    return (
        f"wanted(ordinal, object_id, candidate_rank, graph_source) AS (VALUES {holders})",
        tuple(parameters),
    )


def _date_filter_sql(
    request: MemoryExactRequest,
    *,
    alias: str,
    date_window_applied: bool,
) -> tuple[str, tuple[object, ...]]:
    since = request.since
    until = request.until
    if not date_window_applied or (since is None and until is None):
        return "1", ()
    # The mandatory preflight rejects every in-scope malformed/oversized value.
    # Keep the expression locally total as well: SQLite may evaluate this branch
    # on a row excluded by another WHERE term, and json_each must never parse that
    # unrelated row before the authoritative preflight can classify the scope.
    raw_metadata = f"{alias}.metadata_json"
    metadata = (
        f"CASE WHEN typeof({raw_metadata})='text' "
        f"AND length(CAST({raw_metadata} AS BLOB))<={MEMORY_EXACT_MAX_METADATA_UTF8_BYTES} "
        f"AND json_valid({raw_metadata}) AND json_type({raw_metadata})='object' "
        f"THEN {raw_metadata} ELSE '{{}}' END"
    )
    document_date = f"jericho_iso_date(json_extract({metadata},'$.document_date'))"
    own = [f"{document_date} IS NOT NULL"]
    own_parameters: list[object] = []
    mentioned = (
        f"EXISTS (SELECT 1 FROM json_each({metadata}, '$.dates') date_value "
        "WHERE jericho_iso_date(date_value.value) IS NOT NULL"
    )
    mentioned_parameters: list[object] = []
    if since is not None:
        own.append(f"{document_date}>=?")
        own_parameters.append(since)
        mentioned += " AND jericho_iso_date(date_value.value)>=?"
        mentioned_parameters.append(since)
    if until is not None:
        own.append(f"{document_date}<=?")
        own_parameters.append(until)
        mentioned += " AND jericho_iso_date(date_value.value)<=?"
        mentioned_parameters.append(until)
    mentioned += ")"
    return (
        f"(({' AND '.join(own)}) OR {mentioned})",
        tuple((*own_parameters, *mentioned_parameters)),
    )


def _require_classifiable_date_metadata_in_transaction(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    date_window_applied: bool,
) -> None:
    """Refuse a date total unless every eligible row is safely classifiable."""

    _require_transaction(conn)
    if not date_window_applied or (request.since is None and request.until is None):
        return
    stages = tuple(item.value for item in request.lifecycle_stages)
    holders = ",".join("?" for _item in stages)
    common = f"""FROM knowledge_objects knowledge
                    JOIN raw_objects raw
                      ON raw.id=knowledge.raw_object_id
                     AND raw.user_id=knowledge.user_id
                     AND raw.deleted_at IS NULL
                     AND {_not_private_raw_dependency("raw")}
                   WHERE knowledge.user_id=? AND knowledge.deleted_at IS NULL
                     AND {_not_private_knowledge_dependency("knowledge")}
                     AND knowledge.lifecycle_stage IN ({holders})"""  # nosec B608
    parameters: tuple[object, ...] = (request.tenant_id, *stages)
    oversized = conn.execute(
        f"""SELECT 1 {common}
               AND (typeof(knowledge.metadata_json)!='text'
                    OR length(CAST(knowledge.metadata_json AS BLOB))>?)
             LIMIT 1""",  # nosec B608
        (*parameters, MEMORY_EXACT_MAX_METADATA_UTF8_BYTES),
    ).fetchone()
    if oversized is not None:
        raise MemoryExactStorageError("memory exact date metadata exceeds its classification bound")
    malformed = conn.execute(
        f"""SELECT 1 {common}
               AND CASE WHEN json_valid(knowledge.metadata_json)
                        THEN json_type(knowledge.metadata_json)!='object'
                        ELSE 1 END
             LIMIT 1""",  # nosec B608
        parameters,
    ).fetchone()
    if malformed is not None:
        raise MemoryExactStorageError("memory exact date metadata is not classifiable")
    duplicate = conn.execute(
        f"""SELECT 1 {common}
               AND EXISTS (
                   SELECT 1 FROM json_each(knowledge.metadata_json) date_member
                    WHERE date_member.key IN ('document_date','dates')
                    GROUP BY date_member.key HAVING COUNT(*)>1
               )
             LIMIT 1""",  # nosec B608
        parameters,
    ).fetchone()
    if duplicate is not None:
        raise MemoryExactStorageError("memory exact date metadata is not canonical")


def _require_provider_date_metadata_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    uploaded_by: str | None,
) -> None:
    """Preflight every row the released provider's date predicates may visit."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider date tenant", maximum=240)
    author_sql = ""
    parameters: tuple[object, ...] = (tenant,)
    if uploaded_by is not None:
        author = _scope(uploaded_by, label="provider date uploader", maximum=240)
        author_sql = f" AND {_exact_uploader_knowledge_dependency('k')}"
        parameters = (tenant, author)
    common = f"""FROM knowledge_objects k
                  WHERE k.user_id=? AND k.deleted_at IS NULL
                    AND {_not_private_knowledge_dependency("k")}
                    {author_sql}"""  # nosec B608
    oversized = conn.execute(
        f"""SELECT 1 {common}
               AND (typeof(k.metadata_json)!='text'
                    OR length(CAST(k.metadata_json AS BLOB))>?)
             LIMIT 1""",  # nosec B608
        (*parameters, MEMORY_EXACT_MAX_METADATA_UTF8_BYTES),
    ).fetchone()
    if oversized is not None:
        raise MemoryExactStorageError("memory exact provider date metadata exceeds its bound")
    malformed = conn.execute(
        f"""SELECT 1 {common}
               AND CASE WHEN json_valid(k.metadata_json)
                        THEN json_type(k.metadata_json)!='object'
                        ELSE 1 END
             LIMIT 1""",  # nosec B608
        parameters,
    ).fetchone()
    if malformed is not None:
        raise MemoryExactStorageError("memory exact provider date metadata is not classifiable")
    duplicate = conn.execute(
        f"""SELECT 1 {common}
               AND EXISTS (
                   SELECT 1 FROM json_each(k.metadata_json) date_member
                    WHERE date_member.key IN ('document_date','dates')
                    GROUP BY date_member.key HAVING COUNT(*)>1
               )
             LIMIT 1""",  # nosec B608
        parameters,
    ).fetchone()
    if duplicate is not None:
        raise MemoryExactStorageError("memory exact provider date metadata is not canonical")


def _eligible_sql(
    request: MemoryExactRequest,
    *,
    alias: str,
    date_window_applied: bool,
) -> tuple[str, tuple[object, ...]]:
    stages = tuple(item.value for item in request.lifecycle_stages)
    stage_holders = ",".join("?" for _item in stages)
    date_sql, date_parameters = _date_filter_sql(
        request,
        alias=alias,
        date_window_applied=date_window_applied,
    )
    return (
        f"({alias}.lifecycle_stage IN ({stage_holders}) AND {date_sql})",
        (*stages, *date_parameters),
    )


def _material_size_expression(knowledge: str = "k", raw: str = "r") -> str:
    fields = (
        f"{knowledge}.id",
        f"{knowledge}.raw_object_id",
        f"{knowledge}.entity_id",
        f"{knowledge}.content",
        f"{knowledge}.content_type",
        f"{knowledge}.title",
        f"{knowledge}.summary",
        f"{knowledge}.tags_json",
        f"{knowledge}.metadata_json",
        f"{knowledge}.knowledge_kind",
        f"{knowledge}.lifecycle_stage",
        f"{knowledge}.superseded_by_id",
        f"{knowledge}.created_at",
        f"{knowledge}.updated_at",
        f"{raw}.raw_content",
        f"{raw}.content_type",
        f"{raw}.metadata_json",
        f"{raw}.source",
        f"{raw}.source_ref",
        f"{raw}.content_hash",
        f"{raw}.received_at",
        f"{raw}.created_at",
    )
    return " + ".join(f"length(CAST(COALESCE({field},'') AS BLOB))" for field in fields)


def _provider_material_size_expression(knowledge: str = "k", raw: str = "r") -> str:
    """Conservative bound for every dynamically typed provider projection."""

    canonical = _material_size_expression(knowledge, raw)
    extra = (
        f"{knowledge}.user_id",
        f"{knowledge}.importance",
        f"{knowledge}.quality_score",
        f"{knowledge}.promotion_score",
        f"{knowledge}.version",
        f"{knowledge}.deleted_at",
        f"{raw}.version",
    )
    extra_size = " + ".join(f"length(CAST(COALESCE({field},'') AS BLOB))" for field in extra)
    return f"({canonical}) + {extra_size}"


def _selected_material_sql(
    request: MemoryExactRequest,
    *,
    provider_ids: tuple[str, ...],
    graph_ids: tuple[str, ...],
    date_window_applied: bool,
) -> tuple[str, tuple[object, ...]]:
    wanted, wanted_parameters = _wanted_cte(provider_ids, graph_ids)
    eligible, eligible_parameters = _eligible_sql(
        request,
        alias="k",
        date_window_applied=date_window_applied,
    )
    public_knowledge = _not_private_knowledge_dependency("k")
    public_raw = _not_private_raw_dependency("r")
    sql = f"""WITH {wanted}, selected AS MATERIALIZED (
        SELECT wanted.ordinal, wanted.object_id, wanted.candidate_rank,
               wanted.graph_source,
               CASE WHEN wanted.candidate_rank>=0 AND {eligible} THEN 1 ELSE 0 END
                    AS candidate_eligible,
               k.rowid AS knowledge_rowid, r.rowid AS raw_rowid
          FROM wanted
          JOIN knowledge_objects k
            ON k.id=wanted.object_id AND k.user_id=? AND k.deleted_at IS NULL
           AND {public_knowledge}
          JOIN raw_objects r
            ON r.id=k.raw_object_id AND r.user_id=k.user_id AND r.deleted_at IS NULL
           AND {public_raw}
         WHERE wanted.graph_source=1 OR wanted.candidate_rank>=0
    )"""  # nosec B608 - only module-built predicates and placeholders
    parameters = (
        *wanted_parameters,
        *eligible_parameters,
        request.tenant_id,
    )
    return sql, parameters


@dataclass(frozen=True, slots=True, repr=False)
class _StoredMaterial:
    provider_rank: int
    graph_source: bool
    candidate_eligible: bool
    knowledge_id: str
    raw_object_id: str
    entity_id: str | None
    content: str
    content_type: str
    title: str
    summary: str
    tags_json: str
    knowledge_metadata_json: str
    knowledge_kind: str
    importance: float
    quality_score: float
    promotion_score: float
    lifecycle_stage: str
    knowledge_version: int
    superseded_by_id: str | None
    knowledge_created_at: str
    knowledge_updated_at: str
    raw_source: str
    raw_source_ref: str
    raw_content: str
    raw_content_type: str
    raw_metadata_json: str
    raw_content_hash: str
    raw_version: int
    raw_received_at: str
    raw_created_at: str
    storage_bytes: int
    knowledge_revision_sha256: str
    raw_revision_sha256: str
    source_handle: str


def _json_text(value: object, *, label: str, expected: type[dict] | type[list]) -> str:
    raw = _private_text(value, label=label, maximum=MEMORY_EXACT_MAX_METADATA_UTF8_BYTES)
    parsed = _strict_json(raw, label=label)
    if type(parsed) is not expected:
        raise MemoryExactStorageError(f"{label} has the wrong JSON shape")
    _canonical_bytes(parsed)
    return raw


def _normalized_instant(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryExactStorageError(f"{label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryExactStorageError(f"{label} is invalid")
    return parsed.astimezone(UTC).isoformat()


def _provider_knowledge_revision(values: Mapping[str, Any]) -> str:
    """Digest exactly the content-bearing row the legacy ranker received."""

    if not isinstance(values, Mapping):
        raise MemoryExactStorageError("memory exact provider source row is invalid")
    knowledge_id = _scope(values.get("id"), label="provider source identity", maximum=240)
    tenant_id = _scope(values.get("user_id"), label="provider source tenant", maximum=240)
    raw_object_id = _scope(values.get("raw_object_id"), label="provider source raw identity", maximum=240)
    entity_id = _optional_identity(values.get("entity_id"), label="provider source entity")
    superseded = _optional_identity(
        values.get("superseded_by_id"), label="provider source superseding knowledge"
    )
    content = _private_text(
        values.get("content"),
        label="provider source body",
        maximum=MEMORY_EXACT_MAX_BODY_UTF8_BYTES,
    )
    title = _private_text(
        values.get("title"),
        label="provider source title",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    summary = _private_text(
        values.get("summary"),
        label="provider source summary",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    tags_json = _json_text(values.get("tags_json"), label="provider source tags", expected=list)
    metadata_json = _json_text(values.get("metadata_json"), label="provider source metadata", expected=dict)
    content_type = _bounded_text(
        values.get("content_type"),
        label="provider source content type",
        maximum=512,
        allow_controls=False,
    )
    knowledge_kind = _bounded_text(
        values.get("knowledge_kind"),
        label="provider source knowledge kind",
        maximum=320,
        allow_empty=False,
        allow_controls=False,
    )
    lifecycle_stage = values.get("lifecycle_stage")
    if lifecycle_stage not in {item.value for item in MemoryExactLifecycleStage}:
        raise MemoryExactStorageError("provider source lifecycle stage is invalid")
    version = _integer(values.get("version"), label="provider source version", low=1, high=2**63 - 1)
    created_at = _normalized_instant(values.get("created_at"), label="provider source creation timestamp")
    updated_at = _normalized_instant(values.get("updated_at"), label="provider source update timestamp")
    if values.get("deleted_at") is not None:
        raise MemoryExactStorageError("provider source is not live")
    return _sha256(
        {
            "schema": _PROVIDER_ROW_REVISION_SCHEMA,
            "knowledge_id_sha256": hashlib.sha256(knowledge_id.encode("utf-8")).hexdigest(),
            "tenant_id_sha256": hashlib.sha256(tenant_id.encode("utf-8")).hexdigest(),
            "raw_object_id_sha256": hashlib.sha256(raw_object_id.encode("utf-8")).hexdigest(),
            "body_sha256": _bytes_sha256(content),
            "title_sha256": _bytes_sha256(title),
            "summary_sha256": _bytes_sha256(summary),
            "tags_sha256": _bytes_sha256(tags_json),
            "metadata_sha256": _bytes_sha256(metadata_json),
            "content_type": content_type,
            "knowledge_kind": knowledge_kind,
            "importance": _finite_number(values.get("importance"), label="provider source importance"),
            "quality_score": _finite_number(values.get("quality_score"), label="provider source quality"),
            "promotion_score": _finite_number(
                values.get("promotion_score"), label="provider source promotion"
            ),
            "lifecycle_stage": lifecycle_stage,
            "version": version,
            "entity_binding_sha256": (
                None if entity_id is None else hashlib.sha256(entity_id.encode("utf-8")).hexdigest()
            ),
            "superseding_binding_sha256": (
                None if superseded is None else hashlib.sha256(superseded.encode("utf-8")).hexdigest()
            ),
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )


def _optional_identity(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _scope(value, label=label, maximum=240)


def _stored_material(
    values: Mapping[str, Any],
    *,
    key: bytes,
    tenant_id: str,
) -> _StoredMaterial:
    knowledge_id = _scope(values["knowledge_id"], label="stored knowledge identity", maximum=240)
    raw_object_id = _scope(values["raw_object_id"], label="stored raw identity", maximum=240)
    content = _private_text(
        values["knowledge_content"],
        label="stored knowledge body",
        maximum=MEMORY_EXACT_MAX_BODY_UTF8_BYTES,
    )
    raw_content = _private_text(
        values["raw_content"],
        label="stored raw body",
        maximum=MEMORY_EXACT_MAX_BODY_UTF8_BYTES,
    )
    title = _private_text(
        values["knowledge_title"],
        label="stored knowledge title",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    summary = _private_text(
        values["knowledge_summary"],
        label="stored knowledge summary",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    tags_json = _json_text(values["knowledge_tags_json"], label="stored knowledge tags", expected=list)
    knowledge_metadata_json = _json_text(
        values["knowledge_metadata_json"],
        label="stored knowledge metadata",
        expected=dict,
    )
    raw_metadata_json = _json_text(values["raw_metadata_json"], label="stored raw metadata", expected=dict)
    content_type = _bounded_text(
        values["knowledge_content_type"],
        label="stored knowledge content type",
        maximum=512,
        allow_controls=False,
    )
    knowledge_kind = _bounded_text(
        values["knowledge_kind"],
        label="stored knowledge kind",
        maximum=320,
        allow_empty=False,
        allow_controls=False,
    )
    lifecycle_stage = values["knowledge_lifecycle_stage"]
    if lifecycle_stage not in {item.value for item in MemoryExactLifecycleStage}:
        raise MemoryExactStorageError("stored knowledge lifecycle stage is invalid")
    raw_source = _private_text(
        values["raw_source"], label="stored raw source", maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
    )
    raw_source_ref = _private_text(
        values["raw_source_ref"],
        label="stored raw source reference",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    raw_content_type = _bounded_text(
        values["raw_content_type"],
        label="stored raw content type",
        maximum=512,
        allow_controls=False,
    )
    raw_content_hash = _bounded_text(
        values["raw_content_hash"],
        label="stored raw content hash",
        maximum=512,
        allow_controls=False,
    )
    knowledge_version = _integer(
        values["knowledge_version"], label="stored knowledge version", low=1, high=2**63 - 1
    )
    raw_version = _integer(values["raw_version"], label="stored raw version", low=1, high=2**63 - 1)
    provider_rank = _integer(
        values["candidate_rank"], label="stored provider rank", low=-1, high=MEMORY_EXACT_MAX_PROVIDER_ROWS
    )
    graph_source_raw = values["graph_source"]
    eligible_raw = values["candidate_eligible"]
    if graph_source_raw not in {0, 1} or eligible_raw not in {0, 1}:
        raise MemoryExactStorageError("stored source classification is invalid")
    importance = _finite_number(values["knowledge_importance"], label="stored knowledge importance")
    quality = _finite_number(values["knowledge_quality_score"], label="stored knowledge quality")
    promotion = _finite_number(values["knowledge_promotion_score"], label="stored knowledge promotion")
    knowledge_created_at = _normalized_instant(
        values["knowledge_created_at"], label="stored knowledge creation timestamp"
    )
    knowledge_updated_at = _normalized_instant(
        values["knowledge_updated_at"], label="stored knowledge update timestamp"
    )
    raw_received_at = _normalized_instant(values["raw_received_at"], label="stored raw receive timestamp")
    raw_created_at = _normalized_instant(values["raw_created_at"], label="stored raw timestamp")
    entity_id = _optional_identity(values["knowledge_entity_id"], label="stored primary entity")
    superseded = _optional_identity(
        values["knowledge_superseded_by_id"], label="stored superseding knowledge"
    )
    storage_bytes = sum(
        len(item.encode("utf-8", errors="strict"))
        for item in (
            knowledge_id,
            raw_object_id,
            entity_id or "",
            content,
            content_type,
            title,
            summary,
            tags_json,
            knowledge_metadata_json,
            knowledge_kind,
            lifecycle_stage,
            superseded or "",
            str(values["knowledge_created_at"]),
            str(values["knowledge_updated_at"]),
            raw_content,
            raw_content_type,
            raw_metadata_json,
            raw_source,
            raw_source_ref,
            raw_content_hash,
            str(values["raw_received_at"]),
            str(values["raw_created_at"]),
        )
    )
    if storage_bytes > MEMORY_EXACT_MAX_ROW_UTF8_BYTES:
        raise MemoryExactStorageError("stored memory source exceeds its row byte limit")

    raw_revision = _sha256(
        {
            "schema": _RAW_REVISION_SCHEMA,
            "body_sha256": _bytes_sha256(raw_content),
            "metadata_sha256": _bytes_sha256(raw_metadata_json),
            "source_sha256": _bytes_sha256(raw_source),
            "source_ref_sha256": _bytes_sha256(raw_source_ref),
            "content_type": raw_content_type,
            "declared_content_hash": raw_content_hash,
            "version": raw_version,
            "received_at": str(values["raw_received_at"]),
            "created_at": str(values["raw_created_at"]),
        }
    )
    knowledge_revision = _sha256(
        {
            "schema": _ROW_REVISION_SCHEMA,
            "raw_object_id_sha256": hashlib.sha256(raw_object_id.encode("utf-8")).hexdigest(),
            "body_sha256": _bytes_sha256(content),
            "title_sha256": _bytes_sha256(title),
            "summary_sha256": _bytes_sha256(summary),
            "tags_sha256": _bytes_sha256(tags_json),
            "metadata_sha256": _bytes_sha256(knowledge_metadata_json),
            "content_type": content_type,
            "knowledge_kind": knowledge_kind,
            "importance": importance,
            "quality_score": quality,
            "promotion_score": promotion,
            "lifecycle_stage": lifecycle_stage,
            "version": knowledge_version,
            "entity_binding_sha256": (
                None if entity_id is None else hashlib.sha256(entity_id.encode("utf-8")).hexdigest()
            ),
            "superseding_binding_sha256": (
                None if superseded is None else hashlib.sha256(superseded.encode("utf-8")).hexdigest()
            ),
            "created_at": str(values["knowledge_created_at"]),
            "updated_at": str(values["knowledge_updated_at"]),
            "raw_revision_sha256": raw_revision,
        }
    )
    source_handle = _hmac(
        key,
        domain="friday.memory-exact-source-handle.v1",
        material=_canonical_bytes(
            {
                "tenant": tenant_id,
                "knowledge_id": knowledge_id,
                "raw_object_id": raw_object_id,
            }
        ),
    )
    return _StoredMaterial(
        provider_rank=provider_rank,
        graph_source=bool(graph_source_raw),
        candidate_eligible=bool(eligible_raw),
        knowledge_id=knowledge_id,
        raw_object_id=raw_object_id,
        entity_id=entity_id,
        content=content,
        content_type=content_type,
        title=title,
        summary=summary,
        tags_json=tags_json,
        knowledge_metadata_json=knowledge_metadata_json,
        knowledge_kind=knowledge_kind,
        importance=importance,
        quality_score=quality,
        promotion_score=promotion,
        lifecycle_stage=lifecycle_stage,
        knowledge_version=knowledge_version,
        superseded_by_id=superseded,
        knowledge_created_at=knowledge_created_at,
        knowledge_updated_at=knowledge_updated_at,
        raw_source=raw_source,
        raw_source_ref=raw_source_ref,
        raw_content=raw_content,
        raw_content_type=raw_content_type,
        raw_metadata_json=raw_metadata_json,
        raw_content_hash=raw_content_hash,
        raw_version=raw_version,
        raw_received_at=raw_received_at,
        raw_created_at=raw_created_at,
        storage_bytes=storage_bytes,
        knowledge_revision_sha256=knowledge_revision,
        raw_revision_sha256=raw_revision,
        source_handle=source_handle,
    )


@dataclass(frozen=True, slots=True, repr=False)
class _MaterialScan:
    by_id: dict[str, _StoredMaterial]
    candidates: tuple[_StoredMaterial, ...]
    snapshot_bytes: int


def _bounded_provider_id_selection(
    conn: sqlite3.Connection,
    *,
    rowid_sql: str,
    parameters: tuple[object, ...],
    limit: int,
    reserve_bytes: Callable[[int], None],
) -> tuple[str, ...]:
    """Reserve a rowid-only selection before its stored identities reach Python."""

    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider identity reservation is unavailable")
    raw_rows = conn.execute(rowid_sql, parameters).fetchall()
    rowids = tuple(row[0] for row in raw_rows)
    if (
        len(rowids) > limit
        or any(isinstance(rowid, bool) or not isinstance(rowid, int) or rowid <= 0 for rowid in rowids)
        or len(rowids) != len(set(rowids))
    ):
        raise MemoryExactStorageError("provider rowid selection is invalid")
    if not rowids:
        return ()
    holders = ",".join("(?,?)" for _rowid in rowids)
    selected_parameters: list[object] = []
    for ordinal, rowid in enumerate(rowids):
        selected_parameters.extend((ordinal, rowid))
    selected = f"WITH selected(ordinal,knowledge_rowid) AS (VALUES {holders})"  # nosec B608
    preflight = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                     COALESCE(SUM(length(CAST(k.id AS BLOB)) + 64),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 1 AND 240
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
        tuple(selected_parameters),
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider identity preflight is unavailable")
    row_count, storage_bytes, invalid_rows = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count != len(rowids)
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows != 0
    ):
        raise MemoryExactStorageError("provider identity preflight is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(
        selected
        + """ SELECT k.id FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
               ORDER BY selected.ordinal""",
        tuple(selected_parameters),
    ).fetchall()
    identities = tuple(
        _scope(row[0], label="provider search knowledge identity", maximum=240) for row in rows
    )
    if len(identities) != row_count or len(identities) != len(set(identities)):
        raise MemoryExactStorageError("provider search identity selection changed")
    return identities


def _memory_exact_provider_search_ids_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    query: str,
    limit: int,
    uploaded_by: str | None,
    fts_available: bool,
    reserve_bytes: Callable[[int], None],
) -> tuple[str, ...]:
    """Resolve the legacy FTS/LIKE order without materializing source bodies."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider search tenant", maximum=240)
    text = " ".join((query or "").split()).strip()
    if not text:
        return ()
    bounded_limit = max(1, min(int(limit), 200))
    scope_where = ""
    scope_params: tuple[str, ...] = ()
    if uploaded_by is not None:
        author = str(uploaded_by)
        if not author.strip():
            return ()
        scope_where = f" AND {_exact_uploader_knowledge_dependency('k')}"
        scope_params = (author,)
    identities: tuple[str, ...] = ()
    if type(fts_available) is not bool:
        raise MemoryExactStorageError("provider FTS availability is invalid")
    if fts_available:
        from friday.storage._knowledge import _fts_terms

        terms = _fts_terms(text)
        if terms:
            match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
            source = f"""FROM knowledge_fts
                          JOIN knowledge_objects k ON k.rowid=knowledge_fts.rowid
                         WHERE k.user_id=? AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}
                           {scope_where}
                           AND knowledge_fts MATCH ?
                         ORDER BY bm25(knowledge_fts, 1.0, 2.0, 1.5, 0.5) ASC,
                                  k.importance DESC LIMIT ?"""  # nosec B608
            parameters: tuple[object, ...] = (
                tenant,
                *scope_params,
                match_query,
                bounded_limit,
            )
            try:
                identities = _bounded_provider_id_selection(
                    conn,
                    rowid_sql=f"SELECT k.rowid AS knowledge_rowid {source}",
                    parameters=parameters,
                    limit=bounded_limit,
                    reserve_bytes=reserve_bytes,
                )
            except sqlite3.OperationalError:
                identities = ()
    if not identities:
        escaped = text.replace("%", r"\%").replace("_", r"\_")
        like = f"%{escaped}%"
        source = f"""FROM knowledge_objects k
                 WHERE k.user_id=? AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   {scope_where}
                   AND (k.title LIKE ? ESCAPE '\\' OR k.summary LIKE ? ESCAPE '\\'
                        OR k.content LIKE ? ESCAPE '\\' OR k.tags_json LIKE ? ESCAPE '\\')
                 ORDER BY k.importance DESC, k.updated_at DESC LIMIT ?"""  # nosec B608
        parameters = (tenant, *scope_params, like, like, like, like, bounded_limit)
        identities = _bounded_provider_id_selection(
            conn,
            rowid_sql=f"SELECT k.rowid AS knowledge_rowid {source}",
            parameters=parameters,
            limit=bounded_limit,
            reserve_bytes=reserve_bytes,
        )
    return identities


def _memory_exact_provider_live_id_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    knowledge_id: str,
    uploaded_by: str | None,
) -> tuple[str, ...]:
    """Resolve one live graph/dense candidate without reading its body."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider source tenant", maximum=240)
    identity = _scope(knowledge_id, label="provider source identity", maximum=240)
    scope_where = ""
    parameters: list[object] = [identity, tenant]
    if uploaded_by is not None:
        author = str(uploaded_by)
        if not author.strip():
            return ()
        scope_where = f" AND {_exact_uploader_knowledge_dependency('k')}"
        parameters.append(author)
    row = conn.execute(
        f"""SELECT k.id FROM knowledge_objects k
             JOIN raw_objects r
               ON r.id=k.raw_object_id AND r.user_id=k.user_id
              AND r.deleted_at IS NULL AND {_not_private_raw_dependency("r")}
            WHERE k.id=? AND k.user_id=? AND k.deleted_at IS NULL
              AND {_not_private_knowledge_dependency("k")}
              {scope_where} LIMIT 1""",  # nosec B608
        tuple(parameters),
    ).fetchone()
    return () if row is None else (identity,)


def _install_memory_exact_provider_select_authorizer(conn: sqlite3.Connection) -> None:
    """Install the strict authorizer used by one statement or whole graph lease."""

    allowed = {
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_SELECT,
    }
    recursive = getattr(sqlite3, "SQLITE_RECURSIVE", None)
    if isinstance(recursive, int):
        allowed.add(recursive)

    def authorize(
        action: int,
        _first: str | None,
        _second: str | None,
        _database: str | None,
        _source: str | None,
    ) -> int:
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorize)


def _execute_memory_exact_provider_select(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> sqlite3.Cursor:
    """Prepare one statement under SQLite's own closed read-only authorizer."""

    from friday.storage._core import _install_private_material_authorizer

    _install_memory_exact_provider_select_authorizer(conn)
    try:
        return conn.execute(sql, params)
    except sqlite3.DatabaseError as exc:
        if "author" in str(exc).casefold() or "not authorized" in str(exc).casefold():
            raise MemoryExactStorageError("provider replay statement is not read-only") from None
        raise
    finally:
        # sqlite3 exposes no getter for the previous callback. Every FridayStorage
        # connection owns this canonical guard, so restore its code-owned factory.
        _install_private_material_authorizer(conn)


def _memory_exact_global_entity_merge_bound_in_transaction(
    conn: sqlite3.Connection,
    *,
    reserve_bytes: Callable[[int], None] | None,
) -> bool:
    """Prove a physical table bound before an unindexed scoped merge read."""

    _require_transaction(conn)
    cap = _MEMORY_EXACT_MAX_GLOBAL_ENTITY_MERGE_ROWS
    if (
        isinstance(cap, bool)
        or not isinstance(cap, int)
        or cap < 1
        or (reserve_bytes is not None and not callable(reserve_bytes))
    ):
        raise MemoryExactStorageError("memory exact entity merge history bound is invalid")
    cursor = conn.execute(
        f"""SELECT rowid
              FROM entity_merge_history
             ORDER BY rowid
             LIMIT {cap + 1}"""  # nosec B608 - fixed integer cap
    )
    row_count = 0
    previous_rowid: int | None = None
    try:
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                values = tuple(row)
                if len(values) != 1:
                    raise MemoryExactStorageError("memory exact entity merge history bound is invalid")
                rowid = values[0]
                if (
                    isinstance(rowid, bool)
                    or not isinstance(rowid, int)
                    or (previous_rowid is not None and rowid <= previous_rowid)
                ):
                    raise MemoryExactStorageError("memory exact entity merge history bound is invalid")
                previous_rowid = rowid
                row_count += 1
                if row_count > cap + 1:
                    raise MemoryExactStorageError("memory exact entity merge history bound is invalid")
    finally:
        cursor.close()
    if reserve_bytes is not None:
        reserve_bytes(row_count * 72)
    return row_count <= cap


def _memory_exact_entity_version_rowids_in_transaction(
    conn: sqlite3.Connection,
    *,
    entity_ids: tuple[str, ...],
    maximum_rows: int,
    reserve_bytes: Callable[[int], None] | None,
) -> tuple[bool, tuple[int, ...]]:
    """Probe each entity's UNIQUE-index history without a temp sort."""

    _require_transaction(conn)
    if (
        type(entity_ids) is not tuple
        or isinstance(maximum_rows, bool)
        or not isinstance(maximum_rows, int)
        or maximum_rows < 1
        or (reserve_bytes is not None and not callable(reserve_bytes))
    ):
        raise MemoryExactStorageError("memory exact entity version scope is invalid")
    identities = tuple(
        _scope(identity, label="memory exact entity version identity", maximum=240) for identity in entity_ids
    )
    if len(identities) != len(set(identities)):
        raise MemoryExactStorageError("memory exact entity version scope is invalid")

    rowids: list[int] = []
    seen: set[int] = set()
    for identity in sorted(identities):
        remaining = maximum_rows - len(rowids) + 1
        cursor = conn.execute(
            f"""SELECT version.rowid
                  FROM entity_versions version
                       INDEXED BY sqlite_autoindex_entity_versions_2
                 WHERE version.entity_id=?
                 ORDER BY version.version
                 LIMIT {remaining}""",  # nosec B608 - fixed integer remainder
            (identity,),
        )
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
        for row in rows:
            values = tuple(row)
            if len(values) != 1:
                raise MemoryExactStorageError("memory exact entity version scope is invalid")
            rowid = values[0]
            if isinstance(rowid, bool) or not isinstance(rowid, int) or rowid in seen:
                raise MemoryExactStorageError("memory exact entity version scope is invalid")
            seen.add(rowid)
            rowids.append(rowid)
        if len(rowids) >= maximum_rows + 1:
            if len(rowids) != maximum_rows + 1:
                raise MemoryExactStorageError("memory exact entity version scope is invalid")
            if reserve_bytes is not None:
                reserve_bytes(len(rowids) * 72)
            return True, tuple(rowids)
    if reserve_bytes is not None:
        reserve_bytes(len(rowids) * 72)
    return False, tuple(rowids)


def _memory_exact_provider_scoped_topology_proof_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_ids: tuple[str, ...],
    known_at: str,
    reserve_bytes: Callable[[int], None],
    maximum_identities: int,
    allow_later_unwitnessed: bool,
    global_entity_merge_bound_proof: object | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Validate only keyed topology scalars needed by one historical graph read."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider topology tenant", maximum=240)
    boundary = _validated_known_at(
        known_at,
        label="provider topology boundary",
        reject_future=True,
    )
    if (
        type(entity_ids) is not tuple
        or isinstance(maximum_identities, bool)
        or not isinstance(maximum_identities, int)
        or maximum_identities < 0
        or len(entity_ids) > maximum_identities
        or not callable(reserve_bytes)
        or type(allow_later_unwitnessed) is not bool
        or (
            global_entity_merge_bound_proof is not None
            and global_entity_merge_bound_proof is not _GLOBAL_ENTITY_MERGE_BOUND_PROOF
        )
    ):
        raise MemoryExactStorageError("provider topology source set is invalid")
    identities = tuple(_scope(item, label="provider topology identity", maximum=240) for item in entity_ids)
    if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
        raise MemoryExactStorageError("provider topology source set is invalid")
    if not identities:
        return (
            _sha256(
                {
                    "schema": "friday.memory-exact-provider-topology-proof.v1",
                    "boundary_sha256": _bytes_sha256(boundary),
                    "entities": [],
                    "merges": [],
                }
            ),
            (),
        )

    identity_set = set(identities)
    frontier = identities
    public_merge_entity = _not_private_entity_material_dependency("entity")
    for depth in range(MEMORY_EXACT_MAX_ENTITY_MERGE_DEPTH + 1):
        frontier_values = ",".join("(?)" for _identity in frontier)
        frontier_wanted = f"WITH wanted(entity_id) AS (VALUES {frontier_values})"  # nosec B608
        closure_preflight = conn.execute(
            frontier_wanted
            + f""" SELECT COUNT(*) AS row_count,
                         COALESCE(MAX(length(CAST(COALESCE(entity.merged_into_id,'') AS BLOB))),0)
                             AS maximum_target,
                         COALESCE(SUM(
                             length(CAST(entity.id AS BLOB))
                             + length(CAST(COALESCE(entity.merged_into_id,'') AS BLOB)) + 64
                         ),0) AS storage_bytes,
                         COALESCE(SUM(CASE
                             WHEN entity.merged_into_id IS NULL OR (
                                 typeof(entity.merged_into_id)='text'
                                 AND length(CAST(entity.merged_into_id AS BLOB)) BETWEEN 1 AND 240
                             ) THEN 0 ELSE 1 END),0) AS invalid_rows
                    FROM wanted
                    JOIN entities entity
                      ON entity.id=wanted.entity_id AND entity.user_id=?
                     AND {public_merge_entity}""",  # nosec B608 - fixed private predicate
            (*frontier, tenant),
        ).fetchone()
        if (
            closure_preflight is None
            or any(isinstance(item, bool) or not isinstance(item, int) for item in closure_preflight)
            or int(closure_preflight[0]) != len(frontier)
            or not 0 <= int(closure_preflight[1]) <= 240
            or int(closure_preflight[2]) < 0
            or int(closure_preflight[3]) != 0
        ):
            raise MemoryExactStorageError("provider topology merge closure is invalid")
        reserve_bytes(int(closure_preflight[2]))
        closure_rows = conn.execute(
            frontier_wanted
            + f""" SELECT entity.id,entity.merged_into_id
                    FROM wanted
                    JOIN entities entity
                      ON entity.id=wanted.entity_id AND entity.user_id=?
                     AND {public_merge_entity}
                   ORDER BY entity.id""",  # nosec B608 - fixed private predicate
            (*frontier, tenant),
        ).fetchall()
        discovered = {
            _scope(row[1], label="provider topology merge target", maximum=240)
            for row in closure_rows
            if row[1] is not None
        } - identity_set
        if not discovered:
            break
        if depth >= MEMORY_EXACT_MAX_ENTITY_MERGE_DEPTH:
            raise MemoryExactStorageError("provider topology merge chain exceeds its depth limit")
        if len(identity_set) + len(discovered) > maximum_identities:
            raise MemoryExactStorageError("provider graph topology source set is saturated")
        identity_set.update(discovered)
        frontier = tuple(sorted(discovered))
    identities = tuple(sorted(identity_set))

    values = ",".join("(?)" for _identity in identities)
    wanted = f"WITH wanted(entity_id) AS (VALUES {values})"  # nosec B608 - placeholders only
    public_entity = _not_private_entity_material_dependency("entity")
    current_preflight = conn.execute(
        wanted
        + f""" SELECT COUNT(*) AS row_count,
                     COALESCE(MAX(max(
                         length(CAST(entity.id AS BLOB)),
                         length(CAST(COALESCE(entity.merged_into_id,'') AS BLOB))
                     )),0) AS maximum_identity,
                     COALESCE(SUM(
                         length(CAST(entity.id AS BLOB))
                         + length(CAST(COALESCE(entity.merged_into_id,'') AS BLOB)) + 96
                     ),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(entity.id)='text'
                          AND length(CAST(entity.id AS BLOB)) BETWEEN 1 AND 240
                          AND entity.canonical IN (0,1)
                          AND (entity.merged_into_id IS NULL OR (
                              typeof(entity.merged_into_id)='text'
                              AND length(CAST(entity.merged_into_id AS BLOB)) BETWEEN 1 AND 240
                          ))
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM wanted
                JOIN entities entity ON entity.id=wanted.entity_id AND entity.user_id=?
                 AND {public_entity}""",  # nosec B608 - fixed private predicate
        (*identities, tenant),
    ).fetchone()
    if (
        current_preflight is None
        or any(isinstance(item, bool) or not isinstance(item, int) for item in current_preflight)
        or int(current_preflight[0]) != len(identities)
        or not 0 <= int(current_preflight[1]) <= 240
        or int(current_preflight[2]) < 0
        or int(current_preflight[3]) != 0
    ):
        raise MemoryExactStorageError("provider topology current projection is invalid")
    reserve_bytes(int(current_preflight[2]))
    current_cursor = conn.execute(
        wanted
        + f""" SELECT entity.id, entity.canonical, entity.merged_into_id,
                     entity.deleted_at IS NOT NULL AS deleted
                FROM wanted
                JOIN entities entity ON entity.id=wanted.entity_id AND entity.user_id=?
                 AND {public_entity}
               ORDER BY entity.id""",  # nosec B608 - fixed private predicate
        (*identities, tenant),
    )
    current_rows = current_cursor.fetchall()
    current_cursor.close()
    current: dict[str, tuple[bool, str, bool]] = {}
    for row in current_rows:
        identity = _scope(row[0], label="provider topology current identity", maximum=240)
        merged = ""
        if row[2] is not None:
            merged = _scope(row[2], label="provider topology merge target", maximum=240)
        current[identity] = (bool(row[1]), merged, bool(row[3]))
    if set(current) != set(identities):
        raise MemoryExactStorageError("provider topology current projection changed")

    relation_public = _not_private_relation_dependency("revision")
    existence_cursor = conn.execute(
        wanted
        + f""" SELECT entity.id,
                     CASE WHEN length(CAST((
                         SELECT version.created_at FROM entity_versions version
                          WHERE version.user_id=entity.user_id AND version.entity_id=entity.id
                          ORDER BY version.version,version.created_at,version.id LIMIT 1
                     ) AS BLOB)) BETWEEN 1 AND 64 THEN (
                         SELECT version.created_at FROM entity_versions version
                          WHERE version.user_id=entity.user_id AND version.entity_id=entity.id
                          ORDER BY version.version,version.created_at,version.id LIMIT 1
                     ) END AS first_recorded_at,
                     EXISTS(
                         SELECT 1 FROM relation_revisions revision
                         JOIN entities source_entity
                           ON source_entity.id=revision.source_entity_id
                          AND source_entity.user_id=revision.user_id
                          AND {_not_private_entity_material_dependency("source_entity")}
                         JOIN entities target_entity
                           ON target_entity.id=revision.target_entity_id
                          AND target_entity.user_id=revision.user_id
                          AND {_not_private_entity_material_dependency("target_entity")}
                        WHERE revision.user_id=? AND revision.recorded_at<=?
                          AND (revision.source_entity_id=entity.id
                               OR revision.target_entity_id=entity.id)
                          AND {relation_public} LIMIT 1
                     ) AS witnessed
                FROM wanted
                JOIN entities entity ON entity.id=wanted.entity_id AND entity.user_id=?
                 AND {public_entity}
               ORDER BY entity.id""",  # nosec B608 - fixed private predicates
        (*identities, tenant, boundary, tenant),
    )
    existence_rows = existence_cursor.fetchall()
    existence_cursor.close()
    reserve_bytes(len(identities) * 384)
    relevant: list[str] = []
    entity_proof: list[dict[str, object]] = []
    for row in existence_rows:
        identity = _scope(row[0], label="provider topology existence identity", maximum=240)
        if type(row[1]) is not str or row[2] not in {0, 1}:
            raise MemoryExactStorageError("provider topology existence history is incomplete")
        first_raw = row[1]
        first = _validated_known_at(
            first_raw,
            label="provider topology existence timestamp",
            reject_future=False,
        )
        coarse_same_second = (
            not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", first_raw) and first[:19] == boundary[:19]
        )
        later = not bool(row[2]) and (first > boundary or coarse_same_second)
        if later and not allow_later_unwitnessed:
            raise MemoryExactStorageError("provider topology identity did not exist at the boundary")
        if not later:
            relevant.append(identity)
        entity_proof.append(
            {
                "entity_handle": _hmac(
                    _PROVIDER_SEAL_KEY,
                    domain="friday.memory-exact-provider-topology-entity.v1",
                    material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                ),
                "first_recorded_at_sha256": _bytes_sha256(first_raw),
                "later_unwitnessed": later,
                "witnessed": bool(row[2]),
            }
        )
    if len(existence_rows) != len(identities):
        raise MemoryExactStorageError("provider topology existence projection changed")

    version_proof: list[dict[str, object]] = []
    recorded_tails: dict[str, tuple[bool, str, bool]] = {}
    if relevant:
        relevant_tuple = tuple(sorted(relevant))
        version_saturated, version_rowids = _memory_exact_entity_version_rowids_in_transaction(
            conn,
            entity_ids=relevant_tuple,
            maximum_rows=MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS,
            reserve_bytes=reserve_bytes,
        )
        if version_saturated:
            raise MemoryExactStorageError("provider topology version history exceeds its limits")
        if not version_rowids:
            raise MemoryExactStorageError("provider topology version history is incomplete")
        version_values = ",".join("(?)" for _rowid in version_rowids)
        version_selected = f"WITH selected(version_rowid) AS MATERIALIZED (VALUES {version_values})"  # nosec B608 - bounded integer placeholders only
        version_preflight = conn.execute(
            version_selected
            + f""" SELECT COUNT(*) AS row_count,
                         COALESCE(MAX(length(CAST(version.snapshot_json AS BLOB))),0)
                             AS maximum_json,
                         COALESCE(SUM(length(CAST(version.snapshot_json AS BLOB))),0)
                             AS aggregate_json,
                         COALESCE(MAX(max(
                             length(CAST(version.id AS BLOB)),
                             length(CAST(version.entity_id AS BLOB)),
                             length(CAST(version.created_at AS BLOB)),
                             CASE WHEN length(CAST(version.snapshot_json AS BLOB))
                                           <= {MEMORY_EXACT_MAX_METADATA_UTF8_BYTES}
                                      AND json_valid(version.snapshot_json)
                                  THEN length(CAST(COALESCE(
                                      json_extract(version.snapshot_json,'$.merged_into_id'),'') AS BLOB))
                                  ELSE 0 END
                         )),0) AS maximum_scalar,
                         COALESCE(SUM(
                             length(CAST(version.id AS BLOB))
                             + length(CAST(version.entity_id AS BLOB))
                             + length(CAST(version.created_at AS BLOB)) + 160
                         ),0) AS scalar_bytes,
                         COALESCE(SUM(CASE
                             WHEN length(CAST(version.snapshot_json AS BLOB))
                                      > {MEMORY_EXACT_MAX_METADATA_UTF8_BYTES}
                               OR NOT json_valid(version.snapshot_json)
                               OR json_type(version.snapshot_json,'$.canonical') IS NULL
                               OR json_type(version.snapshot_json,'$.canonical')
                                      NOT IN ('true','false','integer')
                               OR json_type(version.snapshot_json,'$.merged_into_id') IS NULL
                               OR json_type(version.snapshot_json,'$.merged_into_id')
                                      NOT IN ('null','text')
                               OR json_type(version.snapshot_json,'$.deleted_at') IS NULL
                               OR json_type(version.snapshot_json,'$.deleted_at')
                                      NOT IN ('null','text')
                             THEN 1 ELSE 0 END),0) AS invalid_rows
                    FROM selected
                    JOIN entity_versions version
                      ON version.rowid=selected.version_rowid
                    JOIN entities entity
                      ON entity.id=version.entity_id AND entity.user_id=version.user_id
                     AND {public_entity}
                   WHERE version.user_id=?""",  # nosec B608 - fixed private predicate
            (*version_rowids, tenant),
        ).fetchone()
        if (
            version_preflight is None
            or any(isinstance(item, bool) or not isinstance(item, int) for item in version_preflight)
            or not 0 <= int(version_preflight[0]) <= MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS
            or not 0 <= int(version_preflight[1]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
            or not 0 <= int(version_preflight[2]) <= MEMORY_EXACT_MAX_ENTITY_HISTORY_UTF8_BYTES
            or not 0 <= int(version_preflight[3]) <= 240
            or int(version_preflight[4]) < 0
            or int(version_preflight[5]) != 0
        ):
            raise MemoryExactStorageError("provider topology version history exceeds its limits")
        reserve_bytes(int(version_preflight[2]) + int(version_preflight[4]))
        version_cursor = conn.execute(
            version_selected
            + f""" SELECT version.id,version.entity_id,version.version,version.created_at,
                         json_extract(version.snapshot_json,'$.canonical') AS canonical,
                         json_extract(version.snapshot_json,'$.merged_into_id') AS merged_into_id,
                         json_extract(version.snapshot_json,'$.deleted_at') IS NOT NULL AS deleted
                    FROM selected
                    JOIN entity_versions version
                      ON version.rowid=selected.version_rowid
                    JOIN entities entity
                      ON entity.id=version.entity_id AND entity.user_id=version.user_id
                     AND {public_entity}
                   WHERE version.user_id=?
                   ORDER BY version.entity_id,version.version,version.created_at,version.id""",  # nosec B608
            (*version_rowids, tenant),
        )
        version_rows = version_cursor.fetchall()
        version_cursor.close()
        if len(version_rows) != int(version_preflight[0]):
            raise MemoryExactStorageError("provider topology version history changed")
        previous: dict[str, tuple[bool, str, bool]] = {}
        for row in version_rows:
            version_id = _scope(row[0], label="provider topology version identity", maximum=240)
            identity = _scope(row[1], label="provider topology version entity", maximum=240)
            _integer(row[2], label="provider topology version", low=1, high=2**63 - 1)
            raw_recorded = _bounded_text(
                row[3],
                label="provider topology version timestamp",
                maximum=64,
                allow_empty=False,
                allow_controls=False,
            )
            recorded = _validated_known_at(
                raw_recorded,
                label="provider topology version timestamp",
                reject_future=False,
            )
            merged = ""
            if row[5] is not None:
                merged = _scope(row[5], label="provider topology historical merge target", maximum=240)
            topology = (bool(row[4]), merged, bool(row[6]))
            earlier = previous.get(identity)
            coarse_same_second = (
                not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", raw_recorded) and recorded[:19] == boundary[:19]
            )
            if earlier is not None and topology != earlier and (recorded > boundary or coarse_same_second):
                raise MemoryExactStorageError("provider known_at crosses an entity topology change")
            previous[identity] = topology
            version_proof.append(
                {
                    "version_handle": _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-version.v1",
                        material=_canonical_bytes({"tenant": tenant, "version_id": version_id}),
                    ),
                    "entity_handle": _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-entity.v1",
                        material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                    ),
                    "recorded_at_sha256": _bytes_sha256(raw_recorded),
                    "canonical": topology[0],
                    "merged_handle": (
                        None
                        if not merged
                        else _hmac(
                            _PROVIDER_SEAL_KEY,
                            domain="friday.memory-exact-provider-topology-entity.v1",
                            material=_canonical_bytes({"tenant": tenant, "entity_id": merged}),
                        )
                    ),
                    "deleted": topology[2],
                }
            )
        if set(previous) != set(relevant_tuple):
            raise MemoryExactStorageError("provider topology version history is incomplete")
        recorded_tails = previous

    merge_proof: list[dict[str, object]] = []
    active_merges: set[tuple[str, str]] = set()
    if relevant:
        if (
            global_entity_merge_bound_proof is None
            and not _memory_exact_global_entity_merge_bound_in_transaction(
                conn,
                reserve_bytes=reserve_bytes,
            )
        ):
            raise MemoryExactStorageError("provider topology merge history exceeds its limits")
        relevant_tuple = tuple(sorted(relevant))
        relevant_values = ",".join("(?)" for _identity in relevant_tuple)
        merge_wanted = f"WITH wanted(entity_id) AS (VALUES {relevant_values})"  # nosec B608
        merge_selected = (
            merge_wanted
            + f""", selected(history_rowid) AS MATERIALIZED (
            SELECT history.rowid
              FROM wanted
              JOIN entity_merge_history history
                ON history.source_entity_id=wanted.entity_id AND history.user_id=?
             ORDER BY history.source_entity_id,history.created_at,history.id
             LIMIT {MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1}
        )"""
        )  # nosec B608 - placeholders and integer cap only
        merge_preflight = conn.execute(
            merge_selected
            + """ SELECT COUNT(*) AS row_count,
                         COALESCE(MAX(max(
                             length(CAST(history.id AS BLOB)),
                             length(CAST(history.source_entity_id AS BLOB)),
                             length(CAST(history.target_entity_id AS BLOB)),
                             length(CAST(history.created_at AS BLOB)),
                             length(CAST(COALESCE(history.undone_at,'') AS BLOB))
                         )),0) AS maximum_scalar,
                         COALESCE(SUM(
                             length(CAST(history.id AS BLOB))
                             + length(CAST(history.source_entity_id AS BLOB))
                             + length(CAST(history.target_entity_id AS BLOB))
                             + length(CAST(history.created_at AS BLOB))
                             + length(CAST(COALESCE(history.undone_at,'') AS BLOB)) + 128
                         ),0) AS storage_bytes
                    FROM selected
                    JOIN entity_merge_history history
                      ON history.rowid=selected.history_rowid""",
            (*relevant_tuple, tenant),
        ).fetchone()
        if (
            merge_preflight is None
            or any(isinstance(item, bool) or not isinstance(item, int) for item in merge_preflight)
            or not 0 <= int(merge_preflight[0]) <= MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1
            or not 0 <= int(merge_preflight[1]) <= 240
            or int(merge_preflight[2]) < 0
        ):
            raise MemoryExactStorageError("provider topology merge history exceeds its limits")
        if int(merge_preflight[0]) == MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1:
            raise MemoryExactStorageError("provider topology merge history exceeds its limits")
        reserve_bytes(int(merge_preflight[2]))
        merge_cursor = conn.execute(
            merge_selected
            + """ SELECT history.id,history.source_entity_id,history.target_entity_id,
                         history.created_at,history.undone_at
                    FROM selected
                    JOIN entity_merge_history history
                      ON history.rowid=selected.history_rowid
                   ORDER BY history.source_entity_id,history.created_at,history.id""",
            (*relevant_tuple, tenant),
        )
        merge_rows = merge_cursor.fetchall()
        merge_cursor.close()
        if len(merge_rows) != int(merge_preflight[0]):
            raise MemoryExactStorageError("provider topology merge history changed")
        for row in merge_rows:
            merge_id = _scope(row[0], label="provider topology merge identity", maximum=240)
            source = _scope(row[1], label="provider topology merge source", maximum=240)
            target = _scope(row[2], label="provider topology merge target", maximum=240)
            created_raw = _bounded_text(
                row[3],
                label="provider topology merge timestamp",
                maximum=64,
                allow_empty=False,
                allow_controls=False,
            )
            created = _validated_known_at(
                created_raw,
                label="provider topology merge timestamp",
                reject_future=False,
            )
            undone_raw = None
            undone = None
            if row[4] is not None:
                undone_raw = _bounded_text(
                    row[4],
                    label="provider topology unmerge timestamp",
                    maximum=64,
                    allow_empty=False,
                    allow_controls=False,
                )
                undone = _validated_known_at(
                    undone_raw,
                    label="provider topology unmerge timestamp",
                    reject_future=False,
                )
            if (
                source == target
                or created > boundary
                or (undone is not None and (undone > boundary or undone < created))
            ):
                raise MemoryExactStorageError("provider known_at crosses an entity merge or unmerge")
            if undone is None:
                active_merges.add((source, target))
            merge_proof.append(
                {
                    "merge_handle": _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-merge.v1",
                        material=_canonical_bytes({"tenant": tenant, "merge_id": merge_id}),
                    ),
                    "source_handle": _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-entity.v1",
                        material=_canonical_bytes({"tenant": tenant, "entity_id": source}),
                    ),
                    "target_handle": _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-entity.v1",
                        material=_canonical_bytes({"tenant": tenant, "entity_id": target}),
                    ),
                    "created_at_sha256": _bytes_sha256(created_raw),
                    "undone_at_sha256": (None if undone_raw is None else _bytes_sha256(undone_raw)),
                }
            )

    for identity, recorded in recorded_tails.items():
        actual = current[identity]
        recorded_merge = (
            not actual[0] and bool(actual[1]) and actual[2] and (identity, actual[1]) in active_merges
        )
        if recorded != actual and not recorded_merge:
            raise MemoryExactStorageError("provider current topology differs from its history")
    return (
        _sha256(
            {
                "schema": "friday.memory-exact-provider-topology-proof.v1",
                "boundary_sha256": _bytes_sha256(boundary),
                "entities": entity_proof,
                "versions": version_proof,
                "merges": merge_proof,
            }
        ),
        identities,
    )


class _MemoryExactReadOnlyRelationHistoryView:
    """Minimal GraphMixin view whose observation edge can only verify."""

    __slots__ = (
        "_allow_active_managed_context",
        "_conn",
        "_reserve_bytes",
        "_strict_lease",
    )

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        reserve_bytes: Callable[[int], None],
        allow_active_managed_context: bool,
        strict_lease: bool = False,
    ) -> None:
        _require_transaction(conn)
        if (
            not callable(reserve_bytes)
            or type(allow_active_managed_context) is not bool
            or type(strict_lease) is not bool
        ):
            raise MemoryExactStorageError("provider relation history reservation is unavailable")
        self._conn = conn
        self._reserve_bytes = reserve_bytes
        self._allow_active_managed_context = allow_active_managed_context
        self._strict_lease = strict_lease

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> sqlite3.Cursor:
        if self._strict_lease:
            return self._conn.execute(sql, params or ())
        return _execute_memory_exact_provider_select(self._conn, sql, params or ())

    def _observe_relation_history_boundary(self, boundary: str) -> None:
        canonical = _validated_known_at(
            boundary,
            label="memory exact provider known_at boundary",
            reject_future=False,
        )
        row = self.execute(
            """SELECT batch_id,recorded_at,observed_at
                 FROM relation_revision_context WHERE singleton=1"""
        ).fetchone()
        if row is None:
            raise MemoryExactStorageError("memory exact provider relation history observation is unavailable")
        self._reserve_bytes(256)
        batch_id, recorded_at, raw_observed = tuple(row)
        if type(batch_id) is not str or type(recorded_at) is not str or type(raw_observed) is not str:
            raise MemoryExactStorageError("memory exact provider relation history context is invalid")
        observed = _validated_known_at(
            raw_observed,
            label="memory exact provider relation history observation",
            reject_future=False,
        )
        if observed != raw_observed or observed < canonical:
            raise MemoryExactStorageError("memory exact provider relation history boundary was not observed")
        if batch_id == "" and recorded_at == "":
            return
        if not batch_id or not recorded_at:
            raise MemoryExactStorageError("memory exact provider relation history context is invalid")
        _scope(batch_id, label="memory exact provider relation history batch", maximum=240)
        recorded = _validated_known_at(
            recorded_at,
            label="memory exact provider relation history transaction",
            reject_future=False,
        )
        if recorded != recorded_at or recorded != observed:
            raise MemoryExactStorageError("memory exact provider relation history context is invalid")
        if not self._allow_active_managed_context:
            raise MemoryExactStorageError(
                "memory exact provider relation history transaction is not authorized"
            )


def _memory_exact_provider_relation_history_status_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    known_at: str,
    candidate_entity_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
    allow_active_managed_context: bool = False,
    strict_lease: bool = False,
) -> tuple[dict[str, Any], str]:
    """Replay the bounded history floor and exact candidate topology scope."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider relation-history tenant", maximum=240)
    boundary = _validated_known_at(
        known_at,
        label="memory exact provider known_at boundary",
        reject_future=True,
    )
    view = _MemoryExactReadOnlyRelationHistoryView(
        conn,
        reserve_bytes=reserve_bytes,
        allow_active_managed_context=allow_active_managed_context,
        strict_lease=strict_lease,
    )
    marker = view.execute(
        """SELECT CASE
                     WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND 64
                     THEN value ELSE NULL END AS value
                 FROM schema_meta WHERE key='relation_history_complete_from'"""
    ).fetchone()
    reserve_bytes(128)
    if marker is None or type(marker[0]) is not str:
        raise MemoryExactStorageError("memory exact provider relation history floor is unavailable")
    floor = _validated_known_at(
        marker[0],
        label="memory exact provider relation history floor",
        reject_future=False,
    )
    if floor != marker[0] or floor > boundary:
        raise MemoryExactStorageError("memory exact provider relation history is invalid")
    view._observe_relation_history_boundary(boundary)  # noqa: SLF001 - closed local view
    expected = {
        "known_at": boundary,
        "known_at_floor": floor,
        "history_complete": True,
        "identity_basis": "current_names",
    }
    topology_proof, _topology_ids = _memory_exact_provider_scoped_topology_proof_in_transaction(
        conn,
        tenant_id=tenant,
        entity_ids=candidate_entity_ids,
        known_at=boundary,
        reserve_bytes=reserve_bytes,
        maximum_identities=_MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
        allow_later_unwitnessed=True,
    )
    return expected, topology_proof


def _reserve_provider_object_window_ids(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    limit: int,
    uploaded_by: str | None,
    reserve_bytes: Callable[[int], None],
) -> None:
    """Charge every ID materialized by the released dense object window."""

    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider vector window reservation is unavailable")
    author_sql = ""
    parameters: list[object] = [tenant_id]
    if uploaded_by is not None:
        author_sql = f" AND {_exact_uploader_knowledge_dependency('window_k')}"
        parameters.append(uploaded_by)
    parameters.append(limit)
    selected = f"""WITH selected AS MATERIALIZED (
        SELECT window_k.rowid AS knowledge_rowid
          FROM knowledge_objects window_k INDEXED BY idx_knowledge_chunk_scan_order
         WHERE window_k.user_id=? AND window_k.deleted_at IS NULL
           AND {_not_private_knowledge_dependency("window_k")}
           {author_sql}
         ORDER BY window_k.created_at DESC,window_k.id ASC LIMIT ?
    )"""  # nosec B608 - module-owned predicates and placeholders only
    row = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                     COALESCE(SUM(length(CAST(k.id AS BLOB)) + 64),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 1 AND 240
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
        tuple(parameters),
    ).fetchone()
    if row is None:
        raise MemoryExactStorageError("provider vector window preflight is unavailable")
    row_count, storage_bytes, invalid_rows = tuple(row)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 0 <= row_count <= limit
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows != 0
    ):
        raise MemoryExactStorageError("provider vector window preflight is invalid")
    reserve_bytes(storage_bytes)


def _reserve_memory_exact_provider_embeddings_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    model: str,
    dim: int,
    limit: int | None,
    uploaded_by: str | None,
    reserve_bytes: Callable[[int], None],
) -> None:
    """Reserve whole-object vector material before the released loader sees BLOBs."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider vector tenant", maximum=240)
    model_name = _bounded_text(
        model,
        label="provider vector model",
        maximum=512,
        allow_empty=False,
        allow_controls=False,
    )
    dimension = _integer(dim, label="provider vector dimension", low=1, high=1_000_000)
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider vector preflight is unavailable")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1_000_000
    ):
        raise MemoryExactStorageError("provider vector limit is invalid")
    author = None
    if uploaded_by is not None:
        author = _scope(uploaded_by, label="provider vector uploader", maximum=240)
    params: list[object] = [tenant, model_name, dimension]
    if limit is not None and limit > 0:
        _reserve_provider_object_window_ids(
            conn,
            tenant_id=tenant,
            limit=limit,
            uploaded_by=author,
            reserve_bytes=reserve_bytes,
        )
        selected = (
            "SELECT e.rowid AS embedding_rowid "
            "FROM knowledge_embeddings e "
            "WHERE e.user_id=? AND e.model=? AND e.dim=? "
            "AND e.knowledge_object_id IN ("
            "SELECT window_k.id FROM knowledge_objects window_k "
            "INDEXED BY idx_knowledge_chunk_scan_order "
            "WHERE window_k.user_id=? AND window_k.deleted_at IS NULL "
            f"AND {_not_private_knowledge_dependency('window_k')}"  # nosec B608
        )
        params.append(tenant)
        if author is not None:
            selected += f" AND {_exact_uploader_knowledge_dependency('window_k')}"
            params.append(author)
        selected += " ORDER BY window_k.created_at DESC,window_k.id ASC LIMIT ?)"
        params.append(limit)
    else:
        selected = (
            "SELECT e.rowid AS embedding_rowid "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects k ON k.id=e.knowledge_object_id "
            "WHERE e.user_id=? AND e.model=? AND e.dim=? "
            "AND k.user_id=? AND k.deleted_at IS NULL "
            f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608
        )
        params.append(tenant)
        if author is not None:
            selected += f" AND {_exact_uploader_knowledge_dependency('k')}"
            params.append(author)
    row = conn.execute(
        f"""SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(length(CAST(e.knowledge_object_id AS BLOB))
                                + length(e.vector) + 128),0) AS storage_bytes,
                   COALESCE(SUM(CASE
                       WHEN typeof(e.knowledge_object_id)='text'
                        AND length(CAST(e.knowledge_object_id AS BLOB)) BETWEEN 1 AND 240
                        AND typeof(e.vector)='blob'
                        AND length(e.vector)=?
                       THEN 0 ELSE 1 END),0) AS invalid_rows
              FROM ({selected}) selected
              JOIN knowledge_embeddings e ON e.rowid=selected.embedding_rowid""",  # nosec B608
        (dimension * 4, *params),
    ).fetchone()
    if row is None:
        raise MemoryExactStorageError("provider vector preflight is unavailable")
    count, storage_bytes, invalid_rows = tuple(row)
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows != 0
    ):
        raise MemoryExactStorageError("provider vector preflight is invalid")
    reserve_bytes(storage_bytes)


def _reserve_memory_exact_provider_chunk_embeddings_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    model: str,
    dim: int,
    object_limit: int | None,
    row_limit: int | None,
    uploaded_by: str | None,
    reserve_bytes: Callable[[int], None],
) -> None:
    """Reserve passage-vector material before the released loader sees BLOBs."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider chunk tenant", maximum=240)
    model_name = _bounded_text(
        model,
        label="provider chunk model",
        maximum=512,
        allow_empty=False,
        allow_controls=False,
    )
    dimension = _integer(dim, label="provider chunk dimension", low=1, high=1_000_000)
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider chunk preflight is unavailable")
    for value, label in ((object_limit, "object"), (row_limit, "row")):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000
        ):
            raise MemoryExactStorageError(f"provider chunk {label} limit is invalid")
    author = None
    if uploaded_by is not None:
        author = _scope(uploaded_by, label="provider chunk uploader", maximum=240)
    selected = (
        "SELECT c.rowid AS embedding_rowid "
        "FROM knowledge_chunk_embeddings c "
        "JOIN knowledge_objects k ON k.id=c.knowledge_object_id "
        "WHERE c.user_id=? AND c.model=? AND c.dim=? "
        "AND k.user_id=? AND k.deleted_at IS NULL "
        f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608
    )
    params: list[object] = [tenant, model_name, dimension, tenant]
    if author is not None:
        selected += f" AND {_exact_uploader_knowledge_dependency('k')}"
        params.append(author)
    if object_limit is not None and object_limit > 0:
        _reserve_provider_object_window_ids(
            conn,
            tenant_id=tenant,
            limit=object_limit,
            uploaded_by=author,
            reserve_bytes=reserve_bytes,
        )
        selected += (
            " AND c.knowledge_object_id IN ("
            "SELECT window_k.id FROM knowledge_objects window_k "
            "INDEXED BY idx_knowledge_chunk_scan_order "
            "WHERE window_k.user_id=? AND window_k.deleted_at IS NULL "
            f"AND {_not_private_knowledge_dependency('window_k')}"  # nosec B608
        )
        params.append(tenant)
        if author is not None:
            selected += f" AND {_exact_uploader_knowledge_dependency('window_k')}"
            params.append(author)
        selected += " ORDER BY window_k.created_at DESC,window_k.id ASC LIMIT ?)"
        params.append(object_limit)
    selected += " ORDER BY k.created_at DESC,k.id,c.chunk_index"
    if row_limit is not None and row_limit > 0:
        selected += " LIMIT ?"
        params.append(row_limit)
    row = conn.execute(
        f"""SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(length(CAST(c.knowledge_object_id AS BLOB))
                                + length(CAST(c.chunk_index AS BLOB))
                                + length(c.vector) + 129),0) AS storage_bytes,
                   COALESCE(SUM(CASE
                       WHEN typeof(c.knowledge_object_id)='text'
                        AND length(CAST(c.knowledge_object_id AS BLOB)) BETWEEN 1 AND 240
                        AND typeof(c.chunk_index)='integer' AND c.chunk_index>=0
                        AND typeof(c.vector)='blob'
                        AND length(c.vector)=?
                       THEN 0 ELSE 1 END),0) AS invalid_rows
              FROM ({selected}) selected
              JOIN knowledge_chunk_embeddings c ON c.rowid=selected.embedding_rowid""",  # nosec B608
        (dimension * 4, *params),
    ).fetchone()
    if row is None:
        raise MemoryExactStorageError("provider chunk preflight is unavailable")
    count, storage_bytes, invalid_rows = tuple(row)
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows != 0
    ):
        raise MemoryExactStorageError("provider chunk preflight is invalid")
    reserve_bytes(storage_bytes)


def _load_memory_exact_provider_rows_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    knowledge_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[str, str]]]:
    """Preflight a complete provider batch, reserve it, then fetch in small batches."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider material tenant", maximum=240)
    if type(knowledge_ids) is not tuple or len(knowledge_ids) > _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS:
        raise MemoryExactStorageError("provider material row set is invalid")
    identities = tuple(
        _scope(item, label="provider material identity", maximum=240) for item in knowledge_ids
    )
    if len(identities) != len(set(identities)):
        raise MemoryExactStorageError("provider material row set is duplicated")
    if not identities:
        return (), {}
    holders = ",".join("(?,?)" for _item in identities)
    wanted_parameters: list[object] = []
    for ordinal, identity in enumerate(identities):
        wanted_parameters.extend((ordinal, identity))
    selected = f"""WITH wanted(ordinal, object_id) AS (VALUES {holders}),
        selected AS MATERIALIZED (
            SELECT wanted.ordinal, k.rowid AS knowledge_rowid, r.rowid AS raw_rowid
              FROM wanted
              JOIN knowledge_objects k
                ON k.id=wanted.object_id AND k.user_id=? AND k.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("k")}
              JOIN raw_objects r
                ON r.id=k.raw_object_id AND r.user_id=k.user_id AND r.deleted_at IS NULL
               AND {_not_private_raw_dependency("r")}
        )"""  # nosec B608 - placeholders and module-owned predicates only
    size = _provider_material_size_expression()
    preflight = conn.execute(
        selected
        + f""" SELECT COUNT(*) AS row_count,
                       COALESCE(SUM({size}),0) AS aggregate_bytes,
                       COALESCE(MAX({size}),0) AS maximum_row_bytes,
                       COALESCE(MAX(length(CAST(k.content AS BLOB))),0) AS maximum_knowledge_body,
                       COALESCE(MAX(length(CAST(r.raw_content AS BLOB))),0) AS maximum_raw_body,
                       COALESCE(MAX(length(CAST(k.metadata_json AS BLOB))),0) AS maximum_knowledge_metadata,
                       COALESCE(MAX(length(CAST(k.tags_json AS BLOB))),0) AS maximum_tags,
                       COALESCE(MAX(length(CAST(r.metadata_json AS BLOB))),0) AS maximum_raw_metadata,
                       COALESCE(MAX(max(length(CAST(COALESCE(k.title,'') AS BLOB)),
                                        length(CAST(COALESCE(k.summary,'') AS BLOB)),
                                        length(CAST(COALESCE(r.source,'') AS BLOB)),
                                        length(CAST(COALESCE(r.source_ref,'') AS BLOB)))),0)
                           AS maximum_wide_field,
                       COALESCE(SUM(CASE
                           WHEN typeof(k.version)='integer' AND k.version BETWEEN 1 AND 9223372036854775807
                            AND typeof(r.version)='integer' AND r.version BETWEEN 1 AND 9223372036854775807
                            AND typeof(k.importance) IN ('integer','real')
                            AND k.importance BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                            AND typeof(k.quality_score) IN ('integer','real')
                            AND k.quality_score BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                            AND typeof(k.promotion_score) IN ('integer','real')
                            AND k.promotion_score BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                           THEN 0 ELSE 1 END),0) AS invalid_numeric_rows
                  FROM selected
                  JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
                  JOIN raw_objects r ON r.rowid=selected.raw_rowid""",  # nosec B608
        (*wanted_parameters, tenant),
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider material preflight is unavailable")
    values = tuple(preflight)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise MemoryExactStorageError("provider material preflight is invalid")
    (
        row_count,
        aggregate_bytes,
        maximum_row_bytes,
        maximum_knowledge_body,
        maximum_raw_body,
        maximum_knowledge_metadata,
        maximum_tags,
        maximum_raw_metadata,
        maximum_wide_field,
        invalid_numeric_rows,
    ) = values
    if row_count != len(identities):
        raise MemoryExactStorageError("provider material source is unavailable")
    if (
        aggregate_bytes < 0
        or not 0 <= maximum_row_bytes <= MEMORY_EXACT_MAX_ROW_UTF8_BYTES
        or not 0 <= maximum_knowledge_body <= MEMORY_EXACT_MAX_BODY_UTF8_BYTES
        or not 0 <= maximum_raw_body <= MEMORY_EXACT_MAX_BODY_UTF8_BYTES
        or not 0 <= maximum_knowledge_metadata <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= maximum_tags <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= maximum_raw_metadata <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= maximum_wide_field <= MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
        or invalid_numeric_rows != 0
    ):
        raise MemoryExactStorageError("provider material exceeds its storage limits")
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider material reservation is invalid")
    reserve_bytes(aggregate_bytes)

    fields = """selected.ordinal, 0 AS candidate_rank, 0 AS graph_source,
                1 AS candidate_eligible,
                k.id AS knowledge_id, k.user_id AS knowledge_user_id,
                k.raw_object_id, k.entity_id AS knowledge_entity_id,
                k.content AS knowledge_content, k.content_type AS knowledge_content_type,
                k.title AS knowledge_title, k.summary AS knowledge_summary,
                k.tags_json AS knowledge_tags_json,
                k.metadata_json AS knowledge_metadata_json,
                k.knowledge_kind, k.importance AS knowledge_importance,
                k.quality_score AS knowledge_quality_score,
                k.promotion_score AS knowledge_promotion_score,
                k.lifecycle_stage AS knowledge_lifecycle_stage,
                k.version AS knowledge_version,
                k.superseded_by_id AS knowledge_superseded_by_id,
                k.created_at AS knowledge_created_at,
                k.updated_at AS knowledge_updated_at,
                k.deleted_at AS knowledge_deleted_at,
                r.source AS raw_source, r.source_ref AS raw_source_ref,
                r.raw_content, r.content_type AS raw_content_type,
                r.metadata_json AS raw_metadata_json,
                r.content_hash AS raw_content_hash, r.version AS raw_version,
                r.received_at AS raw_received_at, r.created_at AS raw_created_at"""
    cursor = conn.execute(
        selected
        + f""" SELECT {fields}
                  FROM selected
                  JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
                  JOIN raw_objects r ON r.rowid=selected.raw_rowid
                 ORDER BY selected.ordinal""",  # nosec B608
        (*wanted_parameters, tenant),
    )
    rows: list[dict[str, Any]] = []
    revisions: dict[str, tuple[str, str]] = {}
    try:
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for raw_row in batch:
                stored_values = _record(cursor, raw_row)
                material = _stored_material(stored_values, key=_PROVIDER_SEAL_KEY, tenant_id=tenant)
                row = {
                    "id": stored_values["knowledge_id"],
                    "user_id": stored_values["knowledge_user_id"],
                    "raw_object_id": stored_values["raw_object_id"],
                    "entity_id": stored_values["knowledge_entity_id"],
                    "content": stored_values["knowledge_content"],
                    "content_type": stored_values["knowledge_content_type"],
                    "title": stored_values["knowledge_title"],
                    "summary": stored_values["knowledge_summary"],
                    "tags_json": stored_values["knowledge_tags_json"],
                    "metadata_json": stored_values["knowledge_metadata_json"],
                    "knowledge_kind": stored_values["knowledge_kind"],
                    "importance": stored_values["knowledge_importance"],
                    "quality_score": stored_values["knowledge_quality_score"],
                    "promotion_score": stored_values["knowledge_promotion_score"],
                    "lifecycle_stage": stored_values["knowledge_lifecycle_stage"],
                    "version": stored_values["knowledge_version"],
                    "superseded_by_id": stored_values["knowledge_superseded_by_id"],
                    "created_at": stored_values["knowledge_created_at"],
                    "updated_at": stored_values["knowledge_updated_at"],
                    "deleted_at": stored_values["knowledge_deleted_at"],
                }
                identity = str(row["id"])
                rows.append(row)
                revisions[identity] = (
                    material.knowledge_revision_sha256,
                    _provider_knowledge_revision(row),
                )
    finally:
        cursor.close()
    if len(rows) != len(identities) or tuple(str(row["id"]) for row in rows) != identities:
        raise MemoryExactStorageError("provider material changed during selection")
    return tuple(rows), revisions


class _MemoryExactProviderSelectView:
    """SELECT-only storage view for exact provider read-set replay."""

    __slots__ = ("_conn", "_strict_lease")

    def __init__(self, conn: sqlite3.Connection, *, strict_lease: bool = False) -> None:
        _require_transaction(conn)
        if type(strict_lease) is not bool:
            raise MemoryExactStorageError("provider replay lease is invalid")
        self._conn = conn
        self._strict_lease = strict_lease

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> sqlite3.Cursor:
        if self._strict_lease:
            return self._conn.execute(sql, params or ())
        return _execute_memory_exact_provider_select(self._conn, sql, params or ())


class _MemoryExactProviderTopologyCollector:
    """Bound and validate entity identities before graph code consumes their rows."""

    __slots__ = ("_identities", "_maximum", "_proofs", "_validate", "_validated")

    def __init__(
        self,
        *,
        maximum: int,
        initial_identities: tuple[str, ...],
        validate: Callable[[tuple[str, ...]], tuple[str, tuple[str, ...]]] | None,
    ) -> None:
        if (
            type(initial_identities) is not tuple
            or len(initial_identities) > maximum
            or initial_identities != tuple(sorted(initial_identities))
            or len(initial_identities) != len(set(initial_identities))
        ):
            raise MemoryExactStorageError("provider graph topology source set is invalid")
        self._identities = set(initial_identities)
        self._maximum = maximum
        self._proofs: list[str] = []
        self._validate = validate
        self._validated: set[str] = set()

    def add(self, values: Sequence[object]) -> None:
        pending: set[str] = set()
        for value in values:
            if value is None or value == "":
                continue
            identity = _scope(value, label="provider graph topology identity", maximum=240)
            if identity not in self._validated:
                pending.add(identity)
        if not pending:
            return
        new_pending = pending - self._identities
        if len(self._identities) + len(new_pending) > self._maximum:
            raise MemoryExactStorageError("provider graph topology source set is saturated")
        ordered = tuple(sorted(pending))
        expanded = ordered
        if self._validate is not None:
            proof, expanded = self._validate(ordered)
            if not _SHA256.fullmatch(proof):
                raise MemoryExactStorageError("provider graph topology proof is invalid")
            if (
                type(expanded) is not tuple
                or expanded != tuple(sorted(expanded))
                or len(expanded) != len(set(expanded))
                or not set(ordered).issubset(expanded)
            ):
                raise MemoryExactStorageError("provider graph topology proof is invalid")
            if len(self._identities | set(expanded)) > self._maximum:
                raise MemoryExactStorageError("provider graph topology source set is saturated")
            self._proofs.append(proof)
        self._identities.update(expanded)
        self._validated.update(expanded)

    def account_rows(
        self,
        description: object,
        rows: Sequence[sqlite3.Row],
        *,
        track_plain_id: bool,
    ) -> None:
        if not rows:
            return
        if not isinstance(description, Sequence):
            raise MemoryExactStorageError("provider graph cursor description is invalid")
        names = tuple(str(column[0]) for column in description)
        wanted = {
            "entity_id",
            "_entity_id",
            "source_entity_id",
            "target_entity_id",
            "other_id",
            "merged_into_id",
        }
        indices = [index for index, name in enumerate(names) if name in wanted]
        if track_plain_id and "id" in names:
            indices.append(names.index("id"))
        discovered: list[object] = []
        for row in rows:
            values = tuple(row)
            for index in indices:
                discovered.append(values[index])
        self.add(discovered)

    def finish(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple(sorted(self._identities)), tuple(self._proofs)


class _MemoryExactProviderGraphCursor:
    """Incrementally account every row materialized by the graph view."""

    __slots__ = ("_collector", "_cursor", "_reserve_bytes", "_track_plain_id")

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        reserve_bytes: Callable[[int], None],
        collector: _MemoryExactProviderTopologyCollector,
        *,
        track_plain_id: bool,
    ) -> None:
        if (
            type(cursor) is not sqlite3.Cursor
            or not callable(reserve_bytes)
            or type(collector) is not _MemoryExactProviderTopologyCollector
        ):
            raise MemoryExactStorageError("provider graph cursor is invalid")
        self._cursor = cursor
        self._reserve_bytes = reserve_bytes
        self._collector = collector
        self._track_plain_id = track_plain_id

    @property
    def description(self) -> object:
        return self._cursor.description

    def _account(self, rows: Sequence[sqlite3.Row]) -> None:
        storage_bytes = 0
        for row in rows:
            values = tuple(row)
            storage_bytes += 64 + 8 * len(values)
            for value in values:
                value_type = type(value)
                if value is None:
                    continue
                if value_type is str:
                    try:
                        storage_bytes += len(value.encode("utf-8", errors="strict"))
                    except UnicodeError:
                        raise MemoryExactStorageError("provider graph row is invalid") from None
                elif value_type is bytes:
                    storage_bytes += len(value)
                elif value_type in (int, float):
                    storage_bytes += 8
                else:
                    raise MemoryExactStorageError("provider graph row is invalid")
        self._reserve_bytes(storage_bytes)
        self._collector.account_rows(
            self._cursor.description,
            rows,
            track_plain_id=self._track_plain_id,
        )

    def fetchone(self) -> sqlite3.Row | None:
        row = self._cursor.fetchone()
        if row is not None:
            self._account((row,))
        return row

    def fetchmany(self, size: int | None = None) -> list[sqlite3.Row]:
        if size is None:
            rows = self._cursor.fetchmany()
        else:
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise MemoryExactStorageError("provider graph fetch size is invalid")
            rows = self._cursor.fetchmany(size)
        self._account(rows)
        return rows

    def fetchall(self) -> list[sqlite3.Row]:
        rows: list[sqlite3.Row] = []
        while True:
            batch = self._cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            self._account(batch)
            rows.extend(batch)
        return rows

    def close(self) -> None:
        self._cursor.close()

    def __iter__(self) -> _MemoryExactProviderGraphCursor:
        return self

    def __next__(self) -> sqlite3.Row:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


def _memory_exact_provider_incident_relation_ids_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_id: str,
    known_at: str,
    historical_sql: str,
    historical_params: tuple[object, ...],
    reserve_bytes: Callable[[int], None],
) -> tuple[str, ...]:
    """Select the complete bounded relation-ID scope for one historical endpoint."""

    tenant = _scope(tenant_id, label="provider historical relation tenant", maximum=240)
    endpoint = _scope(entity_id, label="provider historical relation endpoint", maximum=240)
    boundary = _validated_known_at(
        known_at,
        label="provider historical relation boundary",
        reject_future=True,
    )
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider historical relation reservation is unavailable")
    if type(historical_sql) is not str or type(historical_params) is not tuple:
        raise MemoryExactStorageError("provider historical relation query is invalid")
    marker = "), selected AS ("
    marker_at = historical_sql.find(marker)
    if marker_at < 0:
        raise MemoryExactStorageError("provider historical relation query is invalid")
    selected_suffix = historical_sql[marker_at + len(marker) :]
    outer = re.search(
        r"\)\s*(SELECT\s+r\.relation_id\s+AS\s+id\b)",
        selected_suffix,
        flags=re.IGNORECASE,
    )
    if outer is None or len(historical_params) < 5:
        raise MemoryExactStorageError("provider historical relation query is invalid")
    scoped_suffix = selected_suffix[outer.start(1) :]
    scoped_params_tail = historical_params[2:]
    if re.search(r"\sLIMIT\s+\?\s*\Z", scoped_suffix, flags=re.IGNORECASE):
        scoped_suffix = re.sub(
            r"\sLIMIT\s+\?\s*\Z",
            "",
            scoped_suffix,
            flags=re.IGNORECASE,
        )
        if not scoped_params_tail:
            raise MemoryExactStorageError("provider historical relation query is invalid")
        scoped_params_tail = scoped_params_tail[:-1]
    incident_prefix = """WITH incident(relation_id) AS (
        SELECT relation_id FROM relation_revisions
         WHERE user_id=? AND recorded_at<=? AND source_entity_id=?
        UNION
        SELECT relation_id FROM relation_revisions
         WHERE user_id=? AND recorded_at<=? AND target_entity_id=?
    ), selected AS (
        SELECT rr.event_seq
          FROM incident scoped
          JOIN relation_revisions rr ON rr.relation_id=scoped.relation_id
         WHERE rr.user_id=? AND rr.recorded_at<=?
           AND rr.present=1
           AND NOT EXISTS (
               SELECT 1 FROM relation_revisions newer
                WHERE newer.user_id=rr.user_id
                  AND newer.relation_id=rr.relation_id
                  AND newer.recorded_at<=?
                  AND (newer.recorded_at>rr.recorded_at
                       OR (newer.recorded_at=rr.recorded_at
                           AND newer.event_seq>rr.event_seq))
           )
    ) """
    scoped_sql = incident_prefix + scoped_suffix
    scoped_params: tuple[object, ...] = (
        tenant,
        boundary,
        endpoint,
        tenant,
        boundary,
        endpoint,
        tenant,
        boundary,
        boundary,
        *scoped_params_tail,
    )
    cap = _MEMORY_EXACT_MAX_HISTORICAL_RELATION_IDS
    selected = (
        "WITH eligible AS MATERIALIZED (SELECT id AS relation_id FROM (" + scoped_sql + f") LIMIT {cap + 1})"
    )
    preflight = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                     COALESCE(MAX(length(CAST(relation_id AS BLOB))),0) AS maximum_identity,
                     COALESCE(SUM(length(CAST(relation_id AS BLOB)) + 64),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(relation_id)='text'
                          AND length(CAST(relation_id AS BLOB)) BETWEEN 1 AND 240
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM eligible""",
        scoped_params,
    ).fetchone()
    if (
        preflight is None
        or any(isinstance(item, bool) or not isinstance(item, int) for item in preflight)
        or not 0 <= int(preflight[0]) <= cap + 1
        or not 0 <= int(preflight[1]) <= 240
        or int(preflight[2]) < 0
        or int(preflight[3]) != 0
    ):
        raise MemoryExactStorageError("provider historical relation scope is invalid")
    if int(preflight[0]) == cap + 1:
        raise MemoryExactStorageError("provider historical relation scope is saturated")
    reserve_bytes(int(preflight[2]))
    rows = conn.execute(
        selected + " SELECT relation_id FROM eligible ORDER BY relation_id",
        scoped_params,
    ).fetchall()
    identities = tuple(
        _scope(row[0], label="provider historical relation identity", maximum=240) for row in rows
    )
    if len(identities) != int(preflight[0]) or identities != tuple(sorted(set(identities))):
        raise MemoryExactStorageError("provider historical relation scope changed")
    return identities


class _MemoryExactProviderGraphSelectView(_MemoryExactProviderSelectView):
    """Closed storage surface used by the two witnessed graph operations."""

    __slots__ = (
        "_allow_active_managed_context",
        "_candidate_entity_ids",
        "_collector",
        "_reserve_bytes",
        "_tenant_id",
    )

    def __init__(
        self,
        conn: sqlite3.Connection,
        reserve_bytes: Callable[[int], None],
        *,
        collector: _MemoryExactProviderTopologyCollector,
        tenant_id: str,
        candidate_entity_ids: tuple[str, ...],
        allow_active_managed_context: bool,
    ) -> None:
        super().__init__(conn, strict_lease=True)
        if (
            not callable(reserve_bytes)
            or type(collector) is not _MemoryExactProviderTopologyCollector
            or type(candidate_entity_ids) is not tuple
            or type(allow_active_managed_context) is not bool
        ):
            raise MemoryExactStorageError("provider graph reservation is unavailable")
        self._reserve_bytes = reserve_bytes
        self._collector = collector
        self._tenant_id = _scope(tenant_id, label="provider graph tenant", maximum=240)
        self._candidate_entity_ids = candidate_entity_ids
        self._allow_active_managed_context = allow_active_managed_context

    def __getattr__(self, _name: str) -> NoReturn:
        raise MemoryExactStorageError("provider graph read is unavailable")

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> _MemoryExactProviderGraphCursor:
        bound = params or ()
        normalized = " ".join(sql.upper().split())
        if (
            "SELECT COALESCE(MAX(RR.EVENT_SEQ), 0) AS WATERMARK" in normalized
            and "FROM RELATION_REVISIONS RR" in normalized
        ):
            # One caller-owned SQLite transaction is already the watermark. Keep
            # the released equality check without scanning the tenant revision set.
            sql = "SELECT 0 AS watermark"
            bound = ()
            normalized = sql.upper()
        elif (
            normalized.startswith("WITH RANKED AS (")
            and "ROW_NUMBER() OVER" in normalized
            and "FROM RELATION_REVISIONS RR" in normalized
            and "WHERE (R.SOURCE_ENTITY_ID=? OR R.TARGET_ENTITY_ID=?)" in normalized
        ):
            if (
                len(bound) < 5
                or bound[0] != self._tenant_id
                or bound[2] != bound[3]
                or bound[4] != self._tenant_id
            ):
                raise MemoryExactStorageError("provider historical relation query is invalid")
            endpoint = _scope(
                bound[2],
                label="provider historical relation endpoint",
                maximum=240,
            )
            boundary = _validated_known_at(
                bound[1],
                label="provider historical relation boundary",
                reject_future=True,
            )
            self._collector.add((endpoint,))
            relation_ids = _memory_exact_provider_incident_relation_ids_in_transaction(
                self._conn,
                tenant_id=self._tenant_id,
                entity_id=endpoint,
                known_at=boundary,
                historical_sql=sql,
                historical_params=bound,
                reserve_bytes=self._reserve_bytes,
            )
            marker = "), selected AS ("
            marker_at = sql.find(marker)
            if marker_at < 0:
                raise MemoryExactStorageError("provider historical relation query is invalid")
            if relation_ids:
                relation_values = ",".join("(?)" for _identity in relation_ids)
                incident = f"incident(relation_id) AS ( VALUES {relation_values}), "  # nosec B608
            else:
                incident = "incident(relation_id) AS (SELECT CAST(NULL AS TEXT) WHERE 0), "
            sql = (
                "WITH "
                + incident
                + "ranked AS (\n"
                + "    SELECT rr.event_seq, rr.relation_id, rr.recorded_at, rr.present,\n"
                + "           ROW_NUMBER() OVER (\n"
                + "               PARTITION BY rr.relation_id\n"
                + "               ORDER BY rr.recorded_at DESC, rr.event_seq DESC\n"
                + "           ) AS snapshot_rank\n"
                + "      FROM incident scoped\n"
                + "      JOIN relation_revisions rr ON rr.relation_id=scoped.relation_id\n"
                + "     WHERE rr.user_id=? AND rr.recorded_at<=?\n"
                + "), selected AS ("
                + sql[marker_at + len(marker) :]
            )
            bound = (*relation_ids, *bound)
            normalized = " ".join(sql.upper().split())
        track_plain_id = (
            "FROM ENTITIES E WHERE ID=? AND USER_ID=?" in normalized or "AS FIRST_RECORDED_AT" in normalized
        )
        return _MemoryExactProviderGraphCursor(
            super().execute(sql, bound),
            self._reserve_bytes,
            self._collector,
            track_plain_id=track_plain_id,
        )

    def find_entities_by_normalized_names(
        self,
        user_id: str,
        names: Sequence[str],
        *,
        include_aliases: bool = True,
        limit: int = 800,
    ) -> list[dict[str, Any]]:
        from friday.storage._graph import GraphMixin

        return GraphMixin.find_entities_by_normalized_names(  # type: ignore[arg-type]
            self,
            user_id,
            names,
            include_aliases=include_aliases,
            limit=limit,
        )

    def count_entity_relations(self, entity_id: str, user_id: str | None = None) -> int:
        tenant = _scope(user_id, label="provider graph relation tenant", maximum=240)
        endpoint = _scope(
            entity_id,
            label="provider graph relation endpoint",
            maximum=240,
        )
        if tenant != self._tenant_id:
            raise MemoryExactStorageError("provider graph relation count is unavailable")
        self._collector.add((endpoint,))
        cap = _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
        public_relation = _not_private_relation_dependency("relation")
        public_source = _not_private_entity_material_dependency("source_entity")
        public_target = _not_private_entity_material_dependency("target_entity")

        def select_rowids(
            *,
            index_name: str,
            predicate: str,
            params: tuple[object, ...],
            limit: int,
        ) -> tuple[int, ...]:
            cursor = self._conn.execute(
                f"""SELECT relation.rowid
                      FROM relations relation INDEXED BY {index_name}
                     WHERE relation.user_id=? AND {predicate}
                     ORDER BY relation.rowid
                     LIMIT {limit}""",  # nosec B608 - fixed keyed predicates/integer cap
                params,
            )
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            rowids = tuple(row[0] for row in rows)
            if (
                any(isinstance(rowid, bool) or not isinstance(rowid, int) for rowid in rowids)
                or len(rowids) != len(set(rowids))
                or any(left >= right for left, right in zip(rowids, rowids[1:], strict=False))
            ):
                raise MemoryExactStorageError("provider graph relation count is invalid")
            self._reserve_bytes(len(rowids) * 72)
            return rowids

        source_rowids = select_rowids(
            index_name="idx_relations_source",
            predicate="relation.source_entity_id=?",
            params=(tenant, endpoint),
            limit=cap + 1,
        )
        if len(source_rowids) == cap + 1:
            raise MemoryExactStorageError("provider graph relation count is saturated")
        target_rowids = select_rowids(
            index_name="idx_relations_target",
            predicate="relation.target_entity_id=? AND relation.source_entity_id<>?",
            params=(tenant, endpoint, endpoint),
            limit=cap - len(source_rowids) + 1,
        )
        count = len(source_rowids) + len(target_rowids)
        if count > cap:
            raise MemoryExactStorageError("provider graph relation count is saturated")
        relation_rowids = (*source_rowids, *target_rowids)
        if not relation_rowids:
            return 0
        relation_values = ",".join("(?)" for _rowid in relation_rowids)
        count_row = self._conn.execute(
            f"""WITH selected(relation_rowid) AS MATERIALIZED (VALUES {relation_values})
                SELECT COUNT(*)
                  FROM selected
                  JOIN relations relation ON relation.rowid=selected.relation_rowid
                  JOIN entities source_entity
                    ON source_entity.id=relation.source_entity_id
                   AND source_entity.user_id=relation.user_id
                   AND {public_source}
                  JOIN entities target_entity
                    ON target_entity.id=relation.target_entity_id
                   AND target_entity.user_id=relation.user_id
                   AND {public_target}
                 WHERE relation.user_id=?
                   AND (relation.source_entity_id=? OR relation.target_entity_id=?)
                   AND relation.deleted_at IS NULL
                   AND {public_relation}""",  # nosec B608 - bounded integer placeholders
            (*relation_rowids, tenant, endpoint, endpoint),
        ).fetchone()
        if (
            count_row is None
            or len(tuple(count_row)) != 1
            or isinstance(count_row[0], bool)
            or not isinstance(count_row[0], int)
            or not 0 <= int(count_row[0]) <= len(relation_rowids)
        ):
            raise MemoryExactStorageError("provider graph relation count is invalid")
        return int(count_row[0])

    def count_entity_knowledge(self, user_id: str, entity_id: str) -> int:
        tenant = _scope(user_id, label="provider graph knowledge tenant", maximum=240)
        endpoint = _scope(
            entity_id,
            label="provider graph knowledge endpoint",
            maximum=240,
        )
        if tenant != self._tenant_id:
            raise MemoryExactStorageError("provider graph knowledge count is unavailable")
        self._collector.add((endpoint,))
        cap = _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
        cursor = self._conn.execute(
            f"""SELECT link.rowid
                  FROM knowledge_entity_links link INDEXED BY idx_links_entity
                 WHERE link.user_id=? AND link.entity_id=? AND link.status='accepted'
                 ORDER BY link.rowid
                 LIMIT {cap + 1}""",  # nosec B608 - fixed keyed predicates/integer cap
            (tenant, endpoint),
        )
        try:
            rows = cursor.fetchall()
        finally:
            cursor.close()
        rowids = tuple(row[0] for row in rows)
        if (
            any(isinstance(rowid, bool) or not isinstance(rowid, int) for rowid in rowids)
            or len(rowids) != len(set(rowids))
            or any(left >= right for left, right in zip(rowids, rowids[1:], strict=False))
        ):
            raise MemoryExactStorageError("provider graph knowledge count is invalid")
        self._reserve_bytes(len(rowids) * 72)
        if len(rowids) == cap + 1:
            raise MemoryExactStorageError("provider graph knowledge count is saturated")
        if not rowids:
            return 0
        link_values = ",".join("(?)" for _rowid in rowids)
        count_row = self._conn.execute(
            f"""WITH selected(link_rowid) AS MATERIALIZED (VALUES {link_values})
                SELECT COUNT(*)
                  FROM selected
                  JOIN knowledge_entity_links link ON link.rowid=selected.link_rowid
                  JOIN knowledge_objects knowledge
                    ON knowledge.id=link.knowledge_object_id
                   AND knowledge.user_id=link.user_id
                  JOIN entities entity
                    ON entity.id=link.entity_id AND entity.user_id=link.user_id
                   AND {_not_private_entity_material_dependency("entity")}
                 WHERE link.user_id=? AND link.entity_id=? AND link.status='accepted'
                   AND knowledge.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("knowledge")}""",  # nosec B608
            (*rowids, tenant, endpoint),
        ).fetchone()
        if (
            count_row is None
            or len(tuple(count_row)) != 1
            or isinstance(count_row[0], bool)
            or not isinstance(count_row[0], int)
            or not 0 <= int(count_row[0]) <= len(rowids)
        ):
            raise MemoryExactStorageError("provider graph knowledge count is invalid")
        return int(count_row[0])

    def list_knowledge_entity_links(
        self,
        user_id: str,
        *,
        entity_id: str | None = None,
        knowledge_object_id: str | None = None,
        status: str | None = "accepted",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tenant = _scope(user_id, label="provider graph link tenant", maximum=240)
        if (
            tenant != self._tenant_id
            or entity_id is not None
            or status != "accepted"
            or limit != 30
            or isinstance(limit, bool)
        ):
            raise MemoryExactStorageError("provider graph link read is unavailable")
        knowledge_id = _scope(
            knowledge_object_id,
            label="provider graph link knowledge identity",
            maximum=240,
        )
        selected = f"""WITH selected AS MATERIALIZED (
            SELECT link.rowid AS link_rowid
              FROM knowledge_entity_links link
              JOIN entities entity
                ON entity.id=link.entity_id AND entity.user_id=link.user_id
              JOIN knowledge_objects knowledge
                ON knowledge.id=link.knowledge_object_id AND knowledge.user_id=link.user_id
             WHERE link.user_id=? AND link.knowledge_object_id=? AND link.status='accepted'
               AND {_not_private_entity_material_dependency("entity")}
               AND {_not_private_knowledge_dependency("knowledge")}
             ORDER BY CASE link.status WHEN 'suggested' THEN 0
                                       WHEN 'accepted' THEN 1 ELSE 2 END,
                      link.confidence DESC,link.created_at DESC LIMIT 30
        )"""  # nosec B608 - fixed code-owned predicates
        preflight = self._conn.execute(
            selected
            + """ SELECT COUNT(*) AS row_count,
                         COALESCE(MAX(max(
                             length(CAST(link.id AS BLOB)),
                             length(CAST(link.entity_id AS BLOB)),
                             length(CAST(link.created_at AS BLOB))
                         )),0) AS maximum_field,
                         COALESCE(SUM(
                             length(CAST(link.id AS BLOB))
                             + length(CAST(link.entity_id AS BLOB))
                             + length(CAST(link.created_at AS BLOB)) + 96
                         ),0) AS storage_bytes,
                         COALESCE(SUM(CASE
                             WHEN typeof(link.id)='text'
                              AND length(CAST(link.id AS BLOB)) BETWEEN 1 AND 240
                              AND typeof(link.entity_id)='text'
                              AND length(CAST(link.entity_id AS BLOB)) BETWEEN 1 AND 240
                              AND typeof(link.created_at)='text'
                              AND length(CAST(link.created_at AS BLOB)) BETWEEN 1 AND 64
                              AND typeof(link.confidence) IN ('integer','real')
                              AND link.confidence=link.confidence
                             THEN 0 ELSE 1 END),0) AS invalid_rows
                    FROM selected
                    JOIN knowledge_entity_links link ON link.rowid=selected.link_rowid""",
            (tenant, knowledge_id),
        ).fetchone()
        if (
            preflight is None
            or any(isinstance(item, bool) or not isinstance(item, int) for item in preflight)
            or not 0 <= int(preflight[0]) <= 30
            or not 0 <= int(preflight[1]) <= 240
            or int(preflight[2]) < 0
            or int(preflight[3]) != 0
        ):
            raise MemoryExactStorageError("provider graph link projection exceeds its limits")
        self._reserve_bytes(int(preflight[2]))
        cursor = self._conn.execute(
            selected
            + """ SELECT link.id,link.entity_id,link.confidence,link.created_at
                    FROM selected
                    JOIN knowledge_entity_links link ON link.rowid=selected.link_rowid
                   ORDER BY CASE link.status WHEN 'suggested' THEN 0
                                             WHEN 'accepted' THEN 1 ELSE 2 END,
                            link.confidence DESC,link.created_at DESC""",
            (tenant, knowledge_id),
        )
        rows = cursor.fetchall()
        cursor.close()
        if len(rows) != int(preflight[0]):
            raise MemoryExactStorageError("provider graph link projection changed")
        self._collector.add(tuple(row[1] for row in rows))
        return [
            {
                "id": row[0],
                "entity_id": row[1],
                "confidence": row[2],
                "created_at": row[3],
            }
            for row in rows
        ]

    def list_entities_knowledge_refs(
        self,
        user_id: str,
        entity_ids: Sequence[str],
        *,
        limit: int = 50,
    ) -> dict[str, list[dict[str, Any]]]:
        from friday.storage._knowledge import KnowledgeMixin

        return KnowledgeMixin.list_entities_knowledge_refs(  # type: ignore[arg-type]
            self,
            user_id,
            entity_ids,
            limit=limit,
        )

    def relation_history_status(self, user_id: str, known_at: str = "") -> dict[str, Any]:
        status_result = _memory_exact_provider_relation_history_status_in_transaction(
            self._conn,
            tenant_id=_scope(user_id, label="provider graph history tenant", maximum=240),
            known_at=known_at,
            candidate_entity_ids=(),
            reserve_bytes=self._reserve_bytes,
            allow_active_managed_context=self._allow_active_managed_context,
            strict_lease=True,
        )
        return status_result[0]

    def _observe_relation_history_boundary(self, boundary: str) -> None:
        _MemoryExactReadOnlyRelationHistoryView(
            self._conn,
            reserve_bytes=self._reserve_bytes,
            allow_active_managed_context=self._allow_active_managed_context,
            strict_lease=True,
        )._observe_relation_history_boundary(boundary)  # noqa: SLF001


def _memory_exact_provider_graph_in_transaction(
    conn: sqlite3.Connection,
    *,
    reserve_bytes: Callable[[int], None],
    collector: _MemoryExactProviderTopologyCollector,
    tenant_id: str,
    candidate_entity_ids: tuple[str, ...],
    allow_active_managed_context: bool,
) -> KnowledgeGraph:
    """Build a KnowledgeGraph inside its caller-owned strict read-only lease."""

    _require_transaction(conn)
    from friday.knowledge_graph import KnowledgeGraph

    try:
        graph = KnowledgeGraph(  # type: ignore[arg-type]
            _MemoryExactProviderGraphSelectView(
                conn,
                reserve_bytes,
                collector=collector,
                tenant_id=tenant_id,
                candidate_entity_ids=candidate_entity_ids,
                allow_active_managed_context=allow_active_managed_context,
            )
        )
    except Exception:
        raise MemoryExactStorageError("provider graph view is unavailable") from None
    if type(graph) is not KnowledgeGraph:
        raise MemoryExactStorageError("provider graph view is invalid")
    return graph


def _replay_memory_exact_provider_graph_operation_in_transaction(
    conn: sqlite3.Connection,
    *,
    storage: object,
    kind: str,
    arguments: tuple[object, ...],
    candidate_entity_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
    allow_active_managed_context: bool = False,
) -> tuple[object, str]:
    """Replay and fully materialize one closed graph operation under one lease."""

    _require_transaction(conn)
    if (
        type(kind) is not str
        or type(arguments) is not tuple
        or type(candidate_entity_ids) is not tuple
        or not callable(reserve_bytes)
        or type(allow_active_managed_context) is not bool
        or getattr(storage, "conn", None) is not conn
    ):
        raise MemoryExactStorageError("provider graph operation is invalid")
    candidates = tuple(
        _scope(item, label="provider graph candidate identity", maximum=240) for item in candidate_entity_ids
    )
    if (
        len(candidates) > _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
        or candidates != tuple(sorted(candidates))
        or len(candidates) != len(set(candidates))
    ):
        raise MemoryExactStorageError("provider graph candidate set is invalid")
    if not arguments:
        raise MemoryExactStorageError("provider graph operation arguments are invalid")
    tenant = _scope(arguments[0], label="provider graph tenant", maximum=240)

    known_at = ""
    if kind == "graph_search_entities":
        if (
            len(arguments) != 4
            or type(arguments[1]) is not str
            or isinstance(arguments[2], bool)
            or not isinstance(arguments[2], int)
            or arguments[3] is not None
        ):
            raise MemoryExactStorageError("provider graph search arguments are invalid")
    elif kind == "graph_context_for_query":
        if (
            len(arguments) != 8
            or type(arguments[1]) is not str
            or any(
                isinstance(arguments[index], bool) or not isinstance(arguments[index], int)
                for index in (2, 3, 4)
            )
            or (arguments[5] is not None and type(arguments[5]) is not tuple)
            or type(arguments[6]) is not str
            or type(arguments[7]) is not str
        ):
            raise MemoryExactStorageError("provider graph context arguments are invalid")
        known_at = arguments[7]
        if known_at:
            known_at = _validated_known_at(
                known_at,
                label="provider graph known_at boundary",
                reject_future=True,
            )
    else:
        raise MemoryExactStorageError("provider graph operation is unavailable")

    global_entity_merge_bound_proof: object | None = None
    if known_at:
        if not _memory_exact_global_entity_merge_bound_in_transaction(
            conn,
            reserve_bytes=reserve_bytes,
        ):
            raise MemoryExactStorageError("provider topology merge history exceeds its limits")
        global_entity_merge_bound_proof = _GLOBAL_ENTITY_MERGE_BOUND_PROOF

    def validate_new_topology(
        identities: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        if not known_at:
            return (
                _sha256(
                    {
                        "schema": "friday.memory-exact-provider-current-topology.v1",
                        "identities": [
                            _hmac(
                                _PROVIDER_SEAL_KEY,
                                domain="friday.memory-exact-provider-topology-entity.v1",
                                material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                            )
                            for identity in identities
                        ],
                    }
                ),
                identities,
            )
        return _memory_exact_provider_scoped_topology_proof_in_transaction(
            conn,
            tenant_id=tenant,
            entity_ids=identities,
            known_at=known_at,
            reserve_bytes=reserve_bytes,
            maximum_identities=MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS,
            allow_later_unwitnessed=False,
            global_entity_merge_bound_proof=global_entity_merge_bound_proof,
        )

    collector = _MemoryExactProviderTopologyCollector(
        maximum=MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS,
        initial_identities=candidates,
        validate=validate_new_topology if known_at else None,
    )
    query_only = conn.execute("PRAGMA query_only").fetchone()
    if (
        query_only is None
        or len(tuple(query_only)) != 1
        or type(query_only[0]) is not int
        or query_only[0] != 0
    ):
        raise MemoryExactStorageError("provider graph read-only lease is unavailable")
    try:
        conn.execute("PRAGMA query_only=ON")
        enabled = conn.execute("PRAGMA query_only").fetchone()
        if enabled is None or len(tuple(enabled)) != 1 or type(enabled[0]) is not int or enabled[0] != 1:
            raise MemoryExactStorageError("provider graph read-only lease is unavailable")
        _install_memory_exact_provider_select_authorizer(conn)
        graph = _memory_exact_provider_graph_in_transaction(
            conn,
            reserve_bytes=reserve_bytes,
            collector=collector,
            tenant_id=tenant,
            candidate_entity_ids=candidates,
            allow_active_managed_context=allow_active_managed_context,
        )
        if kind == "graph_search_entities":
            value = graph.search_entities(
                tenant,
                arguments[1],
                limit=arguments[2],
                entity_type=None,
            )
            if type(value) is not list:
                raise MemoryExactStorageError("provider graph search result is invalid")
            published_ids = tuple(
                _scope(item.get("id"), label="provider graph result identity", maximum=240)
                for item in value
                if type(item) is dict
            )
            if len(published_ids) != len(value):
                raise MemoryExactStorageError("provider graph search result is invalid")
        else:
            seed_ids = None if arguments[5] is None else list(arguments[5])
            value = graph.context_for_query(
                tenant,
                arguments[1],
                depth=arguments[2],
                entity_limit=arguments[3],
                knowledge_limit=arguments[4],
                seed_knowledge_ids=seed_ids,
                as_of=arguments[6],
                known_at=known_at,
            )
            if type(value) is not dict:
                raise MemoryExactStorageError("provider graph context result is invalid")
            published_ids = _graph_entity_ids(value)
        if any(identity not in candidates for identity in published_ids):
            raise MemoryExactStorageError("provider graph result escaped its candidate set")
        collector.add(published_ids)
        reserve_bytes(len(_canonical_bytes(value)))
        topology_ids, topology_proofs = collector.finish()
        proof = _sha256(
            {
                "schema": "friday.memory-exact-provider-graph-topology.v1",
                "candidate_handles": [
                    _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-entity.v1",
                        material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                    )
                    for identity in candidates
                ],
                "topology_handles": [
                    _hmac(
                        _PROVIDER_SEAL_KEY,
                        domain="friday.memory-exact-provider-topology-entity.v1",
                        material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                    )
                    for identity in topology_ids
                ],
                "scoped_proofs": list(topology_proofs),
            }
        )
        return value, proof
    finally:
        from friday.storage._core import _install_private_material_authorizer

        try:
            _install_private_material_authorizer(conn)
            conn.execute("PRAGMA query_only=OFF")
            restored = conn.execute("PRAGMA query_only").fetchone()
            if (
                restored is None
                or len(tuple(restored)) != 1
                or type(restored[0]) is not int
                or restored[0] != 0
            ):
                raise MemoryExactStorageError("provider graph read-only lease restore failed")
        except BaseException as cleanup_error:
            # Best-effort recovery keeps a failed probe from silently poisoning
            # this thread-local connection; the cleanup error still overrides the
            # operation while retaining it as the exception context.
            try:
                conn.set_authorizer(None)
                conn.execute("PRAGMA query_only=OFF")
                _install_private_material_authorizer(conn)
            except BaseException:
                pass
            raise MemoryExactStorageError("provider graph read-only lease restore failed") from cleanup_error


def _memory_exact_provider_graph_candidates_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    reserve_bytes: Callable[[int], None],
) -> tuple[bool, tuple[dict[str, Any], ...]]:
    """Return saturation or the complete bounded graph-search card set."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider graph tenant", maximum=240)
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider graph reservation is unavailable")
    public_entity = _not_private_entity_material_dependency("e")
    cap = _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
    raw_rows = conn.execute(
        f"""SELECT e.rowid FROM entities e
             WHERE e.user_id=? AND e.deleted_at IS NULL AND e.canonical=1
               AND e.merged_into_id IS NULL
               AND {public_entity} AND e.id>?
             ORDER BY e.id LIMIT ?""",  # nosec B608 - module-owned predicate
        (tenant, "", cap + 1),
    ).fetchall()
    rowids = tuple(row[0] for row in raw_rows)
    if (
        len(rowids) > cap + 1
        or any(isinstance(rowid, bool) or not isinstance(rowid, int) or rowid <= 0 for rowid in rowids)
        or len(rowids) != len(set(rowids))
    ):
        raise MemoryExactStorageError("provider graph candidate probe is invalid")
    if len(rowids) == cap + 1:
        return True, ()
    if not rowids:
        return False, ()

    from friday.storage._graph import _entity_search_projection

    holders = ",".join("(?)" for _rowid in rowids)
    projection = _entity_search_projection()
    selected = f"""WITH selected(entity_rowid) AS (VALUES {holders}),
                 cards AS MATERIALIZED (
                     SELECT {projection}
                       FROM selected
                       JOIN entities e ON e.rowid=selected.entity_rowid
                 )"""  # nosec B608 - bounded rowids and code-owned projection
    preflight = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(
                       length(CAST(COALESCE(id,'') AS BLOB))
                       + length(CAST(COALESCE(user_id,'') AS BLOB))
                       + length(CAST(COALESCE(name,'') AS BLOB))
                       + length(CAST(COALESCE(entity_type,'') AS BLOB))
                       + length(CAST(COALESCE(aliases_json,'') AS BLOB))
                       + length(CAST(COALESCE(description,'') AS BLOB))
                       + length(CAST(COALESCE(metadata_json,'') AS BLOB))
                       + length(CAST(COALESCE(canonical,'') AS BLOB))
                       + length(CAST(COALESCE(merged_into_id,'') AS BLOB))
                       + length(CAST(COALESCE(version,'') AS BLOB))
                       + length(CAST(COALESCE(created_at,'') AS BLOB))
                       + length(CAST(COALESCE(updated_at,'') AS BLOB))
                       + length(CAST(COALESCE(deleted_at,'') AS BLOB)) + 256
                   ),0) AS storage_bytes
              FROM cards""",
        rowids,
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider graph preflight is unavailable")
    row_count, storage_bytes = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count != len(rowids)
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
    ):
        raise MemoryExactStorageError("provider graph preflight is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(
        selected + " SELECT * FROM cards ORDER BY id",
        rowids,
    ).fetchall()
    cards = tuple(dict(row) for row in rows)
    if len(cards) != row_count:
        raise MemoryExactStorageError("provider graph candidate set changed")
    return False, cards


def _provider_optional_date(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _bounded_text(
        value,
        label=label,
        maximum=128,
        allow_empty=True,
        allow_controls=False,
    )


def _memory_exact_provider_list_ids_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    limit: int,
    since: str | None,
    until: str | None,
    reserve_bytes: Callable[[int], None],
) -> tuple[str, ...]:
    """Replay the released recent/date-window identity selection."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider list tenant", maximum=240)
    bounded_limit = _integer(
        limit,
        label="provider list limit",
        low=1,
        high=_MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
    )
    lower = _provider_optional_date(since, label="provider list since")
    upper = _provider_optional_date(until, label="provider list until")
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider list reservation is unavailable")
    if lower is not None or upper is not None:
        _require_provider_date_metadata_in_transaction(
            conn,
            tenant_id=tenant,
            uploaded_by=None,
        )

    from friday.storage._knowledge import KnowledgeMixin

    view = _MemoryExactProviderSelectView(conn)
    where, parameters = KnowledgeMixin._knowledge_filter(  # noqa: SLF001
        view,  # type: ignore[arg-type]
        tenant,
        lifecycle_stage=None,
        tag=None,
        entity_id=None,
        query=None,
        since=lower,
        until=upper,
        uploaded_by=None,
    )
    selected = f"""WITH selected AS MATERIALIZED (
        SELECT rowid AS knowledge_rowid FROM knowledge_objects WHERE {where}
         ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ? OFFSET 0
    )"""  # nosec B608 - released predicate and bound parameters only
    selected_parameters = (*parameters, bounded_limit)
    preflight = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                     COALESCE(SUM(length(CAST(k.id AS BLOB)) + 64),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 1 AND 240
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
        selected_parameters,
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider list preflight is unavailable")
    row_count, storage_bytes, invalid_rows = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 0 <= row_count <= bounded_limit
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows != 0
    ):
        raise MemoryExactStorageError("provider list preflight is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(
        selected
        + """ SELECT k.id FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
               ORDER BY k.importance DESC, k.updated_at DESC, k.id DESC""",
        selected_parameters,
    ).fetchall()
    identities = tuple(_scope(row[0], label="provider list knowledge identity", maximum=240) for row in rows)
    if len(identities) != row_count or len(identities) != len(set(identities)):
        raise MemoryExactStorageError("provider list identity selection changed")
    return identities


def _memory_exact_provider_window_ids_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    since: str | None,
    until: str | None,
    reserve_bytes: Callable[[int], None],
) -> set[str] | None:
    """Replay the released all-or-none date-window identity selection."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider window tenant", maximum=240)
    lower = _provider_optional_date(since, label="provider window since")
    upper = _provider_optional_date(until, label="provider window until")
    if not lower and not upper:
        return None
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider window reservation is unavailable")
    _require_provider_date_metadata_in_transaction(
        conn,
        tenant_id=tenant,
        uploaded_by=None,
    )

    from friday.storage._knowledge import KnowledgeMixin

    view = _MemoryExactProviderSelectView(conn)
    where, parameters = KnowledgeMixin._knowledge_filter(  # noqa: SLF001
        view,  # type: ignore[arg-type]
        tenant,
        lifecycle_stage=None,
        tag=None,
        entity_id=None,
        query=None,
        since=lower,
        until=upper,
        uploaded_by=None,
    )
    window_cap = 20_000
    selected = f"""WITH selected AS MATERIALIZED (
        SELECT rowid AS knowledge_rowid FROM knowledge_objects
         WHERE {where} LIMIT ?
    )"""  # nosec B608 - released predicate and bound parameters only
    selected_parameters = (*parameters, window_cap + 1)
    preflight = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                     COALESCE(SUM(length(CAST(k.id AS BLOB)) + 64),0) AS storage_bytes,
                     COALESCE(SUM(CASE
                         WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 1 AND 240
                         THEN 0 ELSE 1 END),0) AS invalid_rows
                FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
        selected_parameters,
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider window preflight is unavailable")
    row_count, storage_bytes, invalid_rows = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 0 <= row_count <= window_cap + 1
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
        or isinstance(invalid_rows, bool)
        or not isinstance(invalid_rows, int)
        or invalid_rows < 0
    ):
        raise MemoryExactStorageError("provider window preflight is invalid")
    if row_count > window_cap:
        return None
    if invalid_rows != 0:
        raise MemoryExactStorageError("provider window identity is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(
        selected
        + """ SELECT k.id FROM selected
                JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
        selected_parameters,
    ).fetchall()
    identities = {_scope(row[0], label="provider window knowledge identity", maximum=240) for row in rows}
    if len(rows) != row_count or len(identities) != row_count:
        raise MemoryExactStorageError("provider window identity selection changed")
    return identities


def _memory_exact_provider_entity_links_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    knowledge_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
) -> dict[str, list[dict[str, Any]]]:
    """Replay bounded accepted entity-label signals used by provider ranking."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider entity-link tenant", maximum=240)
    if type(knowledge_ids) is not tuple or len(knowledge_ids) > 400:
        raise MemoryExactStorageError("provider entity-link source set is invalid")
    identities = tuple(
        _scope(item, label="provider entity-link knowledge identity", maximum=240) for item in knowledge_ids
    )
    if not identities:
        return {}
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider entity-link reservation is unavailable")
    holders = ",".join("?" for _item in identities)
    public_entity = _not_private_entity_material_dependency("e")
    parameters: tuple[object, ...] = (tenant, *identities)
    preflight = conn.execute(
        f"""SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(
                       length(CAST(COALESCE(l.knowledge_object_id,'') AS BLOB))
                       + length(CAST(COALESCE(l.entity_id,'') AS BLOB))
                       + length(CAST(COALESCE(l.confidence,'') AS BLOB))
                       + length(CAST(COALESCE(e.name,'') AS BLOB))
                       + length(CAST(COALESCE(e.entity_type,'') AS BLOB)) + 128
                   ),0) AS storage_bytes
              FROM knowledge_entity_links l
              JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
             WHERE l.user_id=? AND l.status='accepted' AND e.deleted_at IS NULL
               AND {public_entity}
               AND l.knowledge_object_id IN ({holders})""",  # nosec B608
        parameters,
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider entity-link preflight is unavailable")
    row_count, storage_bytes = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
    ):
        raise MemoryExactStorageError("provider entity-link preflight is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(
        f"""SELECT l.knowledge_object_id,
                   substr(l.entity_id,1,160) AS entity_id,
                   l.confidence,
                   substr(e.name,1,240) AS name,
                   substr(e.entity_type,1,80) AS entity_type
              FROM knowledge_entity_links l
              JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
             WHERE l.user_id=? AND l.status='accepted' AND e.deleted_at IS NULL
               AND {public_entity}
               AND l.knowledge_object_id IN ({holders})
             ORDER BY l.confidence DESC,e.name COLLATE NOCASE,
                      l.knowledge_object_id,l.entity_id,l.id""",  # nosec B608
        parameters,
    ).fetchall()
    if len(rows) != row_count:
        raise MemoryExactStorageError("provider entity-link selection changed")
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        knowledge_id = _scope(
            row["knowledge_object_id"],
            label="provider entity-link knowledge identity",
            maximum=240,
        )
        if knowledge_id not in set(identities):
            raise MemoryExactStorageError("provider entity-link escaped its source set")
        entity_id = _scope(
            row["entity_id"],
            label="provider entity-link entity identity",
            maximum=160,
        )
        name = _bounded_text(
            row["name"],
            label="provider entity-link name",
            maximum=240,
            allow_empty=True,
            allow_controls=True,
        )
        entity_type = _bounded_text(
            row["entity_type"],
            label="provider entity-link type",
            maximum=80,
            allow_empty=True,
            allow_controls=False,
        )
        confidence = _finite_number(row["confidence"] or 0.0, label="provider link confidence")
        output.setdefault(knowledge_id, []).append(
            {
                "id": entity_id,
                "name": name,
                "type": entity_type,
                "confidence": confidence,
            }
        )
    return output


def _memory_exact_provider_feedback_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    knowledge_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
) -> dict[str, float]:
    """Replay bounded per-document feedback aggregates used by ranking."""

    _require_transaction(conn)
    tenant = _scope(tenant_id, label="provider feedback tenant", maximum=240)
    if type(knowledge_ids) is not tuple or len(knowledge_ids) > 400:
        raise MemoryExactStorageError("provider feedback source set is invalid")
    identities = tuple(
        _scope(item, label="provider feedback knowledge identity", maximum=240) for item in knowledge_ids
    )
    if not identities:
        return {}
    if not callable(reserve_bytes):
        raise MemoryExactStorageError("provider feedback reservation is unavailable")
    holders = ",".join("?" for _item in identities)
    selected = f"""SELECT target_id,AVG(score) AS score FROM feedback_state
                     WHERE user_id=? AND target_id IN ({holders})
                       AND feedback_type IN ('search_quality','answer_usefulness')
                     GROUP BY target_id"""  # nosec B608
    parameters: tuple[object, ...] = (tenant, *identities)
    preflight = conn.execute(
        f"""WITH selected AS MATERIALIZED ({selected})
            SELECT COUNT(*) AS row_count,
                   COALESCE(SUM(
                       length(CAST(COALESCE(target_id,'') AS BLOB))
                       + length(CAST(COALESCE(score,'') AS BLOB)) + 64
                   ),0) AS storage_bytes
              FROM selected""",  # nosec B608
        parameters,
    ).fetchone()
    if preflight is None:
        raise MemoryExactStorageError("provider feedback preflight is unavailable")
    row_count, storage_bytes = tuple(preflight)
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or not 0 <= row_count <= len(set(identities))
        or isinstance(storage_bytes, bool)
        or not isinstance(storage_bytes, int)
        or storage_bytes < 0
    ):
        raise MemoryExactStorageError("provider feedback preflight is invalid")
    reserve_bytes(storage_bytes)
    rows = conn.execute(selected + " ORDER BY target_id", parameters).fetchall()
    if len(rows) != row_count:
        raise MemoryExactStorageError("provider feedback selection changed")
    result: dict[str, float] = {}
    identity_set = set(identities)
    for row in rows:
        identity = _scope(
            row["target_id"],
            label="provider feedback knowledge identity",
            maximum=240,
        )
        if identity not in identity_set or identity in result:
            raise MemoryExactStorageError("provider feedback escaped its source set")
        result[identity] = _finite_number(
            row["score"] or 0.0,
            label="provider feedback score",
        )
    return result


def _memory_exact_provider_embeddings_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    model: str,
    dim: int,
    limit: int | None,
    reserve_bytes: Callable[[int], None],
) -> list[tuple[str, bytes]]:
    """Replay whole-object embedding bytes through the released SELECT recipe."""

    _reserve_memory_exact_provider_embeddings_in_transaction(
        conn,
        tenant_id=tenant_id,
        model=model,
        dim=dim,
        limit=limit,
        uploaded_by=None,
        reserve_bytes=reserve_bytes,
    )
    from friday.storage._vectors import VectorsMixin

    view = _MemoryExactProviderSelectView(conn)
    rows = VectorsMixin.get_user_embeddings(  # type: ignore[arg-type]
        view,
        tenant_id,
        model,
        dim,
        limit=limit,
        uploaded_by=None,
    )
    return rows


def _memory_exact_provider_chunk_embeddings_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    model: str,
    dim: int,
    object_limit: int | None,
    row_limit: int | None,
    reserve_bytes: Callable[[int], None],
) -> list[tuple[str, bytes]]:
    """Replay passage embedding bytes through the released SELECT recipe."""

    _reserve_memory_exact_provider_chunk_embeddings_in_transaction(
        conn,
        tenant_id=tenant_id,
        model=model,
        dim=dim,
        object_limit=object_limit,
        row_limit=row_limit,
        uploaded_by=None,
        reserve_bytes=reserve_bytes,
    )
    from friday.storage._knowledge import KnowledgeMixin

    view = _MemoryExactProviderSelectView(conn)
    rows = KnowledgeMixin.get_user_chunk_embeddings(  # type: ignore[arg-type]
        view,
        tenant_id,
        model,
        dim,
        object_limit=object_limit,
        row_limit=row_limit,
        uploaded_by=None,
    )
    return rows


def _memory_exact_provider_relation_history_status_replay(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    known_at: str,
    candidate_entity_ids: tuple[str, ...],
    reserve_bytes: Callable[[int], None],
    allow_active_managed_context: bool,
) -> tuple[dict[str, Any], str]:
    tenant = _scope(tenant_id, label="provider relation-history tenant", maximum=240)
    boundary = _bounded_text(
        known_at,
        label="provider relation-history boundary",
        maximum=64,
        allow_empty=True,
        allow_controls=False,
    )
    if boundary:
        return _memory_exact_provider_relation_history_status_in_transaction(
            conn,
            tenant_id=tenant,
            known_at=boundary,
            candidate_entity_ids=candidate_entity_ids,
            reserve_bytes=reserve_bytes,
            allow_active_managed_context=allow_active_managed_context,
        )
    identities = tuple(
        _scope(item, label="provider history candidate identity", maximum=240)
        for item in candidate_entity_ids
    )
    if (
        len(identities) > _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
        or identities != tuple(sorted(identities))
        or len(identities) != len(set(identities))
    ):
        raise MemoryExactStorageError("provider history candidate set is invalid")
    view = _MemoryExactReadOnlyRelationHistoryView(
        conn,
        reserve_bytes=reserve_bytes,
        allow_active_managed_context=allow_active_managed_context,
    )
    marker = view.execute(
        """SELECT CASE
                     WHEN typeof(value)='text' AND length(CAST(value AS BLOB)) BETWEEN 1 AND 64
                     THEN value ELSE NULL END AS value
                 FROM schema_meta WHERE key='relation_history_complete_from'"""
    ).fetchone()
    reserve_bytes(128)
    if marker is None or type(marker[0]) is not str:
        raise MemoryExactStorageError("memory exact provider relation history is unavailable")
    floor = _validated_known_at(
        marker[0],
        label="provider relation-history floor",
        reject_future=False,
    )
    expected = {
        "known_at": "",
        "known_at_floor": floor,
        "history_complete": True,
        "identity_basis": "current_names",
    }
    proof = _sha256(
        {
            "schema": "friday.memory-exact-provider-current-history-scope.v1",
            "candidate_handles": [
                _hmac(
                    _PROVIDER_SEAL_KEY,
                    domain="friday.memory-exact-provider-topology-entity.v1",
                    material=_canonical_bytes({"tenant": tenant, "entity_id": identity}),
                )
                for identity in identities
            ],
        }
    )
    return expected, proof


def _replay_memory_exact_provider_read_in_transaction(
    conn: sqlite3.Connection,
    *,
    kind: str,
    arguments: tuple[object, ...],
    reserve_bytes: Callable[[int], None],
    allow_active_managed_context: bool = False,
) -> object:
    """Replay one closed, bounded provider read without invoking the provider."""

    _require_transaction(conn)
    if (
        type(kind) is not str
        or type(arguments) is not tuple
        or not callable(reserve_bytes)
        or type(allow_active_managed_context) is not bool
    ):
        raise MemoryExactStorageError("provider read witness is invalid")

    if kind == "graph_candidate_cards" and len(arguments) == 1:
        return _memory_exact_provider_graph_candidates_in_transaction(
            conn,
            tenant_id=_scope(
                arguments[0],
                label="provider graph tenant",
                maximum=240,
            ),
            reserve_bytes=reserve_bytes,
        )

    if kind == "search_knowledge" and len(arguments) == 5:
        tenant, query, limit, uploaded_by, fts_available = arguments
        if uploaded_by is not None or type(fts_available) is not bool:
            raise MemoryExactStorageError("provider search witness is invalid")
        text = _private_text(query, label="provider search query", maximum=_MAX_QUERY_BYTES)
        bounded_limit = _integer(limit, label="provider search limit", low=1, high=200)
        return _memory_exact_provider_search_ids_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider search tenant", maximum=240),
            query=text,
            limit=bounded_limit,
            uploaded_by=None,
            fts_available=fts_available,
            reserve_bytes=reserve_bytes,
        )

    if kind == "list_knowledge_objects" and len(arguments) == 10:
        tenant, limit, offset, lifecycle, tag, entity, query, since, until, uploaded_by = arguments
        if (
            offset != 0
            or type(offset) is not int
            or lifecycle is not None
            or tag is not None
            or entity is not None
            or query is not None
            or uploaded_by is not None
        ):
            raise MemoryExactStorageError("provider list witness is invalid")
        return _memory_exact_provider_list_ids_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider list tenant", maximum=240),
            limit=_integer(
                limit,
                label="provider list limit",
                low=1,
                high=_MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
            ),
            since=_provider_optional_date(since, label="provider list since"),
            until=_provider_optional_date(until, label="provider list until"),
            reserve_bytes=reserve_bytes,
        )

    if kind == "knowledge_ids_in_window" and len(arguments) == 4:
        tenant, since, until, uploaded_by = arguments
        if uploaded_by is not None:
            raise MemoryExactStorageError("provider window witness is invalid")
        return _memory_exact_provider_window_ids_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider window tenant", maximum=240),
            since=_provider_optional_date(since, label="provider window since"),
            until=_provider_optional_date(until, label="provider window until"),
            reserve_bytes=reserve_bytes,
        )

    if kind == "provider_rows" and len(arguments) == 2:
        tenant, identities = arguments
        if type(identities) is not tuple:
            raise MemoryExactStorageError("provider material witness is invalid")
        return _load_memory_exact_provider_rows_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider material tenant", maximum=240),
            knowledge_ids=identities,
            reserve_bytes=reserve_bytes,
        )

    if kind == "count_knowledge_objects" and len(arguments) == 2:
        tenant, uploaded_by = arguments
        if uploaded_by is not None:
            raise MemoryExactStorageError("provider count witness is invalid")
        from friday.storage._knowledge import KnowledgeMixin

        view = _MemoryExactProviderSelectView(conn)
        return KnowledgeMixin.count_knowledge_objects(  # type: ignore[arg-type]
            view,
            _scope(tenant, label="provider count tenant", maximum=240),
            uploaded_by=None,
        )

    if kind == "relation_history_status" and len(arguments) == 3:
        tenant, known_at, candidate_entity_ids = arguments
        if type(candidate_entity_ids) is not tuple:
            raise MemoryExactStorageError("provider history candidate set is invalid")
        return _memory_exact_provider_relation_history_status_replay(
            conn,
            tenant_id=_scope(tenant, label="provider history tenant", maximum=240),
            known_at=_bounded_text(
                known_at,
                label="provider history boundary",
                maximum=64,
                allow_empty=True,
                allow_controls=False,
            ),
            candidate_entity_ids=candidate_entity_ids,
            reserve_bytes=reserve_bytes,
            allow_active_managed_context=allow_active_managed_context,
        )

    if kind in {"entity_links_by_document", "feedback_scores"} and len(arguments) == 2:
        tenant, identities = arguments
        if type(identities) is not tuple:
            raise MemoryExactStorageError("provider keyed witness is invalid")
        helper = (
            _memory_exact_provider_entity_links_in_transaction
            if kind == "entity_links_by_document"
            else _memory_exact_provider_feedback_in_transaction
        )
        return helper(
            conn,
            tenant_id=_scope(tenant, label="provider keyed tenant", maximum=240),
            knowledge_ids=identities,
            reserve_bytes=reserve_bytes,
        )

    if kind == "get_user_embeddings" and len(arguments) == 5:
        tenant, model, dim, limit, uploaded_by = arguments
        if uploaded_by is not None:
            raise MemoryExactStorageError("provider vector witness is invalid")
        return _memory_exact_provider_embeddings_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider vector tenant", maximum=240),
            model=_bounded_text(
                model,
                label="provider vector model",
                maximum=512,
                allow_empty=False,
                allow_controls=False,
            ),
            dim=_integer(dim, label="provider vector dimension", low=1, high=1_000_000),
            limit=limit,  # validated by the exact preflight
            reserve_bytes=reserve_bytes,
        )

    if kind == "get_user_chunk_embeddings" and len(arguments) == 6:
        tenant, model, dim, object_limit, row_limit, uploaded_by = arguments
        if uploaded_by is not None:
            raise MemoryExactStorageError("provider chunk-vector witness is invalid")
        return _memory_exact_provider_chunk_embeddings_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider chunk-vector tenant", maximum=240),
            model=_bounded_text(
                model,
                label="provider chunk-vector model",
                maximum=512,
                allow_empty=False,
                allow_controls=False,
            ),
            dim=_integer(
                dim,
                label="provider chunk-vector dimension",
                low=1,
                high=1_000_000,
            ),
            object_limit=object_limit,  # validated by the exact preflight
            row_limit=row_limit,  # validated by the exact preflight
            reserve_bytes=reserve_bytes,
        )

    if kind == "get_knowledge_object" and len(arguments) == 3:
        tenant, knowledge_id, uploaded_by = arguments
        if uploaded_by is not None:
            raise MemoryExactStorageError("provider object witness is invalid")
        return _memory_exact_provider_live_id_in_transaction(
            conn,
            tenant_id=_scope(tenant, label="provider object tenant", maximum=240),
            knowledge_id=_scope(
                knowledge_id,
                label="provider object identity",
                maximum=240,
            ),
            uploaded_by=None,
        )

    raise MemoryExactStorageError("provider read witness operation is unavailable")


def _count_authorized_rows(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    date_window_applied: bool,
) -> int:
    eligible, parameters = _eligible_sql(
        request,
        alias="knowledge",
        date_window_applied=date_window_applied,
    )
    row = conn.execute(
        f"""SELECT COUNT(*)
              FROM knowledge_objects knowledge
              JOIN raw_objects raw
                ON raw.id=knowledge.raw_object_id
               AND raw.user_id=knowledge.user_id
               AND raw.deleted_at IS NULL
               AND {_not_private_raw_dependency("raw")}
             WHERE knowledge.user_id=? AND knowledge.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("knowledge")}
               AND {eligible}""",  # nosec B608
        (request.tenant_id, *parameters),
    ).fetchone()
    if row is None:
        raise MemoryExactStorageError("memory exact authorized total is unavailable")
    return _integer(row[0], label="memory exact authorized total", low=0, high=1_000_000_000)


def _scan_material(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    provider_ids: tuple[str, ...],
    provider_revision_sha256s: tuple[str, ...],
    graph_ids: tuple[str, ...],
    date_window_applied: bool,
    key: bytes,
) -> _MaterialScan:
    prefix, parameters = _selected_material_sql(
        request,
        provider_ids=provider_ids,
        graph_ids=graph_ids,
        date_window_applied=date_window_applied,
    )
    size = _material_size_expression()
    preflight_cursor = conn.execute(
        prefix
        + f""" SELECT COUNT(*) AS row_count,
                       COALESCE(SUM({size}),0) AS aggregate_bytes,
                       COALESCE(MAX({size}),0) AS maximum_row_bytes,
                       COALESCE(MAX(length(CAST(k.content AS BLOB))),0) AS maximum_knowledge_body,
                       COALESCE(MAX(length(CAST(r.raw_content AS BLOB))),0) AS maximum_raw_body,
                       COALESCE(MAX(length(CAST(k.metadata_json AS BLOB))),0) AS maximum_knowledge_metadata,
                       COALESCE(MAX(length(CAST(k.tags_json AS BLOB))),0) AS maximum_tags,
                       COALESCE(MAX(length(CAST(r.metadata_json AS BLOB))),0) AS maximum_raw_metadata,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(k.title,'') AS BLOB)),
                           length(CAST(COALESCE(k.summary,'') AS BLOB)),
                           length(CAST(COALESCE(r.source,'') AS BLOB)),
                           length(CAST(COALESCE(r.source_ref,'') AS BLOB))
                       )),0) AS maximum_wide_field,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(k.id,'') AS BLOB)),
                           length(CAST(COALESCE(k.raw_object_id,'') AS BLOB)),
                           length(CAST(COALESCE(k.entity_id,'') AS BLOB)),
                           length(CAST(COALESCE(k.superseded_by_id,'') AS BLOB))
                       )),0) AS maximum_identity,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(k.content_type,'') AS BLOB)),
                           length(CAST(COALESCE(r.content_type,'') AS BLOB)),
                           length(CAST(COALESCE(r.content_hash,'') AS BLOB))
                       )),0) AS maximum_narrow_field,
                       COALESCE(MAX(length(CAST(COALESCE(k.knowledge_kind,'') AS BLOB))),0)
                           AS maximum_knowledge_kind,
                       COALESCE(MAX(length(CAST(COALESCE(k.lifecycle_stage,'') AS BLOB))),0)
                           AS maximum_lifecycle,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(k.created_at,'') AS BLOB)),
                           length(CAST(COALESCE(k.updated_at,'') AS BLOB)),
                           length(CAST(COALESCE(r.received_at,'') AS BLOB)),
                           length(CAST(COALESCE(r.created_at,'') AS BLOB))
                       )),0) AS maximum_timestamp,
                       COALESCE(SUM(CASE
                           WHEN typeof(k.version)='integer' AND k.version BETWEEN 1 AND 9223372036854775807
                            AND typeof(r.version)='integer' AND r.version BETWEEN 1 AND 9223372036854775807
                            AND typeof(k.importance) IN ('integer','real')
                            AND k.importance BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                            AND typeof(k.quality_score) IN ('integer','real')
                            AND k.quality_score BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                            AND typeof(k.promotion_score) IN ('integer','real')
                            AND k.promotion_score BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308
                           THEN 0 ELSE 1 END),0) AS invalid_numeric_rows
                  FROM selected
                  JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
                  JOIN raw_objects r ON r.rowid=selected.raw_rowid""",  # nosec B608
        parameters,
    )
    raw_preflight = preflight_cursor.fetchone()
    if raw_preflight is None:
        preflight_cursor.close()
        raise MemoryExactStorageError("memory exact material preflight is unavailable")
    preflight = _record(preflight_cursor, raw_preflight)
    preflight_cursor.close()
    integer_fields = (
        "row_count",
        "aggregate_bytes",
        "maximum_row_bytes",
        "maximum_knowledge_body",
        "maximum_raw_body",
        "maximum_knowledge_metadata",
        "maximum_tags",
        "maximum_raw_metadata",
        "maximum_wide_field",
        "maximum_identity",
        "maximum_narrow_field",
        "maximum_knowledge_kind",
        "maximum_lifecycle",
        "maximum_timestamp",
        "invalid_numeric_rows",
    )
    if any(
        isinstance(preflight.get(name), bool) or not isinstance(preflight.get(name), int)
        for name in integer_fields
    ):
        raise MemoryExactStorageError("memory exact material preflight is invalid")
    if (
        not 0
        <= int(preflight["row_count"])
        <= MEMORY_EXACT_MAX_PROVIDER_ROWS + MEMORY_EXACT_MAX_GRAPH_SOURCE_ROWS
        or not 0 <= int(preflight["aggregate_bytes"]) <= MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES
        or not 0 <= int(preflight["maximum_row_bytes"]) <= MEMORY_EXACT_MAX_ROW_UTF8_BYTES
        or not 0 <= int(preflight["maximum_knowledge_body"]) <= MEMORY_EXACT_MAX_BODY_UTF8_BYTES
        or not 0 <= int(preflight["maximum_raw_body"]) <= MEMORY_EXACT_MAX_BODY_UTF8_BYTES
        or not 0 <= int(preflight["maximum_knowledge_metadata"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(preflight["maximum_tags"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(preflight["maximum_raw_metadata"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(preflight["maximum_wide_field"]) <= MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
        or not 0 <= int(preflight["maximum_identity"]) <= 240
        or not 0 <= int(preflight["maximum_narrow_field"]) <= 512
        or not 0 <= int(preflight["maximum_knowledge_kind"]) <= 320
        or not 0 <= int(preflight["maximum_lifecycle"]) <= 80
        or not 0 <= int(preflight["maximum_timestamp"]) <= 64
        or int(preflight["invalid_numeric_rows"]) != 0
    ):
        raise MemoryExactStorageError("memory exact material exceeds its storage limits")

    fields = """selected.ordinal, selected.candidate_rank, selected.graph_source,
                selected.candidate_eligible,
                k.id AS knowledge_id, k.raw_object_id, k.entity_id AS knowledge_entity_id,
                k.content AS knowledge_content, k.content_type AS knowledge_content_type,
                k.title AS knowledge_title, k.summary AS knowledge_summary,
                k.tags_json AS knowledge_tags_json,
                k.metadata_json AS knowledge_metadata_json,
                k.knowledge_kind, k.importance AS knowledge_importance,
                k.quality_score AS knowledge_quality_score,
                k.promotion_score AS knowledge_promotion_score,
                k.lifecycle_stage AS knowledge_lifecycle_stage,
                k.version AS knowledge_version,
                k.superseded_by_id AS knowledge_superseded_by_id,
                k.created_at AS knowledge_created_at,
                k.updated_at AS knowledge_updated_at,
                r.source AS raw_source, r.source_ref AS raw_source_ref,
                r.raw_content, r.content_type AS raw_content_type,
                r.metadata_json AS raw_metadata_json,
                r.content_hash AS raw_content_hash, r.version AS raw_version,
                r.received_at AS raw_received_at, r.created_at AS raw_created_at"""
    cursor = conn.execute(
        prefix
        + f""" SELECT {fields}
                  FROM selected
                  JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
                  JOIN raw_objects r ON r.rowid=selected.raw_rowid
                 ORDER BY selected.ordinal""",  # nosec B608
        parameters,
    )
    by_id: dict[str, _StoredMaterial] = {}
    material_bytes = 0
    try:
        while True:
            batch = cursor.fetchmany(_FETCH_BATCH)
            if not batch:
                break
            for raw_row in batch:
                material = _stored_material(
                    _record(cursor, raw_row),
                    key=key,
                    tenant_id=request.tenant_id,
                )
                if material.knowledge_id in by_id:
                    raise MemoryExactStorageError("memory exact source selection duplicated a row")
                by_id[material.knowledge_id] = material
                material_bytes += material.storage_bytes
                if material_bytes > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                    raise MemoryExactStorageError("memory exact material exceeds its aggregate limit")
    finally:
        cursor.close()
    if len(by_id) != int(preflight["row_count"]) or material_bytes != int(preflight["aggregate_bytes"]):
        raise MemoryExactStorageError("memory exact source changed during selection")
    if len(provider_ids) != len(provider_revision_sha256s):
        raise MemoryExactStorageError("memory exact provider revision ledger is invalid")
    for identity, expected_revision in zip(
        provider_ids,
        provider_revision_sha256s,
        strict=True,
    ):
        material = by_id.get(identity)
        if (
            material is None
            or not isinstance(expected_revision, str)
            or not _SHA256.fullmatch(expected_revision)
            or not hmac.compare_digest(material.knowledge_revision_sha256, expected_revision)
        ):
            raise MemoryExactStorageDrift("memory exact provider source changed after ranking")
    missing_graph = set(graph_ids).difference(by_id)
    if missing_graph:
        raise MemoryExactStorageError("memory exact graph knowledge source is unavailable")
    candidates = tuple(
        by_id[identity]
        for identity in provider_ids
        if identity in by_id and by_id[identity].candidate_eligible
    )
    return _MaterialScan(by_id=by_id, candidates=candidates, snapshot_bytes=material_bytes)


@dataclass(frozen=True, slots=True, repr=False)
class _ExactEntity:
    entity_id: str
    name: str
    entity_type: str
    revision_sha256: str
    source_handle: str


@dataclass(frozen=True, slots=True, repr=False)
class _EndpointEntitySource:
    entity_id: str
    canonical: bool
    merged_into_id: str | None
    deleted_at: str | None
    revision_sha256: str
    source_handle: str


@dataclass(frozen=True, slots=True, repr=False)
class _EndpointResolution:
    stored_entity_id: str
    canonical_entity_id: str
    chain: tuple[_EndpointEntitySource, ...]

    def exact_payload(self) -> list[dict[str, object]]:
        return [
            {
                "source_handle": item.source_handle,
                "revision_sha256": item.revision_sha256,
                "canonical": item.canonical,
                "merged": item.merged_into_id is not None,
                "deleted": item.deleted_at is not None,
            }
            for item in self.chain
        ]


def _endpoint_entity_source(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_id: str,
    key: bytes,
) -> _EndpointEntitySource:
    public = _not_private_entity_material_dependency("entity")
    preflight_cursor = conn.execute(
        f"""SELECT COUNT(*) AS row_count,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(entity.id,'') AS BLOB)),
                       length(CAST(COALESCE(entity.merged_into_id,'') AS BLOB))
                   )),0) AS maximum_identity,
                   COALESCE(MAX(length(CAST(COALESCE(entity.name,'') AS BLOB))),0)
                       AS maximum_name,
                   COALESCE(MAX(length(CAST(COALESCE(entity.normalized_name,'') AS BLOB))),0)
                       AS maximum_normalized_name,
                   COALESCE(MAX(length(CAST(COALESCE(entity.entity_type,'') AS BLOB))),0)
                       AS maximum_type,
                   COALESCE(MAX(length(CAST(COALESCE(entity.aliases_json,'') AS BLOB))),0)
                       AS maximum_aliases,
                   COALESCE(MAX(length(CAST(COALESCE(entity.description,'') AS BLOB))),0)
                       AS maximum_description,
                   COALESCE(MAX(length(CAST(COALESCE(entity.metadata_json,'') AS BLOB))),0)
                       AS maximum_metadata,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(entity.created_at,'') AS BLOB)),
                       length(CAST(COALESCE(entity.updated_at,'') AS BLOB)),
                       length(CAST(COALESCE(entity.deleted_at,'') AS BLOB))
                   )),0) AS maximum_timestamp,
                   COALESCE(SUM(
                       length(CAST(COALESCE(entity.name,'') AS BLOB)) +
                       length(CAST(COALESCE(entity.normalized_name,'') AS BLOB)) +
                       length(CAST(COALESCE(entity.entity_type,'') AS BLOB)) +
                       length(CAST(COALESCE(entity.aliases_json,'') AS BLOB)) +
                       length(CAST(COALESCE(entity.description,'') AS BLOB)) +
                       length(CAST(COALESCE(entity.metadata_json,'') AS BLOB))
                   ),0) AS aggregate_bytes
              FROM entities entity
             WHERE entity.user_id=? AND entity.id=? AND {public}""",  # nosec B608
        (tenant_id, entity_id),
    )
    raw_preflight = preflight_cursor.fetchone()
    if raw_preflight is None:
        preflight_cursor.close()
        raise MemoryExactStorageError("memory exact relation endpoint is unavailable")
    preflight = _record(preflight_cursor, raw_preflight)
    preflight_cursor.close()
    limits = (
        ("row_count", 1),
        ("maximum_identity", 240),
        ("maximum_name", MEMORY_EXACT_MAX_FIELD_UTF8_BYTES),
        ("maximum_normalized_name", MEMORY_EXACT_MAX_FIELD_UTF8_BYTES),
        ("maximum_type", 320),
        ("maximum_aliases", MEMORY_EXACT_MAX_METADATA_UTF8_BYTES),
        ("maximum_description", MEMORY_EXACT_MAX_METADATA_UTF8_BYTES),
        ("maximum_metadata", MEMORY_EXACT_MAX_METADATA_UTF8_BYTES),
        ("maximum_timestamp", 64),
        ("aggregate_bytes", MEMORY_EXACT_MAX_ROW_UTF8_BYTES),
    )
    if (
        any(
            isinstance(preflight.get(name), bool)
            or not isinstance(preflight.get(name), int)
            or not 0 <= int(preflight[name]) <= maximum
            for name, maximum in limits
        )
        or preflight["row_count"] != 1
    ):
        raise MemoryExactStorageError("memory exact relation endpoint is unavailable")
    cursor = conn.execute(
        f"""SELECT entity.id, entity.name, entity.normalized_name, entity.entity_type,
                   entity.aliases_json, entity.description, entity.metadata_json,
                   entity.canonical, entity.merged_into_id, entity.version,
                   entity.created_at, entity.updated_at, entity.deleted_at
              FROM entities entity
             WHERE entity.user_id=? AND entity.id=? AND {public}""",  # nosec B608
        (tenant_id, entity_id),
    )
    raw_row = cursor.fetchone()
    if raw_row is None:
        cursor.close()
        raise MemoryExactStorageError("memory exact relation endpoint changed")
    row = _record(cursor, raw_row)
    if cursor.fetchone() is not None:
        cursor.close()
        raise MemoryExactStorageError("memory exact relation endpoint duplicated")
    cursor.close()
    stored_id = _scope(row["id"], label="stored relation endpoint identity", maximum=240)
    if stored_id != entity_id:
        raise MemoryExactStorageError("memory exact relation endpoint changed identity")
    name = _private_text(
        row["name"], label="stored relation endpoint name", maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
    )
    normalized_name = _private_text(
        row["normalized_name"],
        label="stored relation endpoint normalized name",
        maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
    )
    entity_type = _bounded_text(
        row["entity_type"],
        label="stored relation endpoint type",
        maximum=320,
        allow_empty=False,
        allow_controls=False,
    )
    aliases_json = _json_text(row["aliases_json"], label="stored relation endpoint aliases", expected=list)
    description = _private_text(
        row["description"],
        label="stored relation endpoint description",
        maximum=MEMORY_EXACT_MAX_METADATA_UTF8_BYTES,
    )
    metadata_json = _json_text(row["metadata_json"], label="stored relation endpoint metadata", expected=dict)
    canonical_raw = row["canonical"]
    if canonical_raw not in {0, 1}:
        raise MemoryExactStorageError("stored relation endpoint topology is invalid")
    merged_into_id = _optional_identity(row["merged_into_id"], label="stored relation endpoint merge target")
    deleted_at = None
    if row["deleted_at"] is not None:
        deleted_at = _private_text(
            row["deleted_at"], label="stored relation endpoint deletion timestamp", maximum=64
        )
    version = _integer(row["version"], label="stored relation endpoint version", low=1, high=2**63 - 1)
    created_at = _private_text(
        row["created_at"], label="stored relation endpoint creation timestamp", maximum=64
    )
    updated_at = _private_text(
        row["updated_at"], label="stored relation endpoint update timestamp", maximum=64
    )
    source_handle = _hmac(
        key,
        domain="friday.memory-exact-entity-handle.v1",
        material=_canonical_bytes({"tenant": tenant_id, "entity_id": stored_id}),
    )
    merged_handle = None
    if merged_into_id is not None:
        merged_handle = _hmac(
            key,
            domain="friday.memory-exact-entity-handle.v1",
            material=_canonical_bytes({"tenant": tenant_id, "entity_id": merged_into_id}),
        )
    revision_sha256 = _sha256(
        {
            "schema": "friday.memory-exact-endpoint-entity-revision.v1",
            "name_sha256": _bytes_sha256(name),
            "normalized_name_sha256": _bytes_sha256(normalized_name),
            "entity_type": entity_type,
            "aliases_sha256": _bytes_sha256(aliases_json),
            "description_sha256": _bytes_sha256(description),
            "metadata_sha256": _bytes_sha256(metadata_json),
            "canonical": bool(canonical_raw),
            "merged_into_handle": merged_handle,
            "deleted_at_sha256": (None if deleted_at is None else _bytes_sha256(deleted_at)),
            "version": version,
            "created_at_sha256": _bytes_sha256(created_at),
            "updated_at_sha256": _bytes_sha256(updated_at),
        }
    )
    return _EndpointEntitySource(
        entity_id=stored_id,
        canonical=bool(canonical_raw),
        merged_into_id=merged_into_id,
        deleted_at=deleted_at,
        revision_sha256=revision_sha256,
        source_handle=source_handle,
    )


def _resolve_endpoint(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    stored_entity_id: str,
    expected_canonical_id: str | None,
    key: bytes,
    cache: dict[str, _EndpointEntitySource],
    allow_unresolved: bool = False,
) -> _EndpointResolution | None:
    chain: list[_EndpointEntitySource] = []
    current = stored_entity_id
    visited: set[str] = set()
    while current:
        if current in visited:
            if allow_unresolved:
                return None
            raise MemoryExactStorageError("memory exact relation endpoint merge chain is invalid")
        if len(chain) >= MEMORY_EXACT_MAX_ENTITY_MERGE_DEPTH:
            raise MemoryExactStorageError("memory exact relation endpoint merge chain is too deep")
        visited.add(current)
        source = cache.get(current)
        if source is None:
            if len(cache) >= MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS:
                raise MemoryExactStorageError("memory exact relation endpoint source set is too large")
            if allow_unresolved:
                identity_row = conn.execute(
                    "SELECT user_id FROM entities WHERE id=? LIMIT 1",
                    (current,),
                ).fetchone()
                if identity_row is None:
                    return None
                if identity_row[0] != tenant_id:
                    raise MemoryExactStorageError("memory exact relation endpoint escaped its tenant")
            source = _endpoint_entity_source(conn, tenant_id=tenant_id, entity_id=current, key=key)
            cache[current] = source
        chain.append(source)
        if source.merged_into_id is not None:
            current = source.merged_into_id
            continue
        if source.deleted_at is not None or not source.canonical:
            if allow_unresolved:
                return None
            raise MemoryExactStorageError("memory exact relation endpoint has no live canonical target")
        if expected_canonical_id is not None and source.entity_id != expected_canonical_id:
            raise MemoryExactStorageError("memory exact graph relation endpoint is stale")
        return _EndpointResolution(
            stored_entity_id=stored_entity_id,
            canonical_entity_id=source.entity_id,
            chain=tuple(chain),
        )
    raise MemoryExactStorageError("memory exact relation endpoint merge chain is invalid")


def _graph_entity_ids(graph: Mapping[str, object]) -> tuple[str, ...]:
    result: list[str] = []

    def add(value: object) -> None:
        identity = _scope(value, label="memory exact graph entity identity", maximum=240)
        if identity not in result:
            result.append(identity)

    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            raise MemoryExactStorageError("memory exact graph node changed shape")
        add(node.get("id"))
    for root in graph.get("roots", []):
        if not isinstance(root, dict):
            raise MemoryExactStorageError("memory exact graph root changed shape")
        add(root.get("id"))
    for relation in graph.get("relations", []):
        if not isinstance(relation, dict):
            raise MemoryExactStorageError("memory exact graph relation changed shape")
        add(relation.get("source_entity_id"))
        add(relation.get("target_entity_id"))
    for path in graph.get("paths", []):
        if not isinstance(path, dict):
            raise MemoryExactStorageError("memory exact graph path changed shape")
        for entity_id in path.get("entity_ids", []):
            add(entity_id)
    if len(result) > 128:
        raise MemoryExactStorageError("memory exact graph entity source set is too large")
    return tuple(result)


def _entity_label_claims(graph: Mapping[str, object]) -> dict[str, tuple[str, str]]:
    claims: dict[str, tuple[str, str]] = {}

    def claim(value: object) -> None:
        if not isinstance(value, dict):
            raise MemoryExactStorageError("memory exact graph entity label changed shape")
        identity = str(value["id"])
        label = (str(value["name"]), str(value["entity_type"]))
        previous = claims.get(identity)
        if previous is not None and previous != label:
            raise MemoryExactStorageError("memory exact graph changed an entity label")
        claims[identity] = label

    for node in graph.get("nodes", []):
        claim(node)
    for root in graph.get("roots", []):
        claim(root)
    for path in graph.get("paths", []):
        if not isinstance(path, dict):
            raise MemoryExactStorageError("memory exact graph path changed shape")
        for node in path.get("entities", []):
            claim(node)
    return claims


def _select_entities(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_ids: tuple[str, ...],
    graph: Mapping[str, object],
    key: bytes,
) -> dict[str, _ExactEntity]:
    if not entity_ids:
        return {}
    holders = ",".join("?" for _item in entity_ids)
    public = _not_private_entity_material_dependency("entity")
    preflight_cursor = conn.execute(
        f"""SELECT COUNT(*) AS row_count,
                   COALESCE(MAX(length(CAST(entity.name AS BLOB))),0) AS maximum_name,
                   COALESCE(MAX(length(CAST(entity.entity_type AS BLOB))),0) AS maximum_type,
                   COALESCE(MAX(length(CAST(entity.normalized_name AS BLOB))),0)
                       AS maximum_normalized_name,
                   COALESCE(MAX(length(CAST(entity.aliases_json AS BLOB))),0) AS maximum_aliases,
                   COALESCE(MAX(length(CAST(entity.description AS BLOB))),0) AS maximum_description,
                   COALESCE(MAX(length(CAST(entity.metadata_json AS BLOB))),0) AS maximum_metadata,
                   COALESCE(SUM(length(CAST(COALESCE(entity.name,'') AS BLOB))
                              + length(CAST(COALESCE(entity.normalized_name,'') AS BLOB))
                              + length(CAST(COALESCE(entity.entity_type,'') AS BLOB))
                              + length(CAST(COALESCE(entity.aliases_json,'') AS BLOB))
                              + length(CAST(COALESCE(entity.description,'') AS BLOB))
                              + length(CAST(COALESCE(entity.metadata_json,'') AS BLOB))),0)
                       AS aggregate_bytes,
                   COALESCE(MAX(length(CAST(COALESCE(entity.id,'') AS BLOB))),0)
                       AS maximum_identity,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(entity.created_at,'') AS BLOB)),
                       length(CAST(COALESCE(entity.updated_at,'') AS BLOB))
                   )),0) AS maximum_timestamp
              FROM entities entity
             WHERE entity.user_id=? AND entity.id IN ({holders})
               AND entity.deleted_at IS NULL AND entity.canonical=1
               AND entity.merged_into_id IS NULL AND {public}""",  # nosec B608
        (tenant_id, *entity_ids),
    )
    preflight_raw = preflight_cursor.fetchone()
    if preflight_raw is None:
        preflight_cursor.close()
        raise MemoryExactStorageError("memory exact graph entity preflight is unavailable")
    preflight = _record(preflight_cursor, preflight_raw)
    preflight_cursor.close()
    if (
        preflight.get("row_count") != len(entity_ids)
        or isinstance(preflight.get("maximum_name"), bool)
        or not isinstance(preflight.get("maximum_name"), int)
        or not 0 <= int(preflight["maximum_name"]) <= MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
        or isinstance(preflight.get("maximum_type"), bool)
        or not isinstance(preflight.get("maximum_type"), int)
        or not 0 <= int(preflight["maximum_type"]) <= 320
        or isinstance(preflight.get("maximum_normalized_name"), bool)
        or not isinstance(preflight.get("maximum_normalized_name"), int)
        or not 0 <= int(preflight["maximum_normalized_name"]) <= MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
        or isinstance(preflight.get("maximum_aliases"), bool)
        or not isinstance(preflight.get("maximum_aliases"), int)
        or not 0 <= int(preflight["maximum_aliases"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or isinstance(preflight.get("maximum_description"), bool)
        or not isinstance(preflight.get("maximum_description"), int)
        or not 0 <= int(preflight["maximum_description"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or isinstance(preflight.get("maximum_metadata"), bool)
        or not isinstance(preflight.get("maximum_metadata"), int)
        or not 0 <= int(preflight["maximum_metadata"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or isinstance(preflight.get("aggregate_bytes"), bool)
        or not isinstance(preflight.get("aggregate_bytes"), int)
        or not 0 <= int(preflight["aggregate_bytes"]) <= MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES
        or isinstance(preflight.get("maximum_identity"), bool)
        or not isinstance(preflight.get("maximum_identity"), int)
        or not 0 <= int(preflight["maximum_identity"]) <= 240
        or isinstance(preflight.get("maximum_timestamp"), bool)
        or not isinstance(preflight.get("maximum_timestamp"), int)
        or not 0 <= int(preflight["maximum_timestamp"]) <= 64
    ):
        raise MemoryExactStorageError("memory exact graph entity source is unavailable")
    cursor = conn.execute(
        f"""SELECT entity.id, entity.name, entity.normalized_name,
                   entity.entity_type, entity.aliases_json, entity.description,
                   entity.metadata_json, entity.canonical, entity.merged_into_id,
                   entity.version, entity.created_at, entity.updated_at, entity.deleted_at
              FROM entities entity
             WHERE entity.user_id=? AND entity.id IN ({holders})
               AND entity.deleted_at IS NULL AND entity.canonical=1
               AND entity.merged_into_id IS NULL AND {public}""",  # nosec B608
        (tenant_id, *entity_ids),
    )
    rows = [_record(cursor, raw) for raw in cursor.fetchall()]
    cursor.close()
    if len(rows) != len(entity_ids):
        raise MemoryExactStorageError("memory exact graph entity changed during selection")
    claims = _entity_label_claims(graph)
    result: dict[str, _ExactEntity] = {}
    for row in rows:
        entity_id = _scope(row["id"], label="stored graph entity identity", maximum=240)
        name = _private_text(
            row["name"], label="stored graph entity name", maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
        )
        entity_type = _bounded_text(
            row["entity_type"],
            label="stored graph entity type",
            maximum=320,
            allow_empty=False,
            allow_controls=False,
        )
        projected_entity_type = entity_type[:80]
        normalized_name = _private_text(
            row["normalized_name"],
            label="stored graph normalized entity name",
            maximum=MEMORY_EXACT_MAX_FIELD_UTF8_BYTES,
        )
        aliases_json = _json_text(row["aliases_json"], label="stored graph entity aliases", expected=list)
        description = _private_text(
            row["description"],
            label="stored graph entity description",
            maximum=MEMORY_EXACT_MAX_METADATA_UTF8_BYTES,
        )
        metadata_json = _json_text(row["metadata_json"], label="stored graph entity metadata", expected=dict)
        version = _integer(row["version"], label="stored graph entity version", low=1, high=2**63 - 1)
        _normalized_instant(row["created_at"], label="stored graph entity creation timestamp")
        _normalized_instant(row["updated_at"], label="stored graph entity update timestamp")
        if row["canonical"] != 1 or row["merged_into_id"] is not None or row["deleted_at"] is not None:
            raise MemoryExactStorageError("stored graph entity topology changed")
        claim = claims.get(entity_id)
        # The legacy public graph caps names at 240 and entity types at 80.
        if claim is not None and claim != (name[:240], projected_entity_type):
            raise MemoryExactStorageError("memory exact graph entity label is stale")
        revision = _sha256(
            {
                "schema": "friday.memory-exact-entity-revision.v1",
                "name_sha256": _bytes_sha256(name),
                "normalized_name_sha256": _bytes_sha256(normalized_name),
                "entity_type": entity_type,
                "aliases_sha256": _bytes_sha256(aliases_json),
                "description_sha256": _bytes_sha256(description),
                "metadata_sha256": _bytes_sha256(metadata_json),
                "canonical": True,
                "merged": False,
                "deleted": False,
                "version": version,
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
        )
        handle = _hmac(
            key,
            domain="friday.memory-exact-entity-handle.v1",
            material=_canonical_bytes({"tenant": tenant_id, "entity_id": entity_id}),
        )
        result[entity_id] = _ExactEntity(
            entity_id=entity_id,
            name=name,
            entity_type=projected_entity_type,
            revision_sha256=revision,
            source_handle=handle,
        )
    if set(result) != set(entity_ids):
        raise MemoryExactStorageError("memory exact graph entity source set changed")
    return result


def _validated_known_at(value: object, *, label: str, reject_future: bool) -> str:
    if not isinstance(value, str):
        raise MemoryExactStorageError(f"{label} is invalid")
    try:
        return normalize_known_at(value, reject_future=reject_future)
    except ValueError:
        raise MemoryExactStorageError(f"{label} is invalid") from None


def _historical_entity_topology(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    entity_ids: tuple[str, ...],
    boundary: str,
    key: bytes,
) -> str:
    if not entity_ids:
        return _sha256(
            {"schema": "friday.memory-exact-entity-topology-proof.v1", "merges": [], "versions": []}
        )
    holders = ",".join("?" for _item in entity_ids)
    values = ",".join("(?)" for _item in entity_ids)
    wanted = f"WITH wanted(entity_id) AS (VALUES {values})"  # nosec B608 - placeholders only
    public = _not_private_entity_material_dependency("entity")
    version_saturated, version_rowids = _memory_exact_entity_version_rowids_in_transaction(
        conn,
        entity_ids=entity_ids,
        maximum_rows=MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS,
        reserve_bytes=None,
    )
    if version_saturated:
        raise MemoryExactStorageError("memory exact entity topology history exceeds its limits")
    if not version_rowids:
        raise MemoryExactStorageError("memory exact entity existence history is incomplete")
    version_values = ",".join("(?)" for _rowid in version_rowids)
    version_selected = f"WITH selected(version_rowid) AS MATERIALIZED (VALUES {version_values})"  # nosec B608 - bounded integer placeholders only
    preflight = conn.execute(
        version_selected
        + f""" SELECT COUNT(*) AS row_count,
                   COALESCE(MAX(length(CAST(version.snapshot_json AS BLOB))),0) AS maximum_json,
                   COALESCE(SUM(length(CAST(version.snapshot_json AS BLOB))),0) AS aggregate_json,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(version.id,'') AS BLOB)),
                       length(CAST(COALESCE(version.entity_id,'') AS BLOB))
                   )),0) AS maximum_identity,
                   COALESCE(MAX(length(CAST(COALESCE(version.created_at,'') AS BLOB))),0)
                       AS maximum_timestamp
              FROM selected
              JOIN entity_versions version ON version.rowid=selected.version_rowid
              JOIN entities entity
                ON entity.id=version.entity_id AND entity.user_id=version.user_id
               AND {public}
             WHERE version.user_id=?""",  # nosec B608 - fixed private predicate
        (*version_rowids, tenant_id),
    ).fetchone()
    if (
        preflight is None
        or any(isinstance(item, bool) or not isinstance(item, int) for item in tuple(preflight))
        or not 0 <= int(preflight[0]) <= MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS
        or not 0 <= int(preflight[1]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(preflight[2]) <= MEMORY_EXACT_MAX_ENTITY_HISTORY_UTF8_BYTES
        or not 0 <= int(preflight[3]) <= 240
        or not 0 <= int(preflight[4]) <= 64
    ):
        raise MemoryExactStorageError("memory exact entity topology history exceeds its limits")
    cursor = conn.execute(
        version_selected
        + f""" SELECT version.id AS version_id, version.entity_id, version.version,
                   version.snapshot_json, version.created_at,
                   json_valid(version.snapshot_json) AS snapshot_valid,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_type(version.snapshot_json,'$.canonical') END AS canonical_type,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_extract(version.snapshot_json,'$.canonical') END AS canonical_value,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_type(version.snapshot_json,'$.merged_into_id') END AS merged_type,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_extract(version.snapshot_json,'$.merged_into_id') END AS merged_value,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_type(version.snapshot_json,'$.deleted_at') END AS deleted_type,
                   CASE WHEN json_valid(version.snapshot_json)
                        THEN json_extract(version.snapshot_json,'$.deleted_at') END AS deleted_value
              FROM selected
              JOIN entity_versions version ON version.rowid=selected.version_rowid
              JOIN entities entity
                ON entity.id=version.entity_id AND entity.user_id=version.user_id
               AND {public}
             WHERE version.user_id=?
             ORDER BY version.entity_id, version.version, version.created_at, version.id""",  # nosec B608
        (*version_rowids, tenant_id),
    )
    rows: list[dict[str, Any]] = []
    try:
        while True:
            batch = cursor.fetchmany(64)
            if not batch:
                break
            rows.extend(_record(cursor, raw) for raw in batch)
            if len(rows) > MEMORY_EXACT_MAX_ENTITY_VERSION_ROWS:
                raise MemoryExactStorageError("memory exact entity topology history exceeds its row limit")
    finally:
        cursor.close()
    if len(rows) != int(preflight[0]):
        raise MemoryExactStorageError("memory exact entity topology history changed")
    by_entity: dict[str, list[dict[str, Any]]] = {identity: [] for identity in entity_ids}
    for row in rows:
        identity = str(row["entity_id"])
        if identity in by_entity:
            by_entity[identity].append(row)
    current_cursor = conn.execute(
        f"""SELECT entity.id, entity.canonical, entity.merged_into_id, entity.deleted_at
              FROM entities entity
             WHERE entity.user_id=? AND entity.id IN ({holders})
               AND {public}""",  # nosec B608
        (tenant_id, *entity_ids),
    )
    current_rows = current_cursor.fetchall()
    current_cursor.close()
    current_topology: dict[str, tuple[bool, str, bool]] = {}
    for raw in current_rows:
        identity = _scope(raw[0], label="memory exact current topology identity", maximum=240)
        if raw[1] not in {0, 1}:
            raise MemoryExactStorageError("memory exact current entity topology is invalid")
        merged_id = ""
        if raw[2] is not None:
            merged_id = _scope(raw[2], label="memory exact current topology merge target", maximum=240)
        current_topology[identity] = (bool(raw[1]), merged_id, raw[3] is not None)
    if set(current_topology) != set(entity_ids):
        raise MemoryExactStorageError("memory exact current entity topology source set changed")
    relation_public = _not_private_relation_dependency("revision")
    version_proof: list[dict[str, object]] = []
    recorded_tails: dict[str, tuple[bool, str, bool]] = {}
    for identity, versions in by_entity.items():
        if not versions:
            raise MemoryExactStorageError("memory exact entity existence history is incomplete")
        witnessed = conn.execute(
            f"""SELECT 1 FROM relation_revisions revision
                  JOIN entities source_entity
                    ON source_entity.id=revision.source_entity_id
                   AND source_entity.user_id=revision.user_id
                   AND {_not_private_entity_material_dependency("source_entity")}
                  JOIN entities target_entity
                    ON target_entity.id=revision.target_entity_id
                   AND target_entity.user_id=revision.user_id
                   AND {_not_private_entity_material_dependency("target_entity")}
                 WHERE revision.user_id=? AND revision.recorded_at<=?
                   AND (revision.source_entity_id=? OR revision.target_entity_id=?)
                   AND {relation_public} LIMIT 1""",  # nosec B608
            (tenant_id, boundary, identity, identity),
        ).fetchone()
        previous: tuple[bool, str, bool] | None = None
        first_recorded = ""
        for row in versions:
            version_id = _scope(row["version_id"], label="memory exact entity topology revision", maximum=240)
            version = _integer(
                row["version"],
                label="memory exact entity topology version",
                low=1,
                high=2**63 - 1,
            )
            snapshot_json = _json_text(
                row["snapshot_json"],
                label="memory exact entity topology snapshot",
                expected=dict,
            )
            raw_timestamp = str(row["created_at"] or "")
            recorded = _validated_known_at(
                raw_timestamp, label="memory exact entity topology timestamp", reject_future=False
            )
            if not first_recorded:
                first_recorded = recorded
            if (
                row["snapshot_valid"] != 1
                or row["canonical_type"] not in {"true", "false", "integer"}
                or row["merged_type"] not in {"null", "text"}
                or row["deleted_type"] not in {"null", "text"}
            ):
                raise MemoryExactStorageError("memory exact entity topology history is incomplete")
            topology = (
                bool(row["canonical_value"]),
                str(row["merged_value"] or ""),
                row["deleted_value"] is not None,
            )
            merged_handle = None
            if topology[1]:
                merged_id = _scope(topology[1], label="memory exact historical merge target", maximum=240)
                merged_handle = _hmac(
                    key,
                    domain="friday.memory-exact-entity-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "entity_id": merged_id}),
                )
            version_proof.append(
                {
                    "revision_handle": _hmac(
                        key,
                        domain="friday.memory-exact-entity-version-handle.v1",
                        material=_canonical_bytes({"tenant": tenant_id, "entity_version_id": version_id}),
                    ),
                    "entity_handle": _hmac(
                        key,
                        domain="friday.memory-exact-entity-handle.v1",
                        material=_canonical_bytes({"tenant": tenant_id, "entity_id": identity}),
                    ),
                    "version": version,
                    "snapshot_sha256": _bytes_sha256(snapshot_json),
                    "recorded_at_sha256": _bytes_sha256(raw_timestamp),
                    "canonical": topology[0],
                    "merged_handle": merged_handle,
                    "deleted": topology[2],
                    "witnessed_at_boundary": witnessed is not None,
                }
            )
            coarse_same_second = (
                not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", raw_timestamp) and recorded[:19] == boundary[:19]
            )
            if previous is not None and topology != previous and (recorded > boundary or coarse_same_second):
                raise MemoryExactStorageError("memory exact known_at crosses an entity topology change")
            previous = topology
        if previous is None:
            raise MemoryExactStorageError("memory exact entity topology history is incomplete")
        recorded_tails[identity] = previous
        first_raw = str(versions[0]["created_at"] or "")
        first_coarse = (
            not re.search(r"T\d{2}:\d{2}:\d{2}\.\d+", first_raw) and first_recorded[:19] == boundary[:19]
        )
        if witnessed is None and (first_recorded > boundary or first_coarse):
            raise MemoryExactStorageError("memory exact known_at precedes a selected entity")
    if not _memory_exact_global_entity_merge_bound_in_transaction(
        conn,
        reserve_bytes=None,
    ):
        raise MemoryExactStorageError("memory exact entity merge history exceeds its row limit")
    merge_selected = (
        wanted
        + f""", selected(history_rowid) AS MATERIALIZED (
        SELECT history.rowid
          FROM wanted
          JOIN entity_merge_history history
            ON history.source_entity_id=wanted.entity_id AND history.user_id=?
         ORDER BY history.source_entity_id,history.created_at,history.id
         LIMIT {MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1}
    )"""
    )  # nosec B608 - placeholders and integer cap only
    merge_preflight = conn.execute(
        merge_selected
        + """ SELECT COUNT(*) AS row_count,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(history.id,'') AS BLOB)),
                       length(CAST(COALESCE(history.source_entity_id,'') AS BLOB)),
                       length(CAST(COALESCE(history.target_entity_id,'') AS BLOB)),
                       length(CAST(COALESCE(history.merged_by,'') AS BLOB)),
                       length(CAST(COALESCE(history.undone_by,'') AS BLOB))
                   )),0) AS maximum_identity,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(history.created_at,'') AS BLOB)),
                       length(CAST(COALESCE(history.undone_at,'') AS BLOB))
                   )),0) AS maximum_timestamp,
                   COALESCE(MAX(max(
                       length(CAST(COALESCE(history.source_snapshot_json,'') AS BLOB)),
                       length(CAST(COALESCE(history.target_before_json,'') AS BLOB)),
                       length(CAST(COALESCE(history.target_after_json,'') AS BLOB)),
                       length(CAST(COALESCE(history.transfer_json,'') AS BLOB))
                   )),0) AS maximum_json,
                   COALESCE(SUM(
                       length(CAST(COALESCE(history.source_snapshot_json,'') AS BLOB)) +
                       length(CAST(COALESCE(history.target_before_json,'') AS BLOB)) +
                       length(CAST(COALESCE(history.target_after_json,'') AS BLOB)) +
                       length(CAST(COALESCE(history.transfer_json,'') AS BLOB))
                   ),0) AS aggregate_json
              FROM selected
              JOIN entity_merge_history history ON history.rowid=selected.history_rowid""",
        (*entity_ids, tenant_id),
    ).fetchone()
    if (
        merge_preflight is None
        or any(isinstance(item, bool) or not isinstance(item, int) for item in merge_preflight)
        or not 0 <= int(merge_preflight[0]) <= MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1
        or not 0 <= int(merge_preflight[1]) <= 240
        or not 0 <= int(merge_preflight[2]) <= 64
        or not 0 <= int(merge_preflight[3]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(merge_preflight[4]) <= MEMORY_EXACT_MAX_ENTITY_HISTORY_UTF8_BYTES
    ):
        raise MemoryExactStorageError("memory exact entity merge history exceeds its row limit")
    if int(merge_preflight[0]) == MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS + 1:
        raise MemoryExactStorageError("memory exact entity merge history exceeds its row limit")
    merge_cursor = conn.execute(
        merge_selected
        + """ SELECT history.id AS merge_id, history.source_entity_id,
                   history.target_entity_id, history.source_snapshot_json,
                   history.target_before_json, history.target_after_json,
                   history.transfer_json, history.merged_by, history.created_at,
                   history.undone_at, history.undone_by
              FROM selected
              JOIN entity_merge_history history ON history.rowid=selected.history_rowid
             ORDER BY history.created_at, history.id""",
        (*entity_ids, tenant_id),
    )
    merge_rows: list[dict[str, Any]] = []
    try:
        while True:
            batch = merge_cursor.fetchmany(64)
            if not batch:
                break
            merge_rows.extend(_record(merge_cursor, raw) for raw in batch)
            if len(merge_rows) > MEMORY_EXACT_MAX_ENTITY_MERGE_ROWS:
                raise MemoryExactStorageError("memory exact entity merge history exceeds its row limit")
    finally:
        merge_cursor.close()
    if len(merge_rows) != int(merge_preflight[0]):
        raise MemoryExactStorageError("memory exact entity merge history changed")
    merge_proof: list[dict[str, object]] = []
    active_merges: set[tuple[str, str]] = set()
    for row in merge_rows:
        merge_id = _scope(row["merge_id"], label="memory exact merge identity", maximum=240)
        source_id = _scope(row["source_entity_id"], label="memory exact merge source", maximum=240)
        target_id = _scope(row["target_entity_id"], label="memory exact merge target", maximum=240)
        if source_id == target_id or (row["undone_at"] is None) != (row["undone_by"] is None):
            raise MemoryExactStorageError("memory exact entity merge history is invalid")
        created_at_raw = _private_text(
            row["created_at"], label="memory exact entity merge timestamp", maximum=64
        )
        created_at = _validated_known_at(
            created_at_raw, label="memory exact entity merge timestamp", reject_future=False
        )
        undone_at_raw: str | None = None
        undone_at: str | None = None
        if row["undone_at"] is not None:
            undone_at_raw = _private_text(
                row["undone_at"], label="memory exact entity unmerge timestamp", maximum=64
            )
            undone_at = _validated_known_at(
                undone_at_raw,
                label="memory exact entity unmerge timestamp",
                reject_future=False,
            )
        if (
            created_at > boundary
            or (undone_at is not None and undone_at > boundary)
            or (undone_at is not None and undone_at < created_at)
        ):
            raise MemoryExactStorageError("memory exact known_at crosses an entity merge or unmerge")
        if undone_at is None:
            active_merges.add((source_id, target_id))
        merge_snapshots = {
            field: _json_text(row[field], label=f"memory exact merge {field}", expected=dict)
            for field in (
                "source_snapshot_json",
                "target_before_json",
                "target_after_json",
                "transfer_json",
            )
        }
        merged_by = _scope(row["merged_by"], label="memory exact merge principal", maximum=240)
        undone_by_handle = None
        if row["undone_by"] is not None:
            undone_by = _scope(row["undone_by"], label="memory exact unmerge principal", maximum=240)
            undone_by_handle = _hmac(
                key,
                domain="friday.memory-exact-principal-handle.v1",
                material=_canonical_bytes({"tenant": tenant_id, "principal_id": undone_by}),
            )
        merge_proof.append(
            {
                "merge_handle": _hmac(
                    key,
                    domain="friday.memory-exact-entity-merge-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "entity_merge_id": merge_id}),
                ),
                "source_handle": _hmac(
                    key,
                    domain="friday.memory-exact-entity-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "entity_id": source_id}),
                ),
                "target_handle": _hmac(
                    key,
                    domain="friday.memory-exact-entity-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "entity_id": target_id}),
                ),
                "source_snapshot_sha256": _bytes_sha256(merge_snapshots["source_snapshot_json"]),
                "target_before_sha256": _bytes_sha256(merge_snapshots["target_before_json"]),
                "target_after_sha256": _bytes_sha256(merge_snapshots["target_after_json"]),
                "transfer_sha256": _bytes_sha256(merge_snapshots["transfer_json"]),
                "merged_by_handle": _hmac(
                    key,
                    domain="friday.memory-exact-principal-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "principal_id": merged_by}),
                ),
                "created_at_sha256": _bytes_sha256(created_at_raw),
                "undone_at_sha256": (None if undone_at_raw is None else _bytes_sha256(undone_at_raw)),
                "undone_by_handle": undone_by_handle,
            }
        )
    for identity, recorded in recorded_tails.items():
        current = current_topology[identity]
        recorded_merge = (
            not current[0] and bool(current[1]) and current[2] and (identity, current[1]) in active_merges
        )
        if recorded != current and not recorded_merge:
            raise MemoryExactStorageError(
                "memory exact current entity topology differs from its recorded history"
            )
    return _sha256(
        {
            "schema": "friday.memory-exact-entity-topology-proof.v1",
            "versions": version_proof,
            "merges": merge_proof,
        }
    )


def _validate_temporal_status(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    provider_temporal: Mapping[str, object],
    entity_ids: tuple[str, ...],
    key: bytes,
) -> tuple[MemoryExactTemporalStatus, str]:
    expected_as_of = request.as_of or ""
    expected_known_at = request.known_at or ""
    if (
        provider_temporal.get("as_of") != expected_as_of
        or provider_temporal.get("known_at") != expected_known_at
        or provider_temporal.get("history_complete") is not True
        or provider_temporal.get("identity_basis") != "current_names"
        or provider_temporal.get("temporal_basis") != ("bitemporal" if expected_known_at else "valid_time")
    ):
        raise MemoryExactStorageError("memory exact temporal provider status changed")
    if not expected_known_at:
        if provider_temporal.get("known_at_floor") != "":
            raise MemoryExactStorageError("memory exact current temporal floor is invalid")
        status = MemoryExactTemporalStatus.create(
            as_of=request.as_of,
            history_complete=True,
            identity_basis="current_names",
        )
        return status, _sha256(status.to_model_payload())
    boundary = _validated_known_at(
        expected_known_at, label="memory exact known_at boundary", reject_future=True
    )
    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
    ).fetchone()
    if marker is None or not str(marker[0] or ""):
        raise MemoryExactStorageError("memory exact relation history floor is unavailable")
    floor = _validated_known_at(
        str(marker[0]), label="memory exact relation history floor", reject_future=False
    )
    if floor > boundary or provider_temporal.get("known_at_floor") != floor:
        raise MemoryExactStorageError("memory exact relation history floor changed")
    topology_proof_sha256 = _historical_entity_topology(
        conn,
        tenant_id=request.tenant_id,
        entity_ids=entity_ids,
        boundary=boundary,
        key=key,
    )
    status = MemoryExactTemporalStatus.create(
        as_of=request.as_of,
        known_at=boundary,
        known_at_floor=floor,
        history_complete=True,
        identity_basis="current_names",
    )
    observed_row = conn.execute(
        "SELECT observed_at FROM relation_revision_context WHERE singleton=1"
    ).fetchone()
    if observed_row is None:
        raise MemoryExactStorageError("memory exact relation history observation is unavailable")
    observed_at = _validated_known_at(
        str(observed_row[0] or ""),
        label="memory exact relation history observation",
        reject_future=False,
    )
    if observed_at < boundary:
        raise MemoryExactStorageError("memory exact relation history boundary was not observed")
    watermark_row = conn.execute(
        f"""SELECT COALESCE(MAX(revision.event_seq),0)
              FROM relation_revisions revision
              JOIN entities source_entity
                ON source_entity.id=revision.source_entity_id
               AND source_entity.user_id=revision.user_id
               AND {_not_private_entity_material_dependency("source_entity")}
              JOIN entities target_entity
                ON target_entity.id=revision.target_entity_id
               AND target_entity.user_id=revision.user_id
               AND {_not_private_entity_material_dependency("target_entity")}
             WHERE revision.user_id=? AND revision.recorded_at<=?
               AND {_not_private_relation_dependency("revision")}""",  # nosec B608
        (request.tenant_id, boundary),
    ).fetchone()
    watermark = (
        0
        if watermark_row is None
        else _integer(
            watermark_row[0], label="memory exact relation history watermark", low=0, high=2**63 - 1
        )
    )
    return status, _sha256(
        {
            "status": status.to_model_payload(),
            "watermark": watermark,
            "topology_proof_sha256": topology_proof_sha256,
        }
    )


@dataclass(frozen=True, slots=True, repr=False)
class _ExactRelation:
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    valid_from: str
    valid_to: str | None
    revision_sha256: str
    source_handle: str
    evidence_basis: MemoryExactGraphEvidenceBasis
    evidence_knowledge_id: str | None = None
    implicit: bool = False


def _relation_claims(graph: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    claims: dict[str, list[dict[str, object]]] = {}
    for raw in graph.get("relations", []):
        if not isinstance(raw, dict):
            raise MemoryExactStorageError("memory exact graph relation changed shape")
        claims.setdefault(str(raw["id"]), []).append(raw)
    for path in graph.get("paths", []):
        if not isinstance(path, dict):
            raise MemoryExactStorageError("memory exact graph path changed shape")
        for raw_edge in path.get("edges", []):
            if not isinstance(raw_edge, dict):
                raise MemoryExactStorageError("memory exact graph edge changed shape")
            edge = dict(raw_edge)
            edge["source_entity_id"] = edge["source"]
            edge["target_entity_id"] = edge["target"]
            edge["relation_type"] = edge["type"]
            claims.setdefault(str(edge["id"]), []).append(edge)
    for relation_id, sightings in claims.items():
        first = sightings[0]
        core = (
            first["source_entity_id"],
            first["target_entity_id"],
            first["relation_type"],
            first["implicit"],
            first.get("valid_from", ""),
            first.get("valid_to"),
        )
        if any(
            (
                item["source_entity_id"],
                item["target_entity_id"],
                item["relation_type"],
                item["implicit"],
                item.get("valid_from", ""),
                item.get("valid_to"),
            )
            != core
            for item in sightings[1:]
        ):
            raise MemoryExactStorageError("memory exact graph relation changed between projections")
        if relation_id != str(first["id"]):
            raise MemoryExactStorageError("memory exact graph relation identity changed")
    return claims


def _explicit_relation_query(
    request: MemoryExactRequest,
    relation_ids: tuple[str, ...],
) -> tuple[str, tuple[object, ...]]:
    holders = ",".join("?" for _item in relation_ids)
    source_public = _not_private_entity_material_dependency("source_entity")
    target_public = _not_private_entity_material_dependency("target_entity")
    if request.known_at:
        validity = "relation.valid_to IS NULL"
        validity_parameters: tuple[object, ...] = ()
        if request.as_of:
            validity = "(relation.valid_from='' OR relation.valid_from<=?) AND (relation.valid_to IS NULL OR relation.valid_to>?)"
            validity_parameters = (request.as_of, request.as_of)
        sql = f"""WITH ranked AS MATERIALIZED (
            SELECT revision.event_seq,
                   ROW_NUMBER() OVER (
                       PARTITION BY revision.relation_id
                       ORDER BY revision.recorded_at DESC, revision.event_seq DESC
                   ) AS snapshot_rank
              FROM relation_revisions revision
             WHERE revision.user_id=? AND revision.recorded_at<=?
               AND revision.relation_id IN ({holders})
        ), selected_keys AS MATERIALIZED (
            SELECT event_seq FROM ranked WHERE snapshot_rank=1
        )
        SELECT relation.relation_id AS relation_id, relation.event_seq,
               relation.revision AS relation_revision, relation.recorded_at,
               relation.present, relation.operation, relation.batch_id,
               relation.history_quality,
               relation.source_entity_id, relation.target_entity_id,
               relation.relation_type, relation.weight, relation.metadata_json,
               relation.created_at, relation.deleted_at, relation.valid_from,
               relation.valid_to, relation.invalidated_at, relation.superseded_by
          FROM selected_keys selected
          JOIN relation_revisions relation ON relation.event_seq=selected.event_seq
          JOIN entities source_entity
            ON source_entity.id=relation.source_entity_id
           AND source_entity.user_id=relation.user_id
           AND {source_public}
          JOIN entities target_entity
            ON target_entity.id=relation.target_entity_id
           AND target_entity.user_id=relation.user_id
           AND {target_public}
         WHERE relation.user_id=? AND relation.present=1 AND relation.deleted_at IS NULL
           AND {validity} AND {_not_private_relation_dependency("relation")}"""  # nosec B608
        return (
            sql,
            (
                request.tenant_id,
                request.known_at,
                *relation_ids,
                request.tenant_id,
                *validity_parameters,
            ),
        )
    validity = "relation.valid_to IS NULL"
    validity_parameters = ()
    if request.as_of:
        validity = "(relation.valid_from='' OR relation.valid_from<=?) AND (relation.valid_to IS NULL OR relation.valid_to>?)"
        validity_parameters = (request.as_of, request.as_of)
    sql = f"""SELECT relation.id AS relation_id, 0 AS event_seq,
                    0 AS relation_revision, '' AS recorded_at,
                    1 AS present, 'current_projection' AS operation,
                    '' AS batch_id, 'current_projection' AS history_quality,
                    relation.source_entity_id, relation.target_entity_id,
                    relation.relation_type, relation.weight, relation.metadata_json,
                    relation.created_at, relation.deleted_at, relation.valid_from,
                    relation.valid_to, relation.invalidated_at, relation.superseded_by
               FROM relations relation
               JOIN entities source_entity
                 ON source_entity.id=relation.source_entity_id
                AND source_entity.user_id=relation.user_id
                AND {source_public}
               JOIN entities target_entity
                 ON target_entity.id=relation.target_entity_id
                AND target_entity.user_id=relation.user_id
                AND {target_public}
              WHERE relation.user_id=? AND relation.id IN ({holders})
                AND relation.deleted_at IS NULL AND {validity}
                AND {_not_private_relation_dependency("relation")}"""  # nosec B608
    return sql, (request.tenant_id, *relation_ids, *validity_parameters)


def _prepare_explicit_endpoint_resolutions(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    claims: Mapping[str, list[dict[str, object]]],
    key: bytes,
) -> dict[str, tuple[_EndpointResolution, _EndpointResolution]]:
    relation_ids = tuple(
        relation_id for relation_id, items in claims.items() if items[0]["implicit"] is False
    )
    if not relation_ids:
        return {}
    full_sql, parameters = _explicit_relation_query(request, relation_ids)
    identity_sql = (
        "SELECT source.relation_id, source.source_entity_id, source.target_entity_id "
        f"FROM ({full_sql}) source"
    )
    preflight = conn.execute(
        "SELECT COUNT(*) AS row_count, COALESCE(MAX(max("
        "length(CAST(COALESCE(source.relation_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.source_entity_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.target_entity_id,'') AS BLOB))"
        ")),0) AS maximum_identity "
        f"FROM ({identity_sql}) source",  # nosec B608
        parameters,
    ).fetchone()
    if (
        preflight is None
        or any(isinstance(item, bool) or not isinstance(item, int) for item in preflight)
        or preflight[0] != len(relation_ids)
        or not 0 <= int(preflight[1]) <= 768
    ):
        raise MemoryExactStorageError("memory exact explicit relation source is unavailable")
    cursor = conn.execute(identity_sql, parameters)
    rows = [_record(cursor, raw) for raw in cursor.fetchall()]
    cursor.close()
    if len(rows) != len(relation_ids):
        raise MemoryExactStorageError("memory exact explicit relation source changed")
    cache: dict[str, _EndpointEntitySource] = {}
    result: dict[str, tuple[_EndpointResolution, _EndpointResolution]] = {}
    for row in rows:
        relation_id = _scope(row["relation_id"], label="stored graph relation identity", maximum=768)
        sightings = claims.get(relation_id)
        if not sightings:
            raise MemoryExactStorageError("memory exact graph relation escaped its source set")
        raw_source = _scope(row["source_entity_id"], label="stored relation source", maximum=240)
        raw_target = _scope(row["target_entity_id"], label="stored relation target", maximum=240)
        expected_source = _scope(
            sightings[0]["source_entity_id"],
            label="memory exact graph relation source",
            maximum=240,
        )
        expected_target = _scope(
            sightings[0]["target_entity_id"],
            label="memory exact graph relation target",
            maximum=240,
        )
        source_resolution = _resolve_endpoint(
            conn,
            tenant_id=request.tenant_id,
            stored_entity_id=raw_source,
            expected_canonical_id=expected_source,
            key=key,
            cache=cache,
        )
        target_resolution = _resolve_endpoint(
            conn,
            tenant_id=request.tenant_id,
            stored_entity_id=raw_target,
            expected_canonical_id=expected_target,
            key=key,
            cache=cache,
        )
        if source_resolution is None or target_resolution is None:
            raise MemoryExactStorageError("memory exact relation endpoint proof is unavailable")
        if source_resolution.canonical_entity_id == target_resolution.canonical_entity_id:
            raise MemoryExactStorageError("memory exact graph relation collapsed after a merge")
        result[relation_id] = (source_resolution, target_resolution)
    if set(result) != set(relation_ids):
        raise MemoryExactStorageError("memory exact explicit relation source set changed")
    return result


def _compare_relation_claim(
    claim: Mapping[str, object],
    row: Mapping[str, object],
    entities: Mapping[str, _ExactEntity],
    expected_provenance: Mapping[str, str],
) -> None:
    if (
        claim["source_entity_id"] != row["source_entity_id"]
        or claim["target_entity_id"] != row["target_entity_id"]
        or claim["relation_type"] != row["relation_type"]
        or claim.get("valid_from", "") != (row["valid_from"] or "")
        or claim.get("valid_to") != row["valid_to"]
    ):
        raise MemoryExactStorageError("memory exact graph relation source is stale")
    optional = {
        "created_at": row["created_at"],
        "invalidated_at": row["invalidated_at"],
    }
    for field, expected in optional.items():
        if field in claim and claim[field] != expected:
            raise MemoryExactStorageError("memory exact graph relation provenance is stale")
    if "updated_at" in claim:
        raise MemoryExactStorageError("memory exact graph relation provenance is stale")
    provenance = claim.get("provenance")
    if provenance is not None and provenance != expected_provenance:
        raise MemoryExactStorageError("memory exact graph relation evidence is stale")
    expected_knowledge = expected_provenance.get("knowledge_object_id")
    for field in ("knowledge_object_id", "evidence_knowledge_object_id"):
        if field in claim and claim[field] != expected_knowledge:
            raise MemoryExactStorageError("memory exact graph relation evidence is stale")
    if "weight" in claim:
        stored_weight = float(row["weight"])
        expected_weight = max(0.0, min(1.5, stored_weight)) if "direction" in claim else stored_weight
        if float(claim["weight"]) != expected_weight:
            raise MemoryExactStorageError("memory exact graph relation weight is stale")
    source = entities[str(row["source_entity_id"])]
    target = entities[str(row["target_entity_id"])]
    if "source_name" in claim and claim["source_name"] != source.name[:240]:
        raise MemoryExactStorageError("memory exact graph relation source label is stale")
    if "target_name" in claim and claim["target_name"] != target.name[:240]:
        raise MemoryExactStorageError("memory exact graph relation target label is stale")


def _stored_relation_is_reviewed(metadata: Mapping[str, object]) -> bool:
    raw_origin = metadata.get("origin")
    origin = raw_origin.strip()[:80] if isinstance(raw_origin, str) else ""
    return (
        origin == "review"
        and metadata.get("source") == "reviewed_relation_candidate"
        and isinstance(metadata.get("candidate_id"), str)
        and bool(str(metadata.get("candidate_id") or "").strip())
        and isinstance(metadata.get("reviewed_by"), str)
        and bool(str(metadata.get("reviewed_by") or "").strip())
    )


def _stored_relation_provenance(metadata: Mapping[str, object]) -> dict[str, str]:
    """Rebuild the allowlisted provenance carried by released graph path edges."""

    result: dict[str, str] = {}
    raw_origin = metadata.get("origin")
    origin = raw_origin.strip()[:80] if isinstance(raw_origin, str) else ""
    if origin:
        result["origin"] = origin
    trusted_review = _stored_relation_is_reviewed(metadata)
    if not trusted_review:
        return result
    raw_source = metadata.get("source")
    if isinstance(raw_source, str) and raw_source.strip():
        result["source"] = raw_source.strip()[:160]
    evidence = metadata.get("evidence")
    raw_knowledge = evidence.get("knowledge_object_id") if isinstance(evidence, dict) else None
    if not isinstance(raw_knowledge, str) or not raw_knowledge.strip():
        raw_knowledge = metadata.get("knowledge_object_id")
    if isinstance(raw_knowledge, str) and raw_knowledge.strip():
        result["knowledge_object_id"] = raw_knowledge.strip()[:160]
    return result


def _select_explicit_relations(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    claims: Mapping[str, list[dict[str, object]]],
    entities: Mapping[str, _ExactEntity],
    endpoint_resolutions: Mapping[str, tuple[_EndpointResolution, _EndpointResolution]],
    key: bytes,
) -> dict[str, _ExactRelation]:
    relation_ids = tuple(
        relation_id for relation_id, items in claims.items() if items[0]["implicit"] is False
    )
    if not relation_ids:
        return {}
    sql, parameters = _explicit_relation_query(request, relation_ids)
    preflight_cursor = conn.execute(
        "SELECT COUNT(*) AS row_count, "
        "COALESCE(MAX(length(CAST(source.metadata_json AS BLOB))),0) AS maximum_metadata, "
        "COALESCE(SUM(length(CAST(source.metadata_json AS BLOB))),0) AS aggregate_metadata, "
        "COALESCE(MAX(max("
        "length(CAST(COALESCE(source.relation_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.source_entity_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.target_entity_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.relation_type,'') AS BLOB)),"
        "length(CAST(COALESCE(source.operation,'') AS BLOB)),"
        "length(CAST(COALESCE(source.batch_id,'') AS BLOB)),"
        "length(CAST(COALESCE(source.history_quality,'') AS BLOB)),"
        "length(CAST(COALESCE(source.created_at,'') AS BLOB)),"
        "length(CAST(COALESCE(source.recorded_at,'') AS BLOB)),"
        "length(CAST(COALESCE(source.valid_from,'') AS BLOB)),"
        "length(CAST(COALESCE(source.valid_to,'') AS BLOB)),"
        "length(CAST(COALESCE(source.invalidated_at,'') AS BLOB)),"
        "length(CAST(COALESCE(source.superseded_by,'') AS BLOB))"
        ")),0) AS maximum_field "
        f"FROM ({sql}) source",  # nosec B608
        parameters,
    )
    raw_preflight = preflight_cursor.fetchone()
    if raw_preflight is None:
        preflight_cursor.close()
        raise MemoryExactStorageError("memory exact graph relation preflight is unavailable")
    preflight = _record(preflight_cursor, raw_preflight)
    preflight_cursor.close()
    if (
        preflight.get("row_count") != len(relation_ids)
        or isinstance(preflight.get("maximum_metadata"), bool)
        or not isinstance(preflight.get("maximum_metadata"), int)
        or not 0 <= int(preflight["maximum_metadata"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or isinstance(preflight.get("aggregate_metadata"), bool)
        or not isinstance(preflight.get("aggregate_metadata"), int)
        or not 0 <= int(preflight["aggregate_metadata"]) <= 4 * MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or isinstance(preflight.get("maximum_field"), bool)
        or not isinstance(preflight.get("maximum_field"), int)
        or not 0 <= int(preflight["maximum_field"]) <= MEMORY_EXACT_MAX_FIELD_UTF8_BYTES
    ):
        raise MemoryExactStorageError("memory exact explicit relation source is unavailable")
    cursor = conn.execute(sql, parameters)
    rows = [_record(cursor, raw) for raw in cursor.fetchall()]
    cursor.close()
    if len(rows) != len(relation_ids):
        raise MemoryExactStorageError("memory exact explicit relation changed during selection")
    result: dict[str, _ExactRelation] = {}
    for row in rows:
        relation_id = _scope(row["relation_id"], label="stored graph relation identity", maximum=768)
        if relation_id not in claims:
            raise MemoryExactStorageError("memory exact graph relation escaped its source set")
        raw_source_id = _scope(row["source_entity_id"], label="stored relation source", maximum=240)
        raw_target_id = _scope(row["target_entity_id"], label="stored relation target", maximum=240)
        resolutions = endpoint_resolutions.get(relation_id)
        if resolutions is None:
            raise MemoryExactStorageError("memory exact relation endpoint proof is unavailable")
        source_resolution, target_resolution = resolutions
        if (
            source_resolution.stored_entity_id != raw_source_id
            or target_resolution.stored_entity_id != raw_target_id
        ):
            raise MemoryExactStorageError("memory exact relation endpoint source changed")
        source_id = source_resolution.canonical_entity_id
        target_id = target_resolution.canonical_entity_id
        if source_id not in entities or target_id not in entities:
            raise MemoryExactStorageError("memory exact graph relation escaped its entity scope")
        relation_type = _bounded_text(
            row["relation_type"],
            label="stored relation type",
            maximum=320,
            allow_empty=False,
            allow_controls=False,
        )
        projected_relation_type = relation_type[:80]
        valid_from = _bounded_text(
            row["valid_from"] or "",
            label="stored relation valid_from",
            maximum=64,
            allow_controls=False,
        )
        valid_to = row["valid_to"]
        if valid_to is not None:
            valid_to = _bounded_text(
                valid_to,
                label="stored relation valid_to",
                maximum=64,
                allow_empty=False,
                allow_controls=False,
            )
        metadata = _json_text(row["metadata_json"], label="stored relation metadata", expected=dict)
        metadata_object = _strict_json(metadata, label="stored relation metadata")
        if not isinstance(metadata_object, dict):
            raise MemoryExactStorageError("stored relation metadata changed shape")
        trusted_review = _stored_relation_is_reviewed(metadata_object)
        expected_provenance = _stored_relation_provenance(metadata_object)
        raw_evidence_knowledge_id = expected_provenance.get("knowledge_object_id")
        evidence_knowledge_id = (
            None
            if raw_evidence_knowledge_id is None
            else _scope(
                raw_evidence_knowledge_id,
                label="stored relation evidence knowledge",
                maximum=240,
            )
        )
        evidence_basis = (
            MemoryExactGraphEvidenceBasis.REVIEWED_RELATION
            if trusted_review
            else MemoryExactGraphEvidenceBasis.RELATION_ROW_ONLY
        )
        evidence_knowledge_handle = (
            None
            if evidence_knowledge_id is None
            else _hmac(
                key,
                domain="friday.memory-exact-evidence-knowledge-handle.v1",
                material=_canonical_bytes(
                    {
                        "tenant": request.tenant_id,
                        "knowledge_id": evidence_knowledge_id,
                    }
                ),
            )
        )
        weight = _finite_number(row["weight"], label="stored relation weight")
        projected_row = {
            **row,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "relation_type": projected_relation_type,
        }
        for claim in claims[relation_id]:
            _compare_relation_claim(claim, projected_row, entities, expected_provenance)
        historical = request.known_at is not None
        event_sequence = _integer(
            row["event_seq"],
            label="stored relation event sequence",
            low=1 if historical else 0,
            high=2**63 - 1,
        )
        if not historical and event_sequence != 0:
            raise MemoryExactStorageError("stored current relation provenance is invalid")
        relation_revision = _integer(
            row["relation_revision"],
            label="stored relation revision number",
            low=0,
            high=2**63 - 1,
        )
        present = _integer(row["present"], label="stored relation presence", low=1, high=1)
        operation = _bounded_text(
            row["operation"],
            label="stored relation operation",
            maximum=80,
            allow_empty=False,
            allow_controls=False,
        )
        history_quality = _bounded_text(
            row["history_quality"],
            label="stored relation history quality",
            maximum=80,
            allow_empty=False,
            allow_controls=False,
        )
        recorded_at_sha256 = None
        if historical:
            if operation not in {"insert", "update", "migration_baseline"} or history_quality not in {
                "captured",
                "migration_baseline",
            }:
                raise MemoryExactStorageError("stored relation history provenance is invalid")
            batch_id = _scope(row["batch_id"], label="stored relation history batch", maximum=240)
            recorded_at_raw = _private_text(
                row["recorded_at"],
                label="stored relation recorded timestamp",
                maximum=64,
            )
            recorded_at = _validated_known_at(
                recorded_at_raw,
                label="stored relation recorded timestamp",
                reject_future=False,
            )
            known_at = _validated_known_at(
                request.known_at,
                label="memory exact known_at boundary",
                reject_future=False,
            )
            if recorded_at > known_at:
                raise MemoryExactStorageError("stored relation escaped its known_at boundary")
            recorded_at_sha256 = _bytes_sha256(recorded_at_raw)
        else:
            if (
                operation != "current_projection"
                or history_quality != "current_projection"
                or row["batch_id"] != ""
                or row["recorded_at"] != ""
            ):
                raise MemoryExactStorageError("stored current relation provenance is invalid")
            batch_id = ""
        created_at = _private_text(row["created_at"], label="stored relation creation timestamp", maximum=64)
        if row["deleted_at"] is not None:
            raise MemoryExactStorageError("stored graph relation was deleted")
        invalidated_at = row["invalidated_at"]
        if invalidated_at is not None:
            invalidated_at = _bounded_text(
                invalidated_at,
                label="stored relation invalidation timestamp",
                maximum=64,
                allow_empty=False,
                allow_controls=False,
            )
        superseding_handle = None
        if row["superseded_by"] is not None:
            superseding_id = _scope(row["superseded_by"], label="stored superseding relation", maximum=240)
            superseding_handle = _hmac(
                key,
                domain="friday.memory-exact-relation-handle.v1",
                material=_canonical_bytes({"tenant": request.tenant_id, "relation_id": superseding_id}),
            )
        revision = _sha256(
            {
                "schema": _RELATION_REVISION_SCHEMA,
                "basis": "known_at_revision" if historical else "current_projection",
                "source_handle": entities[source_id].source_handle,
                "target_handle": entities[target_id].source_handle,
                "stored_source_resolution": source_resolution.exact_payload(),
                "stored_target_resolution": target_resolution.exact_payload(),
                "relation_type": relation_type,
                "weight": weight,
                "metadata_sha256": _bytes_sha256(metadata),
                "created_at_sha256": _bytes_sha256(created_at),
                "deleted": False,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "invalidated_at_sha256": (None if invalidated_at is None else _bytes_sha256(invalidated_at)),
                "superseding_handle": superseding_handle,
                "event_sequence": event_sequence,
                "relation_revision": relation_revision,
                "present": present,
                "operation": operation,
                "batch_sha256": None if not batch_id else _bytes_sha256(batch_id),
                "history_quality": history_quality,
                "recorded_at_sha256": recorded_at_sha256,
                "evidence_basis": evidence_basis.value,
                "evidence_knowledge_handle": evidence_knowledge_handle,
            }
        )
        handle = _hmac(
            key,
            domain="friday.memory-exact-relation-handle.v1",
            material=_canonical_bytes({"tenant": request.tenant_id, "relation_id": relation_id}),
        )
        result[relation_id] = _ExactRelation(
            relation_id=relation_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type=projected_relation_type,
            valid_from=valid_from,
            valid_to=valid_to,
            revision_sha256=revision,
            source_handle=handle,
            evidence_basis=evidence_basis,
            evidence_knowledge_id=evidence_knowledge_id,
        )
    if set(result) != set(relation_ids):
        raise MemoryExactStorageError("memory exact explicit relation source set changed")
    return result


def _claim_knowledge_id(items: Sequence[Mapping[str, object]]) -> str:
    values: list[str] = []
    for item in items:
        for field in ("knowledge_object_id", "evidence_knowledge_object_id"):
            value = item.get(field)
            if value and str(value) not in values:
                values.append(str(value))
        provenance = item.get("provenance")
        if isinstance(provenance, dict):
            value = provenance.get("knowledge_object_id")
            if value and str(value) not in values:
                values.append(str(value))
    return values[0] if len(values) == 1 else ""


@dataclass(frozen=True, slots=True, repr=False)
class _ExactImplicitLink:
    link_id: str
    canonical_entity_id: str
    confidence: float
    released_rank_confidence: float
    created_at: str
    exact_source: dict[str, object]


@dataclass(frozen=True, slots=True, repr=False)
class _ImplicitLinkSet:
    by_canonical_entity: dict[str, _ExactImplicitLink]
    evidence_bytes: int


def _implicit_link_set(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    knowledge_id: str,
    key: bytes,
    endpoint_cache: dict[str, _EndpointEntitySource],
) -> _ImplicitLinkSet:
    """Rebuild released top-30 accepted-link canonicalization exactly."""

    public_entity = _not_private_entity_material_dependency("entity")
    public_knowledge = _not_private_knowledge_dependency("knowledge")
    selected = f"""WITH selected(link_rowid) AS MATERIALIZED (
        SELECT link.rowid
          FROM knowledge_entity_links link
          JOIN entities entity
            ON entity.id=link.entity_id AND entity.user_id=link.user_id
          JOIN knowledge_objects knowledge
            ON knowledge.id=link.knowledge_object_id
           AND knowledge.user_id=link.user_id
         WHERE {public_entity} AND {public_knowledge}
           AND link.user_id=? AND link.knowledge_object_id=?
           AND link.status='accepted'
         ORDER BY CASE link.status
                      WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                  link.confidence DESC, link.created_at DESC
         LIMIT {MEMORY_EXACT_MAX_IMPLICIT_LINK_ROWS}
    )"""  # nosec B608 - fixed predicates and a fixed integer ceiling
    parameters = (tenant_id, knowledge_id)
    preflight_cursor = conn.execute(
        selected
        + """ SELECT COUNT(*) AS row_count,
                       COALESCE(MAX(length(CAST(COALESCE(link.evidence_json,'') AS BLOB))),0)
                           AS maximum_evidence,
                       COALESCE(SUM(length(CAST(COALESCE(link.evidence_json,'') AS BLOB))),0)
                           AS aggregate_evidence,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(link.id,'') AS BLOB)),
                           length(CAST(COALESCE(link.knowledge_object_id,'') AS BLOB)),
                           length(CAST(COALESCE(link.entity_id,'') AS BLOB)),
                           length(CAST(COALESCE(link.reviewed_by,'') AS BLOB))
                       )),0) AS maximum_identity,
                       COALESCE(MAX(length(CAST(COALESCE(link.status,'') AS BLOB))),0)
                           AS maximum_status,
                       COALESCE(MAX(max(
                           length(CAST(COALESCE(link.created_at,'') AS BLOB)),
                           length(CAST(COALESCE(link.reviewed_at,'') AS BLOB))
                       )),0) AS maximum_timestamp
                  FROM selected
                  JOIN knowledge_entity_links link ON link.rowid=selected.link_rowid""",
        parameters,
    )
    raw_preflight = preflight_cursor.fetchone()
    if raw_preflight is None:
        preflight_cursor.close()
        raise MemoryExactStorageError("memory exact implicit link preflight is unavailable")
    preflight = _record(preflight_cursor, raw_preflight)
    preflight_cursor.close()
    if (
        any(
            isinstance(preflight.get(field), bool) or not isinstance(preflight.get(field), int)
            for field in (
                "row_count",
                "maximum_evidence",
                "aggregate_evidence",
                "maximum_identity",
                "maximum_status",
                "maximum_timestamp",
            )
        )
        or not 0 <= int(preflight["row_count"]) <= MEMORY_EXACT_MAX_IMPLICIT_LINK_ROWS
        or not 0 <= int(preflight["maximum_evidence"]) <= MEMORY_EXACT_MAX_METADATA_UTF8_BYTES
        or not 0 <= int(preflight["aggregate_evidence"]) <= MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES
        or not 0 <= int(preflight["maximum_identity"]) <= 240
        or not 0 <= int(preflight["maximum_status"]) <= 80
        or not 0 <= int(preflight["maximum_timestamp"]) <= 64
    ):
        raise MemoryExactStorageError("memory exact implicit link source exceeds its limits")
    cursor = conn.execute(
        selected
        + """ SELECT link.id, link.knowledge_object_id, link.entity_id,
                       link.status, link.confidence, link.evidence_json,
                       link.created_at, link.reviewed_at, link.reviewed_by
                  FROM selected
                  JOIN knowledge_entity_links link ON link.rowid=selected.link_rowid
                 ORDER BY CASE link.status
                              WHEN 'suggested' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END,
                          link.confidence DESC, link.created_at DESC""",
        parameters,
    )
    rows = [_record(cursor, raw) for raw in cursor.fetchall()]
    cursor.close()
    if len(rows) != int(preflight["row_count"]):
        raise MemoryExactStorageError("memory exact implicit link source changed")

    by_canonical: dict[str, _ExactImplicitLink] = {}
    seen_links: set[str] = set()
    for row in rows:
        link_id = _scope(row["id"], label="stored graph link identity", maximum=240)
        if link_id in seen_links:
            raise MemoryExactStorageError("memory exact implicit link source duplicated")
        seen_links.add(link_id)
        if row["knowledge_object_id"] != knowledge_id or row["status"] != "accepted":
            raise MemoryExactStorageError("memory exact implicit link scope changed")
        raw_entity_id = _scope(row["entity_id"], label="stored graph link entity", maximum=240)
        resolution = _resolve_endpoint(
            conn,
            tenant_id=tenant_id,
            stored_entity_id=raw_entity_id,
            expected_canonical_id=None,
            key=key,
            cache=endpoint_cache,
            allow_unresolved=True,
        )
        if resolution is None:
            continue
        confidence = _finite_number(row["confidence"], label="stored graph link confidence")
        released_rank_confidence = confidence if confidence else 1.0
        evidence_json = _json_text(row["evidence_json"], label="stored graph link evidence", expected=dict)
        created_at = _private_text(
            row["created_at"], label="stored graph link creation timestamp", maximum=64
        )
        reviewed_at_sha256 = None
        if row["reviewed_at"] is not None:
            reviewed_at = _private_text(
                row["reviewed_at"],
                label="stored graph link review timestamp",
                maximum=64,
            )
            reviewed_at_sha256 = _bytes_sha256(reviewed_at)
        reviewed_by_handle = None
        if row["reviewed_by"] is not None:
            reviewed_by = _scope(row["reviewed_by"], label="stored graph link reviewer", maximum=240)
            reviewed_by_handle = _hmac(
                key,
                domain="friday.memory-exact-principal-handle.v1",
                material=_canonical_bytes({"tenant": tenant_id, "principal_id": reviewed_by}),
            )
        exact = _ExactImplicitLink(
            link_id=link_id,
            canonical_entity_id=resolution.canonical_entity_id,
            confidence=confidence,
            released_rank_confidence=released_rank_confidence,
            created_at=created_at,
            exact_source={
                "handle": _hmac(
                    key,
                    domain="friday.memory-exact-link-handle.v1",
                    material=_canonical_bytes({"tenant": tenant_id, "link_id": link_id}),
                ),
                "stored_entity_resolution": resolution.exact_payload(),
                "confidence": confidence,
                "status": "accepted",
                "evidence_sha256": _bytes_sha256(evidence_json),
                "created_at_sha256": _bytes_sha256(created_at),
                "reviewed_at_sha256": reviewed_at_sha256,
                "reviewed_by_handle": reviewed_by_handle,
            },
        )
        previous = by_canonical.get(exact.canonical_entity_id)
        if previous is None or (
            exact.released_rank_confidence,
            exact.link_id,
        ) > (
            previous.released_rank_confidence,
            previous.link_id,
        ):
            by_canonical[exact.canonical_entity_id] = exact
    return _ImplicitLinkSet(
        by_canonical_entity=by_canonical,
        evidence_bytes=int(preflight["aggregate_evidence"]),
    )


def _select_implicit_relations(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    claims: Mapping[str, list[dict[str, object]]],
    entities: Mapping[str, _ExactEntity],
    material: Mapping[str, _StoredMaterial],
    key: bytes,
) -> dict[str, _ExactRelation]:
    # Present-day accepted links carry neither valid-time nor append-only
    # transaction history.  They never become temporal evidence.
    if request.as_of is not None or request.known_at is not None:
        if any(items[0]["implicit"] is True for items in claims.values()):
            raise MemoryExactStorageError("memory exact temporal graph contains present-day co-occurrence")
        return {}
    result: dict[str, _ExactRelation] = {}
    evidence_budget = 0
    endpoint_cache: dict[str, _EndpointEntitySource] = {}
    link_sets: dict[str, _ImplicitLinkSet] = {}
    over_budget_knowledge: set[str] = set()
    for relation_id, sightings in claims.items():
        first = sightings[0]
        if first["implicit"] is not True:
            continue
        source_id = str(first["source_entity_id"])
        target_id = str(first["target_entity_id"])
        knowledge_id = _claim_knowledge_id(sightings)
        expected_provenance = {
            "origin": "implicit_cooccurrence",
            "source": "accepted_knowledge_links",
            "knowledge_object_id": knowledge_id,
        }
        if (
            source_id not in entities
            or target_id not in entities
            or source_id >= target_id
            or not knowledge_id
            or knowledge_id not in material
            or relation_id != f"co:{knowledge_id}:{min(source_id, target_id)}:{max(source_id, target_id)}"
            or any(
                claim["relation_type"] != "co_occurs_in"
                or claim.get("valid_from", "") != ""
                or claim.get("valid_to") is not None
                or "updated_at" in claim
                or "invalidated_at" in claim
                or bool(claim.get("evidence_knowledge_object_id"))
                or ("source_name" in claim and claim["source_name"] != entities[source_id].name[:240])
                or ("target_name" in claim and claim["target_name"] != entities[target_id].name[:240])
                or ("provenance" in claim and claim["provenance"] != expected_provenance)
                for claim in sightings
            )
        ):
            continue
        if knowledge_id in over_budget_knowledge:
            continue
        link_set = link_sets.get(knowledge_id)
        if link_set is None:
            link_set = _implicit_link_set(
                conn,
                tenant_id=request.tenant_id,
                knowledge_id=knowledge_id,
                key=key,
                endpoint_cache=endpoint_cache,
            )
            if evidence_budget + link_set.evidence_bytes > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                # Present-day implicit structure is optional.  Preserve a
                # visible PARTIAL graph instead of materializing past the
                # aggregate evidence ceiling.
                over_budget_knowledge.add(knowledge_id)
                continue
            evidence_budget += link_set.evidence_bytes
            link_sets[knowledge_id] = link_set
        if not {source_id, target_id} <= set(link_set.by_canonical_entity):
            continue
        source_link = link_set.by_canonical_entity[source_id]
        target_link = link_set.by_canonical_entity[target_id]
        source_confidence = source_link.confidence
        target_confidence = target_link.confidence
        # Released graph expansion uses ``value or 1.0`` before its [0, 1]
        # clamp.  Preserve that slightly unusual zero-value behavior exactly.
        released_source_confidence = max(0.0, min(1.0, source_confidence if source_confidence else 1.0))
        released_target_confidence = max(0.0, min(1.0, target_confidence if target_confidence else 1.0))
        expected_weight = round(released_source_confidence * released_target_confidence, 6)
        if any("weight" in claim and float(claim["weight"]) != expected_weight for claim in sightings):
            continue
        expected_created_at = max(source_link.created_at, target_link.created_at)
        if any("created_at" in claim and claim["created_at"] != expected_created_at for claim in sightings):
            continue
        links = [
            {
                **link.exact_source,
                "canonical_entity_handle": entities[link.canonical_entity_id].source_handle,
            }
            for link in (source_link, target_link)
        ]
        source_material = material[knowledge_id]
        revision = _sha256(
            {
                "schema": _IMPLICIT_REVISION_SCHEMA,
                "relation_type": "co_occurs_in",
                "valid_from": "",
                "valid_to": None,
                "weight": expected_weight,
                "knowledge_source_handle": source_material.source_handle,
                "knowledge_revision_sha256": source_material.knowledge_revision_sha256,
                "raw_revision_sha256": source_material.raw_revision_sha256,
                "evidence_basis": MemoryExactGraphEvidenceBasis.ACCEPTED_LINKS.value,
                "evidence_knowledge_handle": source_material.source_handle,
                "links": links,
            }
        )
        handle = _hmac(
            key,
            domain="friday.memory-exact-implicit-relation-handle.v1",
            material=_canonical_bytes({"tenant": request.tenant_id, "implicit_relation_id": relation_id}),
        )
        result[relation_id] = _ExactRelation(
            relation_id=relation_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relation_type="co_occurs_in",
            valid_from="",
            valid_to=None,
            revision_sha256=revision,
            source_handle=handle,
            evidence_basis=MemoryExactGraphEvidenceBasis.ACCEPTED_LINKS,
            evidence_knowledge_id=knowledge_id,
            implicit=True,
        )
    return result


def _graph_date_projection(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise MemoryExactStorageError("memory exact relation date is invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class _GraphSelection:
    projection: MemoryExactGraphProjection
    source_set_sha256: str
    provider_graph_sha256: str


def _select_graph(
    conn: sqlite3.Connection,
    *,
    request: MemoryExactRequest,
    effective_query: str,
    graph: Mapping[str, object],
    entities: Mapping[str, _ExactEntity],
    endpoint_resolutions: Mapping[str, tuple[_EndpointResolution, _EndpointResolution]],
    material: Mapping[str, _StoredMaterial],
    evidence_result_ordinals: Mapping[str, int],
    key: bytes,
) -> _GraphSelection:
    claims = _relation_claims(graph)
    explicit = _select_explicit_relations(
        conn,
        request=request,
        claims=claims,
        entities=entities,
        endpoint_resolutions=endpoint_resolutions,
        key=key,
    )
    implicit = _select_implicit_relations(
        conn,
        request=request,
        claims=claims,
        entities=entities,
        material=material,
        key=key,
    )
    exact_relations = {**explicit, **implicit}

    admitted_ids: list[str] = []

    def can_admit(ids: Sequence[str]) -> bool:
        additions = [identity for identity in ids if identity not in admitted_ids]
        return len(admitted_ids) + len(additions) <= MEMORY_EXACT_MAX_GRAPH_NODES

    def admit(ids: Sequence[str]) -> None:
        for identity in ids:
            if identity not in admitted_ids:
                admitted_ids.append(identity)

    root_ids = [str(item["id"]) for item in graph.get("roots", []) if isinstance(item, dict)]
    if not can_admit(root_ids):
        raise MemoryExactStorageError("memory exact graph roots exceed the node projection")
    admit(root_ids)

    admitted_paths: list[dict[str, object]] = []
    for path in graph.get("paths", []):
        if len(admitted_paths) >= MEMORY_EXACT_MAX_GRAPH_PATHS:
            break
        if not isinstance(path, dict):
            raise MemoryExactStorageError("memory exact graph path changed shape")
        edges = path.get("edges", [])
        if not all(isinstance(edge, dict) and str(edge["id"]) in exact_relations for edge in edges):
            continue
        entity_ids = [str(identity) for identity in path.get("entity_ids", [])]
        if not can_admit(entity_ids):
            continue
        admit(entity_ids)
        admitted_paths.append(path)

    # Preserve the provider's node preference after reserving complete paths.
    for node in graph.get("nodes", []):
        if len(admitted_ids) >= MEMORY_EXACT_MAX_GRAPH_NODES:
            break
        if not isinstance(node, dict):
            raise MemoryExactStorageError("memory exact graph node changed shape")
        admit((str(node["id"]),))

    admitted_relations: list[dict[str, object]] = []
    for relation in graph.get("relations", []):
        if len(admitted_relations) >= MEMORY_EXACT_MAX_GRAPH_RELATIONS:
            break
        if not isinstance(relation, dict):
            raise MemoryExactStorageError("memory exact graph relation changed shape")
        relation_id = str(relation["id"])
        exact = exact_relations.get(relation_id)
        if exact is None:
            continue
        endpoints = (exact.source_entity_id, exact.target_entity_id)
        if not can_admit(endpoints):
            continue
        admit(endpoints)
        admitted_relations.append(relation)

    ordinals = {identity: index + 1 for index, identity in enumerate(admitted_ids)}
    try:

        def evidence_ordinal(relation: _ExactRelation) -> int | None:
            if relation.evidence_knowledge_id is None:
                return None
            return evidence_result_ordinals.get(relation.evidence_knowledge_id)

        node_projection = tuple(
            MemoryExactGraphNodeProjection(
                ordinal=ordinals[identity],
                name=entities[identity].name[:240],
                entity_type=entities[identity].entity_type,
            )
            for identity in admitted_ids
        )
        relation_projection = tuple(
            MemoryExactGraphRelationProjection(
                ordinal=index + 1,
                source_ordinal=ordinals[exact_relations[str(item["id"])].source_entity_id],
                target_ordinal=ordinals[exact_relations[str(item["id"])].target_entity_id],
                relation_type=exact_relations[str(item["id"])].relation_type,
                implicit=exact_relations[str(item["id"])].implicit,
                evidence_basis=exact_relations[str(item["id"])].evidence_basis,
                evidence_result_ordinal=evidence_ordinal(exact_relations[str(item["id"])]),
                valid_from=_graph_date_projection(exact_relations[str(item["id"])].valid_from),
                valid_to=_graph_date_projection(exact_relations[str(item["id"])].valid_to),
            )
            for index, item in enumerate(admitted_relations)
        )
        path_projection: list[MemoryExactGraphPathProjection] = []
        for path_index, path in enumerate(admitted_paths):
            projected_edges: list[MemoryExactGraphEdgeProjection] = []
            for edge_index, raw_edge in enumerate(path["edges"]):
                if not isinstance(raw_edge, dict):
                    raise MemoryExactStorageError("memory exact graph path changed shape")
                exact = exact_relations[str(raw_edge["id"])]
                projected_edges.append(
                    MemoryExactGraphEdgeProjection(
                        ordinal=edge_index + 1,
                        source_ordinal=ordinals[str(raw_edge["from"])],
                        target_ordinal=ordinals[str(raw_edge["to"])],
                        relation_type=exact.relation_type,
                        direction=MemoryExactGraphDirection(str(raw_edge["direction"])),
                        implicit=exact.implicit,
                        evidence_basis=exact.evidence_basis,
                        evidence_result_ordinal=evidence_ordinal(exact),
                        valid_from=_graph_date_projection(exact.valid_from),
                        valid_to=_graph_date_projection(exact.valid_to),
                    )
                )
            path_projection.append(
                MemoryExactGraphPathProjection(
                    ordinal=path_index + 1,
                    edges=tuple(projected_edges),
                )
            )
        provider_entity_count = len(_graph_entity_ids(graph))

        def exact_coverage(
            *,
            provider_state: object,
            provider_visible: int,
            provider_matched: int,
            shown: int,
        ) -> tuple[int, MemoryExactGraphCoverage]:
            try:
                state = MemoryExactGraphCoverage(str(provider_state))
            except ValueError:
                raise MemoryExactStorageError("memory exact graph coverage changed") from None
            lower_bound = max(provider_visible, provider_matched)
            if state is MemoryExactGraphCoverage.PARTIAL:
                # The provider proved that at least one source row was omitted.
                # Projected endpoint nodes can make ``shown`` larger than the
                # provider's visible node list, but cannot turn that missing
                # source-row witness into complete coverage.
                return max(lower_bound, shown + 1), MemoryExactGraphCoverage.PARTIAL
            if lower_bound > shown:
                return lower_bound, MemoryExactGraphCoverage.PARTIAL
            if state is MemoryExactGraphCoverage.UNKNOWN:
                return shown, MemoryExactGraphCoverage.UNKNOWN
            return shown, MemoryExactGraphCoverage.COMPLETE

        nodes_matched, nodes_coverage = exact_coverage(
            provider_state=graph["nodes_coverage"],
            provider_visible=provider_entity_count,
            provider_matched=int(graph["nodes_matched_at_least"]),
            shown=len(node_projection),
        )
        relations_matched, relations_coverage = exact_coverage(
            provider_state=graph["relations_coverage"],
            provider_visible=len(graph.get("relations", [])),
            provider_matched=int(graph["relations_matched_at_least"]),
            shown=len(relation_projection),
        )
        paths_matched, paths_coverage = exact_coverage(
            provider_state=graph["paths_coverage"],
            provider_visible=len(graph.get("paths", [])),
            provider_matched=int(graph["paths_matched_at_least"]),
            shown=len(path_projection),
        )
        projection = MemoryExactGraphProjection(
            effective_query=effective_query,
            nodes=node_projection,
            relations=relation_projection,
            paths=tuple(path_projection),
            root_ordinals=tuple(ordinals[identity] for identity in root_ids),
            nodes_matched_at_least=nodes_matched,
            relations_matched_at_least=relations_matched,
            paths_matched_at_least=paths_matched,
            nodes_coverage=nodes_coverage,
            relations_coverage=relations_coverage,
            paths_coverage=paths_coverage,
            expanded=bool(graph["expanded"]),
        )
    except (KeyError, ValueError, MemoryExactContractError):
        raise MemoryExactStorageError("memory exact graph projection is invalid") from None

    graph_knowledge = sorted(
        (
            {
                "source_handle": row.source_handle,
                "knowledge_revision_sha256": row.knowledge_revision_sha256,
                "raw_revision_sha256": row.raw_revision_sha256,
            }
            for identity, row in material.items()
            if identity in set(_graph_knowledge_ids(graph))
        ),
        key=lambda item: str(item["source_handle"]),
    )
    entity_sources = sorted(
        (
            {"source_handle": item.source_handle, "revision_sha256": item.revision_sha256}
            for item in entities.values()
        ),
        key=lambda item: str(item["source_handle"]),
    )
    relation_sources = sorted(
        (
            {"source_handle": item.source_handle, "revision_sha256": item.revision_sha256}
            for item in exact_relations.values()
        ),
        key=lambda item: str(item["source_handle"]),
    )
    provider_graph_handle = _hmac(
        key,
        domain="friday.memory-exact-provider-graph.v1",
        material=_canonical_bytes(graph),
    )
    source_set_sha256 = _sha256(
        {
            "schema": _GRAPH_SOURCE_SCHEMA,
            "provider_graph_handle": provider_graph_handle,
            "entities": entity_sources,
            "relations": relation_sources,
            "knowledge": graph_knowledge,
        }
    )
    return _GraphSelection(
        projection=projection,
        source_set_sha256=source_set_sha256,
        provider_graph_sha256=provider_graph_handle,
    )


def _cursor_binding(authority: MemoryExactStorageAuthority) -> dict[str, str]:
    return {
        "authority_context_sha256": authority._authority_context_sha256,
        "request_identity_sha256": authority._request_identity_sha256,
        "selector_sha256": authority._selector_sha256,
        "turn_id_sha256": authority._turn_id_sha256,
        "turn_authority_sha256": authority._turn_authority_sha256,
        "context_authority_sha256": authority._context_authority_sha256,
        "tenant_binding_sha256": authority._tenant_binding_sha256,
        "person_binding_sha256": authority._person_binding_sha256,
        "adapter_binding_sha256": authority._adapter_binding_sha256,
        "capability_binding_sha256": authority._capability_binding_sha256,
        "authorization_binding_sha256": authority._authorization_binding_sha256,
    }


@dataclass(frozen=True, slots=True, repr=False)
class _CursorState:
    offset: int
    total_rows: int
    snapshot_rows: int
    matched_rows: int
    snapshot_bytes: int
    date_window_status_sha256: str
    provider_binding_sha256: str
    full_ledger_sha256: str
    prefix_ledger_sha256: str
    anchor_source_handle: str
    anchor_revision_sha256: str
    temporal_status_sha256: str
    graph_source_set_sha256: str
    snapshot_handle: str


def _encode_cursor(
    key: bytes,
    authority: MemoryExactStorageAuthority,
    state: _CursorState,
) -> str:
    payload: dict[str, object] = {
        "schema": _CURSOR_SCHEMA,
        **_cursor_binding(authority),
        "offset": state.offset,
        "total_rows": state.total_rows,
        "snapshot_rows": state.snapshot_rows,
        "matched_rows": state.matched_rows,
        "snapshot_bytes": state.snapshot_bytes,
        "date_window_status_sha256": state.date_window_status_sha256,
        "provider_binding_sha256": state.provider_binding_sha256,
        "full_ledger_sha256": state.full_ledger_sha256,
        "prefix_ledger_sha256": state.prefix_ledger_sha256,
        "anchor_source_handle": state.anchor_source_handle,
        "anchor_revision_sha256": state.anchor_revision_sha256,
        "temporal_status_sha256": state.temporal_status_sha256,
        "graph_source_set_sha256": state.graph_source_set_sha256,
        "snapshot_handle": state.snapshot_handle,
    }
    payload_bytes = _canonical_bytes(payload)
    envelope = {
        "payload": payload,
        "signature": _hmac(key, domain=_CURSOR_SCHEMA, material=payload_bytes),
    }
    encoded = base64.urlsafe_b64encode(_canonical_bytes(envelope)).rstrip(b"=").decode("ascii")
    if len(encoded.encode("ascii")) > MEMORY_EXACT_MAX_CONTINUATION_BYTES:
        raise MemoryExactStorageError("memory exact continuation exceeds its byte limit")
    return encoded


def _decode_cursor(
    token: str,
    *,
    key: bytes,
    authority: MemoryExactStorageAuthority,
) -> _CursorState:
    if (
        not isinstance(token, str)
        or not token
        or token != token.strip()
        or len(token.encode("utf-8", errors="strict")) > MEMORY_EXACT_MAX_CONTINUATION_BYTES
        or _TOKEN.fullmatch(token) is None
    ):
        raise MemoryExactStorageError("memory exact continuation is invalid")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (UnicodeError, ValueError):
        raise MemoryExactStorageError("memory exact continuation is invalid") from None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
        raise MemoryExactStorageError("memory exact continuation is not canonical")
    try:
        text = raw.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise MemoryExactStorageError("memory exact continuation is invalid") from None
    envelope = _strict_json(text, label="memory exact continuation")
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise MemoryExactStorageError("memory exact continuation envelope is invalid")
    if raw != _canonical_bytes(envelope):
        raise MemoryExactStorageError("memory exact continuation is not canonical")
    payload = envelope["payload"]
    signature = envelope["signature"]
    if (
        not isinstance(payload, dict)
        or not isinstance(signature, str)
        or _SHA256.fullmatch(signature) is None
    ):
        raise MemoryExactStorageError("memory exact continuation envelope is invalid")
    expected_keys = {
        "schema",
        *_cursor_binding(authority),
        "offset",
        "total_rows",
        "snapshot_rows",
        "matched_rows",
        "snapshot_bytes",
        "date_window_status_sha256",
        "provider_binding_sha256",
        "full_ledger_sha256",
        "prefix_ledger_sha256",
        "anchor_source_handle",
        "anchor_revision_sha256",
        "temporal_status_sha256",
        "graph_source_set_sha256",
        "snapshot_handle",
    }
    if set(payload) != expected_keys or payload.get("schema") != _CURSOR_SCHEMA:
        raise MemoryExactStorageError("memory exact continuation payload is invalid")
    expected_signature = _hmac(
        key,
        domain=_CURSOR_SCHEMA,
        material=_canonical_bytes(payload),
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise MemoryExactStorageError("memory exact continuation signature is invalid")
    for field, expected in _cursor_binding(authority).items():
        if payload.get(field) != expected:
            raise MemoryExactStorageError("memory exact continuation authority changed")

    def integer(name: str, *, low: int, high: int) -> int:
        return _integer(payload[name], label="memory exact continuation counter", low=low, high=high)

    offset = integer("offset", low=1, high=MEMORY_EXACT_MAX_PROVIDER_ROWS)
    snapshot_rows = integer("snapshot_rows", low=offset + 1, high=MEMORY_EXACT_MAX_PROVIDER_ROWS)
    matched_rows = integer("matched_rows", low=snapshot_rows, high=1_000_000_000)
    total_rows = integer("total_rows", low=matched_rows, high=1_000_000_000)
    return _CursorState(
        offset=offset,
        total_rows=total_rows,
        snapshot_rows=snapshot_rows,
        matched_rows=matched_rows,
        snapshot_bytes=integer("snapshot_bytes", low=0, high=MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES),
        date_window_status_sha256=_digest(
            payload["date_window_status_sha256"],
            label="cursor date-window status",
        ),
        provider_binding_sha256=_digest(payload["provider_binding_sha256"], label="cursor provider binding"),
        full_ledger_sha256=_digest(payload["full_ledger_sha256"], label="cursor ledger"),
        prefix_ledger_sha256=_digest(payload["prefix_ledger_sha256"], label="cursor prefix ledger"),
        anchor_source_handle=_digest(payload["anchor_source_handle"], label="cursor anchor source"),
        anchor_revision_sha256=_digest(payload["anchor_revision_sha256"], label="cursor anchor revision"),
        temporal_status_sha256=_digest(payload["temporal_status_sha256"], label="cursor temporal status"),
        graph_source_set_sha256=_digest(payload["graph_source_set_sha256"], label="cursor graph source set"),
        snapshot_handle=_digest(payload["snapshot_handle"], label="cursor snapshot"),
    )


def _provider_binding(
    key: bytes,
    *,
    request: MemoryExactRequest,
    snapshot: MemoryExactProviderSnapshot,
    provider_graph_sha256: str,
) -> str:
    if len(snapshot._knowledge_ids) != len(snapshot._knowledge_revision_sha256s):
        raise MemoryExactStorageError("memory exact provider revision ledger is invalid")
    ordered_handles = [
        _hmac(
            key,
            domain="friday.memory-exact-provider-source.v1",
            material=_canonical_bytes(
                {
                    "tenant": request.tenant_id,
                    "knowledge_id": identity,
                    "knowledge_revision_sha256": revision,
                    "ordinal": ordinal,
                }
            ),
        )
        for ordinal, (identity, revision) in enumerate(
            zip(snapshot._knowledge_ids, snapshot._knowledge_revision_sha256s, strict=True)
        )
    ]
    return _sha256(
        {
            "schema": "friday.memory-exact-provider-binding.v1",
            "request_identity_sha256": _request_identity(request),
            "effective_query_sha256": hashlib.sha256(snapshot._effective_query.encode("utf-8")).hexdigest(),
            "ordered_source_handles": ordered_handles,
            "matched_at_least": snapshot._matched_at_least,
            "date_window_applied": snapshot._date_window_applied,
            "date_window_empty": snapshot._date_window_empty,
            "provider_graph_sha256": provider_graph_sha256,
        }
    )


def _ledger_seed() -> str:
    return _sha256({"schema": _LEDGER_SCHEMA, "rows": []})


def _ledger_extend(previous: str, row: _StoredMaterial, *, ordinal: int) -> str:
    return _sha256(
        {
            "schema": _LEDGER_SCHEMA,
            "previous_sha256": previous,
            "ordinal": ordinal,
            "source_handle": row.source_handle,
            "knowledge_revision_sha256": row.knowledge_revision_sha256,
            "raw_revision_sha256": row.raw_revision_sha256,
        }
    )


def _ledger_states(rows: tuple[_StoredMaterial, ...]) -> tuple[str, tuple[str, ...]]:
    ledger = _ledger_seed()
    prefixes: list[str] = []
    for ordinal, row in enumerate(rows):
        ledger = _ledger_extend(ledger, row, ordinal=ordinal)
        prefixes.append(ledger)
    return ledger, tuple(prefixes)


def _matched_rows(
    request: MemoryExactRequest,
    snapshot: MemoryExactProviderSnapshot,
    snapshot_rows: int,
) -> int:
    all_stages = {item.value for item in MemoryExactLifecycleStage}
    requested_stages = {item.value for item in request.lifecycle_stages}
    if requested_stages == all_stages:
        return snapshot._matched_at_least
    # HybridSearcher does not have a lifecycle selector.  For a subset request,
    # only the exact retained rows prove a lower bound in that subset.
    return snapshot_rows


def _snapshot_handle(
    authority: MemoryExactStorageAuthority,
    *,
    date_window_status_sha256: str,
    provider_binding_sha256: str,
    full_ledger_sha256: str,
    temporal_status_sha256: str,
    graph_source_set_sha256: str,
    total_rows: int,
    snapshot_rows: int,
    matched_rows: int,
    snapshot_bytes: int,
) -> str:
    return _sha256(
        {
            "schema": _SNAPSHOT_SCHEMA,
            "authority_context_sha256": authority._authority_context_sha256,
            "selector_sha256": authority._selector_sha256,
            "date_window_status_sha256": date_window_status_sha256,
            "provider_binding_sha256": provider_binding_sha256,
            "full_ledger_sha256": full_ledger_sha256,
            "temporal_status_sha256": temporal_status_sha256,
            "graph_source_set_sha256": graph_source_set_sha256,
            "total_rows": total_rows,
            "snapshot_rows": snapshot_rows,
            "matched_rows": matched_rows,
            "snapshot_bytes": snapshot_bytes,
        }
    )


def _to_candidate(request: MemoryExactRequest, row: _StoredMaterial) -> MemoryExactCandidate:
    try:
        return _create_memory_exact_candidate(
            request=request,
            knowledge_id=row.knowledge_id,
            raw_object_id=row.raw_object_id,
            source_handle=row.source_handle,
            knowledge_revision_sha256=row.knowledge_revision_sha256,
            raw_revision_sha256=row.raw_revision_sha256,
            title=row.title,
            knowledge_kind=row.knowledge_kind,
            lifecycle_stage=MemoryExactLifecycleStage(row.lifecycle_stage),
            updated_at=row.knowledge_updated_at,
            body=row.content,
        )
    except (MemoryExactContractError, ValueError):
        raise MemoryExactStorageError("memory exact candidate projection is invalid") from None


def select_memory_exact_page_in_transaction(
    conn: sqlite3.Connection,
    authority: MemoryExactStorageAuthority,
    provider_snapshot: MemoryExactProviderSnapshot,
    request: MemoryExactRequest | None = None,
) -> MemoryExactPage:
    """Select one exact page after sealed authority and strict scope checks."""

    _require_transaction(conn)
    selected_request = authority.request if request is None else request
    key = _verify_authority(conn, authority, selected_request)
    provider_temporal, graph = _verify_provider_snapshot(provider_snapshot, selected_request)
    date_window_status = _provider_date_window_status(
        selected_request,
        provider_snapshot,
    )
    date_window_status_sha256 = _sha256(date_window_status.to_model_payload())
    continuation = selected_request.continuation
    cursor_state: _CursorState | None = None
    if continuation is not None:
        if type(continuation) is not MemoryExactContinuation:
            raise MemoryExactStorageError("memory exact continuation carrier is invalid")
        cursor_state = _decode_cursor(continuation.token, key=key, authority=authority)

    # This ownership/status probe is deliberately before every COUNT, graph
    # label, metadata length and source body read below.
    _probe_tenant(conn, tenant_id=authority._tenant_id)
    try:
        _require_classifiable_date_metadata_in_transaction(
            conn,
            request=selected_request,
            date_window_applied=provider_snapshot._date_window_applied,
        )
        graph_knowledge_ids = _graph_knowledge_ids(graph)
        _probe_provider_sources(
            conn,
            tenant_id=authority._tenant_id,
            provider_ids=tuple(dict.fromkeys((*provider_snapshot._knowledge_ids, *graph_knowledge_ids))),
        )
        entity_ids = _graph_entity_ids(graph)
        _probe_graph_entities(
            conn,
            tenant_id=authority._tenant_id,
            entity_ids=entity_ids,
        )
        relation_claims = _relation_claims(graph)
        endpoint_resolutions = _prepare_explicit_endpoint_resolutions(
            conn,
            request=selected_request,
            claims=relation_claims,
            key=key,
        )
        topology_entity_ids = tuple(
            dict.fromkeys(
                (
                    *entity_ids,
                    *(
                        source.entity_id
                        for pair in endpoint_resolutions.values()
                        for resolution in pair
                        for source in resolution.chain
                    ),
                )
            )
        )
        if len(topology_entity_ids) > MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS:
            raise MemoryExactStorageError("memory exact graph entity source set is too large")
        temporal_status, temporal_status_sha256 = _validate_temporal_status(
            conn,
            request=selected_request,
            provider_temporal=provider_temporal,
            entity_ids=topology_entity_ids,
            key=key,
        )
        material = _scan_material(
            conn,
            request=selected_request,
            provider_ids=provider_snapshot._knowledge_ids,
            provider_revision_sha256s=provider_snapshot._knowledge_revision_sha256s,
            graph_ids=graph_knowledge_ids,
            date_window_applied=provider_snapshot._date_window_applied,
            key=key,
        )
        entities = _select_entities(
            conn,
            tenant_id=selected_request.tenant_id,
            entity_ids=entity_ids,
            graph=graph,
            key=key,
        )
        snapshot_rows = len(material.candidates)
        offset = 0 if cursor_state is None else cursor_state.offset
        if offset > snapshot_rows:
            raise MemoryExactStorageDrift("memory exact continuation offset exceeds its snapshot")
        page_rows = material.candidates[offset : offset + selected_request.page_size]
        evidence_result_ordinals = {row.knowledge_id: ordinal for ordinal, row in enumerate(page_rows, 1)}
        graph_selection = _select_graph(
            conn,
            request=selected_request,
            effective_query=provider_snapshot._effective_query,
            graph=graph,
            entities=entities,
            endpoint_resolutions=endpoint_resolutions,
            material=material.by_id,
            evidence_result_ordinals=evidence_result_ordinals,
            key=key,
        )
        total_rows = _count_authorized_rows(
            conn,
            request=selected_request,
            date_window_applied=provider_snapshot._date_window_applied,
        )
    except MemoryExactStorageError:
        if cursor_state is not None:
            raise MemoryExactStorageDrift("memory exact continuation source changed") from None
        raise

    matched_rows = _matched_rows(selected_request, provider_snapshot, snapshot_rows)
    if not snapshot_rows <= matched_rows <= total_rows or (
        date_window_status.empty and (total_rows != 0 or snapshot_rows != 0 or matched_rows != 0)
    ):
        if cursor_state is not None:
            raise MemoryExactStorageDrift("memory exact continuation coverage changed")
        raise MemoryExactStorageError("memory exact provider coverage is inconsistent")
    provider_binding_sha256 = _provider_binding(
        key,
        request=selected_request,
        snapshot=provider_snapshot,
        provider_graph_sha256=graph_selection.provider_graph_sha256,
    )
    full_ledger, prefixes = _ledger_states(material.candidates)
    snapshot_handle = _snapshot_handle(
        authority,
        date_window_status_sha256=date_window_status_sha256,
        provider_binding_sha256=provider_binding_sha256,
        full_ledger_sha256=full_ledger,
        temporal_status_sha256=temporal_status_sha256,
        graph_source_set_sha256=graph_selection.source_set_sha256,
        total_rows=total_rows,
        snapshot_rows=snapshot_rows,
        matched_rows=matched_rows,
        snapshot_bytes=material.snapshot_bytes,
    )
    if cursor_state is not None:
        anchor = material.candidates[offset - 1]
        if (
            cursor_state.total_rows != total_rows
            or cursor_state.snapshot_rows != snapshot_rows
            or cursor_state.matched_rows != matched_rows
            or cursor_state.snapshot_bytes != material.snapshot_bytes
            or cursor_state.date_window_status_sha256 != date_window_status_sha256
            or cursor_state.provider_binding_sha256 != provider_binding_sha256
            or cursor_state.full_ledger_sha256 != full_ledger
            or cursor_state.prefix_ledger_sha256 != prefixes[offset - 1]
            or cursor_state.anchor_source_handle != anchor.source_handle
            or cursor_state.anchor_revision_sha256 != anchor.knowledge_revision_sha256
            or cursor_state.temporal_status_sha256 != temporal_status_sha256
            or cursor_state.graph_source_set_sha256 != graph_selection.source_set_sha256
            or cursor_state.snapshot_handle != snapshot_handle
        ):
            raise MemoryExactStorageDrift("memory exact continuation snapshot changed")

    candidates = tuple(_to_candidate(selected_request, row) for row in page_rows)
    carrier_bytes = sum(
        len(candidate.title.encode("utf-8")) + len(candidate.excerpt.encode("utf-8"))
        for candidate in candidates
    )
    if carrier_bytes > MEMORY_EXACT_MAX_PAGE_UTF8_BYTES:
        raise MemoryExactStorageError("memory exact page exceeds its carrier byte limit")
    next_offset = offset + len(page_rows)
    next_continuation: MemoryExactContinuation | None = None
    if next_offset < snapshot_rows:
        if not page_rows or next_offset <= 0:
            raise MemoryExactStorageError("memory exact continuation could not be anchored")
        anchor = page_rows[-1]
        token = _encode_cursor(
            key,
            authority,
            _CursorState(
                offset=next_offset,
                total_rows=total_rows,
                snapshot_rows=snapshot_rows,
                matched_rows=matched_rows,
                snapshot_bytes=material.snapshot_bytes,
                date_window_status_sha256=date_window_status_sha256,
                provider_binding_sha256=provider_binding_sha256,
                full_ledger_sha256=full_ledger,
                prefix_ledger_sha256=prefixes[next_offset - 1],
                anchor_source_handle=anchor.source_handle,
                anchor_revision_sha256=anchor.knowledge_revision_sha256,
                temporal_status_sha256=temporal_status_sha256,
                graph_source_set_sha256=graph_selection.source_set_sha256,
                snapshot_handle=snapshot_handle,
            ),
        )
        try:
            next_continuation = MemoryExactContinuation.create(token)
        except MemoryExactContractError:
            raise MemoryExactStorageError("memory exact continuation carrier is invalid") from None
    try:
        return _create_memory_exact_page(
            request=selected_request,
            candidates=candidates,
            date_window_status=date_window_status,
            temporal_status=temporal_status,
            graph_projection=graph_selection.projection,
            graph_source_set_sha256=graph_selection.source_set_sha256,
            authority_handle=authority.authority_handle,
            snapshot_handle=snapshot_handle,
            offset=offset,
            total_rows=total_rows,
            snapshot_rows=snapshot_rows,
            matched_rows=matched_rows,
            next_continuation=next_continuation,
        )
    except MemoryExactContractError:
        raise MemoryExactStorageError("memory exact page carrier is invalid") from None


def reselect_memory_exact_page_in_transaction(
    conn: sqlite3.Connection,
    new_storage_authority: MemoryExactStorageAuthority,
    provider_snapshot: MemoryExactProviderSnapshot,
    original_page: MemoryExactPage,
) -> MemoryExactPage:
    """Freshly reselect a page after the adapter's late publication reauth."""

    _require_transaction(conn)
    if type(original_page) is not MemoryExactPage or not original_page._is_process_owned():
        raise MemoryExactStorageError("memory exact publication page is invalid")
    if new_storage_authority.request is not original_page.request:
        raise MemoryExactStorageError("memory exact publication request identity changed")
    _verify_authority(conn, new_storage_authority, original_page.request)
    if provider_snapshot.request is not original_page.request:
        raise MemoryExactStorageError("memory exact publication provider request changed")
    try:
        fresh = select_memory_exact_page_in_transaction(
            conn,
            new_storage_authority,
            provider_snapshot,
            original_page.request,
        )
    except MemoryExactStorageDrift:
        raise
    except MemoryExactStorageError:
        raise MemoryExactStorageDrift("memory exact publication source changed") from None
    if (
        fresh.authority_handle != original_page.authority_handle
        or fresh.snapshot_handle != original_page.snapshot_handle
        or fresh.selection_handle != original_page.selection_handle
        or fresh.graph_source_set_sha256 != original_page.graph_source_set_sha256
    ):
        raise MemoryExactStorageDrift("memory exact publication snapshot changed")
    return fresh


__all__ = [
    "MEMORY_EXACT_MAX_BODY_UTF8_BYTES",
    "MEMORY_EXACT_MAX_CONTINUATION_BYTES",
    "MEMORY_EXACT_MAX_EXCERPT_CHARS",
    "MEMORY_EXACT_MAX_FIELD_UTF8_BYTES",
    "MEMORY_EXACT_MAX_GRAPH_NODES",
    "MEMORY_EXACT_MAX_GRAPH_PATH_EDGES",
    "MEMORY_EXACT_MAX_GRAPH_PATHS",
    "MEMORY_EXACT_MAX_GRAPH_RELATIONS",
    "MEMORY_EXACT_MAX_METADATA_UTF8_BYTES",
    "MEMORY_EXACT_MAX_PAGE_UTF8_BYTES",
    "MEMORY_EXACT_MAX_PROVIDER_ROWS",
    "MEMORY_EXACT_MAX_ROW_UTF8_BYTES",
    "MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES",
    "MemoryExactProviderSnapshot",
    "MemoryExactStorageAuthority",
    "MemoryExactStorageDrift",
    "MemoryExactStorageError",
    "_create_memory_exact_provider_snapshot",
    "_issue_memory_exact_storage_authority_in_transaction",
    "reselect_memory_exact_page_in_transaction",
    "select_memory_exact_page_in_transaction",
]
