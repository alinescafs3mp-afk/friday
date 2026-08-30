"""Ephemeral real-path measurement for authenticated conversation recall."""

from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from friday.config import ensure_runtime_dirs, load_settings
from friday.permissions import AuthorizationService
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayResult,
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
from friday.retrieval.contracts import MessageWindowLocator, SourceRef
from friday.retrieval_benchmark.contracts import (
    RecallCaseResultV1,
    RecallCaseV1,
    RecallObservationV1,
    RecallOutcomeV1,
    RecallReportV1,
)
from friday.retrieval_benchmark.conversation_synthetic import (
    _ConversationCaseDiagnostic,
    _ConversationSyntheticPlan,
    conversation_synthetic_plan,
    seed_conversation_synthetic_accepted_boundary,
    seed_conversation_synthetic_foreign_saturation,
    seed_conversation_synthetic_late_rows,
    seed_conversation_synthetic_post_boundary,
    seed_conversation_synthetic_pre_backfill,
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
from friday.retrieval_benchmark.synthetic import SYNTHETIC_PRINCIPAL
from friday.storage import FridayStorage, init_storage

_RUNS = itertools.count(1)
_MAX_ARCHIVE_PAGES = 5
_MAX_ARCHIVE_CANDIDATES = 100
_MAX_WRITER_PAGES = 16

# Frozen from the released pre-R5 head/tail renderer.  Keeping both hashes as
# independent artifacts prevents the compatibility measurement from deriving
# its expected value with the implementation that it is meant to check.
_LEGACY_REPLAY_CASE_ID = "conversation.case.0018"
_LEGACY_REPLAY_SNAPSHOT_SHA256 = "a4dd64ce5806a10781bda4aa1ad3eba4342d07276e2db5a1403c71ec2d12e39a"
_LEGACY_REPLAY_MODEL_SHA256 = "c38741db220799d091b9a5c3dc55e3fbe602ddfd8a9a109f630ff213c4918090"


class ConversationRecallHarnessError(RecallHarnessError):
    """The closed real conversation-recall journey failed."""


@dataclass(frozen=True, slots=True)
class ConversationCaseMeasurementV1:
    """Body-free adjunct facts not represented by the generic recall report."""

    case_id: str
    matrix_cell: str
    projection_contour: str
    target_recalled: bool
    candidate_count: int
    match_channels: tuple[ArchiveMatchChannel, ...]
    passage_window_exact: bool
    matched_excerpt_visible: bool
    authorized_only: bool
    privacy_constraints_exact: bool
    replay_status: ArchiveEvidenceReplayStatus | None
    replay_model_sha256: str | None
    legacy_replay_compatible: bool | None
    gap_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            len(self.case_id) != 64
            or any(character not in "0123456789abcdef" for character in self.case_id)
            or self.matrix_cell
            not in {
                "archive",
                "fallback",
                "adjacent",
                "diversity",
                "replay",
                "privacy",
            }
            or self.projection_contour
            not in {
                "current",
                "backfill_pending",
                "source_changed",
                "foreign_saturated",
                "accepted_boundary",
                "post_boundary",
            }
            or type(self.candidate_count) is not int
            or not 0 <= self.candidate_count <= _MAX_ARCHIVE_CANDIDATES
            or type(self.match_channels) is not tuple
            or any(type(item) is not ArchiveMatchChannel for item in self.match_channels)
            or self.match_channels != tuple(sorted(set(self.match_channels), key=lambda item: item.value))
            or any(
                type(value) is not bool
                for value in (
                    self.target_recalled,
                    self.passage_window_exact,
                    self.matched_excerpt_visible,
                    self.authorized_only,
                    self.privacy_constraints_exact,
                )
            )
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
            or (self.legacy_replay_compatible is not None and type(self.legacy_replay_compatible) is not bool)
            or type(self.gap_codes) is not tuple
            or self.gap_codes != tuple(sorted(set(self.gap_codes)))
            or any(
                item
                not in {
                    "authority_scope_drift",
                    "channel_mismatch",
                    "legacy_replay_not_exact",
                    "matched_excerpt_elided",
                    "passage_window_mismatch",
                    "privacy_constraint_leak",
                    "qrel_miss",
                    "replay_not_exact",
                    "target_not_recalled",
                }
                for item in self.gap_codes
            )
        ):
            raise ConversationRecallHarnessError("conversation measurement is not closed")


