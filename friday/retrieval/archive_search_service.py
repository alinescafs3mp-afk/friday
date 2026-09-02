"""One-snapshot application facade for read-only private archive search.

The facade deliberately owns neither a database connection nor a transaction.
It closes the requested lane plan inside the caller's live SQLite transaction,
federates only authorized storage projections, and crosses the archive authority
gate before returning any model-visible carrier.  Unsupported and failed lanes
remain explicit coverage entries; private queries are never routed outbound.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import NoReturn, SupportsIndex, cast

from friday.permissions import ActorContext, AuthorizationDecision, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_CANDIDATES,
    ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL,
    ArchiveModelBatchLedger,
    ArchiveSearchAuthorityPhase,
    ArchiveSearchCandidateReauthorization,
    ArchiveSearchCoverageReauthorization,
    ArchiveSearchReauthorizationStatus,
    ArchiveSearchRunBinding,
    AuthorizedArchiveBatch,
    RedeemedArchiveContinuation,
    canonical_archive_search_targets,
    create_archive_search_run_binding,
    issue_archive_search_continuation,
    redeem_archive_search_continuation,
)
from friday.retrieval.archive_search_authority import (
    authorize_archive_search_before_model as _authorize_before_model,
)
from friday.retrieval.archive_search_authority import (
    authorize_archive_search_resumed_before_model as _authorize_resumed_before_model,
)
from friday.retrieval.archive_search_contract import (
    MAX_ARCHIVE_MATERIALIZED_CANDIDATES,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPage,
    ArchiveSearchRequest,
    ArchiveSearchResult,
    ArchiveSearchWarning,
)
from friday.retrieval.archive_search_dense import (
    ArchiveDenseQueryPlan,
    project_archive_dense_query_plan,
)
from friday.retrieval.archive_search_federation import (
    FederatedArchiveSearch,
    federate_archive_search,
)
from friday.retrieval.archive_search_message_adapter import (
    archive_message_storage_controls,
    project_archive_message_page,
)
from friday.retrieval.archive_search_obsidian_adapter import (
    project_archive_obsidian_lane_page_in_transaction,
)
from friday.retrieval.catalog_contract import (
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
    IndexIncompleteReason,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    MessageRole,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
)
from friday.retrieval.memory_exact_contract import MemoryExactPage, MemoryExactRequest
from friday.retrieval.message_exact_contract import MessageExactPage, MessageExactRequest
from friday.storage._archive_search_documents import (
    _materialize_archive_document_lane as search_archive_document_lane,
)
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    ArchiveMessageStorageError,
    _accepted_archive_message_boundary_identity_in_transaction,
)
from friday.storage._archive_search_messages import (
    _materialize_authorized_archive_message_page_in_transaction as select_authorized_archive_message_page_in_transaction,
)
from friday.storage._archive_search_obsidian import (
    ArchiveObsidianExactFileReader,
    ArchiveObsidianReadPhase,
    ArchiveObsidianUnavailableReason,
    select_archive_obsidian_lane_in_transaction,
)
from friday.storage._conversation_passages import (
    select_authorized_conversation_passage_projection_in_transaction,
)

_PROCESS_KEY = secrets.token_bytes(32)
_PROCESS_AUTHORITY = object()
MAX_ARCHIVE_EXACT_CHAIN_PAGES = 32
_INTERNAL_LANE_LIMIT = ARCHIVE_AUTHORITY_MAX_CANDIDATES
_DOCUMENT_CORPUS = {
    SearchCorpus.RAW_DOCUMENTS: ArchiveSearchCorpus.DOCUMENTS,
    SearchCorpus.KNOWLEDGE: ArchiveSearchCorpus.KNOWLEDGE,
}
_DOCUMENT_LANES = frozenset({SearchLane.CATALOG, SearchLane.LEXICAL, SearchLane.DENSE})
_OBSIDIAN_LANES = frozenset(
    {
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
    }
)
_CORPUS_CAPABILITY: dict[SearchCorpus, str | None] = {
    SearchCorpus.RAW_DOCUMENTS: "knowledge.read",
    SearchCorpus.KNOWLEDGE: "knowledge.read",
    SearchCorpus.CONVERSATION: "conversations.read",
    SearchCorpus.OBSIDIAN: "obsidian.read",
    SearchCorpus.GENERATED_ARTIFACTS: "knowledge.read",
    SearchCorpus.WEB_CAPTURES: "knowledge.read",
    # Registered external sources deliberately have no execution lane here.
    # In particular, an archive request can never become outbound authority.
    SearchCorpus.EXTERNAL: None,
}


class ArchiveSearchServiceError(RuntimeError):
    """Body-free failure at the application facade boundary."""


class _ArchiveAcceptedBoundaryDrift(ArchiveSearchServiceError):
    """The accepted message boundary changed during one sealed search run."""


def _fail() -> ArchiveSearchServiceError:
    return ArchiveSearchServiceError("archive search service is unavailable")


def _materialized_lane_limit(
    request: ArchiveSearchRequest,
    *,
    usable_dense_targets: frozenset[tuple[SearchCorpus, SearchLane]] = frozenset(),
) -> int:
    """Fair-share the bounded tail across lanes that can actually extend it."""

    targets = canonical_archive_search_targets(request)
    if (
        type(usable_dense_targets) is not frozenset
        or not usable_dense_targets.issubset(targets)
        or any(
            corpus not in _DOCUMENT_CORPUS or lane is not SearchLane.DENSE
            for corpus, lane in usable_dense_targets
        )
    ):
        raise _fail()
    extended_target_count = sum(
        (
            corpus in _DOCUMENT_CORPUS
            and lane in _DOCUMENT_LANES
            and (lane is not SearchLane.DENSE or (corpus, lane) in usable_dense_targets)
            or corpus is SearchCorpus.CONVERSATION
            and lane in {SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}
        )
        for corpus, lane in targets
    )
    fixed_target_count = sum(
        corpus is SearchCorpus.OBSIDIAN and lane in _OBSIDIAN_LANES for corpus, lane in targets
    )
    if extended_target_count < 1:
        raise _fail()
    # The public byte envelope may shrink a requested head, but a successful
    # non-empty federation always fits at least one candidate.  Reserving only
    # that guaranteed head keeps every remaining candidate within the frozen
    # continuation bound even when fewer than ``request.limit`` fit publicly.
    available_budget = (
        1 + ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL - fixed_target_count * ARCHIVE_AUTHORITY_MAX_CANDIDATES
    )
    if available_budget < request.limit * extended_target_count:
        raise _fail()
    return min(
        MAX_ARCHIVE_MATERIALIZED_CANDIDATES,
        available_budget // extended_target_count,
    )


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _fail() from None


def _mac(domain: bytes, value: object) -> bytes:
    return hmac.new(
        _PROCESS_KEY,
        domain + b"\0" + _canonical_bytes(value),
        hashlib.sha256,
    ).digest()


def _same_exact_graph(left: object, right: object) -> bool:
    """Compare canonical values without accepting same-value foreign subclasses."""

    if type(left) is not type(right):
        return False
    if type(left) is tuple:
        left_items = cast(tuple[object, ...], left)
        right_items = cast(tuple[object, ...], right)
        return len(left_items) == len(right_items) and all(
            _same_exact_graph(left_item, right_item)
            for left_item, right_item in zip(left_items, right_items, strict=True)
        )
    if is_dataclass(left) and not isinstance(left, type):
        return all(
            _same_exact_graph(getattr(left, field.name), getattr(right, field.name)) for field in fields(left)
        )
    try:
        return bool(left == right)
    except Exception:
        return False


def _canonical_candidate(value: object) -> ArchiveSearchCandidate | None:
    if type(value) is not ArchiveSearchCandidate:
        return None
    try:
        item = cast(ArchiveSearchCandidate, value)
        encoded = item.to_private_json()
        frozen = ArchiveSearchCandidate.parse_private(encoded)
        if frozen.to_private_json() != encoded or not _same_exact_graph(item, frozen):
            return None
        return frozen
    except Exception:
        return None


def _binding_has_exact_graph(value: object) -> bool:
    if type(value) is not SearchExecutionBinding:
        return False
    binding = cast(SearchExecutionBinding, value)
    try:
        return bool(
            type(binding.authority_scope) is AuthorityScope
            and type(binding.requested_targets) is tuple
            and all(
                type(target) is tuple
                and len(target) == 2
                and type(target[0]) is SearchCorpus
                and type(target[1]) is SearchLane
                for target in binding.requested_targets
            )
            and type(binding.opaque_handle) is str
            and binding.is_live_private_request_binding
        )
    except Exception:
        return False


def _canonical_coverage(value: object) -> SearchCoverage | None:
    if type(value) is not SearchCoverage:
        return None
    item = cast(SearchCoverage, value)
    try:
        if (
            type(item.corpus) is not SearchCorpus
            or type(item.lane) is not SearchLane
            or not _binding_has_exact_graph(item.execution_binding)
            or type(item.states) is not tuple
            or any(type(state) is not CoverageState for state in item.states)
            or (item.eligible_authorized is not None and type(item.eligible_authorized) is not int)
            or any(type(count) is not int for count in (item.examined, item.matched_at_least, item.returned))
            or (item.limit is not None and type(item.limit) is not int)
            or any(
                type(flag) is not bool
                for flag in (
                    item.next_cursor_available,
                    item.authority_rechecked,
                    item.snapshot_current,
                )
            )
        ):
            return None
        frozen = SearchCoverage.create(
            corpus=item.corpus,
            lane=item.lane,
            execution_binding=item.execution_binding,
            states=item.states,
            eligible_authorized=item.eligible_authorized,
            examined=item.examined,
            matched_at_least=item.matched_at_least,
            returned=item.returned,
            authority_rechecked=item.authority_rechecked,
            snapshot_current=item.snapshot_current,
            limit=item.limit,
            next_cursor_available=item.next_cursor_available,
        )
        if not _same_exact_graph(item, frozen):
            return None
        return frozen
    except Exception:
        return None


def _batch_contract_is_canonical(value: object) -> bool:
    if type(value) is not AuthorizedArchiveBatch:
        return False
    try:
        page = cast(AuthorizedArchiveBatch, value)._page
        frozen_request = ArchiveSearchRequest.parse_private(page.request.to_private_json())
        return bool(
            type(page) is ArchiveSearchPage
            and type(page.request) is ArchiveSearchRequest
            and _same_exact_graph(page.request, frozen_request)
            and type(page.results) is tuple
            and all(
                type(result) is ArchiveSearchResult
                and type(result.ordinal) is int
                and _canonical_candidate(result.candidate) is not None
                for result in page.results
            )
            and type(page.coverage) is tuple
            and all(_canonical_coverage(item) is not None for item in page.coverage)
            and type(page.warnings) is tuple
            and all(type(item) is ArchiveSearchWarning for item in page.warnings)
            and (page.continuation is None or type(page.continuation) is str)
        )
    except Exception:
        return False


def _identity(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail() from None
    if len(encoded) > 256 or any(ord(character) < 32 for character in value):
        raise _fail()
    return value


def _accepted_turn_snapshot(
    snapshot: str,
    request: ArchiveSearchRequest,
    *,
    current_conversation_id: str | None,
    boundary_user_message_id: str | None,
    accepted_boundary_identity_sha256: str | None,
) -> str:
    """Bind message continuations to the exact accepted current-user row."""

    if ArchiveSearchCorpus.MESSAGES not in request.corpora:
        if accepted_boundary_identity_sha256 is not None:
            raise _fail()
        return snapshot
    conversation = _identity(current_conversation_id)
    boundary = _identity(boundary_user_message_id)
    boundary_identity = accepted_boundary_identity_sha256
    if boundary_identity is not None and (
        type(boundary_identity) is not str
        or len(boundary_identity) != 64
        or any(character not in "0123456789abcdef" for character in boundary_identity)
    ):
        raise _fail()
    return (
        "archive-message-boundary:"
        + _mac(
            b"friday/archive-search-accepted-turn-snapshot/v1",
            {
                "boundary_user_message_id": boundary,
                "accepted_boundary_identity_sha256": boundary_identity,
                "current_conversation_id": conversation,
                "snapshot_discriminator": snapshot,
            },
        ).hex()
    )


def _storage_request(request: ArchiveSearchRequest) -> ArchiveSearchRequest:
    """Drop only transport state; storage and execution identity stay exact."""

    try:
        payload = request.to_private_payload()
        payload["continuation"] = None
        result = ArchiveSearchRequest.from_private_payload(payload)
        if result.to_identity_json() != request.to_identity_json():
            raise _fail()
        return result
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive search service value is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive search service value is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive search service value is process-private")


@dataclass(frozen=True, slots=True)
class _ArchiveTargetAuthority:
    corpus: SearchCorpus
    lane: SearchLane
    capability: str | None
    allowed: bool

    @property
    def target(self) -> tuple[SearchCorpus, SearchLane]:
        return self.corpus, self.lane

    def material(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "capability": self.capability,
            "corpus": self.corpus.value,
            "lane": self.lane.value,
        }

    def is_valid(self) -> bool:
        expected = _CORPUS_CAPABILITY.get(self.corpus)
        return bool(
            type(self) is _ArchiveTargetAuthority
            and type(self.corpus) is SearchCorpus
            and type(self.lane) is SearchLane
            and (self.capability is None or type(self.capability) is str)
            and self.capability == expected
            and type(self.allowed) is bool
            and (expected is not None or not self.allowed)
        )


def _authority_projection_is_valid(
    value: object,
    targets: tuple[tuple[SearchCorpus, SearchLane], ...],
) -> bool:
    try:
        return bool(
            type(value) is tuple
            and type(targets) is tuple
            and len(cast(tuple[object, ...], value)) == len(targets)
            and all(
                type(item) is _ArchiveTargetAuthority and item.is_valid()
                for item in cast(tuple[_ArchiveTargetAuthority, ...], value)
            )
            and tuple(item.target for item in cast(tuple[_ArchiveTargetAuthority, ...], value)) == targets
        )
    except Exception:
        return False


def _actor_is_exactly_bound(
    actor: object,
    *,
    tenant_id: str,
    principal_id: str,
) -> bool:
    if type(actor) is not ActorContext:
        return False
    value = cast(ActorContext, actor)
    try:
        return bool(
            type(value.user_id) is str
            and type(value.preset_key) is str
            and type(value.source) is str
            and (value.identity_id is None or type(value.identity_id) is str)
            and (value.session_id is None or type(value.session_id) is str)
            and type(value.shared_tenant) is bool
            and type(value.person_id) is str
            and value.user_id == tenant_id
            and value.own_id == principal_id
        )
    except Exception:
        return False


def _fresh_target_authority_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    principal_id: str,
    targets: tuple[tuple[SearchCorpus, SearchLane], ...],
) -> tuple[_ArchiveTargetAuthority, ...]:
    """Resolve exact per-corpus authority from the caller's live transaction."""

    if (
        type(conn) is not sqlite3.Connection
        or not conn.in_transaction
        or type(authorization) is not AuthorizationService
        or not _actor_is_exactly_bound(
            actor,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        or type(targets) is not tuple
    ):
        raise _fail()
    storage = authorization.storage
    try:
        if storage is None or storage.conn is not conn:
            raise _fail()
        principal_row = conn.execute(
            "SELECT preset_key, status FROM users WHERE id=?",
            (principal_id,),
        ).fetchone()
        tenant_row = (
            principal_row
            if tenant_id == principal_id
            else conn.execute(
                "SELECT status FROM users WHERE id=?",
                (tenant_id,),
            ).fetchone()
        )
        if (
            principal_row is None
            or tenant_row is None
            or str(principal_row["status"] or "") != "active"
            or str(tenant_row["status"] or "") != "active"
        ):
            raise _fail()
        fresh_actor = replace(
            actor,
            preset_key=str(principal_row["preset_key"] or "guest"),
        )
        if not _actor_is_exactly_bound(
            fresh_actor,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ):
            raise _fail()

        decisions: dict[str, bool] = {}
        for capability in sorted(
            {value for target in targets if (value := _CORPUS_CAPABILITY.get(target[0])) is not None}
        ):
            decision = authorization.authorize(fresh_actor, capability)
            if (
                type(decision) is not AuthorizationDecision
                or decision.security_id != capability
                or decision.user_id != principal_id
                or decision.preset_key != fresh_actor.preset_key
                or decision.effect not in {"allow", "deny"}
            ):
                raise _fail()
            decisions[capability] = decision.allowed
        projection = tuple(
            _ArchiveTargetAuthority(
                target[0],
                target[1],
                capability,
                False if capability is None else decisions[capability],
            )
            for target in targets
            for capability in (_CORPUS_CAPABILITY.get(target[0]),)
        )
        if not conn.in_transaction or not _authority_projection_is_valid(projection, targets):
            raise _fail()
        return projection
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


def _narrow_authority_projection(
    original: tuple[_ArchiveTargetAuthority, ...],
    current: tuple[_ArchiveTargetAuthority, ...],
    targets: tuple[tuple[SearchCorpus, SearchLane], ...],
) -> tuple[_ArchiveTargetAuthority, ...]:
    if not _authority_projection_is_valid(
        original,
        targets,
    ) or not _authority_projection_is_valid(current, targets):
        raise _fail()
    result = tuple(
        _ArchiveTargetAuthority(
            old.corpus,
            old.lane,
            old.capability,
            old.allowed and fresh.allowed,
        )
        for old, fresh in zip(original, current, strict=True)
        if old.target == fresh.target and old.capability == fresh.capability
    )
    if not _authority_projection_is_valid(result, targets):
        raise _fail()
    return result


def _continued_authority_projection(
    redemption: RedeemedArchiveContinuation,
    current: tuple[_ArchiveTargetAuthority, ...],
    targets: tuple[tuple[SearchCorpus, SearchLane], ...],
) -> tuple[_ArchiveTargetAuthority, ...]:
    """Recover the parent page's closed authority without accepting a new grant."""

    if type(redemption) is not RedeemedArchiveContinuation:
        raise _fail()
    try:
        coverage = redemption._coverage
        if (
            type(coverage) is not tuple
            or len(coverage) != len(targets)
            or any(type(item) is not SearchCoverage for item in coverage)
        ):
            raise _fail()
        by_target = {(item.corpus, item.lane): item for item in coverage}
        if set(by_target) != set(targets) or len(by_target) != len(targets):
            raise _fail()
        parent = tuple(
            _ArchiveTargetAuthority(
                target[0],
                target[1],
                capability,
                bool(
                    capability is not None
                    and CoverageState.PERMISSION_FILTERED not in by_target[target].states
                ),
            )
            for target in targets
            for capability in (_CORPUS_CAPABILITY.get(target[0]),)
        )
        return _narrow_authority_projection(parent, current, targets)
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


@dataclass(frozen=True, slots=True, repr=False)
class _ArchiveSearchRecipe:
    tenant_id: str
    principal_id: str
    request: ArchiveSearchRequest
    snapshot_discriminator: str
    current_conversation_id: str | None
    boundary_user_message_id: str | None
    accepted_boundary_identity_sha256: str | None
    dense_query_plan: ArchiveDenseQueryPlan | None
    continuation: bool
    target_authority: tuple[_ArchiveTargetAuthority, ...]
    seal: bytes

    def material(self) -> dict[str, object]:
        dense_projection = project_archive_dense_query_plan(
            self.dense_query_plan,
            principal_id=self.principal_id,
            query=self.request.dense_query,
        )
        return {
            "accepted_boundary_identity_sha256": self.accepted_boundary_identity_sha256,
            "boundary_user_message_id": self.boundary_user_message_id,
            "continuation": self.continuation,
            "current_conversation_id": self.current_conversation_id,
            "dense_query_plan_identity": (
                None if dense_projection is None else dense_projection.identity_sha256
            ),
            "principal_id": self.principal_id,
            "request": self.request.to_private_payload(),
            "snapshot_discriminator": self.snapshot_discriminator,
            "target_authority": [item.material() for item in self.target_authority],
            "tenant_id": self.tenant_id,
        }

    def is_valid(self) -> bool:
        try:
            frozen_request = ArchiveSearchRequest.parse_private(self.request.to_private_json())
            messages_requested = ArchiveSearchCorpus.MESSAGES in self.request.corpora
            dense_projection = project_archive_dense_query_plan(
                self.dense_query_plan,
                principal_id=self.principal_id,
                query=self.request.dense_query,
            )
            return bool(
                type(self) is _ArchiveSearchRecipe
                and type(self.request) is ArchiveSearchRequest
                and _same_exact_graph(self.request, frozen_request)
                and self.request.continuation is None
                and type(self.continuation) is bool
                and (
                    self.dense_query_plan is None
                    or (type(self.dense_query_plan) is ArchiveDenseQueryPlan and dense_projection is not None)
                )
                and _authority_projection_is_valid(
                    self.target_authority,
                    canonical_archive_search_targets(self.request),
                )
                and type(self.tenant_id) is str
                and type(self.principal_id) is str
                and type(self.snapshot_discriminator) is str
                and (self.current_conversation_id is None or type(self.current_conversation_id) is str)
                and (self.boundary_user_message_id is None or type(self.boundary_user_message_id) is str)
                and (
                    self.accepted_boundary_identity_sha256 is None
                    or (
                        type(self.accepted_boundary_identity_sha256) is str
                        and len(self.accepted_boundary_identity_sha256) == 64
                        and not any(
                            character not in "0123456789abcdef"
                            for character in self.accepted_boundary_identity_sha256
                        )
                    )
                )
                and (
                    (not messages_requested and self.accepted_boundary_identity_sha256 is None)
                    or (
                        messages_requested
                        and type(self.current_conversation_id) is str
                        and type(self.boundary_user_message_id) is str
                    )
                )
                and type(self.seal) is bytes
                and len(self.seal) == 32
                and hmac.compare_digest(
                    self.seal,
                    _mac(b"friday/archive-search-service-recipe/v1", self.material()),
                )
            )
        except Exception:
            return False


def _new_recipe(
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    current_conversation_id: str | None,
    boundary_user_message_id: str | None,
    accepted_boundary_identity_sha256: str | None,
    dense_query_plan: ArchiveDenseQueryPlan | None,
    continuation: bool,
    target_authority: tuple[_ArchiveTargetAuthority, ...],
) -> _ArchiveSearchRecipe:
    recipe = _ArchiveSearchRecipe(
        tenant_id=tenant_id,
        principal_id=principal_id,
        request=request,
        snapshot_discriminator=snapshot_discriminator,
        current_conversation_id=current_conversation_id,
        boundary_user_message_id=boundary_user_message_id,
        accepted_boundary_identity_sha256=accepted_boundary_identity_sha256,
        dense_query_plan=dense_query_plan,
        continuation=continuation,
        target_authority=target_authority,
        seal=b"0" * 32,
    )
    object.__setattr__(
        recipe,
        "seal",
        _mac(b"friday/archive-search-service-recipe/v1", recipe.material()),
    )
    if not recipe.is_valid():
        raise _fail()
    return recipe


class PreparedArchiveSearch(_ProcessPrivate):
    """Sealed run/batch plus the minimal recipe required for phase-2 refresh."""

    __slots__ = ("_batch", "_process_authority", "_recipe", "_run", "_seal")

    _batch: AuthorizedArchiveBatch
    _process_authority: object
    _recipe: _ArchiveSearchRecipe
    _run: ArchiveSearchRunBinding
    _seal: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("prepared archive search is immutable")

    def __repr__(self) -> str:
        return "<PreparedArchiveSearch sealed private>"

    def _material(self) -> dict[str, object]:
        return {
            "batch_sha256": hashlib.sha256(self._batch.model_visible_canonical_bytes).hexdigest(),
            "execution_handle": self._run.execution_binding.opaque_handle,
            "recipe_seal": self._recipe.seal.hex(),
        }

    def _is_valid(self) -> bool:
        try:
            return bool(
                type(self) is PreparedArchiveSearch
                and self._process_authority is _PROCESS_AUTHORITY
                and type(self._run) is ArchiveSearchRunBinding
                and type(self._batch) is AuthorizedArchiveBatch
                and _batch_contract_is_canonical(self._batch)
                and self._recipe.is_valid()
                and self._run.execution_binding.attests_private_request(
                    self._recipe.request.to_identity_json()
                )
                and self._run.execution_binding.attests_snapshot(self._recipe.snapshot_discriminator)
                and hmac.compare_digest(
                    self._seal,
                    _mac(b"friday/archive-search-service-prepared/v1", self._material()),
                )
            )
        except Exception:
            return False

    @property
    def run_binding(self) -> ArchiveSearchRunBinding:
        if not self._is_valid():
            raise _fail()
        return self._run

    @property
    def authorized_batch(self) -> AuthorizedArchiveBatch:
        if not self._is_valid():
            raise _fail()
        return self._batch

    def attests_origin(
        self,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
    ) -> bool:
        """Verify the body-free caller origin sealed into this prepared page."""

        try:
            if not self._is_valid() or type(request) is not ArchiveSearchRequest:
                return False
            request_value = ArchiveSearchRequest.parse_private(request.to_private_json())
            storage_request = _storage_request(request_value)
            snapshot = _accepted_turn_snapshot(
                _identity(snapshot_discriminator),
                storage_request,
                current_conversation_id=self._recipe.current_conversation_id,
                boundary_user_message_id=self._recipe.boundary_user_message_id,
                accepted_boundary_identity_sha256=(self._recipe.accepted_boundary_identity_sha256),
            )
            return bool(
                hmac.compare_digest(
                    storage_request.to_identity_json().encode("ascii"),
                    self._recipe.request.to_identity_json().encode("ascii"),
                )
                and hmac.compare_digest(
                    snapshot.encode("utf-8"),
                    self._recipe.snapshot_discriminator.encode("utf-8"),
                )
            )
        except Exception:
            return False


def _new_prepared(
    run: ArchiveSearchRunBinding,
    batch: AuthorizedArchiveBatch,
    recipe: _ArchiveSearchRecipe,
) -> PreparedArchiveSearch:
    result = cast(PreparedArchiveSearch, object.__new__(PreparedArchiveSearch))
    for name, value in (
        ("_batch", batch),
        ("_process_authority", _PROCESS_AUTHORITY),
        ("_recipe", recipe),
        ("_run", run),
        ("_seal", b"0" * 32),
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(
        result,
        "_seal",
        _mac(b"friday/archive-search-service-prepared/v1", result._material()),
    )
    if not result._is_valid():
        raise _fail()
    return result


def _same_message_exact_request(left: object, right: MessageExactRequest) -> bool:
    try:
        return bool(
            type(left) is MessageExactRequest
            and type(right) is MessageExactRequest
            and hmac.compare_digest(
                cast(MessageExactRequest, left).to_private_json().encode("ascii"),
                right.to_private_json().encode("ascii"),
            )
        )
    except Exception:
        return False


def _same_memory_exact_request(left: object, right: MemoryExactRequest) -> bool:
    try:
        return bool(
            type(left) is MemoryExactRequest
            and type(right) is MemoryExactRequest
            and hmac.compare_digest(
                cast(MemoryExactRequest, left).to_private_json().encode("ascii"),
                right.to_private_json().encode("ascii"),
            )
        )
    except Exception:
        return False


def _message_exact_chain_is_valid(
    request: MessageExactRequest | None,
    pages: tuple[MessageExactPage, ...],
) -> bool:
    try:
        if request is None:
            return not pages
        if not 1 <= len(pages) <= MAX_ARCHIVE_EXACT_CHAIN_PAGES:
            return False
        if any(type(page) is not MessageExactPage or not page._is_process_owned() for page in pages):
            return False
        if not _same_message_exact_request(pages[0].request, request):
            return False
        identity = request.to_identity_json().encode("ascii")
        first = pages[0]
        selection_handles: set[str] = set()
        row_ids: set[str] = set()
        chronological_keys: list[tuple[str, int]] = []
        for page in pages:
            if not hmac.compare_digest(page.request.to_identity_json().encode("ascii"), identity):
                return False
            if (
                page.principal_id != first.principal_id
                or page.authority_handle != first.authority_handle
                or page.snapshot_handle != first.snapshot_handle
                or page.total_rows != first.total_rows
                or page.boundary.message_id != first.boundary.message_id
                or page.boundary.revision_sha256 != first.boundary.revision_sha256
            ):
                return False
            if page.selection_handle in selection_handles:
                return False
            selection_handles.add(page.selection_handle)
            for row in page.rows:
                if row.message_id in row_ids:
                    return False
                row_ids.add(row.message_id)
                chronological_keys.append((row.created_at, row.storage_sequence))
        for previous, current in zip(pages, pages[1:], strict=False):
            outbound = previous.next_continuation
            inbound = current.request.continuation
            if (
                outbound is None
                or inbound is None
                or not hmac.compare_digest(outbound.token, inbound.token)
                or current.offset != previous.offset + len(previous.rows)
            ):
                return False
        return chronological_keys == sorted(chronological_keys) and len(chronological_keys) == len(
            set(chronological_keys)
        )
    except Exception:
        return False


def _memory_exact_chain_is_valid(
    request: MemoryExactRequest | None,
    pages: tuple[MemoryExactPage, ...],
) -> bool:
    try:
        if request is None:
            return not pages
        if not 1 <= len(pages) <= MAX_ARCHIVE_EXACT_CHAIN_PAGES:
            return False
        if any(type(page) is not MemoryExactPage or not page._is_process_owned() for page in pages):
            return False
        if not _same_memory_exact_request(pages[0].request, request):
            return False
        identity = request.to_identity_json().encode("ascii")
        first = pages[0]
        selection_handles: set[str] = set()
        knowledge_ids: set[str] = set()
        revisions: set[str] = set()
        for page in pages:
            if not hmac.compare_digest(page.request.to_identity_json().encode("ascii"), identity):
                return False
            if (
                page.authority_handle != first.authority_handle
                or page.snapshot_handle != first.snapshot_handle
                or page.graph_source_set_sha256 != first.graph_source_set_sha256
                or page.total_rows != first.total_rows
                or page.snapshot_rows != first.snapshot_rows
                or page.matched_rows != first.matched_rows
                or page.date_window_status != first.date_window_status
                or page.temporal_status != first.temporal_status
            ):
                return False
            if page.selection_handle in selection_handles:
                return False
            selection_handles.add(page.selection_handle)
            for candidate in page.candidates:
                if (
                    candidate.knowledge_id in knowledge_ids
                    or candidate.candidate_revision_sha256 in revisions
                ):
                    return False
                knowledge_ids.add(candidate.knowledge_id)
                revisions.add(candidate.candidate_revision_sha256)
        for previous, current in zip(pages, pages[1:], strict=False):
            outbound = previous.next_continuation
            inbound = current.request.continuation
            if (
                outbound is None
                or inbound is None
                or not hmac.compare_digest(outbound.token, inbound.token)
                or current.offset != previous.offset + len(previous.candidates)
            ):
                return False
        return True
    except Exception:
        return False


def _message_exact_boundary_identity(page: MessageExactPage) -> str:
    boundary = page.boundary
    return hashlib.sha256(
        _canonical_bytes(
            {
                "schema": "friday.private-message-window-boundary.v1",
                "id": boundary.message_id,
                "conversation_id": boundary.conversation_id,
                "person_id": boundary.principal_id,
                "role": boundary.role.value,
                "content": boundary.content,
                "created_at": boundary.created_at,
            }
        )
    ).hexdigest()


def _composite_scope_is_valid(
    request: ArchiveSearchRequest,
    prepared_search: PreparedArchiveSearch,
    message_exact_pages: tuple[MessageExactPage, ...],
    memory_exact_pages: tuple[MemoryExactPage, ...],
) -> bool:
    try:
        recipe = prepared_search._recipe
        if not hmac.compare_digest(
            recipe.request.to_private_json().encode("ascii"),
            _storage_request(request).to_private_json().encode("ascii"),
        ):
            return False
        message_request = request.message_exact_request
        memory_request = request.memory_exact_request
        if message_request is not None and (
            recipe.current_conversation_id != message_request.conversation_id
            or recipe.boundary_user_message_id != message_request.accepted_boundary_user_message_id
            or any(page.principal_id != recipe.principal_id for page in message_exact_pages)
            or recipe.accepted_boundary_identity_sha256 is None
            or not hmac.compare_digest(
                recipe.accepted_boundary_identity_sha256,
                _message_exact_boundary_identity(message_exact_pages[0]),
            )
        ):
            return False
        return memory_request is None or (
            memory_request.tenant_id == recipe.tenant_id
            and memory_request.principal_id == recipe.principal_id
        )
    except Exception:
        return False


class PreparedArchiveSearchComposite(_ProcessPrivate):
    """Sealed bounded page-chain segment for one private v3 request.

    This passive carrier grants no retrieval or publication authority.  A last
    page may retain an outbound continuation; each exact adapter still owns its
    normal late authorization before any answer can be published.
    """

    __slots__ = (
        "_memory_exact_pages",
        "_message_exact_pages",
        "_prepared_search",
        "_process_authority",
        "_request",
        "_seal",
    )

    _memory_exact_pages: tuple[MemoryExactPage, ...]
    _message_exact_pages: tuple[MessageExactPage, ...]
    _prepared_search: PreparedArchiveSearch
    _process_authority: object
    _request: ArchiveSearchRequest
    _seal: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("prepared archive search composite is immutable")

    def __repr__(self) -> str:
        return "<PreparedArchiveSearchComposite sealed private>"

    def _material(self) -> dict[str, object]:
        return {
            "archive_page_seal": self._prepared_search._seal.hex(),
            "memory_page_handles": [item.selection_handle for item in self._memory_exact_pages],
            "message_page_handles": [item.selection_handle for item in self._message_exact_pages],
            "request_sha256": hashlib.sha256(self._request.to_private_json().encode("ascii")).hexdigest(),
            "schema": "friday.prepared-archive-search-composite.private.v1",
        }

    def _is_valid(self) -> bool:
        try:
            return bool(
                type(self) is PreparedArchiveSearchComposite
                and self._process_authority is _PROCESS_AUTHORITY
                and type(self._request) is ArchiveSearchRequest
                and type(self._prepared_search) is PreparedArchiveSearch
                and self._prepared_search._is_valid()
                and type(self._message_exact_pages) is tuple
                and type(self._memory_exact_pages) is tuple
                and (
                    self._request.message_exact_request is not None
                    or self._request.memory_exact_request is not None
                )
                and ArchiveSearchRequest.parse_private(self._request.to_private_json()) == self._request
                and self._prepared_search._run._request is self._request
                and _message_exact_chain_is_valid(
                    self._request.message_exact_request,
                    self._message_exact_pages,
                )
                and _memory_exact_chain_is_valid(
                    self._request.memory_exact_request,
                    self._memory_exact_pages,
                )
                and _composite_scope_is_valid(
                    self._request,
                    self._prepared_search,
                    self._message_exact_pages,
                    self._memory_exact_pages,
                )
                and type(self._seal) is bytes
                and len(self._seal) == 32
                and hmac.compare_digest(
                    self._seal,
                    _mac(b"friday/prepared-archive-search-composite/v1", self._material()),
                )
            )
        except Exception:
            return False

    @property
    def request(self) -> ArchiveSearchRequest:
        if not self._is_valid():
            raise _fail()
        return self._request

    @property
    def prepared_search(self) -> PreparedArchiveSearch:
        if not self._is_valid():
            raise _fail()
        return self._prepared_search

    @property
    def message_exact_pages(self) -> tuple[MessageExactPage, ...]:
        if not self._is_valid():
            raise _fail()
        return self._message_exact_pages

    @property
    def memory_exact_pages(self) -> tuple[MemoryExactPage, ...]:
        if not self._is_valid():
            raise _fail()
        return self._memory_exact_pages


def compose_prepared_archive_searches(
    prepared_search: PreparedArchiveSearch,
    *,
    message_exact_pages: tuple[MessageExactPage, ...] = (),
    memory_exact_pages: tuple[MemoryExactPage, ...] = (),
) -> PreparedArchiveSearchComposite:
    """Seal bounded exact-page prefixes without executing or projecting a lane."""

    try:
        if (
            type(prepared_search) is not PreparedArchiveSearch
            or not prepared_search._is_valid()
            or type(message_exact_pages) is not tuple
            or type(memory_exact_pages) is not tuple
            or len(message_exact_pages) > MAX_ARCHIVE_EXACT_CHAIN_PAGES
            or len(memory_exact_pages) > MAX_ARCHIVE_EXACT_CHAIN_PAGES
        ):
            raise _fail()
        request = prepared_search._run._request
        if type(request) is not ArchiveSearchRequest:
            raise _fail()
        result = cast(
            PreparedArchiveSearchComposite,
            object.__new__(PreparedArchiveSearchComposite),
        )
        for name, value in (
            ("_request", request),
            ("_prepared_search", prepared_search),
            ("_message_exact_pages", message_exact_pages),
            ("_memory_exact_pages", memory_exact_pages),
            ("_process_authority", _PROCESS_AUTHORITY),
            ("_seal", b"0" * 32),
        ):
            object.__setattr__(result, name, value)
        object.__setattr__(
            result,
            "_seal",
            _mac(b"friday/prepared-archive-search-composite/v1", result._material()),
        )
        if not result._is_valid():
            raise _fail()
        return result
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


def _coverage(
    target: tuple[SearchCorpus, SearchLane],
    binding: SearchExecutionBinding,
    *,
    states: tuple[CoverageState, ...],
    authority_rechecked: bool,
    snapshot_current: bool,
) -> SearchCoverage:
    return SearchCoverage.create(
        corpus=target[0],
        lane=target[1],
        execution_binding=binding,
        states=states,
        eligible_authorized=None,
        examined=0,
        matched_at_least=0,
        returned=0,
        authority_rechecked=authority_rechecked,
        snapshot_current=snapshot_current,
    )


def _unsupported(
    target: tuple[SearchCorpus, SearchLane],
    binding: SearchExecutionBinding,
) -> SearchCoverage:
    return _coverage(
        target,
        binding,
        states=(CoverageState.UNAVAILABLE,),
        authority_rechecked=True,
        snapshot_current=True,
    )


def _permission_filtered(
    target: tuple[SearchCorpus, SearchLane],
    binding: SearchExecutionBinding,
) -> SearchCoverage:
    return _coverage(
        target,
        binding,
        states=(CoverageState.PARTIAL, CoverageState.PERMISSION_FILTERED),
        authority_rechecked=True,
        snapshot_current=True,
    )


def _storage_unavailable(
    target: tuple[SearchCorpus, SearchLane],
    binding: SearchExecutionBinding,
) -> SearchCoverage:
    return _coverage(
        target,
        binding,
        states=(CoverageState.UNAVAILABLE,),
        authority_rechecked=False,
        snapshot_current=False,
    )


def _collect_document_target(
    conn: sqlite3.Connection,
    *,
    recipe: _ArchiveSearchRecipe,
    run: ArchiveSearchRunBinding,
    target: tuple[SearchCorpus, SearchLane],
    limit: int,
) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
    if target[1] is SearchLane.DENSE and recipe.dense_query_plan is None:
        return (), _unsupported(target, run.execution_binding)
    page = search_archive_document_lane(
        conn,
        tenant_id=recipe.tenant_id,
        owner_id=recipe.principal_id,
        request=recipe.request,
        corpus=_DOCUMENT_CORPUS[target[0]],
        lane=target[1],
        execution_binding=run.execution_binding,
        snapshot_discriminator=recipe.snapshot_discriminator,
        snapshot_current=True,
        dense_query_plan=recipe.dense_query_plan if target[1] is SearchLane.DENSE else None,
        limit=limit,
    )
    if not page.available and not page.authority_rechecked:
        return (), _permission_filtered(target, run.execution_binding)
    coverage = page.to_coverage(
        execution_binding=run.execution_binding,
        tenant_id=recipe.tenant_id,
        owner_id=recipe.principal_id,
        request=recipe.request,
        snapshot_discriminator=recipe.snapshot_discriminator,
    )
    return page.candidates, coverage


def _collect_message_target(
    conn: sqlite3.Connection,
    *,
    recipe: _ArchiveSearchRecipe,
    run: ArchiveSearchRunBinding,
    target: tuple[SearchCorpus, SearchLane],
    limit: int,
) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
    controls = archive_message_storage_controls(recipe.request)
    if (
        recipe.accepted_boundary_identity_sha256 is None
        or recipe.current_conversation_id is None
        or recipe.boundary_user_message_id is None
    ):
        return (), _permission_filtered(target, run.execution_binding)
    fresh_boundary_identity = _accepted_archive_message_boundary_identity_in_transaction(
        conn,
        principal_id=recipe.principal_id,
        conversation_id=recipe.current_conversation_id,
        boundary_user_message_id=recipe.boundary_user_message_id,
    )
    if fresh_boundary_identity is None:
        return (), _permission_filtered(target, run.execution_binding)
    if not hmac.compare_digest(
        fresh_boundary_identity,
        recipe.accepted_boundary_identity_sha256,
    ):
        raise _ArchiveAcceptedBoundaryDrift("archive search service is unavailable")
    try:
        page = select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=recipe.principal_id,
            query=recipe.request.query,
            selection_lane=target[1],
            scope=cast(ArchiveMessageScope, controls["scope"]),
            conversation_id=recipe.current_conversation_id,
            boundary_user_message_id=recipe.boundary_user_message_id,
            roles=cast(tuple[MessageRole, ...], controls["roles"]),
            lifecycle_states=cast(tuple[LifecycleState, ...], controls["lifecycle_states"]),
            since=cast(str | None, controls["since"]),
            until=cast(str | None, controls["until"]),
            limit=limit,
            context_before=cast(int, controls["context_before"]),
            context_after=cast(int, controls["context_after"]),
        )
    except ArchiveMessageStorageError:
        if target[1] is not SearchLane.LEXICAL:
            raise
        return (), _coverage(
            target,
            run.execution_binding,
            states=(CoverageState.PARTIAL, CoverageState.BACKFILL_PENDING),
            authority_rechecked=True,
            snapshot_current=False,
        )
    if page is None:
        return (), _permission_filtered(target, run.execution_binding)
    if (
        recipe.accepted_boundary_identity_sha256 is None
        or page.boundary_identity_sha256 is None
        or not hmac.compare_digest(
            page.boundary_identity_sha256,
            recipe.accepted_boundary_identity_sha256,
        )
    ):
        raise _ArchiveAcceptedBoundaryDrift("archive search service is unavailable")
    if target[1] is SearchLane.LEXICAL:
        for ledger in page.ledgers:
            selected = select_authorized_conversation_passage_projection_in_transaction(
                conn,
                principal_id=recipe.principal_id,
                boundary_conversation_id=recipe.current_conversation_id,
                origin_boundary_user_message_id=recipe.boundary_user_message_id,
                conversation_id=ledger.conversation_id,
                limit=1,
            )
            if (
                selected is None
                or not selected.authorized_projection_complete
                or not hmac.compare_digest(
                    selected.boundary_identity_sha256,
                    recipe.accepted_boundary_identity_sha256,
                )
            ):
                return (), _coverage(
                    target,
                    run.execution_binding,
                    states=(CoverageState.PARTIAL, CoverageState.BACKFILL_PENDING),
                    authority_rechecked=True,
                    snapshot_current=False,
                )
    projection = project_archive_message_page(
        tenant_id=recipe.tenant_id,
        principal_id=recipe.principal_id,
        request=recipe.request,
        page=page,
        index_state=CatalogIndexState(
            CatalogIndexLane.LEXICAL,
            (CatalogIndexStatus.PARTIAL if target[1] is SearchLane.LEXICAL else CatalogIndexStatus.CURRENT),
            (IndexIncompleteReason.BACKFILL_PENDING if target[1] is SearchLane.LEXICAL else None),
        ),
        execution_binding=run.execution_binding,
        snapshot_discriminator=recipe.snapshot_discriminator,
        selection_lane=target[1],
        current_conversation_id=recipe.current_conversation_id,
        boundary_user_message_id=recipe.boundary_user_message_id,
    )
    candidates = projection.candidates
    coverage = projection.to_coverage(
        run.execution_binding,
        tenant_id=recipe.tenant_id,
        principal_id=recipe.principal_id,
        snapshot_discriminator=recipe.snapshot_discriminator,
        returned=len(candidates),
    )
    return candidates, coverage


def _cap_materialized_target(
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: SearchCoverage,
    *,
    limit: int,
) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
    """Apply the final fair share to an already revalidated dense target."""

    if len(candidates) <= limit:
        return candidates, coverage
    states = set(coverage.states)
    states.discard(CoverageState.COMPLETE)
    states.update((CoverageState.CAPPED, CoverageState.PARTIAL))
    bounded = candidates[:limit]
    return bounded, SearchCoverage.create(
        corpus=coverage.corpus,
        lane=coverage.lane,
        execution_binding=coverage.execution_binding,
        states=states,
        eligible_authorized=coverage.eligible_authorized,
        examined=coverage.examined,
        matched_at_least=coverage.matched_at_least,
        returned=len(bounded),
        limit=limit,
        next_cursor_available=False,
        authority_rechecked=coverage.authority_rechecked,
        snapshot_current=coverage.snapshot_current,
    )


def _obsidian_phase(phase: ArchiveSearchAuthorityPhase) -> ArchiveObsidianReadPhase:
    return {
        ArchiveSearchAuthorityPhase.BEFORE_MODEL: ArchiveObsidianReadPhase.BEFORE_MODEL,
        ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION: (ArchiveObsidianReadPhase.BEFORE_PUBLICATION),
    }[phase]


def _collect_obsidian_target(
    conn: sqlite3.Connection,
    *,
    recipe: _ArchiveSearchRecipe,
    run: ArchiveSearchRunBinding,
    target: tuple[SearchCorpus, SearchLane],
    phase: ArchiveSearchAuthorityPhase,
    exact_file_reader: ArchiveObsidianExactFileReader | None,
) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
    page = select_archive_obsidian_lane_in_transaction(
        conn,
        tenant_id=recipe.tenant_id,
        principal_id=recipe.principal_id,
        request=recipe.request,
        snapshot_discriminator=recipe.snapshot_discriminator,
        execution_binding=run.execution_binding,
        lane=target[1],
        limit=_INTERNAL_LANE_LIMIT,
    )
    reason = page.unavailable_reason
    if reason is ArchiveObsidianUnavailableReason.PRINCIPAL_DENIED:
        return (), _permission_filtered(target, run.execution_binding)
    if reason in {
        ArchiveObsidianUnavailableReason.TEMPORAL_UNSUPPORTED,
        ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED,
        ArchiveObsidianUnavailableReason.LANE_UNSUPPORTED,
    }:
        return (), _unsupported(target, run.execution_binding)
    if reason is ArchiveObsidianUnavailableReason.STORAGE_UNAVAILABLE:
        return (), _storage_unavailable(target, run.execution_binding)
    projection = project_archive_obsidian_lane_page_in_transaction(
        conn,
        tenant_id=recipe.tenant_id,
        principal_id=recipe.principal_id,
        request=recipe.request,
        snapshot_discriminator=recipe.snapshot_discriminator,
        execution_binding=run.execution_binding,
        page=page,
        phase=_obsidian_phase(phase),
        exact_file_reader=exact_file_reader,
    )
    coverage = projection.to_coverage(
        execution_binding=run.execution_binding,
        tenant_id=recipe.tenant_id,
        principal_id=recipe.principal_id,
        request=recipe.request,
        snapshot_discriminator=recipe.snapshot_discriminator,
        phase=_obsidian_phase(phase),
    )
    return projection.candidates, coverage


def _collect_federated_in_transaction(
    conn: sqlite3.Connection,
    *,
    recipe: _ArchiveSearchRecipe,
    run: ArchiveSearchRunBinding,
    phase: ArchiveSearchAuthorityPhase,
    exact_file_reader: ArchiveObsidianExactFileReader | None,
    target_authority: tuple[_ArchiveTargetAuthority, ...] | None = None,
) -> FederatedArchiveSearch:
    if (
        type(conn) is not sqlite3.Connection
        or not conn.in_transaction
        or not recipe.is_valid()
        or type(run) is not ArchiveSearchRunBinding
        or type(phase) is not ArchiveSearchAuthorityPhase
    ):
        raise _fail()
    binding = run.execution_binding
    targets = canonical_archive_search_targets(recipe.request)
    if binding.requested_targets != targets:
        raise _fail()
    authority = recipe.target_authority if target_authority is None else target_authority
    if not _authority_projection_is_valid(authority, targets):
        raise _fail()
    authority_by_target = {item.target: item for item in authority}
    candidates_by_target: dict[tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]] = {}
    coverage_by_target: dict[tuple[SearchCorpus, SearchLane], SearchCoverage] = {}
    dense_results: dict[
        tuple[SearchCorpus, SearchLane],
        tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage],
    ] = {}
    if recipe.dense_query_plan is not None:
        for target in targets:
            target_permission = authority_by_target[target]
            if (
                target[0] not in _DOCUMENT_CORPUS
                or target[1] is not SearchLane.DENSE
                or (target_permission.capability is not None and not target_permission.allowed)
            ):
                continue
            try:
                dense_results[target] = _collect_document_target(
                    conn,
                    recipe=recipe,
                    run=run,
                    target=target,
                    limit=MAX_ARCHIVE_MATERIALIZED_CANDIDATES,
                )
            except _ArchiveAcceptedBoundaryDrift:
                raise
            except Exception:
                dense_results[target] = ((), _storage_unavailable(target, binding))
    usable_dense_targets = frozenset(
        target for target, (candidates, _coverage_value) in dense_results.items() if candidates
    )
    materialized_limit = MAX_ARCHIVE_MATERIALIZED_CANDIDATES
    if any(
        corpus in _DOCUMENT_CORPUS
        and lane in _DOCUMENT_LANES
        or corpus is SearchCorpus.CONVERSATION
        and lane in {SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}
        for corpus, lane in targets
    ):
        materialized_limit = _materialized_lane_limit(
            recipe.request,
            usable_dense_targets=usable_dense_targets,
        )
    for target in targets:
        candidates: tuple[ArchiveSearchCandidate, ...] = ()
        coverage = _unsupported(target, binding)
        target_permission = authority_by_target[target]
        if target_permission.capability is not None and not target_permission.allowed:
            candidates_by_target[target] = ()
            coverage_by_target[target] = _permission_filtered(target, binding)
            continue
        try:
            if target in dense_results:
                candidates, coverage = _cap_materialized_target(
                    *dense_results[target],
                    limit=materialized_limit,
                )
            elif target[0] in _DOCUMENT_CORPUS and target[1] in _DOCUMENT_LANES:
                candidates, coverage = _collect_document_target(
                    conn,
                    recipe=recipe,
                    run=run,
                    target=target,
                    limit=materialized_limit,
                )
            elif target[0] is SearchCorpus.CONVERSATION and target[1] in {
                SearchLane.LEXICAL,
                SearchLane.MESSAGE_HISTORY,
            }:
                candidates, coverage = _collect_message_target(
                    conn,
                    recipe=recipe,
                    run=run,
                    target=target,
                    limit=materialized_limit,
                )
            elif target[0] is SearchCorpus.OBSIDIAN and target[1] in _OBSIDIAN_LANES:
                candidates, coverage = _collect_obsidian_target(
                    conn,
                    recipe=recipe,
                    run=run,
                    target=target,
                    phase=phase,
                    exact_file_reader=exact_file_reader,
                )
        except _ArchiveAcceptedBoundaryDrift:
            raise
        except Exception:
            candidates = ()
            coverage = _storage_unavailable(target, binding)
        candidates_by_target[target] = candidates
        coverage_by_target[target] = coverage
    if not conn.in_transaction:
        raise _fail()
    try:
        return federate_archive_search(
            request=recipe.request,
            execution_binding=binding,
            coverage=tuple(coverage_by_target[target] for target in targets),
            candidates_by_target=candidates_by_target,
        )
    except Exception:
        raise _fail() from None


