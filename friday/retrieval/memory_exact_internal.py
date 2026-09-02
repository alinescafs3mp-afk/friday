"""Authenticated direct adapter for exact memory and bounded graph recall.

The adapter is deliberately absent from the dialogue tool catalogue.  A trusted
primary caller supplies one authenticated turn, while the existing
``HybridSearcher`` remains the ranking oracle.  Its output is only a candidate
proposal: the storage lane reselects every selected source in one freshly
authorized SQLite snapshot and issues the process-private carrier used for model
projection and late publication revalidation.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import struct
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, SupportsIndex

from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.supervisor_contracts import ARCHIVE_SEARCH_ID
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    TurnContextError,
    TurnContextIssuer,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import EmbeddingBackend, HybridSearcher, is_relational_query
from friday.retrieval.memory_exact_contract import (
    MEMORY_EXACT_REQUEST_SCHEMA,
    MemoryExactContractError,
    MemoryExactPage,
    MemoryExactProjection,
    MemoryExactPublicationDecision,
    MemoryExactPublicationStatus,
    MemoryExactRequest,
    _claim_memory_exact_publication_decision,
    _create_memory_exact_publication_decision,
    _finish_memory_exact_publication_decision,
    _matches_memory_exact_publication_decision,
    project_memory_exact_page,
)
from friday.storage import FridayStorage
from friday.storage._core import read_only_storage_snapshot

MEMORY_EXACT_INTERNAL_ADAPTER_SCHEMA: Final = "friday.memory-exact-internal-adapter.v1"
MEMORY_EXACT_INTERNAL_ADAPTER_ID: Final = "friday.retrieval.memory_exact_internal.MemoryExactInternalAdapter"
MEMORY_EXACT_SECURITY_IDS: Final = ("search.use", "knowledge.read")
_PROVIDER_DEPENDENCY_LEDGER_SCHEMA: Final = "friday.memory-exact-provider-dependency-ledger.v1"
_PROVIDER_DEPENDENCY_LEDGER_KEY = secrets.token_bytes(32)
_PROVIDER_READ_SET_SCHEMA: Final = "friday.memory-exact-provider-read-set.v1"
_PROVIDER_READ_SET_MAX_OPERATIONS: Final = 1_024
_PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256: Final = hashlib.sha256(
    b"friday.memory-exact-provider-graph-suppressed.v1"
).hexdigest()
_LOCK_TYPE: Final = type(threading.Lock())
_PROVIDER_STORAGE_READ_KINDS: Final = frozenset(
    {
        "count_knowledge_objects",
        "entity_links_by_document",
        "feedback_scores",
        "graph_candidate_cards",
        "get_knowledge_object",
        "get_user_chunk_embeddings",
        "get_user_embeddings",
        "knowledge_ids_in_window",
        "list_knowledge_objects",
        "provider_rows",
        "relation_history_status",
        "search_knowledge",
    }
)
_PROVIDER_CONSTANT_READ_KINDS: Final = frozenset(
    {
        "get_chunk_spans",
        "get_knowledge_usage",
        "known_vocabulary",
        "vocabulary_terms",
    }
)
_PROVIDER_GRAPH_READ_KINDS: Final = frozenset(
    {
        "graph_context_for_query",
        "graph_search_entities",
    }
)
_PROVIDER_READ_KINDS: Final = (
    _PROVIDER_STORAGE_READ_KINDS
    | _PROVIDER_CONSTANT_READ_KINDS
    | _PROVIDER_GRAPH_READ_KINDS
)
_PROVIDER_MAIN_DEPENDENCY_NAMES: Final = frozenset(
    {
        "entities",
        "entity_merge_history",
        "entity_time",
        "entity_versions",
        "feedback_state",
        "inbox",
        "knowledge_chunk_embeddings",
        "knowledge_embeddings",
        "knowledge_entity_links",
        "knowledge_fts",
        "knowledge_fts_config",
        "knowledge_fts_data",
        "knowledge_fts_docsize",
        "knowledge_fts_idx",
        "knowledge_objects",
        "private_entity_material_cache",
        "private_entity_material_cache_state",
        "private_entity_material_derivative_cache",
        "private_entity_material_derivative_state",
        "private_entity_owners",
        "raw_objects",
        "relation_candidates",
        "relation_revision_context",
        "relation_revisions",
        "relations",
        "schema_meta",
        "users",
    }
)
_ASCII_NOCASE_TRANSLATION: Final = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)


class MemoryExactInternalError(RuntimeError):
    """The exact-memory lane could not preserve its closed authority contract."""


class MemoryExactReadDenied(PermissionError):
    """Fresh transactional authority denied a private memory read."""


class _AuthorizationDenied(Exception):
    __slots__ = ()


class _ProviderResourceExceeded(Exception):
    """Process-local marker whose text can never retain provider material."""

    __slots__ = ()


class _ProviderSnapshotInvalid(Exception):
    """Process-local marker for an untrusted provider shape or binding."""

    __slots__ = ()


class _ProviderGraphUnavailable(Exception):
    """Body-free signal consumed only by HybridSearcher's optional graph edge."""

    __slots__ = ()


def _main_data_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA main.data_version").fetchone()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if (
        row is None
        or len(row) != 1
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
        or row[0] < 0
    ):
        raise _ProviderSnapshotInvalid
    return row[0]


def _main_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA main.schema_version").fetchone()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if (
        row is None
        or len(row) != 1
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
        or row[0] < 0
    ):
        raise _ProviderSnapshotInvalid
    return row[0]


def _temp_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("PRAGMA temp.schema_version").fetchone()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if (
        row is None
        or len(row) != 1
        or isinstance(row[0], bool)
        or not isinstance(row[0], int)
        or row[0] < 0
    ):
        raise _ProviderSnapshotInvalid
    return row[0]


def _require_no_provider_temp_shadow(conn: sqlite3.Connection) -> None:
    names = tuple(sorted(_PROVIDER_MAIN_DEPENDENCY_NAMES))
    holders = ",".join("?" for _name in names)
    try:
        row = conn.execute(
            f"""SELECT 1 FROM temp.sqlite_schema
                 WHERE name COLLATE NOCASE IN ({holders}) LIMIT 1""",  # nosec B608
            names,
        ).fetchone()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if row is not None:
        raise _ProviderSnapshotInvalid


def _sqlite_nocase(left: str, right: str) -> int:
    if type(left) is not str or type(right) is not str:
        raise ValueError("NOCASE operands must be text")
    normalized_left = left.translate(_ASCII_NOCASE_TRANSLATION)
    normalized_right = right.translate(_ASCII_NOCASE_TRANSLATION)
    return (normalized_left > normalized_right) - (normalized_left < normalized_right)


def _sqlite_binary(left: str, right: str) -> int:
    if type(left) is not str or type(right) is not str:
        raise ValueError("BINARY operands must be text")
    return (left > right) - (left < right)


def _temp_schema_sha256(conn: sqlite3.Connection) -> str:
    try:
        preflight = conn.execute(
            """SELECT COUNT(*) AS row_count,
                      COALESCE(MAX(length(CAST(type AS BLOB))),0) AS max_type,
                      COALESCE(MAX(length(CAST(name AS BLOB))),0) AS max_name,
                      COALESCE(MAX(length(CAST(tbl_name AS BLOB))),0) AS max_table,
                      COALESCE(MAX(length(CAST(COALESCE(sql,'') AS BLOB))),0) AS max_sql,
                      COALESCE(SUM(
                          length(CAST(type AS BLOB))
                          + length(CAST(name AS BLOB))
                          + length(CAST(tbl_name AS BLOB))
                          + length(CAST(COALESCE(sql,'') AS BLOB))
                      ),0) AS aggregate_bytes
                 FROM temp.sqlite_schema"""
        ).fetchone()
        if preflight is None or len(preflight) != 6:
            raise _ProviderSnapshotInvalid
        values = tuple(preflight)
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            or not 0 <= values[0] <= 512
            or any(not 0 <= value <= 1_048_576 for value in values[1:])
        ):
            raise _ProviderSnapshotInvalid
        cursor = conn.execute(
            """SELECT type,name,tbl_name,sql FROM temp.sqlite_schema
               ORDER BY type,name,tbl_name,sql"""
        )
        rows: list[sqlite3.Row] = []
        try:
            while True:
                batch = cursor.fetchmany(64)
                if not batch:
                    break
                rows.extend(batch)
                if len(rows) > values[0] or len(rows) > 512:
                    raise _ProviderSnapshotInvalid
        finally:
            cursor.close()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if len(rows) != values[0]:
        raise _ProviderSnapshotInvalid
    material: list[tuple[str, str, str, str | None]] = []
    size = 0
    for row in rows:
        values = tuple(row)
        if (
            len(values) != 4
            or any(type(value) is not str for value in values[:3])
            or (values[3] is not None and type(values[3]) is not str)
        ):
            raise _ProviderSnapshotInvalid
        size += sum(len(value.encode("utf-8")) for value in values if value is not None)
        if size > 1_048_576:
            raise _ProviderSnapshotInvalid
        material.append((values[0], values[1], values[2], values[3]))
    if size != preflight[5]:
        raise _ProviderSnapshotInvalid
    return _provider_dependency_ledger_seal(
        {"schema": "friday.memory-exact-temp-schema.v1", "rows": material}
    )


def _function_schema_sha256(conn: sqlite3.Connection) -> str:
    try:
        cursor = conn.execute("PRAGMA function_list")
        try:
            rows = cursor.fetchmany(2_049)
        finally:
            cursor.close()
    except sqlite3.Error:
        raise _ProviderSnapshotInvalid from None
    if len(rows) > 2_048:
        raise _ProviderSnapshotInvalid
    material: list[tuple[object, ...]] = []
    for row in rows:
        values = tuple(row)
        if (
            len(values) != 6
            or type(values[0]) is not str
            or type(values[1]) is not int
            or type(values[2]) is not str
            or type(values[3]) is not str
            or type(values[4]) is not int
            or type(values[5]) is not int
        ):
            raise _ProviderSnapshotInvalid
        material.append(values)
    material.sort(key=lambda item: tuple(str(value) for value in item))
    return _provider_dependency_ledger_seal(
        {"schema": "friday.memory-exact-function-schema.v1", "rows": material}
    )


def _normalize_provider_connection(conn: sqlite3.Connection) -> None:
    """Restore the code-owned SQL semantics used by the bounded provider."""

    if type(conn) is not sqlite3.Connection:
        raise _ProviderSnapshotInvalid
    from friday.storage._core import (
        _install_private_material_authorizer,
        _private_identity_match,
        _private_identity_tokens_json,
        _unicode_casefold,
        iso_date,
    )

    try:
        conn.row_factory = sqlite3.Row
        conn.text_factory = str
        conn.create_collation("BINARY", _sqlite_binary)
        conn.create_collation("NOCASE", _sqlite_nocase)
        conn.create_function("jericho_casefold", 1, _unicode_casefold, deterministic=True)
        conn.create_function(
            "jericho_private_identity_tokens",
            2,
            _private_identity_tokens_json,
            deterministic=True,
        )
        conn.create_function(
            "jericho_private_identity_match",
            2,
            _private_identity_match,
            deterministic=True,
        )
        conn.create_function("jericho_iso_date", 1, iso_date, deterministic=True)
        conn.execute("PRAGMA read_uncommitted=OFF")
        conn.execute("PRAGMA reverse_unordered_selects=OFF")
        conn.execute("PRAGMA automatic_index=ON")
        conn.execute("PRAGMA case_sensitive_like=OFF")
        for pragma, expected in (
            ("read_uncommitted", 0),
            ("reverse_unordered_selects", 0),
            ("automatic_index", 1),
        ):
            row = conn.execute(f"PRAGMA {pragma}").fetchone()  # nosec B608 - fixed names
            if row is None or len(row) != 1 or row[0] != expected:
                raise _ProviderSnapshotInvalid
        _install_private_material_authorizer(conn)
        _require_no_provider_temp_shadow(conn)
    except _ProviderSnapshotInvalid:
        raise
    except (AttributeError, sqlite3.Error, TypeError, ValueError):
        raise _ProviderSnapshotInvalid from None


def _current_storage_connection(storage: FridayStorage) -> sqlite3.Connection:
    """Return the exact live source connection without exposing storage failures."""

    try:
        if type(storage) is not FridayStorage:
            raise _ProviderSnapshotInvalid
        conn = storage.conn
        if type(conn) is not sqlite3.Connection:
            raise _ProviderSnapshotInvalid
        return conn
    except Exception:  # noqa: BLE001 - storage failures may retain private paths
        raise MemoryExactInternalError(
            "memory-exact provider connection is unavailable"
        ) from None