@dataclass(frozen=True, slots=True)
class EphemeralConversationRecallRunV1:
    cases: tuple[RecallCaseV1, ...]
    observations: tuple[RecallObservationV1, ...]
    report: RecallReportV1
    measurements: tuple[ConversationCaseMeasurementV1, ...]
    writer_restart_resumed: bool

    def __post_init__(self) -> None:
        if (
            type(self.cases) is not tuple
            or type(self.observations) is not tuple
            or type(self.report) is not RecallReportV1
            or type(self.measurements) is not tuple
            or len(self.cases) != 24
            or len(self.cases) != len(self.observations)
            or len(self.cases) != len(self.measurements)
            or self.report.case_count != len(self.cases)
            or type(self.writer_restart_resumed) is not bool
            or tuple(item.case_id for item in self.measurements)
            != tuple(case.opaque_case_id for case in self.cases)
        ):
            raise ConversationRecallHarnessError("conversation run is not a closed typed result")

    @property
    def case_results(self) -> tuple[RecallCaseResultV1, ...]:
        return score_recall_case_results(self.cases, self.observations)

    @property
    def gap_count(self) -> int:
        return sum(len(item.gap_codes) for item in self.measurements) + int(not self.writer_restart_resumed)


@dataclass(frozen=True, slots=True, repr=False)
class _PrivateReplayRecord:
    case_id: str
    selected_evidence_json: str
    expected_coverage_grade: ArchiveEvidenceReplayCoverageGrade
    legacy_snapshot_sha256: str | None
    legacy_model_sha256: str | None

    def __post_init__(self) -> None:
        values = (self.legacy_snapshot_sha256, self.legacy_model_sha256)
        if (values[0] is None) != (values[1] is None) or any(
            value is not None
            and (len(value) != 64 or any(character not in "0123456789abcdef" for character in value))
            for value in values
        ):
            raise ConversationRecallHarnessError("legacy replay artifact is invalid")

    def __repr__(self) -> str:
        return "<_PrivateReplayRecord private_identity=True>"


@dataclass(frozen=True, slots=True)
class _CaseArtifacts:
    observation: RecallObservationV1
    measurement: ConversationCaseMeasurementV1
    replay_record: _PrivateReplayRecord | None


def _validated_writer_report(value: object) -> tuple[bool, str | None, int]:
    if type(value) is not dict:
        raise ConversationRecallHarnessError("conversation writer report is invalid")
    report = cast(dict[str, object], value)
    has_more = report.get("has_more")
    cursor = report.get("next_resume_conversation_id")
    written = report.get("anchors_written")
    if (
        type(has_more) is not bool
        or type(written) is not int
        or written < 0
        or (has_more and (not isinstance(cursor, str) or not cursor))
        or (not has_more and cursor is not None)
    ):
        raise ConversationRecallHarnessError("conversation writer report is invalid")
    return has_more, cast(str | None, cursor), written


def _converge_writer(
    storage: FridayStorage,
    principal_id: str,
    *,
    cursor: str | None = None,
) -> int:
    written = 0
    for _page in range(_MAX_WRITER_PAGES):
        report = storage.backfill_conversation_passages(
            principal_id,
            resume_at_conversation_id=cursor,
            limit=256,
        )
        has_more, cursor, page_written = _validated_writer_report(report)
        written += page_written
        if not has_more:
            return written
    raise ConversationRecallHarnessError("conversation writer did not converge within its closed bound")