def _federation_material(value: FederatedArchiveSearch) -> dict[str, object]:
    return {
        "candidates": [item.to_private_payload() for item in value.candidates],
        "coverage": [item.to_payload() for item in value.coverage],
        "tail": [item.to_private_payload() for item in value.tail_candidates],
        "terminal_coverage": [item.to_payload() for item in value.terminal_coverage],
        "warnings": [item.value for item in value.warnings],
    }


def _same_federation(
    left: FederatedArchiveSearch,
    right: FederatedArchiveSearch,
) -> bool:
    try:
        return hmac.compare_digest(
            hashlib.sha256(_canonical_bytes(_federation_material(left))).digest(),
            hashlib.sha256(_canonical_bytes(_federation_material(right))).digest(),
        )
    except Exception:
        return False


@dataclass(frozen=True, slots=True)
class _FreshEvidence:
    run: ArchiveSearchRunBinding
    recipe: _ArchiveSearchRecipe
    federation: FederatedArchiveSearch
    target_authority: tuple[_ArchiveTargetAuthority, ...]


class ArchiveSearchReauthorizationContext(_ProcessPrivate):
    """Sealed fresh full-universe evidence for authority callbacks."""

    __slots__ = ("_entries", "_phase", "_process_authority", "_seal")

    _entries: tuple[_FreshEvidence, ...]
    _phase: ArchiveSearchAuthorityPhase
    _process_authority: object
    _seal: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive reauthorization context is immutable")

    def __repr__(self) -> str:
        return "<ArchiveSearchReauthorizationContext sealed private>"

    def _material(self) -> dict[str, object]:
        return {
            "entries": [
                {
                    "execution_handle": item.run.execution_binding.opaque_handle,
                    "federation": _federation_material(item.federation),
                    "recipe_seal": item.recipe.seal.hex(),
                    "target_authority": [authority.material() for authority in item.target_authority],
                }
                for item in self._entries
            ],
            "phase": self._phase.value,
        }

    def _is_valid(self) -> bool:
        try:
            return bool(
                type(self) is ArchiveSearchReauthorizationContext
                and self._process_authority is _PROCESS_AUTHORITY
                and type(self._phase) is ArchiveSearchAuthorityPhase
                and type(self._entries) is tuple
                and self._entries
                and all(
                    type(item) is _FreshEvidence
                    and type(item.run) is ArchiveSearchRunBinding
                    and item.recipe.is_valid()
                    and type(item.federation) is FederatedArchiveSearch
                    and _authority_projection_is_valid(
                        item.target_authority,
                        canonical_archive_search_targets(item.recipe.request),
                    )
                    for item in self._entries
                )
                and len({id(item.run) for item in self._entries}) == len(self._entries)
                and hmac.compare_digest(
                    self._seal,
                    _mac(b"friday/archive-search-service-context/v1", self._material()),
                )
            )
        except Exception:
            return False


