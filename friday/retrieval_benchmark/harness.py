"""Deterministic ephemeral runner through the shipped archive-search authority path."""

from __future__ import annotations

import itertools
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, cast

from friday.config import ensure_runtime_dirs, load_settings
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BYTES,
    abandon_empty_archive_model_batch_ledger,
    attest_archive_search_before_publication,
    consume_archive_model_batch_ledger_fail_closed,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_service import (
    PreparedArchiveSearch,
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval_benchmark._canonical import (
    MAX_JSONL_BYTES,
    MAX_JSONL_ITEMS,
    RecallContractError,
    canonical_json,
)
from friday.retrieval_benchmark.contracts import (
    RecallCaseResultV1,
    RecallCaseV1,
    RecallObservationV1,
    RecallReportV1,
)
from friday.retrieval_benchmark.metrics import score_recall, score_recall_case_results
from friday.retrieval_benchmark.release import (
    RecallReleaseIdentityError,
    archive_search_release_sha256,
)
from friday.retrieval_benchmark.synthetic import (
    BOUNDARY_CONVERSATION_ID,
    BOUNDARY_MESSAGE_ID,
    SYNTHETIC_PRINCIPAL,
    SYNTHETIC_TENANT,
    seed_synthetic_storage,
    synthetic_cases,
)
from friday.storage import FridayStorage, init_storage

_RUNS = itertools.count(1)
_ENVIRONMENT_LOCK = threading.Lock()
_DISABLED_ENV: Final = {
    "FRIDAY_CODE_EXECUTION_ENABLED": "0",
    "FRIDAY_EMBEDDINGS_ENABLED": "0",
    "FRIDAY_LLM_ENABLED": "0",
    "FRIDAY_MCP_ENABLED": "0",
    "FRIDAY_OBSIDIAN_ENABLED": "0",
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
    "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
    "FRIDAY_WORKERS_ENABLED": "0",
}


class RecallHarnessError(RuntimeError):
    """The real ephemeral archive path failed closed."""


@dataclass(frozen=True, slots=True)
class EphemeralRecallRunV1:
    cases: tuple[RecallCaseV1, ...]
    observations: tuple[RecallObservationV1, ...]
    report: RecallReportV1

    def __post_init__(self) -> None:
        if (
            type(self.cases) is not tuple
            or type(self.observations) is not tuple
            or type(self.report) is not RecallReportV1
            or len(self.cases) != len(self.observations)
            or len(self.cases) != self.report.case_count
        ):
            raise RecallHarnessError("ephemeral run is not a closed typed result")

    @property
    def case_results(self) -> tuple[RecallCaseResultV1, ...]:
        """Return in-memory score facts without retaining them in the public report."""

        return score_recall_case_results(self.cases, self.observations)


@contextmanager
def _isolated_friday_environment(home: Path) -> Iterator[None]:
    """Temporarily remove every ambient Friday/Jericho configuration input."""

    with _ENVIRONMENT_LOCK:
        managed = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("FRIDAY_") or key.startswith("JERICHO_")
        }
        for key in tuple(managed):
            os.environ.pop(key, None)
        overrides = {
            **_DISABLED_ENV,
            "FRIDAY_API_TOKEN": "A" * 48,
            "FRIDAY_DATABASE_MUST_EXIST": "0",
            "FRIDAY_ENV_FILE": str(home / "intentionally-absent.env"),
            "FRIDAY_HOME": str(home),
            "FRIDAY_INGESTION_REVIEW_POLICY": "assessed",
            "FRIDAY_TELEGRAM_BRIDGE_SECRET": "B" * 48,
        }
        os.environ.update(overrides)
        try:
            yield
        finally:
            for key in overrides:
                os.environ.pop(key, None)
            os.environ.update(managed)


def _actor() -> ActorContext:
    return ActorContext(
        user_id=SYNTHETIC_TENANT,
        preset_key="user",
        source="retrieval-recall-benchmark",
        shared_tenant=True,
        person_id=SYNTHETIC_PRINCIPAL,
    )


def _authorization(storage: FridayStorage) -> AuthorizationService:
    return AuthorizationService(storage, shared_tenant=SYNTHETIC_TENANT)


