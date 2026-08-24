"""Honest per-lane search coverage and fail-closed absence decisions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NoReturn, SupportsIndex, cast

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
_MAX_PRIVATE_REQUEST_BYTES = 32_768
_PROCESS_REQUEST_ATTESTATION_KEY = secrets.token_bytes(32)
_PROCESS_BINDING_AUTHORITY = object()


def _private_request_attestation(value: str) -> str:
    return hmac.new(
        _PROCESS_REQUEST_ATTESTATION_KEY,
        b"friday/search-execution-private-request/v1\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _private_authority_attestation(
    authority_scope: AuthorityScope,
    tenant_id: str | None,
    principal_id: str | None,
) -> str:
    material = canonical_json(
        {
            "authority_scope": authority_scope.value,
            "principal_id": principal_id,
            "tenant_id": tenant_id,
        }
    ).encode("ascii")
    return hmac.new(
        _PROCESS_REQUEST_ATTESTATION_KEY,
        b"friday/search-execution-private-authority/v1\0" + material,
        hashlib.sha256,
    ).hexdigest()


def _private_snapshot_attestation(value: str) -> str:
    return hmac.new(
        _PROCESS_REQUEST_ATTESTATION_KEY,
        b"friday/search-execution-private-snapshot/v1\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _private_binding_seal(
    *,
    authority_scope: AuthorityScope,
    requested_targets: tuple[SearchTarget, ...],
    opaque_handle: str,
    request_attestation: str,
    authority_attestation: str,
    snapshot_attestation: str,
) -> str:
    material = canonical_json(
        {
            "authority_attestation": authority_attestation,
            "authority_scope": authority_scope.value,
            "opaque_handle": opaque_handle,
            "request_attestation": request_attestation,
            "requested_targets": [
                {"corpus": corpus.value, "lane": lane.value} for corpus, lane in requested_targets
            ],
            "snapshot_attestation": snapshot_attestation,
        }
    ).encode("ascii")
    return hmac.new(
        _PROCESS_REQUEST_ATTESTATION_KEY,
        b"friday/search-execution-private-binding/v1\0" + material,
        hashlib.sha256,
    ).hexdigest()


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
    _private_request_attestation: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _private_authority_attestation: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _private_snapshot_attestation: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _private_binding_seal: str | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _process_authority: object | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.authority_scope, AuthorityScope):
            raise RetrievalContractError("execution binding requires a closed authority scope")
        if type(self.requested_targets) is not tuple or self.requested_targets != _targets(
            self.requested_targets
        ):
            raise RetrievalContractError("execution binding targets must be canonical")
        lowercase_sha256(self.opaque_handle, label="opaque execution handle")
        if self._private_request_attestation is not None:
            lowercase_sha256(
                self._private_request_attestation,
                label="private request attestation",
            )
        if self._private_authority_attestation is not None:
            lowercase_sha256(
                self._private_authority_attestation,
                label="private authority attestation",
            )
        if self._private_snapshot_attestation is not None:
            lowercase_sha256(
                self._private_snapshot_attestation,
                label="private snapshot attestation",
            )
        if self._private_binding_seal is not None:
            lowercase_sha256(self._private_binding_seal, label="private binding seal")

    def __repr__(self) -> str:
        return "SearchExecutionBinding(private=True)"

    def __copy__(self) -> NoReturn:
        raise TypeError("search execution binding is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("search execution binding is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("search execution binding is process-private")

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
        if len(normalized_private_request_json.encode("utf-8")) > _MAX_PRIVATE_REQUEST_BYTES:
            raise RetrievalContractError("normalized private search request is too large")
        if tenant_id is not None and type(tenant_id) is not str:
            raise RetrievalContractError("binding tenant_id must be canonical text")
        if principal_id is not None and type(principal_id) is not str:
            raise RetrievalContractError("binding principal_id must be canonical text")
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
        if type(snapshot_discriminator) is not str or type(run_discriminator) is not str:
            raise RetrievalContractError("execution discriminators must be canonical text")
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
        binding = cls(
            authority_scope=authority_scope,
            requested_targets=targets,
            opaque_handle=keyed_digest(
                b"friday/search-execution-binding/v1",
                private_payload,
                privacy_key,
            ),
        )
        object.__setattr__(
            binding,
            "_private_request_attestation",
            _private_request_attestation(normalized_private_request_json),
        )
        object.__setattr__(
            binding,
            "_private_authority_attestation",
            _private_authority_attestation(authority_scope, tenant_id, principal_id),
        )
        object.__setattr__(
            binding,
            "_private_snapshot_attestation",
            _private_snapshot_attestation(snapshot),
        )
        object.__setattr__(
            binding,
            "_private_binding_seal",
            _private_binding_seal(
                authority_scope=authority_scope,
                requested_targets=targets,
                opaque_handle=binding.opaque_handle,
                request_attestation=cast(str, binding._private_request_attestation),
                authority_attestation=cast(str, binding._private_authority_attestation),
                snapshot_attestation=cast(str, binding._private_snapshot_attestation),
            ),
        )
        object.__setattr__(binding, "_process_authority", _PROCESS_BINDING_AUTHORITY)
        return binding

    @property
    def is_live_private_request_binding(self) -> bool:
        try:
            request_attestation = self._private_request_attestation
            authority_attestation = self._private_authority_attestation
            snapshot_attestation = self._private_snapshot_attestation
            binding_seal = self._private_binding_seal
            if (
                type(self) is not SearchExecutionBinding
                or self._process_authority is not _PROCESS_BINDING_AUTHORITY
                or type(self.authority_scope) is not AuthorityScope
                or type(self.requested_targets) is not tuple
                or self.requested_targets != _targets(self.requested_targets)
                or type(self.opaque_handle) is not str
                or type(request_attestation) is not str
                or type(authority_attestation) is not str
                or type(snapshot_attestation) is not str
                or type(binding_seal) is not str
            ):
                return False
            expected = _private_binding_seal(
                authority_scope=self.authority_scope,
                requested_targets=self.requested_targets,
                opaque_handle=self.opaque_handle,
                request_attestation=request_attestation,
                authority_attestation=authority_attestation,
                snapshot_attestation=snapshot_attestation,
            )
            return hmac.compare_digest(binding_seal, expected)
        except Exception:
            return False

    def attests_private_request(self, normalized_private_request_json: object) -> bool:
        """Match the exact canonical private request only for a live-created binding."""

        expected = self._private_request_attestation
        if expected is None or type(normalized_private_request_json) is not str:
            return False
        if not self.is_live_private_request_binding:
            return False
        try:
            parse_canonical_object(
                normalized_private_request_json,
                label="normalized private search request",
            )
            if len(normalized_private_request_json.encode("utf-8")) > _MAX_PRIVATE_REQUEST_BYTES:
                return False
        except Exception:
            return False
        actual = _private_request_attestation(normalized_private_request_json)
        return hmac.compare_digest(expected, actual)

    def attests_authority(
        self,
        *,
        authority_scope: AuthorityScope,
        tenant_id: str | None,
        principal_id: str | None,
    ) -> bool:
        """Match the exact private actor axis used to create this live binding."""

        expected = self._private_authority_attestation
        if expected is None or not self.is_live_private_request_binding:
            return False
        try:
            if not isinstance(authority_scope, AuthorityScope):
                return False
            if tenant_id is not None and type(tenant_id) is not str:
                return False
            if principal_id is not None and type(principal_id) is not str:
                return False
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
                return False
            actual = _private_authority_attestation(
                authority_scope,
                tenant_id,
                principal_id,
            )
            return hmac.compare_digest(expected, actual)
        except Exception:
            return False

    def attests_snapshot(self, snapshot_discriminator: object) -> bool:
        """Match the exact private snapshot discriminator of this live run."""

        expected = self._private_snapshot_attestation
        if expected is None or not self.is_live_private_request_binding:
            return False
        try:
            if type(snapshot_discriminator) is not str:
                return False
            snapshot = bounded_text(
                snapshot_discriminator,
                label="snapshot discriminator",
                maximum_bytes=256,
            )
            actual = _private_snapshot_attestation(snapshot)
            return hmac.compare_digest(expected, actual)
        except Exception:
            return False

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
            type(self.execution_binding) is not SearchExecutionBinding
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
            and self.execution_binding.is_live_private_request_binding
            and self.authority_rechecked
            and self.snapshot_current
            and not (set(self.states) & _INVALID_EVIDENCE_STATES)
        ):
            return AbsenceDecision.EVIDENCE_FOUND
        if (
            self.states == (CoverageState.COMPLETE,)
            and self.matched_at_least == 0
            and self.execution_binding.is_live_private_request_binding
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
        or any(type(item) is not SearchCoverage for item in results)
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
