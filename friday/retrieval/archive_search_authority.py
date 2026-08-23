"""Process-private authority and two-phase reauthorization for archive search."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol, SupportsIndex, cast

from friday.retrieval.archive_evidence_snapshot import (
    archive_selected_evidence_snapshot_sha256,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveMatchRank,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPage,
    ArchiveSearchRequest,
    ArchiveSearchWarning,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    SourceRef,
    TextSpanLocator,
)

ARCHIVE_AUTHORITY_MAX_CANDIDATES = 20
ARCHIVE_AUTHORITY_MAX_COVERAGES = len(SearchCorpus) * len(SearchLane)
ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES = 32
ARCHIVE_AUTHORITY_MAX_MODEL_BYTES = 32_768
ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL = 256
ARCHIVE_AUTHORITY_MAX_ANSWER_BYTES = 1_000_000

_PROCESS_KEY = secrets.token_bytes(32)
_NONCE_BYTES = 32
_CONTINUATION_TOKEN_BYTES = 32
_CONTINUATION_TOKEN_CHARS = 43
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_CITATION = re.compile(r"\[(A[1-9][0-9]{0,2}(?:\.[1-8])?)\]")
_PUBLIC_CITATION_LABEL = re.compile(r"A([1-9][0-9]{0,2})(?:\.[1-8])?\Z")
_PUBLIC_CITATION_PREFIX = re.compile(r"\[A", re.IGNORECASE)
_MAX_ACTOR_ID_BYTES = 200
_MAX_MODEL_BYTES = 7_900
_RUN_SCHEMA = "friday.archive-search-run-binding.private.v2"
_BATCH_SCHEMA = "friday.archive-search-model-batch.private.v2"
_ATTESTATION_SCHEMA = "friday.archive-search-publication-attestation.private.v3"
ARCHIVE_SEARCH_SELECTED_EVIDENCE_SCHEMA = "friday.archive-search-selected-evidence.private.v1"
_REDEMPTION_SCHEMA = "friday.archive-search-continuation-redemption.private.v3"
_SELECTED_CONTINUATION_SCHEMA = "friday.archive-search-continuation-selection.private.v1"
_LEDGER_SCHEMA = "friday.archive-search-model-batch-ledger.private.v2"
_CONTINUATION_RECORD_SCHEMA = "friday.archive-search-continuation-record.private.v3"
_CONTINUATION_ISSUE_SCHEMA = "friday.archive-search-continuation-issue.private.v3"

_CONTINUATION_TTL_SECONDS = 15 * 60
_CONTINUATION_MAX_RECORDS = 512
_CONTINUATION_MAX_TOTAL_CANDIDATES = 4_096
_CONTINUATION_MAX_ISSUANCE_KEYS = 16_384
_TURN_LEDGER_MAX_RECORDS = 8_192
_TURN_LEDGER_RETAIN_SECONDS = 6 * 60 * 60
_TURN_LEDGER_REPLAY_FILTER_BYTES = 262_144
_TURN_LEDGER_REPLAY_FILTER_BUCKETS = 7


def _public_citation_label_is_valid(value: object) -> bool:
    if type(value) is not str:
        return False
    match = _PUBLIC_CITATION_LABEL.fullmatch(value)
    return bool(match is not None and int(match.group(1)) <= 640)


_CORPUS_TARGET = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
    ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
    ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
    ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
    ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
    ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
}
_CORPUS_LANES = {
    ArchiveSearchCorpus.DOCUMENTS: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
    ArchiveSearchCorpus.KNOWLEDGE: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
    ArchiveSearchCorpus.MESSAGES: (
        SearchLane.LEXICAL,
        SearchLane.DENSE,
        SearchLane.MESSAGE_HISTORY,
    ),
    ArchiveSearchCorpus.OBSIDIAN: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
    ArchiveSearchCorpus.GENERATED: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
    ArchiveSearchCorpus.WEB: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
    ArchiveSearchCorpus.EXTERNAL: (
        SearchLane.CATALOG,
        SearchLane.EXACT_IDENTITY,
        SearchLane.LEXICAL,
        SearchLane.APPROXIMATE_IDENTITY,
        SearchLane.DENSE,
    ),
}


class ArchiveSearchAuthorityError(ValueError):
    """A closed archive-search authority contract was not satisfied."""


class ArchiveSearchAuthorityPhase(StrEnum):
    BEFORE_MODEL = "before_model"
    BEFORE_PUBLICATION = "before_publication"


class ArchiveSearchCoverageGrade(StrEnum):
    """Honest terminal completeness of the exact pages admitted in one turn."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class ArchiveSearchReauthorizationStatus(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    DRIFTED = "drifted"
    UNAVAILABLE = "unavailable"


class ArchiveSearchPublicationDenialReason(StrEnum):
    ACTOR_OR_RUN_MISMATCH = "actor_or_run_mismatch"
    CARRIER_INVALID = "carrier_invalid"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    MODEL_VISIBLE_BYTES_CHANGED = "model_visible_bytes_changed"
    AUTHORITY_CHANGED = "authority_changed"
    ANSWER_INVALID = "answer_invalid"


class ArchiveSearchPublicationDenied(ArchiveSearchAuthorityError):
    """Body-free closed denial for an answer produced from stale archive evidence."""

    def __init__(self, reason: ArchiveSearchPublicationDenialReason) -> None:
        if not isinstance(reason, ArchiveSearchPublicationDenialReason):
            reason = ArchiveSearchPublicationDenialReason.CARRIER_INVALID
        self.reason = reason
        super().__init__(f"archive publication denied: {reason.value}")


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive authority value is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive authority value is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive authority value is process-private")


