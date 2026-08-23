"""Honest per-lane search coverage and fail-closed absence decisions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from friday.retrieval._contract_utils import (
    RetrievalContractError,
    bounded_count,
    bounded_text,
    canonical_json,
    enum_value,
    exact_object,
    keyed_digest,
    lowercase_sha256,
    parse_canonical_object,
)
from friday.retrieval.identity_contract import AuthorityScope

SEARCH_COVERAGE_SCHEMA = "friday.search-coverage.v1"
SEARCH_EXECUTION_BINDING_SCHEMA = "friday.search-execution-binding.v1"


def _count(value: object, *, label: str, optional: bool = False) -> int | None:
    return bounded_count(value, label=label, optional=optional)


class SearchLane(StrEnum):
    CATALOG = "catalog"
    EXACT_IDENTITY = "exact_identity"
    LEXICAL = "lexical"
    APPROXIMATE_IDENTITY = "approximate_identity"
    DENSE = "dense"
    MESSAGE_HISTORY = "message_history"


class SearchCorpus(StrEnum):
    RAW_DOCUMENTS = "raw_documents"
    KNOWLEDGE = "knowledge"
    OBSIDIAN = "obsidian"
    CONVERSATION = "conversation"
    WEB_CAPTURES = "web_captures"
    EXTERNAL = "external"
    GENERATED_ARTIFACTS = "generated_artifacts"


SearchTarget = tuple[SearchCorpus, SearchLane]


def _targets(values: Iterable[SearchTarget]) -> tuple[SearchTarget, ...]:
    items = tuple(values)
    if not items or any(
        type(item) is not tuple
        or len(item) != 2
        or not isinstance(item[0], SearchCorpus)
        or not isinstance(item[1], SearchLane)
        for item in items
    ):
        raise RetrievalContractError("requested targets must use closed corpus/lane pairs")
    result = tuple(sorted(items, key=lambda item: (item[0].value, item[1].value)))
    if len(result) != len(set(result)):
        raise RetrievalContractError("requested targets must be unique")
    return result


@dataclass(frozen=True, slots=True, repr=False)
class SearchExecutionBinding:
    """Opaque keyed binding for one normalized request and retrieval run."""

    authority_scope: AuthorityScope
    requested_targets: tuple[SearchTarget, ...]
    opaque_handle: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority_scope, AuthorityScope):
            raise RetrievalContractError("execution binding requires a closed authority scope")
        if self.requested_targets != _targets(self.requested_targets):
            raise RetrievalContractError("execution binding targets must be canonical")
        lowercase_sha256(self.opaque_handle, label="opaque execution handle")

    def __repr__(self) -> str:
        return "SearchExecutionBinding(private_request_bound=True)"

    @classmethod
    def create(
        cls,
        *,
        normalized_private_request_json: str,
        authority_scope: AuthorityScope,
        tenant_id: str | None,
        principal_id: str | None,
        requested_targets: Iterable[SearchTarget],
        snapshot_discriminator: str,
        run_discriminator: str,
        privacy_key: bytes,
    ) -> SearchExecutionBinding:
        if not isinstance(authority_scope, AuthorityScope):
            raise RetrievalContractError("execution authority scope must be a closed enum")
        request = parse_canonical_object(
            normalized_private_request_json,
            label="normalized private search request",
        )
        if len(normalized_private_request_json.encode("utf-8")) > 8_192:
            raise RetrievalContractError("normalized private search request is too large")
        if tenant_id is not None:
            bounded_text(tenant_id, label="binding tenant_id", maximum_bytes=200)
        if principal_id is not None:
            bounded_text(principal_id, label="binding principal_id", maximum_bytes=200)
        expected_presence = {
            AuthorityScope.TENANT: (True, False),
            AuthorityScope.PRINCIPAL: (False, True),
            AuthorityScope.TENANT_PRINCIPAL: (True, True),
        }[authority_scope]
        if (tenant_id is not None, principal_id is not None) != expected_presence:
            raise RetrievalContractError("execution authority IDs do not match the declared scope")
        targets = _targets(requested_targets)
        snapshot = bounded_text(
            snapshot_discriminator,
            label="snapshot discriminator",
            maximum_bytes=256,
        )
        run = bounded_text(run_discriminator, label="run discriminator", maximum_bytes=256)
        private_payload: dict[str, object] = {
            "authority_scope": authority_scope.value,
            "normalized_request": request,
            "principal_id": principal_id,
            "requested_targets": [{"corpus": corpus.value, "lane": lane.value} for corpus, lane in targets],
            "run_discriminator": run,
            "snapshot_discriminator": snapshot,
            "tenant_id": tenant_id,
        }
        return cls(
            authority_scope=authority_scope,
            requested_targets=targets,
            opaque_handle=keyed_digest(
                b"friday/search-execution-binding/v1",
                private_payload,
                privacy_key,
            ),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "authority_scope": self.authority_scope.value,
            "opaque_handle": self.opaque_handle,
            "requested_targets": [
                {"corpus": corpus.value, "lane": lane.value} for corpus, lane in self.requested_targets
            ],
            "schema": SEARCH_EXECUTION_BINDING_SCHEMA,
        }

    @classmethod
    def from_payload(cls, value: object) -> SearchExecutionBinding:
        payload = exact_object(
            value,
            frozenset({"authority_scope", "opaque_handle", "requested_targets", "schema"}),
            label="search execution binding",
        )
        if payload["schema"] != SEARCH_EXECUTION_BINDING_SCHEMA:
            raise RetrievalContractError("search execution binding schema is unsupported")
        raw_targets = payload["requested_targets"]
        handle = payload["opaque_handle"]
        if type(raw_targets) is not list or not isinstance(handle, str):
            raise RetrievalContractError("search execution binding payload is invalid")
        targets: list[SearchTarget] = []
        for value_target in raw_targets:
            target = exact_object(
                value_target,
                frozenset({"corpus", "lane"}),
                label="search execution target",
            )
            targets.append(
                (
                    enum_value(SearchCorpus, target["corpus"], label="search corpus"),
                    enum_value(SearchLane, target["lane"], label="search lane"),
                )
            )
        return cls(
            authority_scope=enum_value(
                AuthorityScope,
                payload["authority_scope"],
                label="authority scope",
            ),
            requested_targets=_targets(targets),
            opaque_handle=handle,
        )


class CoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    PERMISSION_FILTERED = "permission_filtered"
    BACKFILL_PENDING = "backfill_pending"
    EMBEDDING_INCOMPATIBLE = "embedding_incompatible"
    CAPPED = "capped"


class AbsenceDecision(StrEnum):
    EVIDENCE_FOUND = "evidence_found"
    AUTHORIZED_ABSENCE_CONFIRMED = "authorized_absence_confirmed"
    NOT_ESTABLISHED = "not_established"


_INVALID_EVIDENCE_STATES = frozenset(
    {
        CoverageState.STALE,
        CoverageState.EMBEDDING_INCOMPATIBLE,
    }
)


@dataclass(frozen=True, slots=True)
class SearchCoverage:
    """Coverage of one requested lane over its authorized visible subset.

    Counts never include filtered objects. ``next_cursor_available`` records only
    whether continuation exists; process-private cursor tokens are not serialized.
    """

    corpus: SearchCorpus
    lane: SearchLane
    execution_binding: SearchExecutionBinding
    states: tuple[CoverageState, ...]
    eligible_authorized: int | None
    examined: int
    matched_at_least: int
    returned: int
    limit: int | None
    next_cursor_available: bool
    authority_rechecked: bool
    snapshot_current: bool

    def __post_init__(self) -> None:
        if not isinstance(self.corpus, SearchCorpus) or not isinstance(self.lane, SearchLane):
            raise RetrievalContractError("search corpus and lane must be closed enums")
        if (
            not isinstance(self.execution_binding, SearchExecutionBinding)
            or (
                self.corpus,
                self.lane,
            )
            not in self.execution_binding.requested_targets
        ):
            raise RetrievalContractError("coverage target is absent from its execution binding")
        if (
            type(self.states) is not tuple
            or not self.states
            or any(not isinstance(item, CoverageState) for item in self.states)
            or self.states != tuple(sorted(self.states, key=lambda item: item.value))
            or len(self.states) != len(set(self.states))
        ):
            raise RetrievalContractError("coverage states must be a sorted unique typed tuple")
        if CoverageState.COMPLETE in self.states and self.states != (CoverageState.COMPLETE,):
            raise RetrievalContractError("complete coverage is exclusive")
        if CoverageState.COMPLETE not in self.states and not (
            {CoverageState.PARTIAL, CoverageState.UNAVAILABLE} & set(self.states)
        ):
            raise RetrievalContractError("incomplete coverage requires partial or unavailable state")
        if CoverageState.PARTIAL in self.states and len(self.states) == 1:
            raise RetrievalContractError("partial coverage requires an explicit simultaneous reason")
        if CoverageState.EMBEDDING_INCOMPATIBLE in self.states and self.lane is not SearchLane.DENSE:
            raise RetrievalContractError("embedding incompatibility belongs only to the dense lane")
        eligible = _count(self.eligible_authorized, label="eligible_authorized", optional=True)
        examined = _count(self.examined, label="examined")
        matched = _count(self.matched_at_least, label="matched_at_least")
        returned = _count(self.returned, label="returned")
        limit = _count(self.limit, label="limit", optional=True)
        assert matched is not None and returned is not None
        if limit == 0:
            raise RetrievalContractError("coverage limit must be positive when present")
        assert examined is not None
        if returned > matched or matched > examined:
            raise RetrievalContractError("coverage counts are inconsistent")
        if eligible is not None and examined > eligible:
            raise RetrievalContractError("examined count exceeds the authorized eligible corpus")
        if limit is not None and returned > limit:
            raise RetrievalContractError("returned count exceeds the declared limit")
        if any(
            type(item) is not bool
            for item in (
                self.next_cursor_available,
                self.authority_rechecked,
                self.snapshot_current,
            )
        ):
            raise RetrievalContractError("coverage attestations and cursor availability must be booleans")
        if CoverageState.COMPLETE in self.states and (
            eligible is None or examined != eligible or self.next_cursor_available
        ):
            raise RetrievalContractError("complete coverage requires eligible==examined and no cursor")
        if (
            CoverageState.UNAVAILABLE in self.states
            and CoverageState.PARTIAL not in self.states
            and (
                eligible is not None
                or examined != 0
                or matched != 0
                or returned != 0
                or self.next_cursor_available
            )
        ):
            raise RetrievalContractError("wholly unavailable coverage cannot claim examination or matches")
        if CoverageState.CAPPED in self.states and limit is None:
            raise RetrievalContractError("capped coverage requires the applied limit")
        if CoverageState.CAPPED in self.states and CoverageState.PARTIAL not in self.states:
            raise RetrievalContractError("capped coverage must be explicitly partial")
        if self.next_cursor_available and CoverageState.CAPPED not in self.states:
            raise RetrievalContractError("continuation requires an explicit capped state")

    @classmethod
    def create(
        cls,
        *,
        corpus: SearchCorpus,
        lane: SearchLane,
        execution_binding: SearchExecutionBinding,
        states: Iterable[CoverageState],
        eligible_authorized: int | None,
        examined: int,
        matched_at_least: int,
        returned: int,
        authority_rechecked: bool,
        snapshot_current: bool,
        limit: int | None = None,
        next_cursor_available: bool = False,
    ) -> SearchCoverage:
        state_values = tuple(states)
        if any(not isinstance(item, CoverageState) for item in state_values):
            raise RetrievalContractError("coverage states must use the typed contract")
        return cls(
            corpus=corpus,
            lane=lane,
            execution_binding=execution_binding,
            states=tuple(sorted(state_values, key=lambda item: item.value)),
            eligible_authorized=eligible_authorized,
            examined=examined,
            matched_at_least=matched_at_least,
            returned=returned,
            limit=limit,
            next_cursor_available=next_cursor_available,
            authority_rechecked=authority_rechecked,
            snapshot_current=snapshot_current,
        )

    def absence_decision(self) -> AbsenceDecision:
        if (
            self.matched_at_least > 0
            and self.authority_rechecked
            and self.snapshot_current
            and not (set(self.states) & _INVALID_EVIDENCE_STATES)
        ):
            return AbsenceDecision.EVIDENCE_FOUND
        if (
            self.states == (CoverageState.COMPLETE,)
            and self.matched_at_least == 0
            and self.authority_rechecked
            and self.snapshot_current
        ):
            return AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
        return AbsenceDecision.NOT_ESTABLISHED

    def to_payload(self) -> dict[str, object]:
        return {
            "authority_rechecked": self.authority_rechecked,
            "corpus": self.corpus.value,
            "eligible_authorized": self.eligible_authorized,
            "examined": self.examined,
            "execution_binding": self.execution_binding.to_payload(),
            "lane": self.lane.value,
            "limit": self.limit,
            "matched_at_least": self.matched_at_least,
            "next_cursor_available": self.next_cursor_available,
            "returned": self.returned,
            "schema": SEARCH_COVERAGE_SCHEMA,
            "snapshot_current": self.snapshot_current,
            "states": [item.value for item in self.states],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, value: object) -> SearchCoverage:
        payload = exact_object(
            value,
            frozenset(
                {
                    "authority_rechecked",
                    "corpus",
                    "eligible_authorized",
                    "examined",
                    "execution_binding",
                    "lane",
                    "limit",
                    "matched_at_least",
                    "next_cursor_available",
                    "returned",
                    "schema",
                    "snapshot_current",
                    "states",
                }
            ),
            label="search coverage",
        )
        if payload["schema"] != SEARCH_COVERAGE_SCHEMA:
            raise RetrievalContractError("search coverage schema is unsupported")
        states = payload["states"]
        if type(states) is not list:
            raise RetrievalContractError("coverage states must be an array")
        next_cursor = payload["next_cursor_available"]
        authority_rechecked = payload["authority_rechecked"]
        snapshot_current = payload["snapshot_current"]
        if any(type(item) is not bool for item in (next_cursor, authority_rechecked, snapshot_current)):
            raise RetrievalContractError("coverage attestations and cursor availability must be booleans")
        matched = _count(payload["matched_at_least"], label="matched_at_least")
        returned = _count(payload["returned"], label="returned")
        examined = _count(payload["examined"], label="examined")
        assert matched is not None and returned is not None and examined is not None
        return cls.create(
            corpus=enum_value(SearchCorpus, payload["corpus"], label="search corpus"),
            lane=enum_value(SearchLane, payload["lane"], label="search lane"),
            execution_binding=SearchExecutionBinding.from_payload(payload["execution_binding"]),
            states=(enum_value(CoverageState, item, label="coverage state") for item in states),
            eligible_authorized=_count(
                payload["eligible_authorized"],
                label="eligible_authorized",
                optional=True,
            ),
            examined=examined,
            matched_at_least=matched,
            returned=returned,
            authority_rechecked=authority_rechecked,
            snapshot_current=snapshot_current,
            limit=_count(payload["limit"], label="limit", optional=True),
            next_cursor_available=next_cursor,
        )

    @classmethod
    def parse(cls, value: str) -> SearchCoverage:
        result = cls.from_payload(parse_canonical_object(value, label="search coverage"))
        if value != result.to_json():
            raise RetrievalContractError("search coverage JSON is not semantically canonical")
        return result


def aggregate_absence_decision(
    coverages: Iterable[SearchCoverage],
    *,
    requested_targets: Iterable[SearchTarget],
) -> AbsenceDecision:
    """Confirm absence only for every explicitly requested applicable lane."""

    results = tuple(coverages)
    try:
        requested = _targets(requested_targets)
    except RetrievalContractError:
        return AbsenceDecision.NOT_ESTABLISHED
    if (
        not results
        or any(not isinstance(item, SearchCoverage) for item in results)
        or len(results) != len(requested)
        or {(item.corpus, item.lane) for item in results} != set(requested)
        or len({(item.corpus, item.lane) for item in results}) != len(results)
        or len({item.execution_binding for item in results}) != 1
        or results[0].execution_binding.requested_targets != requested
    ):
        return AbsenceDecision.NOT_ESTABLISHED
    decisions = tuple(item.absence_decision() for item in results)
    if AbsenceDecision.EVIDENCE_FOUND in decisions:
        return AbsenceDecision.EVIDENCE_FOUND
    if all(item is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED for item in decisions):
        return AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
    return AbsenceDecision.NOT_ESTABLISHED


__all__ = [
    "AbsenceDecision",
    "CoverageState",
    "SearchCoverage",
    "SearchCorpus",
    "SearchExecutionBinding",
    "SearchLane",
    "aggregate_absence_decision",
]