def _provider_configuration_sha256(
    storage: FridayStorage,
    searcher: HybridSearcher,
    graph: KnowledgeGraph,
) -> str:
    if (
        type(storage) is not FridayStorage
        or type(searcher) is not HybridSearcher
        or searcher.storage is not storage
        or type(graph) is not KnowledgeGraph
        or graph.storage is not storage
        or type(storage._fts_available) is not bool  # noqa: SLF001
        or type(searcher._channel_weights) is not dict  # noqa: SLF001
        or type(searcher._ablate) is not frozenset  # noqa: SLF001
    ):
        raise _ProviderSnapshotInvalid
    scalar_names = (
        "_chunk_recall",
        "_confident_min",
        "_dense_evidence_min",
        "_graph_max_depth",
        "_pool_max",
        "_record_usage",
        "_rerank_top",
    )
    scalars: dict[str, object] = {}
    for name in scalar_names:
        value = getattr(searcher, name, None)
        if type(value) not in (bool, float, int) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise _ProviderSnapshotInvalid
        scalars[name] = value
    weights: list[tuple[str, float]] = []
    for name, value in searcher._channel_weights.items():  # noqa: SLF001
        if type(name) is not str or type(value) is not float or not math.isfinite(value):
            raise _ProviderSnapshotInvalid
        weights.append((name, value))
    if any(type(name) is not str for name in searcher._ablate):  # noqa: SLF001
        raise _ProviderSnapshotInvalid
    ablate = tuple(sorted(searcher._ablate))  # noqa: SLF001
    embeddings = searcher.embeddings
    embedding_settings: dict[str, object] | None = None
    if embeddings is not None:
        if type(embeddings) is not EmbeddingBackend or embeddings.settings is not storage.settings:
            raise _ProviderSnapshotInvalid
        settings = embeddings.settings
        embedding_settings = {
            name: getattr(settings, name)
            for name in (
                "embeddings_api_key",
                "embeddings_base_url",
                "embeddings_chunk_blend",
                "embeddings_chunk_chars",
                "embeddings_chunk_max_per_object",
                "embeddings_chunk_overlap_chars",
                "embeddings_chunk_scan_multiplier",
                "embeddings_dense_max_objects",
                "embeddings_enabled",
                "embeddings_max_inputs_per_request",
                "embeddings_model",
                "embeddings_recall_candidates",
                "embeddings_resident_cache",
                "llm_timeout_sec",
                "retrieval_dense_query_budget_sec",
            )
        }
    return _provider_dependency_ledger_seal(
        {
            "ablate": ablate,
            "channel_weights": sorted(weights),
            "embedding_object": None if embeddings is None else id(embeddings),
            "embedding_settings": embedding_settings,
            "fts_available": storage._fts_available,  # noqa: SLF001
            "graph_object": id(graph),
            "graph_storage_object": id(graph.storage),
            "reranker_object": (
                None if searcher._reranker is None else id(searcher._reranker)  # noqa: SLF001
            ),
            "scalars": scalars,
            "searcher_object": id(searcher),
            "searcher_storage_object": id(searcher.storage),
            "storage_object": id(storage),
        }
    )


def _open_main_database_observer(conn: sqlite3.Connection) -> sqlite3.Connection:
    observer: sqlite3.Connection | None = None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        paths = [row[2] for row in rows if len(row) == 3 and row[1] == "main"]
        if len(paths) != 1 or type(paths[0]) is not str or not paths[0]:
            raise _ProviderSnapshotInvalid
        database = Path(paths[0]).resolve(strict=True)
        observer = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
            isolation_level=None,
            timeout=2.0,
        )
        observer.execute("PRAGMA query_only=ON")
        query_only = observer.execute("PRAGMA query_only").fetchone()
        if query_only is None or len(query_only) != 1 or query_only[0] != 1:
            raise _ProviderSnapshotInvalid
        _main_data_version(observer)
        _main_schema_version(observer)
        return observer
    except (OSError, RuntimeError, sqlite3.Error, UnicodeError, ValueError):
        if observer is not None:
            try:
                observer.close()
            except sqlite3.Error:
                pass
        raise _ProviderSnapshotInvalid from None
    except BaseException:
        if observer is not None:
            try:
                observer.close()
            except sqlite3.Error:
                pass
        raise


def _provider_dependency_ledger_seal(material: object) -> str:
    return hmac.new(
        _PROVIDER_DEPENDENCY_LEDGER_KEY,
        _canonical_json(material).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _neutral_provider_graph_context(query: str) -> dict[str, object]:
    if type(query) is not str:
        raise _ProviderSnapshotInvalid
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
        "temporal_basis": "valid_time",
        "as_of": "",
        "known_at": "",
        "known_at_floor": "",
        "history_complete": True,
        "identity_basis": "current_names",
    }


def _provider_graph_candidate_ids(
    tenant_id: str,
    value: object,
) -> tuple[str, ...]:
    """Extract the only code-owned graph scope from an exact card witness."""

    if (
        type(tenant_id) is not str
        or type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not bool
        or type(value[1]) is not tuple
        or (value[0] and value[1])
        or (not value[0] and len(value[1]) > 400)
        or any(type(card) is not dict for card in value[1])
    ):
        raise _ProviderSnapshotInvalid
    identities: list[str] = []
    try:
        for card in value[1]:
            identity = card.get("id")
            if (
                type(identity) is not str
                or not identity
                or len(identity.encode("utf-8")) > 240
                or card.get("user_id") != tenant_id
            ):
                raise _ProviderSnapshotInvalid
            identities.append(identity)
    except UnicodeError:
        raise _ProviderSnapshotInvalid from None
    result = tuple(identities)
    if len(result) != len(set(result)) or result != tuple(sorted(result)):
        raise _ProviderSnapshotInvalid
    return result


def _provider_proved_result(
    value: object,
    *,
    expected: type[dict] | type[list],
) -> tuple[dict[str, Any] | list[dict[str, Any]], str]:
    """Validate one storage-owned result plus its opaque topology proof."""

    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not expected
        or type(value[1]) is not str
        or len(value[1]) != 64
        or any(character not in "0123456789abcdef" for character in value[1])
    ):
        raise _ProviderSnapshotInvalid
    return value[0], value[1]


