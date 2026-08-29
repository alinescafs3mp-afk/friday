"""Pure, bounded federation of independently authorized archive-search lanes.

This module performs no storage access and grants no authority.  It accepts the
complete closed lane plan for one live execution binding, freezes each private
candidate through the canonical contract, merges exact stable sources, and
derives the first page plus an actual bounded continuation tail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import NoReturn, SupportsIndex, cast

from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL,
    canonical_archive_search_targets,
)
from friday.retrieval.archive_search_contract import (
    MAX_ARCHIVE_MATERIALIZED_CANDIDATES,
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPage,
    ArchiveSearchPassage,
    ArchiveSearchRequest,
    ArchiveSearchWarning,
    ReviewScope,
)
from friday.retrieval.contracts import (
    CoverageState,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceRef,
    TemporalFact,
)

_PROCESS_KEY = secrets.token_bytes(32)
_PROCESS_AUTHORITY = object()
_SEAL_SCHEMA = "friday.archive-search-federation.private.v1"

_ARCHIVE_CORPUS = {
    SearchCorpus.RAW_DOCUMENTS: ArchiveSearchCorpus.DOCUMENTS,
    SearchCorpus.KNOWLEDGE: ArchiveSearchCorpus.KNOWLEDGE,
    SearchCorpus.CONVERSATION: ArchiveSearchCorpus.MESSAGES,
    SearchCorpus.OBSIDIAN: ArchiveSearchCorpus.OBSIDIAN,
    SearchCorpus.GENERATED_ARTIFACTS: ArchiveSearchCorpus.GENERATED,
    SearchCorpus.WEB_CAPTURES: ArchiveSearchCorpus.WEB,
    SearchCorpus.EXTERNAL: ArchiveSearchCorpus.EXTERNAL,
}
_CHANNEL_PRIORITY = {
    ArchiveMatchChannel.EXACT_IDENTITY: 0,
    ArchiveMatchChannel.MESSAGE_HISTORY: 1,
    ArchiveMatchChannel.LEXICAL: 2,
    ArchiveMatchChannel.APPROXIMATE_IDENTITY: 3,
    ArchiveMatchChannel.DENSE: 4,
    ArchiveMatchChannel.CATALOG: 5,
}
_CORPUS_PRIORITY = {
    ArchiveSearchCorpus.KNOWLEDGE: 0,
    ArchiveSearchCorpus.DOCUMENTS: 1,
    ArchiveSearchCorpus.OBSIDIAN: 2,
    ArchiveSearchCorpus.MESSAGES: 3,
    ArchiveSearchCorpus.GENERATED: 4,
    ArchiveSearchCorpus.WEB: 5,
    ArchiveSearchCorpus.EXTERNAL: 6,
}
_EVIDENCE_PRIORITY = {
    ArchiveEvidenceAuthority.CANONICAL: 0,
    ArchiveEvidenceAuthority.NONCANONICAL: 1,
    ArchiveEvidenceAuthority.NAVIGATION_ONLY: 2,
}
_PUBLIC_PROBE_KEY = b"\0" * 32
_PUBLIC_PROBE_TOKEN = "A" * 43


class ArchiveSearchFederationError(ValueError):
    """Body-free rejection of inconsistent lane material."""


def _fail() -> ArchiveSearchFederationError:
    return ArchiveSearchFederationError("archive search federation failed")


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


def _mac(value: object) -> str:
    return hmac.new(
        _PROCESS_KEY,
        b"friday/archive-search-federation/v1\0" + _canonical_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def _target(candidate: ArchiveSearchCandidate, match: ArchiveMatchRank) -> tuple[SearchCorpus, SearchLane]:
    corpus = {
        ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
        ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
        ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
        ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
        ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
        ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
        ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
    }[candidate.corpus]
    return corpus, match.channel.search_lane


def _freeze_candidate(value: object) -> ArchiveSearchCandidate:
    if type(value) is not ArchiveSearchCandidate:
        raise _fail()
    try:
        encoded = cast(ArchiveSearchCandidate, value).to_private_json()
        frozen = ArchiveSearchCandidate.parse_private(encoded)
        if frozen.to_private_json() != encoded:
            raise _fail()
        return frozen
    except ArchiveSearchFederationError:
        raise
    except Exception:
        raise _fail() from None


def _same_exact_graph(left: object, right: object) -> bool:
    """Compare canonical values without allowing duck-typed nested substitutes."""

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


def _canonical_coverage(
    value: object,
    binding: SearchExecutionBinding,
    *,
    require_cursor_free: bool,
) -> SearchCoverage:
    if type(value) is not SearchCoverage:
        raise _fail()
    item = cast(SearchCoverage, value)
    try:
        if item.execution_binding is not binding or (require_cursor_free and item.next_cursor_available):
            raise _fail()
        frozen = SearchCoverage.create(
            corpus=item.corpus,
            lane=item.lane,
            execution_binding=binding,
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
        if frozen.to_json() != item.to_json():
            raise _fail()
        return frozen
    except ArchiveSearchFederationError:
        raise
    except Exception:
        raise _fail() from None


def _freeze_coverage(
    value: object,
    binding: SearchExecutionBinding,
) -> SearchCoverage:
    return _canonical_coverage(value, binding, require_cursor_free=True)


def _passage_union(
    left: tuple[ArchiveSearchPassage, ...],
    right: tuple[ArchiveSearchPassage, ...],
) -> tuple[ArchiveSearchPassage, ...]:
    values: dict[str, ArchiveSearchPassage] = {}
    payloads: dict[str, str] = {}
    for item in (*left, *right):
        identity = item.passage_ref.to_private_json()
        payload = _canonical_bytes(item.to_private_payload()).decode("ascii")
        if identity in payloads and not hmac.compare_digest(payloads[identity], payload):
            raise _fail()
        values[identity] = item
        payloads[identity] = payload
    return tuple(values[key] for key in sorted(values))


def _fact_union(
    left: tuple[TemporalFact, ...],
    right: tuple[TemporalFact, ...],
) -> tuple[TemporalFact, ...]:
    values = {item.to_private_json(): item for item in (*left, *right)}
    return tuple(values[key] for key in sorted(values))


def _match_union(
    left: tuple[ArchiveMatchRank, ...],
    right: tuple[ArchiveMatchRank, ...],
) -> tuple[ArchiveMatchRank, ...]:
    values: dict[ArchiveMatchChannel, ArchiveMatchRank] = {}
    for item in (*left, *right):
        previous = values.get(item.channel)
        if previous is not None and previous.rank != item.rank:
            raise _fail()
        values[item.channel] = item
    return tuple(values[channel] for channel in sorted(values, key=lambda item: item.value))


def _display_value(left: str | None, right: str | None) -> str | None:
    values = {item for item in (left, right) if item is not None}
    return min(values, key=lambda item: (item.casefold(), item)) if values else None


def _merge_candidate(
    left: ArchiveSearchCandidate,
    right: ArchiveSearchCandidate,
) -> ArchiveSearchCandidate:
    if (
        left.resolved_source.source_ref != right.resolved_source.source_ref
        or left.corpus is not right.corpus
        or left.resolved_source != right.resolved_source
        or left.lifecycle_state is not right.lifecycle_state
        or left.review_state is not right.review_state
    ):
        raise _fail()
    if left.navigation_only and not right.navigation_only:
        evidence_authority = right.evidence_authority
    elif (
        right.navigation_only
        and not left.navigation_only
        or left.evidence_authority is right.evidence_authority
    ):
        evidence_authority = left.evidence_authority
    else:
        raise _fail()
    try:
        return ArchiveSearchCandidate.create(
            corpus=left.corpus,
            resolved_source=left.resolved_source,
            title=_display_value(left.title, right.title),
            filename=_display_value(left.filename, right.filename),
            review_state=left.review_state,
            evidence_authority=evidence_authority,
            lifecycle_state=left.lifecycle_state,
            matches=_match_union(left.matches, right.matches),
            temporal_facts=_fact_union(left.temporal_facts, right.temporal_facts),
            passages=_passage_union(left.passages, right.passages),
        )
    except ArchiveSearchFederationError:
        raise
    except Exception:
        raise _fail() from None


def _cross_corpus_key(candidate: ArchiveSearchCandidate) -> tuple[object, ...]:
    return (
        1 if candidate.navigation_only else 0,
        _EVIDENCE_PRIORITY[candidate.evidence_authority],
        _CORPUS_PRIORITY[candidate.corpus],
        _order_key(candidate),
    )


def _with_display_from(
    candidate: ArchiveSearchCandidate,
    alternatives: tuple[ArchiveSearchCandidate, ...],
) -> ArchiveSearchCandidate:
    title = candidate.title
    filename = candidate.filename
    for item in alternatives:
        title = _display_value(title, item.title)
        filename = _display_value(filename, item.filename)
    if title == candidate.title and filename == candidate.filename:
        return candidate
    try:
        return ArchiveSearchCandidate.create(
            corpus=candidate.corpus,
            resolved_source=candidate.resolved_source,
            title=title,
            filename=filename,
            review_state=candidate.review_state,
            evidence_authority=candidate.evidence_authority,
            lifecycle_state=candidate.lifecycle_state,
            matches=candidate.matches,
            temporal_facts=candidate.temporal_facts,
            passages=candidate.passages,
        )
    except Exception:
        raise _fail() from None


def _canonical_source(
    candidates: tuple[ArchiveSearchCandidate, ...],
) -> tuple[ArchiveSearchCandidate, frozenset[tuple[SearchCorpus, SearchLane]]]:
    by_corpus: dict[ArchiveSearchCorpus, ArchiveSearchCandidate] = {}
    for candidate in candidates:
        previous = by_corpus.get(candidate.corpus)
        by_corpus[candidate.corpus] = candidate if previous is None else _merge_candidate(previous, candidate)
    values = tuple(by_corpus.values())
    if any(item.resolved_source != values[0].resolved_source for item in values[1:]):
        raise _fail()
    selected = min(values, key=_cross_corpus_key)
    selected_targets = {_target(selected, match) for match in selected.matches}
    all_targets = {_target(item, match) for item in values for match in item.matches}
    return _with_display_from(selected, values), frozenset(all_targets - selected_targets)


def _order_key(candidate: ArchiveSearchCandidate) -> tuple[object, ...]:
    ranked = tuple(
        sorted(
            ((_CHANNEL_PRIORITY[item.channel], item.rank) for item in candidate.matches),
        )
    )
    return (
        1 if candidate.navigation_only else 0,
        ranked[0][0],
        ranked[0][1],
        -len(ranked),
        ranked,
        candidate.corpus.value,
        candidate.resolved_source.source_ref.to_private_json(),
    )


def _returned_by_target(
    candidates: tuple[ArchiveSearchCandidate, ...],
) -> dict[tuple[SearchCorpus, SearchLane], int]:
    result: dict[tuple[SearchCorpus, SearchLane], int] = {}
    for candidate in candidates:
        for match in candidate.matches:
            target = _target(candidate, match)
            result[target] = result.get(target, 0) + 1
    return result


def _rebound_coverage(
    terminal: tuple[SearchCoverage, ...],
    *,
    binding: SearchExecutionBinding,
    head: tuple[ArchiveSearchCandidate, ...],
    capped_targets: frozenset[tuple[SearchCorpus, SearchLane]],
    cursor_targets: frozenset[tuple[SearchCorpus, SearchLane]],
    limit: int,
) -> tuple[SearchCoverage, ...]:
    returned = _returned_by_target(head)
    result: list[SearchCoverage] = []
    for item in terminal:
        target = item.corpus, item.lane
        count = returned.get(target, 0)
        if count > item.matched_at_least:
            raise _fail()
        affected = target in capped_targets
        states: set[CoverageState]
        if affected:
            states = {state for state in item.states if state is not CoverageState.COMPLETE}
            states.update({CoverageState.PARTIAL, CoverageState.CAPPED})
            bounded_limit = min(item.limit, limit) if item.limit is not None else limit
            applied_limit: int | None = max(count, bounded_limit)
        else:
            states = set(item.states)
            applied_limit = item.limit
        try:
            result.append(
                SearchCoverage.create(
                    corpus=item.corpus,
                    lane=item.lane,
                    execution_binding=binding,
                    states=states,
                    eligible_authorized=item.eligible_authorized,
                    examined=item.examined,
                    matched_at_least=item.matched_at_least,
                    returned=count,
                    authority_rechecked=item.authority_rechecked,
                    snapshot_current=item.snapshot_current,
                    limit=applied_limit,
                    next_cursor_available=target in cursor_targets,
                )
            )
        except Exception:
            raise _fail() from None
    return tuple(result)


def _warnings(
    coverage: tuple[SearchCoverage, ...],
) -> tuple[ArchiveSearchWarning, ...]:
    states = {state for item in coverage for state in item.states}
    values: set[ArchiveSearchWarning] = set()
    if CoverageState.BACKFILL_PENDING in states:
        values.add(ArchiveSearchWarning.BACKFILL_PENDING)
    if CoverageState.CAPPED in states:
        values.add(ArchiveSearchWarning.LANE_CAPPED)
        if any(CoverageState.CAPPED in item.states and not item.next_cursor_available for item in coverage):
            values.add(ArchiveSearchWarning.CONTINUATION_UNAVAILABLE)
    if states & {CoverageState.UNAVAILABLE, CoverageState.EMBEDDING_INCOMPATIBLE}:
        values.add(ArchiveSearchWarning.LANE_UNAVAILABLE)
    if CoverageState.PERMISSION_FILTERED in states:
        values.add(ArchiveSearchWarning.PERMISSION_FILTERED)
    if CoverageState.STALE in states:
        values.add(ArchiveSearchWarning.SNAPSHOT_CHANGED)
    return tuple(sorted(values, key=lambda item: item.value))


def _page_fits(
    *,
    request: ArchiveSearchRequest,
    head: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
    continuation_available: bool,
) -> bool:
    try:
        ArchiveSearchPage.create(
            request=request,
            candidates=head,
            coverage=coverage,
            warnings=warnings,
            continuation=_PUBLIC_PROBE_TOKEN if continuation_available else None,
        ).to_public_json(_PUBLIC_PROBE_KEY)
        return True
    except Exception:
        return False


def _bounded_first_page(
    *,
    request: ArchiveSearchRequest,
    binding: SearchExecutionBinding,
    ordered: tuple[ArchiveSearchCandidate, ...],
    terminal_coverage: tuple[SearchCoverage, ...],
    suppressed_targets: frozenset[tuple[SearchCorpus, SearchLane]],
) -> tuple[
    tuple[ArchiveSearchCandidate, ...],
    tuple[ArchiveSearchCandidate, ...],
    tuple[SearchCoverage, ...],
    tuple[ArchiveSearchWarning, ...],
]:
    maximum = min(request.limit, len(ordered))
    counts = range(maximum, 0, -1) if ordered else (0,)
    for count in counts:
        head = ordered[:count]
        actual_tail = ordered[count:]
        tail = () if len(actual_tail) > ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL else actual_tail
        actual_tail_targets = frozenset(
            _target(candidate, match) for candidate in actual_tail for match in candidate.matches
        )
        capped_targets = actual_tail_targets | suppressed_targets
        cursor_targets = actual_tail_targets if tail else frozenset()
        page_coverage = _rebound_coverage(
            terminal_coverage,
            binding=binding,
            head=head,
            capped_targets=capped_targets,
            cursor_targets=cursor_targets,
            limit=request.limit,
        )
        warning_values = _warnings(page_coverage)
        if _page_fits(
            request=request,
            head=head,
            coverage=page_coverage,
            warnings=warning_values,
            continuation_available=bool(tail),
        ):
            return head, tail, page_coverage, warning_values
    raise _fail()


class FederatedArchiveSearch:
    """Sealed process-private first page and optional exact continuation tail."""

    __slots__ = (
        "_binding",
        "_head",
        "_process_authority",
        "_request_identity",
        "_seal",
        "_tail",
        "_terminal_coverage",
        "_coverage",
        "_warnings",
    )

    _binding: SearchExecutionBinding
    _head: tuple[ArchiveSearchCandidate, ...]
    _process_authority: object
    _request_identity: str
    _seal: str
    _tail: tuple[ArchiveSearchCandidate, ...]
    _terminal_coverage: tuple[SearchCoverage, ...]
    _coverage: tuple[SearchCoverage, ...]
    _warnings: tuple[ArchiveSearchWarning, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail()

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive federation result is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("archive federation result is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive federation result is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive federation result is process-private")

    def __repr__(self) -> str:
        try:
            return f"<FederatedArchiveSearch head={len(self._head)} tail={len(self._tail)} private=True>"
        except Exception:
            return "<FederatedArchiveSearch invalid private=True>"

    def _material(self) -> dict[str, object]:
        return {
            "binding_handle": self._binding.opaque_handle,
            "coverage": [item.to_payload() for item in self._coverage],
            "head": [item.to_private_payload() for item in self._head],
            "request_identity": self._request_identity,
            "schema": _SEAL_SCHEMA,
            "tail": [item.to_private_payload() for item in self._tail],
            "terminal_coverage": [item.to_payload() for item in self._terminal_coverage],
            "warnings": [item.value for item in self._warnings],
        }

    def _is_valid(self) -> bool:
        try:
            frozen_head = tuple(_freeze_candidate(item) for item in self._head)
            frozen_tail = tuple(_freeze_candidate(item) for item in self._tail)
            frozen_coverage = tuple(
                _canonical_coverage(
                    item,
                    self._binding,
                    require_cursor_free=False,
                )
                for item in self._coverage
            )
            frozen_terminal = tuple(_freeze_coverage(item, self._binding) for item in self._terminal_coverage)
            return bool(
                type(self) is FederatedArchiveSearch
                and self._process_authority is _PROCESS_AUTHORITY
                and type(self._binding) is SearchExecutionBinding
                and self._binding.is_live_private_request_binding
                and self._binding.attests_private_request(self._request_identity)
                and type(self._head) is tuple
                and type(self._tail) is tuple
                and type(self._coverage) is tuple
                and type(self._terminal_coverage) is tuple
                and type(self._warnings) is tuple
                and all(type(item) is ArchiveSearchCandidate for item in (*self._head, *self._tail))
                and all(
                    type(item) is SearchCoverage and item.execution_binding is self._binding
                    for item in (*self._coverage, *self._terminal_coverage)
                )
                and all(type(item) is ArchiveSearchWarning for item in self._warnings)
                and all(
                    _same_exact_graph(item, frozen)
                    for item, frozen in zip(self._head, frozen_head, strict=True)
                )
                and all(
                    _same_exact_graph(item, frozen)
                    for item, frozen in zip(self._tail, frozen_tail, strict=True)
                )
                and all(
                    _same_exact_graph(item, frozen)
                    for item, frozen in zip(self._coverage, frozen_coverage, strict=True)
                )
                and all(
                    _same_exact_graph(item, frozen)
                    for item, frozen in zip(
                        self._terminal_coverage,
                        frozen_terminal,
                        strict=True,
                    )
                )
                and hmac.compare_digest(self._seal, _mac(self._material()))
            )
        except Exception:
            return False

    def _require_valid(self) -> None:
        if not self._is_valid():
            raise _fail()

    @property
    def candidates(self) -> tuple[ArchiveSearchCandidate, ...]:
        self._require_valid()
        return self._head

    @property
    def tail_candidates(self) -> tuple[ArchiveSearchCandidate, ...]:
        self._require_valid()
        return self._tail

    @property
    def coverage(self) -> tuple[SearchCoverage, ...]:
        self._require_valid()
        return self._coverage

    @property
    def terminal_coverage(self) -> tuple[SearchCoverage, ...]:
        self._require_valid()
        return self._terminal_coverage

    @property
    def warnings(self) -> tuple[ArchiveSearchWarning, ...]:
        self._require_valid()
        return self._warnings

    @property
    def continuation_available(self) -> bool:
        self._require_valid()
        return bool(self._tail)


def _new_result(
    *,
    binding: SearchExecutionBinding,
    request_identity: str,
    head: tuple[ArchiveSearchCandidate, ...],
    tail: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    terminal_coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
) -> FederatedArchiveSearch:
    result = cast(FederatedArchiveSearch, object.__new__(FederatedArchiveSearch))
    for name, value in (
        ("_binding", binding),
        ("_coverage", coverage),
        ("_head", head),
        ("_process_authority", _PROCESS_AUTHORITY),
        ("_request_identity", request_identity),
        ("_seal", "0" * 64),
        ("_tail", tail),
        ("_terminal_coverage", terminal_coverage),
        ("_warnings", warnings),
    ):
        object.__setattr__(result, name, value)
    object.__setattr__(result, "_seal", _mac(result._material()))
    return result


def federate_archive_search(
    *,
    request: ArchiveSearchRequest,
    execution_binding: SearchExecutionBinding,
    coverage: tuple[SearchCoverage, ...],
    candidates_by_target: Mapping[
        tuple[SearchCorpus, SearchLane],
        tuple[ArchiveSearchCandidate, ...],
    ],
) -> FederatedArchiveSearch:
    """Freeze and merge one complete, exact set of authorized lane results."""

    try:
        if type(request) is not ArchiveSearchRequest or request.continuation is not None:
            raise _fail()
        request_copy = ArchiveSearchRequest.parse_private(request.to_private_json())
        request_identity = request_copy.to_identity_json()
        if (
            type(execution_binding) is not SearchExecutionBinding
            or not execution_binding.is_live_private_request_binding
            or not execution_binding.attests_private_request(request_identity)
        ):
            raise _fail()
        expected_targets = canonical_archive_search_targets(request_copy)
        if execution_binding.requested_targets != expected_targets:
            raise _fail()
        if type(coverage) is not tuple or type(candidates_by_target) is not dict:
            raise _fail()
        if len(coverage) != len(expected_targets):
            raise _fail()
        frozen_coverage = tuple(_freeze_coverage(item, execution_binding) for item in coverage)
        if frozen_coverage != tuple(
            sorted(frozen_coverage, key=lambda item: (item.corpus.value, item.lane.value))
        ):
            raise _fail()
        coverage_targets = tuple((item.corpus, item.lane) for item in frozen_coverage)
        supplied_targets = tuple(candidates_by_target)
        if (
            any(
                type(target) is not tuple
                or len(target) != 2
                or type(target[0]) is not SearchCorpus
                or type(target[1]) is not SearchLane
                for target in supplied_targets
            )
            or coverage_targets != expected_targets
            or set(supplied_targets) != set(expected_targets)
        ):
            raise _fail()

        frozen_lanes: dict[
            tuple[SearchCorpus, SearchLane],
            tuple[ArchiveSearchCandidate, ...],
        ] = {}
        for target in expected_targets:
            raw_candidates = candidates_by_target[target]
            if type(raw_candidates) is not tuple or len(raw_candidates) > MAX_ARCHIVE_MATERIALIZED_CANDIDATES:
                raise _fail()
            candidates = tuple(_freeze_candidate(item) for item in raw_candidates)
            lane_coverage = frozen_coverage[coverage_targets.index(target)]
            if lane_coverage.returned != len(candidates) or (
                lane_coverage.matched_at_least > lane_coverage.returned
                and CoverageState.CAPPED not in lane_coverage.states
            ):
                raise _fail()
            source_refs: tuple[SourceRef, ...] = tuple(item.resolved_source.source_ref for item in candidates)
            if len(source_refs) != len(set(source_refs)):
                raise _fail()
            expected_ranks = tuple(range(1, len(candidates) + 1))
            ranks: list[int] = []
            for candidate in candidates:
                if (
                    candidate.corpus is not _ARCHIVE_CORPUS[target[0]]
                    or len(candidate.matches) != 1
                    or _target(candidate, candidate.matches[0]) != target
                ):
                    raise _fail()
                ranks.append(candidate.matches[0].rank)
            if tuple(ranks) != expected_ranks or (ranks and ranks[-1] > lane_coverage.matched_at_least):
                raise _fail()
            frozen_lanes[target] = candidates

        all_candidates = tuple(candidate for target in expected_targets for candidate in frozen_lanes[target])
        constraints = {item.corpus: item.states for item in request_copy.lifecycle_constraints}
        if any(
            item.corpus in constraints and item.lifecycle_state not in constraints[item.corpus]
            for item in all_candidates
        ):
            raise _fail()
        if request_copy.review_scope is ReviewScope.CONFIRMED_ONLY and any(
            item.evidence_authority is ArchiveEvidenceAuthority.NONCANONICAL
            or (
                item.corpus in {ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE}
                and item.review_state is not ArchiveReviewState.CONFIRMED
            )
            for item in all_candidates
        ):
            raise _fail()

        grouped: dict[SourceRef, list[ArchiveSearchCandidate]] = {}
        for target in expected_targets:
            for candidate in frozen_lanes[target]:
                source = candidate.resolved_source.source_ref
                grouped.setdefault(source, []).append(candidate)
        merged: list[ArchiveSearchCandidate] = []
        suppressed_targets: set[tuple[SearchCorpus, SearchLane]] = set()
        for source_candidates in grouped.values():
            candidate, suppressed = _canonical_source(tuple(source_candidates))
            merged.append(candidate)
            suppressed_targets.update(suppressed)
        ordered = tuple(sorted(merged, key=_order_key))

        # A globally deduplicated source may be represented by one canonical
        # corpus only.  Persist the other corpus targets as explicitly capped
        # and cursorless in the continuation baseline; otherwise a resumed page
        # could resurrect their pre-dedup COMPLETE/returned counts.
        continuation_coverage = _rebound_coverage(
            frozen_coverage,
            binding=execution_binding,
            head=ordered,
            capped_targets=frozenset(suppressed_targets),
            cursor_targets=frozenset(),
            limit=request_copy.limit,
        )

        head, frozen_tail, page_coverage, warning_values = _bounded_first_page(
            request=request_copy,
            binding=execution_binding,
            ordered=ordered,
            terminal_coverage=continuation_coverage,
            suppressed_targets=frozenset(suppressed_targets),
        )
        return _new_result(
            binding=execution_binding,
            request_identity=request_identity,
            head=head,
            tail=frozen_tail,
            coverage=page_coverage,
            terminal_coverage=continuation_coverage,
            warnings=warning_values,
        )
    except ArchiveSearchFederationError:
        raise
    except Exception:
        raise _fail() from None


__all__ = [
    "ArchiveSearchFederationError",
    "FederatedArchiveSearch",
    "federate_archive_search",
]