def _new_context(
    phase: ArchiveSearchAuthorityPhase,
    entries: tuple[_FreshEvidence, ...],
) -> ArchiveSearchReauthorizationContext:
    context = cast(
        ArchiveSearchReauthorizationContext,
        object.__new__(ArchiveSearchReauthorizationContext),
    )
    for name, value in (
        ("_entries", entries),
        ("_phase", phase),
        ("_process_authority", _PROCESS_AUTHORITY),
        ("_seal", b"0" * 32),
    ):
        object.__setattr__(context, name, value)
    object.__setattr__(
        context,
        "_seal",
        _mac(b"friday/archive-search-service-context/v1", context._material()),
    )
    if not context._is_valid():
        raise _fail()
    return context


def _context_entry(
    context: object,
    phase: ArchiveSearchAuthorityPhase,
    run: ArchiveSearchRunBinding,
) -> _FreshEvidence | None:
    if (
        type(context) is not ArchiveSearchReauthorizationContext
        or not cast(ArchiveSearchReauthorizationContext, context)._is_valid()
        or cast(ArchiveSearchReauthorizationContext, context)._phase is not phase
    ):
        return None
    return next(
        (item for item in cast(ArchiveSearchReauthorizationContext, context)._entries if item.run is run),
        None,
    )


def _same_candidate(left: ArchiveSearchCandidate, right: ArchiveSearchCandidate) -> bool:
    try:
        frozen_left = _canonical_candidate(left)
        frozen_right = _canonical_candidate(right)
        return bool(
            frozen_left is not None
            and frozen_right is not None
            and hmac.compare_digest(
                frozen_left.to_private_json(),
                frozen_right.to_private_json(),
            )
        )
    except Exception:
        return False