def _accepted_candidate_labels(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], int]:
    raw_candidates = payload.get("candidates")
    if type(raw_candidates) is not list:
        raise RecallHarnessError("archive public candidates are invalid")
    accepted_labels: list[str] = []
    for raw_candidate in raw_candidates:
        if type(raw_candidate) is not dict:
            raise RecallHarnessError("archive public candidate is invalid")
        candidate = cast(dict[str, object], raw_candidate)
        label = candidate.get("label")
        raw_passages = candidate.get("passages")
        if (
            not isinstance(label, str)
            or type(raw_passages) is not list
            or type(candidate.get("navigation_only")) is not bool
        ):
            raise RecallHarnessError("archive public candidate projection is invalid")
        if (
            candidate.get("evidence_authority") == ArchiveEvidenceAuthority.CANONICAL.value
            and candidate["navigation_only"] is False
            and bool(raw_passages)
        ):
            accepted_labels.append(label)
    return tuple(accepted_labels), len(raw_candidates)


def _continuation(payload: Mapping[str, object]) -> str | None:
    value = payload.get("continuation")
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise RecallHarnessError("archive continuation is invalid")
    return value


def _run_case(
    storage: FridayStorage,
    authorization: AuthorizationService,
    actor: ActorContext,
    case: RecallCaseV1,
    *,
    release_sha256: str,
    run_number: int,
) -> RecallObservationV1:
    ledger = create_archive_model_batch_ledger(
        tenant_id=SYNTHETIC_TENANT,
        principal_id=SYNTHETIC_PRINCIPAL,
        turn_discriminator=f"recall-benchmark-{run_number}-{case.case_id}",
    )
    prepared_searches: list[PreparedArchiveSearch] = []
    accepted_labels: list[str] = []
    candidate_count = 0
    admitted_bytes = 0
    request: ArchiveSearchRequest = case.request
    admitted = False
    attestation_attempted = False
    try:
        for page_index in range(1, 6):
            current_conversation_id: str | None = None
            boundary_user_message_id: str | None = None
            if ArchiveSearchCorpus.MESSAGES in request.corpora:
                current_conversation_id = BOUNDARY_CONVERSATION_ID
                boundary_user_message_id = BOUNDARY_MESSAGE_ID
            with storage.transaction() as conn:
                prepared = prepare_archive_search_in_transaction(
                    conn,
                    authorization=authorization,
                    actor=actor,
                    tenant_id=SYNTHETIC_TENANT,
                    principal_id=SYNTHETIC_PRINCIPAL,
                    request=request,
                    snapshot_discriminator=release_sha256,
                    run_discriminator=f"recall-{run_number}-{case.case_id}-page-{page_index}",
                    turn_ledger=ledger,
                    current_conversation_id=current_conversation_id,
                    boundary_user_message_id=boundary_user_message_id,
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
            token = _continuation(payload)
            if (
                token is None
                or candidate_count >= 100
                or admitted_bytes > ARCHIVE_AUTHORITY_MAX_MODEL_BYTES - 7_900
            ):
                break
            request = replace(case.request, continuation=token)
        if not prepared_searches:
            raise RecallHarnessError("archive search emitted no typed page")
        ledger.freeze_for_publication()
        with storage.transaction() as conn:
            authority_context = refresh_archive_search_reauthorization_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=SYNTHETIC_TENANT,
                principal_id=SYNTHETIC_PRINCIPAL,
                prepared_searches=tuple(prepared_searches),
            )
        citation_answer = " ".join(f"[{label}]" for label in accepted_labels)
        if not citation_answer:
            citation_answer = "No accepted factual candidate."
        attestation_attempted = True
        attestation = attest_archive_search_before_publication(
            tenant_id=SYNTHETIC_TENANT,
            principal_id=SYNTHETIC_PRINCIPAL,
            ledger=ledger,
            answer=citation_answer,
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=authority_context,
        )
        return RecallObservationV1.from_archive_attestation(
            case=case,
            release_sha256=release_sha256,
            attestation=attestation,
            prepared_searches=prepared_searches,
        )
    except Exception as exc:
        if not attestation_attempted:
            try:
                if admitted:
                    consume_archive_model_batch_ledger_fail_closed(ledger)
                else:
                    abandon_empty_archive_model_batch_ledger(ledger)
            except Exception:
                pass
        if isinstance(exc, RecallHarnessError):
            raise
        raise RecallHarnessError(f"real archive path failed for {case.case_id}") from exc