def _owner_projection_state(
    storage: FridayStorage,
    principal_id: str,
) -> dict[str, tuple[str, str | None, int]]:
    with storage.transaction() as conn:
        rows = conn.execute(
            """SELECT projection.conversation_id,projection.projection_status,
                      projection.incomplete_reason,projection.passage_count
                 FROM conversation_passage_projections projection
                 JOIN conversations conversation
                   ON conversation.id=projection.conversation_id
                WHERE conversation.user_id=?
                ORDER BY projection.conversation_id""",
            (principal_id,),
        ).fetchall()
    result: dict[str, tuple[str, str | None, int]] = {}
    for row in rows:
        conversation_id = row["conversation_id"]
        status = row["projection_status"]
        reason = row["incomplete_reason"]
        count = row["passage_count"]
        if (
            not isinstance(conversation_id, str)
            or status not in {"current", "incomplete"}
            or (reason is not None and not isinstance(reason, str))
            or type(count) is not int
            or count < 0
            or conversation_id in result
        ):
            raise ConversationRecallHarnessError("conversation writer state is invalid")
        result[conversation_id] = (status, reason, count)
    return result


def _invalidate_committed_prefix_for_cursor_probe(
    storage: FridayStorage,
    principal_id: str,
) -> str:
    """Reset the sole committed prefix without changing its source bytes."""

    with storage.transaction() as conn:
        rows = conn.execute(
            """SELECT projection.conversation_id,
                      projection.indexed_through_message_id
                 FROM conversation_passage_projections projection
                 JOIN conversations conversation
                   ON conversation.id=projection.conversation_id
                WHERE conversation.user_id=?
                  AND projection.projection_status='current'
                  AND projection.passage_count=1
                ORDER BY projection.conversation_id""",
            (principal_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ConversationRecallHarnessError("conversation cursor probe is ambiguous")
        conversation_id = rows[0]["conversation_id"]
        message_id = rows[0]["indexed_through_message_id"]
        if not isinstance(conversation_id, str) or not isinstance(message_id, str):
            raise ConversationRecallHarnessError("conversation cursor probe is invalid")
        changed = conn.execute(
            "UPDATE messages SET created_at=created_at WHERE id=? AND conversation_id=?",
            (message_id, conversation_id),
        )
        if changed.rowcount != 1:
            raise ConversationRecallHarnessError("conversation cursor probe was not exact")
    return conversation_id


def _assert_timestamp_reset_state(
    storage: FridayStorage,
    plan: _ConversationSyntheticPlan,
    *,
    reset_applied: bool,
) -> None:
    if len(plan.timestamp_resets) != 1:
        raise ConversationRecallHarnessError("conversation reset fixture is not closed")
    reset = plan.timestamp_resets[0]
    with storage.transaction() as conn:
        row = conn.execute(
            """SELECT message.created_at,projection.projection_status,
                      projection.incomplete_reason,projection.indexed_message_count,
                      projection.passage_count,
                      (SELECT COUNT(*) FROM conversation_passages passage
                        WHERE passage.conversation_id=projection.conversation_id) AS child_count,
                      (SELECT COUNT(*) FROM messages source
                        WHERE source.conversation_id=projection.conversation_id
                          AND source.role IN ('user','assistant')) AS source_count
                 FROM conversation_passage_projections projection
                 JOIN messages message ON message.id=?
                WHERE projection.conversation_id=?""",
            (reset.message_id, reset.conversation_id),
        ).fetchone()
    if row is None:
        raise ConversationRecallHarnessError("conversation reset state is unavailable")
    expected: tuple[object, ...]
    if reset_applied:
        expected = (
            reset.final_created_at,
            "incomplete",
            "source_changed",
            0,
            0,
            0,
        )
    else:
        source_count = row["source_count"]
        if type(source_count) is not int or source_count < 1:
            raise ConversationRecallHarnessError("conversation reset source is invalid")
        expected = (
            reset.initial_created_at,
            "current",
            None,
            source_count,
            source_count,
            source_count,
        )
    observed = (
        row["created_at"],
        row["projection_status"],
        row["incomplete_reason"],
        row["indexed_message_count"],
        row["passage_count"],
        row["child_count"],
    )
    if observed != expected:
        raise ConversationRecallHarnessError("conversation timestamp reset was not exact")


def _candidate_payloads(
    payloads: tuple[Mapping[str, object], ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for payload in payloads:
        raw_candidates = payload.get("candidates")
        if type(raw_candidates) is not list:
            raise ConversationRecallHarnessError("archive candidate payload is invalid")
        for raw_candidate in raw_candidates:
            if type(raw_candidate) is not dict:
                raise ConversationRecallHarnessError("archive candidate payload is invalid")
            candidate = cast(dict[str, object], raw_candidate)
            label = candidate.get("label")
            if not isinstance(label, str) or label in result:
                raise ConversationRecallHarnessError("archive candidate labels are not canonical")
            result[label] = candidate
    return result


def _target_entry(
    attestation: ArchiveSearchPublicationAttestation,
    *,
    source_ref: SourceRef,
) -> ArchiveSearchCandidateProjectionEntry | None:
    matches = tuple(
        entry for entry in attestation.candidate_projection.candidates if entry.source_ref == source_ref
    )
    if len(matches) > 1:
        raise ConversationRecallHarnessError("target source was projected more than once")
    return matches[0] if matches else None


def _legacy_replay_artifact(
    diagnostic: _ConversationCaseDiagnostic,
    entry: ArchiveSearchCandidateProjectionEntry,
) -> tuple[str, str] | None:
    if diagnostic.case.case_id != _LEGACY_REPLAY_CASE_ID:
        return None
    if entry.passage_refs != (diagnostic.passage_ref,):
        raise ConversationRecallHarnessError("legacy replay passage binding changed")
    if entry.resolved_snapshot_sha256 == _LEGACY_REPLAY_SNAPSHOT_SHA256:
        raise ConversationRecallHarnessError("legacy and current replay snapshots collapsed")
    return (
        _LEGACY_REPLAY_SNAPSHOT_SHA256,
        _LEGACY_REPLAY_MODEL_SHA256,
    )


def _measurement(
    *,
    diagnostic: _ConversationCaseDiagnostic,
    plan: _ConversationSyntheticPlan,
    attestation: ArchiveSearchPublicationAttestation,
    payloads: tuple[Mapping[str, object], ...],
) -> tuple[ConversationCaseMeasurementV1, _PrivateReplayRecord | None]:
    # The synthetic plan is package-private by design.  These exact attributes
    # are its only harness seam and are never copied into the public report.
    case = diagnostic.case
    source_ref = diagnostic.source_ref
    passage_ref = diagnostic.passage_ref
    expected_channels = diagnostic.expected_channels
    entry = _target_entry(attestation, source_ref=source_ref)
    payload_by_label = _candidate_payloads(payloads)
    target_payload = None if entry is None else payload_by_label.get(entry.public_citation_label)
    if entry is not None and target_payload is None:
        raise ConversationRecallHarnessError("target projection lost its model-visible binding")

    observed_channels: tuple[ArchiveMatchChannel, ...] = ()
    matched_excerpt_visible = False
    if target_payload is not None:
        raw_channels = target_payload.get("match_channels")
        raw_passages = target_payload.get("passages")
        if type(raw_channels) is not list or type(raw_passages) is not list:
            raise ConversationRecallHarnessError("target candidate payload is invalid")
        try:
            observed_channels = tuple(
                sorted(
                    {ArchiveMatchChannel(item) for item in raw_channels},
                    key=lambda item: item.value,
                )
            )
        except (TypeError, ValueError):
            raise ConversationRecallHarnessError("target match channels are invalid") from None
        anchor = next(
            (row for row in plan.messages if row.message_id == diagnostic.anchor_message_id),
            None,
        )
        if anchor is None:
            raise ConversationRecallHarnessError("target anchor is unavailable")
        anchor_text = " ".join(anchor.content.split())
        excerpts: list[str] = []
        for raw_passage in raw_passages:
            if type(raw_passage) is not dict or not isinstance(raw_passage.get("excerpt"), str):
                raise ConversationRecallHarnessError("target candidate passage is invalid")
            excerpts.append(cast(str, raw_passage["excerpt"]))
        matched_excerpt_visible = bool(anchor_text) and any(anchor_text in excerpt for excerpt in excerpts)

    passage_window_exact = bool(entry is not None and passage_ref in entry.passage_refs)
    authorized_only = all(
        projected.source_ref.principal_id == source_ref.principal_id
        for projected in attestation.candidate_projection.candidates
    )
    forbidden_sources = set(diagnostic.forbidden_source_refs)
    forbidden_messages = set(diagnostic.forbidden_message_ids)
    requested_roles = set(case.request.roles)
    projected_passages = tuple(
        passage
        for projected in attestation.candidate_projection.candidates
        for passage in projected.passage_refs
    )
    privacy_constraints_exact = bool(
        all(
            projected.source_ref not in forbidden_sources
            for projected in attestation.candidate_projection.candidates
        )
        and all(
            type(passage.locator) is MessageWindowLocator
            and (not requested_roles or passage.locator.matched_role in requested_roles)
            and passage.locator.first_message_id not in forbidden_messages
            and passage.locator.last_message_id not in forbidden_messages
            for passage in projected_passages
        )
    )
    gaps: set[str] = set()
    if entry is None:
        gaps.add("target_not_recalled")
    if observed_channels != expected_channels:
        gaps.add("channel_mismatch")
    if not passage_window_exact:
        gaps.add("passage_window_mismatch")
    if diagnostic.matrix_cell.value == "adjacent" and not matched_excerpt_visible:
        gaps.add("matched_excerpt_elided")
    if not authorized_only:
        gaps.add("authority_scope_drift")
    if not privacy_constraints_exact:
        gaps.add("privacy_constraint_leak")
    if diagnostic.restart_replay and entry is None:
        gaps.add("replay_not_exact")

    replay_record: _PrivateReplayRecord | None = None
    if diagnostic.restart_replay and entry is not None:
        legacy_artifact = _legacy_replay_artifact(diagnostic, entry)
        selected = ArchiveSearchSelectedEvidence(
            corpus=entry.corpus,
            source_ref=entry.source_ref,
            passage_refs=entry.passage_refs,
            resolved_snapshot_sha256=entry.resolved_snapshot_sha256,
        )
        replay_record = _PrivateReplayRecord(
            case_id=case.opaque_case_id,
            selected_evidence_json=selected.to_private_json(),
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade(attestation.coverage_grade.value),
            legacy_snapshot_sha256=(None if legacy_artifact is None else legacy_artifact[0]),
            legacy_model_sha256=(None if legacy_artifact is None else legacy_artifact[1]),
        )

    return (
        ConversationCaseMeasurementV1(
            case_id=case.opaque_case_id,
            matrix_cell=diagnostic.matrix_cell.value,
            projection_contour=diagnostic.projection_contour.value,
            target_recalled=entry is not None,
            candidate_count=attestation.candidate_projection.candidate_count,
            match_channels=observed_channels,
            passage_window_exact=passage_window_exact,
            matched_excerpt_visible=matched_excerpt_visible,
            authorized_only=authorized_only,
            privacy_constraints_exact=privacy_constraints_exact,
            replay_status=None,
            replay_model_sha256=None,
            legacy_replay_compatible=None,
            gap_codes=tuple(sorted(gaps)),
        ),
        replay_record,
    )


def _run_case(
    storage: FridayStorage,
    authorization: AuthorizationService,
    case: RecallCaseV1,
    diagnostic: _ConversationCaseDiagnostic,
    plan: _ConversationSyntheticPlan,
    *,
    release_sha256: str,
    run_number: int,
) -> _CaseArtifacts:
    actor = _actor()
    ledger = create_archive_model_batch_ledger(
        tenant_id=actor.user_id,
        principal_id=actor.own_id,
        turn_discriminator=f"conversation-recall-{run_number}-{case.case_id}",
    )
    prepared_searches: list[PreparedArchiveSearch] = []
    payloads: list[Mapping[str, object]] = []
    accepted_labels: list[str] = []
    candidate_count = 0
    admitted_bytes = 0
    request: ArchiveSearchRequest = case.request
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
                    request=request,
                    snapshot_discriminator=release_sha256,
                    run_discriminator=(f"conversation-recall-{run_number}-{case.case_id}-page-{page_index}"),
                    turn_ledger=ledger,
                    current_conversation_id=plan.accepted_conversation_id,
                    boundary_user_message_id=plan.accepted_boundary_message_id,
                )
            payload = prepared.authorized_batch.public_tool_result_payload
            page_labels, page_candidate_count = _accepted_candidate_labels(payload)
            accepted_labels.extend(page_labels)
            candidate_count += page_candidate_count
            model_bytes = prepared.authorized_batch.model_visible_canonical_bytes
            ledger.admit_model_tool_bytes(
                prepared.run_binding,
                prepared.authorized_batch,
                model_bytes,
            )
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
            request = replace(case.request, continuation=token)
        if not prepared_searches:
            raise ConversationRecallHarnessError("archive search emitted no typed page")
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
        observation = RecallObservationV1.from_archive_attestation(
            case=case,
            release_sha256=release_sha256,
            attestation=attestation,
            prepared_searches=prepared_searches,
        )
        measurement, replay_record = _measurement(
            diagnostic=diagnostic,
            plan=plan,
            attestation=attestation,
            payloads=tuple(payloads),
        )
        return _CaseArtifacts(observation, measurement, replay_record)
    except Exception as exc:
        if not attestation_attempted:
            try:
                if admitted:
                    consume_archive_model_batch_ledger_fail_closed(ledger)
                else:
                    abandon_empty_archive_model_batch_ledger(ledger)
            except Exception:
                pass
        if isinstance(exc, ConversationRecallHarnessError):
            raise
        raise ConversationRecallHarnessError(
            f"real conversation archive path failed for {case.case_id}"
        ) from exc


def _exact_replay_model_sha256(replay: ArchiveEvidenceReplayResult) -> str | None:
    if replay.status is not ArchiveEvidenceReplayStatus.EXACT:
        return None
    return hashlib.sha256(replay.model_visible_bytes).hexdigest()


def _apply_replays(
    storage: FridayStorage,
    authorization: AuthorizationService,
    plan: _ConversationSyntheticPlan,
    measurements: tuple[ConversationCaseMeasurementV1, ...],
    records: tuple[_PrivateReplayRecord, ...],
) -> tuple[ConversationCaseMeasurementV1, ...]:
    actor = _actor()
    by_case = {item.case_id: item for item in measurements}
    for record in records:
        try:
            raw = json.loads(record.selected_evidence_json)
            selected = ArchiveSearchSelectedEvidence.from_private_payload(raw)
        except Exception:
            raise ConversationRecallHarnessError("private replay record is not canonical") from None
        if selected.to_private_json() != record.selected_evidence_json:
            raise ConversationRecallHarnessError("private replay record changed during restart")
        with storage.transaction() as conn:
            replay = replay_archive_evidence_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=actor.user_id,
                principal_id=actor.own_id,
                origin_boundary_user_message_id=plan.accepted_boundary_message_id,
                corpus=selected.corpus,
                source_ref=selected.source_ref,
                passage_refs=selected.passage_refs,
                expected_source_snapshot_sha256=selected.resolved_snapshot_sha256,
                expected_coverage_grade=record.expected_coverage_grade,
            )
        measurement = by_case[record.case_id]
        gaps = set(measurement.gap_codes)
        model_sha256 = _exact_replay_model_sha256(replay)
        if model_sha256 is None:
            gaps.add("replay_not_exact")
        legacy_compatible: bool | None = None
        if record.legacy_snapshot_sha256 is not None:
            if record.legacy_model_sha256 is None:
                raise ConversationRecallHarnessError("legacy replay model artifact is unavailable")
            with storage.transaction() as conn:
                legacy_replay = replay_archive_evidence_in_transaction(
                    conn,
                    authorization=authorization,
                    actor=actor,
                    tenant_id=actor.user_id,
                    principal_id=actor.own_id,
                    origin_boundary_user_message_id=plan.accepted_boundary_message_id,
                    corpus=selected.corpus,
                    source_ref=selected.source_ref,
                    passage_refs=selected.passage_refs,
                    expected_source_snapshot_sha256=record.legacy_snapshot_sha256,
                    expected_coverage_grade=record.expected_coverage_grade,
                )
            legacy_model_sha256 = _exact_replay_model_sha256(legacy_replay)
            legacy_compatible = legacy_model_sha256 == record.legacy_model_sha256
            if not legacy_compatible:
                gaps.add("legacy_replay_not_exact")
        by_case[record.case_id] = replace(
            measurement,
            replay_status=replay.status,
            replay_model_sha256=model_sha256,
            legacy_replay_compatible=legacy_compatible,
            gap_codes=tuple(sorted(gaps)),
        )
    return tuple(by_case[item.case_id] for item in measurements)


def run_conversation_ephemeral() -> EphemeralConversationRecallRunV1:
    """Measure 24 closed cases through the real archive and replay seams."""

    run_number = next(_RUNS)
    plan = conversation_synthetic_plan()
    cases = plan.cases
    try:
        release_sha256 = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise ConversationRecallHarnessError("archive release source set is unavailable") from exc

    with tempfile.TemporaryDirectory(prefix="friday-conversation-recall-") as directory:
        home = Path(directory) / "home"
        with _isolated_friday_environment(home):
            settings = load_settings()
            ensure_runtime_dirs(settings)
            observations: tuple[RecallObservationV1, ...] = ()
            measurements: tuple[ConversationCaseMeasurementV1, ...] = ()
            writer_restart_resumed = False
            storage: FridayStorage | None = None
            try:
                storage = init_storage(settings)
                seed_conversation_synthetic_pre_backfill(storage)
                seed_conversation_synthetic_foreign_saturation(storage)
                _converge_writer(storage, plan.foreign_principal_id)

                first_report = storage.backfill_conversation_passages(
                    SYNTHETIC_PRINCIPAL,
                    limit=1,
                )
                has_more, restart_cursor, first_written = _validated_writer_report(first_report)
                if not has_more or restart_cursor is None or first_written != 1:
                    raise ConversationRecallHarnessError(
                        "conversation writer did not expose a restartable committed prefix"
                    )
                committed_state = _owner_projection_state(storage, SYNTHETIC_PRINCIPAL)
                committed_prefixes = tuple(
                    conversation_id for conversation_id, state in committed_state.items() if state[2] > 0
                )
                if len(committed_prefixes) != 1 or committed_state[committed_prefixes[0]] != (
                    "current",
                    None,
                    1,
                ):
                    raise ConversationRecallHarnessError(
                        "conversation writer prefix is not independently observable"
                    )
                committed_prefix_id = _invalidate_committed_prefix_for_cursor_probe(
                    storage,
                    SYNTHETIC_PRINCIPAL,
                )
                before_restart = _owner_projection_state(storage, SYNTHETIC_PRINCIPAL)
                if before_restart.get(committed_prefix_id) != (
                    "incomplete",
                    "source_changed",
                    0,
                ):
                    raise ConversationRecallHarnessError(
                        "conversation cursor probe did not reset the committed prefix"
                    )
                storage.close()
                storage = None
                storage = init_storage(settings)
                resumed_report = storage.backfill_conversation_passages(
                    SYNTHETIC_PRINCIPAL,
                    resume_at_conversation_id=restart_cursor,
                    limit=1,
                )
                resumed_has_more, resumed_cursor, resumed_written = _validated_writer_report(resumed_report)
                after_restart = _owner_projection_state(storage, SYNTHETIC_PRINCIPAL)
                changed_conversations = tuple(
                    conversation_id
                    for conversation_id, state in after_restart.items()
                    if state != before_restart.get(conversation_id)
                )
                writer_restart_resumed = bool(
                    resumed_has_more
                    and resumed_cursor is not None
                    and resumed_cursor != restart_cursor
                    and resumed_written == 1
                    and after_restart.get(committed_prefix_id) == before_restart[committed_prefix_id]
                    and len(changed_conversations) == 1
                    and changed_conversations[0] != committed_prefix_id
                    and after_restart[changed_conversations[0]][2]
                    == before_restart[changed_conversations[0]][2] + 1
                )
                _converge_writer(
                    storage,
                    SYNTHETIC_PRINCIPAL,
                    cursor=resumed_cursor,
                )
                # The probe deliberately invalidates a prefix behind the
                # issued cursor.  A fresh bounded cycle closes that synthetic
                # race only after the cursor-honoring observation is sealed.
                _converge_writer(storage, SYNTHETIC_PRINCIPAL)
                _assert_timestamp_reset_state(storage, plan, reset_applied=False)

                seed_conversation_synthetic_late_rows(storage)
                _assert_timestamp_reset_state(storage, plan, reset_applied=True)
                seed_conversation_synthetic_accepted_boundary(storage)
                seed_conversation_synthetic_post_boundary(storage)
                authorization = _authorization(storage)
                artifacts = tuple(
                    _run_case(
                        storage,
                        authorization,
                        case,
                        plan.diagnostic(case.case_id),
                        plan,
                        release_sha256=release_sha256,
                        run_number=run_number,
                    )
                    for case in cases
                )
                observations = tuple(item.observation for item in artifacts)
                measurements = tuple(item.measurement for item in artifacts)
                replay_records = tuple(
                    item.replay_record for item in artifacts if item.replay_record is not None
                )

                storage.close()
                storage = None
                storage = init_storage(settings)
                measurements = _apply_replays(
                    storage,
                    _authorization(storage),
                    plan,
                    measurements,
                    cast(tuple[_PrivateReplayRecord, ...], replay_records),
                )
                try:
                    current_release_sha256 = archive_search_release_sha256()
                except RecallReleaseIdentityError as exc:
                    raise ConversationRecallHarnessError("archive release source set is unavailable") from exc
                if current_release_sha256 != release_sha256:
                    raise ConversationRecallHarnessError(
                        "archive release source set changed during the benchmark"
                    )
            finally:
                if storage is not None:
                    storage.close(final=True)

    report = score_recall(cases, observations)
    results = score_recall_case_results(cases, observations)
    by_case_result = {item.case_id: item for item in results}
    closed_measurements: list[ConversationCaseMeasurementV1] = []
    for measurement in measurements:
        gaps = set(measurement.gap_codes)
        if by_case_result[measurement.case_id].outcome is not RecallOutcomeV1.HIT:
            gaps.add("qrel_miss")
        closed_measurements.append(replace(measurement, gap_codes=tuple(sorted(gaps))))
    return EphemeralConversationRecallRunV1(
        cases,
        observations,
        report,
        tuple(closed_measurements),
        writer_restart_resumed,
    )


__all__ = [
    "ConversationCaseMeasurementV1",
    "ConversationRecallHarnessError",
    "EphemeralConversationRecallRunV1",
    "run_conversation_ephemeral",
]
