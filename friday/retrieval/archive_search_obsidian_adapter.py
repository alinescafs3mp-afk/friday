"""Exact projection of authorized Obsidian lane pages into archive evidence.

The storage layer owns selection, owner authorization and exact-file reads.  This
adapter only consumes its process-private, sealed values inside the caller's
SQLite transaction and converts them to the shared archive-search contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields, is_dataclass
from pathlib import PurePosixPath
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
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
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
    TextSpanLocator,
)
from friday.storage._archive_search_obsidian import (
    ArchiveObsidianCoverage,
    ArchiveObsidianExactFileReader,
    ArchiveObsidianHit,
    ArchiveObsidianIndexState,
    ArchiveObsidianLanePage,
    ArchiveObsidianMatchKind,
    ArchiveObsidianReadPhase,
    ArchiveObsidianStorageError,
    ArchiveObsidianUnavailableReason,
    verify_archive_obsidian_factual_hit_in_transaction,
    verify_archive_obsidian_navigation_hit_in_transaction,
)

OBSIDIAN_PASSAGE_INDEX_VERSION = "archive-obsidian-char-v1"
_MAX_DISPLAY_CHARS = 260
_MAX_EXCERPT_CHARS = 720
_PROCESS_KEY = secrets.token_bytes(32)
_PROCESS_AUTHORITY = object()
_SUPPORTED_LANES = frozenset(
    {
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
    }
)
_CHANNEL = {
    SearchLane.CATALOG: ArchiveMatchChannel.CATALOG,
    SearchLane.EXACT_IDENTITY: ArchiveMatchChannel.EXACT_IDENTITY,
    SearchLane.LEXICAL: ArchiveMatchChannel.LEXICAL,
    SearchLane.APPROXIMATE_IDENTITY: ArchiveMatchChannel.APPROXIMATE_IDENTITY,
}


class ArchiveObsidianAdapterError(ValueError):
    """Body-free rejection at the Obsidian archive projection boundary."""


def _fail() -> ArchiveObsidianAdapterError:
    return ArchiveObsidianAdapterError("archive Obsidian projection failed")


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive Obsidian projection is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive Obsidian projection is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive Obsidian projection is process-private")


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


def _identity(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail() from None
    if len(encoded) > 200 or any(unicodedata.category(char).startswith("C") for char in value):
        raise _fail()
    return value


def _snapshot(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail()
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail() from None
    if len(encoded) > 256 or any(unicodedata.category(char).startswith("C") for char in value):
        raise _fail()
    return value


def _actor_handle(tenant_id: str, principal_id: str) -> bytes:
    return _mac(
        b"friday/archive-obsidian-adapter-actor/v1",
        {"principal_id": principal_id, "tenant_id": tenant_id},
    )


def _request_handle(request: ArchiveSearchRequest) -> bytes:
    return _mac(
        b"friday/archive-obsidian-adapter-request/v1",
        request.to_identity_json(),
    )


def _snapshot_handle(snapshot_discriminator: str) -> bytes:
    return _mac(
        b"friday/archive-obsidian-adapter-snapshot/v1",
        snapshot_discriminator,
    )


def _freeze_request(value: object) -> ArchiveSearchRequest:
    if type(value) is not ArchiveSearchRequest:
        raise _fail()
    try:
        encoded = cast(ArchiveSearchRequest, value).to_private_json()
        frozen = ArchiveSearchRequest.parse_private(encoded)
        if frozen.to_private_json() != encoded:
            raise _fail()
        return frozen
    except ArchiveObsidianAdapterError:
        raise
    except Exception:
        raise _fail() from None


def _freeze_candidate(value: object) -> ArchiveSearchCandidate:
    if type(value) is not ArchiveSearchCandidate:
        raise _fail()
    try:
        encoded = cast(ArchiveSearchCandidate, value).to_private_json()
        frozen = ArchiveSearchCandidate.parse_private(encoded)
        if frozen.to_private_json() != encoded:
            raise _fail()
        return frozen
    except ArchiveObsidianAdapterError:
        raise
    except Exception:
        raise _fail() from None


def _freeze_coverage(
    value: object,
    execution_binding: SearchExecutionBinding,
) -> SearchCoverage:
    if type(value) is not SearchCoverage:
        raise _fail()
    item = cast(SearchCoverage, value)
    try:
        if item.execution_binding is not execution_binding:
            raise _fail()
        encoded = item.to_json()
        parsed = SearchCoverage.parse(encoded)
        frozen = SearchCoverage.create(
            corpus=parsed.corpus,
            lane=parsed.lane,
            execution_binding=execution_binding,
            states=parsed.states,
            eligible_authorized=parsed.eligible_authorized,
            examined=parsed.examined,
            matched_at_least=parsed.matched_at_least,
            returned=parsed.returned,
            authority_rechecked=parsed.authority_rechecked,
            snapshot_current=parsed.snapshot_current,
            limit=parsed.limit,
            next_cursor_available=parsed.next_cursor_available,
        )
        if frozen.to_json() != encoded:
            raise _fail()
        return frozen
    except ArchiveObsidianAdapterError:
        raise
    except Exception:
        raise _fail() from None


def _same_exact_graph(left: object, right: object) -> bool:
    """Compare canonical values without accepting nested duck-typed substitutes."""

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
            _same_exact_graph(getattr(left, field.name), getattr(right, field.name))
            for field in fields(left)
        )
    try:
        return bool(left == right)
    except Exception:
        return False


def _hit_uses_exact_primitives(hit: object) -> bool:
    if type(hit) is not ArchiveObsidianHit:
        return False
    value = cast(ArchiveObsidianHit, hit)
    try:
        return bool(
            all(
                type(item) is str
                for item in (
                    value.binding_id,
                    value.vault_id,
                    value.path,
                    value.title,
                    value.current_revision,
                    value._execution_handle,
                )
            )
            and type(value.aliases) is tuple
            and all(type(item) is str for item in value.aliases)
            and type(value.lifecycle) is LifecycleState
            and type(value.index_state) is ArchiveObsidianIndexState
            and type(value.metadata_coverage) is ArchiveObsidianCoverage
            and type(value.body_coverage) is ArchiveObsidianCoverage
            and type(value.lane) is SearchLane
            and type(value.match_kind) is ArchiveObsidianMatchKind
            and type(value.rank) is int
            and type(value.factual) is bool
            and type(value.index_revision_current) is bool
            and type(value.index_path_current) is bool
            and (value._indexed_body is None or type(value._indexed_body) is str)
            and all(
                type(item) is bytes
                for item in (
                    value._principal_handle,
                    value._request_handle,
                    value._seal,
                    value._snapshot_handle,
                    value._tenant_handle,
                )
            )
        )
    except Exception:
        return False


def _page_uses_exact_primitives(page: object) -> bool:
    if type(page) is not ArchiveObsidianLanePage:
        return False
    value = cast(ArchiveObsidianLanePage, page)
    try:
        return bool(
            type(value.lane) is SearchLane
            and type(value.hits) is tuple
            and all(_hit_uses_exact_primitives(hit) for hit in value.hits)
            and (value.eligible_authorized is None or type(value.eligible_authorized) is int)
            and all(
                type(item) is int
                for item in (
                    value.examined,
                    value.matched,
                    value.returned,
                    value.limit,
                    value.stale,
                    value.backfill_pending,
                )
            )
            and type(value.capped) is bool
            and type(value.matched_exact) is bool
            and (
                value.unavailable_reason is None
                or type(value.unavailable_reason) is ArchiveObsidianUnavailableReason
            )
            and type(value._execution_handle) is str
            and all(
                type(item) is bytes
                for item in (
                    value._principal_handle,
                    value._request_handle,
                    value._seal,
                    value._snapshot_handle,
                    value._tenant_handle,
                )
            )
        )
    except Exception:
        return False


def _display(value: str, *, fallback: str) -> str | None:
    candidate = value.strip()
    if not candidate or any(unicodedata.category(char).startswith("C") for char in candidate):
        candidate = fallback.strip()
    candidate = candidate[:_MAX_DISPLAY_CHARS].rstrip()
    if not candidate or any(unicodedata.category(char).startswith("C") for char in candidate):
        return None
    try:
        candidate.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    return candidate


def _source(
    *,
    principal_id: str,
    binding_id: str,
    current_revision: str,
    lifecycle: LifecycleState,
) -> tuple[ResolvedSource, SourceRevision]:
    source_ref = SourceRef(
        source_kind=SourceKind.OBSIDIAN_NOTE,
        authority_scope=AuthorityScope.PRINCIPAL,
        tenant_id=None,
        principal_id=principal_id,
        canonical_object_kind=CanonicalObjectKind.OBSIDIAN_BINDING,
        canonical_object_id=binding_id,
    )
    representation = SourceRepresentation(RepresentationKind.OBSIDIAN_BINDING, binding_id)
    revision = SourceRevision(
        representation,
        RevisionKind.OBSIDIAN_REVISION_SHA256,
        current_revision,
    )
    resolved = ResolvedSource.create(
        source_ref=source_ref,
        representations=(representation,),
        lifecycle=(LifecycleRef(representation, lifecycle),),
        revisions=(revision,),
        revalidation_targets=(RevalidationTarget(representation, AuthorityScope.PRINCIPAL),),
    )
    return resolved, revision


def _candidate_display(path: str, title: str) -> tuple[str | None, str | None]:
    pure = PurePosixPath(path)
    filename = _display(pure.name, fallback=pure.name)
    candidate_title = _display(title, fallback=pure.stem)
    return candidate_title, filename


def _navigation_candidate(
    *,
    principal_id: str,
    binding_id: str,
    vault_id: str,
    path: str,
    title: str,
    aliases: tuple[str, ...],
    current_revision: str,
    lifecycle: LifecycleState,
    index_state: ArchiveObsidianIndexState,
    index_revision_current: bool,
    index_path_current: bool,
    metadata_coverage: ArchiveObsidianCoverage,
    body_coverage: ArchiveObsidianCoverage,
    lane: SearchLane,
    match_kind: ArchiveObsidianMatchKind,
    rank: int,
) -> ArchiveSearchCandidate:
    if lane not in {
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.APPROXIMATE_IDENTITY,
    }:
        raise _fail()
    if lane is SearchLane.EXACT_IDENTITY and match_kind is not ArchiveObsidianMatchKind.EXACT:
        raise _fail()
    if lane is SearchLane.APPROXIMATE_IDENTITY and match_kind not in {
        ArchiveObsidianMatchKind.TYPO,
        ArchiveObsidianMatchKind.KEYBOARD_LAYOUT,
    }:
        raise _fail()
    # These fields are part of the storage carrier seal even though the shared
    # archive candidate does not expose them directly.
    _ = (
        vault_id,
        aliases,
        index_state,
        index_revision_current,
        index_path_current,
        metadata_coverage,
        body_coverage,
    )
    resolved, _revision = _source(
        principal_id=principal_id,
        binding_id=binding_id,
        current_revision=current_revision,
        lifecycle=lifecycle,
    )
    candidate_title, filename = _candidate_display(path, title)
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.OBSIDIAN,
        resolved_source=resolved,
        title=candidate_title,
        filename=filename,
        review_state=ArchiveReviewState.NOT_APPLICABLE,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=lifecycle,
        matches=(ArchiveMatchRank(_CHANNEL[lane], rank),),
    )


def _fold(value: str) -> str:
    folded = unicodedata.normalize("NFC", value).casefold().replace("ё", "е")
    # NFD keeps canonical composition from crossing codepoint segment
    # boundaries (notably Hangul Jamo), so folded offsets can be mapped back to
    # the exact original Python string without changing the excerpt text.
    return unicodedata.normalize("NFD", folded)


def _fold_segments(value: str) -> Iterator[tuple[str, int, int]]:
    start = 0
    for index in range(1, len(value) + 1):
        if index < len(value) and unicodedata.combining(value[index]) != 0:
            continue
        yield _fold(value[start:index]), start, index
        start = index


def _original_span(
    value: str,
    *,
    folded_value: str,
    folded_start: int,
    folded_end: int,
) -> tuple[int, int] | None:
    cursor = 0
    original_start: int | None = None
    original_end: int | None = None
    for folded, start, end in _fold_segments(value):
        next_cursor = cursor + len(folded)
        if folded_value[cursor:next_cursor] != folded:
            return None
        if original_start is None and cursor <= folded_start < next_cursor:
            original_start = start
        if original_end is None and cursor < folded_end <= next_cursor:
            original_end = end
        cursor = next_cursor
    if (
        cursor != len(folded_value)
        or original_start is None
        or original_end is None
        or original_end <= original_start
    ):
        return None
    return original_start, original_end


def _exact_span(body: str, query: str) -> tuple[int, int] | None:
    folded_body = _fold(body)
    raw_terms = (
        query,
        *(term for term in re.findall(r"[^\W_]+", query, flags=re.UNICODE) if len(term) >= 2),
    )
    needles = tuple(dict.fromkeys(_fold(term) for term in raw_terms if term))
    for needle in needles:
        folded_start = folded_body.find(needle)
        if not needle or folded_start < 0:
            continue
        span = _original_span(
            body,
            folded_value=folded_body,
            folded_start=folded_start,
            folded_end=folded_start + len(needle),
        )
        if span is not None:
            return span
    return None


def _excerpt(body: str, query: str) -> tuple[str, int, int]:
    match = _exact_span(body, query)
    if match is None:
        raise _fail()
    match_start, match_end = match
    start = max(0, match_start - _MAX_EXCERPT_CHARS // 2)
    end = min(len(body), start + _MAX_EXCERPT_CHARS)
    start = max(0, end - _MAX_EXCERPT_CHARS)
    for index in range(start, match_start):
        if unicodedata.category(body[index]).startswith("C"):
            start = index + 1
    for index in range(match_end, end):
        if unicodedata.category(body[index]).startswith("C"):
            end = index
            break
    while start < match_start and body[start].isspace():
        start += 1
    while end > match_end and body[end - 1].isspace():
        end -= 1
    excerpt = body[start:end]
    if (
        not excerpt
        or excerpt != excerpt.strip()
        or len(excerpt) > _MAX_EXCERPT_CHARS
        or any(unicodedata.category(char).startswith("C") for char in excerpt)
    ):
        start, end = match_start, match_end
        excerpt = body[start:end]
    if (
        not excerpt
        or excerpt != excerpt.strip()
        or any(unicodedata.category(char).startswith("C") for char in excerpt)
    ):
        raise _fail()
    return excerpt, start, end


def _factual_candidate(
    *,
    principal_id: str,
    binding_id: str,
    path: str,
    title: str,
    current_revision: str,
    lifecycle: LifecycleState,
    lane: SearchLane,
    match_kind: ArchiveObsidianMatchKind,
    rank: int,
    query: str,
    body: str,
) -> ArchiveSearchCandidate:
    if (
        lane is not SearchLane.LEXICAL
        or lifecycle is not LifecycleState.ACTIVE
        or match_kind
        not in {
            ArchiveObsidianMatchKind.LEXICAL_PHRASE,
            ArchiveObsidianMatchKind.LEXICAL_TERMS,
        }
    ):
        raise _fail()
    excerpt, start, end = _excerpt(body, query)
    resolved, revision = _source(
        principal_id=principal_id,
        binding_id=binding_id,
        current_revision=current_revision,
        lifecycle=lifecycle,
    )
    passage_ref = PassageRef.from_resolved_source(
        resolved,
        source_revision=revision,
        locator=TextSpanLocator(chunk_index=0, start_char=start, end_char=end),
        passage_index_version=OBSIDIAN_PASSAGE_INDEX_VERSION,
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    candidate_title, filename = _candidate_display(path, title)
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.OBSIDIAN,
        resolved_source=resolved,
        title=candidate_title,
        filename=filename,
        review_state=ArchiveReviewState.NOT_APPLICABLE,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=lifecycle,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, rank),),
        passages=(ArchiveSearchPassage(passage_ref, excerpt),),
    )


@dataclass(frozen=True, slots=True)
class _CapturedNavigationHit:
    aliases: tuple[str, ...]
    binding_id: str
    body_coverage: ArchiveObsidianCoverage
    current_revision: str
    index_path_current: bool
    index_revision_current: bool
    index_state: ArchiveObsidianIndexState
    lane: SearchLane
    lifecycle: LifecycleState
    match_kind: ArchiveObsidianMatchKind
    metadata_coverage: ArchiveObsidianCoverage
    path: str
    rank: int
    title: str
    vault_id: str

    @classmethod
    def from_hit(cls, hit: ArchiveObsidianHit) -> _CapturedNavigationHit:
        return cls(
            aliases=tuple(hit.aliases),
            binding_id=hit.binding_id,
            body_coverage=hit.body_coverage,
            current_revision=hit.current_revision,
            index_path_current=hit.index_path_current,
            index_revision_current=hit.index_revision_current,
            index_state=hit.index_state,
            lane=hit.lane,
            lifecycle=hit.lifecycle,
            match_kind=hit.match_kind,
            metadata_coverage=hit.metadata_coverage,
            path=hit.path,
            rank=hit.rank,
            title=hit.title,
            vault_id=hit.vault_id,
        )


def _navigation_consumer(
    captured: _CapturedNavigationHit,
    *,
    principal_id: str,
) -> Callable[..., ArchiveSearchCandidate]:
    def consume(**_verified_values: object) -> ArchiveSearchCandidate:
        return _navigation_candidate(
            principal_id=principal_id,
            binding_id=captured.binding_id,
            vault_id=captured.vault_id,
            path=captured.path,
            title=captured.title,
            aliases=captured.aliases,
            current_revision=captured.current_revision,
            lifecycle=captured.lifecycle,
            index_state=captured.index_state,
            index_revision_current=captured.index_revision_current,
            index_path_current=captured.index_path_current,
            metadata_coverage=captured.metadata_coverage,
            body_coverage=captured.body_coverage,
            lane=captured.lane,
            match_kind=captured.match_kind,
            rank=captured.rank,
        )

    return consume


@dataclass(frozen=True, slots=True)
class _CapturedFactualHit:
    binding_id: str
    current_revision: str
    lane: SearchLane
    lifecycle: LifecycleState
    match_kind: ArchiveObsidianMatchKind
    path: str
    rank: int
    title: str

    @classmethod
    def from_hit(cls, hit: ArchiveObsidianHit) -> _CapturedFactualHit:
        return cls(
            binding_id=hit.binding_id,
            current_revision=hit.current_revision,
            lane=hit.lane,
            lifecycle=hit.lifecycle,
            match_kind=hit.match_kind,
            path=hit.path,
            rank=hit.rank,
            title=hit.title,
        )


def _factual_consumer(
    captured: _CapturedFactualHit,
    *,
    principal_id: str,
    query: str,
) -> Callable[[str], ArchiveSearchCandidate]:
    def consume(body: str, /) -> ArchiveSearchCandidate:
        return _factual_candidate(
            principal_id=principal_id,
            binding_id=captured.binding_id,
            path=captured.path,
            title=captured.title,
            current_revision=captured.current_revision,
            lifecycle=captured.lifecycle,
            lane=captured.lane,
            match_kind=captured.match_kind,
            rank=captured.rank,
            query=query,
            body=body,
        )

    return consume


class ArchiveObsidianLaneProjection(_ProcessPrivate):
    """Sealed exact candidates and coverage for one verified storage page."""

    __slots__ = (
        "_actor_handle",
        "_candidates",
        "_coverage",
        "_execution_binding",
        "_execution_handle",
        "_lane",
        "_phase",
        "_process_authority",
        "_request_handle",
        "_seal",
        "_snapshot_handle",
    )

    _actor_handle: bytes
    _candidates: tuple[ArchiveSearchCandidate, ...]
    _coverage: SearchCoverage
    _execution_binding: SearchExecutionBinding
    _execution_handle: str
    _lane: SearchLane
    _phase: ArchiveObsidianReadPhase
    _process_authority: object
    _request_handle: bytes
    _seal: bytes
    _snapshot_handle: bytes

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive Obsidian projection is immutable")

    def __repr__(self) -> str:
        try:
            return (
                "<ArchiveObsidianLaneProjection "
                f"lane={self._lane.value!r} candidates={len(self._candidates)} private=True>"
            )
        except Exception:
            return "<ArchiveObsidianLaneProjection invalid private=True>"

    def _material(self) -> dict[str, object]:
        return {
            "actor_handle": self._actor_handle.hex(),
            "candidates": [item.to_private_payload() for item in self._candidates],
            "coverage": self._coverage.to_payload(),
            "execution_handle": self._execution_handle,
            "lane": self._lane.value,
            "phase": self._phase.value,
            "request_handle": self._request_handle.hex(),
            "snapshot_handle": self._snapshot_handle.hex(),
        }

    def is_valid(self) -> bool:
        try:
            frozen_candidates = tuple(_freeze_candidate(item) for item in self._candidates)
            frozen_coverage = _freeze_coverage(self._coverage, self._execution_binding)
            ranks = tuple(item.matches[0].rank for item in frozen_candidates)
            return bool(
                type(self) is ArchiveObsidianLaneProjection
                and self._process_authority is _PROCESS_AUTHORITY
                and type(self._execution_binding) is SearchExecutionBinding
                and self._execution_binding.is_live_private_request_binding
                and type(self._lane) is SearchLane
                and self._lane in _SUPPORTED_LANES
                and type(self._phase) is ArchiveObsidianReadPhase
                and type(self._candidates) is tuple
                and all(
                    type(item) is ArchiveSearchCandidate
                    and item.corpus is ArchiveSearchCorpus.OBSIDIAN
                    and item.match_channels == (_CHANNEL[self._lane],)
                    for item in self._candidates
                )
                and len(frozen_candidates) == len(self._candidates)
                and all(
                    _same_exact_graph(item, frozen)
                    for item, frozen in zip(
                        self._candidates,
                        frozen_candidates,
                        strict=True,
                    )
                )
                and type(self._coverage) is SearchCoverage
                and self._coverage.execution_binding is self._execution_binding
                and self._coverage.corpus is SearchCorpus.OBSIDIAN
                and self._coverage.lane is self._lane
                and self._coverage.returned == len(self._candidates)
                and _same_exact_graph(self._coverage, frozen_coverage)
                and ranks == tuple(range(1, len(frozen_candidates) + 1))
                and (not ranks or ranks[-1] <= frozen_coverage.matched_at_least)
                and (
                    frozen_coverage.matched_at_least == frozen_coverage.returned
                    or CoverageState.CAPPED in frozen_coverage.states
                )
                and type(self._execution_handle) is str
                and hmac.compare_digest(
                    self._execution_handle,
                    self._execution_binding.opaque_handle,
                )
                and all(
                    type(item) is bytes and len(item) == 32
                    for item in (
                        self._actor_handle,
                        self._request_handle,
                        self._snapshot_handle,
                        self._seal,
                    )
                )
                and hmac.compare_digest(
                    self._seal,
                    _mac(
                        b"friday/archive-obsidian-adapter-projection/v1",
                        self._material(),
                    ),
                )
            )
        except Exception:
            return False

    @property
    def candidates(self) -> tuple[ArchiveSearchCandidate, ...]:
        if not self.is_valid():
            raise _fail()
        return self._candidates

    @property
    def lane(self) -> SearchLane:
        if not self.is_valid():
            raise _fail()
        return self._lane

    @property
    def phase(self) -> ArchiveObsidianReadPhase:
        if not self.is_valid():
            raise _fail()
        return self._phase

    def same_evidence_as(self, other: object) -> bool:
        return bool(
            type(other) is ArchiveObsidianLaneProjection
            and self.is_valid()
            and cast(ArchiveObsidianLaneProjection, other).is_valid()
            and hmac.compare_digest(
                self._seal,
                cast(ArchiveObsidianLaneProjection, other)._seal,
            )
        )

    def to_coverage(
        self,
        *,
        execution_binding: SearchExecutionBinding,
        tenant_id: str,
        principal_id: str,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
        phase: ArchiveObsidianReadPhase,
    ) -> SearchCoverage:
        try:
            tenant = _identity(tenant_id)
            principal = _identity(principal_id)
            snapshot = _snapshot(snapshot_discriminator)
            request_copy = _freeze_request(request)
            if not _same_exact_graph(request, request_copy):
                raise _fail()
            if (
                not self.is_valid()
                or type(execution_binding) is not SearchExecutionBinding
                or execution_binding is not self._execution_binding
                or type(phase) is not ArchiveObsidianReadPhase
                or phase is not self._phase
                or not execution_binding.attests_private_request(request_copy.to_identity_json())
                or not execution_binding.attests_authority(
                    authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                    tenant_id=tenant,
                    principal_id=principal,
                )
                or not execution_binding.attests_snapshot(snapshot)
                or not hmac.compare_digest(
                    self._actor_handle,
                    _actor_handle(tenant, principal),
                )
                or not hmac.compare_digest(
                    self._request_handle,
                    _request_handle(request_copy),
                )
                or not hmac.compare_digest(
                    self._snapshot_handle,
                    _snapshot_handle(snapshot),
                )
            ):
                raise _fail()
            return self._coverage
        except ArchiveObsidianAdapterError:
            raise
        except Exception:
            raise _fail() from None


def _new_projection(
    *,
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: SearchCoverage,
    lane: SearchLane,
    phase: ArchiveObsidianReadPhase,
    execution_binding: SearchExecutionBinding,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
) -> ArchiveObsidianLaneProjection:
    frozen_candidates = tuple(_freeze_candidate(item) for item in candidates)
    frozen_coverage = _freeze_coverage(coverage, execution_binding)
    if (
        not _same_exact_graph(candidates, frozen_candidates)
        or not _same_exact_graph(coverage, frozen_coverage)
    ):
        raise _fail()
    projection = cast(
        ArchiveObsidianLaneProjection,
        object.__new__(ArchiveObsidianLaneProjection),
    )
    for name, value in (
        ("_actor_handle", _actor_handle(tenant_id, principal_id)),
        ("_candidates", frozen_candidates),
        ("_coverage", frozen_coverage),
        ("_execution_binding", execution_binding),
        ("_execution_handle", execution_binding.opaque_handle),
        ("_lane", lane),
        ("_phase", phase),
        ("_process_authority", _PROCESS_AUTHORITY),
        ("_request_handle", _request_handle(request)),
        ("_seal", b"0" * 32),
        ("_snapshot_handle", _snapshot_handle(snapshot_discriminator)),
    ):
        object.__setattr__(projection, name, value)
    object.__setattr__(
        projection,
        "_seal",
        _mac(
            b"friday/archive-obsidian-adapter-projection/v1",
            projection._material(),
        ),
    )
    if not projection.is_valid():
        raise _fail()
    return projection


def project_archive_obsidian_lane_page_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    snapshot_discriminator: str,
    execution_binding: SearchExecutionBinding,
    page: ArchiveObsidianLanePage,
    phase: ArchiveObsidianReadPhase,
    exact_file_reader: ArchiveObsidianExactFileReader | None = None,
) -> ArchiveObsidianLaneProjection:
    """Verify and consume every hit into one exact process-private projection."""

    try:
        if type(conn) is not sqlite3.Connection or not conn.in_transaction:
            raise _fail()
        tenant = _identity(tenant_id)
        principal = _identity(principal_id)
        snapshot = _snapshot(snapshot_discriminator)
        request_copy = _freeze_request(request)
        if not _same_exact_graph(request, request_copy):
            raise _fail()
        if (
            ArchiveSearchCorpus.OBSIDIAN not in request_copy.corpora
            or type(execution_binding) is not SearchExecutionBinding
            or not _page_uses_exact_primitives(page)
            or page.lane not in _SUPPORTED_LANES
            or type(phase) is not ArchiveObsidianReadPhase
            or not execution_binding.is_live_private_request_binding
            or not execution_binding.attests_private_request(request_copy.to_identity_json())
            or not execution_binding.attests_authority(
                authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                tenant_id=tenant,
                principal_id=principal,
            )
            or not execution_binding.attests_snapshot(snapshot)
            or (SearchCorpus.OBSIDIAN, page.lane) not in execution_binding.requested_targets
            or any(
                type(hit) is not ArchiveObsidianHit
                or hit.lane is not page.lane
                or hit.factual is not (page.lane is SearchLane.LEXICAL)
                for hit in page.hits
            )
            or bool(page.hits)
            and page.lane is SearchLane.LEXICAL
            and not callable(exact_file_reader)
        ):
            raise _fail()
        lane = page.lane
        hits = page.hits
        coverage = page.to_coverage(
            execution_binding=execution_binding,
            tenant_id=tenant,
            principal_id=principal,
            request=request_copy,
            snapshot_discriminator=snapshot,
        )
        candidates: list[ArchiveSearchCandidate] = []
        for hit in hits:
            if (
                not _hit_uses_exact_primitives(hit)
                or hit.lane is not lane
                or hit.factual is not (lane is SearchLane.LEXICAL)
            ):
                raise _fail()
            if lane is SearchLane.LEXICAL:
                captured = _CapturedFactualHit.from_hit(hit)
                verified_body = verify_archive_obsidian_factual_hit_in_transaction(
                    conn,
                    execution_binding=execution_binding,
                    tenant_id=tenant,
                    principal_id=principal,
                    request=request_copy,
                    snapshot_discriminator=snapshot,
                    hit=hit,
                    phase=phase,
                    exact_file_reader=cast(ArchiveObsidianExactFileReader, exact_file_reader),
                )
                candidate = verified_body.consume_with(
                    execution_binding=execution_binding,
                    tenant_id=tenant,
                    principal_id=principal,
                    request=request_copy,
                    snapshot_discriminator=snapshot,
                    hit=hit,
                    phase=phase,
                    consumer=_factual_consumer(
                        captured,
                        principal_id=principal,
                        query=request_copy.query,
                    ),
                )
            else:
                captured_navigation = _CapturedNavigationHit.from_hit(hit)
                verified_navigation = verify_archive_obsidian_navigation_hit_in_transaction(
                    conn,
                    execution_binding=execution_binding,
                    tenant_id=tenant,
                    principal_id=principal,
                    request=request_copy,
                    snapshot_discriminator=snapshot,
                    hit=hit,
                    phase=phase,
                )
                candidate = verified_navigation.consume_with(
                    execution_binding=execution_binding,
                    tenant_id=tenant,
                    principal_id=principal,
                    request=request_copy,
                    snapshot_discriminator=snapshot,
                    hit=hit,
                    phase=phase,
                    consumer=_navigation_consumer(
                        captured_navigation,
                        principal_id=principal,
                    ),
                )
            candidates.append(candidate)
        candidate_values = tuple(candidates)
        if coverage.returned != len(candidate_values):
            raise _fail()
        return _new_projection(
            candidates=candidate_values,
            coverage=coverage,
            lane=lane,
            phase=phase,
            execution_binding=execution_binding,
            tenant_id=tenant,
            principal_id=principal,
            request=request_copy,
            snapshot_discriminator=snapshot,
        )
    except (ArchiveObsidianAdapterError, ArchiveObsidianStorageError):
        raise _fail() from None
    except Exception:
        raise _fail() from None


__all__ = [
    "ArchiveObsidianAdapterError",
    "ArchiveObsidianLaneProjection",
    "OBSIDIAN_PASSAGE_INDEX_VERSION",
    "project_archive_obsidian_lane_page_in_transaction",
]
