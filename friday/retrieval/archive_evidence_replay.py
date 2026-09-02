"""Restart-safe exact replay of one durably selected archive source.

This seam never searches, invokes a model, or substitutes another source.  It
only reselects the persisted identities in the caller's live SQLite snapshot,
rechecks their exact source revision, and mints fresh process-private excerpts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import replace
from enum import StrEnum
from typing import NoReturn, SupportsIndex, cast

from friday.permissions import ActorContext, AuthorizationDecision, AuthorizationService
from friday.retrieval.archive_evidence_snapshot import (
    archive_selected_evidence_snapshot_sha256,
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.archive_search_message_adapter import (
    MESSAGE_PASSAGE_INDEX_VERSION,
    _bounded_message_excerpt,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    MessageRole,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevisionKind,
    SourceKind,
    SourceRef,
    TextSpanLocator,
)
from friday.storage._archive_search_documents import (
    ArchiveDocumentReplaySource,
    ArchiveDocumentStorageError,
    select_authorized_archive_document_replay_source_in_transaction,
)
from friday.storage._archive_search_messages import (
    ArchiveMessageReplayWindow,
    ArchiveMessageStorageError,
    select_authorized_archive_message_replay_source_in_transaction,
)

_SUPPORTED_CORPORA = frozenset(
    {
        ArchiveSearchCorpus.DOCUMENTS,
        ArchiveSearchCorpus.KNOWLEDGE,
        ArchiveSearchCorpus.MESSAGES,
    }
)
_CORPUS_CAPABILITY = {
    ArchiveSearchCorpus.DOCUMENTS: "knowledge.read",
    ArchiveSearchCorpus.KNOWLEDGE: "knowledge.read",
    ArchiveSearchCorpus.MESSAGES: "conversations.read",
}
_DOCUMENT_SOURCE_KINDS = frozenset({SourceKind.DOCUMENT})
_KNOWLEDGE_SOURCE_KINDS = frozenset(
    {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_MAX_ACTOR_BYTES = 200
_MAX_DOCUMENT_EXCERPT_CHARS = 720
_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)


class ArchiveEvidenceReplayError(ValueError):
    """A caller supplied a value outside the closed exact-replay contract."""


class ArchiveEvidenceReplayStatus(StrEnum):
    EXACT = "exact"
    DRIFTED = "drifted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class ArchiveEvidenceReplayCoverageGrade(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("archive evidence replay is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("archive evidence replay is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("archive evidence replay is process-private")


def _fail(message: str) -> ArchiveEvidenceReplayError:
    return ArchiveEvidenceReplayError(message)


def _actor(value: object, *, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail(f"archive replay {label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail(f"archive replay {label} is invalid") from None
    if len(encoded) > _MAX_ACTOR_BYTES or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise _fail(f"archive replay {label} is invalid")
    return value


class ArchiveEvidenceReplayExcerpt(_ProcessPrivate):
    __slots__ = ("citation_label", "passage_ref", "text")

    citation_label: str
    passage_ref: PassageRef
    text: str

    def __init__(
        self,
        citation_label: str,
        passage_ref: PassageRef,
        text: str,
        *,
        _factory: object = None,
    ) -> None:
        if (
            _factory is not _FACTORY
            or not re.fullmatch(r"A1\.[1-8]", citation_label)
            or type(passage_ref) is not PassageRef
            or type(text) is not str
            or not text
        ):
            raise _fail("archive replay excerpt is invalid")
        try:
            text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _fail("archive replay excerpt is invalid") from None
        object.__setattr__(self, "citation_label", citation_label)
        object.__setattr__(self, "passage_ref", passage_ref)
        object.__setattr__(self, "text", text)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive evidence replay is immutable")

    def __repr__(self) -> str:
        return f"ArchiveEvidenceReplayExcerpt(citation_label={self.citation_label!r}, private=True)"


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
        raise _fail("archive replay result is invalid") from None


class ArchiveEvidenceReplayResult(_ProcessPrivate):
    __slots__ = (
        "_corpus",
        "_coverage_grade",
        "_excerpts",
        "_model_visible_bytes",
        "_resolved_source",
        "_seal",
        "_status",
    )

    _status: ArchiveEvidenceReplayStatus
    _corpus: ArchiveSearchCorpus
    _coverage_grade: ArchiveEvidenceReplayCoverageGrade | None
    _resolved_source: ResolvedSource | None
    _excerpts: tuple[ArchiveEvidenceReplayExcerpt, ...]
    _model_visible_bytes: bytes
    _seal: bytes

    def __init__(
        self,
        status: ArchiveEvidenceReplayStatus,
        corpus: ArchiveSearchCorpus,
        coverage_grade: ArchiveEvidenceReplayCoverageGrade | None,
        resolved_source: ResolvedSource | None,
        excerpts: tuple[ArchiveEvidenceReplayExcerpt, ...],
        model_visible_bytes: bytes,
        *,
        _factory: object = None,
    ) -> None:
        exact = status is ArchiveEvidenceReplayStatus.EXACT
        if (
            _factory is not _FACTORY
            or type(status) is not ArchiveEvidenceReplayStatus
            or type(corpus) is not ArchiveSearchCorpus
            or corpus not in _SUPPORTED_CORPORA
            or type(excerpts) is not tuple
            or any(type(item) is not ArchiveEvidenceReplayExcerpt for item in excerpts)
            or type(model_visible_bytes) is not bytes
            or (
                exact
                and (
                    type(coverage_grade) is not ArchiveEvidenceReplayCoverageGrade
                    or type(resolved_source) is not ResolvedSource
                    or not excerpts
                    or not model_visible_bytes
                )
            )
            or (
                not exact
                and (
                    coverage_grade is not None
                    or resolved_source is not None
                    or excerpts
                    or model_visible_bytes
                )
            )
        ):
            raise _fail("archive replay result is invalid")
        object.__setattr__(self, "_status", status)
        object.__setattr__(self, "_corpus", corpus)
        object.__setattr__(self, "_coverage_grade", coverage_grade)
        object.__setattr__(self, "_resolved_source", resolved_source)
        object.__setattr__(self, "_excerpts", excerpts)
        object.__setattr__(self, "_model_visible_bytes", model_visible_bytes)
        object.__setattr__(self, "_seal", _result_seal(self))

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive evidence replay is immutable")

    def __repr__(self) -> str:
        return (
            "ArchiveEvidenceReplayResult("
            f"status={self._status.value!r}, corpus={self._corpus.value!r}, private=True)"
        )

    def is_valid(self) -> bool:
        try:
            return bool(
                type(self) is ArchiveEvidenceReplayResult
                and type(self._seal) is bytes
                and len(self._seal) == hashlib.sha256().digest_size
                and hmac.compare_digest(self._seal, _result_seal(self))
            )
        except Exception:
            return False

    def _require_valid(self) -> None:
        if not self.is_valid():
            raise _fail("archive replay result is unavailable")

    @property
    def status(self) -> ArchiveEvidenceReplayStatus:
        self._require_valid()
        return self._status

    @property
    def corpus(self) -> ArchiveSearchCorpus:
        self._require_valid()
        return self._corpus

    @property
    def coverage_grade(self) -> ArchiveEvidenceReplayCoverageGrade | None:
        self._require_valid()
        return self._coverage_grade

    @property
    def resolved_source(self) -> ResolvedSource | None:
        self._require_valid()
        return self._resolved_source

    @property
    def excerpts(self) -> tuple[ArchiveEvidenceReplayExcerpt, ...]:
        self._require_valid()
        return self._excerpts

    @property
    def model_visible_bytes(self) -> bytes:
        self._require_valid()
        if self._status is not ArchiveEvidenceReplayStatus.EXACT:
            raise _fail("archive replay has no model-visible evidence")
        return self._model_visible_bytes


def _result_seal(value: ArchiveEvidenceReplayResult) -> bytes:
    material = {
        "corpus": value._corpus.value,
        "coverage_grade": (None if value._coverage_grade is None else value._coverage_grade.value),
        "excerpts": [
            {
                "citation_label": item.citation_label,
                "passage_ref": item.passage_ref.to_private_payload(),
                "text": item.text,
            }
            for item in value._excerpts
        ],
        "model_visible_sha256": hashlib.sha256(value._model_visible_bytes).hexdigest(),
        "resolved_source": (
            None if value._resolved_source is None else value._resolved_source.to_private_payload()
        ),
        "schema": "friday.archive-evidence-replay-result.private.v1",
        "status": value._status.value,
    }
    return hmac.new(
        _PROCESS_KEY,
        b"friday/archive-evidence-replay-result/v1\0" + _canonical_bytes(material),
        hashlib.sha256,
    ).digest()


def _closed_result(
    status: ArchiveEvidenceReplayStatus,
    corpus: ArchiveSearchCorpus,
) -> ArchiveEvidenceReplayResult:
    if status not in {
        ArchiveEvidenceReplayStatus.DRIFTED,
        ArchiveEvidenceReplayStatus.DENIED,
        ArchiveEvidenceReplayStatus.UNAVAILABLE,
    }:
        raise _fail("archive replay closed status is invalid")
    return ArchiveEvidenceReplayResult(
        status,
        corpus,
        None,
        None,
        (),
        b"",
        _factory=_FACTORY,
    )


def unavailable_archive_evidence_replay_result(
    corpus: ArchiveSearchCorpus,
) -> ArchiveEvidenceReplayResult:
    """Return the sole source-free result for an unavailable replay seam."""

    if type(corpus) is not ArchiveSearchCorpus or corpus not in _SUPPORTED_CORPORA:
        raise _fail("archive replay corpus is invalid")
    return _closed_result(ArchiveEvidenceReplayStatus.UNAVAILABLE, corpus)


def _exact_result(
    *,
    corpus: ArchiveSearchCorpus,
    coverage_grade: ArchiveEvidenceReplayCoverageGrade,
    resolved_source: ResolvedSource,
    passage_refs: tuple[PassageRef, ...],
    texts: tuple[str, ...],
) -> ArchiveEvidenceReplayResult:
    excerpts = tuple(
        ArchiveEvidenceReplayExcerpt(
            f"A1.{index}",
            passage_ref,
            text,
            _factory=_FACTORY,
        )
        for index, (passage_ref, text) in enumerate(
            zip(passage_refs, texts, strict=True),
            1,
        )
    )
    visible = _canonical_bytes(
        {
            "corpus": corpus.value,
            "coverage_grade": coverage_grade.value,
            "evidence": [{"citation": f"[{item.citation_label}]", "excerpt": item.text} for item in excerpts],
            "schema": "friday.archive-evidence-replay-model.v1",
        }
    )
    return ArchiveEvidenceReplayResult(
        ArchiveEvidenceReplayStatus.EXACT,
        corpus,
        coverage_grade,
        resolved_source,
        excerpts,
        visible,
        _factory=_FACTORY,
    )


def _validate_source_and_passages(
    *,
    tenant_id: str,
    principal_id: str,
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    passage_refs: tuple[PassageRef, ...],
) -> None:
    if type(source_ref) is not SourceRef:
        raise _fail("archive replay source is invalid")
    if (
        type(passage_refs) is not tuple
        or not 1 <= len(passage_refs) <= 8
        or any(type(item) is not PassageRef for item in passage_refs)
        or any(item.source_ref != source_ref for item in passage_refs)
    ):
        raise _fail("archive replay passages are invalid")
    identities = tuple(item.to_private_json() for item in passage_refs)
    locator_identities = tuple(_canonical_bytes(item.locator.to_private_payload()) for item in passage_refs)
    if (
        identities != tuple(sorted(identities))
        or len(identities) != len(set(identities))
        or len(locator_identities) != len(set(locator_identities))
        or len({item.source_revision for item in passage_refs}) != 1
        or len({item.passage_index_version for item in passage_refs}) != 1
    ):
        raise _fail("archive replay passages are not canonical")

    if corpus is ArchiveSearchCorpus.MESSAGES:
        valid = (
            source_ref.source_kind is SourceKind.CONVERSATION
            and source_ref.authority_scope is AuthorityScope.PRINCIPAL
            and source_ref.tenant_id is None
            and source_ref.principal_id == principal_id
            and source_ref.canonical_object_kind is CanonicalObjectKind.CONVERSATION
            and all(
                type(item.locator) is MessageWindowLocator
                and item.passage_index_version == MESSAGE_PASSAGE_INDEX_VERSION
                and item.source_revision.kind is RevisionKind.MESSAGE_LEDGER_SHA256
                and item.source_revision.representation.kind is RepresentationKind.CONVERSATION
                and item.source_revision.representation.object_id == source_ref.canonical_object_id
                for item in passage_refs
            )
        )
    else:
        source_kinds = (
            _DOCUMENT_SOURCE_KINDS if corpus is ArchiveSearchCorpus.DOCUMENTS else _KNOWLEDGE_SOURCE_KINDS
        )
        expected_representation = (
            RepresentationKind.RAW_OBJECT
            if corpus is ArchiveSearchCorpus.DOCUMENTS
            else RepresentationKind.KNOWLEDGE_OBJECT
        )
        expected_revision = (
            RevisionKind.RAW_CONTENT_SHA256
            if corpus is ArchiveSearchCorpus.DOCUMENTS
            else RevisionKind.KNOWLEDGE_VERSION
        )
        valid_passage_versions = (
            frozenset(
                {
                    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
                    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
                }
            )
            if corpus is ArchiveSearchCorpus.DOCUMENTS
            else frozenset({LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION})
        )
        valid = (
            source_ref.source_kind in source_kinds
            and source_ref.authority_scope is AuthorityScope.TENANT_PRINCIPAL
            and source_ref.tenant_id == tenant_id
            and source_ref.principal_id == principal_id
            and source_ref.canonical_object_kind is CanonicalObjectKind.RAW_OBJECT
            and all(
                type(item.locator) is TextSpanLocator
                and item.passage_index_version in valid_passage_versions
                and item.source_revision.kind is expected_revision
                and item.source_revision.representation.kind is expected_representation
                for item in passage_refs
            )
        )
    if not valid:
        raise _fail("archive replay source and passages disagree")


def _active_origin_boundary(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    principal_id: str,
    origin_boundary_user_message_id: str,
) -> ArchiveEvidenceReplayStatus | None:
    try:
        row = conn.execute(
            """SELECT EXISTS (
                       SELECT 1 FROM users WHERE id=? AND status='active'
                   ), EXISTS (
                       SELECT 1 FROM users WHERE id=? AND status='active'
                   ), EXISTS (
                       SELECT 1
                         FROM messages b
                         JOIN conversations c
                           ON c.id=b.conversation_id AND c.user_id=b.user_id
                        WHERE b.id=? AND b.user_id=? AND b.role='user'
                          AND c.user_id=?
                   )""",
            (
                tenant_id,
                principal_id,
                origin_boundary_user_message_id,
                principal_id,
                principal_id,
            ),
        ).fetchone()
    except sqlite3.Error:
        return ArchiveEvidenceReplayStatus.UNAVAILABLE
    if row is None or len(row) != 3 or any(type(item) is not int for item in row):
        return ArchiveEvidenceReplayStatus.UNAVAILABLE
    return None if tuple(row) == (1, 1, 1) else ArchiveEvidenceReplayStatus.DENIED


def _actor_is_exactly_bound(
    actor: object,
    *,
    tenant_id: str,
    principal_id: str,
) -> bool:
    if type(actor) is not ActorContext:
        return False
    try:
        return bool(
            type(actor.user_id) is str
            and type(actor.preset_key) is str
            and type(actor.source) is str
            and (actor.identity_id is None or type(actor.identity_id) is str)
            and (actor.session_id is None or type(actor.session_id) is str)
            and type(actor.shared_tenant) is bool
            and type(actor.person_id) is str
            and actor.user_id == tenant_id
            and actor.own_id == principal_id
        )
    except Exception:
        return False


def _fresh_authority_status(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    principal_id: str,
    corpus: ArchiveSearchCorpus,
) -> ArchiveEvidenceReplayStatus | None:
    """Recheck both capabilities from the caller's current SQLite snapshot."""

    try:
        storage = authorization.storage
        if storage is None or storage.conn is not conn:
            return ArchiveEvidenceReplayStatus.UNAVAILABLE
        principal_row = conn.execute(
            "SELECT preset_key, status FROM users WHERE id=?",
            (principal_id,),
        ).fetchone()
        tenant_row = (
            principal_row
            if tenant_id == principal_id
            else conn.execute(
                "SELECT status FROM users WHERE id=?",
                (tenant_id,),
            ).fetchone()
        )
        if principal_row is None or tenant_row is None:
            return ArchiveEvidenceReplayStatus.DENIED
        if str(principal_row["status"] or "") != "active" or str(tenant_row["status"] or "") != "active":
            return ArchiveEvidenceReplayStatus.DENIED
        fresh_actor = replace(
            actor,
            preset_key=str(principal_row["preset_key"] or "guest"),
        )
        if not _actor_is_exactly_bound(
            fresh_actor,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ):
            return ArchiveEvidenceReplayStatus.UNAVAILABLE
        for capability in ("search.use", _CORPUS_CAPABILITY[corpus]):
            decision = authorization.authorize(fresh_actor, capability)
            if (
                type(decision) is not AuthorizationDecision
                or decision.security_id != capability
                or decision.user_id != principal_id
                or decision.preset_key != fresh_actor.preset_key
                or decision.effect not in {"allow", "deny"}
            ):
                return ArchiveEvidenceReplayStatus.UNAVAILABLE
            if not decision.allowed:
                return ArchiveEvidenceReplayStatus.DENIED
        return None if conn.in_transaction else ArchiveEvidenceReplayStatus.UNAVAILABLE
    except Exception:
        return ArchiveEvidenceReplayStatus.UNAVAILABLE


