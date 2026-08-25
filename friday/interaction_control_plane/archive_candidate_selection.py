"""Body-free durable candidate selection contracts for one archive question.

The projection retains only exact source/passage identities, immutable snapshot
digests, closed coverage state and an ordinal question.  It deliberately has no
field capable of storing a query, title, filename, excerpt, prompt or model
prose.  Candidate contents remain in their authoritative stores and must be
replayed through the existing selected-evidence authority boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
    SelectedArchiveEvidenceError,
    canonical_passage_refs_json,
    parse_canonical_passage_refs,
)
from friday.interaction_control_plane.work_item_contract import (
    ARCHIVE_CANDIDATE_MAX_COUNT,
    ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON,
    ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_SCHEMA,
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkItemContractError,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
    canonical_work_item_instant,
)
from friday.retrieval.archive_search_authority import (
    ARCHIVE_SEARCH_ACCEPTED_CANDIDATE_PROJECTION_SCHEMA,
    ARCHIVE_SEARCH_CANDIDATE_PROJECTION_ENTRY_SCHEMA,
    ArchiveSearchAcceptedCandidateProjection,
    ArchiveSearchAuthorityError,
)
from friday.retrieval.contracts import PassageRef, SourceRef

ARCHIVE_CANDIDATE_ITEM_SCHEMA = "friday.archive-candidate-item.v1"
ARCHIVE_CANDIDATE_SET_SCHEMA = "friday.archive-candidate-set.v1"
ARCHIVE_CANDIDATE_QUESTION_SCHEMA = "friday.archive-candidate-ordinal-question.v1"
ARCHIVE_CANDIDATE_WORK_ITEM_SCHEMA = "friday.archive-candidate-selection-work-item.v1"
ARCHIVE_CANDIDATE_REASK_VERDICT_KIND = "archive_candidate_ordinal_reask"

_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CANDIDATE_SET_ID_RE = re.compile(r"cset_[0-9a-f]{16}\Z")
_QUESTION_ID_RE = re.compile(r"question_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_CITATION_LABEL_RE = re.compile(r"A([1-9][0-9]{0,2})\Z")
_ORDINAL_TRIM = " \t\r\n.,!?;:()[]{}\"'«»“”„`"
_RU_ORDINALS = (
    "первый",
    "второй",
    "третий",
    "четвертый",
    "пятый",
    "шестой",
    "седьмой",
    "восьмой",
    "девятый",
    "десятый",
    "одиннадцатый",
    "двенадцатый",
    "тринадцатый",
    "четырнадцатый",
    "пятнадцатый",
    "шестнадцатый",
    "семнадцатый",
    "восемнадцатый",
    "девятнадцатый",
    "двадцатый",
)
_EN_ORDINALS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
    "twentieth",
)
_WORD_ORDINALS = {
    **{value: index for index, value in enumerate(_RU_ORDINALS, start=1)},
    "четвёртый": 4,
    **{value: index for index, value in enumerate(_EN_ORDINALS, start=1)},
}


class ArchiveCandidateSelectionError(WorkItemContractError):
    """A value is outside the closed candidate-selection foundation."""


class ArchiveCandidateQuestionKind(StrEnum):
    SELECT_ORDINAL = "select_archive_candidate_ordinal"


class ArchiveCandidateQuestionState(StrEnum):
    WAITING = "waiting"
    ANSWERED = "answered"


def parse_archive_candidate_ordinal(value: object) -> int | None:
    """Parse only a standalone RU/EN ordinal inside the global 1..20 contract."""

    if type(value) is not str:
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if not encoded or len(encoded) > 64:
        return None
    normalized = value.strip(_ORDINAL_TRIM).casefold()
    if not normalized or any(character.isspace() for character in normalized):
        return None
    word = _WORD_ORDINALS.get(normalized)
    if word is not None:
        return word
    numeric = re.fullmatch(r"([1-9]|1[0-9]|20)(?:-(?:й|ый|ой))?", normalized)
    if numeric is not None:
        return int(numeric.group(1))
    english = re.fullmatch(r"([1-9]|1[0-9]|20)(st|nd|rd|th)", normalized)
    if english is None:
        return None
    ordinal = int(english.group(1))
    expected_suffix = (
        "th"
        if 10 <= ordinal % 100 <= 20
        else {1: "st", 2: "nd", 3: "rd"}.get(
            ordinal % 10,
            "th",
        )
    )
    return ordinal if english.group(2) == expected_suffix else None


def archive_candidate_reask_prompt(maximum_ordinal: object) -> str:
    """Return the one deterministic source-free re-ask body; it is never persisted here."""

    if type(maximum_ordinal) is not int or not 2 <= maximum_ordinal <= ARCHIVE_CANDIDATE_MAX_COUNT:
        raise ArchiveCandidateSelectionError("candidate re-ask range is invalid")
    return (
        "Не распознал выбор. Ответьте только номером от 1 до "
        f"{maximum_ordinal} или одним порядковым словом (RU/EN)."
    )


def _public_citation_label(value: object) -> str:
    if not isinstance(value, str) or (match := _PUBLIC_CITATION_LABEL_RE.fullmatch(value)) is None:
        raise ArchiveCandidateSelectionError("candidate public citation label is invalid")
    if int(match.group(1)) > 640:
        raise ArchiveCandidateSelectionError("candidate public citation label is invalid")
    return value


def archive_candidate_selection_offer_suffix(public_citation_labels: object) -> str:
    """Render the exact body-free ordinal-to-public-label offer appended after model prose."""

    if type(public_citation_labels) is not tuple or not (
        2 <= len(public_citation_labels) <= ARCHIVE_CANDIDATE_MAX_COUNT
    ):
        raise ArchiveCandidateSelectionError("candidate offer labels are outside the closed limit")
    labels = tuple(_public_citation_label(value) for value in public_citation_labels)
    if len(labels) != len(set(labels)):
        raise ArchiveCandidateSelectionError("candidate offer labels must be unique")
    lines = ["Выберите источник:"]
    lines.extend(f"{ordinal} — {label}" for ordinal, label in enumerate(labels, start=1))
    lines.append(f"Ответьте только номером от 1 до {len(labels)} или одним порядковым словом (RU/EN).")
    return "\n".join(lines)


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ArchiveCandidateSelectionError(f"{label} is not a canonical identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ArchiveCandidateSelectionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ArchiveCandidateSelectionError("candidate value is not canonical JSON") from None


def _exact_keys(value: Mapping[str, object], expected: Iterable[str], *, label: str) -> None:
    if frozenset(value) != frozenset(expected):
        raise ArchiveCandidateSelectionError(f"{label} keys do not match the closed contract")


def _canonical_instant(value: object, *, label: str) -> str:
    try:
        result = canonical_work_item_instant(value, label=label)
    except WorkItemContractError as exc:
        raise ArchiveCandidateSelectionError(str(exc)) from exc
    if result != value:
        raise ArchiveCandidateSelectionError(f"{label} must already be canonical")
    return result


@dataclass(frozen=True, slots=True)
class ArchiveCandidateSelectionActiveFrame:
    """Closed marker; the exact set and question live in typed sidecars."""

    @classmethod
    def parse(cls, value: object) -> ArchiveCandidateSelectionActiveFrame:
        if value != ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON:
            raise ArchiveCandidateSelectionError("archive candidate active frame is invalid")
        return cls()

    def to_payload(self) -> dict[str, str]:
        return {"schema": ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_SCHEMA}

    def to_json(self) -> str:
        return ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveCandidateItem:
    """One ordered exact source reference; never a display or content carrier."""

    ordinal: int
    public_citation_label: str
    corpus: SelectedArchiveCorpus
    source_ref: SourceRef
    passage_refs: tuple[PassageRef, ...]
    source_snapshot_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or not 1 <= self.ordinal <= ARCHIVE_CANDIDATE_MAX_COUNT
        ):
            raise ArchiveCandidateSelectionError("candidate ordinal is outside the closed limit")
        if not isinstance(self.corpus, SelectedArchiveCorpus):
            raise ArchiveCandidateSelectionError("candidate corpus is invalid")
        _public_citation_label(self.public_citation_label)
        if type(self.source_ref) is not SourceRef:
            raise ArchiveCandidateSelectionError("candidate source_ref is invalid")
        if type(self.passage_refs) is not tuple or any(
            type(item) is not PassageRef for item in self.passage_refs
        ):
            raise ArchiveCandidateSelectionError("candidate passage refs are invalid")
        _digest(self.source_snapshot_sha256, label="source_snapshot_sha256")

    def __repr__(self) -> str:
        return (
            "ArchiveCandidateItem(private_source=True, "
            f"ordinal={self.ordinal}, passage_count={len(self.passage_refs)})"
        )

    def _selected_evidence(
        self,
        *,
        work_item_id: str,
        coverage_sha256: str,
        coverage_grade: SelectedArchiveCoverageGrade,
        origin_boundary_user_message_id: str,
    ) -> SelectedArchiveEvidence:
        try:
            return SelectedArchiveEvidence(
                work_item_id=work_item_id,
                corpus=self.corpus,
                source_ref=self.source_ref,
                passage_refs=self.passage_refs,
                source_snapshot_sha256=self.source_snapshot_sha256,
                coverage_sha256=coverage_sha256,
                coverage_grade=coverage_grade,
                origin_boundary_user_message_id=origin_boundary_user_message_id,
            )
        except SelectedArchiveEvidenceError as exc:
            raise ArchiveCandidateSelectionError("candidate is not replayable selected evidence") from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "corpus": self.corpus.value,
            "ordinal": self.ordinal,
            "public_citation_label": self.public_citation_label,
            "passage_refs": [item.to_private_payload() for item in self.passage_refs],
            "schema": ARCHIVE_CANDIDATE_ITEM_SCHEMA,
            "source_ref": self.source_ref.to_private_payload(),
            "source_snapshot_sha256": self.source_snapshot_sha256,
        }


def _authority_projection_sha256(
    *,
    candidates: tuple[ArchiveCandidateItem, ...],
    coverage_grade: SelectedArchiveCoverageGrade,
    coverage_sha256: str,
    evidence_sha256: str,
) -> str:
    """Rebuild the exact public phase-2 projection digest from durable identities."""

    payload = {
        "candidate_count": len(candidates),
        "candidates": [
            {
                "corpus": candidate.corpus.value,
                "ordinal": candidate.ordinal,
                "passage_refs": [item.to_private_payload() for item in candidate.passage_refs],
                "public_citation_label": candidate.public_citation_label,
                "resolved_snapshot_sha256": candidate.source_snapshot_sha256,
                "schema": ARCHIVE_SEARCH_CANDIDATE_PROJECTION_ENTRY_SCHEMA,
                "source_ref": candidate.source_ref.to_private_payload(),
            }
            for candidate in candidates
        ],
        "coverage_grade": coverage_grade.value,
        "coverage_sha256": coverage_sha256,
        "evidence_sha256": evidence_sha256,
        "schema": ARCHIVE_SEARCH_ACCEPTED_CANDIDATE_PROJECTION_SCHEMA,
    }
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveCandidateSet:
    """One immutable, ordered candidate set bound to an accepted archive turn."""

    id: str
    work_item_id: str
    evidence_sha256: str
    coverage_sha256: str
    coverage_grade: SelectedArchiveCoverageGrade
    authority_projection_sha256: str
    origin_boundary_user_message_id: str
    candidates: tuple[ArchiveCandidateItem, ...]

    def __post_init__(self) -> None:
        _identifier(self.id, _CANDIDATE_SET_ID_RE, label="candidate_set_id")
        work_item_id = _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _digest(self.evidence_sha256, label="evidence_sha256")
        coverage_sha256 = _digest(self.coverage_sha256, label="coverage_sha256")
        _digest(
            self.authority_projection_sha256,
            label="authority_projection_sha256",
        )
        if not isinstance(self.coverage_grade, SelectedArchiveCoverageGrade):
            raise ArchiveCandidateSelectionError("coverage_grade is invalid")
        origin = _identifier(
            self.origin_boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="origin_boundary_user_message_id",
        )
        if (
            type(self.candidates) is not tuple
            or not 2 <= len(self.candidates) <= ARCHIVE_CANDIDATE_MAX_COUNT
            or tuple(item.ordinal for item in self.candidates) != tuple(range(1, len(self.candidates) + 1))
            or any(type(item) is not ArchiveCandidateItem for item in self.candidates)
        ):
            raise ArchiveCandidateSelectionError("candidate set ordering/cardinality is invalid")
        source_identities = tuple(item.source_ref.to_private_json() for item in self.candidates)
        if len(source_identities) != len(set(source_identities)):
            raise ArchiveCandidateSelectionError("candidate sources must be unique")
        citation_labels = tuple(item.public_citation_label for item in self.candidates)
        if len(citation_labels) != len(set(citation_labels)):
            raise ArchiveCandidateSelectionError("candidate public citation labels must be unique")
        if not hmac.compare_digest(
            self.authority_projection_sha256,
            _authority_projection_sha256(
                candidates=self.candidates,
                coverage_grade=self.coverage_grade,
                coverage_sha256=self.coverage_sha256,
                evidence_sha256=self.evidence_sha256,
            ),
        ):
            raise ArchiveCandidateSelectionError("candidate authority projection digest changed")
        for item in self.candidates:
            item._selected_evidence(
                work_item_id=work_item_id,
                coverage_sha256=coverage_sha256,
                coverage_grade=self.coverage_grade,
                origin_boundary_user_message_id=origin,
            )

    def __repr__(self) -> str:
        return f"ArchiveCandidateSet(private_source=True, candidate_count={len(self.candidates)})"

    def to_payload(self) -> dict[str, object]:
        return {
            "candidates": [item.to_payload() for item in self.candidates],
            "coverage_grade": self.coverage_grade.value,
            "coverage_sha256": self.coverage_sha256,
            "evidence_sha256": self.evidence_sha256,
            "authority_projection_sha256": self.authority_projection_sha256,
            "id": self.id,
            "origin_boundary_user_message_id": self.origin_boundary_user_message_id,
            "schema": ARCHIVE_CANDIDATE_SET_SCHEMA,
            "work_item_id": self.work_item_id,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_payload())

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    def selected_evidence(self, ordinal: int, *, work_item_id: str | None = None) -> SelectedArchiveEvidence:
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not 1 <= ordinal <= len(self.candidates)
        ):
            raise ArchiveCandidateSelectionError("selected ordinal is outside the candidate set")
        return self.candidates[ordinal - 1]._selected_evidence(
            work_item_id=(
                self.work_item_id
                if work_item_id is None
                else _identifier(work_item_id, _WORK_ITEM_ID_RE, label="selected_work_item_id")
            ),
            coverage_sha256=self.coverage_sha256,
            coverage_grade=self.coverage_grade,
            origin_boundary_user_message_id=self.origin_boundary_user_message_id,
        )

    @classmethod
    def from_accepted_projection(
        cls,
        *,
        id: str,
        work_item_id: str,
        origin_boundary_user_message_id: str,
        projection: ArchiveSearchAcceptedCandidateProjection,
    ) -> ArchiveCandidateSet:
        """Project only an exact phase-2 sealed candidate order into durable identities."""

        if type(projection) is not ArchiveSearchAcceptedCandidateProjection:
            raise ArchiveCandidateSelectionError(
                "candidate set requires an exact accepted authority projection"
            )
        try:
            entries = projection.candidates
            candidates = tuple(
                ArchiveCandidateItem(
                    ordinal=entry.ordinal,
                    public_citation_label=entry.public_citation_label,
                    corpus=SelectedArchiveCorpus(entry.corpus.value),
                    source_ref=entry.source_ref,
                    passage_refs=entry.passage_refs,
                    source_snapshot_sha256=entry.resolved_snapshot_sha256,
                )
                for entry in entries
            )
            return cls(
                id=id,
                work_item_id=work_item_id,
                evidence_sha256=projection.evidence_sha256,
                coverage_sha256=projection.coverage_sha256,
                coverage_grade=SelectedArchiveCoverageGrade(projection.coverage_grade.value),
                authority_projection_sha256=projection.canonical_sha256,
                origin_boundary_user_message_id=origin_boundary_user_message_id,
                candidates=candidates,
            )
        except ArchiveCandidateSelectionError:
            raise
        except (ArchiveSearchAuthorityError, TypeError, ValueError):
            raise ArchiveCandidateSelectionError(
                "accepted authority projection is not a replayable candidate set"
            ) from None

    @classmethod
    def from_storage_rows(
        cls,
        set_row: Mapping[str, object],
        item_rows: Sequence[Mapping[str, object]],
    ) -> ArchiveCandidateSet:
        _exact_keys(
            set_row,
            (
                "id",
                "work_item_id",
                "evidence_sha256",
                "coverage_sha256",
                "coverage_grade",
                "authority_projection_sha256",
                "origin_boundary_user_message_id",
                "candidate_set_sha256",
            ),
            label="candidate set storage row",
        )
        candidates: list[ArchiveCandidateItem] = []
        for raw in item_rows:
            _exact_keys(
                raw,
                (
                    "candidate_set_id",
                    "work_item_id",
                    "ordinal",
                    "public_citation_label",
                    "corpus",
                    "source_ref_json",
                    "passage_refs_json",
                    "source_snapshot_sha256",
                ),
                label="candidate item storage row",
            )
            if raw["candidate_set_id"] != set_row["id"] or raw["work_item_id"] != set_row["work_item_id"]:
                raise ArchiveCandidateSelectionError("candidate item belongs to another set")
            raw_corpus = raw["corpus"]
            if not isinstance(raw_corpus, str):
                raise ArchiveCandidateSelectionError("candidate item corpus is invalid")
            try:
                source = SourceRef.parse_private(raw["source_ref_json"])  # type: ignore[arg-type]
                passages = parse_canonical_passage_refs(raw["passage_refs_json"])
                corpus = SelectedArchiveCorpus(raw_corpus)
            except (TypeError, ValueError) as exc:
                raise ArchiveCandidateSelectionError("candidate item storage row is invalid") from exc
            candidates.append(
                ArchiveCandidateItem(
                    ordinal=raw["ordinal"],  # type: ignore[arg-type]
                    public_citation_label=raw["public_citation_label"],  # type: ignore[arg-type]
                    corpus=corpus,
                    source_ref=source,
                    passage_refs=passages,
                    source_snapshot_sha256=raw["source_snapshot_sha256"],  # type: ignore[arg-type]
                )
            )
        raw_grade = set_row["coverage_grade"]
        if not isinstance(raw_grade, str):
            raise ArchiveCandidateSelectionError("candidate set coverage grade is invalid")
        try:
            grade = SelectedArchiveCoverageGrade(raw_grade)
        except (TypeError, ValueError) as exc:
            raise ArchiveCandidateSelectionError("candidate set coverage grade is invalid") from exc
        value = cls(
            id=set_row["id"],  # type: ignore[arg-type]
            work_item_id=set_row["work_item_id"],  # type: ignore[arg-type]
            evidence_sha256=set_row["evidence_sha256"],  # type: ignore[arg-type]
            coverage_sha256=set_row["coverage_sha256"],  # type: ignore[arg-type]
            coverage_grade=grade,
            authority_projection_sha256=set_row["authority_projection_sha256"],  # type: ignore[arg-type]
            origin_boundary_user_message_id=set_row["origin_boundary_user_message_id"],  # type: ignore[arg-type]
            candidates=tuple(candidates),
        )
        if set_row["candidate_set_sha256"] != value.canonical_sha256():
            raise ArchiveCandidateSelectionError("candidate set digest changed")
        return value

    def set_storage_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "work_item_id": self.work_item_id,
            "evidence_sha256": self.evidence_sha256,
            "coverage_sha256": self.coverage_sha256,
            "coverage_grade": self.coverage_grade.value,
            "authority_projection_sha256": self.authority_projection_sha256,
            "origin_boundary_user_message_id": self.origin_boundary_user_message_id,
            "candidate_set_sha256": self.canonical_sha256(),
        }

    def item_storage_payloads(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "candidate_set_id": self.id,
                "work_item_id": self.work_item_id,
                "ordinal": item.ordinal,
                "public_citation_label": item.public_citation_label,
                "corpus": item.corpus.value,
                "source_ref_json": item.source_ref.to_private_json(),
                "passage_refs_json": canonical_passage_refs_json(item.passage_refs),
                "source_snapshot_sha256": item.source_snapshot_sha256,
            }
            for item in self.candidates
        )


@dataclass(frozen=True, slots=True)
class ArchiveCandidateOrdinalQuestion:
    """One body-free ordinal question; user wording is intentionally absent."""

    id: str
    work_item_id: str
    candidate_set_id: str
    kind: ArchiveCandidateQuestionKind
    minimum_ordinal: int
    maximum_ordinal: int
    state: ArchiveCandidateQuestionState
    selected_ordinal: int | None
    created_at: str
    prompt_boundary_user_message_id: str
    prompt_assistant_message_id: str
    prompt_updated_at: str
    prompt_revision: int
    answered_at: str | None
    replay_boundary_user_message_id: str | None
    replay_assistant_message_id: str | None
    accepted_replay_plan_sha256: str | None
    accepted_replay_outcome_sha256: str | None
    failed_ordinal: int | None
    failure_boundary_user_message_id: str | None
    failure_assistant_message_id: str | None
    failure_recorded_at: str | None
    accepted_failure_plan_sha256: str | None
    accepted_failure_outcome_sha256: str | None

    def __post_init__(self) -> None:
        _identifier(self.id, _QUESTION_ID_RE, label="question_id")
        _identifier(self.work_item_id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.candidate_set_id, _CANDIDATE_SET_ID_RE, label="candidate_set_id")
        if self.kind is not ArchiveCandidateQuestionKind.SELECT_ORDINAL:
            raise ArchiveCandidateSelectionError("candidate question kind is invalid")
        if (
            self.minimum_ordinal != 1
            or not isinstance(self.maximum_ordinal, int)
            or isinstance(self.maximum_ordinal, bool)
            or not 2 <= self.maximum_ordinal <= ARCHIVE_CANDIDATE_MAX_COUNT
        ):
            raise ArchiveCandidateSelectionError("candidate question range is invalid")
        created = _canonical_instant(self.created_at, label="question_created_at")
        prompt_boundary = _identifier(
            self.prompt_boundary_user_message_id,
            _MESSAGE_ID_RE,
            label="prompt_boundary_user_message_id",
        )
        prompt_assistant = _identifier(
            self.prompt_assistant_message_id,
            _MESSAGE_ID_RE,
            label="prompt_assistant_message_id",
        )
        prompt_updated = _canonical_instant(
            self.prompt_updated_at,
            label="prompt_updated_at",
        )
        if prompt_boundary == prompt_assistant or prompt_updated < created:
            raise ArchiveCandidateSelectionError("candidate prompt boundary is invalid")
        if (
            not isinstance(self.prompt_revision, int)
            or isinstance(self.prompt_revision, bool)
            or not 1 <= self.prompt_revision <= WORK_ITEM_MAX_REVISION
        ):
            raise ArchiveCandidateSelectionError("candidate prompt revision is invalid")
        failure_values = (
            self.failed_ordinal,
            self.failure_boundary_user_message_id,
            self.failure_assistant_message_id,
            self.failure_recorded_at,
            self.accepted_failure_plan_sha256,
            self.accepted_failure_outcome_sha256,
        )
        failure_present = tuple(value is not None for value in failure_values)
        if any(failure_present) and not all(failure_present):
            raise ArchiveCandidateSelectionError("candidate replay failure receipt is incomplete")
        if all(failure_present):
            if (
                not isinstance(self.failed_ordinal, int)
                or isinstance(self.failed_ordinal, bool)
                or not 1 <= self.failed_ordinal <= self.maximum_ordinal
            ):
                raise ArchiveCandidateSelectionError("failed candidate ordinal is invalid")
            failure_boundary = _identifier(
                self.failure_boundary_user_message_id,
                _MESSAGE_ID_RE,
                label="failure_boundary_user_message_id",
            )
            failure_assistant = _identifier(
                self.failure_assistant_message_id,
                _MESSAGE_ID_RE,
                label="failure_assistant_message_id",
            )
            if failure_boundary == failure_assistant:
                raise ArchiveCandidateSelectionError("failure message anchors must differ")
            failure_at = _canonical_instant(
                self.failure_recorded_at,
                label="failure_recorded_at",
            )
            if failure_at < prompt_updated:
                raise ArchiveCandidateSelectionError("candidate replay failure precedes its prompt")
            _digest(
                self.accepted_failure_plan_sha256,
                label="accepted_failure_plan_sha256",
            )
            _digest(
                self.accepted_failure_outcome_sha256,
                label="accepted_failure_outcome_sha256",
            )
        if self.state is ArchiveCandidateQuestionState.WAITING:
            if any(
                value is not None
                for value in (
                    self.selected_ordinal,
                    self.answered_at,
                    self.replay_boundary_user_message_id,
                    self.replay_assistant_message_id,
                    self.accepted_replay_plan_sha256,
                    self.accepted_replay_outcome_sha256,
                )
            ):
                raise ArchiveCandidateSelectionError("waiting question cannot have an answer")
        elif self.state is ArchiveCandidateQuestionState.ANSWERED:
            if (
                not isinstance(self.selected_ordinal, int)
                or isinstance(self.selected_ordinal, bool)
                or not 1 <= self.selected_ordinal <= self.maximum_ordinal
                or self.answered_at is None
                or self.replay_boundary_user_message_id is None
                or self.replay_assistant_message_id is None
                or self.accepted_replay_plan_sha256 is None
                or self.accepted_replay_outcome_sha256 is None
                or any(failure_present)
            ):
                raise ArchiveCandidateSelectionError("answered question has an invalid ordinal")
            answered = _canonical_instant(self.answered_at, label="question_answered_at")
            if answered < created:
                raise ArchiveCandidateSelectionError("question answer precedes creation")
            boundary = _identifier(
                self.replay_boundary_user_message_id,
                _MESSAGE_ID_RE,
                label="replay_boundary_user_message_id",
            )
            assistant = _identifier(
                self.replay_assistant_message_id,
                _MESSAGE_ID_RE,
                label="replay_assistant_message_id",
            )
            if boundary == assistant:
                raise ArchiveCandidateSelectionError("replay message anchors must differ")
            _digest(self.accepted_replay_plan_sha256, label="accepted_replay_plan_sha256")
            _digest(self.accepted_replay_outcome_sha256, label="accepted_replay_outcome_sha256")
        else:
            raise ArchiveCandidateSelectionError("candidate question state is invalid")

    def answer(
        self,
        ordinal: int,
        *,
        answered_at: str,
        replay_boundary_user_message_id: str,
        replay_assistant_message_id: str,
        accepted_replay_plan_sha256: str,
        accepted_replay_outcome_sha256: str,
    ) -> ArchiveCandidateOrdinalQuestion:
        if self.state is not ArchiveCandidateQuestionState.WAITING:
            raise ArchiveCandidateSelectionError("candidate question is no longer waiting")
        if self.failed_ordinal is not None:
            raise ArchiveCandidateSelectionError("candidate replay failure is already recorded")
        return replace(
            self,
            state=ArchiveCandidateQuestionState.ANSWERED,
            selected_ordinal=ordinal,
            answered_at=answered_at,
            replay_boundary_user_message_id=replay_boundary_user_message_id,
            replay_assistant_message_id=replay_assistant_message_id,
            accepted_replay_plan_sha256=accepted_replay_plan_sha256,
            accepted_replay_outcome_sha256=accepted_replay_outcome_sha256,
        )

    def reask(
        self,
        *,
        prompt_boundary_user_message_id: str,
        prompt_assistant_message_id: str,
        prompt_updated_at: str,
        prompt_revision: int,
    ) -> ArchiveCandidateOrdinalQuestion:
        if self.state is not ArchiveCandidateQuestionState.WAITING:
            raise ArchiveCandidateSelectionError("candidate question is no longer waiting")
        if self.failed_ordinal is not None:
            raise ArchiveCandidateSelectionError("candidate replay failure is already recorded")
        return replace(
            self,
            prompt_boundary_user_message_id=prompt_boundary_user_message_id,
            prompt_assistant_message_id=prompt_assistant_message_id,
            prompt_updated_at=prompt_updated_at,
            prompt_revision=prompt_revision,
        )

    def record_replay_failure(
        self,
        ordinal: int,
        *,
        boundary_user_message_id: str,
        assistant_message_id: str,
        recorded_at: str,
        accepted_plan_sha256: str,
        accepted_outcome_sha256: str,
    ) -> ArchiveCandidateOrdinalQuestion:
        """Bind one source-free accepted replay failure without answering the question."""

        if self.state is not ArchiveCandidateQuestionState.WAITING:
            raise ArchiveCandidateSelectionError("candidate question is no longer waiting")
        if self.failed_ordinal is not None:
            raise ArchiveCandidateSelectionError("candidate replay failure is already recorded")
        return replace(
            self,
            failed_ordinal=ordinal,
            failure_boundary_user_message_id=boundary_user_message_id,
            failure_assistant_message_id=assistant_message_id,
            failure_recorded_at=recorded_at,
            accepted_failure_plan_sha256=accepted_plan_sha256,
            accepted_failure_outcome_sha256=accepted_outcome_sha256,
        )

    @property
    def has_replay_failure_receipt(self) -> bool:
        return self.failed_ordinal is not None

    def to_payload(self) -> dict[str, object]:
        return {
            "answered_at": self.answered_at,
            "accepted_replay_outcome_sha256": self.accepted_replay_outcome_sha256,
            "accepted_replay_plan_sha256": self.accepted_replay_plan_sha256,
            "accepted_failure_outcome_sha256": self.accepted_failure_outcome_sha256,
            "accepted_failure_plan_sha256": self.accepted_failure_plan_sha256,
            "candidate_set_id": self.candidate_set_id,
            "created_at": self.created_at,
            "failed_ordinal": self.failed_ordinal,
            "failure_assistant_message_id": self.failure_assistant_message_id,
            "failure_boundary_user_message_id": self.failure_boundary_user_message_id,
            "failure_recorded_at": self.failure_recorded_at,
            "id": self.id,
            "kind": self.kind.value,
            "maximum_ordinal": self.maximum_ordinal,
            "minimum_ordinal": self.minimum_ordinal,
            "prompt_assistant_message_id": self.prompt_assistant_message_id,
            "prompt_boundary_user_message_id": self.prompt_boundary_user_message_id,
            "prompt_revision": self.prompt_revision,
            "prompt_updated_at": self.prompt_updated_at,
            "replay_assistant_message_id": self.replay_assistant_message_id,
            "replay_boundary_user_message_id": self.replay_boundary_user_message_id,
            "schema": ARCHIVE_CANDIDATE_QUESTION_SCHEMA,
            "selected_ordinal": self.selected_ordinal,
            "state": self.state.value,
            "work_item_id": self.work_item_id,
        }

    @classmethod
    def from_storage_row(cls, row: Mapping[str, object]) -> ArchiveCandidateOrdinalQuestion:
        _exact_keys(
            row,
            (
                "id",
                "work_item_id",
                "candidate_set_id",
                "kind",
                "minimum_ordinal",
                "maximum_ordinal",
                "state",
                "selected_ordinal",
                "created_at",
                "prompt_boundary_user_message_id",
                "prompt_assistant_message_id",
                "prompt_updated_at",
                "prompt_revision",
                "answered_at",
                "replay_boundary_user_message_id",
                "replay_assistant_message_id",
                "accepted_replay_plan_sha256",
                "accepted_replay_outcome_sha256",
                "failed_ordinal",
                "failure_boundary_user_message_id",
                "failure_assistant_message_id",
                "failure_recorded_at",
                "accepted_failure_plan_sha256",
                "accepted_failure_outcome_sha256",
            ),
            label="candidate question storage row",
        )
        raw_kind = row["kind"]
        raw_state = row["state"]
        if not isinstance(raw_kind, str) or not isinstance(raw_state, str):
            raise ArchiveCandidateSelectionError("candidate question enum fields are invalid")
        try:
            return cls(
                id=row["id"],  # type: ignore[arg-type]
                work_item_id=row["work_item_id"],  # type: ignore[arg-type]
                candidate_set_id=row["candidate_set_id"],  # type: ignore[arg-type]
                kind=ArchiveCandidateQuestionKind(raw_kind),
                minimum_ordinal=row["minimum_ordinal"],  # type: ignore[arg-type]
                maximum_ordinal=row["maximum_ordinal"],  # type: ignore[arg-type]
                state=ArchiveCandidateQuestionState(raw_state),
                selected_ordinal=row["selected_ordinal"],  # type: ignore[arg-type]
                created_at=row["created_at"],  # type: ignore[arg-type]
                prompt_boundary_user_message_id=row["prompt_boundary_user_message_id"],  # type: ignore[arg-type]
                prompt_assistant_message_id=row["prompt_assistant_message_id"],  # type: ignore[arg-type]
                prompt_updated_at=row["prompt_updated_at"],  # type: ignore[arg-type]
                prompt_revision=row["prompt_revision"],  # type: ignore[arg-type]
                answered_at=row["answered_at"],  # type: ignore[arg-type]
                replay_boundary_user_message_id=row["replay_boundary_user_message_id"],  # type: ignore[arg-type]
                replay_assistant_message_id=row["replay_assistant_message_id"],  # type: ignore[arg-type]
                accepted_replay_plan_sha256=row["accepted_replay_plan_sha256"],  # type: ignore[arg-type]
                accepted_replay_outcome_sha256=row["accepted_replay_outcome_sha256"],  # type: ignore[arg-type]
                failed_ordinal=row["failed_ordinal"],  # type: ignore[arg-type]
                failure_boundary_user_message_id=row["failure_boundary_user_message_id"],  # type: ignore[arg-type]
                failure_assistant_message_id=row["failure_assistant_message_id"],  # type: ignore[arg-type]
                failure_recorded_at=row["failure_recorded_at"],  # type: ignore[arg-type]
                accepted_failure_plan_sha256=row["accepted_failure_plan_sha256"],  # type: ignore[arg-type]
                accepted_failure_outcome_sha256=row["accepted_failure_outcome_sha256"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise ArchiveCandidateSelectionError("candidate question storage row is invalid") from exc

    def storage_payload(self) -> dict[str, object]:
        payload = self.to_payload()
        payload.pop("schema")
        return payload


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveCandidateSelectionWorkItem:
    """Joined projection of one candidate Work Item and all of its sidecars."""

    id: str
    user_id: str
    conversation_id: str
    state: WorkState
    active_frame: ArchiveCandidateSelectionActiveFrame
    anchor_user_message_id: str
    anchor_assistant_message_id: str
    accepted_plan_sha256: str
    accepted_outcome_sha256: str
    revision: int
    transition: WorkTransition
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None
    candidate_set: ArchiveCandidateSet
    question: ArchiveCandidateOrdinalQuestion

    def __post_init__(self) -> None:
        identifier = _identifier(self.id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.user_id, _USER_ID_RE, label="user_id")
        _identifier(self.conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
        _identifier(self.anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
        _identifier(self.anchor_assistant_message_id, _MESSAGE_ID_RE, label="anchor_assistant_message_id")
        if self.anchor_user_message_id == self.anchor_assistant_message_id:
            raise ArchiveCandidateSelectionError("candidate work anchors must differ")
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.accepted_outcome_sha256, label="accepted_outcome_sha256")
        if type(self.active_frame) is not ArchiveCandidateSelectionActiveFrame:
            raise ArchiveCandidateSelectionError("candidate active frame is invalid")
        if (
            type(self.candidate_set) is not ArchiveCandidateSet
            or self.candidate_set.work_item_id != identifier
            or self.candidate_set.origin_boundary_user_message_id != self.anchor_user_message_id
        ):
            raise ArchiveCandidateSelectionError("candidate set does not belong to its Work Item")
        if (
            type(self.question) is not ArchiveCandidateOrdinalQuestion
            or self.question.work_item_id != identifier
            or self.question.candidate_set_id != self.candidate_set.id
            or self.question.maximum_ordinal != len(self.candidate_set.candidates)
            or self.question.created_at != self.created_at
        ):
            raise ArchiveCandidateSelectionError("candidate question does not match its set")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 1 <= self.revision <= WORK_ITEM_MAX_REVISION
        ):
            raise ArchiveCandidateSelectionError("candidate revision is outside the closed limit")
        if self.question.prompt_revision > self.revision:
            raise ArchiveCandidateSelectionError("candidate prompt revision exceeds Work Item revision")
        created = _canonical_instant(self.created_at, label="created_at")
        updated = _canonical_instant(self.updated_at, label="updated_at")
        expires = _canonical_instant(self.expires_at, label="expires_at")
        if self.question.prompt_updated_at > updated:
            raise ArchiveCandidateSelectionError("candidate prompt timestamp exceeds Work Item timestamp")
        if self.question.failure_recorded_at is not None and self.question.failure_recorded_at > updated:
            raise ArchiveCandidateSelectionError(
                "candidate replay failure timestamp exceeds Work Item timestamp"
            )
        if updated < created or datetime.fromisoformat(expires) > datetime.fromisoformat(updated) + timedelta(
            hours=WORK_ITEM_TTL_HOURS
        ):
            raise ArchiveCandidateSelectionError("candidate work timestamps are invalid")
        if self.state in {WorkState.WAITING_FOR_INPUT, WorkState.SUSPENDED}:
            if self.closed_at is not None or expires <= updated:
                raise ArchiveCandidateSelectionError("open candidate work lifecycle is invalid")
        else:
            if self.state not in {WorkState.COMPLETED, WorkState.CANCELLED, WorkState.EXPIRED}:
                raise ArchiveCandidateSelectionError("candidate work state is invalid")
            if self.closed_at is None:
                raise ArchiveCandidateSelectionError("closed candidate work requires closed_at")
            closed = _canonical_instant(self.closed_at, label="closed_at")
            if (
                closed != updated
                or (self.state is WorkState.COMPLETED and expires <= updated)
                or (self.state is WorkState.EXPIRED and expires > updated)
            ):
                raise ArchiveCandidateSelectionError("candidate closed lifecycle is invalid")
        expected = {
            WorkState.WAITING_FOR_INPUT: {
                WorkTransition.QUESTION_ASKED,
                WorkTransition.QUESTION_REASKED,
            },
            WorkState.COMPLETED: {WorkTransition.CANDIDATE_REPLAYED},
            WorkState.SUSPENDED: {WorkTransition.SUSPENDED},
            WorkState.CANCELLED: {WorkTransition.CANCELLED},
            WorkState.EXPIRED: {WorkTransition.EXPIRED},
        }
        if self.transition not in expected.get(self.state, set()):
            raise ArchiveCandidateSelectionError("candidate transition does not match state")
        if self.transition is WorkTransition.QUESTION_ASKED:
            if (
                self.revision != 1
                or self.question.state is not ArchiveCandidateQuestionState.WAITING
                or self.question.prompt_revision != 1
                or self.question.prompt_updated_at != self.created_at
                or self.question.prompt_boundary_user_message_id != self.anchor_user_message_id
                or self.question.prompt_assistant_message_id != self.anchor_assistant_message_id
                or self.question.has_replay_failure_receipt
            ):
                raise ArchiveCandidateSelectionError("new candidate question is inconsistent")
        elif self.transition is WorkTransition.QUESTION_REASKED:
            if (
                self.revision < 2
                or self.question.state is not ArchiveCandidateQuestionState.WAITING
                or self.question.prompt_revision != self.revision
                or self.question.prompt_updated_at != self.updated_at
                or self.question.has_replay_failure_receipt
            ):
                raise ArchiveCandidateSelectionError("re-asked candidate question is inconsistent")
        elif self.revision < 2 or self.question.prompt_revision >= self.revision:
            raise ArchiveCandidateSelectionError("post-question candidate work requires revision 2")
        if self.transition is WorkTransition.CANDIDATE_REPLAYED:
            if (
                self.question.state is not ArchiveCandidateQuestionState.ANSWERED
                or self.question.answered_at != self.updated_at
                or self.question.has_replay_failure_receipt
            ):
                raise ArchiveCandidateSelectionError("selected candidate question is inconsistent")
        elif self.transition is WorkTransition.SUSPENDED and self.question.has_replay_failure_receipt:
            if (
                self.question.state is not ArchiveCandidateQuestionState.WAITING
                or self.question.failure_recorded_at != self.updated_at
            ):
                raise ArchiveCandidateSelectionError("failed candidate replay receipt is inconsistent")
        elif self.question.state is not ArchiveCandidateQuestionState.WAITING:
            raise ArchiveCandidateSelectionError("only a completed candidate Work Item may hold an answer")

    @property
    def selected_evidence(self) -> SelectedArchiveEvidence | None:
        ordinal = self.question.selected_ordinal
        return None if ordinal is None else self.candidate_set.selected_evidence(ordinal)

    @property
    def failed_evidence(self) -> SelectedArchiveEvidence | None:
        ordinal = self.question.failed_ordinal
        return None if ordinal is None else self.candidate_set.selected_evidence(ordinal)

    @classmethod
    def from_storage_rows(
        cls,
        work: Mapping[str, object],
        candidate_set: ArchiveCandidateSet,
        question: ArchiveCandidateOrdinalQuestion,
    ) -> ArchiveCandidateSelectionWorkItem:
        _exact_keys(
            work,
            (
                "id",
                "user_id",
                "conversation_id",
                "kind",
                "goal",
                "state",
                "playbook",
                "completion_contract",
                "active_frame_json",
                "anchor_user_message_id",
                "anchor_assistant_message_id",
                "accepted_plan_sha256",
                "accepted_outcome_sha256",
                "revision",
                "transition",
                "created_at",
                "updated_at",
                "expires_at",
                "closed_at",
            ),
            label="candidate Work Item storage row",
        )
        if (
            work["kind"] != WorkKind.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value
            or work["goal"] != WorkGoal.EXACT_ARCHIVE_CANDIDATE_SELECTION_AND_EVIDENCE_REPLAY.value
            or work["playbook"] != WorkPlaybook.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value
            or work["completion_contract"]
            != WorkCompletionContract.ACCEPTED_EXACT_ARCHIVE_CANDIDATE_AND_EVIDENCE_REPLAY.value
        ):
            raise ArchiveCandidateSelectionError("candidate workflow identity is invalid")
        raw_state = work["state"]
        raw_transition = work["transition"]
        if not isinstance(raw_state, str) or not isinstance(raw_transition, str):
            raise ArchiveCandidateSelectionError("candidate Work Item enum fields are invalid")
        try:
            return cls(
                id=work["id"],  # type: ignore[arg-type]
                user_id=work["user_id"],  # type: ignore[arg-type]
                conversation_id=work["conversation_id"],  # type: ignore[arg-type]
                state=WorkState(raw_state),
                active_frame=ArchiveCandidateSelectionActiveFrame.parse(work["active_frame_json"]),
                anchor_user_message_id=work["anchor_user_message_id"],  # type: ignore[arg-type]
                anchor_assistant_message_id=work["anchor_assistant_message_id"],  # type: ignore[arg-type]
                accepted_plan_sha256=work["accepted_plan_sha256"],  # type: ignore[arg-type]
                accepted_outcome_sha256=work["accepted_outcome_sha256"],  # type: ignore[arg-type]
                revision=work["revision"],  # type: ignore[arg-type]
                transition=WorkTransition(raw_transition),
                created_at=work["created_at"],  # type: ignore[arg-type]
                updated_at=work["updated_at"],  # type: ignore[arg-type]
                expires_at=work["expires_at"],  # type: ignore[arg-type]
                closed_at=work["closed_at"],  # type: ignore[arg-type]
                candidate_set=candidate_set,
                question=question,
            )
        except (TypeError, ValueError) as exc:
            raise ArchiveCandidateSelectionError("candidate Work Item storage row is invalid") from exc

    def to_payload(self) -> dict[str, object]:
        return {
            "accepted_outcome_sha256": self.accepted_outcome_sha256,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "active_frame": self.active_frame.to_payload(),
            "anchor_assistant_message_id": self.anchor_assistant_message_id,
            "anchor_user_message_id": self.anchor_user_message_id,
            "candidate_set": self.candidate_set.to_payload(),
            "closed_at": self.closed_at,
            "completion_contract": (
                WorkCompletionContract.ACCEPTED_EXACT_ARCHIVE_CANDIDATE_AND_EVIDENCE_REPLAY.value
            ),
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "goal": WorkGoal.EXACT_ARCHIVE_CANDIDATE_SELECTION_AND_EVIDENCE_REPLAY.value,
            "id": self.id,
            "kind": WorkKind.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value,
            "playbook": WorkPlaybook.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE.value,
            "question": self.question.to_payload(),
            "revision": self.revision,
            "schema": ARCHIVE_CANDIDATE_WORK_ITEM_SCHEMA,
            "state": self.state.value,
            "transition": self.transition.value,
            "updated_at": self.updated_at,
            "user_id": self.user_id,
        }


__all__ = [
    "ARCHIVE_CANDIDATE_ITEM_SCHEMA",
    "ARCHIVE_CANDIDATE_MAX_COUNT",
    "ARCHIVE_CANDIDATE_QUESTION_SCHEMA",
    "ARCHIVE_CANDIDATE_REASK_VERDICT_KIND",
    "ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON",
    "ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_SCHEMA",
    "ARCHIVE_CANDIDATE_SET_SCHEMA",
    "ARCHIVE_CANDIDATE_WORK_ITEM_SCHEMA",
    "ArchiveCandidateItem",
    "ArchiveCandidateOrdinalQuestion",
    "ArchiveCandidateQuestionKind",
    "ArchiveCandidateQuestionState",
    "ArchiveCandidateSelectionActiveFrame",
    "ArchiveCandidateSelectionError",
    "ArchiveCandidateSelectionWorkItem",
    "ArchiveCandidateSet",
    "archive_candidate_selection_offer_suffix",
    "archive_candidate_reask_prompt",
    "parse_archive_candidate_ordinal",
]
