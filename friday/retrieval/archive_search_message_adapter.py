"""Typed projection from authorized message rows into archive-search evidence.

The storage selector owns SQL authorization and the exact message ledger.  This
module only converts that already-authorized, process-private snapshot into the
shared retrieval identity contract.  It never queries storage and never turns a
stale or unavailable FTS derivative into factual evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, SupportsIndex, cast

from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPassage,
    ArchiveSearchRequest,
    ConversationScope,
)
from friday.retrieval.catalog_contract import (
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
    IndexIncompleteReason,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalFact,
    TemporalOrigin,
    TemporalRole,
)
from friday.storage._archive_search_messages import (
    ArchiveMessageHit,
    ArchiveMessageSearchPage,
    ArchiveMessageStorageError,
)

MESSAGE_PASSAGE_INDEX_VERSION = "archive-message-window-v1"
_MAX_PROJECTED_PASSAGES = 8
_MAX_EXCERPT_CHARS = 1_900
_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)


class ArchiveMessageAdapterError(ValueError):
    """Body-free failure at the message-to-archive projection boundary."""


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive message projection is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive message projection is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive message projection is process-private")


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
        raise ArchiveMessageAdapterError("archive message projection is invalid") from None


def _digest(domain: bytes, value: object) -> str:
    return hmac.new(
        _PROCESS_KEY,
        domain + b"\0" + _canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def _actor(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ArchiveMessageAdapterError("archive message actor is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageAdapterError("archive message actor is invalid") from None
    if len(encoded) > 200 or any(ord(character) < 32 for character in value):
        raise ArchiveMessageAdapterError("archive message actor is invalid")
    return value


def _snapshot(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ArchiveMessageAdapterError("archive message snapshot is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveMessageAdapterError("archive message snapshot is invalid") from None
    if len(encoded) > 256 or any(ord(character) < 32 for character in value):
        raise ArchiveMessageAdapterError("archive message snapshot is invalid")
    return value


def _message_lifecycle(request: ArchiveSearchRequest) -> tuple[LifecycleState, ...]:
    for constraint in request.lifecycle_constraints:
        if constraint.corpus is ArchiveSearchCorpus.MESSAGES:
            if not set(constraint.states) <= {LifecycleState.ACTIVE, LifecycleState.ARCHIVED}:
                raise ArchiveMessageAdapterError("archive message lifecycle is unavailable")
            return constraint.states
    return (LifecycleState.ACTIVE, LifecycleState.ARCHIVED)


def _message_time_window(request: ArchiveSearchRequest) -> tuple[str | None, str | None]:
    constraints = tuple(
        item for item in request.temporal_constraints if item.corpus is ArchiveSearchCorpus.MESSAGES
    )
    if not constraints:
        return None, None
    if any(item.role is not TemporalRole.CONVERSATION_TIME for item in constraints):
        raise ArchiveMessageAdapterError("archive message temporal role is unavailable")
    since = max(item.start for item in constraints)
    until = min(item.end for item in constraints)
    if since >= until:
        raise ArchiveMessageAdapterError("archive message temporal window is empty")
    return since, until


def archive_message_storage_controls(
    request: ArchiveSearchRequest,
) -> dict[str, object]:
    """Return exact selector controls, or fail closed for unsupported semantics."""

    if type(request) is not ArchiveSearchRequest or ArchiveSearchCorpus.MESSAGES not in request.corpora:
        raise ArchiveMessageAdapterError("archive message request is invalid")
    if request.title_hints or request.filename_hints or request.entity_hints:
        raise ArchiveMessageAdapterError("archive message hint semantics are unavailable")
    since, until = _message_time_window(request)
    roles = request.roles or (MessageRole.ASSISTANT, MessageRole.USER)
    return {
        "context_after": request.context.after,
        "context_before": request.context.before,
        "lifecycle_states": _message_lifecycle(request),
        "roles": roles,
        "scope": request.conversation_scope,
        "since": since,
        "until": until,
    }


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArchiveMessageAdapterError("archive message timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArchiveMessageAdapterError("archive message timestamp is invalid")
    return parsed.astimezone(UTC)


def _excerpt(hit: ArchiveMessageHit) -> str:
    parts: list[str] = []
    for context in hit.context:
        role = "Пользователь" if context.row.role is MessageRole.USER else "Friday"
        content = " ".join(context.row.content.split())
        if content:
            parts.append(f"{role}: {content}")
    text = " | ".join(parts) or "Сообщение без текстового содержимого"
    if len(text) <= _MAX_EXCERPT_CHARS:
        return text
    # Keep both ends of a long window: the matched row is normally near the
    # middle and a head-only slice silently discards the requested neighbours.
    left = (_MAX_EXCERPT_CHARS - 5) // 2
    right = _MAX_EXCERPT_CHARS - 5 - left
    return f"{text[:left].rstrip()} … {text[-right:].lstrip()}"


def _resolved_source(
    *,
    principal_id: str,
    conversation_id: str,
    ledger_sha256: str,
    archived: bool,
) -> tuple[ResolvedSource, SourceRevision]:
    source_ref = SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        principal_id,
        CanonicalObjectKind.CONVERSATION,
        conversation_id,
    )
    representation = SourceRepresentation(RepresentationKind.CONVERSATION, conversation_id)
    revision = SourceRevision(
        representation,
        RevisionKind.MESSAGE_LEDGER_SHA256,
        ledger_sha256,
    )
    resolved = ResolvedSource.create(
        source_ref=source_ref,
        representations=(representation,),
        lifecycle=(
            LifecycleRef(
                representation,
                LifecycleState.ARCHIVED if archived else LifecycleState.ACTIVE,
            ),
        ),
        revisions=(revision,),
        revalidation_targets=(RevalidationTarget(representation, AuthorityScope.PRINCIPAL),),
    )
    return resolved, revision


def _passage(
    hit: ArchiveMessageHit,
    resolved: ResolvedSource,
    revision: SourceRevision,
) -> ArchiveSearchPassage:
    ordered = tuple(sorted(hit.context, key=lambda item: item.relative_position))
    if not ordered:
        raise ArchiveMessageAdapterError("archive message context is unavailable")
    first = ordered[0].row
    last = ordered[-1].row
    start_at = _instant(first.created_at)
    end_at = _instant(last.created_at)
    try:
        end_at += timedelta(microseconds=1)
    except OverflowError:
        raise ArchiveMessageAdapterError("archive message window is unavailable") from None
    if end_at <= start_at:
        end_at = start_at + timedelta(microseconds=1)
    locator = MessageWindowLocator.create(
        first_message_id=first.message_id,
        last_message_id=last.message_id,
        start_at=start_at,
        end_at=end_at,
        context_before=max(0, -min(item.relative_position for item in ordered)),
        context_after=max(0, max(item.relative_position for item in ordered)),
        matched_role=hit.message.role,
    )
    passage_ref = PassageRef.from_resolved_source(
        resolved,
        source_revision=revision,
        locator=locator,
        passage_index_version=MESSAGE_PASSAGE_INDEX_VERSION,
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    return ArchiveSearchPassage(passage_ref, _excerpt(hit))


def _candidate(
    principal_id: str,
    hits: tuple[ArchiveMessageHit, ...],
    *,
    rank: int,
) -> ArchiveSearchCandidate:
    first_hit = hits[0]
    ledger = first_hit.ledger
    if any(
        hit.message.principal_id != principal_id
        or hit.message.conversation_id != first_hit.message.conversation_id
        or hit.ledger.conversation_id != ledger.conversation_id
        or hit.ledger.row_ledger_sha256 != ledger.row_ledger_sha256
        or hit.ledger.conversation_archived is not ledger.conversation_archived
        for hit in hits
    ):
        raise ArchiveMessageAdapterError("archive message source snapshot is inconsistent")
    resolved, revision = _resolved_source(
        principal_id=principal_id,
        conversation_id=ledger.conversation_id,
        ledger_sha256=ledger.row_ledger_sha256,
        archived=ledger.conversation_archived,
    )
    passages: list[ArchiveSearchPassage] = []
    seen_passages: set[str] = set()
    facts: list[TemporalFact] = []
    seen_facts: set[str] = set()
    for hit in hits:
        if len(passages) < _MAX_PROJECTED_PASSAGES:
            passage = _passage(hit, resolved, revision)
            identity = passage.passage_ref.to_private_json()
            if identity not in seen_passages:
                seen_passages.add(identity)
                passages.append(passage)
        fact = TemporalFact.for_instant(
            role=TemporalRole.CONVERSATION_TIME,
            value=_instant(hit.message.created_at),
            origin=TemporalOrigin.STORAGE_COLUMN,
            source_revision=revision,
        )
        identity = fact.to_private_json()
        if identity not in seen_facts:
            seen_facts.add(identity)
            facts.append(fact)
    if not passages:
        raise ArchiveMessageAdapterError("archive message factual passage is unavailable")
    lifecycle = LifecycleState.ARCHIVED if ledger.conversation_archived else LifecycleState.ACTIVE
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.MESSAGES,
        resolved_source=resolved,
        review_state=ArchiveReviewState.NOT_APPLICABLE,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=lifecycle,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.MESSAGE_HISTORY, rank),),
        title=ledger.conversation_title or "Переписка",
        temporal_facts=facts,
        passages=passages,
    )


def _index_state(value: CatalogIndexState) -> CatalogIndexState:
    if type(value) is not CatalogIndexState or value.lane is not CatalogIndexLane.LEXICAL:
        raise ArchiveMessageAdapterError("archive message index attestation is invalid")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveMessageLaneProjection(_ProcessPrivate):
    candidates: tuple[ArchiveSearchCandidate, ...]
    eligible_authorized: int | None
    examined: int
    matched_at_least: int
    backend_capped: bool
    applied_limit: int
    index_state: CatalogIndexState
    _request_identity: str
    _actor_handle: str
    _snapshot_handle: str
    _execution_handle: str
    _selection_handle: str
    _seal: str
    _factory: InitVar[object] = None

    def __post_init__(self, _factory: object) -> None:
        if (
            _factory is not _FACTORY
            or type(self.candidates) is not tuple
            or any(type(item) is not ArchiveSearchCandidate for item in self.candidates)
            or any(item.corpus is not ArchiveSearchCorpus.MESSAGES for item in self.candidates)
            or (
                self.eligible_authorized is not None
                and (type(self.eligible_authorized) is not int or self.eligible_authorized < 0)
            )
            or type(self.examined) is not int
            or type(self.matched_at_least) is not int
            or min(self.examined, self.matched_at_least) < 0
            or self.matched_at_least > self.examined
            or type(self.backend_capped) is not bool
            or type(self.applied_limit) is not int
            or not 1 <= self.applied_limit <= 20
            or type(self.index_state) is not CatalogIndexState
            or type(self._request_identity) is not str
            or type(self._actor_handle) is not str
            or type(self._snapshot_handle) is not str
            or type(self._execution_handle) is not str
            or type(self._selection_handle) is not str
            or type(self._seal) is not str
        ):
            raise ArchiveMessageAdapterError("archive message projection is invalid")

    def __repr__(self) -> str:
        return (
            "ArchiveMessageLaneProjection("
            f"candidate_count={len(self.candidates)}, current="
            f"{self.index_state.status is CatalogIndexStatus.CURRENT}, private=True)"
        )

    def _material(self) -> dict[str, object]:
        return {
            "actor_handle": self._actor_handle,
            "applied_limit": self.applied_limit,
            "backend_capped": self.backend_capped,
            "candidates": [item.to_private_payload() for item in self.candidates],
            "eligible_authorized": self.eligible_authorized,
            "examined": self.examined,
            "execution_handle": self._execution_handle,
            "index_state": self.index_state.to_private_payload(),
            "matched_at_least": self.matched_at_least,
            "request_identity": self._request_identity,
            "selection_handle": self._selection_handle,
            "snapshot_handle": self._snapshot_handle,
        }

    def is_valid(self) -> bool:
        try:
            return hmac.compare_digest(
                self._seal,
                _digest(b"friday/archive-message-projection/v1", self._material()),
            )
        except Exception:
            return False

    def same_evidence_as(self, other: object) -> bool:
        return bool(
            type(other) is ArchiveMessageLaneProjection
            and self.is_valid()
            and cast(ArchiveMessageLaneProjection, other).is_valid()
            and hmac.compare_digest(self._seal, cast(ArchiveMessageLaneProjection, other)._seal)
        )

    def to_coverage(
        self,
        execution_binding: SearchExecutionBinding,
        *,
        tenant_id: str,
        principal_id: str,
        snapshot_discriminator: str,
        returned: int,
    ) -> SearchCoverage:
        tenant = _actor(tenant_id)
        principal = _actor(principal_id)
        snapshot = _snapshot(snapshot_discriminator)
        actor_handle = _digest(
            b"friday/archive-message-projection-actor/v1",
            {"principal_id": principal, "tenant_id": tenant},
        )
        snapshot_handle = _digest(
            b"friday/archive-message-projection-snapshot/v1",
            {"snapshot_discriminator": snapshot},
        )
        if (
            not self.is_valid()
            or type(execution_binding) is not SearchExecutionBinding
            or not execution_binding.attests_private_request(self._request_identity)
            or not execution_binding.attests_authority(
                authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                tenant_id=tenant,
                principal_id=principal,
            )
            or not execution_binding.attests_snapshot(snapshot)
            or (SearchCorpus.CONVERSATION, SearchLane.MESSAGE_HISTORY)
            not in execution_binding.requested_targets
            or not hmac.compare_digest(self._execution_handle, execution_binding.opaque_handle)
            or not hmac.compare_digest(self._actor_handle, actor_handle)
            or not hmac.compare_digest(self._snapshot_handle, snapshot_handle)
            or type(returned) is not int
            or not 0 <= returned <= min(self.matched_at_least, len(self.candidates))
        ):
            raise ArchiveMessageAdapterError("archive message coverage proof is invalid")
        status = self.index_state.status
        if status is CatalogIndexStatus.CURRENT:
            states: tuple[CoverageState, ...]
            if self.backend_capped:
                states = (CoverageState.CAPPED, CoverageState.PARTIAL)
            else:
                states = (CoverageState.COMPLETE,)
            return SearchCoverage.create(
                corpus=SearchCorpus.CONVERSATION,
                lane=SearchLane.MESSAGE_HISTORY,
                execution_binding=execution_binding,
                states=states,
                eligible_authorized=self.eligible_authorized,
                examined=self.examined,
                matched_at_least=self.matched_at_least,
                returned=returned,
                limit=self.applied_limit if CoverageState.CAPPED in states else None,
                next_cursor_available=False,
                authority_rechecked=True,
                snapshot_current=True,
            )
        reason_by_incomplete: dict[IndexIncompleteReason | None, CoverageState] = {
            IndexIncompleteReason.BACKFILL_PENDING: CoverageState.BACKFILL_PENDING,
            IndexIncompleteReason.SOURCE_CHANGED: CoverageState.STALE,
            IndexIncompleteReason.EMBEDDING_INCOMPATIBLE: (CoverageState.EMBEDDING_INCOMPATIBLE),
        }
        reason = reason_by_incomplete.get(
            self.index_state.incomplete_reason,
            CoverageState.UNAVAILABLE,
        )
        return SearchCoverage.create(
            corpus=SearchCorpus.CONVERSATION,
            lane=SearchLane.MESSAGE_HISTORY,
            execution_binding=execution_binding,
            states=(CoverageState.PARTIAL, reason),
            eligible_authorized=None,
            examined=0,
            matched_at_least=0,
            returned=0,
            authority_rechecked=True,
            snapshot_current=False,
        )


def project_archive_message_page(
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    page: ArchiveMessageSearchPage,
    index_state: CatalogIndexState,
    execution_binding: SearchExecutionBinding,
    snapshot_discriminator: str,
    current_conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
) -> ArchiveMessageLaneProjection:
    """Convert one exact storage page without widening its authority scope."""

    try:
        tenant = _actor(tenant_id)
        principal = _actor(principal_id)
        snapshot = _snapshot(snapshot_discriminator)
        if (
            type(request) is not ArchiveSearchRequest
            or type(page) is not ArchiveMessageSearchPage
            or not page.is_valid()
            or type(execution_binding) is not SearchExecutionBinding
        ):
            raise ArchiveMessageAdapterError("archive message projection input is invalid")
        request_identity = request.to_identity_json()
        if (
            not execution_binding.attests_private_request(request_identity)
            or not execution_binding.attests_authority(
                authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                tenant_id=tenant,
                principal_id=principal,
            )
            or not execution_binding.attests_snapshot(snapshot)
            or (SearchCorpus.CONVERSATION, SearchLane.MESSAGE_HISTORY)
            not in execution_binding.requested_targets
        ):
            raise ArchiveMessageAdapterError("archive message execution binding is invalid")
        controls = archive_message_storage_controls(request)
        if (
            page.principal_id != principal
            or page.query != request.query
            or page.scope is not controls["scope"]
            or page.roles != controls["roles"]
            or page.lifecycle_states != controls["lifecycle_states"]
            or page.since != controls["since"]
            or page.until != controls["until"]
            or page.limit < request.limit
            or page.context_before != request.context.before
            or page.context_after != request.context.after
        ):
            raise ArchiveMessageAdapterError("archive message selection controls drifted")
        current = _actor(current_conversation_id)
        boundary = _actor(boundary_user_message_id)
        if (
            page.conversation_id != current
            or page.boundary_user_message_id != boundary
            or page.boundary_identity_sha256 is None
            or (
                request.conversation_scope is ConversationScope.CURRENT
                and any(hit.message.conversation_id != current for hit in page.hits)
            )
        ):
            raise ArchiveMessageAdapterError("archive accepted-turn boundary drifted")
        if any(
            hit.message.principal_id != principal
            or any(
                context.row.principal_id != principal
                or context.relative_position < -request.context.before
                or context.relative_position > request.context.after
                for context in hit.context
            )
            for hit in page.hits
        ):
            raise ArchiveMessageAdapterError("archive message authority scope drifted")
        derivative = _index_state(index_state)
        candidates: tuple[ArchiveSearchCandidate, ...] = ()
        eligible: int | None = None
        examined = 0
        matched = 0
        backend_capped = False
        if derivative.status is CatalogIndexStatus.CURRENT:
            grouped: dict[str, list[ArchiveMessageHit]] = defaultdict(list)
            for hit in page.hits:
                grouped[hit.message.conversation_id].append(hit)
            ordered = sorted(
                grouped.values(),
                key=lambda values: (
                    min(item.match_rank for item in values),
                    values[0].message.conversation_id,
                ),
            )
            all_candidates = tuple(
                _candidate(
                    principal,
                    tuple(sorted(values, key=lambda item: item.match_rank)),
                    rank=rank,
                )
                for rank, values in enumerate(ordered, 1)
            )
            candidates = all_candidates[: page.limit]
            eligible = page.examined
            examined = page.examined
            # Coverage ranks and returned values count stable conversation
            # candidates, not the individual matching rows grouped into them.
            # Otherwise two hits in one conversation would claim an invisible
            # second result and make a COMPLETE lane falsely non-exhaustive.
            matched = len(all_candidates)
            backend_capped = page.has_more or len(all_candidates) > len(candidates)
        actor_handle = _digest(
            b"friday/archive-message-projection-actor/v1",
            {"principal_id": principal, "tenant_id": tenant},
        )
        snapshot_handle = _digest(
            b"friday/archive-message-projection-snapshot/v1",
            {"snapshot_discriminator": snapshot},
        )
        projection = ArchiveMessageLaneProjection(
            candidates,
            eligible,
            examined,
            matched,
            backend_capped,
            page.limit,
            derivative,
            request_identity,
            actor_handle,
            snapshot_handle,
            execution_binding.opaque_handle,
            page.selection_handle,
            "0" * 64,
            _factory=_FACTORY,
        )
        object.__setattr__(
            projection,
            "_seal",
            _digest(b"friday/archive-message-projection/v1", projection._material()),
        )
        return projection
    except (ArchiveMessageAdapterError, ArchiveMessageStorageError):
        raise ArchiveMessageAdapterError("archive message projection failed") from None
    except Exception:
        raise ArchiveMessageAdapterError("archive message projection failed") from None


__all__ = [
    "ArchiveMessageAdapterError",
    "ArchiveMessageLaneProjection",
    "MESSAGE_PASSAGE_INDEX_VERSION",
    "archive_message_storage_controls",
    "project_archive_message_page",
]