def _actor_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ArchiveSearchAuthorityError("archive actor identity is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveSearchAuthorityError("archive actor identity is invalid") from None
    if len(encoded) > _MAX_ACTOR_ID_BYTES or any(ord(character) < 32 for character in value):
        raise ArchiveSearchAuthorityError("archive actor identity is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ArchiveSearchAuthorityError("archive authority material is invalid") from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mac(domain: bytes, value: bytes) -> str:
    return hmac.new(_PROCESS_KEY, domain + b"\0" + value, hashlib.sha256).hexdigest()


def _actor_handle(tenant_id: object, principal_id: object) -> str:
    return _mac(
        b"friday/archive-search-actor/v2",
        _canonical_json(
            {
                "principal_id": _actor_id(principal_id),
                "tenant_id": _actor_id(tenant_id),
            }
        ),
    )


def _request_handle(request: object) -> str:
    if type(request) is not ArchiveSearchRequest:
        raise ArchiveSearchAuthorityError("archive request is invalid")
    request_value = cast(ArchiveSearchRequest, request)
    try:
        material = request_value.to_private_json().encode("ascii")
    except Exception:
        raise ArchiveSearchAuthorityError("archive request identity is unavailable") from None
    return _mac(b"friday/archive-search-request/v2", material)


def _candidate_handle(candidate: object) -> str:
    if type(candidate) is not ArchiveSearchCandidate:
        raise ArchiveSearchAuthorityError("archive candidate is invalid")
    candidate_value = cast(ArchiveSearchCandidate, candidate)
    try:
        material = candidate_value.to_private_json().encode("ascii")
    except Exception:
        raise ArchiveSearchAuthorityError("archive candidate identity is unavailable") from None
    return _mac(b"friday/archive-search-candidate/v2", material)


def _coverage_handle(coverage: object) -> str:
    if type(coverage) is not SearchCoverage:
        raise ArchiveSearchAuthorityError("archive coverage is invalid")
    coverage_value = cast(SearchCoverage, coverage)
    try:
        material = coverage_value.to_json().encode("ascii")
    except Exception:
        raise ArchiveSearchAuthorityError("archive coverage identity is unavailable") from None
    return _mac(b"friday/archive-search-coverage/v2", material)


def _target_for_candidate_match(
    candidate: ArchiveSearchCandidate,
    match: ArchiveMatchRank,
) -> tuple[SearchCorpus, SearchLane]:
    return _CORPUS_TARGET[candidate.corpus], match.channel.search_lane


def _targets_match_request(
    request: ArchiveSearchRequest,
    binding: SearchExecutionBinding,
) -> bool:
    return binding.requested_targets == canonical_archive_search_targets(request)


def canonical_archive_search_targets(
    request: ArchiveSearchRequest,
) -> tuple[tuple[SearchCorpus, SearchLane], ...]:
    """Return the closed complete lane plan; omitted lanes must be UNAVAILABLE, never absent."""

    if type(request) is not ArchiveSearchRequest:
        raise ArchiveSearchAuthorityError("archive request is invalid")
    targets = {(_CORPUS_TARGET[corpus], lane) for corpus in request.corpora for lane in _CORPUS_LANES[corpus]}
    return tuple(sorted(targets, key=lambda item: (item[0].value, item[1].value)))


def _source_scope_matches(run: ArchiveSearchRunBinding, candidate: ArchiveSearchCandidate) -> bool:
    source = candidate.resolved_source.source_ref
    if source.authority_scope is AuthorityScope.TENANT:
        return source.tenant_id == run._tenant_id and source.principal_id is None
    if source.authority_scope is AuthorityScope.PRINCIPAL:
        return source.tenant_id is None and source.principal_id == run._principal_id
    return source.tenant_id == run._tenant_id and source.principal_id == run._principal_id


class ArchiveSearchRunBinding(_ProcessPrivate):
    """Exact actor/request/target/snapshot/run authority, usable only in this process."""

    __slots__ = (
        "_actor_handle",
        "_execution_binding",
        "_nonce",
        "_principal_id",
        "_privacy_key",
        "_request",
        "_request_handle",
        "_seal",
        "_snapshot_handle",
        "_tenant_id",
        "_turn_ledger",
        "_turn_ledger_handle",
    )

    _actor_handle: str
    _execution_binding: SearchExecutionBinding
    _nonce: bytes
    _principal_id: str
    _privacy_key: bytes
    _request: ArchiveSearchRequest
    _request_handle: str
    _seal: str
    _snapshot_handle: str
    _tenant_id: str
    _turn_ledger: ArchiveModelBatchLedger
    _turn_ledger_handle: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive run bindings require the canonical factory")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive run binding is immutable")

    def __repr__(self) -> str:
        return "<ArchiveSearchRunBinding sealed private>"

    @property
    def execution_binding(self) -> SearchExecutionBinding:
        """The exact immutable binding retrieval coverage must reference."""

        return self._execution_binding

    @property
    def turn_ledger(self) -> ArchiveModelBatchLedger:
        """Return the sole process-private admission ledger for this turn."""

        return self._turn_ledger


def _run_material(run: ArchiveSearchRunBinding) -> bytes:
    return _canonical_json(
        {
            "actor_handle": run._actor_handle,
            "execution_binding": run._execution_binding.to_payload(),
            "nonce": run._nonce.hex(),
            "privacy_key_digest": _mac(
                b"friday/archive-search-run-privacy-key-digest/v2",
                run._privacy_key,
            ),
            "request_handle": run._request_handle,
            "schema": _RUN_SCHEMA,
            "snapshot_handle": run._snapshot_handle,
            "turn_ledger_handle": run._turn_ledger_handle,
        }
    )


def _run_is_valid(
    value: object,
    *,
    tenant_id: object,
    principal_id: object,
) -> bool:
    if type(value) is not ArchiveSearchRunBinding:
        return False
    run = cast(ArchiveSearchRunBinding, value)
    try:
        if (
            type(run._request) is not ArchiveSearchRequest
            or type(run._execution_binding) is not SearchExecutionBinding
            or type(run._nonce) is not bytes
            or len(run._nonce) != _NONCE_BYTES
            or type(run._privacy_key) is not bytes
            or len(run._privacy_key) != 32
            or type(run._turn_ledger) is not ArchiveModelBatchLedger
            or _DIGEST.fullmatch(run._turn_ledger_handle) is None
            or not _ledger_identity_is_valid(run._turn_ledger)
            or not hmac.compare_digest(
                run._turn_ledger_handle,
                run._turn_ledger._identity_handle,
            )
            or run._execution_binding.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
            or not _targets_match_request(run._request, run._execution_binding)
            or not run._execution_binding.attests_private_request(run._request.to_identity_json())
            or _DIGEST.fullmatch(run._actor_handle) is None
            or _DIGEST.fullmatch(run._request_handle) is None
            or _DIGEST.fullmatch(run._snapshot_handle) is None
            or _DIGEST.fullmatch(run._seal) is None
        ):
            return False
        expected_actor = _actor_handle(run._tenant_id, run._principal_id)
        current_actor = _actor_handle(tenant_id, principal_id)
        checks = (
            hmac.compare_digest(run._actor_handle, expected_actor),
            hmac.compare_digest(run._actor_handle, current_actor),
            hmac.compare_digest(run._request_handle, _request_handle(run._request)),
            hmac.compare_digest(
                run._seal,
                _mac(b"friday/archive-search-run-binding/v2", _run_material(run)),
            ),
        )
        return all(checks)
    except Exception:
        return False


def create_archive_search_run_binding(
    *,
    tenant_id: str,
    principal_id: str,
    request: ArchiveSearchRequest,
    requested_targets: Iterable[tuple[SearchCorpus, SearchLane]],
    snapshot_discriminator: str,
    run_discriminator: str,
    turn_ledger: ArchiveModelBatchLedger,
) -> ArchiveSearchRunBinding:
    """Create one process-owned run and its exact ``SearchExecutionBinding``."""

    try:
        tenant = _actor_id(tenant_id)
        principal = _actor_id(principal_id)
        if not _ledger_accepts_new_run(
            turn_ledger,
            tenant_id=tenant,
            principal_id=principal,
        ):
            raise ArchiveSearchAuthorityError("archive turn ledger is unavailable")
        if type(request) is not ArchiveSearchRequest:
            raise ArchiveSearchAuthorityError("archive request is invalid")
        targets = tuple(requested_targets)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        private_seed = _canonical_json(
            {
                "actor": _actor_handle(tenant, principal),
                "nonce": nonce.hex(),
                "request": _request_handle(request),
                "run_discriminator": run_discriminator,
                "snapshot_discriminator": snapshot_discriminator,
                "targets": [(corpus.value, lane.value) for corpus, lane in targets],
            }
        )
        privacy_key = hmac.new(
            _PROCESS_KEY,
            b"friday/archive-search-run-privacy/v2\0" + private_seed,
            hashlib.sha256,
        ).digest()
        execution_binding = SearchExecutionBinding.create(
            normalized_private_request_json=request.to_identity_json(),
            authority_scope=AuthorityScope.TENANT_PRINCIPAL,
            tenant_id=tenant,
            principal_id=principal,
            requested_targets=targets,
            snapshot_discriminator=snapshot_discriminator,
            run_discriminator=run_discriminator,
            privacy_key=privacy_key,
        )
        if not _targets_match_request(request, execution_binding):
            raise ArchiveSearchAuthorityError("archive targets do not cover the request")
        run = cast(ArchiveSearchRunBinding, object.__new__(ArchiveSearchRunBinding))
        for name, item in (
            ("_actor_handle", _actor_handle(tenant, principal)),
            ("_execution_binding", execution_binding),
            ("_nonce", nonce),
            ("_principal_id", principal),
            ("_privacy_key", privacy_key),
            ("_request", request),
            ("_request_handle", _request_handle(request)),
            ("_seal", "0" * 64),
            (
                "_snapshot_handle",
                _mac(
                    b"friday/archive-search-snapshot-discriminator/v1",
                    snapshot_discriminator.encode("utf-8"),
                ),
            ),
            ("_tenant_id", tenant),
            ("_turn_ledger", turn_ledger),
            ("_turn_ledger_handle", turn_ledger._identity_handle),
        ):
            object.__setattr__(run, name, item)
        object.__setattr__(
            run,
            "_seal",
            _mac(b"friday/archive-search-run-binding/v2", _run_material(run)),
        )
        _register_run_with_ledger(turn_ledger, run)
        return run
    except Exception:
        raise ArchiveSearchAuthorityError("archive run binding creation failed") from None


class ArchiveSearchCandidateReauthorization(_ProcessPrivate):
    """Typed callback result; a successful result carries the exact fresh candidate."""

    __slots__ = ("_current", "_status")

    _current: ArchiveSearchCandidate | None
    _status: ArchiveSearchReauthorizationStatus

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("candidate reauthorization requires a closed factory")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("candidate reauthorization is immutable")

    def __repr__(self) -> str:
        return f"ArchiveSearchCandidateReauthorization(status={self._status.value!r}, private=True)"

    @property
    def status(self) -> ArchiveSearchReauthorizationStatus:
        return self._status

    @classmethod
    def authorized(cls, candidate: ArchiveSearchCandidate) -> ArchiveSearchCandidateReauthorization:
        if type(candidate) is not ArchiveSearchCandidate:
            raise ArchiveSearchAuthorityError("authorized candidate must use the exact contract")
        return _candidate_reauthorization(cls, ArchiveSearchReauthorizationStatus.AUTHORIZED, candidate)

    @classmethod
    def rejected(
        cls,
        status: ArchiveSearchReauthorizationStatus,
    ) -> ArchiveSearchCandidateReauthorization:
        if type(status) is not ArchiveSearchReauthorizationStatus or status not in {
            ArchiveSearchReauthorizationStatus.DENIED,
            ArchiveSearchReauthorizationStatus.DRIFTED,
            ArchiveSearchReauthorizationStatus.UNAVAILABLE,
        }:
            raise ArchiveSearchAuthorityError("candidate rejection status is invalid")
        return _candidate_reauthorization(cls, status, None)


def _candidate_reauthorization(
    cls: type[ArchiveSearchCandidateReauthorization],
    status: ArchiveSearchReauthorizationStatus,
    current: ArchiveSearchCandidate | None,
) -> ArchiveSearchCandidateReauthorization:
    result = cast(ArchiveSearchCandidateReauthorization, object.__new__(cls))
    object.__setattr__(result, "_status", status)
    object.__setattr__(result, "_current", current)
    return result


class ArchiveSearchCoverageReauthorization(_ProcessPrivate):
    """Typed callback result; a successful result carries exact fresh lane coverage."""

    __slots__ = ("_current", "_status")

    _current: SearchCoverage | None
    _status: ArchiveSearchReauthorizationStatus

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("coverage reauthorization requires a closed factory")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("coverage reauthorization is immutable")

    def __repr__(self) -> str:
        return f"ArchiveSearchCoverageReauthorization(status={self._status.value!r}, private=True)"

    @property
    def status(self) -> ArchiveSearchReauthorizationStatus:
        return self._status

    @classmethod
    def authorized(cls, coverage: SearchCoverage) -> ArchiveSearchCoverageReauthorization:
        if type(coverage) is not SearchCoverage:
            raise ArchiveSearchAuthorityError("authorized coverage must use the exact contract")
        return _coverage_reauthorization(cls, ArchiveSearchReauthorizationStatus.AUTHORIZED, coverage)

    @classmethod
    def rejected(
        cls,
        status: ArchiveSearchReauthorizationStatus,
    ) -> ArchiveSearchCoverageReauthorization:
        if type(status) is not ArchiveSearchReauthorizationStatus or status not in {
            ArchiveSearchReauthorizationStatus.DENIED,
            ArchiveSearchReauthorizationStatus.DRIFTED,
            ArchiveSearchReauthorizationStatus.UNAVAILABLE,
        }:
            raise ArchiveSearchAuthorityError("coverage rejection status is invalid")
        return _coverage_reauthorization(cls, status, None)


def _coverage_reauthorization(
    cls: type[ArchiveSearchCoverageReauthorization],
    status: ArchiveSearchReauthorizationStatus,
    current: SearchCoverage | None,
) -> ArchiveSearchCoverageReauthorization:
    result = cast(ArchiveSearchCoverageReauthorization, object.__new__(cls))
    object.__setattr__(result, "_status", status)
    object.__setattr__(result, "_current", current)
    return result


class ArchiveSearchCandidateReauthorizer(Protocol):
    def __call__(
        self,
        phase: ArchiveSearchAuthorityPhase,
        run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        authority_context: object,
        /,
    ) -> ArchiveSearchCandidateReauthorization: ...


class ArchiveSearchCoverageReauthorizer(Protocol):
    def __call__(
        self,
        phase: ArchiveSearchAuthorityPhase,
        run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        authority_context: object,
        /,
    ) -> ArchiveSearchCoverageReauthorization: ...


def _call_candidate_reauthorizer(
    callback: ArchiveSearchCandidateReauthorizer,
    phase: ArchiveSearchAuthorityPhase,
    run: ArchiveSearchRunBinding,
    candidate: ArchiveSearchCandidate,
    context: object,
) -> tuple[ArchiveSearchReauthorizationStatus, ArchiveSearchCandidate | None]:
    try:
        result = callback(phase, run, candidate, context)
        if type(result) is not ArchiveSearchCandidateReauthorization:
            raise TypeError
        status = result._status
        current = result._current
        if type(status) is not ArchiveSearchReauthorizationStatus:
            raise TypeError
        if status is ArchiveSearchReauthorizationStatus.AUTHORIZED:
            if type(current) is not ArchiveSearchCandidate:
                raise TypeError
        elif current is not None:
            raise TypeError
        return status, current
    except Exception:
        return ArchiveSearchReauthorizationStatus.UNAVAILABLE, None


def _call_coverage_reauthorizer(
    callback: ArchiveSearchCoverageReauthorizer,
    phase: ArchiveSearchAuthorityPhase,
    run: ArchiveSearchRunBinding,
    coverage: SearchCoverage,
    context: object,
) -> tuple[ArchiveSearchReauthorizationStatus, SearchCoverage | None]:
    try:
        result = callback(phase, run, coverage, context)
        if type(result) is not ArchiveSearchCoverageReauthorization:
            raise TypeError
        status = result._status
        current = result._current
        if type(status) is not ArchiveSearchReauthorizationStatus:
            raise TypeError
        if status is ArchiveSearchReauthorizationStatus.AUTHORIZED:
            if type(current) is not SearchCoverage:
                raise TypeError
        elif current is not None:
            raise TypeError
        return status, current
    except Exception:
        return ArchiveSearchReauthorizationStatus.UNAVAILABLE, None


def _same_candidate(left: ArchiveSearchCandidate, right: ArchiveSearchCandidate) -> bool:
    try:
        return hmac.compare_digest(_candidate_handle(left), _candidate_handle(right))
    except Exception:
        return False


def _same_coverage(left: SearchCoverage, right: SearchCoverage) -> bool:
    try:
        return hmac.compare_digest(_coverage_handle(left), _coverage_handle(right))
    except Exception:
        return False


def _merge_failure(
    failures: dict[tuple[SearchCorpus, SearchLane], set[ArchiveSearchReauthorizationStatus]],
    target: tuple[SearchCorpus, SearchLane],
    status: ArchiveSearchReauthorizationStatus,
) -> None:
    if status is not ArchiveSearchReauthorizationStatus.AUTHORIZED:
        failures.setdefault(target, set()).add(status)


def _candidate_with_matches(
    candidate: ArchiveSearchCandidate,
    matches: tuple[ArchiveMatchRank, ...],
) -> ArchiveSearchCandidate:
    if matches == candidate.matches:
        return candidate
    return ArchiveSearchCandidate.create(
        corpus=candidate.corpus,
        resolved_source=candidate.resolved_source,
        review_state=candidate.review_state,
        evidence_authority=candidate.evidence_authority,
        lifecycle_state=candidate.lifecycle_state,
        matches=matches,
        title=candidate.title,
        filename=candidate.filename,
        temporal_facts=candidate.temporal_facts,
        passages=candidate.passages,
    )


def _reauthorized_candidates(
    run: ArchiveSearchRunBinding,
    candidates: tuple[ArchiveSearchCandidate, ...],
    allowed_targets: set[tuple[SearchCorpus, SearchLane]],
    failures: dict[tuple[SearchCorpus, SearchLane], set[ArchiveSearchReauthorizationStatus]],
    callback: ArchiveSearchCandidateReauthorizer,
    context: object,
) -> tuple[ArchiveSearchCandidate, ...]:
    admitted: list[tuple[ArchiveSearchCandidate, tuple[ArchiveMatchRank, ...]]] = []
    for candidate in candidates:
        status, current = _call_candidate_reauthorizer(
            callback,
            ArchiveSearchAuthorityPhase.BEFORE_MODEL,
            run,
            candidate,
            context,
        )
        if status is ArchiveSearchReauthorizationStatus.AUTHORIZED and (
            current is None or not _same_candidate(candidate, current)
        ):
            status = ArchiveSearchReauthorizationStatus.DRIFTED
        if not _source_scope_matches(run, candidate):
            status = ArchiveSearchReauthorizationStatus.DENIED
        if status is not ArchiveSearchReauthorizationStatus.AUTHORIZED:
            for match in candidate.matches:
                _merge_failure(failures, _target_for_candidate_match(candidate, match), status)
            continue
        matches = tuple(
            match
            for match in candidate.matches
            if _target_for_candidate_match(candidate, match) in allowed_targets
        )
        if matches:
            admitted.append((candidate, matches))

    ranks: dict[tuple[int, SearchCorpus, SearchLane], int] = {}
    by_target: dict[tuple[SearchCorpus, SearchLane], list[tuple[int, int]]] = {}
    for candidate_index, (candidate, matches) in enumerate(admitted):
        for match in matches:
            target = _target_for_candidate_match(candidate, match)
            by_target.setdefault(target, []).append((match.rank, candidate_index))
    for target, rows in by_target.items():
        rows.sort()
        if len(rows) != len({rank for rank, _index in rows}):
            raise ArchiveSearchAuthorityError("archive lane ranks are invalid")
        for new_rank, (_old_rank, candidate_index) in enumerate(rows, 1):
            ranks[(candidate_index, *target)] = new_rank

    result: list[ArchiveSearchCandidate] = []
    for candidate_index, (candidate, matches) in enumerate(admitted):
        reranked = tuple(
            ArchiveMatchRank(
                match.channel,
                ranks[(candidate_index, *_target_for_candidate_match(candidate, match))],
            )
            for match in matches
        )
        result.append(_candidate_with_matches(candidate, reranked))
    return tuple(result)


def _degraded_coverage(
    coverage: SearchCoverage,
    failures: set[ArchiveSearchReauthorizationStatus],
    returned: int,
) -> SearchCoverage:
    states = {item for item in coverage.states if item is not CoverageState.COMPLETE}
    states.add(CoverageState.PARTIAL)
    if ArchiveSearchReauthorizationStatus.DENIED in failures:
        states.add(CoverageState.PERMISSION_FILTERED)
    if ArchiveSearchReauthorizationStatus.DRIFTED in failures:
        states.add(CoverageState.STALE)
    if ArchiveSearchReauthorizationStatus.UNAVAILABLE in failures:
        states.add(CoverageState.UNAVAILABLE)
    if coverage.limit is not None and returned > coverage.limit:
        raise ArchiveSearchAuthorityError("archive degraded coverage exceeds its limit")
    return SearchCoverage.create(
        corpus=coverage.corpus,
        lane=coverage.lane,
        execution_binding=coverage.execution_binding,
        states=states,
        eligible_authorized=None,
        examined=returned,
        matched_at_least=returned,
        returned=returned,
        authority_rechecked=ArchiveSearchReauthorizationStatus.UNAVAILABLE not in failures,
        snapshot_current=not bool(
            failures
            & {
                ArchiveSearchReauthorizationStatus.DRIFTED,
                ArchiveSearchReauthorizationStatus.UNAVAILABLE,
            }
        ),
        limit=coverage.limit,
        next_cursor_available=False,
    )


def _warnings_for_failures(
    warnings: Iterable[ArchiveSearchWarning],
    failures: Iterable[ArchiveSearchReauthorizationStatus],
) -> tuple[ArchiveSearchWarning, ...]:
    result = set(warnings)
    for status in failures:
        if status is ArchiveSearchReauthorizationStatus.DENIED:
            result.add(ArchiveSearchWarning.PERMISSION_FILTERED)
        elif status is ArchiveSearchReauthorizationStatus.DRIFTED:
            result.add(ArchiveSearchWarning.SNAPSHOT_CHANGED)
        elif status is ArchiveSearchReauthorizationStatus.UNAVAILABLE:
            result.add(ArchiveSearchWarning.LANE_UNAVAILABLE)
    return tuple(sorted(result, key=lambda item: item.value))


class AuthorizedArchiveBatch(_ProcessPrivate):
    """Sealed exact page which crossed the BEFORE_MODEL authority boundary."""

    __slots__ = (
        "_candidate_handles",
        "_coverage_handles",
        "_model_page_index",
        "_model_visible_bytes",
        "_nonce",
        "_page",
        "_run_handle",
        "_seal",
    )

    _candidate_handles: tuple[str, ...]
    _coverage_handles: tuple[str, ...]
    _model_page_index: int
    _model_visible_bytes: bytes
    _nonce: bytes
    _page: ArchiveSearchPage
    _run_handle: str
    _seal: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive model batches require BEFORE_MODEL authorization")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive model batch is immutable")

    def __repr__(self) -> str:
        return "<AuthorizedArchiveBatch sealed model-visible>"

    @property
    def model_visible_canonical_bytes(self) -> bytes:
        """Exact safe bytes that must be recorded beside the model tool message."""

        return bytes(self._model_visible_bytes)

    @property
    def public_tool_result_payload(self) -> dict[str, object]:
        """Return a fresh public ToolResult payload; private request/IDs are absent."""

        try:
            value = json.loads(self._model_visible_bytes)
        except Exception:
            raise ArchiveSearchAuthorityError("archive model payload is unavailable") from None
        if type(value) is not dict:
            raise ArchiveSearchAuthorityError("archive model payload is unavailable")
        return cast(dict[str, object], value)


def _batch_material(batch: AuthorizedArchiveBatch) -> bytes:
    return _canonical_json(
        {
            "candidate_handles": list(batch._candidate_handles),
            "coverage_handles": list(batch._coverage_handles),
            "model_page_index": batch._model_page_index,
            "model_visible_sha256": _sha256(batch._model_visible_bytes),
            "nonce": batch._nonce.hex(),
            "run_handle": batch._run_handle,
            "schema": _BATCH_SCHEMA,
        }
    )


def _run_model_page_index_unlocked(run: ArchiveSearchRunBinding) -> int:
    """Return the append-only run ordinal already sealed by the turn ledger."""

    ledger = run._turn_ledger
    positions = tuple(index for index, current in enumerate(ledger._runs, 1) if current is run)
    if len(positions) != 1 or not 1 <= positions[0] <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES:
        raise ArchiveSearchAuthorityError("archive model page index is unavailable")
    return positions[0]


def _run_model_page_index(run: ArchiveSearchRunBinding) -> int:
    ledger = run._turn_ledger
    with ledger._lock:
        if not _ledger_is_valid(ledger):
            raise ArchiveSearchAuthorityError("archive model page index is unavailable")
        return _run_model_page_index_unlocked(run)


def _project_model_visible_page(
    run: ArchiveSearchRunBinding,
    page: ArchiveSearchPage,
    *,
    model_page_index: int,
) -> bytes:
    """Project one page with turn-unique labels and no private ordinal carrier."""

    if type(model_page_index) is not int or not 1 <= model_page_index <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES:
        raise ArchiveSearchAuthorityError("archive model page index is invalid")
    try:
        payload = json.loads(page.to_public_json(run._privacy_key))
        if type(payload) is not dict:
            raise TypeError
        candidates = payload["candidates"]
        if type(candidates) is not list:
            raise TypeError
        label_offset = (model_page_index - 1) * ARCHIVE_AUTHORITY_MAX_CANDIDATES
        for local_index, raw_candidate in enumerate(candidates, 1):
            if type(raw_candidate) is not dict:
                raise TypeError
            candidate = cast(dict[str, object], raw_candidate)
            passages = candidate.get("passages")
            if candidate.get("label") != f"A{local_index}" or type(passages) is not list:
                raise TypeError
            public_ordinal = label_offset + local_index
            candidate["label"] = f"A{public_ordinal}"
            for passage_index, raw_passage in enumerate(passages, 1):
                if type(raw_passage) is not dict:
                    raise TypeError
                passage = cast(dict[str, object], raw_passage)
                if passage.get("label") != f"A{local_index}.{passage_index}":
                    raise TypeError
                passage["label"] = f"A{public_ordinal}.{passage_index}"
        return _canonical_json(payload)
    except Exception:
        raise ArchiveSearchAuthorityError("archive public projection failed") from None


def _new_batch(run: ArchiveSearchRunBinding, page: ArchiveSearchPage) -> AuthorizedArchiveBatch:
    try:
        model_page_index = _run_model_page_index(run)
        model_bytes = _project_model_visible_page(
            run,
            page,
            model_page_index=model_page_index,
        )
    except Exception:
        raise ArchiveSearchAuthorityError("archive public projection failed") from None
    if not model_bytes or len(model_bytes) > _MAX_MODEL_BYTES:
        raise ArchiveSearchAuthorityError("archive public projection is outside its closed limit")
    batch = cast(AuthorizedArchiveBatch, object.__new__(AuthorizedArchiveBatch))
    for name, item in (
        ("_candidate_handles", tuple(_candidate_handle(result.candidate) for result in page.results)),
        ("_coverage_handles", tuple(_coverage_handle(coverage) for coverage in page.coverage)),
        ("_model_page_index", model_page_index),
        ("_model_visible_bytes", model_bytes),
        ("_nonce", secrets.token_bytes(_NONCE_BYTES)),
        ("_page", page),
        ("_run_handle", run._seal),
        ("_seal", "0" * 64),
    ):
        object.__setattr__(batch, name, item)
    object.__setattr__(
        batch,
        "_seal",
        _mac(b"friday/archive-search-model-batch/v2", _batch_material(batch)),
    )
    return batch