def _document_texts(
    replay: ArchiveDocumentReplaySource,
    passage_refs: tuple[PassageRef, ...],
) -> tuple[str, ...] | None:
    texts: list[str] = []
    for passage_ref in passage_refs:
        locator = passage_ref.locator
        if type(locator) is not TextSpanLocator:
            return None
        if (
            locator.end_char > len(replay.body)
            or locator.end_char - locator.start_char > _MAX_DOCUMENT_EXCERPT_CHARS
        ):
            return None
        text: str | None
        if passage_ref.passage_index_version == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION:
            if locator.chunk_index != 0:
                return None
            text = replay.body[locator.start_char : locator.end_char]
        elif (
            passage_ref.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
            and replay.corpus is ArchiveSearchCorpus.DOCUMENTS
        ):
            text = replay.stored_passage_text(locator)
            if text is None:
                return None
        else:
            return None
        if (
            not text
            or text != text.strip()
            or "\r" in text
            or any(
                unicodedata.category(character).startswith("C") and character != "\n" for character in text
            )
        ):
            return None
        texts.append(text)
    return tuple(texts)


def _message_excerpt(
    window: ArchiveMessageReplayWindow,
    *,
    matched_index: int,
) -> str:
    return _bounded_message_excerpt(
        tuple((row.role, row.content) for row in window.rows),
        matched_index=matched_index,
    )