def _target_authority(
    evidence: _FreshEvidence,
    target: tuple[SearchCorpus, SearchLane],
) -> _ArchiveTargetAuthority | None:
    return next(
        (item for item in evidence.target_authority if item.target == target),
        None,
    )


def _same_continuation_candidate_evidence(
    observed: ArchiveSearchCandidate,
    fresh: ArchiveSearchCandidate,
) -> bool:
    """Allow only authority-produced page-relative rank rebasing on resume."""

    observed_value = _canonical_candidate(observed)
    fresh_value = _canonical_candidate(fresh)
    if observed_value is None or fresh_value is None:
        return False
    if (
        observed_value.resolved_source.source_ref != fresh_value.resolved_source.source_ref
        or len(observed_value.matches) != len(fresh_value.matches)
        or any(
            left.channel is not right.channel or left.rank > right.rank
            for left, right in zip(
                observed_value.matches,
                fresh_value.matches,
                strict=True,
            )
        )
    ):
        return False
    try:
        rebased = ArchiveSearchCandidate.create(
            corpus=observed_value.corpus,
            resolved_source=observed_value.resolved_source,
            title=observed_value.title,
            filename=observed_value.filename,
            review_state=observed_value.review_state,
            evidence_authority=observed_value.evidence_authority,
            lifecycle_state=observed_value.lifecycle_state,
            matches=fresh_value.matches,
            temporal_facts=observed_value.temporal_facts,
            passages=observed_value.passages,
        )
        return _same_candidate(rebased, fresh_value)
    except Exception:
        return False