def _batch_is_valid(batch: object, run: ArchiveSearchRunBinding) -> bool:
    if type(batch) is not AuthorizedArchiveBatch:
        return False
    value = cast(AuthorizedArchiveBatch, batch)
    try:
        if (
            type(value._page) is not ArchiveSearchPage
            or value._page.request is not run._request
            or type(value._candidate_handles) is not tuple
            or type(value._coverage_handles) is not tuple
            or type(value._model_page_index) is not int
            or not 1 <= value._model_page_index <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
            or type(value._model_visible_bytes) is not bytes
            or not value._model_visible_bytes
            or len(value._model_visible_bytes) > _MAX_MODEL_BYTES
            or type(value._nonce) is not bytes
            or len(value._nonce) != _NONCE_BYTES
            or _DIGEST.fullmatch(value._run_handle) is None
            or _DIGEST.fullmatch(value._seal) is None
            or not hmac.compare_digest(value._run_handle, run._seal)
            or any(
                coverage.execution_binding is not run._execution_binding for coverage in value._page.coverage
            )
        ):
            return False
        candidates = tuple(result.candidate for result in value._page.results)
        current_candidates = tuple(_candidate_handle(candidate) for candidate in candidates)
        current_coverages = tuple(_coverage_handle(coverage) for coverage in value._page.coverage)
        model_page_index = _run_model_page_index_unlocked(run)
        projected = _project_model_visible_page(
            run,
            value._page,
            model_page_index=model_page_index,
        )
        checks = (
            value._model_page_index == model_page_index,
            hmac.compare_digest(
                _canonical_json(list(value._candidate_handles)),
                _canonical_json(list(current_candidates)),
            ),
            hmac.compare_digest(
                _canonical_json(list(value._coverage_handles)),
                _canonical_json(list(current_coverages)),
            ),
            hmac.compare_digest(value._model_visible_bytes, projected),
            hmac.compare_digest(
                value._seal,
                _mac(b"friday/archive-search-model-batch/v2", _batch_material(value)),
            ),
        )
        return all(checks)
    except Exception:
        return False


class _ArchiveModelLedgerState(StrEnum):
    OPEN = "open"
    FROZEN = "frozen"
    CONSUMED = "consumed"


class ArchiveModelBatchLedger(_ProcessPrivate):
    """The sole turn-owned aggregate of exact archive bytes admitted to the model."""

    __slots__ = (
        "_actor_handle",
        "_created_at",
        "_entries",
        "_identity_handle",
        "_lock",
        "_nonce",
        "_principal_id",
        "_runs",
        "_seal",
        "_state",
        "_tenant_id",
        "_turn_handle",
    )

    _actor_handle: str
    _created_at: float
    _entries: tuple[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch, bytes], ...]
    _identity_handle: str
    _lock: threading.Lock
    _nonce: bytes
    _principal_id: str
    _runs: tuple[ArchiveSearchRunBinding, ...]
    _seal: str
    _state: _ArchiveModelLedgerState
    _tenant_id: str
    _turn_handle: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive model ledgers require the canonical factory")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive model ledger is immutable")

    def __repr__(self) -> str:
        return "<ArchiveModelBatchLedger sealed private>"

    def admit_model_tool_bytes(
        self,
        run_binding: ArchiveSearchRunBinding,
        batch: AuthorizedArchiveBatch,
        model_tool_bytes: bytes,
    ) -> None:
        """Append one batch iff these are exactly the bytes crossing the model boundary."""

        try:
            with self._lock:
                if (
                    not _ledger_is_valid(self)
                    or self._state is not _ArchiveModelLedgerState.OPEN
                    or type(batch) is not AuthorizedArchiveBatch
                    or type(model_tool_bytes) is not bytes
                    or len(self._entries) >= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
                    or sum(len(body) for _run, _batch, body in self._entries) + len(model_tool_bytes)
                    > ARCHIVE_AUTHORITY_MAX_MODEL_BYTES
                    or type(run_binding) is not ArchiveSearchRunBinding
                    or run_binding not in self._runs
                    or run_binding._turn_ledger is not self
                    or any(
                        existing_run is run_binding and existing_batch._nonce == batch._nonce
                        for existing_run, existing_batch, _body in self._entries
                    )
                    or any(existing_run is run_binding for existing_run, _batch, _body in self._entries)
                    or any(
                        existing_batch._nonce == batch._nonce for _run, existing_batch, _body in self._entries
                    )
                    or not _run_is_valid(
                        run_binding,
                        tenant_id=self._tenant_id,
                        principal_id=self._principal_id,
                    )
                    or not _batch_is_valid(batch, run_binding)
                    or not hmac.compare_digest(batch._model_visible_bytes, model_tool_bytes)
                ):
                    raise ArchiveSearchAuthorityError("archive model admission ledger rejected bytes")
                object.__setattr__(
                    self,
                    "_entries",
                    (*self._entries, (run_binding, batch, model_tool_bytes)),
                )
                _reseal_ledger(self)
        except Exception:
            raise ArchiveSearchAuthorityError("archive model admission ledger rejected bytes") from None

    def freeze_for_publication(self) -> None:
        """Close the append boundary before answer generation/publication attestation."""

        try:
            with self._lock:
                if (
                    not _ledger_is_valid(self)
                    or self._state is not _ArchiveModelLedgerState.OPEN
                    or not self._entries
                ):
                    raise ArchiveSearchAuthorityError("archive model ledger cannot be frozen")
                object.__setattr__(self, "_state", _ArchiveModelLedgerState.FROZEN)
                _reseal_ledger(self)
        except Exception:
            raise ArchiveSearchAuthorityError("archive model ledger cannot be frozen") from None


def _ledger_material(ledger: ArchiveModelBatchLedger) -> bytes:
    return _canonical_json(
        {
            "actor_handle": ledger._actor_handle,
            "entries": [
                {
                    "run_handle": run._seal,
                    "batch_seal": batch._seal,
                    "model_tool_sha256": _sha256(model_bytes),
                }
                for run, batch, model_bytes in ledger._entries
            ],
            "identity_handle": ledger._identity_handle,
            "nonce": ledger._nonce.hex(),
            "runs": [run._seal for run in ledger._runs],
            "schema": _LEDGER_SCHEMA,
            "state": ledger._state.value,
            "turn_handle": ledger._turn_handle,
        }
    )


def _reseal_ledger(ledger: ArchiveModelBatchLedger) -> None:
    object.__setattr__(
        ledger,
        "_seal",
        _mac(b"friday/archive-search-model-batch-ledger/v2", _ledger_material(ledger)),
    )


def _ledger_is_valid(value: object) -> bool:
    if type(value) is not ArchiveModelBatchLedger:
        return False
    ledger = cast(ArchiveModelBatchLedger, value)
    try:
        return bool(
            _ledger_identity_is_valid(ledger)
            and type(ledger._entries) is tuple
            and len(ledger._entries) <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
            and sum(len(entry[2]) for entry in ledger._entries) <= ARCHIVE_AUTHORITY_MAX_MODEL_BYTES
            and all(
                type(entry) is tuple
                and len(entry) == 3
                and type(entry[0]) is ArchiveSearchRunBinding
                and entry[0] in ledger._runs
                and entry[0]._turn_ledger is ledger
                and type(entry[1]) is AuthorizedArchiveBatch
                and type(entry[2]) is bytes
                and _run_is_valid(
                    entry[0],
                    tenant_id=ledger._tenant_id,
                    principal_id=ledger._principal_id,
                )
                and _batch_is_valid(entry[1], entry[0])
                and hmac.compare_digest(entry[1]._model_visible_bytes, entry[2])
                for entry in ledger._entries
            )
            and len({entry[1]._nonce for entry in ledger._entries}) == len(ledger._entries)
            and len({id(entry[0]) for entry in ledger._entries}) == len(ledger._entries)
            and type(ledger._runs) is tuple
            and len(ledger._runs) <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
            and len({run._seal for run in ledger._runs}) == len(ledger._runs)
            and all(
                type(run) is ArchiveSearchRunBinding
                and run._turn_ledger is ledger
                and hmac.compare_digest(run._turn_ledger_handle, ledger._identity_handle)
                for run in ledger._runs
            )
            and type(ledger._nonce) is bytes
            and len(ledger._nonce) == _NONCE_BYTES
            and type(ledger._lock) is type(threading.Lock())
            and type(ledger._state) is _ArchiveModelLedgerState
            and hmac.compare_digest(
                ledger._seal,
                _mac(b"friday/archive-search-model-batch-ledger/v2", _ledger_material(ledger)),
            )
        )
    except Exception:
        return False


_TURN_LEDGER_REGISTRY_LOCK = threading.Lock()
_TURN_LEDGER_REGISTRY: OrderedDict[tuple[str, str], ArchiveModelBatchLedger] = OrderedDict()