def run_ephemeral() -> EphemeralRecallRunV1:
    """Seed a temporary corpus and score it through the real read-only facade."""

    run_number = next(_RUNS)
    cases = synthetic_cases()
    try:
        release_sha256 = archive_search_release_sha256()
    except RecallReleaseIdentityError as exc:
        raise RecallHarnessError("archive release source set is unavailable") from exc
    with tempfile.TemporaryDirectory(prefix="friday-recall-benchmark-") as directory:
        home = Path(directory) / "home"
        with _isolated_friday_environment(home):
            settings = load_settings()
            ensure_runtime_dirs(settings)
            storage = init_storage(settings)
            try:
                seed_synthetic_storage(storage)
                authorization = _authorization(storage)
                actor = _actor()
                observations = tuple(
                    _run_case(
                        storage,
                        authorization,
                        actor,
                        case,
                        release_sha256=release_sha256,
                        run_number=run_number,
                    )
                    for case in cases
                )
                try:
                    current_release_sha256 = archive_search_release_sha256()
                except RecallReleaseIdentityError as exc:
                    raise RecallHarnessError("archive release source set is unavailable") from exc
                if current_release_sha256 != release_sha256:
                    raise RecallHarnessError("archive release source set changed during the benchmark")
            finally:
                storage.close(final=True)
    report = score_recall(cases, observations)
    return EphemeralRecallRunV1(cases, observations, report)


def _bounded_jsonl(
    values: tuple[RecallCaseV1 | RecallObservationV1, ...],
    *,
    label: str,
) -> str:
    records: list[str] = []
    total_bytes = 0
    for item in values:
        record = f"{item.to_json()}\n"
        total_bytes += len(record.encode("ascii"))
        if total_bytes > MAX_JSONL_BYTES:
            raise RecallContractError(f"{label} JSONL exceeds its closed byte bound")
        records.append(record)
    return "".join(records)


def observations_jsonl(observations: tuple[RecallObservationV1, ...]) -> str:
    """Canonical body-free JSONL with one exact trailing newline."""

    if (
        not observations
        or len(observations) > MAX_JSONL_ITEMS
        or any(type(item) is not RecallObservationV1 for item in observations)
    ):
        raise RecallContractError("observation JSONL requires typed observations")
    values = tuple(sorted(observations, key=lambda item: item.case_id))
    if len({item.case_id for item in values}) != len(values):
        raise RecallContractError("observation JSONL contains duplicate case IDs")
    return _bounded_jsonl(values, label="observation")


def cases_jsonl(cases: tuple[RecallCaseV1, ...]) -> str:
    """Canonical owner-private input shape with one exact trailing newline."""

    if not cases or len(cases) > MAX_JSONL_ITEMS or any(type(item) is not RecallCaseV1 for item in cases):
        raise RecallContractError("case JSONL requires typed cases")
    values = tuple(sorted(cases, key=lambda item: item.case_id))
    if len({item.case_id for item in values}) != len(values):
        raise RecallContractError("case JSONL contains duplicate case IDs")
    if len({item.privacy_key_hex for item in values}) != len(values):
        raise RecallContractError("case JSONL privacy keys must be unique")
    return _bounded_jsonl(values, label="case")


def run_ephemeral_summary_json(run: EphemeralRecallRunV1) -> str:
    """Return only the body-free report; observations are an explicit sidecar."""

    if type(run) is not EphemeralRecallRunV1:
        raise RecallHarnessError("ephemeral summary requires a typed run")
    # Reparse before emission so no accidental object representation crosses the boundary.
    return RecallReportV1.parse(canonical_json(run.report.to_payload())).to_json()


__all__ = [
    "EphemeralRecallRunV1",
    "RecallHarnessError",
    "archive_search_release_sha256",
    "cases_jsonl",
    "observations_jsonl",
    "run_ephemeral",
    "run_ephemeral_summary_json",
]