def _fresh_coverage(
    evidence: _FreshEvidence,
    target: tuple[SearchCorpus, SearchLane],
    *,
    terminal: bool,
) -> SearchCoverage | None:
    values = evidence.federation.terminal_coverage if terminal else evidence.federation.coverage
    return next(
        (item for item in values if (item.corpus, item.lane) == target),
        None,
    )


def _same_coverage(left: SearchCoverage, right: SearchCoverage) -> bool:
    try:
        frozen_left = _canonical_coverage(left)
        frozen_right = _canonical_coverage(right)
        return bool(
            left.execution_binding is right.execution_binding
            and frozen_left is not None
            and frozen_right is not None
            and hmac.compare_digest(frozen_left.to_json(), frozen_right.to_json())
        )
    except Exception:
        return False


def _expected_degraded_coverage(value: SearchCoverage) -> SearchCoverage | None:
    failures: set[CoverageState] = set()
    if not value.authority_rechecked:
        failures.add(CoverageState.UNAVAILABLE)
    elif not value.snapshot_current:
        failures.add(CoverageState.STALE)
    if not failures:
        return None
    states: set[CoverageState] = {item for item in value.states if item is not CoverageState.COMPLETE}
    states.add(CoverageState.PARTIAL)
    states.update(failures)
    try:
        return SearchCoverage.create(
            corpus=value.corpus,
            lane=value.lane,
            execution_binding=value.execution_binding,
            states=states,
            eligible_authorized=None,
            examined=0,
            matched_at_least=0,
            returned=0,
            authority_rechecked=CoverageState.UNAVAILABLE not in failures,
            snapshot_current=False,
            limit=value.limit,
            next_cursor_available=False,
        )
    except Exception:
        return None


