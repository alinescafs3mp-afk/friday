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
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, SupportsIndex

from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.supervisor_contracts import ARCHIVE_SEARCH_ID
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    TurnContextError,
    TurnContextIssuer,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, is_relational_query
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


class _ProviderReadEnvelope:
    """One request-local byte budget and exact source-revision ledger."""

    __slots__ = ("_lock", "_poisoned", "_revisions", "_used_bytes")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._poisoned: str | None = None
        self._revisions: dict[str, tuple[str, str]] = {}
        self._used_bytes = 0

    def poison(self, *, resource: bool = False) -> None:
        with self._lock:
            if resource or self._poisoned is None:
                self._poisoned = "resource" if resource else "invalid"

    def require_clean(self) -> None:
        with self._lock:
            if self._poisoned == "resource":
                raise _ProviderResourceExceeded
            if self._poisoned is not None:
                raise _ProviderSnapshotInvalid

    def reserve(self, size: int) -> None:
        from friday.storage._memory_exact_internal import MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES

        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            self.poison()
            raise _ProviderSnapshotInvalid
        with self._lock:
            total = self._used_bytes + size
            if total > MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES:
                self._poisoned = "resource"
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


class _BoundedProviderStorage:
    """FridayStorage facade that preflights every content-bearing ranker read."""

    __slots__ = ("_envelope", "_request", "_storage")

    def __init__(
        self,
        storage: FridayStorage,
        envelope: _ProviderReadEnvelope,
        request: MemoryExactRequest,
    ) -> None:
        self._storage = storage
        self._envelope = envelope
        self._request = request

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

    def _load(
        self, conn: sqlite3.Connection, tenant_id: str, identities: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        from friday.storage._memory_exact_internal import (
            _load_memory_exact_provider_rows_in_transaction,
        )

        try:
            rows, revisions = _load_memory_exact_provider_rows_in_transaction(
                conn,
                tenant_id=tenant_id,
                knowledge_ids=identities,
                reserve_bytes=self._envelope.reserve,
            )
            self._envelope.remember(revisions)
            return list(rows)
        except BaseException:
            self._envelope.poison()
            raise

    def ensure_result_sources(self, tenant_id: str, identities: tuple[str, ...]) -> None:
        tenant = self._require_tenant(tenant_id)
        known = self._envelope.revisions()
        missing = tuple(identity for identity in identities if identity not in known)
        if not missing:
            return
        with read_only_storage_snapshot(self._storage) as conn:
            self._load(conn, tenant, missing)

    def search_knowledge(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        from friday.storage._memory_exact_internal import (
            _memory_exact_provider_search_ids_in_transaction,
        )

        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        with read_only_storage_snapshot(self._storage) as conn:
            identities = _memory_exact_provider_search_ids_in_transaction(
                conn,
                tenant_id=tenant,
                query=query,
                limit=limit,
                uploaded_by=uploaded_by,
                fts_available=bool(self._storage._fts_available),  # noqa: SLF001
                reserve_bytes=self._envelope.reserve,
            )
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
        with read_only_storage_snapshot(self._storage) as conn:
            if since is not None or until is not None:
                from friday.storage._memory_exact_internal import (
                    _require_provider_date_metadata_in_transaction,
                )

                _require_provider_date_metadata_in_transaction(
                    conn,
                    tenant_id=tenant,
                    uploaded_by=uploaded_by,
                )
            where, parameters = self._storage._knowledge_filter(  # noqa: SLF001
                tenant,
                lifecycle_stage=lifecycle_stage,
                tag=tag,
                entity_id=entity_id,
                query=query,
                since=since,
                until=until,
                uploaded_by=uploaded_by,
            )
            bounded_limit = limit
            selected = f"""WITH selected AS MATERIALIZED (
                SELECT rowid AS knowledge_rowid FROM knowledge_objects WHERE {where}
                 ORDER BY importance DESC, updated_at DESC, id DESC LIMIT ? OFFSET ?
            )"""  # nosec B608 - released predicate and bound parameters only
            selected_parameters = (*parameters, bounded_limit, 0)
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
            if preflight is None or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in tuple(preflight)
            ):
                self._refuse()
            row_count, storage_bytes, invalid_rows = tuple(preflight)
            if row_count > bounded_limit or invalid_rows:
                self._refuse()
            self._envelope.reserve(storage_bytes)
            rows = conn.execute(
                selected
                + """ SELECT k.id FROM selected
                        JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid
                       ORDER BY k.importance DESC, k.updated_at DESC, k.id DESC""",
                selected_parameters,
            ).fetchall()
            identities = tuple(row[0] for row in rows if type(row[0]) is str)
            if len(rows) != row_count or len(identities) != row_count:
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
            return None
        from friday.storage._memory_exact_internal import (
            _require_provider_date_metadata_in_transaction,
        )

        with read_only_storage_snapshot(self._storage) as conn:
            _require_provider_date_metadata_in_transaction(
                conn,
                tenant_id=tenant,
                uploaded_by=uploaded_by,
            )
            where, parameters = self._storage._knowledge_filter(  # noqa: SLF001
                tenant,
                lifecycle_stage=None,
                tag=None,
                entity_id=None,
                since=since,
                until=until,
                uploaded_by=uploaded_by,
            )
            # Match FridayStorage's wide-window semantics, but materialize only
            # integer rowids until both the count and aggregate identity bytes are
            # known.  A hostile TEXT primary key therefore never reaches Python
            # before the request-local envelope accepts it.
            window_cap = 20_000
            selected = f"""WITH selected AS MATERIALIZED (
                SELECT rowid AS knowledge_rowid FROM knowledge_objects
                 WHERE {where} LIMIT ?
            )"""  # nosec B608 - released predicate and bound parameters only
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
                (*parameters, window_cap + 1),
            ).fetchone()
            if preflight is None or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in tuple(preflight)
            ):
                self._refuse()
            row_count, storage_bytes, invalid_rows = tuple(preflight)
            if row_count > window_cap:
                return None
            if invalid_rows:
                self._refuse()
            self._envelope.reserve(storage_bytes)
            rows = conn.execute(
                selected
                + """ SELECT k.id FROM selected
                        JOIN knowledge_objects k ON k.rowid=selected.knowledge_rowid""",
                (*parameters, window_cap + 1),
            ).fetchall()
            identities = {
                str(row[0])
                for row in rows
                if type(row[0]) is str and row[0] and len(row[0].encode("utf-8")) <= 240
            }
            if len(rows) != row_count or len(identities) != row_count:
                self._refuse()
            return identities

    def known_vocabulary(self, terms: Sequence[str]) -> set[str]:
        """Disable the released corpus-wide vocabulary side channel."""

        if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)):
            self._refuse()
        return set()

    def vocabulary_terms(self, prefixes: Sequence[str], *, limit: int = 400) -> list[str]:
        """No cross-tenant FTS vocabulary may influence an authenticated query."""

        if (
            not isinstance(prefixes, Sequence)
            or isinstance(prefixes, (str, bytes))
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            self._refuse()
        return []

    def count_knowledge_objects(
        self,
        user_id: str,
        *,
        uploaded_by: str | None = None,
    ) -> int:
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        return self._storage.count_knowledge_objects(tenant, uploaded_by=None)

    def relation_history_status(self, user_id: str, known_at: str = "") -> dict[str, Any]:
        tenant = self._require_tenant(user_id)
        if type(known_at) is not str or known_at != (self._request.known_at or ""):
            self._refuse()
        if not known_at:
            return self._storage.relation_history_status(tenant, known_at="")
        from friday.storage._memory_exact_internal import (
            _memory_exact_provider_relation_history_status_in_transaction,
        )

        # The released method normally persists a logical-clock observation for
        # a new historical boundary.  This adapter is effect_class=read, so the
        # storage helper runs the same validator through a SELECT-only view and
        # requires that the promise was already durable.
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                return _memory_exact_provider_relation_history_status_in_transaction(
                    conn,
                    tenant_id=tenant,
                    known_at=known_at,
                )
        except BaseException:
            self._envelope.poison()
            raise

    def get_knowledge_usage(
        self,
        user_id: str,
        knowledge_object_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        from friday.retrieval import _USAGE_WEIGHT

        self._require_tenant(user_id)
        self._bounded_id_sequence(knowledge_object_ids, maximum=400)
        # The released usage coefficient is exactly zero.  Its rows cannot alter
        # ordering, selected sources, or the exact projection, so the closed lane
        # omits this diagnostic-only material instead of opening another loader.
        if _USAGE_WEIGHT != 0.0:
            self._refuse()
        return {}

    def get_chunk_spans(
        self,
        user_id: str,
        model: str,
        keys: Sequence[tuple[str, int]],
        *,
        uploaded_by: str | None = None,
    ) -> dict[tuple[str, int], tuple[int, int]]:
        self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)) or len(keys) > 400:
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
        # Passage spans only decorate the provider's discarded diagnostic row.
        # Exact storage builds the model excerpt from its own freshly selected
        # source, so omitting them cannot change source order or publication.
        return {}

    def _bounded_id_sequence(
        self,
        values: Sequence[str],
        *,
        maximum: int,
    ) -> tuple[str, ...]:
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
            or len(values) > maximum
            or any(type(item) is not str or not item or len(item.encode("utf-8")) > 240 for item in values)
        ):
            self._refuse()
        return tuple(values)

    def entity_links_by_document(
        self,
        user_id: str,
        document_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Bound the exact entity-label signal used by the released ranker."""

        from friday.storage._privacy import _not_private_entity_material_dependency

        tenant = self._require_tenant(user_id)
        identities = self._bounded_id_sequence(document_ids, maximum=400)
        if not identities:
            return {}
        holders = ",".join("?" for _item in identities)
        selected = f"""SELECT l.knowledge_object_id,
                               substr(l.entity_id,1,160) AS entity_id,
                               l.confidence,
                               substr(e.name,1,240) AS name,
                               substr(e.entity_type,1,80) AS entity_type
                          FROM knowledge_entity_links l
                          JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                         WHERE l.user_id=? AND l.status='accepted' AND e.deleted_at IS NULL
                           AND {_not_private_entity_material_dependency("e")}
                           AND l.knowledge_object_id IN ({holders})
                         ORDER BY l.confidence DESC,e.name COLLATE NOCASE"""  # nosec B608
        parameters: tuple[object, ...] = (tenant, *identities)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
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
                           AND {_not_private_entity_material_dependency("e")}
                           AND l.knowledge_object_id IN ({holders})""",  # nosec B608
                    parameters,
                ).fetchone()
                if (
                    preflight is None
                    or isinstance(preflight[0], bool)
                    or not isinstance(preflight[0], int)
                    or preflight[0] < 0
                    or isinstance(preflight[1], bool)
                    or not isinstance(preflight[1], int)
                    or preflight[1] < 0
                ):
                    self._refuse()
                self._envelope.reserve(preflight[1])
                rows = conn.execute(selected, parameters).fetchall()
                if len(rows) != preflight[0]:
                    self._refuse()
            output: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                confidence = float(row["confidence"] or 0.0)
                if not math.isfinite(confidence):
                    self._refuse()
                output.setdefault(str(row["knowledge_object_id"]), []).append(
                    {
                        "id": row["entity_id"],
                        "name": row["name"],
                        "type": row["entity_type"],
                        "confidence": confidence,
                    }
                )
            return output
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
        if not identities:
            return {}
        holders = ",".join("?" for _item in identities)
        selected = f"""SELECT target_id,AVG(score) AS score FROM feedback_state
                         WHERE user_id=? AND target_id IN ({holders})
                           AND feedback_type IN ('search_quality','answer_usefulness')
                         GROUP BY target_id"""  # nosec B608
        parameters: tuple[object, ...] = (tenant, *identities)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
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
                if (
                    preflight is None
                    or isinstance(preflight[0], bool)
                    or not isinstance(preflight[0], int)
                    or preflight[0] < 0
                    or isinstance(preflight[1], bool)
                    or not isinstance(preflight[1], int)
                    or preflight[1] < 0
                ):
                    self._refuse()
                self._envelope.reserve(preflight[1])
                rows = conn.execute(selected, parameters).fetchall()
            result: dict[str, float] = {}
            for row in rows:
                score = float(row["score"] or 0.0)
                if not math.isfinite(score):
                    self._refuse()
                result[str(row["target_id"])] = score
            return result
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
        from friday.storage._memory_exact_internal import (
            _reserve_memory_exact_provider_embeddings_in_transaction,
        )

        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                _reserve_memory_exact_provider_embeddings_in_transaction(
                    conn,
                    tenant_id=tenant,
                    model=model,
                    dim=dim,
                    limit=limit,
                    uploaded_by=uploaded_by,
                    reserve_bytes=self._envelope.reserve,
                )
                return self._storage.get_user_embeddings(
                    tenant,
                    model,
                    dim,
                    limit=limit,
                    uploaded_by=uploaded_by,
                )
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
        from friday.storage._memory_exact_internal import (
            _reserve_memory_exact_provider_chunk_embeddings_in_transaction,
        )

        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                _reserve_memory_exact_provider_chunk_embeddings_in_transaction(
                    conn,
                    tenant_id=tenant,
                    model=model,
                    dim=dim,
                    object_limit=object_limit,
                    row_limit=row_limit,
                    uploaded_by=uploaded_by,
                    reserve_bytes=self._envelope.reserve,
                )
                return self._storage.get_user_chunk_embeddings(
                    tenant,
                    model,
                    dim,
                    object_limit=object_limit,
                    row_limit=row_limit,
                    uploaded_by=uploaded_by,
                )
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
        from friday.storage._memory_exact_internal import (
            _memory_exact_provider_live_id_in_transaction,
        )

        if user_id is None:
            raise MemoryExactInternalError("memory-exact provider requires an exact tenant")
        tenant = self._require_tenant(user_id)
        self._require_unscoped_author(uploaded_by)
        with read_only_storage_snapshot(self._storage) as conn:
            identities = _memory_exact_provider_live_id_in_transaction(
                conn,
                tenant_id=tenant,
                knowledge_id=ko_id,
                uploaded_by=uploaded_by,
            )
            rows = self._load(conn, tenant, identities)
            return rows[0] if rows else None