def _legacy_bounded_message_excerpt(rows: tuple[tuple[MessageRole, str], ...]) -> str:
    """Render the released v1 head/tail form for durable pre-R5 snapshots."""

    parts: list[str] = []
    for row_role, row_content in rows:
        role = "Пользователь" if row_role is MessageRole.USER else "Friday"
        content = " ".join(row_content.split())
        if content:
            parts.append(f"{role}: {content}")
    text = " | ".join(parts) or "Сообщение без текстового содержимого"
    if len(text) <= 1_900:
        return text
    left = (1_900 - 5) // 2
    right = 1_900 - 5 - left
    return f"{text[:left].rstrip()} … {text[-right:].lstrip()}"


def _legacy_message_excerpt(window: ArchiveMessageReplayWindow) -> str:
    return _legacy_bounded_message_excerpt(tuple((row.role, row.content) for row in window.rows))


def replay_archive_evidence_in_transaction(
    conn: sqlite3.Connection,
    *,
    authorization: AuthorizationService,
    actor: ActorContext,
    tenant_id: str,
    principal_id: str,
    origin_boundary_user_message_id: str,
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    passage_refs: tuple[PassageRef, ...],
    expected_source_snapshot_sha256: str,
    expected_coverage_grade: ArchiveEvidenceReplayCoverageGrade,
) -> ArchiveEvidenceReplayResult:
    """Replay exactly one selected source, or return one closed failure status.

    Authority, actor activity, storage ownership, lifecycle and revisions are
    all rechecked in the caller-owned transaction before any body is returned.
    """

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise _fail("archive replay requires a caller-owned SQLite transaction")
    tenant = _actor(tenant_id, label="tenant")
    principal = _actor(principal_id, label="principal")
    if (
        type(origin_boundary_user_message_id) is not str
        or _MESSAGE_ID.fullmatch(origin_boundary_user_message_id) is None
    ):
        raise _fail("archive replay origin boundary is invalid")
    if type(corpus) is not ArchiveSearchCorpus or corpus not in _SUPPORTED_CORPORA:
        raise _fail("archive replay corpus is invalid")
    if type(authorization) is not AuthorizationService or not _actor_is_exactly_bound(
        actor,
        tenant_id=tenant,
        principal_id=principal,
    ):
        raise _fail("archive replay authority context is invalid")
    if (
        type(expected_source_snapshot_sha256) is not str
        or _DIGEST.fullmatch(expected_source_snapshot_sha256) is None
    ):
        raise _fail("archive replay snapshot digest is invalid")
    if type(expected_coverage_grade) is not ArchiveEvidenceReplayCoverageGrade:
        raise _fail("archive replay coverage grade is invalid")
    try:
        _validate_source_and_passages(
            tenant_id=tenant,
            principal_id=principal,
            corpus=corpus,
            source_ref=source_ref,
            passage_refs=passage_refs,
        )
    except ArchiveEvidenceReplayError:
        raise
    except Exception:
        raise _fail("archive replay source or passages are invalid") from None

    authority_status = _fresh_authority_status(
        conn,
        authorization=authorization,
        actor=actor,
        tenant_id=tenant,
        principal_id=principal,
        corpus=corpus,
    )
    if authority_status is not None:
        return _closed_result(authority_status, corpus)
    origin_status = _active_origin_boundary(
        conn,
        tenant_id=tenant,
        principal_id=principal,
        origin_boundary_user_message_id=origin_boundary_user_message_id,
    )
    if origin_status is not None:
        return _closed_result(origin_status, corpus)

    try:
        legacy_texts: tuple[str, ...] | None = None
        if corpus in {ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE}:
            knowledge_object_id = (
                None
                if corpus is ArchiveSearchCorpus.DOCUMENTS
                else passage_refs[0].source_revision.representation.object_id
            )
            replay = select_authorized_archive_document_replay_source_in_transaction(
                conn,
                tenant_id=tenant,
                owner_id=principal,
                origin_boundary_user_message_id=origin_boundary_user_message_id,
                corpus=corpus,
                source_ref=source_ref,
                knowledge_object_id=knowledge_object_id,
                source_revision=passage_refs[0].source_revision,
            )
            if replay is None:
                return _closed_result(ArchiveEvidenceReplayStatus.DRIFTED, corpus)
            resolved_source = replay.resolved_source
            texts = _document_texts(replay, passage_refs)
        else:
            replay_messages = select_authorized_archive_message_replay_source_in_transaction(
                conn,
                principal_id=principal,
                origin_boundary_user_message_id=origin_boundary_user_message_id,
                source_ref=source_ref,
                locators=tuple(
                    cast(MessageWindowLocator, passage_ref.locator) for passage_ref in passage_refs
                ),  # validated above as exact MessageWindowLocator values
            )
            if replay_messages is None:
                return _closed_result(ArchiveEvidenceReplayStatus.DRIFTED, corpus)
            resolved_source = replay_messages.resolved_source
            texts = tuple(
                _message_excerpt(
                    window,
                    matched_index=cast(MessageWindowLocator, passage_ref.locator).context_before,
                )
                for window, passage_ref in zip(
                    replay_messages.windows,
                    passage_refs,
                    strict=True,
                )
            )
            legacy_texts = tuple(_legacy_message_excerpt(window) for window in replay_messages.windows)
    except (ArchiveDocumentStorageError, ArchiveMessageStorageError, sqlite3.Error):
        return _closed_result(ArchiveEvidenceReplayStatus.UNAVAILABLE, corpus)
    except Exception:
        return _closed_result(ArchiveEvidenceReplayStatus.UNAVAILABLE, corpus)

    try:
        drifted = bool(
            resolved_source.source_ref != source_ref
            or any(not passage_ref.revision_matches(resolved_source) for passage_ref in passage_refs)
            or texts is None
            or len(texts) != len(passage_refs)
        )
        exact_texts: tuple[str, ...] | None = None
        if not drifted:
            current_texts = cast(tuple[str, ...], texts)
            if hmac.compare_digest(
                archive_selected_evidence_snapshot_sha256(
                    resolved_source,
                    passage_refs,
                    current_texts,
                ),
                expected_source_snapshot_sha256,
            ):
                exact_texts = current_texts
            elif legacy_texts is not None and hmac.compare_digest(
                archive_selected_evidence_snapshot_sha256(
                    resolved_source,
                    passage_refs,
                    legacy_texts,
                ),
                expected_source_snapshot_sha256,
            ):
                exact_texts = legacy_texts
            else:
                drifted = True
    except Exception:
        return _closed_result(ArchiveEvidenceReplayStatus.UNAVAILABLE, corpus)
    if drifted or exact_texts is None:
        return _closed_result(ArchiveEvidenceReplayStatus.DRIFTED, corpus)
    try:
        return _exact_result(
            corpus=corpus,
            coverage_grade=expected_coverage_grade,
            resolved_source=resolved_source,
            passage_refs=passage_refs,
            texts=exact_texts,
        )
    except Exception:
        return _closed_result(ArchiveEvidenceReplayStatus.UNAVAILABLE, corpus)


__all__ = [
    "ArchiveEvidenceReplayCoverageGrade",
    "ArchiveEvidenceReplayError",
    "ArchiveEvidenceReplayExcerpt",
    "ArchiveEvidenceReplayResult",
    "ArchiveEvidenceReplayStatus",
    "replay_archive_evidence_in_transaction",
    "unavailable_archive_evidence_replay_result",
]