class _RotatingTurnReplayFilter:
    """Bounded Bloom epochs which retain replays without lifetime saturation."""

    __slots__ = (
        "_bucket_count",
        "_clock",
        "_current",
        "_epoch_seconds",
        "_epoch_started",
        "_filters",
    )

    def __init__(
        self,
        *,
        filter_bytes: int,
        retain_seconds: float,
        bucket_count: int,
        clock: Callable[[], float],
    ) -> None:
        if (
            type(filter_bytes) is not int
            or filter_bytes <= 0
            or not isinstance(retain_seconds, (int, float))
            or isinstance(retain_seconds, bool)
            or retain_seconds <= 0
            or type(bucket_count) is not int
            or bucket_count < 2
            or not callable(clock)
        ):
            raise ValueError("archive replay filter configuration is invalid")
        self._bucket_count = bucket_count
        self._clock = clock
        self._current = 0
        self._epoch_seconds = float(retain_seconds) / float(bucket_count - 1)
        self._epoch_started = float(clock())
        self._filters = [bytearray(filter_bytes) for _ in range(bucket_count)]

    def _rotate(self) -> None:
        now = float(self._clock())
        if now < self._epoch_started:
            for bucket in self._filters:
                bucket[:] = b"\0" * len(bucket)
            self._current = 0
            self._epoch_started = now
            return
        steps = int((now - self._epoch_started) // self._epoch_seconds)
        if steps <= 0:
            return
        if steps >= self._bucket_count:
            for bucket in self._filters:
                bucket[:] = b"\0" * len(bucket)
            self._current = 0
        else:
            for _ in range(steps):
                self._current = (self._current + 1) % self._bucket_count
                bucket = self._filters[self._current]
                bucket[:] = b"\0" * len(bucket)
        self._epoch_started += steps * self._epoch_seconds

    def _offsets(self, turn_handle: str) -> tuple[int, int, int, int]:
        digest = hashlib.sha256(
            b"friday/archive-search-consumed-turn/v3\0" + turn_handle.encode("ascii")
        ).digest()
        bit_count = len(self._filters[0]) * 8
        return (
            int.from_bytes(digest[0:4], "big") % bit_count,
            int.from_bytes(digest[4:8], "big") % bit_count,
            int.from_bytes(digest[8:12], "big") % bit_count,
            int.from_bytes(digest[12:16], "big") % bit_count,
        )

    def contains(self, turn_handle: str) -> bool:
        self._rotate()
        offsets = self._offsets(turn_handle)
        return any(
            all(bucket[offset // 8] & (1 << (offset % 8)) for offset in offsets) for bucket in self._filters
        )

    def add(self, turn_handle: str) -> None:
        self._rotate()
        bucket = self._filters[self._current]
        for offset in self._offsets(turn_handle):
            bucket[offset // 8] |= 1 << (offset % 8)


_TURN_LEDGER_CONSUMED_FILTER = _RotatingTurnReplayFilter(
    filter_bytes=_TURN_LEDGER_REPLAY_FILTER_BYTES,
    retain_seconds=_TURN_LEDGER_RETAIN_SECONDS,
    bucket_count=_TURN_LEDGER_REPLAY_FILTER_BUCKETS,
    clock=time.monotonic,
)


def _turn_was_consumed_locked(turn_handle: str) -> bool:
    return _TURN_LEDGER_CONSUMED_FILTER.contains(turn_handle)


def _mark_turn_consumed_locked(turn_handle: str) -> None:
    _TURN_LEDGER_CONSUMED_FILTER.add(turn_handle)


def _ledger_registry_key(ledger: ArchiveModelBatchLedger) -> tuple[str, str]:
    return ledger._actor_handle, ledger._turn_handle


def _ledger_identity_is_valid(value: object) -> bool:
    if type(value) is not ArchiveModelBatchLedger:
        return False
    ledger = cast(ArchiveModelBatchLedger, value)
    try:
        if (
            type(ledger._tenant_id) is not str
            or type(ledger._principal_id) is not str
            or type(ledger._created_at) is not float
            or type(ledger._nonce) is not bytes
            or len(ledger._nonce) != _NONCE_BYTES
            or _DIGEST.fullmatch(ledger._actor_handle) is None
            or _DIGEST.fullmatch(ledger._turn_handle) is None
            or _DIGEST.fullmatch(ledger._identity_handle) is None
            or not hmac.compare_digest(
                ledger._actor_handle,
                _actor_handle(ledger._tenant_id, ledger._principal_id),
            )
            or not hmac.compare_digest(
                ledger._identity_handle,
                _mac(
                    b"friday/archive-search-turn-ledger-identity/v2",
                    _canonical_json(
                        {
                            "actor_handle": ledger._actor_handle,
                            "nonce": ledger._nonce.hex(),
                            "turn_handle": ledger._turn_handle,
                        }
                    ),
                ),
            )
        ):
            return False
        with _TURN_LEDGER_REGISTRY_LOCK:
            return _TURN_LEDGER_REGISTRY.get(_ledger_registry_key(ledger)) is ledger
    except Exception:
        return False


def _ledger_accepts_new_run(
    value: object,
    *,
    tenant_id: str,
    principal_id: str,
) -> bool:
    if type(value) is not ArchiveModelBatchLedger:
        return False
    ledger = cast(ArchiveModelBatchLedger, value)
    try:
        with ledger._lock:
            return bool(
                _ledger_is_valid(ledger)
                and ledger._state is _ArchiveModelLedgerState.OPEN
                and len(ledger._runs) < ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
                and hmac.compare_digest(ledger._actor_handle, _actor_handle(tenant_id, principal_id))
            )
    except Exception:
        return False


def _register_run_with_ledger(
    ledger: ArchiveModelBatchLedger,
    run: ArchiveSearchRunBinding,
) -> None:
    with ledger._lock:
        if (
            not _ledger_is_valid(ledger)
            or ledger._state is not _ArchiveModelLedgerState.OPEN
            or run._turn_ledger is not ledger
            or run in ledger._runs
            or len(ledger._runs) >= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES
        ):
            raise ArchiveSearchAuthorityError("archive turn ledger rejected run")
        object.__setattr__(ledger, "_runs", (*ledger._runs, run))
        _reseal_ledger(ledger)


def create_archive_model_batch_ledger(
    *,
    tenant_id: str,
    principal_id: str,
    turn_discriminator: str,
) -> ArchiveModelBatchLedger:
    """Create once, or return the same sole ledger, for one exact actor/turn."""

    try:
        tenant = _actor_id(tenant_id)
        principal = _actor_id(principal_id)
        turn = _actor_id(turn_discriminator)
        actor = _actor_handle(tenant, principal)
        turn_handle = _mac(
            b"friday/archive-search-turn-discriminator/v2",
            _canonical_json({"actor_handle": actor, "turn_discriminator": turn}),
        )
        key = actor, turn_handle
        now = time.monotonic()
        with _TURN_LEDGER_REGISTRY_LOCK:
            existing = _TURN_LEDGER_REGISTRY.get(key)
            if existing is not None:
                _TURN_LEDGER_REGISTRY.move_to_end(key)
                if not _ledger_identity_is_valid_without_registry(existing):
                    raise ArchiveSearchAuthorityError("archive model ledger is unavailable")
                return existing
            if _turn_was_consumed_locked(turn_handle):
                raise ArchiveSearchAuthorityError("archive model ledger turn was consumed")
            while len(_TURN_LEDGER_REGISTRY) >= _TURN_LEDGER_MAX_RECORDS:
                oldest_key, oldest = next(iter(_TURN_LEDGER_REGISTRY.items()))
                if (
                    oldest._state is not _ArchiveModelLedgerState.CONSUMED
                    or now - oldest._created_at < _TURN_LEDGER_RETAIN_SECONDS
                ):
                    raise ArchiveSearchAuthorityError("archive model ledger registry is full")
                del _TURN_LEDGER_REGISTRY[oldest_key]
            ledger = cast(ArchiveModelBatchLedger, object.__new__(ArchiveModelBatchLedger))
            nonce = secrets.token_bytes(_NONCE_BYTES)
            identity = _mac(
                b"friday/archive-search-turn-ledger-identity/v2",
                _canonical_json(
                    {
                        "actor_handle": actor,
                        "nonce": nonce.hex(),
                        "turn_handle": turn_handle,
                    }
                ),
            )
            for name, value in (
                ("_actor_handle", actor),
                ("_created_at", now),
                ("_entries", ()),
                ("_identity_handle", identity),
                ("_lock", threading.Lock()),
                ("_nonce", nonce),
                ("_principal_id", principal),
                ("_runs", ()),
                ("_seal", "0" * 64),
                ("_state", _ArchiveModelLedgerState.OPEN),
                ("_tenant_id", tenant),
                ("_turn_handle", turn_handle),
            ):
                object.__setattr__(ledger, name, value)
            _reseal_ledger(ledger)
            _TURN_LEDGER_REGISTRY[key] = ledger
            return ledger
    except Exception:
        raise ArchiveSearchAuthorityError("archive model ledger creation failed") from None


def abandon_empty_archive_model_batch_ledger(ledger: ArchiveModelBatchLedger) -> None:
    """Consume and unregister one valid OPEN turn ledger that admitted no bytes.

    A handler can fail after its run has been bound but before a model-visible
    batch exists.  Such a turn must remain replay-closed without leaving an
    immortal OPEN entry in the process registry.  Non-empty, frozen, consumed,
    copied or foreign ledgers are deliberately outside this narrow transition.
    """

    try:
        if type(ledger) is not ArchiveModelBatchLedger:
            raise ArchiveSearchAuthorityError("archive model ledger cannot be abandoned")
        with ledger._lock:
            if (
                not _ledger_is_valid(ledger)
                or ledger._state is not _ArchiveModelLedgerState.OPEN
                or ledger._entries
            ):
                raise ArchiveSearchAuthorityError("archive model ledger cannot be abandoned")
            object.__setattr__(ledger, "_state", _ArchiveModelLedgerState.CONSUMED)
            _reseal_ledger(ledger)
            with _TURN_LEDGER_REGISTRY_LOCK:
                key = _ledger_registry_key(ledger)
                if _TURN_LEDGER_REGISTRY.get(key) is not ledger:
                    raise ArchiveSearchAuthorityError("archive model ledger cannot be abandoned")
                _mark_turn_consumed_locked(ledger._turn_handle)
                del _TURN_LEDGER_REGISTRY[key]
    except ArchiveSearchAuthorityError:
        raise
    except Exception:
        raise ArchiveSearchAuthorityError("archive model ledger cannot be abandoned") from None


def consume_archive_model_batch_ledger_fail_closed(ledger: ArchiveModelBatchLedger) -> None:
    """Replay-close and unregister a non-empty unpublished turn ledger.

    Cancellation after exact bytes reached a model has no answer to attest, but
    the same bytes and turn discriminator must never remain reusable.  This is
    intentionally not a publication shortcut: it returns no entries or
    attestation and accepts only the canonical registered OPEN/FROZEN ledger.
    """

    try:
        if type(ledger) is not ArchiveModelBatchLedger:
            raise ArchiveSearchAuthorityError("archive model ledger cannot be consumed")
        with ledger._lock:
            if (
                not _ledger_is_valid(ledger)
                or ledger._state not in {_ArchiveModelLedgerState.OPEN, _ArchiveModelLedgerState.FROZEN}
                or not ledger._entries
            ):
                raise ArchiveSearchAuthorityError("archive model ledger cannot be consumed")
            with _TURN_LEDGER_REGISTRY_LOCK:
                key = _ledger_registry_key(ledger)
                if _TURN_LEDGER_REGISTRY.get(key) is not ledger:
                    raise ArchiveSearchAuthorityError("archive model ledger cannot be consumed")
                object.__setattr__(ledger, "_state", _ArchiveModelLedgerState.CONSUMED)
                _reseal_ledger(ledger)
                _mark_turn_consumed_locked(ledger._turn_handle)
                del _TURN_LEDGER_REGISTRY[key]
    except ArchiveSearchAuthorityError:
        raise
    except Exception:
        raise ArchiveSearchAuthorityError("archive model ledger cannot be consumed") from None


def _ledger_identity_is_valid_without_registry(ledger: ArchiveModelBatchLedger) -> bool:
    try:
        return bool(
            type(ledger._tenant_id) is str
            and type(ledger._principal_id) is str
            and type(ledger._created_at) is float
            and type(ledger._nonce) is bytes
            and len(ledger._nonce) == _NONCE_BYTES
            and _DIGEST.fullmatch(ledger._actor_handle)
            and _DIGEST.fullmatch(ledger._turn_handle)
            and _DIGEST.fullmatch(ledger._identity_handle)
            and hmac.compare_digest(
                ledger._actor_handle,
                _actor_handle(ledger._tenant_id, ledger._principal_id),
            )
            and hmac.compare_digest(
                ledger._identity_handle,
                _mac(
                    b"friday/archive-search-turn-ledger-identity/v2",
                    _canonical_json(
                        {
                            "actor_handle": ledger._actor_handle,
                            "nonce": ledger._nonce.hex(),
                            "turn_handle": ledger._turn_handle,
                        }
                    ),
                ),
            )
        )
    except Exception:
        return False


def _consume_model_batch_ledger(
    ledger: ArchiveModelBatchLedger,
) -> tuple[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch, bytes], ...]:
    with ledger._lock:
        if (
            not _ledger_is_valid(ledger)
            or ledger._state is not _ArchiveModelLedgerState.FROZEN
            or not ledger._entries
        ):
            raise ArchiveSearchAuthorityError("archive model ledger is unavailable")
        entries = ledger._entries
        object.__setattr__(ledger, "_state", _ArchiveModelLedgerState.CONSUMED)
        _reseal_ledger(ledger)
        with _TURN_LEDGER_REGISTRY_LOCK:
            _mark_turn_consumed_locked(ledger._turn_handle)
        return entries


def _unregister_consumed_model_batch_ledger(ledger: ArchiveModelBatchLedger) -> None:
    """Drop private admitted bytes after the last publication callback.

    Replay closure lives in the rotating consumed-turn filter, not in a
    six-hour registry copy of the exact batches.  Keep the registered object
    alive through every phase-2 callback and attestation calculation because
    run validation depends on that process-private identity; remove it only
    once that transaction has completed (successfully or fail-closed).
    """

    try:
        if type(ledger) is not ArchiveModelBatchLedger:
            return
        with ledger._lock:
            if ledger._state is not _ArchiveModelLedgerState.CONSUMED:
                return
            key = _ledger_registry_key(ledger)
            with _TURN_LEDGER_REGISTRY_LOCK:
                if _TURN_LEDGER_REGISTRY.get(key) is ledger:
                    del _TURN_LEDGER_REGISTRY[key]
    except Exception:
        # Publication has already consumed the turn and replay-closed it.  A
        # cleanup failure must never manufacture a successful attestation or
        # replace the code-owned denial reason exposed to the caller.
        return


def _authorized_archive_search_page(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
    continuation_token: str | None,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> ArchiveSearchPage:
    if not _run_is_valid(run_binding, tenant_id=tenant_id, principal_id=principal_id):
        raise ArchiveSearchAuthorityError("archive run is unavailable")
    if (
        type(candidates) is not tuple
        or len(candidates) > ARCHIVE_AUTHORITY_MAX_CANDIDATES
        or any(type(candidate) is not ArchiveSearchCandidate for candidate in candidates)
        or type(coverage) is not tuple
        or not coverage
        or len(coverage) > ARCHIVE_AUTHORITY_MAX_COVERAGES
        or any(type(item) is not SearchCoverage for item in coverage)
        or type(warnings) is not tuple
        or any(type(item) is not ArchiveSearchWarning for item in warnings)
        or not callable(candidate_reauthorizer)
        or not callable(coverage_reauthorizer)
    ):
        raise ArchiveSearchAuthorityError("archive model admission inputs are invalid")
    targets = tuple((item.corpus, item.lane) for item in coverage)
    if (
        len(targets) != len(set(targets))
        or set(targets) != set(run_binding._execution_binding.requested_targets)
        or any(item.execution_binding is not run_binding._execution_binding for item in coverage)
    ):
        raise ArchiveSearchAuthorityError("archive coverage binding is invalid")

    current_coverage: dict[tuple[SearchCorpus, SearchLane], SearchCoverage] = {}
    failures: dict[tuple[SearchCorpus, SearchLane], set[ArchiveSearchReauthorizationStatus]] = {}
    for item in coverage:
        target = item.corpus, item.lane
        status, current = _call_coverage_reauthorizer(
            coverage_reauthorizer,
            ArchiveSearchAuthorityPhase.BEFORE_MODEL,
            run_binding,
            item,
            authority_context,
        )
        if status is ArchiveSearchReauthorizationStatus.AUTHORIZED and (
            current is None
            or (current.corpus, current.lane) != target
            or current.execution_binding is not run_binding._execution_binding
        ):
            status = ArchiveSearchReauthorizationStatus.DRIFTED
            current = None
        if status is ArchiveSearchReauthorizationStatus.AUTHORIZED and current is not None:
            if not current.authority_rechecked:
                status = ArchiveSearchReauthorizationStatus.UNAVAILABLE
            elif not current.snapshot_current:
                status = ArchiveSearchReauthorizationStatus.DRIFTED
            current_coverage[target] = current
        else:
            current_coverage[target] = item
        _merge_failure(failures, target, status)

    allowed_targets = set(current_coverage) - set(failures)
    admitted = _reauthorized_candidates(
        run_binding,
        candidates,
        allowed_targets,
        failures,
        candidate_reauthorizer,
        authority_context,
    )
    returned_by_target: dict[tuple[SearchCorpus, SearchLane], int] = {}
    for candidate in admitted:
        for match in candidate.matches:
            target = _target_for_candidate_match(candidate, match)
            returned_by_target[target] = returned_by_target.get(target, 0) + 1

    final_coverage = tuple(
        _degraded_coverage(item, failures[target], returned_by_target.get(target, 0))
        if target in failures
        else item
        for target, item in sorted(
            current_coverage.items(),
            key=lambda pair: (pair[0][0].value, pair[0][1].value),
        )
    )
    flat_failures = {status for statuses in failures.values() for status in statuses}
    final_warnings = _warnings_for_failures(warnings, flat_failures)
    has_continuation = any(item.next_cursor_available for item in final_coverage)
    if (continuation_token is not None) != has_continuation:
        raise ArchiveSearchAuthorityError("archive continuation coverage is inconsistent")
    return ArchiveSearchPage.create(
        request=run_binding._request,
        candidates=admitted,
        coverage=final_coverage,
        warnings=final_warnings,
        continuation=continuation_token,
    )


def _new_live_archive_batch(
    run: ArchiveSearchRunBinding,
    page: ArchiveSearchPage,
) -> AuthorizedArchiveBatch:
    continuation_token = page.continuation
    if continuation_token is not None and not _CONTINUATION_REGISTRY.token_is_live(
        continuation_token,
        actor_handle=run._actor_handle,
        request_identity_handle=_request_identity_handle(run._request),
        expected_targets={(item.corpus, item.lane) for item in page.coverage if item.next_cursor_available},
    ):
        raise ArchiveSearchAuthorityError("archive continuation is unavailable")
    return _new_batch(run, page)


def _authorize_archive_search_before_model(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
    continuation_token: str | None,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> AuthorizedArchiveBatch:
    page = _authorized_archive_search_page(
        tenant_id=tenant_id,
        principal_id=principal_id,
        run_binding=run_binding,
        candidates=candidates,
        coverage=coverage,
        warnings=warnings,
        continuation_token=continuation_token,
        candidate_reauthorizer=candidate_reauthorizer,
        coverage_reauthorizer=coverage_reauthorizer,
        authority_context=authority_context,
    )
    return _new_live_archive_batch(run_binding, page)


def authorize_archive_search_before_model(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...] = (),
    continuation: IssuedArchiveContinuation | None = None,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> AuthorizedArchiveBatch:
    """Reauthorize, filter and seal the exact page before any model can see it."""

    token: str | None = None
    try:
        # An inbound continuation is useful only after the registry has
        # redeemed the exact token and selected its sealed tail.  The ordinary
        # first-page gate must never accept a hand-built continued request.
        if run_binding._request.continuation is not None:
            raise ArchiveSearchAuthorityError("archive continuation requires redemption")
        if continuation is not None and type(continuation) is not IssuedArchiveContinuation:
            raise ArchiveSearchAuthorityError("archive continuation issue is invalid")
        token = (
            None if continuation is None else _CONTINUATION_REGISTRY.claim_issue(continuation, run_binding)
        )
        try:
            return _authorize_archive_search_before_model(
                tenant_id=tenant_id,
                principal_id=principal_id,
                run_binding=run_binding,
                candidates=candidates,
                coverage=coverage,
                warnings=warnings,
                continuation_token=token,
                candidate_reauthorizer=candidate_reauthorizer,
                coverage_reauthorizer=coverage_reauthorizer,
                authority_context=authority_context,
            )
        except Exception:
            if token is not None:
                _CONTINUATION_REGISTRY.revoke_token(token)
            raise
    except Exception:
        if token is not None:
            _CONTINUATION_REGISTRY.revoke_token(token)
        elif type(continuation) is IssuedArchiveContinuation:
            with suppress(Exception):
                _CONTINUATION_REGISTRY.revoke_token(continuation._token)
        raise ArchiveSearchAuthorityError("archive model admission failed") from None


def _continuation_token_handle(value: object) -> str:
    if type(value) is not str or not value or len(value) > 512:
        raise ArchiveSearchAuthorityError("archive continuation token is invalid")
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveSearchAuthorityError("archive continuation token is invalid") from None
    if any(
        character not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in encoded
    ):
        raise ArchiveSearchAuthorityError("archive continuation token is invalid")
    return _mac(b"friday/archive-search-continuation-token/v2", encoded)


def _continuation_token_has_public_shape(value: object) -> bool:
    if type(value) is not str or len(value) != _CONTINUATION_TOKEN_CHARS:
        return False
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        return False
    return bool(
        len(encoded) == _CONTINUATION_TOKEN_CHARS
        and all(
            character in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in encoded
        )
    )


def _continuation_probe_token(inbound_token: str) -> str:
    probe = "A" * _CONTINUATION_TOKEN_CHARS
    if probe == inbound_token:
        probe = "B" * _CONTINUATION_TOKEN_CHARS
    return probe


def _request_identity_handle(request: object) -> str:
    if type(request) is not ArchiveSearchRequest:
        raise ArchiveSearchAuthorityError("archive request identity is unavailable")
    try:
        material = cast(ArchiveSearchRequest, request).to_identity_json().encode("ascii")
    except Exception:
        raise ArchiveSearchAuthorityError("archive request identity is unavailable") from None
    return _mac(b"friday/archive-search-request-identity/v2", material)


class _ArchiveContinuationRecord(_ProcessPrivate):
    __slots__ = (
        "actor_handle",
        "candidate_handles",
        "candidates",
        "coverage",
        "coverage_handles",
        "expires_at",
        "issued_at",
        "registry_generation",
        "request_identity_handle",
        "seal",
        "snapshot_handle",
        "token",
        "token_handle",
        "warnings",
    )

    actor_handle: str
    candidate_handles: tuple[str, ...]
    candidates: tuple[ArchiveSearchCandidate, ...]
    coverage: tuple[SearchCoverage, ...]
    coverage_handles: tuple[str, ...]
    expires_at: float
    issued_at: float
    registry_generation: bytes
    request_identity_handle: str
    seal: str
    snapshot_handle: str
    token: str
    token_handle: str
    warnings: tuple[ArchiveSearchWarning, ...]

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive continuation record is immutable")

    def __repr__(self) -> str:
        return "<_ArchiveContinuationRecord sealed private>"


def _continuation_record_material(record: _ArchiveContinuationRecord) -> bytes:
    return _canonical_json(
        {
            "actor_handle": record.actor_handle,
            "candidate_handles": list(record.candidate_handles),
            "coverage_handles": list(record.coverage_handles),
            "expires_at": record.expires_at.hex(),
            "issued_at": record.issued_at.hex(),
            "registry_generation": record.registry_generation.hex(),
            "request_identity_handle": record.request_identity_handle,
            "schema": _CONTINUATION_RECORD_SCHEMA,
            "snapshot_handle": record.snapshot_handle,
            "token_handle": record.token_handle,
            "warnings": [item.value for item in record.warnings],
        }
    )


def _continuation_record_is_valid(
    value: object,
    *,
    generation: bytes,
) -> bool:
    if type(value) is not _ArchiveContinuationRecord:
        return False
    record = cast(_ArchiveContinuationRecord, value)
    try:
        return bool(
            type(record.candidates) is tuple
            and 0 < len(record.candidates) <= ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL
            and all(type(item) is ArchiveSearchCandidate for item in record.candidates)
            and type(record.coverage) is tuple
            and record.coverage
            and len(record.coverage) <= ARCHIVE_AUTHORITY_MAX_COVERAGES
            and all(type(item) is SearchCoverage for item in record.coverage)
            and type(record.warnings) is tuple
            and all(type(item) is ArchiveSearchWarning for item in record.warnings)
            and type(record.issued_at) is float
            and type(record.expires_at) is float
            and record.expires_at > record.issued_at
            and type(record.registry_generation) is bytes
            and hmac.compare_digest(record.registry_generation, generation)
            and _DIGEST.fullmatch(record.actor_handle)
            and _DIGEST.fullmatch(record.request_identity_handle)
            and _DIGEST.fullmatch(record.snapshot_handle)
            and _DIGEST.fullmatch(record.token_handle)
            and _DIGEST.fullmatch(record.seal)
            and _continuation_token_has_public_shape(record.token)
            and hmac.compare_digest(record.token_handle, _continuation_token_handle(record.token))
            and hmac.compare_digest(
                _canonical_json(list(record.candidate_handles)),
                _canonical_json([_candidate_handle(item) for item in record.candidates]),
            )
            and hmac.compare_digest(
                _canonical_json(list(record.coverage_handles)),
                _canonical_json([_coverage_handle(item) for item in record.coverage]),
            )
            and hmac.compare_digest(
                record.seal,
                _mac(
                    b"friday/archive-search-continuation-record/v3",
                    _continuation_record_material(record),
                ),
            )
        )
    except Exception:
        return False


def _validate_continuation_tail(
    run: ArchiveSearchRunBinding,
    candidates: object,
    coverage: object,
    warnings: object,
) -> tuple[
    tuple[ArchiveSearchCandidate, ...],
    tuple[SearchCoverage, ...],
    tuple[ArchiveSearchWarning, ...],
]:
    if (
        type(candidates) is not tuple
        or not candidates
        or len(candidates) > ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL
        or any(type(item) is not ArchiveSearchCandidate for item in candidates)
        or type(coverage) is not tuple
        or not coverage
        or len(coverage) > ARCHIVE_AUTHORITY_MAX_COVERAGES
        or any(type(item) is not SearchCoverage for item in coverage)
        or type(warnings) is not tuple
        or any(type(item) is not ArchiveSearchWarning for item in warnings)
    ):
        raise ArchiveSearchAuthorityError("archive continuation tail is invalid")
    candidate_values = cast(tuple[ArchiveSearchCandidate, ...], candidates)
    coverage_values = cast(tuple[SearchCoverage, ...], coverage)
    warning_values = cast(tuple[ArchiveSearchWarning, ...], warnings)
    try:
        sources = tuple(item.resolved_source.source_ref for item in candidate_values)
        if (
            len(sources) != len(set(sources))
            or any(item.corpus not in run._request.corpora for item in candidate_values)
            or any(not _source_scope_matches(run, item) for item in candidate_values)
            or any(item.next_cursor_available for item in coverage_values)
        ):
            raise ArchiveSearchAuthorityError("archive continuation tail is invalid")
        bindings = {id(item.execution_binding): item.execution_binding for item in coverage_values}
        if len(bindings) != 1:
            raise ArchiveSearchAuthorityError("archive continuation tail is invalid")
        binding = next(iter(bindings.values()))
        targets = {(item.corpus, item.lane) for item in coverage_values}
        if (
            binding is not run._execution_binding
            or targets != set(run._execution_binding.requested_targets)
            or any(item.execution_binding is not binding for item in coverage_values)
        ):
            raise ArchiveSearchAuthorityError("archive continuation tail is invalid")
        matched_by_target: dict[tuple[SearchCorpus, SearchLane], int] = {}
        for candidate in candidate_values:
            for match in candidate.matches:
                target = _target_for_candidate_match(candidate, match)
                matched_by_target[target] = matched_by_target.get(target, 0) + 1
        coverage_by_target = {(item.corpus, item.lane): item for item in coverage_values}
        if any(
            target not in coverage_by_target or count > coverage_by_target[target].matched_at_least
            for target, count in matched_by_target.items()
        ):
            raise ArchiveSearchAuthorityError("archive continuation tail is invalid")
    except ArchiveSearchAuthorityError:
        raise
    except Exception:
        raise ArchiveSearchAuthorityError("archive continuation tail is invalid") from None
    return candidate_values, coverage_values, warning_values


class IssuedArchiveContinuation(_ProcessPrivate):
    """One-use private proof that a public cursor has a live materialized tail."""

    __slots__ = (
        "_claimed",
        "_lock",
        "_record_seal",
        "_registry_generation",
        "_run_handle",
        "_seal",
        "_token",
        "_token_handle",
    )

    _claimed: bool
    _lock: threading.Lock
    _record_seal: str
    _registry_generation: bytes
    _run_handle: str
    _seal: str
    _token: str
    _token_handle: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive continuation issues require the canonical registry")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive continuation issue is immutable")

    def __repr__(self) -> str:
        return "<IssuedArchiveContinuation sealed one-use private>"


def _issued_material(issue: IssuedArchiveContinuation) -> bytes:
    return _canonical_json(
        {
            "claimed": issue._claimed,
            "record_seal": issue._record_seal,
            "registry_generation": issue._registry_generation.hex(),
            "run_handle": issue._run_handle,
            "schema": _CONTINUATION_ISSUE_SCHEMA,
            "token_handle": issue._token_handle,
        }
    )


def _reseal_issue(issue: IssuedArchiveContinuation) -> None:
    object.__setattr__(
        issue,
        "_seal",
        _mac(b"friday/archive-search-continuation-issue/v3", _issued_material(issue)),
    )


class _ArchiveContinuationRegistry(_ProcessPrivate):
    __slots__ = (
        "_clock",
        "_generation",
        "_issued",
        "_lock",
        "_records",
        "_total_candidates",
        "_ttl_seconds",
    )

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _CONTINUATION_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._generation = secrets.token_bytes(_NONCE_BYTES)
        self._issued: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._records: OrderedDict[str, _ArchiveContinuationRecord] = OrderedDict()
        self._total_candidates = 0
        self._ttl_seconds = float(ttl_seconds)

    def __repr__(self) -> str:
        return "<_ArchiveContinuationRegistry bounded private>"

    def _recount_locked(self) -> None:
        self._total_candidates = sum(len(record.candidates) for record in self._records.values())

    def _purge_locked(self, now: float) -> None:
        expired = [
            token_handle
            for token_handle, record in self._records.items()
            if record.expires_at <= now
            or not _continuation_record_is_valid(record, generation=self._generation)
        ]
        for token_handle in expired:
            self._records.pop(token_handle)
        self._recount_locked()
        for issue_key, expires_at in tuple(self._issued.items()):
            if expires_at <= now:
                del self._issued[issue_key]

    def _mint_record_locked(
        self,
        *,
        actor_handle: str,
        request_identity_handle: str,
        snapshot_handle: str,
        candidates: tuple[ArchiveSearchCandidate, ...],
        coverage: tuple[SearchCoverage, ...],
        warnings: tuple[ArchiveSearchWarning, ...],
        now: float,
        exclude_token_handle: str | None = None,
    ) -> _ArchiveContinuationRecord:
        while (
            len(self._records) >= _CONTINUATION_MAX_RECORDS
            or self._total_candidates + len(candidates) > _CONTINUATION_MAX_TOTAL_CANDIDATES
        ):
            if not self._records:
                raise ArchiveSearchAuthorityError("archive continuation registry is full")
            self._records.popitem(last=False)
            self._recount_locked()
        for _attempt in range(8):
            token = secrets.token_urlsafe(_CONTINUATION_TOKEN_BYTES)
            if not _continuation_token_has_public_shape(token):
                continue
            token_handle = _continuation_token_handle(token)
            if token_handle not in self._records and (
                exclude_token_handle is None or not hmac.compare_digest(token_handle, exclude_token_handle)
            ):
                break
        else:
            raise ArchiveSearchAuthorityError("archive continuation token allocation failed")
        record = object.__new__(_ArchiveContinuationRecord)
        for name, value in (
            ("actor_handle", actor_handle),
            ("candidate_handles", tuple(_candidate_handle(item) for item in candidates)),
            ("candidates", candidates),
            ("coverage", coverage),
            ("coverage_handles", tuple(_coverage_handle(item) for item in coverage)),
            ("expires_at", now + self._ttl_seconds),
            ("issued_at", now),
            ("registry_generation", self._generation),
            ("request_identity_handle", request_identity_handle),
            ("seal", "0" * 64),
            ("snapshot_handle", snapshot_handle),
            ("token", token),
            ("token_handle", token_handle),
            ("warnings", warnings),
        ):
            object.__setattr__(record, name, value)
        object.__setattr__(
            record,
            "seal",
            _mac(
                b"friday/archive-search-continuation-record/v3",
                _continuation_record_material(record),
            ),
        )
        self._records[token_handle] = record
        self._total_candidates += len(candidates)
        return record

    def issue(
        self,
        *,
        run: ArchiveSearchRunBinding,
        candidates: tuple[ArchiveSearchCandidate, ...],
        coverage: tuple[SearchCoverage, ...],
        warnings: tuple[ArchiveSearchWarning, ...],
    ) -> IssuedArchiveContinuation:
        now = float(self._clock())
        issue_key = _mac(
            b"friday/archive-search-continuation-mint-once/v3",
            _canonical_json(
                {
                    "candidate_handles": [_candidate_handle(item) for item in candidates],
                    "coverage_handles": [_coverage_handle(item) for item in coverage],
                    "run_handle": run._seal,
                }
            ),
        )
        with self._lock:
            self._purge_locked(now)
            if issue_key in self._issued:
                raise ArchiveSearchAuthorityError("archive continuation was already minted")
            if len(self._issued) >= _CONTINUATION_MAX_ISSUANCE_KEYS:
                raise ArchiveSearchAuthorityError("archive continuation registry is full")
            record = self._mint_record_locked(
                actor_handle=run._actor_handle,
                request_identity_handle=_request_identity_handle(run._request),
                snapshot_handle=run._snapshot_handle,
                candidates=candidates,
                coverage=coverage,
                warnings=warnings,
                now=now,
            )
            self._issued[issue_key] = record.expires_at
        issue = cast(IssuedArchiveContinuation, object.__new__(IssuedArchiveContinuation))
        for name, value in (
            ("_claimed", False),
            ("_lock", threading.Lock()),
            ("_record_seal", record.seal),
            ("_registry_generation", self._generation),
            ("_run_handle", run._seal),
            ("_seal", "0" * 64),
            ("_token", record.token),
            ("_token_handle", record.token_handle),
        ):
            object.__setattr__(issue, name, value)
        _reseal_issue(issue)
        return issue

    def claim_issue(
        self,
        issue: IssuedArchiveContinuation,
        run: ArchiveSearchRunBinding,
    ) -> str:
        with issue._lock, self._lock:
            now = float(self._clock())
            self._purge_locked(now)
            record = self._records.get(issue._token_handle)
            if (
                type(issue) is not IssuedArchiveContinuation
                or issue._claimed
                or type(issue._lock) is not type(threading.Lock())
                or not hmac.compare_digest(issue._registry_generation, self._generation)
                or not hmac.compare_digest(issue._run_handle, run._seal)
                or not hmac.compare_digest(issue._token_handle, _continuation_token_handle(issue._token))
                or not hmac.compare_digest(
                    issue._seal,
                    _mac(
                        b"friday/archive-search-continuation-issue/v3",
                        _issued_material(issue),
                    ),
                )
                or record is None
                or not _continuation_record_is_valid(record, generation=self._generation)
                or record.expires_at <= now
                or not hmac.compare_digest(record.seal, issue._record_seal)
                or not hmac.compare_digest(record.actor_handle, run._actor_handle)
                or not hmac.compare_digest(
                    record.request_identity_handle,
                    _request_identity_handle(run._request),
                )
                or not hmac.compare_digest(record.snapshot_handle, run._snapshot_handle)
            ):
                raise ArchiveSearchAuthorityError("archive continuation issue is unavailable")
            self._records.move_to_end(issue._token_handle)
            object.__setattr__(issue, "_claimed", True)
            _reseal_issue(issue)
            return issue._token

    def mint_selected_child(
        self,
        *,
        run: ArchiveSearchRunBinding,
        selection: _SelectedArchiveContinuation,
    ) -> _ArchiveContinuationRecord:
        """Consume one post-reauthorization selection and mint exactly one child."""

        if type(selection) is not _SelectedArchiveContinuation:
            raise ArchiveSearchAuthorityError("archive continuation child is unavailable")
        with selection._lock:
            now = float(self._clock())
            with self._lock:
                self._purge_locked(now)
                if (
                    not _selected_continuation_is_valid(
                        selection,
                        run,
                        registry_generation=self._generation,
                    )
                    or selection._claimed
                ):
                    raise ArchiveSearchAuthorityError("archive continuation child is unavailable")
                # Claim before allocation.  A full/failed registry therefore
                # leaves no reusable capability and can never mint later.
                object.__setattr__(selection, "_claimed", True)
                _reseal_selected_continuation(selection)
                return self._mint_record_locked(
                    actor_handle=run._actor_handle,
                    request_identity_handle=_request_identity_handle(run._request),
                    snapshot_handle=run._snapshot_handle,
                    candidates=selection._candidates,
                    coverage=selection._coverage,
                    warnings=selection._warnings,
                    now=now,
                    exclude_token_handle=selection._parent_token_handle,
                )

    def revoke_token(self, token: object) -> None:
        try:
            token_handle = _continuation_token_handle(token)
        except Exception:
            return
        with self._lock:
            record = self._records.pop(token_handle, None)
            if record is not None:
                self._recount_locked()

    def token_is_live(
        self,
        token: object,
        *,
        actor_handle: str,
        request_identity_handle: str,
        record_seal: str | None = None,
        expected_targets: set[tuple[SearchCorpus, SearchLane]] | None = None,
    ) -> bool:
        try:
            token_handle = _continuation_token_handle(token)
            with self._lock:
                now = float(self._clock())
                self._purge_locked(now)
                record = self._records.get(token_handle)
                record_targets = (
                    {
                        _target_for_candidate_match(candidate, match)
                        for candidate in record.candidates
                        for match in candidate.matches
                    }
                    if record is not None
                    else set()
                )
                if (
                    record is None
                    or not _continuation_record_is_valid(record, generation=self._generation)
                    or record.expires_at <= now
                    or not hmac.compare_digest(record.actor_handle, actor_handle)
                    or not hmac.compare_digest(
                        record.request_identity_handle,
                        request_identity_handle,
                    )
                    or (record_seal is not None and not hmac.compare_digest(record.seal, record_seal))
                    or (expected_targets is not None and record_targets != expected_targets)
                ):
                    return False
                self._records.move_to_end(token_handle)
                return True
        except Exception:
            return False

    def redeem(
        self,
        *,
        tenant_id: object,
        principal_id: object,
        run: object,
    ) -> RedeemedArchiveContinuation:
        # The lookup record is removed before actor, run, expiry, or seal checks.
        try:
            request = cast(ArchiveSearchRunBinding, run)._request
            inbound_token = request.continuation
            token_handle = _continuation_token_handle(inbound_token)
        except Exception:
            raise ArchiveSearchAuthorityError("archive continuation redemption failed") from None
        with self._lock:
            now = float(self._clock())
            record = self._records.pop(token_handle, None)
            if record is not None:
                self._recount_locked()
        if record is None:
            raise ArchiveSearchAuthorityError("archive continuation redemption failed")
        try:
            tenant = _actor_id(tenant_id)
            principal = _actor_id(principal_id)
            if (
                not _continuation_record_is_valid(record, generation=self._generation)
                or record.expires_at <= now
                or not _run_is_valid(run, tenant_id=tenant, principal_id=principal)
            ):
                raise ArchiveSearchAuthorityError("archive continuation redemption failed")
            run_value = cast(ArchiveSearchRunBinding, run)
            if (
                not hmac.compare_digest(record.token_handle, token_handle)
                or not hmac.compare_digest(record.actor_handle, run_value._actor_handle)
                or not hmac.compare_digest(
                    record.request_identity_handle,
                    _request_identity_handle(run_value._request),
                )
                or not hmac.compare_digest(record.snapshot_handle, run_value._snapshot_handle)
                or not _stored_coverage_matches_resumed_run(record.coverage, run_value)
            ):
                raise ArchiveSearchAuthorityError("archive continuation redemption failed")
            return _new_redemption(
                run=run_value,
                parent=record,
                candidates=record.candidates,
                coverage=record.coverage,
                warnings=record.warnings,
                registry_generation=self._generation,
            )
        except Exception:
            raise ArchiveSearchAuthorityError("archive continuation redemption failed") from None


_CONTINUATION_REGISTRY = _ArchiveContinuationRegistry()


def _stored_coverage_matches_resumed_run(
    coverage: tuple[SearchCoverage, ...],
    run: ArchiveSearchRunBinding,
) -> bool:
    try:
        bindings = {id(item.execution_binding): item.execution_binding for item in coverage}
        if len(bindings) != 1:
            return False
        prior = next(iter(bindings.values()))
        return bool(
            type(prior) is SearchExecutionBinding
            and prior.authority_scope is AuthorityScope.TENANT_PRINCIPAL
            and prior.requested_targets == run._execution_binding.requested_targets
            and prior.attests_private_request(run._request.to_identity_json())
            and {(item.corpus, item.lane) for item in coverage} == set(prior.requested_targets)
            and all(item.execution_binding is prior for item in coverage)
        )
    except Exception:
        return False


def _continuation_page_coverage(
    run: ArchiveSearchRunBinding,
    terminal: tuple[SearchCoverage, ...],
    candidates: tuple[ArchiveSearchCandidate, ...],
    *,
    child_candidates: tuple[ArchiveSearchCandidate, ...],
) -> tuple[SearchCoverage, ...]:
    returned: dict[tuple[SearchCorpus, SearchLane], int] = {}
    for candidate in candidates:
        for match in candidate.matches:
            target = _target_for_candidate_match(candidate, match)
            returned[target] = returned.get(target, 0) + 1
    continuing_targets = {
        _target_for_candidate_match(candidate, match)
        for candidate in child_candidates
        for match in candidate.matches
    }
    rebound: list[SearchCoverage] = []
    for item in terminal:
        target = item.corpus, item.lane
        count = returned.get(target, 0)
        if count > item.matched_at_least:
            raise ArchiveSearchAuthorityError("archive continuation coverage is inconsistent")
        states: set[CoverageState]
        if target in continuing_targets:
            states = {state for state in item.states if state is not CoverageState.COMPLETE}
            states.update({CoverageState.PARTIAL, CoverageState.CAPPED})
            limit: int | None = run._request.limit
        else:
            states = set(item.states)
            limit = item.limit if item.limit is None or item.limit >= count else run._request.limit
        rebound.append(
            SearchCoverage.create(
                corpus=item.corpus,
                lane=item.lane,
                execution_binding=run._execution_binding,
                states=states,
                eligible_authorized=item.eligible_authorized,
                examined=item.examined,
                matched_at_least=item.matched_at_least,
                returned=count,
                authority_rechecked=item.authority_rechecked,
                snapshot_current=item.snapshot_current,
                limit=limit,
                next_cursor_available=target in continuing_targets,
            )
        )
    return tuple(rebound)


class _ArchiveRedemptionState(StrEnum):
    FRESH = "fresh"
    SELECTING = "selecting"
    CONSUMED = "consumed"


class RedeemedArchiveContinuation(_ProcessPrivate):
    """One-use sealed continuation page issued only after registry redemption."""

    __slots__ = (
        "_candidate_handles",
        "_candidates",
        "_coverage",
        "_coverage_handles",
        "_lock",
        "_nonce",
        "_parent_record_seal",
        "_registry_generation",
        "_run_handle",
        "_seal",
        "_selection_handle",
        "_state",
        "_token_handle",
        "_warnings",
    )

    _candidate_handles: tuple[str, ...]
    _candidates: tuple[ArchiveSearchCandidate, ...]
    _coverage: tuple[SearchCoverage, ...]
    _coverage_handles: tuple[str, ...]
    _lock: threading.Lock
    _nonce: bytes
    _parent_record_seal: str
    _registry_generation: bytes
    _run_handle: str
    _seal: str
    _selection_handle: str | None
    _state: _ArchiveRedemptionState
    _token_handle: str
    _warnings: tuple[ArchiveSearchWarning, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive continuation requires registry redemption")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive continuation redemption is immutable")

    def __repr__(self) -> str:
        return "<RedeemedArchiveContinuation sealed one-use private>"


def _redemption_material(redemption: RedeemedArchiveContinuation) -> bytes:
    return _canonical_json(
        {
            "candidate_handles": list(redemption._candidate_handles),
            "coverage_handles": list(redemption._coverage_handles),
            "nonce": redemption._nonce.hex(),
            "parent_record_seal": redemption._parent_record_seal,
            "registry_generation": redemption._registry_generation.hex(),
            "run_handle": redemption._run_handle,
            "schema": _REDEMPTION_SCHEMA,
            "selection_handle": redemption._selection_handle,
            "state": redemption._state.value,
            "token_handle": redemption._token_handle,
            "warnings": [item.value for item in redemption._warnings],
        }
    )


def _reseal_redemption(redemption: RedeemedArchiveContinuation) -> None:
    object.__setattr__(
        redemption,
        "_seal",
        _mac(
            b"friday/archive-search-continuation-redemption/v3",
            _redemption_material(redemption),
        ),
    )


def _redemption_is_valid(
    value: object,
    run: ArchiveSearchRunBinding,
) -> bool:
    if type(value) is not RedeemedArchiveContinuation:
        return False
    redemption = cast(RedeemedArchiveContinuation, value)
    try:
        request_token = run._request.continuation
        if (
            type(request_token) is not str
            or type(redemption._candidates) is not tuple
            or not 0 < len(redemption._candidates) <= ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL
            or any(type(item) is not ArchiveSearchCandidate for item in redemption._candidates)
            or type(redemption._coverage) is not tuple
            or not redemption._coverage
            or len(redemption._coverage) > ARCHIVE_AUTHORITY_MAX_COVERAGES
            or any(type(item) is not SearchCoverage for item in redemption._coverage)
            or type(redemption._state) is not _ArchiveRedemptionState
            or (
                redemption._selection_handle is not None
                and _DIGEST.fullmatch(redemption._selection_handle) is None
            )
            or (
                (redemption._state is _ArchiveRedemptionState.FRESH)
                is (redemption._selection_handle is not None)
            )
            or type(redemption._warnings) is not tuple
            or any(type(item) is not ArchiveSearchWarning for item in redemption._warnings)
            or type(redemption._nonce) is not bytes
            or len(redemption._nonce) != _NONCE_BYTES
            or type(redemption._lock) is not type(threading.Lock())
            or type(redemption._registry_generation) is not bytes
            or not hmac.compare_digest(
                redemption._registry_generation,
                _CONTINUATION_REGISTRY._generation,
            )
            or not hmac.compare_digest(redemption._run_handle, run._seal)
            or not hmac.compare_digest(
                redemption._token_handle,
                _continuation_token_handle(request_token),
            )
        ):
            return False
        candidate_handles = tuple(_candidate_handle(item) for item in redemption._candidates)
        coverage_handles = tuple(_coverage_handle(item) for item in redemption._coverage)
        return bool(
            hmac.compare_digest(
                _canonical_json(list(redemption._candidate_handles)),
                _canonical_json(list(candidate_handles)),
            )
            and hmac.compare_digest(
                _canonical_json(list(redemption._coverage_handles)),
                _canonical_json(list(coverage_handles)),
            )
            and hmac.compare_digest(
                redemption._seal,
                _mac(
                    b"friday/archive-search-continuation-redemption/v3",
                    _redemption_material(redemption),
                ),
            )
        )
    except Exception:
        return False


def _new_redemption(
    *,
    run: ArchiveSearchRunBinding,
    parent: _ArchiveContinuationRecord,
    candidates: tuple[ArchiveSearchCandidate, ...],
    coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
    registry_generation: bytes,
) -> RedeemedArchiveContinuation:
    redemption = cast(RedeemedArchiveContinuation, object.__new__(RedeemedArchiveContinuation))
    for name, item in (
        ("_candidate_handles", tuple(_candidate_handle(candidate) for candidate in candidates)),
        ("_candidates", candidates),
        ("_coverage", coverage),
        ("_coverage_handles", tuple(_coverage_handle(item) for item in coverage)),
        ("_lock", threading.Lock()),
        ("_nonce", secrets.token_bytes(_NONCE_BYTES)),
        ("_parent_record_seal", parent.seal),
        ("_registry_generation", registry_generation),
        ("_run_handle", run._seal),
        ("_seal", "0" * 64),
        ("_selection_handle", None),
        ("_state", _ArchiveRedemptionState.FRESH),
        ("_token_handle", parent.token_handle),
        ("_warnings", warnings),
    ):
        object.__setattr__(redemption, name, item)
    _reseal_redemption(redemption)
    return redemption


class _ArchiveContinuationSelectionLease(_ProcessPrivate):
    """Unforgeable local ownership of one in-progress resumed-page selection."""

    __slots__ = (
        "_claimed",
        "_handle",
        "_lock",
        "_nonce",
        "_redemption_seal",
        "_registry_generation",
        "_run_handle",
        "_seal",
    )

    _claimed: bool
    _handle: str
    _lock: threading.Lock
    _nonce: bytes
    _redemption_seal: str
    _registry_generation: bytes
    _run_handle: str
    _seal: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive continuation selection lease is unavailable")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive continuation selection lease is immutable")

    def __repr__(self) -> str:
        return "<_ArchiveContinuationSelectionLease sealed one-use private>"


def _selection_lease_handle(
    *,
    nonce: bytes,
    run_handle: str,
    registry_generation: bytes,
) -> str:
    return _mac(
        b"friday/archive-search-continuation-selection-lease-identity/v1",
        _canonical_json(
            {
                "nonce": nonce.hex(),
                "registry_generation": registry_generation.hex(),
                "run_handle": run_handle,
            }
        ),
    )


def _selection_lease_material(lease: _ArchiveContinuationSelectionLease) -> bytes:
    return _canonical_json(
        {
            "claimed": lease._claimed,
            "handle": lease._handle,
            "nonce": lease._nonce.hex(),
            "redemption_seal": lease._redemption_seal,
            "registry_generation": lease._registry_generation.hex(),
            "run_handle": lease._run_handle,
            "schema": "friday.archive-search-continuation-selection-lease.private.v1",
        }
    )


def _reseal_selection_lease(lease: _ArchiveContinuationSelectionLease) -> None:
    object.__setattr__(
        lease,
        "_seal",
        _mac(
            b"friday/archive-search-continuation-selection-lease/v1",
            _selection_lease_material(lease),
        ),
    )


def _selection_lease_is_valid(
    value: object,
    run: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
) -> bool:
    if type(value) is not _ArchiveContinuationSelectionLease:
        return False
    lease = cast(_ArchiveContinuationSelectionLease, value)
    try:
        expected_state = (
            _ArchiveRedemptionState.CONSUMED if lease._claimed else _ArchiveRedemptionState.SELECTING
        )
        return bool(
            type(lease._claimed) is bool
            and type(lease._lock) is type(threading.Lock())
            and type(lease._nonce) is bytes
            and len(lease._nonce) == _NONCE_BYTES
            and type(lease._registry_generation) is bytes
            and hmac.compare_digest(
                lease._registry_generation,
                redemption._registry_generation,
            )
            and hmac.compare_digest(lease._run_handle, run._seal)
            and hmac.compare_digest(lease._run_handle, redemption._run_handle)
            and redemption._state is expected_state
            and type(redemption._selection_handle) is str
            and hmac.compare_digest(lease._handle, redemption._selection_handle)
            and hmac.compare_digest(lease._redemption_seal, redemption._seal)
            and hmac.compare_digest(
                lease._handle,
                _selection_lease_handle(
                    nonce=lease._nonce,
                    run_handle=lease._run_handle,
                    registry_generation=lease._registry_generation,
                ),
            )
            and hmac.compare_digest(
                lease._seal,
                _mac(
                    b"friday/archive-search-continuation-selection-lease/v1",
                    _selection_lease_material(lease),
                ),
            )
        )
    except Exception:
        return False


class _SelectedArchiveContinuation(_ProcessPrivate):
    """One-use exact suffix created only after resumed reauthorization and fit."""

    __slots__ = (
        "_candidate_handles",
        "_candidates",
        "_claimed",
        "_coverage",
        "_coverage_handles",
        "_lock",
        "_selection_lease_handle",
        "_selection_lease_seal",
        "_nonce",
        "_parent_token_handle",
        "_redemption_seal",
        "_registry_generation",
        "_run_handle",
        "_seal",
        "_warnings",
    )

    _candidate_handles: tuple[str, ...]
    _candidates: tuple[ArchiveSearchCandidate, ...]
    _claimed: bool
    _coverage: tuple[SearchCoverage, ...]
    _coverage_handles: tuple[str, ...]
    _lock: threading.Lock
    _selection_lease_handle: str
    _selection_lease_seal: str
    _nonce: bytes
    _parent_token_handle: str
    _redemption_seal: str
    _registry_generation: bytes
    _run_handle: str
    _seal: str
    _warnings: tuple[ArchiveSearchWarning, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("archive continuation selection is unavailable")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive continuation selection is immutable")

    def __repr__(self) -> str:
        return "<_SelectedArchiveContinuation sealed one-use private>"


def _selected_continuation_material(selection: _SelectedArchiveContinuation) -> bytes:
    return _canonical_json(
        {
            "candidate_handles": list(selection._candidate_handles),
            "claimed": selection._claimed,
            "coverage_handles": list(selection._coverage_handles),
            "selection_lease_handle": selection._selection_lease_handle,
            "selection_lease_seal": selection._selection_lease_seal,
            "nonce": selection._nonce.hex(),
            "parent_token_handle": selection._parent_token_handle,
            "redemption_seal": selection._redemption_seal,
            "registry_generation": selection._registry_generation.hex(),
            "run_handle": selection._run_handle,
            "schema": _SELECTED_CONTINUATION_SCHEMA,
            "warnings": [item.value for item in selection._warnings],
        }
    )


def _reseal_selected_continuation(selection: _SelectedArchiveContinuation) -> None:
    object.__setattr__(
        selection,
        "_seal",
        _mac(
            b"friday/archive-search-continuation-selection/v1",
            _selected_continuation_material(selection),
        ),
    )


def _selected_continuation_is_valid(
    value: object,
    run: ArchiveSearchRunBinding,
    *,
    registry_generation: bytes,
) -> bool:
    if type(value) is not _SelectedArchiveContinuation:
        return False
    selection = cast(_SelectedArchiveContinuation, value)
    try:
        if (
            type(selection._candidates) is not tuple
            or not 0 < len(selection._candidates) <= ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL
            or any(type(item) is not ArchiveSearchCandidate for item in selection._candidates)
            or type(selection._coverage) is not tuple
            or not selection._coverage
            or len(selection._coverage) > ARCHIVE_AUTHORITY_MAX_COVERAGES
            or any(type(item) is not SearchCoverage for item in selection._coverage)
            or type(selection._warnings) is not tuple
            or any(type(item) is not ArchiveSearchWarning for item in selection._warnings)
            or type(selection._claimed) is not bool
            or type(selection._lock) is not type(threading.Lock())
            or _DIGEST.fullmatch(selection._selection_lease_handle) is None
            or _DIGEST.fullmatch(selection._selection_lease_seal) is None
            or type(selection._nonce) is not bytes
            or len(selection._nonce) != _NONCE_BYTES
            or type(selection._registry_generation) is not bytes
            or not hmac.compare_digest(selection._registry_generation, registry_generation)
            or not hmac.compare_digest(selection._run_handle, run._seal)
            or _DIGEST.fullmatch(selection._parent_token_handle) is None
            or _DIGEST.fullmatch(selection._redemption_seal) is None
            or not _stored_coverage_matches_resumed_run(selection._coverage, run)
        ):
            return False
        return bool(
            hmac.compare_digest(
                _canonical_json(list(selection._candidate_handles)),
                _canonical_json([_candidate_handle(item) for item in selection._candidates]),
            )
            and hmac.compare_digest(
                _canonical_json(list(selection._coverage_handles)),
                _canonical_json([_coverage_handle(item) for item in selection._coverage]),
            )
            and hmac.compare_digest(
                selection._seal,
                _mac(
                    b"friday/archive-search-continuation-selection/v1",
                    _selected_continuation_material(selection),
                ),
            )
        )
    except Exception:
        return False


def _new_selected_continuation(
    *,
    run: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
    lease: _ArchiveContinuationSelectionLease,
    candidates: tuple[ArchiveSearchCandidate, ...],
) -> _SelectedArchiveContinuation:
    if (
        not _redemption_is_valid(redemption, run)
        or not _selection_lease_is_valid(lease, run, redemption)
        or not lease._claimed
        or redemption._state is not _ArchiveRedemptionState.CONSUMED
        or not candidates
    ):
        raise ArchiveSearchAuthorityError("archive continuation selection is unavailable")
    selection = cast(
        _SelectedArchiveContinuation,
        object.__new__(_SelectedArchiveContinuation),
    )
    for name, value in (
        ("_candidate_handles", tuple(_candidate_handle(item) for item in candidates)),
        ("_candidates", candidates),
        ("_claimed", False),
        ("_coverage", redemption._coverage),
        ("_coverage_handles", redemption._coverage_handles),
        ("_lock", threading.Lock()),
        ("_selection_lease_handle", lease._handle),
        ("_selection_lease_seal", lease._seal),
        ("_nonce", secrets.token_bytes(_NONCE_BYTES)),
        ("_parent_token_handle", redemption._token_handle),
        ("_redemption_seal", redemption._seal),
        ("_registry_generation", redemption._registry_generation),
        ("_run_handle", run._seal),
        ("_seal", "0" * 64),
        ("_warnings", redemption._warnings),
    ):
        object.__setattr__(selection, name, value)
    _reseal_selected_continuation(selection)
    return selection


def issue_archive_search_continuation(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    tail_candidates: tuple[ArchiveSearchCandidate, ...],
    terminal_coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...] = (),
) -> IssuedArchiveContinuation:
    """Materialize one bounded live cursor over an actual, non-empty frozen tail."""

    try:
        if not _run_is_valid(run_binding, tenant_id=tenant_id, principal_id=principal_id):
            raise ArchiveSearchAuthorityError("archive continuation run is unavailable")
        candidates, coverage, warning_values = _validate_continuation_tail(
            run_binding,
            tail_candidates,
            terminal_coverage,
            warnings,
        )
        return _CONTINUATION_REGISTRY.issue(
            run=run_binding,
            candidates=candidates,
            coverage=coverage,
            warnings=warning_values,
        )
    except Exception:
        raise ArchiveSearchAuthorityError("archive continuation issue failed") from None


def redeem_archive_search_continuation(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
) -> RedeemedArchiveContinuation:
    """Atomically consume the request's registry-issued inbound cursor."""

    return _CONTINUATION_REGISTRY.redeem(
        tenant_id=tenant_id,
        principal_id=principal_id,
        run=run_binding,
    )


def _begin_redeemed_continuation(
    run: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
) -> tuple[
    _ArchiveContinuationSelectionLease,
    tuple[ArchiveSearchCandidate, ...],
    tuple[SearchCoverage, ...],
    tuple[ArchiveSearchWarning, ...],
]:
    with redemption._lock:
        if (
            not _redemption_is_valid(redemption, run)
            or redemption._state is not _ArchiveRedemptionState.FRESH
        ):
            raise ArchiveSearchAuthorityError("archive continuation redemption is unavailable")
        # SELECTING is sealed before any caller-controlled reauthorization
        # callback.  A nested attempt therefore cannot consume or mint from the
        # same redemption while the outer page is still being chosen.
        nonce = secrets.token_bytes(_NONCE_BYTES)
        lease_handle = _selection_lease_handle(
            nonce=nonce,
            run_handle=run._seal,
            registry_generation=redemption._registry_generation,
        )
        object.__setattr__(redemption, "_selection_handle", lease_handle)
        object.__setattr__(redemption, "_state", _ArchiveRedemptionState.SELECTING)
        _reseal_redemption(redemption)
        lease = cast(
            _ArchiveContinuationSelectionLease,
            object.__new__(_ArchiveContinuationSelectionLease),
        )
        for name, value in (
            ("_claimed", False),
            ("_handle", lease_handle),
            ("_lock", threading.Lock()),
            ("_nonce", nonce),
            ("_redemption_seal", redemption._seal),
            ("_registry_generation", redemption._registry_generation),
            ("_run_handle", run._seal),
            ("_seal", "0" * 64),
        ):
            object.__setattr__(lease, name, value)
        _reseal_selection_lease(lease)
        return (
            lease,
            redemption._candidates,
            redemption._coverage,
            redemption._warnings,
        )


def _abort_redeemed_continuation(
    run: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
    lease: _ArchiveContinuationSelectionLease,
) -> None:
    """Fail closed after selection began; no later retry may mint a child."""

    if (
        type(redemption) is not RedeemedArchiveContinuation
        or type(lease) is not _ArchiveContinuationSelectionLease
        or type(redemption._lock) is not type(threading.Lock())
        or type(lease._lock) is not type(threading.Lock())
        or redemption._lock is lease._lock
    ):
        return
    try:
        with lease._lock, redemption._lock:
            if (
                _redemption_is_valid(redemption, run)
                and _selection_lease_is_valid(lease, run, redemption)
                and not lease._claimed
                and redemption._state is _ArchiveRedemptionState.SELECTING
            ):
                object.__setattr__(redemption, "_state", _ArchiveRedemptionState.CONSUMED)
                _reseal_redemption(redemption)
                object.__setattr__(lease, "_claimed", True)
                object.__setattr__(lease, "_redemption_seal", redemption._seal)
                _reseal_selection_lease(lease)
    except Exception:
        return


def _finish_redeemed_continuation(
    run: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
    lease: _ArchiveContinuationSelectionLease,
    *,
    head_count: int,
) -> _SelectedArchiveContinuation | None:
    """Seal the exact post-fit suffix and consume the redemption atomically."""

    if (
        type(redemption) is not RedeemedArchiveContinuation
        or type(lease) is not _ArchiveContinuationSelectionLease
        or type(redemption._lock) is not type(threading.Lock())
        or type(lease._lock) is not type(threading.Lock())
        or redemption._lock is lease._lock
    ):
        raise ArchiveSearchAuthorityError("archive continuation selection is unavailable")
    with lease._lock, redemption._lock:
        if (
            not _redemption_is_valid(redemption, run)
            or not _selection_lease_is_valid(lease, run, redemption)
            or lease._claimed
            or redemption._state is not _ArchiveRedemptionState.SELECTING
            or type(head_count) is not int
            or not 0 < head_count <= len(redemption._candidates)
        ):
            raise ArchiveSearchAuthorityError("archive continuation selection is unavailable")
        suffix = redemption._candidates[head_count:]
        object.__setattr__(redemption, "_state", _ArchiveRedemptionState.CONSUMED)
        _reseal_redemption(redemption)
        object.__setattr__(lease, "_claimed", True)
        object.__setattr__(lease, "_redemption_seal", redemption._seal)
        _reseal_selection_lease(lease)
        if not suffix:
            return None
        return _new_selected_continuation(
            run=run,
            redemption=redemption,
            lease=lease,
            candidates=suffix,
        )


def _terminal_continuation_warnings(
    warnings: tuple[ArchiveSearchWarning, ...],
    page_coverage: tuple[SearchCoverage, ...],
) -> tuple[ArchiveSearchWarning, ...]:
    result = set(warnings)
    if any(CoverageState.CAPPED in item.states and not item.next_cursor_available for item in page_coverage):
        result.add(ArchiveSearchWarning.CONTINUATION_UNAVAILABLE)
    return tuple(sorted(result, key=lambda item: item.value))


def _page_fits_public_contract(
    run: ArchiveSearchRunBinding,
    page: ArchiveSearchPage,
) -> bool:
    try:
        model_bytes = _project_model_visible_page(
            run,
            page,
            model_page_index=_run_model_page_index(run),
        )
    except Exception:
        return False
    return bool(model_bytes and len(model_bytes) <= _MAX_MODEL_BYTES)


def _select_resumed_archive_page(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    candidates: tuple[ArchiveSearchCandidate, ...],
    terminal_coverage: tuple[SearchCoverage, ...],
    warnings: tuple[ArchiveSearchWarning, ...],
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> tuple[ArchiveSearchPage, int]:
    inbound_token = run_binding._request.continuation
    if type(inbound_token) is not str:
        raise ArchiveSearchAuthorityError("archive continuation request is unavailable")
    probe_token = _continuation_probe_token(inbound_token)
    maximum = min(run_binding._request.limit, len(candidates))
    for head_count in range(maximum, 0, -1):
        head = candidates[:head_count]
        suffix = candidates[head_count:]
        coverage = _continuation_page_coverage(
            run_binding,
            terminal_coverage,
            head,
            child_candidates=suffix,
        )
        page = _authorized_archive_search_page(
            tenant_id=tenant_id,
            principal_id=principal_id,
            run_binding=run_binding,
            candidates=head,
            coverage=coverage,
            warnings=_terminal_continuation_warnings(
                warnings,
                coverage,
            ),
            continuation_token=probe_token if suffix else None,
            candidate_reauthorizer=candidate_reauthorizer,
            coverage_reauthorizer=coverage_reauthorizer,
            authority_context=authority_context,
        )
        if _page_fits_public_contract(run_binding, page):
            return page, head_count
    raise ArchiveSearchAuthorityError("archive continuation page is outside its closed limit")


def authorize_archive_search_resumed_before_model(
    *,
    tenant_id: str,
    principal_id: str,
    run_binding: ArchiveSearchRunBinding,
    redemption: RedeemedArchiveContinuation,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> AuthorizedArchiveBatch:
    """Consume one sealed registry redemption through the ordinary BEFORE_MODEL gate."""

    child_token: str | None = None
    selection_lease: _ArchiveContinuationSelectionLease | None = None
    try:
        if not _run_is_valid(run_binding, tenant_id=tenant_id, principal_id=principal_id):
            raise ArchiveSearchAuthorityError("archive continuation run is unavailable")
        selection_lease, candidates, terminal_coverage, warnings = _begin_redeemed_continuation(
            run_binding,
            redemption,
        )
        page, head_count = _select_resumed_archive_page(
            tenant_id=tenant_id,
            principal_id=principal_id,
            run_binding=run_binding,
            candidates=candidates,
            terminal_coverage=terminal_coverage,
            warnings=warnings,
            candidate_reauthorizer=candidate_reauthorizer,
            coverage_reauthorizer=coverage_reauthorizer,
            authority_context=authority_context,
        )
        if selection_lease is None:
            raise ArchiveSearchAuthorityError("archive continuation selection is unavailable")
        selection = _finish_redeemed_continuation(
            run_binding,
            redemption,
            selection_lease,
            head_count=head_count,
        )
        if selection is not None:
            child = _CONTINUATION_REGISTRY.mint_selected_child(
                run=run_binding,
                selection=selection,
            )
            child_token = child.token
            page = ArchiveSearchPage.create(
                request=run_binding._request,
                candidates=tuple(result.candidate for result in page.results),
                coverage=page.coverage,
                warnings=page.warnings,
                continuation=child_token,
            )
        return _new_live_archive_batch(run_binding, page)
    except Exception:
        if selection_lease is not None:
            _abort_redeemed_continuation(run_binding, redemption, selection_lease)
        if child_token is not None:
            _CONTINUATION_REGISTRY.revoke_token(child_token)
        raise ArchiveSearchAuthorityError("archive continuation model admission failed") from None


def _selected_evidence_shape_is_compatible(
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    passage_refs: tuple[PassageRef, ...],
) -> bool:
    if (
        type(corpus) is not ArchiveSearchCorpus
        or type(source_ref) is not SourceRef
        or type(passage_refs) is not tuple
        or not 1 <= len(passage_refs) <= 8
        or any(type(item) is not PassageRef or item.source_ref != source_ref for item in passage_refs)
    ):
        return False
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        return bool(
            source_ref.source_kind is SourceKind.DOCUMENT
            and all(
                type(item.locator) is TextSpanLocator
                and item.source_revision.representation.kind is RepresentationKind.RAW_OBJECT
                for item in passage_refs
            )
        )
    if corpus is ArchiveSearchCorpus.KNOWLEDGE:
        return bool(
            source_ref.source_kind
            in {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
            and all(
                type(item.locator) is TextSpanLocator
                and item.source_revision.representation.kind is RepresentationKind.KNOWLEDGE_OBJECT
                for item in passage_refs
            )
        )
    if corpus is ArchiveSearchCorpus.MESSAGES:
        return bool(
            source_ref.source_kind is SourceKind.CONVERSATION
            and all(
                type(item.locator) is MessageWindowLocator
                and item.source_revision.representation.kind is RepresentationKind.CONVERSATION
                for item in passage_refs
            )
        )
    return False


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchSelectedEvidence:
    """Body-free identity of one factual candidate selected by exact citations."""

    corpus: ArchiveSearchCorpus
    source_ref: SourceRef
    passage_refs: tuple[PassageRef, ...]
    resolved_snapshot_sha256: str

    def __post_init__(self) -> None:
        if type(self.corpus) is not ArchiveSearchCorpus or self.corpus not in {
            ArchiveSearchCorpus.DOCUMENTS,
            ArchiveSearchCorpus.KNOWLEDGE,
            ArchiveSearchCorpus.MESSAGES,
        }:
            raise ArchiveSearchAuthorityError("archive selected corpus is invalid")
        if type(self.source_ref) is not SourceRef:
            raise ArchiveSearchAuthorityError("archive selected source is invalid")
        if (
            type(self.passage_refs) is not tuple
            or not 1 <= len(self.passage_refs) <= 8
            or any(type(item) is not PassageRef for item in self.passage_refs)
            or any(item.source_ref != self.source_ref for item in self.passage_refs)
        ):
            raise ArchiveSearchAuthorityError("archive selected passages are invalid")
        identities = tuple(item.to_private_json() for item in self.passage_refs)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ArchiveSearchAuthorityError("archive selected passages are not canonical")
        if not _selected_evidence_shape_is_compatible(
            self.corpus,
            self.source_ref,
            self.passage_refs,
        ):
            raise ArchiveSearchAuthorityError("archive selected corpus and source disagree")
        if (
            type(self.resolved_snapshot_sha256) is not str
            or _DIGEST.fullmatch(self.resolved_snapshot_sha256) is None
        ):
            raise ArchiveSearchAuthorityError("archive selected snapshot digest is invalid")

    def __repr__(self) -> str:
        return "<ArchiveSearchSelectedEvidence body-free private-identity>"

    @property
    def source_snapshot_sha256(self) -> str:
        """Alias used by the durable selected-evidence sidecar contract."""

        return self.resolved_snapshot_sha256

    def to_private_payload(self) -> dict[str, object]:
        return {
            "corpus": self.corpus.value,
            "passage_refs": [item.to_private_payload() for item in self.passage_refs],
            "resolved_snapshot_sha256": self.resolved_snapshot_sha256,
            "schema": ARCHIVE_SEARCH_SELECTED_EVIDENCE_SCHEMA,
            "source_ref": self.source_ref.to_private_payload(),
        }

    def to_private_json(self) -> str:
        return _canonical_json(self.to_private_payload()).decode("ascii")

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveSearchSelectedEvidence:
        if type(value) is not dict or frozenset(value) != frozenset(
            {
                "passage_refs",
                "corpus",
                "resolved_snapshot_sha256",
                "schema",
                "source_ref",
            }
        ):
            raise ArchiveSearchAuthorityError("archive selected evidence keys are invalid")
        payload = cast(dict[str, object], value)
        if payload["schema"] != ARCHIVE_SEARCH_SELECTED_EVIDENCE_SCHEMA:
            raise ArchiveSearchAuthorityError("archive selected evidence schema is invalid")
        raw_passages = payload["passage_refs"]
        if type(raw_passages) is not list or not 1 <= len(raw_passages) <= 8:
            raise ArchiveSearchAuthorityError("archive selected passages are invalid")
        try:
            raw_corpus = payload["corpus"]
            if type(raw_corpus) is not str:
                raise ArchiveSearchAuthorityError("archive selected corpus is invalid")
            corpus = ArchiveSearchCorpus(raw_corpus)
            source_ref = SourceRef.from_private_payload(payload["source_ref"])
            passage_refs = tuple(PassageRef.from_private_payload(item) for item in raw_passages)
        except Exception:
            raise ArchiveSearchAuthorityError("archive selected evidence is invalid") from None
        snapshot = payload["resolved_snapshot_sha256"]
        if type(snapshot) is not str:
            raise ArchiveSearchAuthorityError("archive selected snapshot digest is invalid")
        return cls(corpus, source_ref, passage_refs, snapshot)


class ArchiveSearchPublicationAttestation(_ProcessPrivate):
    """Accepted phase-2 proof bound to the exact carriers and answer digest."""

    __slots__ = (
        "_answer_sha256",
        "_candidate_count",
        "_carrier_ledger",
        "_coverage_grade",
        "_coverage_sha256",
        "_evidence_sha256",
        "_nonce",
        "_plan_sha256",
        "_run_ledger",
        "_seal",
        "_selected_evidence",
        "_turn_ledger_handle",
        "_used_citation_labels",
    )

    _answer_sha256: str
    _candidate_count: int
    _carrier_ledger: str
    _coverage_grade: ArchiveSearchCoverageGrade
    _coverage_sha256: str
    _evidence_sha256: str
    _nonce: bytes
    _plan_sha256: str
    _run_ledger: str
    _seal: str
    _selected_evidence: ArchiveSearchSelectedEvidence | None
    _turn_ledger_handle: str
    _used_citation_labels: tuple[str, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ArchiveSearchAuthorityError("publication attestations require phase-2 authorization")

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive publication attestation is immutable")

    def __repr__(self) -> str:
        return "<ArchiveSearchPublicationAttestation accepted private>"

    @property
    def answer_sha256(self) -> str:
        self._require_valid()
        return self._answer_sha256

    @property
    def plan_sha256(self) -> str:
        self._require_valid()
        return self._plan_sha256

    @property
    def evidence_sha256(self) -> str:
        self._require_valid()
        return self._evidence_sha256

    @property
    def coverage_sha256(self) -> str:
        self._require_valid()
        return self._coverage_sha256

    @property
    def coverage_grade(self) -> ArchiveSearchCoverageGrade:
        self._require_valid()
        return self._coverage_grade

    @property
    def candidate_count(self) -> int:
        self._require_valid()
        return self._candidate_count

    @property
    def used_citation_labels(self) -> tuple[str, ...]:
        self._require_valid()
        return self._used_citation_labels

    @property
    def used_citation_count(self) -> int:
        self._require_valid()
        return len(self._used_citation_labels)

    @property
    def selected_evidence(self) -> ArchiveSearchSelectedEvidence | None:
        self._require_valid()
        return self._selected_evidence

    def _is_valid(self) -> bool:
        try:
            return bool(
                type(self) is ArchiveSearchPublicationAttestation
                and all(
                    _DIGEST.fullmatch(value) is not None
                    for value in (
                        self._answer_sha256,
                        self._carrier_ledger,
                        self._coverage_sha256,
                        self._evidence_sha256,
                        self._plan_sha256,
                        self._run_ledger,
                        self._turn_ledger_handle,
                        self._seal,
                    )
                )
                and type(self._candidate_count) is int
                and 0
                <= self._candidate_count
                <= ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES
                and type(self._coverage_grade) is ArchiveSearchCoverageGrade
                and type(self._used_citation_labels) is tuple
                and len(self._used_citation_labels) == len(set(self._used_citation_labels))
                and all(_public_citation_label_is_valid(item) for item in self._used_citation_labels)
                and (
                    self._selected_evidence is None
                    or type(self._selected_evidence) is ArchiveSearchSelectedEvidence
                )
                and type(self._nonce) is bytes
                and len(self._nonce) == _NONCE_BYTES
                and hmac.compare_digest(
                    self._seal,
                    _mac(
                        b"friday/archive-search-publication-attestation/v3",
                        _attestation_material(self),
                    ),
                )
            )
        except Exception:
            return False

    def _require_valid(self) -> None:
        if not self._is_valid():
            raise ArchiveSearchAuthorityError("archive publication attestation is unavailable")

    def attests_answer(self, answer: str) -> bool:
        try:
            answer_sha256 = _answer_digest(answer)
            return bool(self._is_valid() and hmac.compare_digest(self._answer_sha256, answer_sha256))
        except Exception:
            return False


def _answer_digest(answer: object) -> str:
    if type(answer) is not str or not answer:
        raise ArchiveSearchAuthorityError("archive answer is invalid")
    try:
        encoded = answer.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ArchiveSearchAuthorityError("archive answer is invalid") from None
    if len(encoded) > ARCHIVE_AUTHORITY_MAX_ANSWER_BYTES:
        raise ArchiveSearchAuthorityError("archive answer is invalid")
    return _sha256(encoded)


def _attestation_material(attestation: ArchiveSearchPublicationAttestation) -> bytes:
    return _canonical_json(
        {
            "answer_sha256": attestation._answer_sha256,
            "candidate_count": attestation._candidate_count,
            "carrier_ledger": attestation._carrier_ledger,
            "coverage_grade": attestation._coverage_grade.value,
            "coverage_sha256": attestation._coverage_sha256,
            "evidence_sha256": attestation._evidence_sha256,
            "nonce": attestation._nonce.hex(),
            "plan_sha256": attestation._plan_sha256,
            "run_ledger": attestation._run_ledger,
            "schema": _ATTESTATION_SCHEMA,
            "selected_evidence": (
                None
                if attestation._selected_evidence is None
                else attestation._selected_evidence.to_private_payload()
            ),
            "turn_ledger_handle": attestation._turn_ledger_handle,
            "used_citation_labels": list(attestation._used_citation_labels),
        }
    )


def _publication_projection(
    entries: tuple[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch, bytes], ...],
    answer: str,
) -> tuple[
    str,
    str,
    str,
    ArchiveSearchCoverageGrade,
    int,
    tuple[str, ...],
    ArchiveSearchSelectedEvidence | None,
]:
    """Derive the body-free accepted outcome projection from consumed exact pages."""

    request_identities: list[str] = []
    evidence_records: list[dict[str, object]] = []
    coverage_records: list[dict[str, object]] = []
    label_map: dict[
        str,
        tuple[
            tuple[int, int],
            ArchiveSearchCandidate,
            tuple[PassageRef, ...] | None,
        ],
    ] = {}
    candidate_count = 0
    for entry_index, (run, batch, _visible) in enumerate(entries, 1):
        request_identity_sha256 = _sha256(run._request.identity_digest_material())
        if request_identity_sha256 not in request_identities:
            request_identities.append(request_identity_sha256)
        page_candidates: list[dict[str, object]] = []
        for result in batch._page.results:
            candidate = result.candidate
            public_ordinal = (batch._model_page_index - 1) * ARCHIVE_AUTHORITY_MAX_CANDIDATES + result.ordinal
            candidate_label = f"A{public_ordinal}"
            candidate_key = (entry_index, result.ordinal)
            passage_refs = tuple(item.passage_ref for item in candidate.passages)
            label_map[candidate_label] = (candidate_key, candidate, None)
            for passage_index, passage_ref in enumerate(passage_refs, 1):
                label_map[f"{candidate_label}.{passage_index}"] = (
                    candidate_key,
                    candidate,
                    (passage_ref,),
                )
            page_candidates.append(
                {
                    "candidate_sha256": _sha256(candidate.to_private_json().encode("ascii")),
                    "label": candidate_label,
                }
            )
            candidate_count += 1
        evidence_records.append(
            {
                "candidates": page_candidates,
                "model_page_index": batch._model_page_index,
            }
        )
        coverage_records.append(
            {
                "coverage_sha256": [_sha256(item.to_json().encode("ascii")) for item in batch._page.coverage],
                "exhaustive": batch._page.exhaustive,
                "has_continuation": batch._page.continuation is not None,
                "model_page_index": batch._model_page_index,
                "warnings": [item.value for item in batch._page.warnings],
            }
        )

    citation_matches = tuple(_PUBLIC_CITATION.finditer(answer))
    valid_citation_matches = tuple(
        match for match in citation_matches if _public_citation_label_is_valid(match.group(1))
    )
    used_citation_labels = tuple(dict.fromkeys(match.group(1) for match in valid_citation_matches))
    valid_citation_starts = {match.start() for match in valid_citation_matches}
    malformed_citation_present = any(
        match.start() not in valid_citation_starts for match in _PUBLIC_CITATION_PREFIX.finditer(answer)
    )
    maximum_labels = ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES * 9
    if len(used_citation_labels) > maximum_labels:
        raise ArchiveSearchAuthorityError("archive answer has too many citation labels")

    selected: ArchiveSearchSelectedEvidence | None = None
    mapped = [label_map.get(label) for label in used_citation_labels]
    if not malformed_citation_present and used_citation_labels and all(item is not None for item in mapped):
        exact_mapped = cast(
            list[
                tuple[
                    tuple[int, int],
                    ArchiveSearchCandidate,
                    tuple[PassageRef, ...] | None,
                ]
            ],
            mapped,
        )
        candidate_keys = {item[0] for item in exact_mapped}
        candidate = exact_mapped[0][1]
        if (
            len(candidate_keys) == 1
            and candidate.corpus
            in {
                ArchiveSearchCorpus.DOCUMENTS,
                ArchiveSearchCorpus.KNOWLEDGE,
                ArchiveSearchCorpus.MESSAGES,
            }
            and candidate.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
            and not candidate.navigation_only
            and 1 <= len(candidate.passages) <= 8
        ):
            selected_passages = (
                tuple(item.passage_ref for item in candidate.passages)
                if any(item[2] is None for item in exact_mapped)
                else tuple(
                    passage.passage_ref
                    for passage in candidate.passages
                    if any(item[2] is not None and passage.passage_ref in item[2] for item in exact_mapped)
                )
            )
            if selected_passages and _selected_evidence_shape_is_compatible(
                candidate.corpus,
                candidate.resolved_source.source_ref,
                selected_passages,
            ):
                selected_excerpts = tuple(
                    passage.excerpt
                    for passage in candidate.passages
                    if passage.passage_ref in selected_passages
                )
                selected = ArchiveSearchSelectedEvidence(
                    corpus=candidate.corpus,
                    source_ref=candidate.resolved_source.source_ref,
                    passage_refs=selected_passages,
                    resolved_snapshot_sha256=archive_selected_evidence_snapshot_sha256(
                        candidate.resolved_source,
                        selected_passages,
                        selected_excerpts,
                    ),
                )

    same_plan = len(request_identities) == 1
    chained_pages = bool(entries and entries[0][0]._request.continuation is None)
    for (_previous_run, previous_batch, _previous_visible), (
        next_run,
        _next_batch,
        _next_visible,
    ) in zip(entries, entries[1:], strict=False):
        previous_token = previous_batch._page.continuation
        next_token = next_run._request.continuation
        if (
            type(previous_token) is not str
            or type(next_token) is not str
            or not hmac.compare_digest(previous_token, next_token)
        ):
            chained_pages = False
            break
    page_warnings = {warning for _run, batch, _visible in entries for warning in batch._page.warnings}
    warnings_allow_complete = not page_warnings or bool(
        len(entries) > 1 and page_warnings == {ArchiveSearchWarning.LANE_CAPPED}
    )
    coverage_grade = (
        ArchiveSearchCoverageGrade.COMPLETE
        if (same_plan and chained_pages and warnings_allow_complete and entries[-1][1]._page.exhaustive)
        else ArchiveSearchCoverageGrade.PARTIAL
    )
    return (
        _sha256(_canonical_json(request_identities)),
        _sha256(_canonical_json(evidence_records)),
        _sha256(_canonical_json(coverage_records)),
        coverage_grade,
        candidate_count,
        used_citation_labels,
        selected,
    )


def _new_attestation(
    ledger: ArchiveModelBatchLedger,
    entries: tuple[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch, bytes], ...],
    answer: str,
) -> ArchiveSearchPublicationAttestation:
    (
        plan_sha256,
        evidence_sha256,
        coverage_sha256,
        coverage_grade,
        candidate_count,
        used_citation_labels,
        selected_evidence,
    ) = _publication_projection(entries, answer)
    carrier_ledger = _mac(
        b"friday/archive-search-publication-ledger/v2",
        _canonical_json(
            [
                {
                    "batch_seal": batch._seal,
                    "model_tool_sha256": _sha256(model_bytes),
                    "run_handle": run._seal,
                }
                for run, batch, model_bytes in entries
            ]
        ),
    )
    run_ledger = _mac(
        b"friday/archive-search-publication-run-ledger/v2",
        _canonical_json([run._seal for run, _batch, _model_bytes in entries]),
    )
    attestation = cast(
        ArchiveSearchPublicationAttestation,
        object.__new__(ArchiveSearchPublicationAttestation),
    )
    for name, value in (
        ("_answer_sha256", _answer_digest(answer)),
        ("_candidate_count", candidate_count),
        ("_carrier_ledger", carrier_ledger),
        ("_coverage_grade", coverage_grade),
        ("_coverage_sha256", coverage_sha256),
        ("_evidence_sha256", evidence_sha256),
        ("_nonce", secrets.token_bytes(_NONCE_BYTES)),
        ("_plan_sha256", plan_sha256),
        ("_run_ledger", run_ledger),
        ("_seal", "0" * 64),
        ("_selected_evidence", selected_evidence),
        ("_turn_ledger_handle", ledger._identity_handle),
        ("_used_citation_labels", used_citation_labels),
    ):
        object.__setattr__(attestation, name, value)
    object.__setattr__(
        attestation,
        "_seal",
        _mac(
            b"friday/archive-search-publication-attestation/v3",
            _attestation_material(attestation),
        ),
    )
    return attestation


def _deny(reason: ArchiveSearchPublicationDenialReason) -> NoReturn:
    raise ArchiveSearchPublicationDenied(reason) from None


def _attest_archive_search_before_publication(
    *,
    tenant_id: str,
    principal_id: str,
    ledger: ArchiveModelBatchLedger,
    answer: str,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> ArchiveSearchPublicationAttestation:
    if (
        type(ledger) is not ArchiveModelBatchLedger
        or not callable(candidate_reauthorizer)
        or not callable(coverage_reauthorizer)
    ):
        _deny(ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE)
    try:
        entries = _consume_model_batch_ledger(ledger)
    except Exception:
        _deny(ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE)
    try:
        actor = _actor_handle(tenant_id, principal_id)
        actor_mismatch = not hmac.compare_digest(ledger._actor_handle, actor)
    except Exception:
        actor_mismatch = True

    carrier_invalid = False
    authority_changed = False
    for run_binding, batch, _visible in entries:
        try:
            run_current = _run_is_valid(
                run_binding,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            coverages = tuple(batch._page.coverage)
            candidates = tuple(result.candidate for result in batch._page.results)
        except Exception:
            run_current = False
            coverages = ()
            candidates = ()
            carrier_invalid = True
        if not run_current:
            actor_mismatch = True
        for coverage in coverages:
            coverage_status, current_coverage = _call_coverage_reauthorizer(
                coverage_reauthorizer,
                ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION,
                run_binding,
                coverage,
                authority_context,
            )
            try:
                accepted = bool(
                    coverage_status is ArchiveSearchReauthorizationStatus.AUTHORIZED
                    and type(current_coverage) is SearchCoverage
                    and current_coverage.execution_binding is run_binding._execution_binding
                    and _same_coverage(coverage, current_coverage)
                )
            except Exception:
                accepted = False
            if not accepted:
                authority_changed = True
        for candidate in candidates:
            candidate_status, current_candidate = _call_candidate_reauthorizer(
                candidate_reauthorizer,
                ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION,
                run_binding,
                candidate,
                authority_context,
            )
            try:
                accepted = bool(
                    candidate_status is ArchiveSearchReauthorizationStatus.AUTHORIZED
                    and type(current_candidate) is ArchiveSearchCandidate
                    and _source_scope_matches(run_binding, current_candidate)
                    and _same_candidate(candidate, current_candidate)
                )
            except Exception:
                accepted = False
            if not accepted:
                authority_changed = True

    if authority_changed:
        _deny(ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED)
    if actor_mismatch:
        _deny(ArchiveSearchPublicationDenialReason.ACTOR_OR_RUN_MISMATCH)
    try:
        if any(
            not _run_is_valid(run, tenant_id=tenant_id, principal_id=principal_id)
            or not _batch_is_valid(batch, run)
            or not hmac.compare_digest(batch._model_visible_bytes, visible)
            for run, batch, visible in entries
        ):
            carrier_invalid = True
    except Exception:
        carrier_invalid = True
    if carrier_invalid:
        _deny(ArchiveSearchPublicationDenialReason.CARRIER_INVALID)
    try:
        return _new_attestation(ledger, entries, answer)
    except Exception:
        _deny(ArchiveSearchPublicationDenialReason.ANSWER_INVALID)


def attest_archive_search_before_publication(
    *,
    tenant_id: str,
    principal_id: str,
    ledger: ArchiveModelBatchLedger,
    answer: str,
    candidate_reauthorizer: ArchiveSearchCandidateReauthorizer,
    coverage_reauthorizer: ArchiveSearchCoverageReauthorizer,
    authority_context: object,
) -> ArchiveSearchPublicationAttestation:
    """Consume the exact model ledger and reauthorize every entry before commit."""

    try:
        return _attest_archive_search_before_publication(
            tenant_id=tenant_id,
            principal_id=principal_id,
            ledger=ledger,
            answer=answer,
            candidate_reauthorizer=candidate_reauthorizer,
            coverage_reauthorizer=coverage_reauthorizer,
            authority_context=authority_context,
        )
    except ArchiveSearchPublicationDenied as exc:
        reason = (
            exc.reason
            if type(exc.reason) is ArchiveSearchPublicationDenialReason
            else ArchiveSearchPublicationDenialReason.CARRIER_INVALID
        )
        raise ArchiveSearchPublicationDenied(reason) from None
    except Exception:
        raise ArchiveSearchPublicationDenied(ArchiveSearchPublicationDenialReason.CARRIER_INVALID) from None
    finally:
        _unregister_consumed_model_batch_ledger(ledger)


__all__ = [
    "ARCHIVE_SEARCH_SELECTED_EVIDENCE_SCHEMA",
    "ARCHIVE_AUTHORITY_MAX_ANSWER_BYTES",
    "ARCHIVE_AUTHORITY_MAX_CANDIDATES",
    "ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL",
    "ARCHIVE_AUTHORITY_MAX_COVERAGES",
    "ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES",
    "ARCHIVE_AUTHORITY_MAX_MODEL_BYTES",
    "ArchiveModelBatchLedger",
    "ArchiveSearchAuthorityError",
    "ArchiveSearchAuthorityPhase",
    "ArchiveSearchCoverageGrade",
    "ArchiveSearchCandidateReauthorization",
    "ArchiveSearchCandidateReauthorizer",
    "ArchiveSearchCoverageReauthorization",
    "ArchiveSearchCoverageReauthorizer",
    "ArchiveSearchPublicationAttestation",
    "ArchiveSearchPublicationDenied",
    "ArchiveSearchPublicationDenialReason",
    "ArchiveSearchReauthorizationStatus",
    "ArchiveSearchRunBinding",
    "ArchiveSearchSelectedEvidence",
    "AuthorizedArchiveBatch",
    "IssuedArchiveContinuation",
    "RedeemedArchiveContinuation",
    "abandon_empty_archive_model_batch_ledger",
    "attest_archive_search_before_publication",
    "authorize_archive_search_before_model",
    "authorize_archive_search_resumed_before_model",
    "canonical_archive_search_targets",
    "consume_archive_model_batch_ledger_fail_closed",
    "create_archive_model_batch_ledger",
    "create_archive_search_run_binding",
    "issue_archive_search_continuation",
    "redeem_archive_search_continuation",
]