def _continuation_coverage_attested(
    observed: SearchCoverage,
    fresh: SearchCoverage,
    *,
    limit: int,
) -> bool:
    try:
        if (
            observed.execution_binding is not fresh.execution_binding
            or (observed.corpus, observed.lane) != (fresh.corpus, fresh.lane)
            or observed.eligible_authorized != fresh.eligible_authorized
            or observed.examined != fresh.examined
            or observed.matched_at_least != fresh.matched_at_least
            or observed.authority_rechecked is not fresh.authority_rechecked
            or observed.snapshot_current is not fresh.snapshot_current
            or observed.returned > fresh.matched_at_least
        ):
            return False
        if observed.next_cursor_available:
            states: set[CoverageState] = {item for item in fresh.states if item is not CoverageState.COMPLETE}
            states.update({CoverageState.PARTIAL, CoverageState.CAPPED})
            expected_limit: int | None = limit
        else:
            states = set(fresh.states)
            expected_limit = fresh.limit if fresh.limit is None or fresh.limit >= observed.returned else limit
        expected = SearchCoverage.create(
            corpus=fresh.corpus,
            lane=fresh.lane,
            execution_binding=fresh.execution_binding,
            states=states,
            eligible_authorized=fresh.eligible_authorized,
            examined=fresh.examined,
            matched_at_least=fresh.matched_at_least,
            returned=observed.returned,
            authority_rechecked=fresh.authority_rechecked,
            snapshot_current=fresh.snapshot_current,
            limit=expected_limit,
            next_cursor_available=observed.next_cursor_available,
        )
        return _same_coverage(observed, expected)
    except Exception:
        return False