def _provider_graph_omitted_payload(
    request: MemoryExactRequest,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Force a current failed/saturated graph to the exact ``kg=None`` shape."""

    if (
        type(request) is not MemoryExactRequest
        or type(raw) is not dict
        or request.as_of
        or request.known_at
        or type(raw.get("query")) is not str
    ):
        raise _ProviderSnapshotInvalid
    payload = dict(raw)
    payload["entity_matches"] = []
    payload["graph_context"] = {
        "query": raw["query"],
        "expanded": False,
        "as_of": "",
        "known_at": "",
        "known_at_floor": "",
        "history_complete": True,
        "identity_basis": "current_names",
        "temporal_basis": "valid_time",
        "roots": [],
        "nodes": [],
        "entities": [],
        "relations": [],
        "paths": [],
        "paths_matched_at_least": 0,
        "paths_truncated": False,
    }
    return payload


def _provider_read_value_sha256(value: object) -> str:
    """Digest one exact built-in value without text-decoding binary material."""

    digest = hashlib.sha256()
    used_bytes = 0
    used_nodes = 0

    def emit(tag: bytes, payload: bytes = b"") -> None:
        nonlocal used_bytes
        used_bytes += len(tag) + len(payload) + 8
        if used_bytes > 32 * 1024 * 1024:
            raise _ProviderResourceExceeded
        digest.update(tag)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def visit(item: object, depth: int) -> None:
        nonlocal used_nodes
        used_nodes += 1
        if depth > 24 or used_nodes > 200_000:
            raise _ProviderResourceExceeded
        item_type = type(item)
        if item is None:
            emit(b"n")
        elif item_type is bool:
            emit(b"b", b"1" if item else b"0")
        elif item_type is int:
            emit(b"i", str(item).encode("ascii"))
        elif item_type is float:
            if not math.isfinite(item):
                raise _ProviderSnapshotInvalid
            emit(b"f", struct.pack(">d", item))
        elif item_type is str:
            try:
                encoded = item.encode("utf-8")
            except UnicodeError:
                raise _ProviderSnapshotInvalid from None
            emit(b"s", encoded)
        elif item_type is bytes:
            emit(b"y", item)
        elif item_type is tuple or item_type is list:
            emit(b"t" if item_type is tuple else b"l", len(item).to_bytes(8, "big"))
            for child in item:
                visit(child, depth + 1)
        elif item_type is dict:
            if any(type(key) is not str for key in item):
                raise _ProviderSnapshotInvalid
            emit(b"d", len(item).to_bytes(8, "big"))
            try:
                keys = sorted(item, key=lambda key: key.encode("utf-8"))
            except UnicodeError:
                raise _ProviderSnapshotInvalid from None
            for key in keys:
                visit(key, depth + 1)
                visit(item[key], depth + 1)
        elif item_type is set or item_type is frozenset:
            if any(type(child) is not str for child in item):
                raise _ProviderSnapshotInvalid
            emit(b"e" if item_type is set else b"r", len(item).to_bytes(8, "big"))
            try:
                children = sorted(item, key=lambda child: child.encode("utf-8"))
            except UnicodeError:
                raise _ProviderSnapshotInvalid from None
            for child in children:
                visit(child, depth + 1)
        else:
            raise _ProviderSnapshotInvalid

    visit(value, 0)
    return digest.hexdigest()


def _freeze_provider_read_arguments(value: object, *, depth: int = 0) -> object:
    """Copy the small replay key into an immutable, exact-type-only tree."""

    if depth > 12:
        raise _ProviderSnapshotInvalid
    value_type = type(value)
    if value is None or value_type in (bool, int, float, str, bytes):
        if value_type is float and not math.isfinite(value):
            raise _ProviderSnapshotInvalid
        return value
    if value_type is tuple:
        return tuple(
            _freeze_provider_read_arguments(item, depth=depth + 1) for item in value
        )
    raise _ProviderSnapshotInvalid


class _ProviderReadWitness:
    """One immutable body-free binding between a bounded call and its result."""

    __slots__ = (
        "_arguments",
        "_arguments_sha256",
        "_kind",
        "_result_sha256",
        "_seal",
    )

    def __init__(self, kind: str, arguments: tuple[object, ...], result: object) -> None:
        if type(kind) is not str or kind not in _PROVIDER_READ_KINDS:
            raise _ProviderSnapshotInvalid
        frozen = _freeze_provider_read_arguments(arguments)
        if type(frozen) is not tuple:
            raise _ProviderSnapshotInvalid
        arguments_sha256 = _provider_read_value_sha256(frozen)
        result_sha256 = _provider_read_value_sha256(result)
        material = {
            "arguments_sha256": arguments_sha256,
            "kind": kind,
            "result_sha256": result_sha256,
            "schema": _PROVIDER_READ_SET_SCHEMA,
        }
        object.__setattr__(self, "_arguments", frozen)
        object.__setattr__(self, "_arguments_sha256", arguments_sha256)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_result_sha256", result_sha256)
        object.__setattr__(self, "_seal", _provider_dependency_ledger_seal(material))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("memory-exact provider read witness is immutable")

    def __repr__(self) -> str:
        return "_ProviderReadWitness(body_free=True)"

    def __copy__(self) -> NoReturn:
        raise TypeError("memory-exact provider read witness is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory-exact provider read witness is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory-exact provider read witness is process-private")

    def _is_process_owned(self) -> bool:
        try:
            material = {
                "arguments_sha256": self._arguments_sha256,
                "kind": self._kind,
                "result_sha256": self._result_sha256,
                "schema": _PROVIDER_READ_SET_SCHEMA,
            }
            return (
                type(self._kind) is str
                and self._kind in _PROVIDER_READ_KINDS
                and type(self._arguments) is tuple
                and _provider_read_value_sha256(self._arguments)
                == self._arguments_sha256
                and type(self._result_sha256) is str
                and len(self._result_sha256) == 64
                and type(self._seal) is str
                and hmac.compare_digest(
                    self._seal,
                    _provider_dependency_ledger_seal(material),
                )
            )
        except Exception:  # noqa: BLE001 - integrity probes are fail-closed
            return False

    def matches(self, value: object) -> bool:
        return self._is_process_owned() and hmac.compare_digest(
            self._result_sha256,
            _provider_read_value_sha256(value),
        )


class _ProviderReadSet:
    """Collect and seal the fixed-size exact bounded-provider operation set."""

    __slots__ = (
        "_by_key",
        "_finalized",
        "_graph_candidate_cards_sha256",
        "_graph_saturated",
        "_graph_suppressed_at",
        "_lock",
        "_poisoned",
        "_seal",
        "_witnesses",
    )

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], _ProviderReadWitness] | None = {}
        self._finalized = False
        self._graph_candidate_cards_sha256: str | None = None
        self._graph_saturated: bool | None = None
        self._graph_suppressed_at: int | None = None
        self._lock = threading.Lock()
        self._poisoned: str | None = None
        self._seal: str | None = None
        self._witnesses: tuple[_ProviderReadWitness, ...] = ()

    def __repr__(self) -> str:
        return "_ProviderReadSet(body_free=True, bounded=True)"

    def __copy__(self) -> NoReturn:
        raise TypeError("memory-exact provider read set is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory-exact provider read set is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory-exact provider read set is process-private")

    def poison(self, *, resource: bool = False) -> None:
        with self._lock:
            if resource or self._poisoned is None:
                self._poisoned = "resource" if resource else "invalid"

    def suppress_graph(self) -> None:
        with self._lock:
            if self._finalized or self._by_key is None or self._poisoned is not None:
                raise _ProviderSnapshotInvalid
            if self._graph_suppressed_at is None:
                self._graph_suppressed_at = len(self._by_key)

    @property
    def graph_suppressed(self) -> bool:
        with self._lock:
            return self._graph_suppressed_at is not None

    def observe(
        self,
        kind: str,
        arguments: tuple[object, ...],
        result: object,
    ) -> object:
        try:
            witness = _ProviderReadWitness(kind, arguments, result)
            if kind == "graph_candidate_cards":
                if (
                    len(witness._arguments) != 1
                    or type(witness._arguments[0]) is not str
                ):
                    raise _ProviderSnapshotInvalid
                _provider_graph_candidate_ids(witness._arguments[0], result)
                graph_saturated: bool | None = result[0]
                graph_candidate_cards_sha256: str | None = _provider_read_value_sha256(
                    result[1]
                )
            else:
                graph_saturated = None
                graph_candidate_cards_sha256 = None
        except _ProviderResourceExceeded:
            self.poison(resource=True)
            raise
        except BaseException:
            self.poison()
            raise
        key = (witness._kind, witness._arguments_sha256)
        with self._lock:
            if self._finalized or self._by_key is None or self._poisoned is not None:
                raise _ProviderSnapshotInvalid
            previous = self._by_key.get(key)
            if previous is not None:
                if previous._arguments != witness._arguments or not previous.matches(result):
                    self._poisoned = "invalid"
                    raise _ProviderSnapshotInvalid
                if kind == "graph_candidate_cards" and (
                    self._graph_saturated is not graph_saturated
                    or self._graph_candidate_cards_sha256
                    != graph_candidate_cards_sha256
                ):
                    self._poisoned = "invalid"
                    raise _ProviderSnapshotInvalid
                return result
            if len(self._by_key) >= _PROVIDER_READ_SET_MAX_OPERATIONS:
                self._poisoned = "resource"
                raise _ProviderResourceExceeded
            self._by_key[key] = witness
            if kind == "graph_candidate_cards":
                if self._graph_saturated is not None:
                    self._poisoned = "invalid"
                    raise _ProviderSnapshotInvalid
                self._graph_saturated = graph_saturated
                self._graph_candidate_cards_sha256 = graph_candidate_cards_sha256
        return result

    def require_collecting(self) -> None:
        with self._lock:
            if self._poisoned == "resource":
                raise _ProviderResourceExceeded
            if self._poisoned is not None or self._finalized or self._by_key is None:
                raise _ProviderSnapshotInvalid

    def finalize(self) -> None:
        with self._lock:
            if self._poisoned == "resource":
                raise _ProviderResourceExceeded
            if self._poisoned is not None or self._finalized or self._by_key is None:
                raise _ProviderSnapshotInvalid
            witnesses = tuple(self._by_key.values())
            if (
                type(self._graph_saturated) is not bool
                or type(self._graph_candidate_cards_sha256) is not str
                or len(self._graph_candidate_cards_sha256) != 64
                or (
                    self._graph_suppressed_at is not None
                    and (
                        type(self._graph_suppressed_at) is not int
                        or not 0 <= self._graph_suppressed_at < len(witnesses)
                        or witnesses[self._graph_suppressed_at]._kind
                        not in (_PROVIDER_GRAPH_READ_KINDS | {"graph_candidate_cards"})
                    )
                )
                or (
                    self._graph_saturated is True
                    and any(
                        witness._kind in _PROVIDER_GRAPH_READ_KINDS
                        for witness in witnesses
                    )
                )
                or sum(
                    witness._kind == "graph_candidate_cards"
                    for witness in witnesses
                )
                != 1
                or any(not witness._is_process_owned() for witness in witnesses)
            ):
                raise _ProviderSnapshotInvalid
            material = {
                "count": len(witnesses),
                "graph_candidate_cards_sha256": self._graph_candidate_cards_sha256,
                "graph_saturated": self._graph_saturated,
                "graph_suppressed_at": self._graph_suppressed_at,
                "schema": _PROVIDER_READ_SET_SCHEMA,
                "witnesses": [witness._seal for witness in witnesses],
            }
            self._witnesses = witnesses
            self._seal = _provider_dependency_ledger_seal(material)
            self._by_key = None
            self._finalized = True

    def _is_process_owned(self) -> bool:
        try:
            with self._lock:
                witnesses = self._witnesses
                seal = self._seal
                valid_shape = (
                    self._finalized
                    and self._by_key is None
                    and self._poisoned is None
                    and type(witnesses) is tuple
                    and len(witnesses) <= _PROVIDER_READ_SET_MAX_OPERATIONS
                    and type(self._graph_saturated) is bool
                    and type(self._graph_candidate_cards_sha256) is str
                    and len(self._graph_candidate_cards_sha256) == 64
                    and (
                        self._graph_suppressed_at is None
                        or (
                            type(self._graph_suppressed_at) is int
                            and 0 <= self._graph_suppressed_at < len(witnesses)
                        )
                    )
                    and type(seal) is str
                    and len(seal) == 64
                )
            if not valid_shape or any(
                type(witness) is not _ProviderReadWitness
                or not witness._is_process_owned()
                for witness in witnesses
            ):
                return False
            material = {
                "count": len(witnesses),
                "graph_candidate_cards_sha256": self._graph_candidate_cards_sha256,
                "graph_saturated": self._graph_saturated,
                "graph_suppressed_at": self._graph_suppressed_at,
                "schema": _PROVIDER_READ_SET_SCHEMA,
                "witnesses": [witness._seal for witness in witnesses],
            }
            return hmac.compare_digest(seal, _provider_dependency_ledger_seal(material))
        except Exception:  # noqa: BLE001 - integrity probes are fail-closed
            return False

    def seal_sha256(self) -> str:
        if not self._is_process_owned() or self._seal is None:
            raise _ProviderSnapshotInvalid
        return self._seal

    def replay(self, conn: sqlite3.Connection, graph: KnowledgeGraph) -> None:
        if (
            type(conn) is not sqlite3.Connection
            or not conn.in_transaction
            or type(graph) is not KnowledgeGraph
            or type(graph.storage) is not FridayStorage
            or _current_storage_connection(graph.storage) is not conn
        ):
            raise _ProviderSnapshotInvalid
        if not self._is_process_owned():
            raise _ProviderSnapshotInvalid
        witnesses = self._witnesses
        from friday.storage._memory_exact_internal import (
            MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES,
            _replay_memory_exact_provider_graph_operation_in_transaction,
            _replay_memory_exact_provider_read_in_transaction,
        )

        used_bytes = 0

        def commit_bytes(size: int) -> None:
            nonlocal used_bytes
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise _ProviderSnapshotInvalid
            total = used_bytes + size
            if total > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                raise _ProviderResourceExceeded
            used_bytes = total

        graph_saturated: bool | None = None
        candidate_entity_ids: tuple[str, ...] | None = None
        replay_suppressed = False
        for ordinal, witness in enumerate(witnesses):
            if witness._kind == "graph_candidate_cards":
                budget = _ProviderStagedReadBudget()
                try:
                    value = _replay_memory_exact_provider_read_in_transaction(
                        conn,
                        allow_active_managed_context=True,
                        kind=witness._kind,
                        arguments=witness._arguments,
                        reserve_bytes=budget.reserve,
                    )
                    commit_bytes(budget.finish())
                except Exception:  # noqa: BLE001 - graph failures are normalized
                    if self._graph_suppressed_at != ordinal:
                        raise
                    replay_suppressed = True
                    value = (True, ())
                if (
                    type(value) is not tuple
                    or len(value) != 2
                    or type(value[0]) is not bool
                    or type(value[1]) is not tuple
                ):
                    raise _ProviderSnapshotInvalid
                graph_saturated = value[0]
                candidate_entity_ids = _provider_graph_candidate_ids(
                    witness._arguments[0],
                    value,
                )
            elif witness._kind in _PROVIDER_STORAGE_READ_KINDS:
                if witness._kind == "relation_history_status" and (
                    candidate_entity_ids is None
                    or len(witness._arguments) != 3
                    or witness._arguments[2] != candidate_entity_ids
                ):
                    raise _ProviderSnapshotInvalid
                value = _replay_memory_exact_provider_read_in_transaction(
                    conn,
                    allow_active_managed_context=True,
                    kind=witness._kind,
                    arguments=witness._arguments,
                    reserve_bytes=commit_bytes,
                )
                if witness._kind == "relation_history_status":
                    _provider_proved_result(value, expected=dict)
            elif witness._kind == "known_vocabulary":
                value = set()
            elif witness._kind == "vocabulary_terms":
                value = []
            elif witness._kind == "get_knowledge_usage":
                value = {}
            elif witness._kind == "get_chunk_spans":
                value = {}
            elif witness._kind in _PROVIDER_GRAPH_READ_KINDS:
                arguments = witness._arguments
                if (
                    graph_saturated is not False
                    or self._graph_saturated is not False
                    or len(arguments) < 2
                    or type(arguments[0]) is not str
                    or type(arguments[1]) is not str
                ):
                    raise _ProviderSnapshotInvalid
                if witness._kind == "graph_search_entities" and (
                    len(arguments) != 4
                    or isinstance(arguments[2], bool)
                    or not isinstance(arguments[2], int)
                    or arguments[3] is not None
                ):
                    raise _ProviderSnapshotInvalid
                if witness._kind == "graph_context_for_query" and (
                    len(arguments) != 8
                    or isinstance(arguments[2], bool)
                    or not isinstance(arguments[2], int)
                    or isinstance(arguments[3], bool)
                    or not isinstance(arguments[3], int)
                    or isinstance(arguments[4], bool)
                    or not isinstance(arguments[4], int)
                    or (
                        arguments[5] is not None
                        and type(arguments[5]) is not tuple
                    )
                    or type(arguments[6]) is not str
                    or type(arguments[7]) is not str
                ):
                    raise _ProviderSnapshotInvalid
                if replay_suppressed:
                    value = (
                        (
                            []
                            if witness._kind == "graph_search_entities"
                            else _neutral_provider_graph_context(arguments[1])
                        ),
                        _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256,
                    )
                else:
                    budget = _ProviderStagedReadBudget()
                    try:
                        if candidate_entity_ids is None:
                            raise _ProviderSnapshotInvalid
                        value = _replay_memory_exact_provider_graph_operation_in_transaction(
                            conn,
                            storage=graph.storage,
                            allow_active_managed_context=True,
                            kind=witness._kind,
                            arguments=arguments,
                            candidate_entity_ids=candidate_entity_ids,
                            reserve_bytes=budget.reserve,
                        )
                        _provider_proved_result(
                            value,
                            expected=(
                                list
                                if witness._kind == "graph_search_entities"
                                else dict
                            ),
                        )
                        commit_bytes(budget.finish())
                    except Exception:  # noqa: BLE001 - compare the sealed sentinel
                        if self._graph_suppressed_at != ordinal:
                            raise
                        replay_suppressed = True
                        value = (
                            (
                                []
                                if witness._kind == "graph_search_entities"
                                else _neutral_provider_graph_context(arguments[1])
                            ),
                            _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256,
                        )
            else:
                raise _ProviderSnapshotInvalid
            if not witness.matches(value):
                from friday.storage._memory_exact_internal import MemoryExactStorageDrift

                raise MemoryExactStorageDrift("memory exact provider read set changed")
        if (
            graph_saturated is not self._graph_saturated
            or replay_suppressed != (self._graph_suppressed_at is not None)
            or not self._is_process_owned()
        ):
            raise _ProviderSnapshotInvalid


class _ProviderDependencyLedger:
    """Immutable O(1) mutation epoch plus one sealed bounded provider read set."""

    __slots__ = (
        "_connection",
        "_graph",
        "_observer",
        "_observer_data_version",
        "_observer_lock",
        "_provider_configuration_sha256",
        "_read_set",
        "_request_identity_sha256",
        "_schema_version",
        "_seal",
        "_searcher",
        "_source_data_version",
        "_storage",
        "_storage_generation",
        "_temp_schema_version",
        "_tenant_sha256",
        "_total_changes",
        "_trusted_function_schema_sha256",
        "_trusted_temp_schema_sha256",
    )

    def __init__(
        self,
        *,
        storage: FridayStorage,
        conn: sqlite3.Connection,
        observer: sqlite3.Connection,
        observer_lock: Any,
        observer_generation: int,
        read_set: _ProviderReadSet,
        request: MemoryExactRequest,
        searcher: HybridSearcher,
        graph: KnowledgeGraph,
        trusted_function_schema_sha256: str,
        trusted_temp_schema_sha256: str,
    ) -> None:
        if (
            type(storage) is not FridayStorage
            or type(conn) is not sqlite3.Connection
            or conn is not _current_storage_connection(storage)
            or conn.in_transaction
            or type(observer) is not sqlite3.Connection
            or observer.in_transaction
            or type(observer_lock) is not _LOCK_TYPE
            or isinstance(observer_generation, bool)
            or not isinstance(observer_generation, int)
            or observer_generation < 0
            or type(storage._generation) is not int  # noqa: SLF001
            or storage._generation != observer_generation  # noqa: SLF001
            or type(read_set) is not _ProviderReadSet
            or type(request) is not MemoryExactRequest
            or type(searcher) is not HybridSearcher
            or type(graph) is not KnowledgeGraph
        ):
            raise _ProviderSnapshotInvalid
        _normalize_provider_connection(conn)
        if (
            type(trusted_function_schema_sha256) is not str
            or len(trusted_function_schema_sha256) != 64
            or _function_schema_sha256(conn) != trusted_function_schema_sha256
            or type(trusted_temp_schema_sha256) is not str
            or len(trusted_temp_schema_sha256) != 64
            or _temp_schema_sha256(conn) != trusted_temp_schema_sha256
        ):
            raise _ProviderSnapshotInvalid
        source_data_version = _main_data_version(conn)
        total_changes = conn.total_changes
        schema_version = _main_schema_version(conn)
        temp_schema_version = _temp_schema_version(conn)
        if (
            isinstance(total_changes, bool)
            or not isinstance(total_changes, int)
            or total_changes < 0
        ):
            raise _ProviderSnapshotInvalid
        with observer_lock:
            observer_data_version = _main_data_version(observer)
            observer_schema_version = _main_schema_version(observer)
        if (
            _main_data_version(conn) != source_data_version
            or observer_schema_version != schema_version
        ):
            raise _ProviderSnapshotInvalid
        request_identity_sha256 = request.identity_sha256()
        tenant_sha256 = hashlib.sha256(request.tenant_id.encode("utf-8")).hexdigest()
        provider_configuration_sha256 = _provider_configuration_sha256(
            storage,
            searcher,
            graph,
        )
        material = {
            "completeness": "sqlite-mutation-epoch",
            "connection_object": id(conn),
            "graph_object": id(graph),
            "observer_data_version": observer_data_version,
            "observer_lock_object": id(observer_lock),
            "observer_object": id(observer),
            "provider_configuration_sha256": provider_configuration_sha256,
            "read_set_object": id(read_set),
            "request_identity_sha256": request_identity_sha256,
            "schema": _PROVIDER_DEPENDENCY_LEDGER_SCHEMA,
            "schema_version": schema_version,
            "searcher_object": id(searcher),
            "source_data_version": source_data_version,
            "storage_generation": observer_generation,
            "storage_object": id(storage),
            "temp_schema_version": temp_schema_version,
            "tenant_sha256": tenant_sha256,
            "total_changes": total_changes,
            "trusted_function_schema_sha256": trusted_function_schema_sha256,
            "trusted_temp_schema_sha256": trusted_temp_schema_sha256,
        }
        object.__setattr__(self, "_connection", conn)
        object.__setattr__(self, "_graph", graph)
        object.__setattr__(self, "_observer", observer)
        object.__setattr__(self, "_observer_data_version", observer_data_version)
        object.__setattr__(self, "_observer_lock", observer_lock)
        object.__setattr__(
            self,
            "_provider_configuration_sha256",
            provider_configuration_sha256,
        )
        object.__setattr__(self, "_read_set", read_set)
        object.__setattr__(self, "_request_identity_sha256", request_identity_sha256)
        object.__setattr__(self, "_schema_version", schema_version)
        object.__setattr__(self, "_searcher", searcher)
        object.__setattr__(self, "_source_data_version", source_data_version)
        object.__setattr__(self, "_storage", storage)
        object.__setattr__(self, "_storage_generation", observer_generation)
        object.__setattr__(self, "_temp_schema_version", temp_schema_version)
        object.__setattr__(self, "_tenant_sha256", tenant_sha256)
        object.__setattr__(self, "_total_changes", total_changes)
        object.__setattr__(
            self,
            "_trusted_function_schema_sha256",
            trusted_function_schema_sha256,
        )
        object.__setattr__(
            self,
            "_trusted_temp_schema_sha256",
            trusted_temp_schema_sha256,
        )
        object.__setattr__(self, "_seal", _provider_dependency_ledger_seal(material))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("memory-exact provider dependency ledger is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("memory-exact provider dependency ledger is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory-exact provider dependency ledger is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory-exact provider dependency ledger is process-private")

    def __repr__(self) -> str:
        return "_ProviderDependencyLedger(body_free=True, o1_epoch=True, bounded_read_set=True)"

    def _material(self) -> dict[str, object]:
        return {
            "completeness": "sqlite-mutation-epoch",
            "connection_object": id(self._connection),
            "graph_object": id(self._graph),
            "observer_data_version": self._observer_data_version,
            "observer_lock_object": id(self._observer_lock),
            "observer_object": id(self._observer),
            "provider_configuration_sha256": self._provider_configuration_sha256,
            "read_set_object": id(self._read_set),
            "request_identity_sha256": self._request_identity_sha256,
            "schema": _PROVIDER_DEPENDENCY_LEDGER_SCHEMA,
            "schema_version": self._schema_version,
            "searcher_object": id(self._searcher),
            "source_data_version": self._source_data_version,
            "storage_generation": self._storage_generation,
            "storage_object": id(self._storage),
            "temp_schema_version": self._temp_schema_version,
            "tenant_sha256": self._tenant_sha256,
            "total_changes": self._total_changes,
            "trusted_function_schema_sha256": self._trusted_function_schema_sha256,
            "trusted_temp_schema_sha256": self._trusted_temp_schema_sha256,
        }

    def _is_epoch_owned(self, request: MemoryExactRequest) -> bool:
        try:
            return (
                type(request) is MemoryExactRequest
                and type(self._storage) is FridayStorage
                and type(self._storage_generation) is int
                and self._storage_generation >= 0
                and type(self._storage._generation) is int  # noqa: SLF001
                and self._storage._generation == self._storage_generation  # noqa: SLF001
                and type(self._connection) is sqlite3.Connection
                and self._connection is _current_storage_connection(self._storage)
                and type(self._observer) is sqlite3.Connection
                and type(self._observer_lock) is _LOCK_TYPE
                and type(self._searcher) is HybridSearcher
                and type(self._graph) is KnowledgeGraph
                and type(self._read_set) is _ProviderReadSet
                and type(self._trusted_function_schema_sha256) is str
                and len(self._trusted_function_schema_sha256) == 64
                and type(self._trusted_temp_schema_sha256) is str
                and len(self._trusted_temp_schema_sha256) == 64
                and request.identity_sha256() == self._request_identity_sha256
                and hashlib.sha256(request.tenant_id.encode("utf-8")).hexdigest()
                == self._tenant_sha256
                and _provider_configuration_sha256(
                    self._storage,
                    self._searcher,
                    self._graph,
                )
                == self._provider_configuration_sha256
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in (
                        self._observer_data_version,
                        self._schema_version,
                        self._source_data_version,
                        self._temp_schema_version,
                        self._total_changes,
                    )
                )
                and hmac.compare_digest(
                    self._seal,
                    _provider_dependency_ledger_seal(self._material()),
                )
            )
        except Exception:  # noqa: BLE001 - integrity probes are fail-closed
            return False

    def _is_process_owned(self, request: MemoryExactRequest) -> bool:
        return self._is_epoch_owned(request) and self._read_set._is_process_owned()

    def seal_sha256(self, request: MemoryExactRequest) -> str:
        if not self._is_process_owned(request):
            raise _ProviderSnapshotInvalid
        return _provider_dependency_ledger_seal(
            {
                "epoch_seal": self._seal,
                "read_set_seal": self._read_set.seal_sha256(),
                "schema": _PROVIDER_DEPENDENCY_LEDGER_SCHEMA,
            }
        )

    def replay_read_set(
        self,
        conn: sqlite3.Connection,
        *,
        request: MemoryExactRequest,
    ) -> None:
        if (
            type(conn) is not sqlite3.Connection
            or conn is not self._connection
            or not self._is_process_owned(request)
        ):
            raise _ProviderSnapshotInvalid
        self._read_set.replay(conn, self._graph)
        if not self._is_process_owned(request):
            raise _ProviderSnapshotInvalid

    def require_stable(
        self,
        conn: sqlite3.Connection,
        *,
        request: MemoryExactRequest,
        local_change_delta: int,
    ) -> None:
        if (
            isinstance(local_change_delta, bool)
            or not isinstance(local_change_delta, int)
            or local_change_delta < 0
            or not self._is_epoch_owned(request)
            or type(conn) is not sqlite3.Connection
            or conn is not self._connection
        ):
            raise _ProviderSnapshotInvalid
        source_data_version = _main_data_version(conn)
        total_changes = conn.total_changes
        source_schema_version = _main_schema_version(conn)
        temp_schema_version = _temp_schema_version(conn)
        if isinstance(total_changes, bool) or not isinstance(total_changes, int):
            raise _ProviderSnapshotInvalid
        with self._observer_lock:
            observer_data_version = _main_data_version(self._observer)
            observer_schema_version = _main_schema_version(self._observer)
        if (
            _main_data_version(conn) != source_data_version
            or total_changes != self._total_changes + local_change_delta
            or source_data_version != self._source_data_version
            or observer_data_version != self._observer_data_version
            or source_schema_version != self._schema_version
            or observer_schema_version != self._schema_version
            or temp_schema_version != self._temp_schema_version
            or _function_schema_sha256(conn) != self._trusted_function_schema_sha256
            or _temp_schema_sha256(conn) != self._trusted_temp_schema_sha256
        ):
            from friday.storage._memory_exact_internal import MemoryExactStorageDrift

            raise MemoryExactStorageDrift("memory exact source changed after ranking")



class _ProviderReadEnvelope:
    """One request-local byte budget and exact source-revision ledger."""

    __slots__ = ("_lock", "_poisoned", "_read_set", "_revisions", "_used_bytes")

    def __init__(self, read_set: _ProviderReadSet) -> None:
        if type(read_set) is not _ProviderReadSet:
            raise _ProviderSnapshotInvalid
        self._lock = threading.Lock()
        self._poisoned: str | None = None
        self._read_set = read_set
        self._revisions: dict[str, tuple[str, str]] = {}
        self._used_bytes = 0

    def poison(self, *, resource: bool = False) -> None:
        with self._lock:
            if resource or self._poisoned is None:
                self._poisoned = "resource" if resource else "invalid"
        self._read_set.poison(resource=resource)

    def require_clean(self) -> None:
        with self._lock:
            if self._poisoned == "resource":
                raise _ProviderResourceExceeded
            if self._poisoned is not None:
                raise _ProviderSnapshotInvalid
        self._read_set.require_collecting()

    def reserve(self, size: int) -> None:
        from friday.storage._memory_exact_internal import MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES

        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            self.poison()
            raise _ProviderSnapshotInvalid
        with self._lock:
            total = self._used_bytes + size
            if total > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                self._poisoned = "resource"
                self._read_set.poison(resource=True)
                raise _ProviderResourceExceeded
            self._used_bytes = total

    def observe(
        self,
        kind: str,
        arguments: tuple[object, ...],
        value: object,
    ) -> object:
        return self._read_set.observe(kind, arguments, value)

    def suppress_graph(self) -> None:
        self._read_set.suppress_graph()

    def commit_staged(self, size: int) -> None:
        from friday.storage._memory_exact_internal import MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES

        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _ProviderSnapshotInvalid
        with self._lock:
            total = self._used_bytes + size
            if total > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                raise _ProviderResourceExceeded
            self._used_bytes = total

    def remember(self, revisions: Mapping[str, tuple[str, str]]) -> None:
        if not isinstance(revisions, Mapping):
            self.poison()
            raise _ProviderSnapshotInvalid
        with self._lock:
            for identity, revision in revisions.items():
                if type(identity) is not str or type(revision) is not tuple or len(revision) != 2:
                    if self._poisoned is None:
                        self._poisoned = "invalid"
                    raise _ProviderSnapshotInvalid
                previous = self._revisions.get(identity)
                if previous is not None and previous != revision:
                    if self._poisoned is None:
                        self._poisoned = "invalid"
                    raise _ProviderSnapshotInvalid
                self._revisions[identity] = revision

    def revisions(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            return dict(self._revisions)


class _ProviderStagedReadBudget:
    """Non-poisoning byte account committed only for a successful graph read."""

    __slots__ = ("_closed", "_used_bytes")

    def __init__(self) -> None:
        self._closed = False
        self._used_bytes = 0

    def reserve(self, size: int) -> None:
        from friday.storage._memory_exact_internal import MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES

        if (
            self._closed
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise _ProviderSnapshotInvalid
        total = self._used_bytes + size
        if total > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
            raise _ProviderResourceExceeded
        self._used_bytes = total

    def finish(self) -> int:
        if self._closed:
            raise _ProviderSnapshotInvalid
        self._closed = True
        return self._used_bytes


class _BoundedProviderStorage:
    """FridayStorage facade that preflights every content-bearing ranker read."""

    __slots__ = (
        "_envelope",
        "_fts_available",
        "_graph_candidate_entity_ids",
        "_request",
        "_storage",
        "_trusted_function_schema_sha256",
        "_trusted_temp_schema_sha256",
    )

    def __init__(
        self,
        storage: FridayStorage,
        envelope: _ProviderReadEnvelope,
        request: MemoryExactRequest,
        trusted_function_schema_sha256: str,
        trusted_temp_schema_sha256: str,
    ) -> None:
        self._storage = storage
        self._envelope = envelope
        self._request = request
        if type(storage._fts_available) is not bool:  # noqa: SLF001
            self._refuse()
        self._fts_available = storage._fts_available  # noqa: SLF001
        self._graph_candidate_entity_ids: tuple[str, ...] | None = None
        self._trusted_function_schema_sha256 = trusted_function_schema_sha256
        self._trusted_temp_schema_sha256 = trusted_temp_schema_sha256

    def __getattr__(self, _name: str) -> Any:
        self._refuse()

    def _require_tenant(self, tenant_id: object) -> str:
        if type(tenant_id) is not str or tenant_id != self._request.tenant_id:
            self._refuse()
        return tenant_id

    def _require_unscoped_author(self, uploaded_by: object) -> None:
        if uploaded_by is not None:
            self._refuse()

    def _refuse(self) -> None:
        self._envelope.poison()
        raise _ProviderSnapshotInvalid

    def _prepare_connection(self) -> sqlite3.Connection:
        try:
            conn = _current_storage_connection(self._storage)
            if conn.in_transaction:
                self._refuse()
            _normalize_provider_connection(conn)
            if (
                _function_schema_sha256(conn) != self._trusted_function_schema_sha256
                or _temp_schema_sha256(conn) != self._trusted_temp_schema_sha256
            ):
                self._refuse()
        except BaseException:
            self._envelope.poison()
            raise
        return conn

    def _read_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        arguments: tuple[object, ...],
    ) -> object:
        from friday.storage._memory_exact_internal import (
            _replay_memory_exact_provider_read_in_transaction,
        )

        try:
            value = _replay_memory_exact_provider_read_in_transaction(
                conn,
                allow_active_managed_context=False,
                kind=kind,
                arguments=arguments,
                reserve_bytes=self._envelope.reserve,
            )
            return self._envelope.observe(kind, arguments, value)
        except BaseException as error:
            self._envelope.poison(
                resource=isinstance(error, _ProviderResourceExceeded)
            )
            raise

    def _load(
        self, conn: sqlite3.Connection, tenant_id: str, identities: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        try:
            value = self._read_in_transaction(
                conn,
                kind="provider_rows",
                arguments=(tenant_id, identities),
            )
            if (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not tuple
                or type(value[1]) is not dict
            ):
                self._refuse()
            rows, revisions = value
            self._envelope.remember(revisions)
            return list(rows)
        except BaseException:
            self._envelope.poison()
            raise

    def ensure_result_sources_in_transaction(
        self,
        conn: sqlite3.Connection,
        tenant_id: str,
        identities: tuple[str, ...],
    ) -> None:
        if (
            type(conn) is not sqlite3.Connection
            or conn is not _current_storage_connection(self._storage)
            or not conn.in_transaction
        ):
            self._refuse()
        tenant = self._require_tenant(tenant_id)
        known = self._envelope.revisions()
        missing = tuple(identity for identity in identities if identity not in known)
        if not missing:
            return
        self._load(conn, tenant, missing)

    def graph_candidate_cards(self) -> tuple[bool, tuple[dict[str, Any], ...]]:
        """Witness either the complete graph corpus or its code-owned saturation."""

        tenant = self._request.tenant_id
        arguments = (tenant,)
        budget = _ProviderStagedReadBudget()
        try:
            self._prepare_connection()
            from friday.storage._memory_exact_internal import (
                _replay_memory_exact_provider_read_in_transaction,
            )

            with read_only_storage_snapshot(self._storage) as conn:
                value = _replay_memory_exact_provider_read_in_transaction(
                    conn,
                    allow_active_managed_context=False,
                    kind="graph_candidate_cards",
                    arguments=arguments,
                    reserve_bytes=budget.reserve,
                )
            self._envelope.commit_staged(budget.finish())
        except Exception as error:  # noqa: BLE001 - graph failures stay body-free
            if self._request.as_of or self._request.known_at:
                self._envelope.poison(
                    resource=isinstance(error, _ProviderResourceExceeded)
                )
                raise
            self._envelope.suppress_graph()
            value = (True, ())
        try:
            candidate_entity_ids = _provider_graph_candidate_ids(tenant, value)
        except _ProviderSnapshotInvalid:
            self._refuse()
        self._envelope.observe("graph_candidate_cards", arguments, value)
        self._graph_candidate_entity_ids = candidate_entity_ids
        return value

    def graph_candidate_entity_ids(self) -> tuple[str, ...]:
        identities = self._graph_candidate_entity_ids
        if type(identities) is not tuple:
            self._refuse()
        return identities

    def search_knowledge(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        self._prepare_connection()
        with read_only_storage_snapshot(self._storage) as conn:
            arguments = (tenant, query, limit, uploaded_by, self._fts_available)
            identities = self._read_in_transaction(
                conn,
                kind="search_knowledge",
                arguments=arguments,
            )
            if type(identities) is not tuple:
                self._refuse()
            return self._load(conn, tenant, identities)

    def list_knowledge_objects(
        self,
        user_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        lifecycle_stage: str | None = None,
        tag: str | None = None,
        entity_id: str | None = None,
        query: str | None = None,
        since: str | None = None,
        until: str | None = None,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        from friday.storage._memory_exact_internal import (
            _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
        )

        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        if since != self._request.since or until != self._request.until:
            self._refuse()
        if (
            offset != 0
            or lifecycle_stage is not None
            or tag is not None
            or entity_id is not None
            or query is not None
        ):
            self._refuse()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= (_MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS)
        ):
            self._refuse()
        self._prepare_connection()
        with read_only_storage_snapshot(self._storage) as conn:
            arguments = (
                tenant,
                limit,
                offset,
                lifecycle_stage,
                tag,
                entity_id,
                query,
                since,
                until,
                uploaded_by,
            )
            identities = self._read_in_transaction(
                conn,
                kind="list_knowledge_objects",
                arguments=arguments,
            )
            if type(identities) is not tuple:
                self._refuse()
            return self._load(conn, tenant, identities)

    def knowledge_ids_in_window(
        self,
        user_id: str,
        *,
        since: str | None = None,
        until: str | None = None,
        uploaded_by: str | None = None,
    ) -> set[str] | None:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        if since != self._request.since or until != self._request.until:
            self._refuse()
        if not since and not until:
            return self._envelope.observe(
                "knowledge_ids_in_window",
                (tenant, since, until, uploaded_by),
                None,
            )

        self._prepare_connection()
        with read_only_storage_snapshot(self._storage) as conn:
            identities = self._read_in_transaction(
                conn,
                kind="knowledge_ids_in_window",
                arguments=(tenant, since, until, uploaded_by),
            )
            if identities is not None and type(identities) is not set:
                self._refuse()
            return identities

    def known_vocabulary(self, terms: Sequence[str]) -> set[str]:
        """Disable the released corpus-wide vocabulary side channel."""

        try:
            invalid = (
                type(terms) not in (list, tuple)
                or len(terms) > 400
                or any(
                    type(term) is not str
                    or not term
                    or len(term.encode("utf-8")) > 512
                    for term in terms
                )
            )
        except UnicodeError:
            invalid = True
        if invalid:
            self._refuse()
        value: set[str] = set()
        self._envelope.observe("known_vocabulary", (tuple(terms),), value)
        return value

    def vocabulary_terms(self, prefixes: Sequence[str], *, limit: int = 400) -> list[str]:
        """No cross-tenant FTS vocabulary may influence an authenticated query."""

        try:
            invalid = (
                type(prefixes) not in (list, tuple)
                or len(prefixes) > 400
                or any(
                    type(prefix) is not str
                    or not prefix
                    or len(prefix.encode("utf-8")) > 512
                    for prefix in prefixes
                )
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 400
            )
        except UnicodeError:
            invalid = True
        if invalid:
            self._refuse()
        value: list[str] = []
        self._envelope.observe(
            "vocabulary_terms",
            (tuple(prefixes), limit),
            value,
        )
        return value

    def count_knowledge_objects(
        self,
        user_id: str,
        *,
        uploaded_by: str | None = None,
    ) -> int:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        self._prepare_connection()
        with read_only_storage_snapshot(self._storage) as conn:
            value = self._read_in_transaction(
                conn,
                kind="count_knowledge_objects",
                arguments=(tenant, uploaded_by),
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._refuse()
        return value

    def relation_history_status(self, user_id: str, known_at: str = "") -> dict[str, Any]:
        tenant = self._require_tenant(user_id)
        if type(known_at) is not str or known_at != (self._request.known_at or ""):
            self._refuse()
        candidate_entity_ids = self.graph_candidate_entity_ids()
        self._prepare_connection()
        # The released method normally persists a logical-clock observation for
        # a new historical boundary.  This adapter is effect_class=read, so the
        # storage helper runs the same validator through a SELECT-only view and
        # requires that the promise was already durable.
        budget = _ProviderStagedReadBudget()
        try:
            from friday.storage._memory_exact_internal import (
                _replay_memory_exact_provider_read_in_transaction,
            )

            arguments = (tenant, known_at, candidate_entity_ids)
            with read_only_storage_snapshot(self._storage) as conn:
                value = _replay_memory_exact_provider_read_in_transaction(
                    conn,
                    allow_active_managed_context=False,
                    kind="relation_history_status",
                    arguments=arguments,
                    reserve_bytes=budget.reserve,
                )
            released, _proof_sha256 = _provider_proved_result(value, expected=dict)
            self._envelope.commit_staged(budget.finish())
            self._envelope.observe("relation_history_status", arguments, value)
            return released
        except BaseException as error:
            self._envelope.poison(
                resource=isinstance(error, _ProviderResourceExceeded)
            )
            raise

    def get_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        from friday.retrieval import _USAGE_WEIGHT

        tenant = self._require_tenant(user_id)
        identities = self._bounded_id_sequence(knowledge_object_ids, maximum=400)
        # The released usage coefficient is exactly zero.  Its rows cannot alter
        # ordering, selected sources, or the exact projection, so the closed lane
        # omits this diagnostic-only material instead of opening another loader.
        if _USAGE_WEIGHT != 0.0:
            self._refuse()
        value: dict[str, dict[str, Any]] = {}
        self._envelope.observe(
            "get_knowledge_usage",
            (tenant, identities),
            value,
        )
        return value

    def get_chunk_spans(
        self,
        user_id: str,
        model: str,
        keys: Sequence[tuple[str, int]],
        *,
        uploaded_by: str | None = None,
    ) -> dict[tuple[str, int], tuple[int, int]]:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        try:
            if type(keys) not in (list, tuple) or len(keys) > 400:
                self._refuse()
            if type(model) is not str or not model or len(model.encode("utf-8")) > 512:
                self._refuse()
            for key in keys:
                if (
                    type(key) is not tuple
                    or len(key) != 2
                    or type(key[0]) is not str
                    or not key[0]
                    or len(key[0].encode("utf-8")) > 240
                    or isinstance(key[1], bool)
                    or not isinstance(key[1], int)
                    or key[1] < 0
                ):
                    self._refuse()
        except UnicodeError:
            self._refuse()
        # Passage spans only decorate the provider's discarded diagnostic row.
        # Exact storage builds the model excerpt from its own freshly selected
        # source, so omitting them cannot change source order or publication.
        value: dict[tuple[str, int], tuple[int, int]] = {}
        self._envelope.observe(
            "get_chunk_spans",
            (tenant, model, tuple(keys), uploaded_by),
            value,
        )
        return value

    def _bounded_id_sequence(
        self,
        values: Sequence[str],
        *,
        maximum: int,
    ) -> tuple[str, ...]:
        try:
            invalid = (
                type(values) not in (list, tuple)
                or len(values) > maximum
                or any(
                    type(item) is not str
                    or not item
                    or len(item.encode("utf-8")) > 240
                    for item in values
                )
            )
        except UnicodeError:
            invalid = True
        if invalid:
            self._refuse()
        return tuple(values)

    def entity_links_by_document(
        self,
        user_id: str,
        document_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Bound the exact entity-label signal used by the released ranker."""

        tenant = self._require_tenant(user_id)
        identities = self._bounded_id_sequence(document_ids, maximum=400)
        try:
            self._prepare_connection()
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._read_in_transaction(
                    conn,
                    kind="entity_links_by_document",
                    arguments=(tenant, identities),
                )
            if type(value) is not dict:
                self._refuse()
            return value
        except BaseException:
            self._envelope.poison()
            raise

    def feedback_scores(
        self,
        user_id: str,
        document_ids: Sequence[str],
    ) -> dict[str, float]:
        """Bound the per-document aggregate that can affect provider order."""

        tenant = self._require_tenant(user_id)
        identities = self._bounded_id_sequence(document_ids, maximum=400)
        try:
            self._prepare_connection()
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._read_in_transaction(
                    conn,
                    kind="feedback_scores",
                    arguments=(tenant, identities),
                )
            if type(value) is not dict:
                self._refuse()
            return value
        except BaseException:
            self._envelope.poison()
            raise

    def get_user_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        limit: int | None = None,
        uploaded_by: str | None = None,
    ) -> list[tuple[str, bytes]]:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        try:
            self._prepare_connection()
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._read_in_transaction(
                    conn,
                    kind="get_user_embeddings",
                    arguments=(tenant, model, dim, limit, uploaded_by),
                )
            if type(value) is not list:
                self._refuse()
            return value
        except BaseException:
            self._envelope.poison()
            raise

    def get_user_chunk_embeddings(
        self,
        user_id: str,
        model: str,
        dim: int,
        *,
        object_limit: int | None = None,
        row_limit: int | None = None,
        uploaded_by: str | None = None,
    ) -> list[tuple[str, bytes]]:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        try:
            self._prepare_connection()
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._read_in_transaction(
                    conn,
                    kind="get_user_chunk_embeddings",
                    arguments=(
                        tenant,
                        model,
                        dim,
                        object_limit,
                        row_limit,
                        uploaded_by,
                    ),
                )
            if type(value) is not list:
                self._refuse()
            return value
        except BaseException:
            self._envelope.poison()
            raise

    def get_knowledge_object(
        self,
        ko_id: str,
        user_id: str | None = None,
        *,
        uploaded_by: str | None = None,
    ) -> dict[str, Any] | None:
        if user_id is None:
            raise MemoryExactInternalError("memory-exact provider requires an exact tenant")
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        self._prepare_connection()
        with read_only_storage_snapshot(self._storage) as conn:
            identities = self._read_in_transaction(
                conn,
                kind="get_knowledge_object",
                arguments=(tenant, ko_id, uploaded_by),
            )
            if type(identities) is not tuple:
                self._refuse()
            rows = self._load(conn, tenant, identities)
            return rows[0] if rows else None


