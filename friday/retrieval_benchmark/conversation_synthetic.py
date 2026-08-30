"""Private deterministic conversation-recall corpus and phased seed plan.

The module deliberately does not introduce a benchmark DTO.  Shipped cases
remain :class:`RecallCaseV1`; the frozen package-local plan only keeps the raw
fixture rows and exact diagnostics needed by the real-path harness.

Seeding is intentionally phased.  A harness seeds the pre-backfill and foreign
rows, converges the foreign principal before the benchmark principal, converges
the benchmark principal, and only then seeds late rows, the accepted boundary,
and the post-boundary row.  Keeping those steps separate preserves the lexical
fallback, source-reset, restart, and accepted-prefix contours under measurement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from friday.retrieval.archive_search_contract import (
    ArchiveContextWindow,
    ArchiveLifecycleConstraint,
    ArchiveMatchChannel,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
    ConversationScope,
)
from friday.retrieval.archive_search_message_adapter import MESSAGE_PASSAGE_INDEX_VERSION
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    PassageLocatorKind,
    PassageRef,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.retrieval_benchmark.contracts import (
    RecallAlternativeV1,
    RecallCaseV1,
    RecallEvidenceSourceV1,
    RecallTaxonomyV1,
    opaque_passage_window_identity,
    opaque_source_identity,
)
from friday.retrieval_benchmark.synthetic import (
    BOUNDARY_CONVERSATION_ID,
    BOUNDARY_MESSAGE_ID,
    SYNTHETIC_PRINCIPAL,
    SYNTHETIC_TENANT,
)

_FOREIGN_PRINCIPAL: Final = "recall-benchmark-foreign-principal"
_BOUNDARY_AT: Final = "2026-06-30T12:00:00+00:00"
_POST_BOUNDARY_AT: Final = "2026-06-30T12:00:01+00:00"
_RESET_INITIAL_AT: Final = "2026-05-20T00:01:00+00:00"
_RESET_FINAL_AT: Final = "2026-05-20T00:02:00+00:00"
# The released conversation lexical pool is 100 materialized candidates times
# eight bounded hits.  One sentinel beyond it proves owner filtering cannot
# silently promote the partial global derivative into absence authority.
_FOREIGN_SATURATION_COUNT: Final = 801


class _MatrixCell(StrEnum):
    ARCHIVE = "archive"
    FALLBACK = "fallback"
    ADJACENT = "adjacent"
    DIVERSITY = "diversity"
    REPLAY = "replay"
    PRIVACY = "privacy"


class _SeedPhase(StrEnum):
    PRE_BACKFILL = "pre_backfill"
    FOREIGN_SATURATION = "foreign_saturation"
    LATE = "late"
    ACCEPTED_BOUNDARY = "accepted_boundary"
    POST_BOUNDARY = "post_boundary"


class _ProjectionContour(StrEnum):
    CURRENT = "current"
    BACKFILL_PENDING = "backfill_pending"
    SOURCE_CHANGED = "source_changed"
    FOREIGN_SATURATED = "foreign_saturated"
    ACCEPTED_BOUNDARY = "accepted_boundary"
    POST_BOUNDARY = "post_boundary"


class _ConversationSyntheticStorage(Protocol):
    def ensure_user(self, user_id: str) -> object: ...

    def transaction(self): ...  # type: ignore[no-untyped-def]


@dataclass(frozen=True, slots=True, repr=False)
class _ConversationRow:
    conversation_id: str
    principal_id: str
    title: str
    archived: bool
    created_at: str
    phase: _SeedPhase


@dataclass(frozen=True, slots=True, repr=False)
class _MessageRow:
    message_id: str
    conversation_id: str
    principal_id: str
    role: MessageRole
    content: str
    created_at: str
    phase: _SeedPhase


@dataclass(frozen=True, slots=True, repr=False)
class _MessageTimestampReset:
    message_id: str
    conversation_id: str
    initial_created_at: str
    final_created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class _CaseIntent:
    ordinal: int
    matrix_cell: _MatrixCell
    query: str
    target_conversation_id: str
    target_message_id: str
    expected_window_message_ids: tuple[str, ...]
    expected_channels: tuple[ArchiveMatchChannel, ...]
    projection_contour: _ProjectionContour
    context: ArchiveContextWindow = ArchiveContextWindow()
    conversation_scope: ConversationScope = ConversationScope.ALL
    roles: tuple[MessageRole, ...] = ()
    lifecycle_states: tuple[LifecycleState, ...] = ()
    temporal_start: str | None = None
    temporal_end: str | None = None
    restart_replay: bool = False
    forbidden_conversation_ids: tuple[str, ...] = ()
    forbidden_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, repr=False)
class _ConversationCaseDiagnostic:
    """Process-private truth the harness must not recover from raw bodies."""

    case: RecallCaseV1
    matrix_cell: _MatrixCell
    source_ref: SourceRef
    passage_ref: PassageRef
    anchor_message_id: str
    expected_window_message_ids: tuple[str, ...]
    expected_context: ArchiveContextWindow
    expected_channels: tuple[ArchiveMatchChannel, ...]
    projection_contour: _ProjectionContour
    restart_replay: bool
    forbidden_source_refs: tuple[SourceRef, ...]
    forbidden_message_ids: tuple[str, ...]

    @property
    def case_id(self) -> str:
        return self.case.case_id


@dataclass(frozen=True, slots=True, repr=False)
class _ConversationSyntheticPlan:
    cases: tuple[RecallCaseV1, ...]
    diagnostics: tuple[_ConversationCaseDiagnostic, ...]
    conversations: tuple[_ConversationRow, ...]
    messages: tuple[_MessageRow, ...]
    timestamp_resets: tuple[_MessageTimestampReset, ...]
    foreign_principal_id: str
    accepted_conversation_id: str
    accepted_boundary_message_id: str

    def diagnostic(self, case_id: str) -> _ConversationCaseDiagnostic:
        """Return one exact bounded diagnostic without exposing a body map."""

        match = next((item for item in self.diagnostics if item.case_id == case_id), None)
        if match is None:
            raise KeyError(case_id)
        return match


def _conversation_id(ordinal: int) -> str:
    return f"conv_{0xC000000000000000 + ordinal:016x}"


def _message_id(ordinal: int) -> str:
    return f"msg_{0xD000000000000000 + ordinal:016x}"


def _case_privacy_key(ordinal: int) -> str:
    return hashlib.sha256(
        f"friday/retrieval-recall-conversation-case-key/v1/{ordinal:04d}".encode("ascii")
    ).hexdigest()


def _source_ref(
    conversation_id: str,
    principal_id: str = SYNTHETIC_PRINCIPAL,
) -> SourceRef:
    return SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        principal_id,
        CanonicalObjectKind.CONVERSATION,
        conversation_id,
    )


def _row_identity(row: _MessageRow) -> str:
    material = json.dumps(
        {
            "content": row.content,
            "conversation_id": row.conversation_id,
            "created_at": row.created_at,
            "id": row.message_id,
            "person_id": row.principal_id,
            "role": row.role.value,
            "schema": "friday.private-message-window-row.v1",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _ledger_sha256(rows: tuple[_MessageRow, ...]) -> str:
    material = json.dumps(
        {
            "row_identity_sha256s": [_row_identity(row) for row in rows],
            "schema": "friday.private-message-window-row-ledger.v1",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _message_passage_ref(
    *,
    source_rows: tuple[_MessageRow, ...],
    anchor_message_id: str,
    window_message_ids: tuple[str, ...],
) -> PassageRef:
    if not source_rows:
        raise RuntimeError("conversation synthetic source ledger is empty")
    source = _source_ref(source_rows[0].conversation_id)
    if any(
        row.conversation_id != source.canonical_object_id or row.principal_id != SYNTHETIC_PRINCIPAL
        for row in source_rows
    ):
        raise RuntimeError("conversation synthetic source ledger is not owner-exact")
    by_id = {row.message_id: row for row in source_rows}
    try:
        window = tuple(by_id[message_id] for message_id in window_message_ids)
    except KeyError:
        raise RuntimeError("conversation synthetic qrel window escaped its ledger") from None
    ledger_positions = tuple(tuple(by_id).index(row.message_id) for row in window)
    if (
        not window
        or len(by_id) != len(source_rows)
        or ledger_positions != tuple(range(ledger_positions[0], ledger_positions[0] + len(window)))
        or anchor_message_id not in window_message_ids
    ):
        raise RuntimeError("conversation synthetic qrel window is not exact and contiguous")
    anchor_index = window_message_ids.index(anchor_message_id)
    start_at = datetime.fromisoformat(window[0].created_at)
    end_at = datetime.fromisoformat(window[-1].created_at) + timedelta(microseconds=1)
    representation = SourceRepresentation(
        RepresentationKind.CONVERSATION,
        source.canonical_object_id,
    )
    revision = SourceRevision(
        representation,
        RevisionKind.MESSAGE_LEDGER_SHA256,
        _ledger_sha256(source_rows),
    )
    return PassageRef(
        source_ref=source,
        source_revision=revision,
        locator=MessageWindowLocator.create(
            first_message_id=window[0].message_id,
            last_message_id=window[-1].message_id,
            start_at=start_at,
            end_at=end_at,
            context_before=anchor_index,
            context_after=len(window) - anchor_index - 1,
            matched_role=by_id[anchor_message_id].role,
        ),
        passage_index_version=MESSAGE_PASSAGE_INDEX_VERSION,
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )


def _request(intent: _CaseIntent) -> ArchiveSearchRequest:
    temporal_constraints: tuple[ArchiveTemporalConstraint, ...] = ()
    if intent.temporal_start is not None or intent.temporal_end is not None:
        if intent.temporal_start is None or intent.temporal_end is None:
            raise RuntimeError("conversation synthetic temporal qrel is half specified")
        temporal_constraints = (
            ArchiveTemporalConstraint(
                corpus=ArchiveSearchCorpus.MESSAGES,
                role=TemporalRole.CONVERSATION_TIME,
                value_kind=TemporalValueKind.INSTANT,
                precision=TemporalPrecision.INSTANT,
                start=intent.temporal_start,
                end=intent.temporal_end,
            ),
        )
    lifecycle_constraints = (
        ()
        if not intent.lifecycle_states
        else (
            ArchiveLifecycleConstraint.create(
                ArchiveSearchCorpus.MESSAGES,
                intent.lifecycle_states,
            ),
        )
    )
    return ArchiveSearchRequest.create(
        query=intent.query,
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        temporal_constraints=temporal_constraints,
        lifecycle_constraints=lifecycle_constraints,
        conversation_scope=intent.conversation_scope,
        roles=intent.roles,
        limit=10,
        context=intent.context,
    )


def _build_plan() -> _ConversationSyntheticPlan:
    conversations: list[_ConversationRow] = []
    messages: list[_MessageRow] = []
    timestamp_resets: list[_MessageTimestampReset] = []
    intents: list[_CaseIntent] = []
    next_conversation = 1
    next_message = 1

    def timestamp(ordinal: int) -> str:
        return (datetime(2026, 5, 1, tzinfo=UTC) + timedelta(minutes=ordinal)).isoformat()

    def add_conversation(
        title: str,
        *,
        archived: bool = False,
        phase: _SeedPhase = _SeedPhase.PRE_BACKFILL,
        conversation_id: str | None = None,
        principal_id: str = SYNTHETIC_PRINCIPAL,
    ) -> _ConversationRow:
        nonlocal next_conversation
        identifier = conversation_id or _conversation_id(next_conversation)
        next_conversation += int(conversation_id is None)
        row = _ConversationRow(
            identifier,
            principal_id,
            title,
            archived,
            "2026-05-01T00:00:00+00:00",
            phase,
        )
        conversations.append(row)
        return row

    def add_message(
        conversation: _ConversationRow,
        content: str,
        *,
        role: MessageRole = MessageRole.USER,
        phase: _SeedPhase | None = None,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> _MessageRow:
        nonlocal next_message
        identifier = message_id or _message_id(next_message)
        ordinal = next_message
        next_message += int(message_id is None)
        row = _MessageRow(
            identifier,
            conversation.conversation_id,
            conversation.principal_id,
            role,
            content,
            created_at or timestamp(ordinal),
            phase or conversation.phase,
        )
        messages.append(row)
        return row

    def add_intent(
        cell: _MatrixCell,
        query: str,
        target: _MessageRow,
        *,
        window: tuple[_MessageRow, ...] | None = None,
        channels: tuple[ArchiveMatchChannel, ...],
        contour: _ProjectionContour,
        context: ArchiveContextWindow = ArchiveContextWindow(),
        conversation_scope: ConversationScope = ConversationScope.ALL,
        roles: tuple[MessageRole, ...] = (),
        lifecycle_states: tuple[LifecycleState, ...] = (),
        temporal_start: str | None = None,
        temporal_end: str | None = None,
        restart_replay: bool = False,
        forbidden_sources: tuple[_ConversationRow, ...] = (),
        forbidden_messages: tuple[_MessageRow, ...] = (),
    ) -> None:
        intents.append(
            _CaseIntent(
                ordinal=len(intents) + 1,
                matrix_cell=cell,
                query=query,
                target_conversation_id=target.conversation_id,
                target_message_id=target.message_id,
                expected_window_message_ids=tuple(
                    row.message_id for row in ((target,) if window is None else window)
                ),
                expected_channels=channels,
                projection_contour=contour,
                context=context,
                conversation_scope=conversation_scope,
                roles=roles,
                lifecycle_states=lifecycle_states,
                temporal_start=temporal_start,
                temporal_end=temporal_end,
                restart_replay=restart_replay,
                forbidden_conversation_ids=tuple(row.conversation_id for row in forbidden_sources),
                forbidden_message_ids=tuple(row.message_id for row in forbidden_messages),
            )
        )

    both = (ArchiveMatchChannel.LEXICAL, ArchiveMatchChannel.MESSAGE_HISTORY)
    history = (ArchiveMatchChannel.MESSAGE_HISTORY,)

    # Archive search: active, archived, current-scope, and temporal/role controls.
    conversation = add_conversation("Archive active checksum")
    target = add_message(
        conversation,
        "The amaranth protocol stores the verified launch checksum.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.ARCHIVE,
        "amaranth launch checksum",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
    )

    conversation = add_conversation("Archive retired escrow", archived=True)
    target = add_message(
        conversation,
        "The birchglass archive records the retired escrow phrase.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.ARCHIVE,
        "birchglass escrow",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        lifecycle_states=(LifecycleState.ARCHIVED,),
    )

    boundary_conversation = add_conversation(
        "Synthetic accepted conversation",
        conversation_id=BOUNDARY_CONVERSATION_ID,
    )
    target = add_message(
        boundary_conversation,
        "The currentscope cedar memo says the relay opens after dusk.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.ARCHIVE,
        "currentscope cedar relay",
        target,
        channels=history,
        contour=_ProjectionContour.ACCEPTED_BOUNDARY,
        conversation_scope=ConversationScope.CURRENT,
    )

    conversation = add_conversation("Archive dated route")
    target = add_message(
        conversation,
        "The datedelta quill meeting chose the northern route.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.ARCHIVE,
        "datedelta northern route",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        roles=(MessageRole.ASSISTANT,),
        temporal_start=target.created_at,
        temporal_end=(datetime.fromisoformat(target.created_at) + timedelta(minutes=1)).isoformat(),
    )

    # Fallback: never-backfilled, appended tail, derivative reset, and a global
    # foreign FTS pool which cannot establish owner-complete lexical coverage.
    conversation = add_conversation(
        "Fallback late conversation",
        phase=_SeedPhase.LATE,
    )
    target = add_message(
        conversation,
        "The latequartz note preserves the emergency rendezvous code.",
    )
    add_intent(
        _MatrixCell.FALLBACK,
        "latequartz rendezvous",
        target,
        channels=history,
        contour=_ProjectionContour.BACKFILL_PENDING,
    )

    conversation = add_conversation("Fallback appended tail")
    add_message(conversation, "This base row is deliberately unrelated to the later answer.")
    target = add_message(
        conversation,
        "The appendviolet answer places the spare key beneath the sundial.",
        role=MessageRole.ASSISTANT,
        phase=_SeedPhase.LATE,
    )
    add_intent(
        _MatrixCell.FALLBACK,
        "appendviolet spare key",
        target,
        channels=history,
        contour=_ProjectionContour.SOURCE_CHANGED,
    )

    conversation = add_conversation("Fallback source reset")
    add_message(
        conversation,
        "This base row predates the timestamp source reset.",
        created_at="2026-05-20T00:00:00+00:00",
    )
    replacement = "The resetcobalt record confirms the west bridge inspection."
    reset_target = add_message(
        conversation,
        replacement,
        role=MessageRole.ASSISTANT,
        created_at=_RESET_FINAL_AT,
    )
    timestamp_resets.append(
        _MessageTimestampReset(
            reset_target.message_id,
            conversation.conversation_id,
            _RESET_INITIAL_AT,
            _RESET_FINAL_AT,
        )
    )
    add_intent(
        _MatrixCell.FALLBACK,
        "resetcobalt west bridge",
        reset_target,
        channels=history,
        contour=_ProjectionContour.SOURCE_CHANGED,
    )

    conversation = add_conversation("Fallback saturated lexical pool")
    target = add_message(
        conversation,
        "The poolamber owner record contains the authoritative fallback answer.",
    )
    add_intent(
        _MatrixCell.FALLBACK,
        "poolamber",
        target,
        channels=both,
        contour=_ProjectionContour.FOREIGN_SATURATED,
    )

    # Adjacent context: each qrel names the exact requested, available window.
    conversation = add_conversation("Adjacent context before")
    before = add_message(conversation, "The prior turn names the warehouse district.")
    target = add_message(
        conversation,
        "The adjacentmaple answer confirms locker seventeen.",
        role=MessageRole.ASSISTANT,
    )
    add_message(conversation, "The following turn changes to an unrelated subject.")
    add_intent(
        _MatrixCell.ADJACENT,
        "adjacentmaple locker",
        target,
        window=(before, target),
        channels=both,
        contour=_ProjectionContour.CURRENT,
        context=ArchiveContextWindow(before=1, after=0),
    )

    conversation = add_conversation("Adjacent context after")
    target = add_message(
        conversation,
        "The adjacentwillow answer starts the calibration sequence.",
    )
    after = add_message(
        conversation,
        "The next assistant turn supplies the calibration interval.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.ADJACENT,
        "adjacentwillow calibration",
        target,
        window=(target, after),
        channels=both,
        contour=_ProjectionContour.CURRENT,
        context=ArchiveContextWindow(before=0, after=1),
    )

    conversation = add_conversation("Adjacent context symmetric long-window")
    before = add_message(
        conversation,
        "Long adjacent context before " + "alpha " * 260,
    )
    target = add_message(
        conversation,
        "The adjacentcedar answer selects the silver compass.",
        role=MessageRole.ASSISTANT,
    )
    after = add_message(
        conversation,
        "Long adjacent context after " + "omega " * 260,
    )
    add_intent(
        _MatrixCell.ADJACENT,
        "adjacentcedar silver compass",
        target,
        window=(before, target, after),
        channels=both,
        contour=_ProjectionContour.CURRENT,
        context=ArchiveContextWindow(before=1, after=1),
    )

    conversation = add_conversation("Adjacent context radius two")
    window_rows = (
        add_message(conversation, "Radius two context opening turn."),
        add_message(conversation, "Radius two context preceding turn.", role=MessageRole.ASSISTANT),
        add_message(conversation, "The adjacentpine answer chooses beacon forty two."),
        add_message(conversation, "Radius two context following turn.", role=MessageRole.ASSISTANT),
        add_message(conversation, "Radius two context closing turn."),
    )
    add_intent(
        _MatrixCell.ADJACENT,
        "adjacentpine beacon",
        window_rows[2],
        window=window_rows,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        context=ArchiveContextWindow(before=2, after=2),
    )

    # Diversity: cross-lane merge, multiple hits in one source, a relevant
    # source beyond the public head, and foreign-pool starvation of one lane.
    conversation = add_conversation("Diversity cross lane")
    target = add_message(
        conversation,
        "The dualchanneliris decision keeps the green deployment window.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.DIVERSITY,
        "dualchanneliris green deployment",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
    )

    conversation = add_conversation("Diversity repeated source")
    add_message(conversation, "multihitbirch first supporting observation")
    add_message(conversation, "multihitbirch second supporting observation", role=MessageRole.ASSISTANT)
    target = add_message(conversation, "multihitbirch final authoritative observation")
    add_intent(
        _MatrixCell.DIVERSITY,
        "multihitbirch",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
    )

    conversation = add_conversation("Diversity continuation target")
    for index in range(24):
        decoy_conversation = add_conversation(f"Diversity continuation decoy {index + 1:02d}")
        add_message(decoy_conversation, "crowdedlotus synthetic decoy record")
    target = add_message(
        conversation,
        "crowdedlotus verified target answer",
        created_at="2026-05-01T00:00:01+00:00",
    )
    add_intent(
        _MatrixCell.DIVERSITY,
        "crowdedlotus",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
    )

    conversation = add_conversation("Diversity saturated pool")
    target = add_message(
        conversation,
        "The diversitypooltoken owner message names the relevant source.",
    )
    add_intent(
        _MatrixCell.DIVERSITY,
        "diversitypooltoken",
        target,
        channels=both,
        contour=_ProjectionContour.FOREIGN_SATURATED,
    )

    # Replay: diagnostics instruct the harness to close and reopen storage and
    # repeat exact search/replay checks without changing the qrel identity.
    conversation = add_conversation("Replay exact source")
    target = add_message(
        conversation,
        "The replayopal entry fixes the final inventory count at thirty one.",
    )
    add_intent(
        _MatrixCell.REPLAY,
        "replayopal inventory count",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        restart_replay=True,
    )

    conversation = add_conversation("Replay context window")
    before = add_message(
        conversation,
        "Long replay context before " + "north " * 260,
    )
    target = add_message(
        conversation,
        "The replaytopaz answer schedules the inspection for Tuesday.",
        role=MessageRole.ASSISTANT,
    )
    after = add_message(
        conversation,
        "Long replay context after " + "south " * 260,
    )
    add_intent(
        _MatrixCell.REPLAY,
        "replaytopaz inspection Tuesday",
        target,
        window=(before, target, after),
        channels=both,
        contour=_ProjectionContour.CURRENT,
        context=ArchiveContextWindow(before=1, after=1),
        restart_replay=True,
    )

    conversation = add_conversation("Replay archived source", archived=True)
    target = add_message(
        conversation,
        "The replaygarnet archive preserves the retired access procedure.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.REPLAY,
        "replaygarnet access procedure",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        lifecycle_states=(LifecycleState.ARCHIVED,),
        restart_replay=True,
    )

    conversation = add_conversation("Replay changed tail")
    add_message(conversation, "The stable prefix predates the replay answer.")
    target = add_message(
        conversation,
        "The replayindigo tail preserves the corrected routing number.",
        phase=_SeedPhase.LATE,
    )
    add_intent(
        _MatrixCell.REPLAY,
        "replayindigo routing number",
        target,
        channels=history,
        contour=_ProjectionContour.SOURCE_CHANGED,
        restart_replay=True,
    )

    # Privacy: owner filtering, accepted-boundary filtering, role filtering,
    # and lifecycle filtering each retain an authorized positive qrel.
    conversation = add_conversation("Privacy owner collision")
    target = add_message(
        conversation,
        "The ownerprivacyalpha answer belongs only to the benchmark principal.",
    )
    add_intent(
        _MatrixCell.PRIVACY,
        "ownerprivacyalpha",
        target,
        channels=history,
        contour=_ProjectionContour.FOREIGN_SATURATED,
    )

    conversation = add_conversation("Privacy accepted prefix")
    target = add_message(
        conversation,
        "The boundaryshieldbeta answer existed before the accepted request.",
    )
    post_boundary_decoy = add_message(
        conversation,
        "The boundaryshieldbeta post-boundary decoy must never enter recall.",
        phase=_SeedPhase.POST_BOUNDARY,
        created_at=_POST_BOUNDARY_AT,
    )
    add_intent(
        _MatrixCell.PRIVACY,
        "boundaryshieldbeta",
        target,
        channels=history,
        contour=_ProjectionContour.POST_BOUNDARY,
        forbidden_messages=(post_boundary_decoy,),
    )

    conversation = add_conversation("Privacy role filter")
    role_decoy = add_message(
        conversation,
        "The rolefiltergamma user decoy is not the requested speaker.",
    )
    target = add_message(
        conversation,
        "The rolefiltergamma assistant answer names the copper staircase.",
        role=MessageRole.ASSISTANT,
    )
    add_intent(
        _MatrixCell.PRIVACY,
        "rolefiltergamma copper staircase",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        roles=(MessageRole.ASSISTANT,),
        forbidden_messages=(role_decoy,),
    )

    conversation = add_conversation("Privacy active lifecycle")
    target = add_message(
        conversation,
        "The lifecycledelta active answer selects the east terminal.",
    )
    archived_decoy = add_conversation("Privacy archived lifecycle decoy", archived=True)
    add_message(
        archived_decoy,
        "The lifecycledelta archived decoy selects the wrong terminal.",
    )
    add_intent(
        _MatrixCell.PRIVACY,
        "lifecycledelta east terminal",
        target,
        channels=both,
        contour=_ProjectionContour.CURRENT,
        lifecycle_states=(LifecycleState.ACTIVE,),
        forbidden_sources=(archived_decoy,),
    )

    # The accepted turn is inserted only after every late/reset contour exists.
    add_message(
        boundary_conversation,
        "synthetic accepted conversation recall request",
        phase=_SeedPhase.ACCEPTED_BOUNDARY,
        message_id=BOUNDARY_MESSAGE_ID,
        created_at=_BOUNDARY_AT,
    )

    # One foreign source owns all 801 postings.  The harness backfills this
    # principal first, so each saturation query's retained global pool is
    # provably foreign even though MESSAGE_HISTORY stays owner-complete.
    foreign_conversation = _ConversationRow(
        "conv_e000000000000001",
        _FOREIGN_PRINCIPAL,
        "Foreign saturation source",
        False,
        "2026-05-10T00:00:00+00:00",
        _SeedPhase.FOREIGN_SATURATION,
    )
    conversations.append(foreign_conversation)
    for index in range(1, _FOREIGN_SATURATION_COUNT + 1):
        messages.append(
            _MessageRow(
                f"msg_{0xE000000000000000 + index:016x}",
                foreign_conversation.conversation_id,
                _FOREIGN_PRINCIPAL,
                MessageRole.USER,
                f"poolamber diversitypooltoken ownerprivacyalpha foreign{index:04d}",
                (datetime(2026, 5, 10, tzinfo=UTC) + timedelta(seconds=index)).isoformat(),
                _SeedPhase.FOREIGN_SATURATION,
            )
        )

    pre_boundary_messages = tuple(
        row
        for row in messages
        if row.principal_id == SYNTHETIC_PRINCIPAL and row.phase in {_SeedPhase.PRE_BACKFILL, _SeedPhase.LATE}
    )
    diagnostics: list[_ConversationCaseDiagnostic] = []
    conversations_by_id = {row.conversation_id: row for row in conversations}
    for intent in intents:
        source_rows = tuple(
            sorted(
                (
                    row
                    for row in pre_boundary_messages
                    if row.conversation_id == intent.target_conversation_id
                ),
                key=lambda row: (datetime.fromisoformat(row.created_at), row.message_id),
            )
        )
        passage_ref = _message_passage_ref(
            source_rows=source_rows,
            anchor_message_id=intent.target_message_id,
            window_message_ids=intent.expected_window_message_ids,
        )
        privacy_key_hex = _case_privacy_key(intent.ordinal)
        privacy_key = bytes.fromhex(privacy_key_hex)
        case = RecallCaseV1(
            case_id=f"conversation.case.{intent.ordinal:04d}",
            privacy_key_hex=privacy_key_hex,
            taxonomy=RecallTaxonomyV1.MESSAGE_PARAPHRASE,
            evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
            request=_request(intent),
            expected_corpus=ArchiveSearchCorpus.MESSAGES,
            alternatives=(
                RecallAlternativeV1(
                    source_identity=opaque_source_identity(passage_ref.source_ref, privacy_key),
                    passage_window_identities=(opaque_passage_window_identity(passage_ref, privacy_key),),
                    locator_kind=PassageLocatorKind.MESSAGE_WINDOW,
                    relevance_grade=3,
                    temporal_role=TemporalRole.CONVERSATION_TIME,
                ),
            ),
            expected_no_hit=False,
        )
        diagnostics.append(
            _ConversationCaseDiagnostic(
                case=case,
                matrix_cell=intent.matrix_cell,
                source_ref=passage_ref.source_ref,
                passage_ref=passage_ref,
                anchor_message_id=intent.target_message_id,
                expected_window_message_ids=intent.expected_window_message_ids,
                expected_context=intent.context,
                expected_channels=intent.expected_channels,
                projection_contour=intent.projection_contour,
                restart_replay=intent.restart_replay,
                forbidden_source_refs=tuple(
                    _source_ref(
                        conversation_id,
                        conversations_by_id[conversation_id].principal_id,
                    )
                    for conversation_id in intent.forbidden_conversation_ids
                ),
                forbidden_message_ids=intent.forbidden_message_ids,
            )
        )

    expected_cells = tuple(cell for cell in _MatrixCell for _index in range(4))
    observed_cells = tuple(item.matrix_cell for item in diagnostics)
    if (
        len(intents) != 24
        or observed_cells != expected_cells
        or len({item.case_id for item in diagnostics}) != 24
        or len({item.message_id for item in messages}) != len(messages)
        or len({item.conversation_id for item in conversations}) != len(conversations)
        or len(timestamp_resets) != 1
        or any(
            reset.message_id not in {item.message_id for item in messages}
            or reset.conversation_id not in {item.conversation_id for item in conversations}
            or reset.initial_created_at == reset.final_created_at
            for reset in timestamp_resets
        )
        or any(
            message_id not in {item.message_id for item in messages}
            for intent in intents
            for message_id in intent.forbidden_message_ids
        )
        or any(
            conversation_id not in {item.conversation_id for item in conversations}
            for intent in intents
            for conversation_id in intent.forbidden_conversation_ids
        )
    ):
        raise RuntimeError("conversation synthetic corpus matrix is inconsistent")
    cases = tuple(item.case for item in diagnostics)
    return _ConversationSyntheticPlan(
        cases=cases,
        diagnostics=tuple(diagnostics),
        conversations=tuple(conversations),
        messages=tuple(messages),
        timestamp_resets=tuple(timestamp_resets),
        foreign_principal_id=_FOREIGN_PRINCIPAL,
        accepted_conversation_id=BOUNDARY_CONVERSATION_ID,
        accepted_boundary_message_id=BOUNDARY_MESSAGE_ID,
    )


_PLAN: Final = _build_plan()


def conversation_synthetic_plan() -> _ConversationSyntheticPlan:
    """Return the immutable private rows, cases, and exact case diagnostics."""

    return _PLAN


def conversation_synthetic_cases() -> tuple[RecallCaseV1, ...]:
    """Return the deterministic existing-contract manifest of 24 cases."""

    return _PLAN.cases


def _seed_phase(storage: _ConversationSyntheticStorage, phase: _SeedPhase) -> None:
    initial_reset_times = {item.message_id: item.initial_created_at for item in _PLAN.timestamp_resets}
    with storage.transaction() as conn:
        for conversation in _PLAN.conversations:
            if conversation.phase is not phase:
                continue
            conn.execute(
                """INSERT INTO conversations(
                       id,user_id,title,last_message,unread_count,is_pinned,is_archived,
                       mode,created_at,updated_at
                   ) VALUES(?,?,?,'',0,0,?,'dialogue',?,?)""",
                (
                    conversation.conversation_id,
                    conversation.principal_id,
                    conversation.title,
                    int(conversation.archived),
                    conversation.created_at,
                    conversation.created_at,
                ),
            )
        for message in _PLAN.messages:
            if message.phase is not phase:
                continue
            conn.execute(
                """INSERT INTO messages(
                       id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
                   ) VALUES(?,?,?,?,?,'{}',NULL,?)""",
                (
                    message.message_id,
                    message.conversation_id,
                    message.principal_id,
                    message.role.value,
                    message.content,
                    initial_reset_times.get(message.message_id, message.created_at),
                ),
            )


def seed_conversation_synthetic_pre_backfill(storage: _ConversationSyntheticStorage) -> None:
    """Seed the benchmark owner rows which must be converged before late rows."""

    storage.ensure_user(SYNTHETIC_TENANT)
    storage.ensure_user(SYNTHETIC_PRINCIPAL)
    _seed_phase(storage, _SeedPhase.PRE_BACKFILL)


def seed_conversation_synthetic_foreign_saturation(
    storage: _ConversationSyntheticStorage,
) -> None:
    """Seed the foreign pool; its principal must be backfilled before the owner."""

    storage.ensure_user(_PLAN.foreign_principal_id)
    _seed_phase(storage, _SeedPhase.FOREIGN_SATURATION)


def seed_conversation_synthetic_late_rows(storage: _ConversationSyntheticStorage) -> None:
    """Create backfill-pending, appended-tail, and exact source-reset contours."""

    _seed_phase(storage, _SeedPhase.LATE)
    with storage.transaction() as conn:
        for reset in _PLAN.timestamp_resets:
            changed = conn.execute(
                """UPDATE messages SET created_at=?
                    WHERE id=? AND conversation_id=? AND created_at=?""",
                (
                    reset.final_created_at,
                    reset.message_id,
                    reset.conversation_id,
                    reset.initial_created_at,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("conversation synthetic source reset was not exact")


def seed_conversation_synthetic_accepted_boundary(
    storage: _ConversationSyntheticStorage,
) -> None:
    """Insert the accepted user-message boundary after all searchable source rows."""

    _seed_phase(storage, _SeedPhase.ACCEPTED_BOUNDARY)


def seed_conversation_synthetic_post_boundary(storage: _ConversationSyntheticStorage) -> None:
    """Insert the excluded future row only after the accepted boundary exists."""

    _seed_phase(storage, _SeedPhase.POST_BOUNDARY)


__all__ = [
    "conversation_synthetic_cases",
    "conversation_synthetic_plan",
    "seed_conversation_synthetic_accepted_boundary",
    "seed_conversation_synthetic_foreign_saturation",
    "seed_conversation_synthetic_late_rows",
    "seed_conversation_synthetic_post_boundary",
    "seed_conversation_synthetic_pre_backfill",
]