def _coverage_attested(
    evidence: _FreshEvidence,
    coverage: SearchCoverage,
    *,
    phase: ArchiveSearchAuthorityPhase,
) -> bool:
    target = coverage.corpus, coverage.lane
    fresh = _fresh_coverage(evidence, target, terminal=evidence.recipe.continuation)
    if fresh is None:
        return False
    if evidence.recipe.continuation:
        direct = _continuation_coverage_attested(
            coverage,
            fresh,
            limit=evidence.recipe.request.limit,
        )
        if direct:
            return True
        degraded = _expected_degraded_coverage(fresh)
        return bool(
            phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION
            and degraded is not None
            and _same_coverage(coverage, degraded)
        )
    if _same_coverage(coverage, fresh):
        return True
    degraded = _expected_degraded_coverage(fresh)
    return bool(
        phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION
        and degraded is not None
        and _same_coverage(coverage, degraded)
    )


def _rejection_for_coverage(value: SearchCoverage | None) -> ArchiveSearchReauthorizationStatus:
    if value is None:
        return ArchiveSearchReauthorizationStatus.UNAVAILABLE
    if CoverageState.PERMISSION_FILTERED in value.states:
        return ArchiveSearchReauthorizationStatus.DENIED
    if not value.authority_rechecked or CoverageState.UNAVAILABLE in value.states:
        return ArchiveSearchReauthorizationStatus.UNAVAILABLE
    return ArchiveSearchReauthorizationStatus.DRIFTED


def reauthorize_archive_search_candidate(
    phase: ArchiveSearchAuthorityPhase,
    run_binding: ArchiveSearchRunBinding,
    candidate: ArchiveSearchCandidate,
    authority_context: object,
    /,
) -> ArchiveSearchCandidateReauthorization:
    """Canonical callback for both model admission and publication attestation."""

    evidence = _context_entry(authority_context, phase, run_binding)
    if evidence is None or type(candidate) is not ArchiveSearchCandidate:
        return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.UNAVAILABLE)
    try:
        corpus = {
            ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
            ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
            ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
            ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
            ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
            ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
            ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
        }[candidate.corpus]
        candidate_targets = tuple((corpus, match.channel.search_lane) for match in candidate.matches)
        target_permissions = tuple(_target_authority(evidence, target) for target in candidate_targets)
    except Exception:
        return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.UNAVAILABLE)
    if any(item is None or item.capability is None for item in target_permissions):
        return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.UNAVAILABLE)
    if any(not item.allowed for item in target_permissions if item is not None):
        return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.DENIED)
    fresh_candidates = (
        *evidence.federation.candidates,
        *evidence.federation.tail_candidates,
    )
    current = next(
        (item for item in fresh_candidates if _same_candidate(item, candidate)),
        None,
    )
    if current is not None:
        return ArchiveSearchCandidateReauthorization.authorized(current)
    if (
        phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION
        and evidence.recipe.continuation
        and any(_same_continuation_candidate_evidence(candidate, item) for item in fresh_candidates)
    ):
        return ArchiveSearchCandidateReauthorization.authorized(candidate)
    target_coverages = tuple(
        _fresh_coverage(
            evidence,
            target,
            terminal=evidence.recipe.continuation,
        )
        for target in candidate_targets
    )
    statuses = {_rejection_for_coverage(item) for item in target_coverages}
    status = (
        ArchiveSearchReauthorizationStatus.DENIED
        if ArchiveSearchReauthorizationStatus.DENIED in statuses
        else ArchiveSearchReauthorizationStatus.UNAVAILABLE
        if ArchiveSearchReauthorizationStatus.UNAVAILABLE in statuses
        else ArchiveSearchReauthorizationStatus.DRIFTED
    )
    return ArchiveSearchCandidateReauthorization.rejected(status)