class _ProviderDenseCache:
    """Force vector reads through the request-local bounded storage facade."""

    __slots__ = ()

    def get(self, *_args: object, **_kwargs: object) -> None:
        return None


class _BoundedProviderGraph:
    """Closed graph surface executed through one SELECT-only transaction view."""

    __slots__ = (
        "_candidate_entity_ids",
        "_envelope",
        "_graph",
        "_request",
        "_storage",
        "_trusted_function_schema_sha256",
        "_trusted_temp_schema_sha256",
        "_unavailable",
    )

    def __init__(
        self,
        graph: KnowledgeGraph,
        storage: FridayStorage,
        envelope: _ProviderReadEnvelope,
        request: MemoryExactRequest,
        candidate_entity_ids: tuple[str, ...],
        trusted_function_schema_sha256: str,
        trusted_temp_schema_sha256: str,
    ) -> None:
        if (
            type(graph) is not KnowledgeGraph
            or type(storage) is not FridayStorage
            or graph.storage is not storage
            or type(candidate_entity_ids) is not tuple
            or len(candidate_entity_ids) > 400
            or len(candidate_entity_ids) != len(set(candidate_entity_ids))
            or any(type(identity) is not str or not identity for identity in candidate_entity_ids)
        ):
            envelope.poison()
            raise _ProviderSnapshotInvalid
        self._graph = graph
        self._storage = storage
        self._envelope = envelope
        self._request = request
        self._candidate_entity_ids = candidate_entity_ids
        self._trusted_function_schema_sha256 = trusted_function_schema_sha256
        self._trusted_temp_schema_sha256 = trusted_temp_schema_sha256
        self._unavailable = False

    def __getattr__(self, _name: str) -> Any:
        self._envelope.poison()
        raise _ProviderSnapshotInvalid

    @property
    def unavailable(self) -> bool:
        return self._unavailable

    def _prepare_connection(self) -> None:
        try:
            conn = _current_storage_connection(self._storage)
            if conn.in_transaction:
                raise _ProviderSnapshotInvalid
            _normalize_provider_connection(conn)
            if (
                _function_schema_sha256(conn) != self._trusted_function_schema_sha256
                or _temp_schema_sha256(conn) != self._trusted_temp_schema_sha256
            ):
                raise _ProviderSnapshotInvalid
        except BaseException:
            self._envelope.poison()
            raise

    def _tenant(self, user_id: object) -> str:
        if (
            type(user_id) is not str
            or user_id != self._request.tenant_id
            or self._graph.storage is not self._storage
        ):
            self._envelope.poison()
            raise _ProviderSnapshotInvalid
        return user_id

    def _operation(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        arguments: tuple[object, ...],
        reserve_bytes: Any,
    ) -> tuple[object, str]:
        from friday.storage._memory_exact_internal import (
            _replay_memory_exact_provider_graph_operation_in_transaction,
        )

        value = _replay_memory_exact_provider_graph_operation_in_transaction(
            conn,
            storage=self._storage,
            allow_active_managed_context=False,
            kind=kind,
            arguments=arguments,
            candidate_entity_ids=self._candidate_entity_ids,
            reserve_bytes=reserve_bytes,
        )
        if type(value) is not tuple:
            raise _ProviderSnapshotInvalid
        return value

    def _bounded_result(
        self,
        value: object,
        *,
        expected: type[dict] | type[list],
        kind: str,
        arguments: tuple[object, ...],
    ) -> Any:
        try:
            released, _proof_sha256 = _provider_proved_result(
                value,
                expected=expected,
            )
        except _ProviderSnapshotInvalid:
            self._envelope.poison()
            raise _ProviderSnapshotInvalid
        self._envelope.observe(kind, arguments, value)
        return released

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
        tenant = self._tenant(user_id)
        try:
            self._prepare_connection()
            if as_of != (self._request.as_of or "") or known_at != (self._request.known_at or ""):
                raise _ProviderSnapshotInvalid
            if (
                type(query) is not str
                or isinstance(depth, bool)
                or not isinstance(depth, int)
                or isinstance(entity_limit, bool)
                or not isinstance(entity_limit, int)
                or isinstance(knowledge_limit, bool)
                or not isinstance(knowledge_limit, int)
                or (
                    seed_knowledge_ids is not None
                    and (
                        type(seed_knowledge_ids) is not list
                        or len(seed_knowledge_ids) > 400
                        or any(
                            type(identity) is not str
                            or not identity
                            or len(identity.encode("utf-8")) > 240
                            for identity in seed_knowledge_ids
                        )
                    )
                )
            ):
                raise _ProviderSnapshotInvalid
            frozen_seeds = (
                None if seed_knowledge_ids is None else tuple(seed_knowledge_ids)
            )
            arguments = (
                tenant,
                query,
                depth,
                entity_limit,
                knowledge_limit,
                frozen_seeds,
                as_of,
                known_at,
            )
        except BaseException:
            self._envelope.poison()
            raise
        if self._unavailable:
            self._bounded_result(
                (
                    _neutral_provider_graph_context(query),
                    _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256,
                ),
                expected=dict,
                kind="graph_context_for_query",
                arguments=arguments,
            )
            raise _ProviderGraphUnavailable
        budget = _ProviderStagedReadBudget()
        suppressed = False
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._operation(
                    conn,
                    kind="graph_context_for_query",
                    arguments=arguments,
                    reserve_bytes=budget.reserve,
                )
            self._prepare_connection()
            if self._graph.storage is not self._storage:
                raise _ProviderSnapshotInvalid
            released, _proof_sha256 = _provider_proved_result(value, expected=dict)
            self._envelope.commit_staged(budget.finish())
        except Exception as error:  # noqa: BLE001 - graph failures stay body-free
            if self._request.as_of or self._request.known_at:
                self._envelope.poison(
                    resource=isinstance(error, _ProviderResourceExceeded)
                )
                raise
            self._unavailable = True
            self._envelope.suppress_graph()
            value = (
                _neutral_provider_graph_context(query),
                _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256,
            )
            suppressed = True
        result = self._bounded_result(
            value,
            expected=dict,
            kind="graph_context_for_query",
            arguments=arguments,
        )
        if suppressed:
            raise _ProviderGraphUnavailable
        return result

    def search_entities(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 10,
        entity_type: Any = None,
    ) -> list[dict[str, Any]]:
        tenant = self._tenant(user_id)
        try:
            self._prepare_connection()
            if (
                type(query) is not str
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or entity_type is not None
            ):
                raise _ProviderSnapshotInvalid
            arguments = (tenant, query, limit, None)
        except BaseException:
            self._envelope.poison()
            raise
        if self._unavailable:
            return self._bounded_result(
                ([], _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256),
                expected=list,
                kind="graph_search_entities",
                arguments=arguments,
            )
        budget = _ProviderStagedReadBudget()
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                value = self._operation(
                    conn,
                    kind="graph_search_entities",
                    arguments=arguments,
                    reserve_bytes=budget.reserve,
                )
            self._prepare_connection()
            if self._graph.storage is not self._storage:
                raise _ProviderSnapshotInvalid
            _provider_proved_result(value, expected=list)
            self._envelope.commit_staged(budget.finish())
        except Exception as error:  # noqa: BLE001 - graph failures stay body-free
            if self._request.as_of or self._request.known_at:
                self._envelope.poison(
                    resource=isinstance(error, _ProviderResourceExceeded)
                )
                raise
            self._unavailable = True
            self._envelope.suppress_graph()
            value = ([], _PROVIDER_GRAPH_SUPPRESSED_PROOF_SHA256)
        return self._bounded_result(
            value,
            expected=list,
            kind="graph_search_entities",
            arguments=arguments,
        )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise MemoryExactInternalError("memory-exact adapter binding is invalid") from None


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryExactAdapterBinding:
    """Static identity consumed by the future archive-search resolver."""

    adapter_id: str = MEMORY_EXACT_INTERNAL_ADAPTER_ID
    capability_id: str = ARCHIVE_SEARCH_ID
    security_ids: tuple[str, ...] = MEMORY_EXACT_SECURITY_IDS
    request_schema: str = MEMORY_EXACT_REQUEST_SCHEMA
    effect_class: str = "read"
    model_visible: bool = False

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            self.adapter_id != MEMORY_EXACT_INTERNAL_ADAPTER_ID
            or self.capability_id != ARCHIVE_SEARCH_ID
            or self.security_ids != MEMORY_EXACT_SECURITY_IDS
            or self.request_schema != MEMORY_EXACT_REQUEST_SCHEMA
            or self.effect_class != "read"
            or self.model_visible is not False
        ):
            raise MemoryExactInternalError("memory-exact adapter binding is not closed")

    def payload(self) -> dict[str, object]:
        self._validate()
        return {
            "adapter_id": self.adapter_id,
            "capability_id": self.capability_id,
            "effect_class": self.effect_class,
            "model_visible": self.model_visible,
            "request_schema": self.request_schema,
            "schema": MEMORY_EXACT_INTERNAL_ADAPTER_SCHEMA,
            "security_ids": list(self.security_ids),
        }

    def canonical_sha256(self) -> str:
        return _sha256(self.payload())


