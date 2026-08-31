"""Ephemeral real-path measurement for the five document-recall contours."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from friday.config import ensure_runtime_dirs, load_settings
from friday.permissions import AuthorizationService
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayStatus,
    replay_archive_evidence_in_transaction,
)
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BYTES,
    ArchiveSearchCandidateProjectionEntry,
    ArchiveSearchPublicationAttestation,
    ArchiveSearchSelectedEvidence,
    abandon_empty_archive_model_batch_ledger,
    attest_archive_search_before_publication,
    consume_archive_model_batch_ledger_fail_closed,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveMatchChannel,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_service import (
    PreparedArchiveSearch,
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval.contracts import (
    AbsenceDecision,
    AuthorityScope,
    SourceRef,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.retrieval_benchmark._canonical import canonical_json
from friday.retrieval_benchmark.contracts import (
    RecallCaseResultV1,
    RecallCaseV1,
    RecallObservationV1,
    RecallOutcomeV1,
    RecallReportV1,
)
from friday.retrieval_benchmark.document_synthetic import (
    DocumentRecallClassV1,
    _DocumentCaseDiagnostic,
    _DocumentSyntheticPlan,
    document_synthetic_plan,
    seed_document_synthetic,
)
from friday.retrieval_benchmark.harness import (
    RecallHarnessError,
    _accepted_candidate_labels,
    _actor,
    _authorization,
    _continuation,
    _isolated_friday_environment,
)
from friday.retrieval_benchmark.metrics import score_recall, score_recall_case_results
from friday.retrieval_benchmark.release import (
    RecallReleaseIdentityError,
    archive_search_release_sha256,
)
from friday.retrieval_benchmark.synthetic import (
    BOUNDARY_MESSAGE_ID,
    SYNTHETIC_PRINCIPAL,
    SYNTHETIC_TENANT,
)
from friday.storage import FridayStorage, init_storage

_RUNS = itertools.count(1)
_CITATION = re.compile(r"A([1-9][0-9]{0,2})\Z")
_MAX_ARCHIVE_PAGES = 5
_MAX_ARCHIVE_CANDIDATES = 100
_MAX_WRITER_PAGES = 4
_MEASUREMENT_SCHEMA = "friday.document-recall-measurements.body-free.v1"
_GAP_CODES = frozenset(
    {
        "authority_scope_drift",
        "channel_mismatch",
        "discovery_authority_drift",
        "discovery_false_absence",
        "discovery_miss",
        "negative_control_drift",
        "passage_mismatch",
        "qrel_miss",
        "replay_not_exact",
        "safety_false_absence",
        "safety_false_complete",
        "target_not_recalled",
        "temporal_role_mismatch",
    }
)


class DocumentRecallHarnessError(RecallHarnessError):
    """The closed real document-recall journey failed."""


@dataclass(frozen=True, slots=True)
class DocumentCaseMeasurementV1:
    """Body-free facts outside the generic recall observation contract."""

    case_id: str
    recall_class: DocumentRecallClassV1
    target_recalled: bool
    target_rank: int | None
    match_channels: tuple[ArchiveMatchChannel, ...]
    passage_exact: bool
    temporal_role_exact: bool
    discovery_target_visible: bool
    discovery_navigation_only: bool
    discovery_absence_decision: AbsenceDecision
    discovery_exhaustive: bool
    negative_control_exact: bool
    authorized_only: bool
    replay_status: ArchiveEvidenceReplayStatus | None
    replay_model_sha256: str | None
    safety_absence_decision: AbsenceDecision | None
    safety_exhaustive: bool | None
    gap_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        booleans = (
            self.target_recalled,
            self.passage_exact,
            self.temporal_role_exact,
            self.discovery_target_visible,
            self.discovery_navigation_only,
            self.discovery_exhaustive,
            self.negative_control_exact,
            self.authorized_only,
        )
        if (
            len(self.case_id) != 64
            or any(character not in "0123456789abcdef" for character in self.case_id)
            or type(self.recall_class) is not DocumentRecallClassV1
            or any(type(value) is not bool for value in booleans)
            or (
                self.target_rank is not None
                and (type(self.target_rank) is not int or not 1 <= self.target_rank <= 100)
            )
            or self.target_recalled != (self.target_rank is not None)
            or type(self.match_channels) is not tuple
            or any(type(item) is not ArchiveMatchChannel for item in self.match_channels)
            or self.match_channels != tuple(sorted(set(self.match_channels), key=lambda item: item.value))
            or type(self.discovery_absence_decision) is not AbsenceDecision
            or (
                self.replay_status is not None and type(self.replay_status) is not ArchiveEvidenceReplayStatus
            )
            or (
                self.replay_model_sha256 is not None
                and (
                    len(self.replay_model_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in self.replay_model_sha256)
                )
            )
            or (self.replay_status is ArchiveEvidenceReplayStatus.EXACT)
            != (self.replay_model_sha256 is not None)
            or (
                self.safety_absence_decision is not None
                and type(self.safety_absence_decision) is not AbsenceDecision
            )
            or (self.safety_exhaustive is not None and type(self.safety_exhaustive) is not bool)
            or (self.safety_absence_decision is None) != (self.safety_exhaustive is None)
            or type(self.gap_codes) is not tuple
            or self.gap_codes != tuple(sorted(set(self.gap_codes)))
            or not set(self.gap_codes) <= _GAP_CODES
        ):
            raise DocumentRecallHarnessError("document measurement is not closed")

    def to_payload(self) -> dict[str, object]:
        return {
            "authorized_only": self.authorized_only,
            "case_id": self.case_id,
            "discovery_absence_decision": self.discovery_absence_decision.value,
            "discovery_exhaustive": self.discovery_exhaustive,
            "discovery_navigation_only": self.discovery_navigation_only,
            "discovery_target_visible": self.discovery_target_visible,
            "gap_codes": list(self.gap_codes),
            "match_channels": [item.value for item in self.match_channels],
            "negative_control_exact": self.negative_control_exact,
            "passage_exact": self.passage_exact,
            "recall_class": self.recall_class.value,
            "replay_model_sha256": self.replay_model_sha256,
            "replay_status": None if self.replay_status is None else self.replay_status.value,
            "safety_absence_decision": (
                None if self.safety_absence_decision is None else self.safety_absence_decision.value
            ),
            "safety_exhaustive": self.safety_exhaustive,
            "target_rank": self.target_rank,
            "target_recalled": self.target_recalled,
            "temporal_role_exact": self.temporal_role_exact,
        }


@dataclass(frozen=True, slots=True)
class EphemeralDocumentRecallRunV1:
    cases: tuple[RecallCaseV1, ...]
    observations: tuple[RecallObservationV1, ...]
    report: RecallReportV1
    measurements: tuple[DocumentCaseMeasurementV1, ...]
    restart_performed: bool

    def __post_init__(self) -> None:
        if (
            type(self.cases) is not tuple
            or type(self.observations) is not tuple
            or type(self.report) is not RecallReportV1
            or type(self.measurements) is not tuple
            or len(self.cases) != 5
            or len(self.observations) != 5
            or len(self.measurements) != 5
            or self.report.case_count != 5
            or type(self.restart_performed) is not bool
            or tuple(item.case_id for item in self.measurements)
            != tuple(case.opaque_case_id for case in self.cases)
        ):
            raise DocumentRecallHarnessError("document run is not a closed typed result")

    @property
    def case_results(self) -> tuple[RecallCaseResultV1, ...]:
        return score_recall_case_results(self.cases, self.observations)

    @property
    def gap_count(self) -> int:
        return sum(len(item.gap_codes) for item in self.measurements) + int(not self.restart_performed)


@dataclass(frozen=True, slots=True, repr=False)
class _ExecutedSearch:
    prepared: tuple[PreparedArchiveSearch, ...]
    payloads: tuple[Mapping[str, object], ...]
    attestation: ArchiveSearchPublicationAttestation


@dataclass(frozen=True, slots=True, repr=False)
class _PrivateReplayRecord:
    case_id: str
    selected_evidence_json: str
    expected_coverage_grade: ArchiveEvidenceReplayCoverageGrade


@dataclass(frozen=True, slots=True)
class _CaseArtifacts:
    observation: RecallObservationV1
    measurement: DocumentCaseMeasurementV1
    replay_record: _PrivateReplayRecord | None


def _converge_document_writer(storage: FridayStorage) -> None:
    cursor: str | None = None
    for _page in range(_MAX_WRITER_PAGES):
        report = storage.backfill_document_catalog(
            SYNTHETIC_TENANT,
            after_raw_object_id=cursor,
            limit=64,
            include_document_passages=True,
        )
        if type(report) is not dict:
            raise DocumentRecallHarnessError("document writer report is invalid")
        has_more = report.get("has_more")
        next_cursor = report.get("next_after_raw_object_id")
        if (
            type(has_more) is not bool
            or (has_more and (not isinstance(next_cursor, str) or not next_cursor))
            or (not has_more and next_cursor is not None)
        ):
            raise DocumentRecallHarnessError("document writer report is invalid")
        if not has_more:
            return
        cursor = cast(str, next_cursor)
    raise DocumentRecallHarnessError("document writer did not converge within its closed bound")


def _execute_search(
    storage: FridayStorage,
    authorization: AuthorizationService,
    request: ArchiveSearchRequest,
    *,
    release_sha256: str,
    discriminator: str,
) -> _ExecutedSearch:
    actor = _actor()
    ledger = create_archive_model_batch_ledger(
        tenant_id=actor.user_id,
        principal_id=actor.own_id,
        turn_discriminator=discriminator,
    )
    prepared_searches: list[PreparedArchiveSearch] = []
    payloads: list[Mapping[str, object]] = []
    accepted_labels: list[str] = []
    candidate_count = 0
    admitted_bytes = 0
    page_request = request
    admitted = False
    attestation_attempted = False
    try:
        for page_index in range(1, _MAX_ARCHIVE_PAGES + 1):
            with storage.transaction() as conn:
                prepared = prepare_archive_search_in_transaction(
                    conn,
                    authorization=authorization,
                    actor=actor,
                    tenant_id=actor.user_id,
                    principal_id=actor.own_id,
                    request=page_request,
                    snapshot_discriminator=release_sha256,
                    run_discriminator=f"{discriminator}-page-{page_index}",
                    turn_ledger=ledger,
                    current_conversation_id=None,
                    boundary_user_message_id=None,
                )
            payload = prepared.authorized_batch.public_tool_result_payload
            page_labels, page_candidate_count = _accepted_candidate_labels(payload)
            accepted_labels.extend(page_labels)
            candidate_count += page_candidate_count
            model_bytes = prepared.authorized_batch.model_visible_canonical_bytes
            ledger.admit_model_tool_bytes(prepared.run_binding, prepared.authorized_batch, model_bytes)
            admitted = True
            admitted_bytes += len(model_bytes)
            prepared_searches.append(prepared)
            payloads.append(payload)
            token = _continuation(payload)
            if (
                token is None
                or candidate_count >= _MAX_ARCHIVE_CANDIDATES
                or admitted_bytes > ARCHIVE_AUTHORITY_MAX_MODEL_BYTES - 7_900
            ):
                break
            page_request = replace(request, continuation=token)
        if not prepared_searches:
            raise DocumentRecallHarnessError("archive search emitted no typed page")
        ledger.freeze_for_publication()
        with storage.transaction() as conn:
            authority_context = refresh_archive_search_reauthorization_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=actor.user_id,
                principal_id=actor.own_id,
                prepared_searches=tuple(prepared_searches),
            )
        answer = " ".join(f"[{label}]" for label in accepted_labels)
        if not answer:
            answer = "No accepted factual candidate."
        attestation_attempted = True
        attestation = attest_archive_search_before_publication(
            tenant_id=actor.user_id,
            principal_id=actor.own_id,
            ledger=ledger,
            answer=answer,
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=authority_context,
        )
        return _ExecutedSearch(tuple(prepared_searches), tuple(payloads), attestation)
    except Exception as exc:
        if not attestation_attempted:
            try:
                if admitted:
                    consume_archive_model_batch_ledger_fail_closed(ledger)
                else:
                    abandon_empty_archive_model_batch_ledger(ledger)
            except Exception:
                pass
        if isinstance(exc, DocumentRecallHarnessError):
            raise
        raise DocumentRecallHarnessError("real document archive path failed") from exc


def _payload_candidates(
    payloads: tuple[Mapping[str, object], ...],
) -> tuple[dict[str, object], ...]:
    candidates: list[dict[str, object]] = []
    labels: set[str] = set()
    for payload in payloads:
        raw_candidates = payload.get("candidates")
        if type(raw_candidates) is not list:
            raise DocumentRecallHarnessError("archive candidate payload is invalid")
        for raw_candidate in raw_candidates:
            if type(raw_candidate) is not dict:
                raise DocumentRecallHarnessError("archive candidate payload is invalid")
            candidate = cast(dict[str, object], raw_candidate)
            label = candidate.get("label")
            if not isinstance(label, str) or label in labels:
                raise DocumentRecallHarnessError("archive candidate labels are not canonical")
            labels.add(label)
            candidates.append(candidate)
    return tuple(candidates)


def _target_entry(
    attestation: ArchiveSearchPublicationAttestation,
    source_ref: SourceRef,
) -> ArchiveSearchCandidateProjectionEntry | None:
    matches = tuple(
        item for item in attestation.candidate_projection.candidates if item.source_ref == source_ref
    )
    if len(matches) > 1:
        raise DocumentRecallHarnessError("target source was projected more than once")
    return matches[0] if matches else None


def _candidate_by_label(
    payloads: tuple[Mapping[str, object], ...],
    label: str,
) -> dict[str, object]:
    matches = tuple(item for item in _payload_candidates(payloads) if item.get("label") == label)
    if len(matches) != 1:
        raise DocumentRecallHarnessError("target public candidate binding is unavailable")
    return matches[0]


def _candidate_by_filename(
    payloads: tuple[Mapping[str, object], ...],
    filename: str,
) -> dict[str, object] | None:
    matches = tuple(item for item in _payload_candidates(payloads) if item.get("filename") == filename)
    if len(matches) > 1:
        raise DocumentRecallHarnessError("discovery target is ambiguous")
    return matches[0] if matches else None


def _channels(candidate: Mapping[str, object] | None) -> tuple[ArchiveMatchChannel, ...]:
    if candidate is None:
        return ()
    raw = candidate.get("match_channels")
    if type(raw) is not list:
        raise DocumentRecallHarnessError("target match channels are invalid")
    try:
        return tuple(sorted({ArchiveMatchChannel(item) for item in raw}, key=lambda item: item.value))
    except (TypeError, ValueError):
        raise DocumentRecallHarnessError("target match channels are invalid") from None


def _public_absence(search: _ExecutedSearch) -> AbsenceDecision:
    value = search.payloads[-1].get("absence")
    if not isinstance(value, str):
        raise DocumentRecallHarnessError("archive absence projection is invalid")
    try:
        return AbsenceDecision(value)
    except ValueError:
        raise DocumentRecallHarnessError("archive absence projection is invalid") from None


def _public_exhaustive(search: _ExecutedSearch) -> bool:
    value = search.payloads[-1].get("exhaustive")
    if type(value) is not bool:
        raise DocumentRecallHarnessError("archive exhaustive projection is invalid")
    return value


def _temporal_role_exact(
    diagnostic: _DocumentCaseDiagnostic,
    candidate: Mapping[str, object] | None,
) -> bool:
    if diagnostic.recall_class is not DocumentRecallClassV1.DATE:
        return True
    if candidate is None:
        return False
    raw = candidate.get("temporal_facts")
    if type(raw) is not list:
        raise DocumentRecallHarnessError("target temporal facts are invalid")
    return raw == [
        {
            "end": "2024-05-11",
            "origin": TemporalOrigin.LEGACY_COLLAPSED.value,
            "precision": TemporalPrecision.DAY.value,
            "role": TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE.value,
            "start": "2024-05-10",
            "value_kind": TemporalValueKind.DATE_INTERVAL.value,
        }
    ]


def _measurement(
    diagnostic: _DocumentCaseDiagnostic,
    plan: _DocumentSyntheticPlan,
    evidence: _ExecutedSearch,
    discovery: _ExecutedSearch,
    safety: _ExecutedSearch | None,
) -> tuple[DocumentCaseMeasurementV1, _PrivateReplayRecord | None]:
    entry = _target_entry(evidence.attestation, diagnostic.target.source_ref)
    evidence_candidate = (
        None if entry is None else _candidate_by_label(evidence.payloads, entry.public_citation_label)
    )
    negative_entry = _target_entry(
        evidence.attestation,
        diagnostic.negative_control.source_ref,
    )
    negative_candidate = (
        None
        if negative_entry is None
        else _candidate_by_label(evidence.payloads, negative_entry.public_citation_label)
    )
    negative_channels = _channels(negative_candidate)
    negative_control_exact = negative_channels == diagnostic.expected_negative_channels
    channels = _channels(evidence_candidate)
    match = None if entry is None else _CITATION.fullmatch(entry.public_citation_label)
    target_rank = None if match is None else int(match.group(1))
    discovery_candidate = (
        evidence_candidate
        if discovery is evidence
        else _candidate_by_filename(
            discovery.payloads,
            diagnostic.discovery_filename,
        )
    )
    discovery_visible = discovery_candidate is not None
    discovery_navigation_only = bool(
        discovery_candidate is not None
        and discovery_candidate.get("navigation_only") is True
        and discovery_candidate.get("evidence_authority") == "navigation_only"
        and discovery_candidate.get("passages") == []
    )
    expected_navigation = diagnostic.discovery_navigation_only
    discovery_absence = _public_absence(discovery)
    discovery_exhaustive = _public_exhaustive(discovery)
    searches = (evidence, discovery) if safety is None else (evidence, discovery, safety)
    foreign_filenames = {
        item.filename
        for item in plan.documents
        if item.principal_id == plan.foreign_principal_id or item.tenant_id == plan.foreign_tenant_id
    }
    authorized_only = bool(
        all(
            projected.source_ref.authority_scope is AuthorityScope.TENANT_PRINCIPAL
            and projected.source_ref.tenant_id == SYNTHETIC_TENANT
            and projected.source_ref.principal_id == SYNTHETIC_PRINCIPAL
            for projected in evidence.attestation.candidate_projection.candidates
        )
        and not any(
            candidate.get("filename") in foreign_filenames
            for search in searches
            for candidate in _payload_candidates(search.payloads)
        )
    )
    passage_exact = bool(entry is not None and entry.passage_refs == (diagnostic.expected_passage_ref,))
    temporal_exact = _temporal_role_exact(diagnostic, evidence_candidate)
    safety_absence = None if safety is None else _public_absence(safety)
    safety_exhaustive = None if safety is None else _public_exhaustive(safety)

    gaps: set[str] = set()
    if entry is None:
        gaps.add("target_not_recalled")
        gaps.add("replay_not_exact")
    if channels != diagnostic.expected_channels:
        gaps.add("channel_mismatch")
    if not negative_control_exact:
        gaps.add("negative_control_drift")
    if not passage_exact:
        gaps.add("passage_mismatch")
    if not temporal_exact:
        gaps.add("temporal_role_mismatch")
    if not discovery_visible:
        gaps.add("discovery_miss")
    if discovery_visible and discovery_navigation_only != expected_navigation:
        gaps.add("discovery_authority_drift")
    if discovery_absence is not AbsenceDecision.EVIDENCE_FOUND:
        gaps.add("discovery_false_absence")
    if not authorized_only:
        gaps.add("authority_scope_drift")
    if safety is not None:
        if safety_absence is not AbsenceDecision.NOT_ESTABLISHED:
            gaps.add("safety_false_absence")
        if safety_exhaustive is not False:
            gaps.add("safety_false_complete")

    replay_record: _PrivateReplayRecord | None = None
    if entry is not None:
        selected = ArchiveSearchSelectedEvidence(
            corpus=entry.corpus,
            source_ref=entry.source_ref,
            passage_refs=entry.passage_refs,
            resolved_snapshot_sha256=entry.resolved_snapshot_sha256,
        )
        replay_record = _PrivateReplayRecord(
            diagnostic.case.opaque_case_id,
            selected.to_private_json(),
            ArchiveEvidenceReplayCoverageGrade(evidence.attestation.coverage_grade.value),
        )

    return (
        DocumentCaseMeasurementV1(
            case_id=diagnostic.case.opaque_case_id,
            recall_class=diagnostic.recall_class,
            target_recalled=entry is not None,
            target_rank=target_rank,
            match_channels=channels,
            passage_exact=passage_exact,
            temporal_role_exact=temporal_exact,
            discovery_target_visible=discovery_visible,
            discovery_navigation_only=discovery_navigation_only,
            discovery_absence_decision=discovery_absence,
            discovery_exhaustive=discovery_exhaustive,
            negative_control_exact=negative_control_exact,
            authorized_only=authorized_only,
            replay_status=None,
            replay_model_sha256=None,
            safety_absence_decision=safety_absence,
            safety_exhaustive=safety_exhaustive,
            gap_codes=tuple(sorted(gaps)),
        ),
        replay_record,
    )


def _run_case(
    storage: FridayStorage,
    authorization: AuthorizationService,
    diagnostic: _DocumentCaseDiagnostic,
    plan: _DocumentSyntheticPlan,
    *,
    release_sha256: str,
    run_number: int,
) -> _CaseArtifacts:
    prefix = f"document-recall-{run_number}-{diagnostic.case.case_id}"
    evidence = _execute_search(
        storage,
        authorization,
        diagnostic.case.request,
        release_sha256=release_sha256,
        discriminator=f"{prefix}-evidence",
    )
    discovery = (
        evidence
        if diagnostic.discovery_request == diagnostic.case.request
        else _execute_search(
            storage,
            authorization,
            diagnostic.discovery_request,
            release_sha256=release_sha256,
            discriminator=f"{prefix}-discovery",
        )
    )
    safety = (
        None
        if diagnostic.safety_request is None
        else _execute_search(
            storage,
            authorization,
            diagnostic.safety_request,
            release_sha256=release_sha256,
            discriminator=f"{prefix}-safety",
        )
    )
    observation = RecallObservationV1.from_archive_attestation(
        case=diagnostic.case,
        release_sha256=release_sha256,
        attestation=evidence.attestation,
        prepared_searches=evidence.prepared,
    )
    measurement, replay_record = _measurement(
        diagnostic,
        plan,
        evidence,
        discovery,
        safety,
    )
    return _CaseArtifacts(observation, measurement, replay_record)


def _apply_replays(
    storage: FridayStorage,
    authorization: AuthorizationService,
    measurements: tuple[DocumentCaseMeasurementV1, ...],
    records: tuple[_PrivateReplayRecord, ...],
) -> tuple[DocumentCaseMeasurementV1, ...]:
    actor = _actor()
    by_case = {item.case_id: item for item in measurements}
    for record in records:
        try:
            raw = json.loads(record.selected_evidence_json)
            selected = ArchiveSearchSelectedEvidence.from_private_payload(raw)
        except Exception:
            raise DocumentRecallHarnessError("private replay record is not canonical") from None
        if selected.to_private_json() != record.selected_evidence_json:
            raise DocumentRecallHarnessError("private replay record changed during restart")
        with storage.transaction() as conn:
            replay = replay_archive_evidence_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=actor.user_id,
                principal_id=actor.own_id,
                origin_boundary_user_message_id=BOUNDARY_MESSAGE_ID,
                corpus=selected.corpus,
                source_ref=selected.source_ref,
                passage_refs=selected.passage_refs,
                expected_source_snapshot_sha256=selected.resolved_snapshot_sha256,
                expected_coverage_grade=record.expected_coverage_grade,
            )
        measurement = by_case[record.case_id]
        gaps = set(measurement.gap_codes)
        model_sha256 = (
            hashlib.sha256(replay.model_visible_bytes).hexdigest()
            if replay.status is ArchiveEvidenceReplayStatus.EXACT
            else None
        )
        if model_sha256 is None:
            gaps.add("replay_not_exact")
        by_case[record.case_id] = replace(
            measurement,
            replay_status=replay.status,
            replay_model_sha256=model_sha256,
            gap_codes=tuple(sorted(gaps)),
        )
    return tuple(by_case[item.case_id] for item in measurements)


def run_document_ephemeral() -> EphemeralDocumentRecallRunV1:
    """Measure the five closed document classes through real archive authority."""

    run_number = next(_RUNS)
    plan = document_synthetic_plan()
    try:
        release_sha256 = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise DocumentRecallHarnessError("archive release source set is unavailable") from exc

    with tempfile.TemporaryDirectory(prefix="friday-document-recall-") as directory:
        home = Path(directory) / "home"
        with _isolated_friday_environment(home):
            settings = load_settings()
            ensure_runtime_dirs(settings)
            storage: FridayStorage | None = None
            measurements: tuple[DocumentCaseMeasurementV1, ...] = ()
            observations: tuple[RecallObservationV1, ...] = ()
            restart_performed = False
            try:
                storage = init_storage(settings)
                seed_document_synthetic(storage)
                _converge_document_writer(storage)
                authorization = _authorization(storage)
                artifacts = tuple(
                    _run_case(
                        storage,
                        authorization,
                        diagnostic,
                        plan,
                        release_sha256=release_sha256,
                        run_number=run_number,
                    )
                    for diagnostic in plan.diagnostics
                )
                observations = tuple(item.observation for item in artifacts)
                measurements = tuple(item.measurement for item in artifacts)
                replay_records = tuple(
                    item.replay_record for item in artifacts if item.replay_record is not None
                )

                storage.close()
                storage = None
                storage = init_storage(settings)
                restart_performed = True
                measurements = _apply_replays(
                    storage,
                    _authorization(storage),
                    measurements,
                    cast(tuple[_PrivateReplayRecord, ...], replay_records),
                )
                try:
                    current_release_sha256 = archive_search_release_sha256()
                except RecallReleaseIdentityError as exc:
                    raise DocumentRecallHarnessError("archive release source set is unavailable") from exc
                if current_release_sha256 != release_sha256:
                    raise DocumentRecallHarnessError(
                        "archive release source set changed during the benchmark"
                    )
            finally:
                if storage is not None:
                    storage.close(final=True)

    report = score_recall(plan.cases, observations)
    results = score_recall_case_results(plan.cases, observations)
    by_case_result = {item.case_id: item for item in results}
    closed_measurements: list[DocumentCaseMeasurementV1] = []
    for measurement in measurements:
        gaps = set(measurement.gap_codes)
        if by_case_result[measurement.case_id].outcome is not RecallOutcomeV1.HIT:
            gaps.add("qrel_miss")
        closed_measurements.append(replace(measurement, gap_codes=tuple(sorted(gaps))))
    return EphemeralDocumentRecallRunV1(
        plan.cases,
        observations,
        report,
        tuple(closed_measurements),
        restart_performed,
    )


def document_measurements_json(
    measurements: tuple[DocumentCaseMeasurementV1, ...],
) -> str:
    """Return one canonical body-free measurement envelope."""

    if (
        type(measurements) is not tuple
        or len(measurements) != 5
        or any(type(item) is not DocumentCaseMeasurementV1 for item in measurements)
    ):
        raise DocumentRecallHarnessError("document measurements require the closed five cases")
    values = tuple(sorted(measurements, key=lambda item: item.case_id))
    if len({item.case_id for item in values}) != 5:
        raise DocumentRecallHarnessError("document measurements contain duplicate cases")
    return canonical_json(
        {
            "measurements": [item.to_payload() for item in values],
            "schema": _MEASUREMENT_SCHEMA,
        }
    )


__all__ = [
    "DocumentCaseMeasurementV1",
    "DocumentRecallHarnessError",
    "EphemeralDocumentRecallRunV1",
    "document_measurements_json",
    "run_document_ephemeral",
]
