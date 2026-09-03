"""Dispatch owner for exact window, temporal and graph lanes on archive_search.

Model JSON may express a closed intent projection.  It cannot inject the
private R8D/R8E request objects; the kernel derives those from the live
invocation and authenticated turn.  Exact adapters stay unactivated in the
dialogue catalogue.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from friday.orchestration.turn_context import AuthenticatedTurnContext
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_service import (
    MAX_ARCHIVE_EXACT_CHAIN_PAGES,
    compose_prepared_archive_searches,
)
from friday.retrieval.memory_exact_contract import (
    MEMORY_EXACT_MAX_PAGE_SIZE,
    MemoryExactContractError,
    MemoryExactPage,
    MemoryExactRequest,
)
from friday.retrieval.memory_exact_internal import (
    MemoryExactInternalAdapter,
    MemoryExactInternalError,
    MemoryExactReadDenied,
)
from friday.retrieval.message_exact_contract import (
    MESSAGE_EXACT_DEFAULT_PAGE_SIZE,
    MessageExactContentMode,
    MessageExactContractError,
    MessageExactPage,
    MessageExactRequest,
)
from friday.retrieval.message_exact_internal import (
    MessageExactInternalAdapter,
    MessageExactInternalError,
    MessageExactReadDenied,
)
from friday.storage import FridayStorage

ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA: Final = "friday.archive-search-composite-page.public.v1"
ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA: Final = "friday.archive-search-exact-model-payload.v1"
_EXACT_MODEL_PAYLOAD_KEYS: Final = frozenset({"memory", "message_window", "schema"})


class ArchiveExactDispatchError(ValueError):
    """Exact archive intent could not be bound, authorized, or executed."""


@dataclass(frozen=True, slots=True)
class ArchiveExactIntent:
    """Model-visible exact-lane projection.  Identities stay off this object."""

    as_of: str | None = None
    known_at: str | None = None
    exact_window: bool = False
    include_graph: bool = False
    content_mode: MessageExactContentMode = MessageExactContentMode.EXCERPT

    @property
    def active(self) -> bool:
        return bool(self.exact_window or self.include_graph or self.as_of or self.known_at)

    @property
    def requests_message_window(self) -> bool:
        return self.exact_window is True

    @property
    def requests_memory_exact(self) -> bool:
        return bool(self.include_graph or self.as_of or self.known_at)


@dataclass(frozen=True, slots=True)
class ArchiveExactLaneResult:
    execution_request: ArchiveSearchRequest
    message_pages: tuple[MessageExactPage, ...]
    memory_pages: tuple[MemoryExactPage, ...]
    model_payload: dict[str, object] | None


def _closed_text(value: object, *, label: str) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        raise ArchiveExactDispatchError(f"{label} must be text")
    return " ".join(value.split())


def parse_archive_exact_intent(
    *,
    as_of: str = "",
    known_at: str = "",
    exact_window: bool = False,
    include_graph: bool = False,
    content_mode: str = MessageExactContentMode.EXCERPT.value,
) -> ArchiveExactIntent:
    """Parse model-visible exact intents.  Private exact requests stay forbidden."""

    if type(exact_window) is not bool or type(include_graph) is not bool:
        raise ArchiveExactDispatchError("exact archive flags must be booleans")
    as_of_text = _closed_text(as_of, label="as_of")
    known_at_text = _closed_text(known_at, label="known_at")
    mode_text = _closed_text(content_mode, label="content_mode")
    try:
        mode = MessageExactContentMode(mode_text or MessageExactContentMode.EXCERPT.value)
    except ValueError as exc:
        raise ArchiveExactDispatchError("content_mode is outside the closed contract") from exc
    if mode is MessageExactContentMode.FULL_CONTENT and exact_window is not True:
        raise ArchiveExactDispatchError("full_content requires exact_window")
    return ArchiveExactIntent(
        as_of=as_of_text or None,
        known_at=known_at_text or None,
        exact_window=exact_window,
        include_graph=include_graph,
        content_mode=mode,
    )


def _copy_archive_request(
    request: ArchiveSearchRequest,
    *,
    message_exact_request: MessageExactRequest | None,
    memory_exact_request: MemoryExactRequest | None,
) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=request.query,
        corpora=request.corpora,
        title_hints=request.title_hints,
        filename_hints=request.filename_hints,
        entity_hints=request.entity_hints,
        temporal_constraints=request.temporal_constraints,
        lifecycle_constraints=request.lifecycle_constraints,
        conversation_scope=request.conversation_scope,
        roles=request.roles,
        review_scope=request.review_scope,
        limit=request.limit,
        context=request.context,
        continuation=request.continuation,
        focus=request.focus,
        message_exact_request=message_exact_request,
        memory_exact_request=memory_exact_request,
    )


def derive_archive_exact_requests(
    request: ArchiveSearchRequest,
    intent: ArchiveExactIntent,
    *,
    tenant_id: str,
    principal_id: str,
    active_turn_id: str,
    conversation_id: str | None,
    boundary_user_message_id: str | None,
) -> tuple[MessageExactRequest | None, MemoryExactRequest | None]:
    """Derive private exact requests from a model intent plus live turn scope."""

    if type(request) is not ArchiveSearchRequest or type(intent) is not ArchiveExactIntent:
        raise ArchiveExactDispatchError("exact archive derivation requires typed inputs")
    if not intent.active:
        return None, None
    message_request: MessageExactRequest | None = None
    memory_request: MemoryExactRequest | None = None
    if intent.requests_message_window:
        if ArchiveSearchCorpus.MESSAGES not in request.corpora:
            raise ArchiveExactDispatchError("exact_window requires the messages corpus")
        if not conversation_id or not boundary_user_message_id:
            raise ArchiveExactDispatchError("exact_window requires the current conversation boundary")
        try:
            if request.roles:
                message_request = MessageExactRequest.create(
                    conversation_id=conversation_id,
                    accepted_boundary_user_message_id=boundary_user_message_id,
                    roles=request.roles,
                    page_size=MESSAGE_EXACT_DEFAULT_PAGE_SIZE,
                    content_mode=intent.content_mode,
                )
            else:
                message_request = MessageExactRequest.create(
                    conversation_id=conversation_id,
                    accepted_boundary_user_message_id=boundary_user_message_id,
                    page_size=MESSAGE_EXACT_DEFAULT_PAGE_SIZE,
                    content_mode=intent.content_mode,
                )
        except (MessageExactContractError, TypeError, ValueError) as exc:
            raise ArchiveExactDispatchError("exact message selection is outside the closed contract") from exc
    if intent.requests_memory_exact:
        if ArchiveSearchCorpus.KNOWLEDGE not in request.corpora:
            raise ArchiveExactDispatchError("temporal or graph selection requires the knowledge corpus")
        page_size = min(request.limit, MEMORY_EXACT_MAX_PAGE_SIZE)
        try:
            memory_request = MemoryExactRequest.create(
                tenant_id=tenant_id,
                principal_id=principal_id,
                active_turn_id=active_turn_id,
                query=request.query,
                as_of=intent.as_of,
                known_at=intent.known_at,
                page_size=page_size,
                snapshot_limit=page_size,
            )
        except (MemoryExactContractError, TypeError, ValueError) as exc:
            raise ArchiveExactDispatchError("exact memory selection is outside the closed contract") from exc
    return message_request, memory_request


def collect_message_exact_pages(
    storage: FridayStorage,
    adapter: MessageExactInternalAdapter,
    *,
    context: AuthenticatedTurnContext,
    request: MessageExactRequest,
) -> tuple[MessageExactPage, ...]:
    """Collect one bounded current-conversation chain under the live turn."""

    pages: list[MessageExactPage] = []
    current = request
    try:
        for _index in range(MAX_ARCHIVE_EXACT_CHAIN_PAGES):
            with storage.transaction() as conn:
                page = adapter.prepare_in_transaction(conn, context=context, request=current)
            pages.append(page)
            if page.next_continuation is None:
                return tuple(pages)
            current = replace(request, continuation=page.next_continuation)
    except MessageExactReadDenied:
        raise
    except (MessageExactContractError, MessageExactInternalError) as exc:
        raise ArchiveExactDispatchError("exact message selection is unavailable") from exc
    raise ArchiveExactDispatchError("exact message chain exceeded the closed page budget")


async def collect_memory_exact_pages(
    adapter: MemoryExactInternalAdapter,
    *,
    context: AuthenticatedTurnContext,
    request: MemoryExactRequest,
) -> tuple[MemoryExactPage, ...]:
    """Collect one bounded memory/graph chain before archive materialization."""

    pages: list[MemoryExactPage] = []
    current = request
    try:
        for _index in range(MAX_ARCHIVE_EXACT_CHAIN_PAGES):
            page = await adapter.prepare(context=context, request=current)
            pages.append(page)
            if page.next_continuation is None:
                return tuple(pages)
            current = replace(request, continuation=page.next_continuation)
    except MemoryExactReadDenied:
        raise
    except (MemoryExactContractError, MemoryExactInternalError) as exc:
        raise ArchiveExactDispatchError("exact memory selection is unavailable") from exc
    raise ArchiveExactDispatchError("exact memory chain exceeded the closed page budget")


def project_archive_exact_model_payload(
    *,
    message_adapter: MessageExactInternalAdapter | None,
    memory_adapter: MemoryExactInternalAdapter | None,
    context: AuthenticatedTurnContext,
    message_pages: tuple[MessageExactPage, ...],
    memory_pages: tuple[MemoryExactPage, ...],
) -> dict[str, object] | None:
    """Return synthesis-safe exact projections.  IDs and cursors stay private."""

    if not message_pages and not memory_pages:
        return None
    message_payload: object | None = None
    memory_payload: object | None = None
    if message_pages:
        if message_adapter is None:
            raise ArchiveExactDispatchError("exact message projection adapter is unavailable")
        message_payload = {
            "pages": [message_adapter.project_for_model(page).to_model_payload() for page in message_pages],
            "schema": "friday.archive-search-exact-message-pages.model.v1",
        }
    if memory_pages:
        if memory_adapter is None:
            raise ArchiveExactDispatchError("exact memory projection adapter is unavailable")
        memory_payload = {
            "pages": [
                memory_adapter.project_for_model(context=context, page=page).to_model_payload()
                for page in memory_pages
            ],
            "schema": "friday.archive-search-exact-memory-pages.model.v1",
        }
    return {
        "memory": memory_payload,
        "message_window": message_payload,
        "schema": ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA,
    }


def compose_archive_search_exact_envelope(
    archive_page: Mapping[str, object],
    exact_payload: Mapping[str, object],
) -> str:
    """Wrap the sealed archive page with exact model projections."""

    if type(archive_page) is not dict or type(exact_payload) is not dict:
        raise ArchiveExactDispatchError("exact archive envelope requires closed objects")
    if frozenset(exact_payload) != _EXACT_MODEL_PAYLOAD_KEYS:
        raise ArchiveExactDispatchError("exact archive envelope keys do not match the closed contract")
    if exact_payload["schema"] != ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA:
        raise ArchiveExactDispatchError("exact archive envelope schema is unsupported")
    if exact_payload["memory"] is None and exact_payload["message_window"] is None:
        raise ArchiveExactDispatchError("exact archive envelope requires a projection")
    try:
        return json.dumps(
            {
                "archive": archive_page,
                "memory": exact_payload["memory"],
                "message_window": exact_payload["message_window"],
                "schema": ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ArchiveExactDispatchError("exact archive envelope is not canonical JSON") from exc


async def prepare_archive_exact_lanes(
    *,
    request: ArchiveSearchRequest,
    intent: ArchiveExactIntent,
    storage: FridayStorage,
    turn_context: AuthenticatedTurnContext | None,
    tenant_id: str,
    principal_id: str,
    conversation_id: str | None,
    boundary_user_message_id: str | None,
    message_adapter: MessageExactInternalAdapter | None,
    memory_adapter: MemoryExactInternalAdapter | None,
) -> ArchiveExactLaneResult:
    """Run exact lanes for one archive request.  Inactive intents are a no-op."""

    if not intent.active:
        return ArchiveExactLaneResult(request, (), (), None)
    if type(turn_context) is not AuthenticatedTurnContext:
        raise ArchiveExactDispatchError("exact archive selection requires an authenticated turn")
    if intent.requests_message_window and message_adapter is None:
        raise ArchiveExactDispatchError("exact message adapter is unavailable")
    if intent.requests_memory_exact and memory_adapter is None:
        raise ArchiveExactDispatchError("exact memory adapter is unavailable")
    message_request, memory_request = derive_archive_exact_requests(
        request,
        intent,
        tenant_id=tenant_id,
        principal_id=principal_id,
        active_turn_id=turn_context.turn_id,
        conversation_id=conversation_id,
        boundary_user_message_id=boundary_user_message_id,
    )
    message_pages: tuple[MessageExactPage, ...] = ()
    memory_pages: tuple[MemoryExactPage, ...] = ()
    if message_request is not None:
        assert message_adapter is not None
        message_pages = collect_message_exact_pages(
            storage,
            message_adapter,
            context=turn_context,
            request=message_request,
        )
    if memory_request is not None:
        assert memory_adapter is not None
        memory_pages = await collect_memory_exact_pages(
            memory_adapter,
            context=turn_context,
            request=memory_request,
        )
    execution_request = _copy_archive_request(
        request,
        message_exact_request=message_request,
        memory_exact_request=memory_request,
    )
    model_payload = project_archive_exact_model_payload(
        message_adapter=message_adapter,
        memory_adapter=memory_adapter,
        context=turn_context,
        message_pages=message_pages,
        memory_pages=memory_pages,
    )
    return ArchiveExactLaneResult(
        execution_request,
        message_pages,
        memory_pages,
        model_payload,
    )


def seal_archive_exact_composite(
    prepared_search: Any,
    lanes: ArchiveExactLaneResult,
) -> Any | None:
    """Seal the passive composite only when an exact lane actually ran."""

    if lanes.model_payload is None:
        return None
    return compose_prepared_archive_searches(
        prepared_search,
        message_exact_pages=lanes.message_pages,
        memory_exact_pages=lanes.memory_pages,
    )


__all__ = [
    "ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA",
    "ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA",
    "ArchiveExactDispatchError",
    "ArchiveExactIntent",
    "ArchiveExactLaneResult",
    "compose_archive_search_exact_envelope",
    "derive_archive_exact_requests",
    "parse_archive_exact_intent",
    "prepare_archive_exact_lanes",
    "project_archive_exact_model_payload",
    "seal_archive_exact_composite",
]