MEMORY_EXACT_ADAPTER_BINDING: Final = MemoryExactAdapterBinding()
_PUBLICATION_REFRESH_FACTORY = object()
_PUBLICATION_REFRESH_KEY = secrets.token_bytes(32)


def _publication_refresh_seal(value: object) -> str:
    return hmac.new(
        _PUBLICATION_REFRESH_KEY,
        _canonical_json(value).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


class MemoryExactPublicationRefresh:
    """Process-private, source-thread-bound final provider refresh result."""

    __slots__ = (
        "_authorization_bindings",
        "_context_authority_sha256",
        "_decision",
        "_dependency_ledger",
        "_page",
        "_provider_snapshot",
        "_seal",
        "_turn_id_sha256",
        "status",
    )

    def __init__(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
        decision: MemoryExactPublicationDecision,
        status: MemoryExactPublicationStatus,
        authorization_bindings: tuple[tuple[str, str, str], ...],
        provider_snapshot: object | None,
        dependency_ledger: _ProviderDependencyLedger | None,
        factory: object = None,
    ) -> None:
        if factory is not _PUBLICATION_REFRESH_FACTORY:
            raise MemoryExactInternalError("memory-exact publication refresh is process-private")
        if (
            type(context) is not AuthenticatedTurnContext
            or type(page) is not MemoryExactPage
            or type(decision) is not MemoryExactPublicationDecision
            or type(status) is not MemoryExactPublicationStatus
            or type(authorization_bindings) is not tuple
        ):
            raise MemoryExactInternalError("memory-exact publication refresh is invalid")
        authorized = status is MemoryExactPublicationStatus.AUTHORIZED
        bindings_valid = all(
            type(item) is tuple and len(item) == 3 and all(type(part) is str and part for part in item)
            for item in authorization_bindings
        )
        if (
            not bindings_valid
            or authorized != (provider_snapshot is not None)
            or authorized != (dependency_ledger is not None)
            or authorized != bool(authorization_bindings)
            or (
                dependency_ledger is not None
                and (
                    type(dependency_ledger) is not _ProviderDependencyLedger
                    or not dependency_ledger._is_process_owned(page.request)
                )
            )
            or (
                authorization_bindings
                and tuple(item[0] for item in authorization_bindings) != MEMORY_EXACT_SECURITY_IDS
            )
        ):
            raise MemoryExactInternalError("memory-exact publication refresh is not closed")
        context_authority_sha256 = context.context_authority_sha256
        turn_id_sha256 = hashlib.sha256(context.turn_id.encode("ascii")).hexdigest()
        material = {
            "authorization_bindings_sha256": _sha256(authorization_bindings),
            "context_authority_sha256": context_authority_sha256,
            "decision_object": id(decision),
            "dependency_ledger_object": (
                None if dependency_ledger is None else id(dependency_ledger)
            ),
            "dependency_ledger_seal": (
                None
                if dependency_ledger is None
                else dependency_ledger.seal_sha256(page.request)
            ),
            "page_authority_handle": page.authority_handle,
            "page_graph_source_set_sha256": page.graph_source_set_sha256,
            "page_object": id(page),
            "page_selection_handle": page.selection_handle,
            "page_snapshot_handle": page.snapshot_handle,
            "provider_object": None if provider_snapshot is None else id(provider_snapshot),
            "status": status.value,
            "turn_id_sha256": turn_id_sha256,
        }
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_authorization_bindings", authorization_bindings)
        object.__setattr__(self, "_context_authority_sha256", context_authority_sha256)
        object.__setattr__(self, "_turn_id_sha256", turn_id_sha256)
        object.__setattr__(self, "_page", page)
        object.__setattr__(self, "_decision", decision)
        object.__setattr__(self, "_provider_snapshot", provider_snapshot)
        object.__setattr__(self, "_dependency_ledger", dependency_ledger)
        object.__setattr__(self, "_seal", _publication_refresh_seal(material))

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("memory-exact publication refresh is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("memory-exact publication refresh is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("memory-exact publication refresh is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("memory-exact publication refresh is process-private")

    def __repr__(self) -> str:
        status = self.status.value if type(self.status) is MemoryExactPublicationStatus else "invalid"
        return f"MemoryExactPublicationRefresh(status={status!r}, body_free=True)"

    def _is_process_owned(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
        decision: MemoryExactPublicationDecision,
    ) -> bool:
        try:
            if (
                type(self.status) is not MemoryExactPublicationStatus
                or self._page is not page
                or self._decision is not decision
                or type(context) is not AuthenticatedTurnContext
                or context.context_authority_sha256 != self._context_authority_sha256
                or hashlib.sha256(context.turn_id.encode("ascii")).hexdigest() != self._turn_id_sha256
                or (self.status is MemoryExactPublicationStatus.AUTHORIZED)
                != (type(self._dependency_ledger) is _ProviderDependencyLedger)
                or (
                    self._dependency_ledger is not None
                    and not self._dependency_ledger._is_process_owned(page.request)
                )
            ):
                return False
            material = {
                "authorization_bindings_sha256": _sha256(self._authorization_bindings),
                "context_authority_sha256": self._context_authority_sha256,
                "decision_object": id(self._decision),
                "dependency_ledger_object": (
                    None if self._dependency_ledger is None else id(self._dependency_ledger)
                ),
                "dependency_ledger_seal": (
                    None
                    if self._dependency_ledger is None
                    else self._dependency_ledger.seal_sha256(page.request)
                ),
                "page_authority_handle": page.authority_handle,
                "page_graph_source_set_sha256": page.graph_source_set_sha256,
                "page_object": id(self._page),
                "page_selection_handle": page.selection_handle,
                "page_snapshot_handle": page.snapshot_handle,
                "provider_object": (None if self._provider_snapshot is None else id(self._provider_snapshot)),
                "status": self.status.value,
                "turn_id_sha256": self._turn_id_sha256,
            }
            return hmac.compare_digest(self._seal, _publication_refresh_seal(material))
        except (AttributeError, MemoryExactInternalError, TypeError, UnicodeError, ValueError):
            return False


class MemoryExactInternalAdapter:
    """Prepare, project and late-revalidate one exact ranked memory page."""

    __slots__ = (
        "_authorization",
        "_dependency_observer",
        "_dependency_observer_generation",
        "_dependency_observer_lock",
        "_graph",
        "_issuer",
        "_searcher",
        "_storage",
        "_trusted_function_schema_sha256",
        "_trusted_temp_schema_sha256",
    )

    def __init__(
        self,
        authorization: AuthorizationService,
        issuer: TurnContextIssuer,
        storage: FridayStorage,
        searcher: HybridSearcher,
        graph: KnowledgeGraph,
    ) -> None:
        if type(authorization) is not AuthorizationService:
            raise TypeError("memory-exact adapter requires AuthorizationService")
        if type(issuer) is not TurnContextIssuer:
            raise TypeError("memory-exact adapter requires TurnContextIssuer")
        if type(storage) is not FridayStorage:
            raise TypeError("memory-exact adapter requires FridayStorage")
        if type(searcher) is not HybridSearcher or searcher.storage is not storage:
            raise TypeError("memory-exact adapter requires the bound HybridSearcher")
        if type(graph) is not KnowledgeGraph or graph.storage is not storage:
            raise TypeError("memory-exact adapter requires the bound KnowledgeGraph")
        if authorization.storage is not storage:
            raise TypeError("memory-exact authorization and storage must share one authority")
        self._authorization = authorization
        self._issuer = issuer
        self._storage = storage
        self._searcher = searcher
        self._graph = graph
        observer: sqlite3.Connection | None = None
        try:
            source = _current_storage_connection(storage)
            generation = storage._generation  # noqa: SLF001
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
            ):
                raise _ProviderSnapshotInvalid
            _normalize_provider_connection(source)
            trusted_function_schema_sha256 = _function_schema_sha256(source)
            trusted_temp_schema_sha256 = _temp_schema_sha256(source)
            observer = _open_main_database_observer(source)
            if (
                type(observer) is not sqlite3.Connection
                or observer.in_transaction
                or storage._generation != generation  # noqa: SLF001
                or _current_storage_connection(storage) is not source
            ):
                raise _ProviderSnapshotInvalid
        except Exception:  # noqa: BLE001 - initialization failures stay body-free
            if observer is not None:
                try:
                    observer.close()
                except sqlite3.Error:
                    pass
            raise MemoryExactInternalError(
                "memory-exact provider connection is unavailable"
            ) from None
        self._trusted_function_schema_sha256 = trusted_function_schema_sha256
        self._trusted_temp_schema_sha256 = trusted_temp_schema_sha256
        self._dependency_observer = observer
        self._dependency_observer_generation = generation
        self._dependency_observer_lock = threading.Lock()

    def __del__(self) -> None:
        try:
            object.__getattribute__(self, "_dependency_observer").close()
        except Exception:  # noqa: BLE001 - destructor must never escape at shutdown
            pass

    @property
    def binding(self) -> MemoryExactAdapterBinding:
        return MEMORY_EXACT_ADAPTER_BINDING

    def _admitted_scope(
        self,
        context: AuthenticatedTurnContext,
        request: MemoryExactRequest,
    ) -> tuple[AuthenticatedTurnContext, ActorContext]:
        if type(context) is not AuthenticatedTurnContext or type(request) is not MemoryExactRequest:
            raise MemoryExactInternalError("memory-exact call requires exact typed inputs")
        admitted = self._issuer.require_context(context)
        authority = admitted.authority
        actor = authority.actor
        if (
            type(actor) is not ActorContext
            or request.tenant_id != authority.tenant_id
            or request.principal_id != authority.person_id
            or request.active_turn_id != admitted.turn_id
            or admitted.identity.authority_sha256 != authority.canonical_sha256()
        ):
            raise MemoryExactInternalError("memory-exact request escaped its authenticated turn")
        return admitted, actor

    def _authorization_bindings(
        self,
        conn: sqlite3.Connection,
        actor: ActorContext,
    ) -> tuple[tuple[str, str, str], ...]:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise MemoryExactInternalError("memory-exact adapter requires a caller-owned SQLite transaction")
        principal_id = actor.own_id
        bindings: list[tuple[str, str, str]] = []
        for security_id in MEMORY_EXACT_SECURITY_IDS:
            decision = self._authorization.authorize_in_transaction(conn, actor, security_id)
            preset_key = decision.preset_key
            if not decision.allowed:
                raise _AuthorizationDenied
            if (
                decision.security_id != security_id
                or decision.user_id != principal_id
                or type(preset_key) is not str
                or not preset_key
                or preset_key != preset_key.strip()
            ):
                raise MemoryExactInternalError("memory-exact transactional authorization binding is invalid")
            bindings.append((security_id, decision.user_id, preset_key))
        return tuple(bindings)

    def _fresh_bindings(
        self,
        actor: ActorContext,
    ) -> tuple[tuple[str, str, str], ...]:
        try:
            source = _current_storage_connection(self._storage)
            _normalize_provider_connection(source)
            with read_only_storage_snapshot(self._storage) as conn:
                return self._authorization_bindings(conn, actor)
        except _AuthorizationDenied:
            raise
        except MemoryExactInternalError:
            raise
        except Exception:  # noqa: BLE001 - storage failures may retain private paths
            raise MemoryExactInternalError(
                "memory-exact authorization storage is unavailable"
            ) from None

    def _require_no_publication_transaction(self) -> None:
        try:
            in_transaction = _current_storage_connection(self._storage).in_transaction
        except MemoryExactInternalError:
            raise
        except Exception:  # noqa: BLE001 - connection diagnostics may retain paths
            raise MemoryExactInternalError(
                "memory-exact provider connection is unavailable"
            ) from None
        if in_transaction:
            raise MemoryExactInternalError(
                "memory-exact provider refresh requires no active publication transaction"
            )

    def _storage_authority(
        self,
        conn: sqlite3.Connection,
        *,
        admitted: AuthenticatedTurnContext,
        actor: ActorContext,
        request: MemoryExactRequest,
        expected_bindings: tuple[tuple[str, str, str], ...] | None,
    ) -> Any:
        from friday.storage._memory_exact_internal import (
            _issue_memory_exact_storage_authority_in_transaction,
        )

        authorization_bindings = self._authorization_bindings(conn, actor)
        if expected_bindings is not None and authorization_bindings != expected_bindings:
            raise _AuthorizationDenied
        return _issue_memory_exact_storage_authority_in_transaction(
            conn,
            request=request,
            tenant_id=actor.user_id,
            principal_id=actor.own_id,
            turn_id=admitted.turn_id,
            turn_authority_sha256=admitted.identity.authority_sha256,
            context_authority_sha256=admitted.context_authority_sha256,
            tenant_binding_sha256=admitted.authority.tenant_binding_sha256,
            person_binding_sha256=admitted.authority.person_binding_sha256,
            adapter_binding_sha256=self.binding.canonical_sha256(),
            authorization_bindings=authorization_bindings,
        )

    async def _provider_snapshot(
        self,
        request: MemoryExactRequest,
        *,
        admitted: AuthenticatedTurnContext,
        actor: ActorContext,
        expected_bindings: tuple[tuple[str, str, str], ...],
    ) -> tuple[Any, _ProviderDependencyLedger]:
        """Run the ranker inside one authenticated O(1) mutation epoch."""

        self._require_no_publication_transaction()
        from friday.storage._memory_exact_internal import (
            MemoryExactStorageDrift,
            _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
        )

        read_set = _ProviderReadSet()
        try:
            dependency_ledger = _ProviderDependencyLedger(
                storage=self._storage,
                conn=_current_storage_connection(self._storage),
                observer=self._dependency_observer,
                observer_lock=self._dependency_observer_lock,
                observer_generation=self._dependency_observer_generation,
                read_set=read_set,
                request=request,
                searcher=self._searcher,
                graph=self._graph,
                trusted_function_schema_sha256=(
                    self._trusted_function_schema_sha256
                ),
                trusted_temp_schema_sha256=self._trusted_temp_schema_sha256,
            )
            with read_only_storage_snapshot(self._storage) as conn:
                self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=request,
                    expected_bindings=expected_bindings,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
        except _AuthorizationDenied:
            raise
        except (
            MemoryExactContractError,
            _ProviderSnapshotInvalid,
            sqlite3.Error,
        ):
            raise MemoryExactInternalError(
                "memory-exact provider dependency ledger is unavailable"
            ) from None
        graph_expansion = bool(request.as_of or request.known_at or is_relational_query(request.query))
        envelope = _ProviderReadEnvelope(read_set)
        bounded_storage = _BoundedProviderStorage(
            self._storage,
            envelope,
            request,
            self._trusted_function_schema_sha256,
            self._trusted_temp_schema_sha256,
        )
        provider_searcher = copy.copy(self._searcher)
        provider_searcher.storage = bounded_storage
        provider_searcher._channel_weights = dict(self._searcher._channel_weights)  # noqa: SLF001
        provider_searcher._ablate = frozenset(self._searcher._ablate)  # noqa: SLF001
        if provider_searcher.embeddings is not None:
            provider_searcher.embeddings = copy.copy(provider_searcher.embeddings)
        if (
            isinstance(provider_searcher._pool_max, bool)  # noqa: SLF001
            or not isinstance(provider_searcher._pool_max, int)  # noqa: SLF001
            or not 1
            <= provider_searcher._pool_max  # noqa: SLF001
            <= _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS
        ):
            raise MemoryExactInternalError("memory-exact provider pool exceeds its closed row bound")
        provider_searcher._dense_cache = _ProviderDenseCache()  # noqa: SLF001
        provider_searcher._vector_cache = OrderedDict()  # noqa: SLF001
        provider_searcher._field_vector_cache = OrderedDict()  # noqa: SLF001
        provider_searcher._entity_vector_cache = OrderedDict()  # noqa: SLF001
        provider_searcher._vector_cache_lock = threading.Lock()  # noqa: SLF001
        provider_searcher._entity_links_by_document = (  # noqa: SLF001
            bounded_storage.entity_links_by_document
        )
        provider_searcher._feedback_scores = bounded_storage.feedback_scores  # noqa: SLF001
        try:
            graph_saturated = bounded_storage.graph_candidate_cards()[0]
            candidate_entity_ids = bounded_storage.graph_candidate_entity_ids()
            if graph_saturated and (request.as_of or request.known_at):
                raise _ProviderSnapshotInvalid
            provider_graph = _BoundedProviderGraph(
                self._graph,
                self._storage,
                envelope,
                request,
                candidate_entity_ids,
                self._trusted_function_schema_sha256,
                self._trusted_temp_schema_sha256,
            )
            raw = await provider_searcher.search(
                request.tenant_id,
                request.query,
                limit=request.snapshot_limit,
                include_entities=True,
                kg=None if graph_saturated else provider_graph,
                graph_expansion=graph_expansion and not graph_saturated,
                explain=False,
                since=request.since or None,
                until=request.until or None,
                as_of=request.as_of,
                known_at=request.known_at,
                record_usage=False,
            )
            self._require_no_publication_transaction()
            _normalize_provider_connection(_current_storage_connection(self._storage))
            self._issuer.require_context(admitted)
            envelope.require_clean()
            # The provider may yield a valid local identity that none of its
            # bounded storage calls loaded. Reauthorize after the await and
            # verify the global revision ledger before inspecting any provider
            # callback-capable value or issuing source/material SQL.
            with read_only_storage_snapshot(self._storage) as conn:
                self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=request,
                    expected_bindings=expected_bindings,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
                if type(raw) is not dict:
                    raise _ProviderSnapshotInvalid
                raw_results = raw.get("results")
                if (
                    type(raw_results) not in (list, tuple)
                    or len(raw_results) > request.snapshot_limit
                    or any(type(item) is not dict for item in raw_results)
                ):
                    raise _ProviderSnapshotInvalid
                result_ids: list[str] = []
                for item in raw_results:
                    identity = item.get("id")
                    if (
                        type(identity) is not str
                        or not identity
                        or len(identity.encode("utf-8")) > 240
                    ):
                        raise _ProviderSnapshotInvalid
                    result_ids.append(identity)
                bounded_storage.ensure_result_sources_in_transaction(
                    conn,
                    request.tenant_id,
                    tuple(result_ids),
                )
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
                from friday.storage._memory_exact_internal import (
                    _create_memory_exact_provider_snapshot,
                )

                try:
                    graph_omitted = (
                        graph_saturated
                        or provider_graph.unavailable
                        or read_set.graph_suppressed
                    )
                    provider_payload = (
                        _provider_graph_omitted_payload(request, raw)
                        if graph_omitted
                        else raw
                    )
                    provider_snapshot = _create_memory_exact_provider_snapshot(
                        request,
                        provider_payload,
                        envelope.revisions(),
                        graph_saturated=graph_omitted,
                    )
                except Exception:  # noqa: BLE001 - provider values may retain private material
                    raise _ProviderSnapshotInvalid from None
                envelope.require_clean()
                read_set.finalize()
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
            dependency_ledger.require_stable(
                _current_storage_connection(self._storage),
                request=request,
                local_change_delta=0,
            )
            return provider_snapshot, dependency_ledger
        except (_AuthorizationDenied, TurnContextError):
            raise
        except _ProviderResourceExceeded:
            raise MemoryExactInternalError(
                "memory-exact provider exceeded its aggregate byte bound"
            ) from None
        except (MemoryError, OverflowError, RecursionError):
            raise MemoryExactInternalError("memory-exact provider exceeded its resource bound") from None
        except MemoryExactStorageDrift:
            raise
        except _ProviderSnapshotInvalid:
            raise MemoryExactInternalError("memory-exact provider snapshot is invalid") from None
        except Exception:  # noqa: BLE001 - every provider failure can retain private material
            raise MemoryExactInternalError("memory-exact provider is unavailable") from None

    async def prepare(
        self,
        *,
        context: AuthenticatedTurnContext,
        request: MemoryExactRequest,
    ) -> MemoryExactPage:
        """Authorize before ranking, then reauthorize in the exact source snapshot."""

        admitted, actor = self._admitted_scope(context, request)
        try:
            initial_bindings = self._fresh_bindings(actor)
        except _AuthorizationDenied:
            raise MemoryExactReadDenied("memory-exact read authorization denied") from None
        except Exception:  # noqa: BLE001 - storage failures may retain private material
            raise MemoryExactInternalError("memory-exact authorization storage is unavailable") from None
        try:
            provider_snapshot, dependency_ledger = await self._provider_snapshot(
                request,
                admitted=admitted,
                actor=actor,
                expected_bindings=initial_bindings,
            )
            # Ranking is an awaited, potentially remote stage.  It cannot extend
            # the inherited turn deadline and then use authority admitted before
            # that deadline expired.
            self._issuer.require_context(admitted)
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=request,
                    expected_bindings=initial_bindings,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
                from friday.storage._memory_exact_internal import (
                    select_memory_exact_page_in_transaction,
                )

                page = select_memory_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    provider_snapshot,
                    request=request,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=request,
                    local_change_delta=0,
                )
            dependency_ledger.require_stable(
                _current_storage_connection(self._storage),
                request=request,
                local_change_delta=0,
            )
        except _AuthorizationDenied:
            raise MemoryExactReadDenied("memory-exact read authorization changed") from None
        except TurnContextError:
            raise
        except MemoryExactInternalError:
            raise
        except Exception:  # noqa: BLE001 - storage failures may retain private material
            raise MemoryExactInternalError("memory-exact source storage is unavailable") from None
        # The exact snapshot can itself outlive the parent deadline.  Refuse to
        # hand its carrier to the next stage unless the same context is live at
        # the return edge.
        self._issuer.require_context(admitted)
        return page

    def project_for_model(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
    ) -> MemoryExactProjection:
        """Return the only projection permitted to cross the model boundary."""

        if type(page) is not MemoryExactPage or not page._is_process_owned():
            raise MemoryExactInternalError("memory-exact projection requires its private page")
        admitted, actor = self._admitted_scope(context, page.request)
        try:
            _normalize_provider_connection(_current_storage_connection(self._storage))
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=page.request,
                    expected_bindings=None,
                )
                if not hmac.compare_digest(storage_authority.authority_handle, page.authority_handle):
                    raise _AuthorizationDenied
                projection = project_memory_exact_page(page)
        except _AuthorizationDenied:
            raise MemoryExactReadDenied("memory-exact projection authorization denied") from None
        except MemoryExactInternalError:
            raise
        except Exception:  # noqa: BLE001 - storage failures may retain private material
            raise MemoryExactInternalError("memory-exact projection authority is unavailable") from None
        self._issuer.require_context(admitted)
        return projection

    async def reauthorize_for_publication(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
    ) -> MemoryExactPublicationDecision:
        """Re-run selection and source binding under fresh final authority."""

        from friday.storage._memory_exact_internal import (
            MemoryExactStorageDrift,
            reselect_memory_exact_page_in_transaction,
        )

        if type(page) is not MemoryExactPage or not page._is_process_owned():
            raise MemoryExactInternalError("memory-exact publication requires its private page")
        status = MemoryExactPublicationStatus.UNAVAILABLE
        try:
            admitted, actor = self._admitted_scope(context, page.request)
            initial_bindings = self._fresh_bindings(actor)
            provider_snapshot, dependency_ledger = await self._provider_snapshot(
                page.request,
                admitted=admitted,
                actor=actor,
                expected_bindings=initial_bindings,
            )
            self._issuer.require_context(admitted)
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=page.request,
                    expected_bindings=initial_bindings,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=page.request,
                    local_change_delta=0,
                )
                current = reselect_memory_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    provider_snapshot,
                    page,
                )
                dependency_ledger.require_stable(
                    conn,
                    request=page.request,
                    local_change_delta=0,
                )
            dependency_ledger.require_stable(
                _current_storage_connection(self._storage),
                request=page.request,
                local_change_delta=0,
            )
            # No authorized receipt may outlive the authenticated parent turn.
            # This is deliberately the final operation before computing the
            # decision status; there is no await between this check and sealing.
            self._issuer.require_context(admitted)
        except _AuthorizationDenied:
            status = MemoryExactPublicationStatus.DENIED
        except MemoryExactStorageDrift:
            status = MemoryExactPublicationStatus.DRIFTED
        except (MemoryExactContractError, MemoryExactInternalError, TurnContextError):
            status = MemoryExactPublicationStatus.UNAVAILABLE
        except Exception:  # noqa: BLE001 - private failures deny publication without bodies
            status = MemoryExactPublicationStatus.UNAVAILABLE
        else:
            status = (
                MemoryExactPublicationStatus.AUTHORIZED
                if current.selection_handle == page.selection_handle
                and current.snapshot_handle == page.snapshot_handle
                and current.authority_handle == page.authority_handle
                and current.graph_source_set_sha256 == page.graph_source_set_sha256
                else MemoryExactPublicationStatus.DRIFTED
            )
        return _create_memory_exact_publication_decision(page=page, status=status)

    async def refresh_publication_authority(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
        decision: MemoryExactPublicationDecision,
    ) -> MemoryExactPublicationRefresh:
        """Refresh the provider asynchronously without consuming publication."""

        from friday.storage._memory_exact_internal import (
            MemoryExactStorageDrift,
            reselect_memory_exact_page_in_transaction,
        )

        if (
            type(page) is not MemoryExactPage
            or not page._is_process_owned()
            or type(decision) is not MemoryExactPublicationDecision
            or not decision._is_process_owned()
        ):
            raise MemoryExactInternalError("memory-exact publication refresh is invalid")
        self._require_no_publication_transaction()
        admitted, actor = self._admitted_scope(context, page.request)
        status = MemoryExactPublicationStatus.UNAVAILABLE
        authorization_bindings: tuple[tuple[str, str, str], ...] = ()
        provider_snapshot: object | None = None
        dependency_ledger: _ProviderDependencyLedger | None = None
        current: MemoryExactPage | None = None
        try:
            if not _matches_memory_exact_publication_decision(decision=decision, page=page):
                raise MemoryExactContractError("memory publication decision is not refreshable")
            initial_bindings = self._fresh_bindings(actor)
            refreshed_provider, refreshed_ledger = await self._provider_snapshot(
                page.request,
                admitted=admitted,
                actor=actor,
                expected_bindings=initial_bindings,
            )
            self._require_no_publication_transaction()
            self._issuer.require_context(admitted)
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=page.request,
                    expected_bindings=initial_bindings,
                )
                refreshed_ledger.require_stable(
                    conn,
                    request=page.request,
                    local_change_delta=0,
                )
                current = reselect_memory_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    refreshed_provider,
                    page,
                )
                refreshed_ledger.require_stable(
                    conn,
                    request=page.request,
                    local_change_delta=0,
                )
            refreshed_ledger.require_stable(
                _current_storage_connection(self._storage),
                request=page.request,
                local_change_delta=0,
            )
            self._issuer.require_context(admitted)
        except _AuthorizationDenied:
            status = MemoryExactPublicationStatus.DENIED
        except MemoryExactStorageDrift:
            status = MemoryExactPublicationStatus.DRIFTED
        except (MemoryExactContractError, MemoryExactInternalError, TurnContextError):
            status = MemoryExactPublicationStatus.UNAVAILABLE
        except Exception:  # noqa: BLE001 - provider failures remain body-free
            status = MemoryExactPublicationStatus.UNAVAILABLE
        else:
            assert current is not None
            status = (
                MemoryExactPublicationStatus.AUTHORIZED
                if current.selection_handle == page.selection_handle
                and current.snapshot_handle == page.snapshot_handle
                and current.authority_handle == page.authority_handle
                and current.graph_source_set_sha256 == page.graph_source_set_sha256
                else MemoryExactPublicationStatus.DRIFTED
            )
            if status is MemoryExactPublicationStatus.AUTHORIZED:
                authorization_bindings = initial_bindings
                provider_snapshot = refreshed_provider
                dependency_ledger = refreshed_ledger
        return MemoryExactPublicationRefresh(
            context=admitted,
            page=page,
            decision=decision,
            status=status,
            authorization_bindings=authorization_bindings,
            provider_snapshot=provider_snapshot,
            dependency_ledger=dependency_ledger,
            factory=_PUBLICATION_REFRESH_FACTORY,
        )

    def consume_publication_authority_in_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
        decision: MemoryExactPublicationDecision,
        refresh: MemoryExactPublicationRefresh,
    ) -> bool:
        """Burn and validate on the refresh thread in the publication transaction."""

        from friday.storage._memory_exact_internal import (
            MemoryExactStorageDrift,
            reselect_memory_exact_page_in_transaction,
        )

        if (
            type(page) is not MemoryExactPage
            or not page._is_process_owned()
            or type(decision) is not MemoryExactPublicationDecision
            or not decision._is_process_owned()
        ):
            return False
        try:
            if type(conn) is not sqlite3.Connection:
                return False
            source = _current_storage_connection(self._storage)
            if conn is not source or not conn.in_transaction:
                return False
        except Exception:  # noqa: BLE001 - pre-claim connection checks fail unburned
            return False
        claim_token = _claim_memory_exact_publication_decision(decision=decision, page=page)
        if claim_token is None:
            return False
        live_authorized = False
        try:
            if (
                type(refresh) is not MemoryExactPublicationRefresh
                or refresh.status is not MemoryExactPublicationStatus.AUTHORIZED
                or not refresh._is_process_owned(
                    context=context,
                    page=page,
                    decision=decision,
                )
                or refresh._provider_snapshot is None
                or type(refresh._dependency_ledger) is not _ProviderDependencyLedger
            ):
                raise MemoryExactInternalError("memory-exact publication refresh is unavailable")
            _normalize_provider_connection(conn)
            admitted, actor = self._admitted_scope(context, page.request)
            storage_authority = self._storage_authority(
                conn,
                admitted=admitted,
                actor=actor,
                request=page.request,
                expected_bindings=refresh._authorization_bindings,
            )
            transaction_context = conn.execute(
                """SELECT batch_id,recorded_at,observed_at
                     FROM relation_revision_context WHERE singleton=1"""
            ).fetchone()
            if (
                transaction_context is None
                or type(transaction_context[0]) is not str
                or not transaction_context[0]
                or type(transaction_context[1]) is not str
                or not transaction_context[1]
                or type(transaction_context[2]) is not str
                or transaction_context[2] != transaction_context[1]
            ):
                raise MemoryExactInternalError(
                    "memory-exact publication transaction context is unavailable"
                )
            refresh._dependency_ledger.require_stable(
                conn,
                request=page.request,
                local_change_delta=1,
            )
            refresh._dependency_ledger.replay_read_set(
                conn,
                request=page.request,
            )
            refresh._dependency_ledger.require_stable(
                conn,
                request=page.request,
                local_change_delta=1,
            )
            current = reselect_memory_exact_page_in_transaction(
                conn,
                storage_authority,
                refresh._provider_snapshot,
                page,
            )
            refresh._dependency_ledger.require_stable(
                conn,
                request=page.request,
                local_change_delta=1,
            )
            self._issuer.require_context(admitted)
            live_authorized = (
                conn.in_transaction
                and current.selection_handle == page.selection_handle
                and current.snapshot_handle == page.snapshot_handle
                and current.authority_handle == page.authority_handle
                and current.graph_source_set_sha256 == page.graph_source_set_sha256
            )
        except (
            _AuthorizationDenied,
            MemoryExactContractError,
            MemoryExactInternalError,
            MemoryExactStorageDrift,
            TurnContextError,
        ):
            live_authorized = False
        except Exception:  # noqa: BLE001 - a claimed receipt must fail closed
            live_authorized = False
        return _finish_memory_exact_publication_decision(
            decision=decision,
            page=page,
            claim_token=claim_token,
            live_authorized=live_authorized,
        )


__all__ = [
    "MEMORY_EXACT_ADAPTER_BINDING",
    "MEMORY_EXACT_INTERNAL_ADAPTER_ID",
    "MEMORY_EXACT_INTERNAL_ADAPTER_SCHEMA",
    "MEMORY_EXACT_SECURITY_IDS",
    "MemoryExactAdapterBinding",
    "MemoryExactInternalAdapter",
    "MemoryExactInternalError",
    "MemoryExactPublicationRefresh",
    "MemoryExactReadDenied",
]