def reauthorize_archive_search_coverage(
    phase: ArchiveSearchAuthorityPhase,
    run_binding: ArchiveSearchRunBinding,
    coverage: SearchCoverage,
    authority_context: object,
    /,
) -> ArchiveSearchCoverageReauthorization:
    """Attest page-shaped coverage only after reproducing its full lane baseline."""

    evidence = _context_entry(authority_context, phase, run_binding)
    if evidence is None or type(coverage) is not SearchCoverage:
        return ArchiveSearchCoverageReauthorization.rejected(ArchiveSearchReauthorizationStatus.UNAVAILABLE)
    target = coverage.corpus, coverage.lane
    target_permission = _target_authority(evidence, target)
    current_fresh = _fresh_coverage(
        evidence,
        target,
        terminal=evidence.recipe.continuation,
    )
    if target_permission is None or current_fresh is None:
        return ArchiveSearchCoverageReauthorization.rejected(ArchiveSearchReauthorizationStatus.UNAVAILABLE)
    if target_permission.capability is None:
        if CoverageState.UNAVAILABLE not in current_fresh.states:
            return ArchiveSearchCoverageReauthorization.rejected(
                ArchiveSearchReauthorizationStatus.UNAVAILABLE
            )
    elif not target_permission.allowed and CoverageState.PERMISSION_FILTERED not in current_fresh.states:
        return ArchiveSearchCoverageReauthorization.rejected(ArchiveSearchReauthorizationStatus.DENIED)
    if _coverage_attested(evidence, coverage, phase=phase):
        if phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION or evidence.recipe.continuation:
            current: SearchCoverage | None = coverage
        else:
            current = _fresh_coverage(
                evidence,
                (coverage.corpus, coverage.lane),
                terminal=False,
            )
        if current is not None:
            return ArchiveSearchCoverageReauthorization.authorized(current)
    return ArchiveSearchCoverageReauthorization.rejected(_rejection_for_coverage(current_fresh))


def prepare_archive_search_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    run_discriminator: str,
    turn_ledger: ArchiveModelBatchLedger,
    current_conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
    exact_file_reader: ArchiveObsidianExactFileReader | None = None,
    dense_query_plan: ArchiveDenseQueryPlan | None = None,
) -> PreparedArchiveSearch:
    """Build and authorize one fresh or resumed archive page in one transaction."""

    try:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise _fail()
        tenant = _identity(tenant_id)
        principal = _identity(principal_id)
        snapshot = _identity(snapshot_discriminator)
        run_id = _identity(run_discriminator)
        if type(request) is not ArchiveSearchRequest:
            raise _fail()
        request_value = ArchiveSearchRequest.parse_private(request.to_private_json())
        storage_request = _storage_request(request_value)
        dense_requested = any(
            corpus in storage_request.corpora
            for corpus in (ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE)
        )
        if not dense_requested or (
            dense_query_plan is not None
            and project_archive_dense_query_plan(
                dense_query_plan,
                principal_id=principal,
                query=storage_request.dense_query,
            )
            is None
        ):
            dense_query_plan = None
        continuation = request_value.continuation is not None
        targets = canonical_archive_search_targets(storage_request)
        current_authority = _fresh_target_authority_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=tenant,
            principal_id=principal,
            targets=targets,
        )
        messages_authorized = any(
            item.corpus is SearchCorpus.CONVERSATION and item.allowed for item in current_authority
        )
        accepted_boundary_identity_sha256 = (
            _accepted_archive_message_boundary_identity_in_transaction(
                conn,
                principal_id=principal,
                conversation_id=_identity(current_conversation_id),
                boundary_user_message_id=_identity(boundary_user_message_id),
            )
            if ArchiveSearchCorpus.MESSAGES in storage_request.corpora and messages_authorized
            else None
        )
        snapshot = _accepted_turn_snapshot(
            snapshot,
            storage_request,
            current_conversation_id=current_conversation_id,
            boundary_user_message_id=boundary_user_message_id,
            accepted_boundary_identity_sha256=accepted_boundary_identity_sha256,
        )
        run = create_archive_search_run_binding(
            tenant_id=tenant,
            principal_id=principal,
            request=request_value,
            requested_targets=targets,
            snapshot_discriminator=snapshot,
            run_discriminator=run_id,
            turn_ledger=turn_ledger,
        )
        if continuation:
            redemption = redeem_archive_search_continuation(
                tenant_id=tenant,
                principal_id=principal,
                run_binding=run,
            )
            target_authority = _continued_authority_projection(
                redemption,
                current_authority,
                targets,
            )
        else:
            redemption = None
            target_authority = current_authority
        recipe = _new_recipe(
            tenant_id=tenant,
            principal_id=principal,
            request=storage_request,
            snapshot_discriminator=snapshot,
            current_conversation_id=current_conversation_id,
            boundary_user_message_id=boundary_user_message_id,
            accepted_boundary_identity_sha256=accepted_boundary_identity_sha256,
            dense_query_plan=dense_query_plan,
            continuation=continuation,
            target_authority=target_authority,
        )
        if continuation:
            fresh = _collect_federated_in_transaction(
                conn,
                recipe=recipe,
                run=run,
                phase=ArchiveSearchAuthorityPhase.BEFORE_MODEL,
                exact_file_reader=exact_file_reader,
            )
            context = _new_context(
                ArchiveSearchAuthorityPhase.BEFORE_MODEL,
                (_FreshEvidence(run, recipe, fresh, recipe.target_authority),),
            )
            if redemption is None:
                raise _fail()
            batch = _authorize_resumed_before_model(
                tenant_id=tenant,
                principal_id=principal,
                run_binding=run,
                redemption=redemption,
                candidate_reauthorizer=reauthorize_archive_search_candidate,
                coverage_reauthorizer=reauthorize_archive_search_coverage,
                authority_context=context,
            )
        else:
            initial = _collect_federated_in_transaction(
                conn,
                recipe=recipe,
                run=run,
                phase=ArchiveSearchAuthorityPhase.BEFORE_MODEL,
                exact_file_reader=exact_file_reader,
            )
            fresh = _collect_federated_in_transaction(
                conn,
                recipe=recipe,
                run=run,
                phase=ArchiveSearchAuthorityPhase.BEFORE_MODEL,
                exact_file_reader=exact_file_reader,
            )
            if not _same_federation(initial, fresh):
                raise _fail()
            context = _new_context(
                ArchiveSearchAuthorityPhase.BEFORE_MODEL,
                (_FreshEvidence(run, recipe, fresh, recipe.target_authority),),
            )
            issue = (
                issue_archive_search_continuation(
                    tenant_id=tenant,
                    principal_id=principal,
                    run_binding=run,
                    tail_candidates=initial.tail_candidates,
                    terminal_coverage=initial.terminal_coverage,
                    warnings=initial.warnings,
                )
                if initial.continuation_available
                else None
            )
            batch = _authorize_before_model(
                tenant_id=tenant,
                principal_id=principal,
                run_binding=run,
                candidates=initial.candidates,
                coverage=initial.coverage,
                warnings=initial.warnings,
                continuation=issue,
                candidate_reauthorizer=reauthorize_archive_search_candidate,
                coverage_reauthorizer=reauthorize_archive_search_coverage,
                authority_context=context,
            )
        if not conn.in_transaction:
            raise _fail()
        return _new_prepared(run, batch, recipe)
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


def refresh_archive_search_reauthorization_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    principal_id: str,
    prepared_searches: tuple[PreparedArchiveSearch, ...],
    exact_file_reader: ArchiveObsidianExactFileReader | None = None,
) -> ArchiveSearchReauthorizationContext:
    """Reproduce every prepared run for the final publication transaction."""

    try:
        if (
            type(conn) is not sqlite3.Connection
            or not conn.in_transaction
            or type(prepared_searches) is not tuple
            or not prepared_searches
        ):
            raise _fail()
        tenant = _identity(tenant_id)
        principal = _identity(principal_id)
        entries: list[_FreshEvidence] = []
        for prepared in prepared_searches:
            if type(prepared) is not PreparedArchiveSearch or not prepared._is_valid():
                raise _fail()
            recipe = prepared._recipe
            if recipe.tenant_id != tenant or recipe.principal_id != principal:
                raise _fail()
            targets = canonical_archive_search_targets(recipe.request)
            current_authority = _fresh_target_authority_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=tenant,
                principal_id=principal,
                targets=targets,
            )
            effective_authority = _narrow_authority_projection(
                recipe.target_authority,
                current_authority,
                targets,
            )
            fresh = _collect_federated_in_transaction(
                conn,
                recipe=recipe,
                run=prepared._run,
                phase=ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION,
                exact_file_reader=exact_file_reader,
                target_authority=effective_authority,
            )
            entries.append(
                _FreshEvidence(
                    prepared._run,
                    recipe,
                    fresh,
                    effective_authority,
                )
            )
        if not conn.in_transaction:
            raise _fail()
        return _new_context(
            ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION,
            tuple(entries),
        )
    except ArchiveSearchServiceError:
        raise
    except Exception:
        raise _fail() from None


__all__ = [
    "MAX_ARCHIVE_EXACT_CHAIN_PAGES",
    "ArchiveSearchReauthorizationContext",
    "ArchiveSearchServiceError",
    "PreparedArchiveSearch",
    "PreparedArchiveSearchComposite",
    "compose_prepared_archive_searches",
    "prepare_archive_search_in_transaction",
    "reauthorize_archive_search_candidate",
    "reauthorize_archive_search_coverage",
    "refresh_archive_search_reauthorization_in_transaction",
]