class _ProviderDenseCache:
    """Force vector reads through the request-local bounded storage facade."""

    __slots__ = ()

    def get(self, *_args: object, **_kwargs: object) -> None:
        return None


class _BoundedProviderGraph:
    """Closed graph surface; the released graph implementation keeps its own storage."""

    __slots__ = ("_envelope", "_graph", "_request")

    def __init__(
        self,
        graph: KnowledgeGraph,
        envelope: _ProviderReadEnvelope,
        request: MemoryExactRequest,
    ) -> None:
        self._graph = graph
        self._envelope = envelope
        self._request = request

    def _tenant(self, user_id: object) -> str:
        if type(user_id) is not str or user_id != self._request.tenant_id:
            self._envelope.poison()
            raise _ProviderSnapshotInvalid
        return user_id

    def _bounded_result(self, value: object, *, expected: type[dict] | type[list]) -> Any:
        if type(value) is not expected:
            self._envelope.poison()
            raise _ProviderSnapshotInvalid
        self._envelope.reserve(len(_canonical_json(value).encode("ascii")))
        return value

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
            if as_of != (self._request.as_of or "") or known_at != (self._request.known_at or ""):
                raise _ProviderSnapshotInvalid
            if known_at:
                from friday.storage._memory_exact_internal import (
                    _memory_exact_provider_relation_history_status_in_transaction,
                )

                with read_only_storage_snapshot(self._graph.storage) as conn:
                    _memory_exact_provider_relation_history_status_in_transaction(
                        conn,
                        tenant_id=tenant,
                        known_at=known_at,
                    )
            value = self._graph.context_for_query(
                tenant,
                query,
                depth=depth,
                entity_limit=entity_limit,
                knowledge_limit=knowledge_limit,
                seed_knowledge_ids=seed_knowledge_ids,
                as_of=as_of,
                known_at=known_at,
            )
            return self._bounded_result(value, expected=dict)
        except BaseException:
            self._envelope.poison()
            raise

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
            value = self._graph.search_entities(
                tenant,
                query,
                limit=limit,
                entity_type=entity_type,
            )
            return self._bounded_result(value, expected=list)
        except BaseException:
            self._envelope.poison()
            raise


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
    """Process-private result of the final asynchronous provider refresh."""

    __slots__ = (
        "_authorization_bindings",
        "_context_authority_sha256",
        "_decision",
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
            or authorized != bool(authorization_bindings)
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
            ):
                return False
            material = {
                "authorization_bindings_sha256": _sha256(self._authorization_bindings),
                "context_authority_sha256": self._context_authority_sha256,
                "decision_object": id(self._decision),
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

    __slots__ = ("_authorization", "_graph", "_issuer", "_searcher", "_storage")

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
        with read_only_storage_snapshot(self._storage) as conn:
            return self._authorization_bindings(conn, actor)

    def _require_no_publication_transaction(self) -> None:
        if self._storage.conn.in_transaction:
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

    async def _provider_snapshot(self, request: MemoryExactRequest) -> Any:
        """Run the released ranker without writes, then close its untrusted shape."""

        self._require_no_publication_transaction()
        graph_expansion = bool(request.as_of or request.known_at or is_relational_query(request.query))
        envelope = _ProviderReadEnvelope()
        bounded_storage = _BoundedProviderStorage(self._storage, envelope, request)
        provider_searcher = copy.copy(self._searcher)
        provider_searcher.storage = bounded_storage
        from friday.storage._memory_exact_internal import (
            _MEMORY_EXACT_MAX_PROVIDER_MATERIAL_ROWS,
        )

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
        provider_graph = _BoundedProviderGraph(self._graph, envelope, request)
        try:
            raw = await provider_searcher.search(
                request.tenant_id,
                request.query,
                limit=request.snapshot_limit,
                include_entities=True,
                kg=provider_graph,
                graph_expansion=graph_expansion,
                explain=False,
                since=request.since or None,
                until=request.until or None,
                as_of=request.as_of,
                known_at=request.known_at,
                record_usage=False,
            )
            self._require_no_publication_transaction()
            envelope.require_clean()
            if not isinstance(raw, Mapping):
                raise _ProviderSnapshotInvalid
            raw_results = raw.get("results")
            if (
                not isinstance(raw_results, (list, tuple))
                or len(raw_results) > request.snapshot_limit
                or any(not isinstance(item, Mapping) for item in raw_results)
            ):
                raise _ProviderSnapshotInvalid
            result_ids: list[str] = []
            for item in raw_results:
                identity = item.get("id")
                if type(identity) is not str or not identity or len(identity.encode("utf-8")) > 240:
                    raise _ProviderSnapshotInvalid
                result_ids.append(identity)
            bounded_storage.ensure_result_sources(request.tenant_id, tuple(result_ids))
            from friday.storage._memory_exact_internal import (
                _create_memory_exact_provider_snapshot,
            )

            try:
                return _create_memory_exact_provider_snapshot(request, raw, envelope.revisions())
            except Exception:  # noqa: BLE001 - provider values may retain private material
                raise _ProviderSnapshotInvalid from None
        except _ProviderResourceExceeded:
            raise MemoryExactInternalError(
                "memory-exact provider exceeded its aggregate byte bound"
            ) from None
        except (MemoryError, OverflowError, RecursionError):
            raise MemoryExactInternalError("memory-exact provider exceeded its resource bound") from None
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
        except (MemoryError, OverflowError, RecursionError, sqlite3.Error):
            raise MemoryExactInternalError("memory-exact authorization storage is unavailable") from None
        provider_snapshot = await self._provider_snapshot(request)
        # Ranking is an awaited, potentially remote stage.  It cannot extend
        # the inherited turn deadline and then use authority admitted before
        # that deadline expired.
        self._issuer.require_context(admitted)
        try:
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=request,
                    expected_bindings=initial_bindings,
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
        except _AuthorizationDenied:
            raise MemoryExactReadDenied("memory-exact read authorization changed") from None
        except (MemoryError, OverflowError, RecursionError, sqlite3.Error):
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
        except (MemoryError, OverflowError, RecursionError, sqlite3.Error):
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
            provider_snapshot = await self._provider_snapshot(page.request)
            self._issuer.require_context(admitted)
            with read_only_storage_snapshot(self._storage) as conn:
                storage_authority = self._storage_authority(
                    conn,
                    admitted=admitted,
                    actor=actor,
                    request=page.request,
                    expected_bindings=initial_bindings,
                )
                current = reselect_memory_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    provider_snapshot,
                    page,
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
        current: MemoryExactPage | None = None
        try:
            if not _matches_memory_exact_publication_decision(decision=decision, page=page):
                raise MemoryExactContractError("memory publication decision is not refreshable")
            initial_bindings = self._fresh_bindings(actor)
            refreshed_provider = await self._provider_snapshot(page.request)
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
                current = reselect_memory_exact_page_in_transaction(
                    conn,
                    storage_authority,
                    refreshed_provider,
                    page,
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
        return MemoryExactPublicationRefresh(
            context=admitted,
            page=page,
            decision=decision,
            status=status,
            authorization_bindings=authorization_bindings,
            provider_snapshot=provider_snapshot,
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
        """Burn and validate one receipt inside the caller's publication transaction."""

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
        if type(conn) is not sqlite3.Connection or conn is not self._storage.conn or not conn.in_transaction:
            return False
        try:
            transaction_context = conn.execute(
                "SELECT batch_id,recorded_at FROM relation_revision_context WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error:
            return False
        if (
            transaction_context is None
            or type(transaction_context[0]) is not str
            or not transaction_context[0]
            or type(transaction_context[1]) is not str
            or not transaction_context[1]
        ):
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
            ):
                raise MemoryExactInternalError("memory-exact publication refresh is unavailable")
            admitted, actor = self._admitted_scope(context, page.request)
            storage_authority = self._storage_authority(
                conn,
                admitted=admitted,
                actor=actor,
                request=page.request,
                expected_bindings=refresh._authorization_bindings,
            )
            current = reselect_memory_exact_page_in_transaction(
                conn,
                storage_authority,
                refresh._provider_snapshot,
                page,
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
