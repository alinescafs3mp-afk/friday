"""Authenticated direct adapter for exact memory and bounded graph recall.

The adapter is deliberately absent from the dialogue tool catalogue.  A trusted
primary caller supplies one authenticated turn, while the existing
``HybridSearcher`` remains the ranking oracle.  Its output is only a candidate
proposal: the storage lane reselects every selected source in one freshly
authorized SQLite snapshot and issues the process-private carrier used for model
projection and late publication revalidation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

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

    def _storage_authority(
        self,
        conn: sqlite3.Connection,
        *,
        admitted: AuthenticatedTurnContext,
        actor: ActorContext,
        request: MemoryExactRequest,
        expected_bindings: tuple[tuple[str, str, str], ...],
    ) -> Any:
        from friday.storage._memory_exact_internal import (
            _issue_memory_exact_storage_authority_in_transaction,
        )

        authorization_bindings = self._authorization_bindings(conn, actor)
        if authorization_bindings != expected_bindings:
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

        graph_expansion = bool(request.as_of or request.known_at or is_relational_query(request.query))
        try:
            raw = await self._searcher.search(
                request.tenant_id,
                request.query,
                limit=request.snapshot_limit,
                include_entities=True,
                kg=self._graph,
                graph_expansion=graph_expansion,
                explain=False,
                since=request.since or None,
                until=request.until or None,
                as_of=request.as_of,
                known_at=request.known_at,
                record_usage=False,
            )
        except (MemoryError, OverflowError):
            raise MemoryExactInternalError("memory-exact provider exceeded its resource bound") from None
        except Exception:  # noqa: BLE001 - provider failures can retain the private query
            raise MemoryExactInternalError("memory-exact provider is unavailable") from None
        if not isinstance(raw, Mapping):
            raise MemoryExactInternalError("memory-exact provider returned no closed snapshot")
        from friday.storage._memory_exact_internal import (
            _create_memory_exact_provider_snapshot,
        )

        try:
            return _create_memory_exact_provider_snapshot(request, raw)
        except Exception:  # noqa: BLE001 - provider failures may retain private material
            # Provider exceptions can retain the private query or source body.
            raise MemoryExactInternalError("memory-exact provider snapshot is invalid") from None

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
        self._admitted_scope(context, page.request)
        return project_memory_exact_page(page)

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

    async def consume_publication_authority(
        self,
        *,
        context: AuthenticatedTurnContext,
        page: MemoryExactPage,
        decision: MemoryExactPublicationDecision,
    ) -> bool:
        """Burn one receipt, then revalidate it at the immediate live edge."""

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
        claim_token = _claim_memory_exact_publication_decision(
            decision=decision,
            page=page,
        )
        if claim_token is None:
            return False
        live_authorized = False
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
                live_authorized = (
                    current.selection_handle == page.selection_handle
                    and current.snapshot_handle == page.snapshot_handle
                    and current.authority_handle == page.authority_handle
                    and current.graph_source_set_sha256 == page.graph_source_set_sha256
                )
            self._issuer.require_context(admitted)
            return _finish_memory_exact_publication_decision(
                decision=decision,
                page=page,
                claim_token=claim_token,
                live_authorized=live_authorized,
            )
        except (
            _AuthorizationDenied,
            MemoryExactContractError,
            MemoryExactInternalError,
            MemoryExactStorageDrift,
            TurnContextError,
        ):
            pass
        except Exception:  # noqa: BLE001 - a claimed receipt must fail closed
            pass
        return _finish_memory_exact_publication_decision(
            decision=decision,
            page=page,
            claim_token=claim_token,
            live_authorized=False,
        )


__all__ = [
    "MEMORY_EXACT_ADAPTER_BINDING",
    "MEMORY_EXACT_INTERNAL_ADAPTER_ID",
    "MEMORY_EXACT_INTERNAL_ADAPTER_SCHEMA",
    "MEMORY_EXACT_SECURITY_IDS",
    "MemoryExactAdapterBinding",
    "MemoryExactInternalAdapter",
    "MemoryExactInternalError",
    "MemoryExactReadDenied",
]
