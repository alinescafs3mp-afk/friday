"""Fail-closed assembly of current-turn and selected historical file evidence.

The public request carries only opaque Raw identifiers or a process-owned exact
historical selector.  This module is the single bridge from those references to
model-visible text: it reauthorizes the exact tenant and uploader, verifies the
registered bytes under one SQLite write barrier, and binds every immutable Raw
row to a process-owned snapshot token.

The V12 canary intentionally accepts only complete native UTF-8 extraction:
one/two current-turn files or a bounded, exact selection of the actor's own
previous files.  OCR, speech/vision output, partial parsing, ambiguous or
foreign-user history, reply/replay references and oversized documents remain
legacy-owned until their separate evidence contracts are implemented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from friday.evidence_bundle import CitationBinding, EvidenceBundle, EvidencePart
from friday.file_delivery import (
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_file_in_transaction,
)
from friday.file_evidence import FileBodyKind, FileEvidenceSet, FileEvidenceView, FileRegistrationKind
from friday.permissions import ActorContext, AuthorizationService
from friday.raw_metadata import bounded_raw_file_metadata
from friday.secret_hygiene import named_secrets
from friday.source_identity import (
    AuthorizedFileSnapshotToken,
    authorized_file_snapshot_token_authorizes_scope,
    raw_source_identity_sha256,
)
from friday.storage._core import guarded_storage_transaction
from friday.telemetry.logging import redact_friday_api_tokens

_PROCESS_AUTHORITY = object()
_RAW_ID_RE = re.compile(r"raw_[0-9a-f]{16}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PROVENANCE_STUB_RE = re.compile(r"\A\s*\[[A-Za-z][A-Za-z0-9_-]{0,40}:\s*", re.ASCII)
_MAX_FILES = 12
_MAX_PART_CHARS = 48_000
_MAX_TOTAL_CHARS = 120_000
_HISTORICAL_SELECTOR_KINDS = frozenset({"exact_filename", "time_window", "latest", "source_search_result"})
_SOURCE_SEARCH_RESULT_RAW_IDS = "source_search_result_raw_ids"
_SOURCE_SEARCH_RESULT_IDENTITIES = "source_search_result_identities"
# This is the durable page emitted by agent_runtime._SOURCE_SEARCH_PAGE_SIZE.
# Keep the independent reader fail-closed if a future producer widens it.
_SOURCE_SEARCH_RESULT_LIMIT = 10
_SOURCE_SEARCH_METADATA_MAX_CHARS = 65_536
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}")
_NATIVE_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".csv",
        ".ini",
        ".json",
        ".jsonl",
        ".log",
        ".markdown",
        ".md",
        ".py",
        ".rst",
        ".sql",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_NATIVE_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/toml",
        "application/x-ndjson",
        "application/xml",
        "application/yaml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/tab-separated-values",
        "text/xml",
        "text/yaml",
    }
)
_BINARY_MAGICS = (
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\x1f\x8b",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
)


class CurrentTurnFileReference(Protocol):
    @property
    def ordinal(self) -> int: ...

    @property
    def raw_object_id(self) -> str: ...

    @property
    def source_identity_sha256(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def media_type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PinnedFileEvidenceReference:
    """Body-free durable pin which grants no authority by itself."""

    raw_object_id: str
    source_identity_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.raw_object_id, str)
            or _RAW_ID_RE.fullmatch(self.raw_object_id) is None
            or not isinstance(self.source_identity_sha256, str)
            or _SHA256_RE.fullmatch(self.source_identity_sha256) is None
            or not isinstance(self.content_sha256, str)
            or _SHA256_RE.fullmatch(self.content_sha256) is None
        ):
            raise ValueError("pinned file evidence reference is invalid")


@dataclass(frozen=True, slots=True)
class _HistoricalSourceReference:
    """Transaction-local Raw identity created only after fresh authorization."""

    ordinal: int
    raw_object_id: str
    source_identity_sha256: str
    name: str = "registered-file"
    media_type: str = "binary"


class FileEvidenceUnavailable(Exception):
    """The current source cannot enter the strict V12 file canary."""


@dataclass(frozen=True, slots=True)
class HistoricalFileSelectionToken:
    """Process-owned selector whose exact result must survive publication."""

    tenant_id: str
    uploaded_by: str
    kind: str
    raw_ids: tuple[str, ...]
    filename: str
    received_since: str | None
    received_until: str | None
    document_since: str | None
    document_until: str | None
    latest_count: int | None
    conversation_id: str
    source_message_id: str
    source_result_raw_ids: tuple[str, ...]
    source_result_identities: tuple[tuple[str, str], ...]
    source_result_ordinal: int | None
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.raw_ids) is not tuple:
            raise ValueError("historical file selector is invalid")
        if type(self.source_result_raw_ids) is not tuple:
            raise ValueError("historical file selector is invalid")
        if type(self.source_result_identities) is not tuple or any(
            type(item) is not tuple or len(item) != 2 for item in self.source_result_identities
        ):
            raise ValueError("historical file selector is invalid")
        scalar_strings = (
            self.tenant_id,
            self.uploaded_by,
            self.kind,
            self.filename,
            self.conversation_id,
            self.source_message_id,
            *self.raw_ids,
            *self.source_result_raw_ids,
            *(value for item in self.source_result_identities for value in item),
        )
        optional_strings = (
            self.received_since,
            self.received_until,
            self.document_since,
            self.document_until,
        )
        has_received_window = self.received_since is not None or self.received_until is not None
        has_document_window = self.document_since is not None or self.document_until is not None
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or any(type(value) is not str for value in scalar_strings)
            or any(value is not None and type(value) is not str for value in optional_strings)
            or (
                self.latest_count is not None
                and (type(self.latest_count) is not int or self.latest_count not in {1, 2})
            )
            or (
                self.source_result_ordinal is not None
                and (
                    type(self.source_result_ordinal) is not int
                    or not 1 <= self.source_result_ordinal <= _SOURCE_SEARCH_RESULT_LIMIT
                )
            )
            or self.kind not in _HISTORICAL_SELECTOR_KINDS
            or not self.tenant_id
            or not self.uploaded_by
            or not 1 <= len(self.raw_ids) <= 2
            or len(set(self.raw_ids)) != len(self.raw_ids)
            or any(_RAW_ID_RE.fullmatch(raw_id) is None for raw_id in self.raw_ids)
        ):
            raise ValueError("historical file selector is invalid")
        if self.kind == "exact_filename":
            if (
                not self.filename
                or len(self.filename) > 260
                or self.conversation_id
                or self.source_message_id
                or self.source_result_raw_ids
                or self.source_result_identities
                or self.source_result_ordinal is not None
                or any(
                    value is not None
                    for value in (
                        self.received_since,
                        self.received_until,
                        self.document_since,
                        self.document_until,
                        self.latest_count,
                    )
                )
            ):
                raise ValueError("historical exact-name selector is invalid")
        elif self.kind == "latest":
            if (
                self.latest_count not in {1, 2}
                or self.filename
                or self.conversation_id
                or self.source_message_id
                or self.source_result_raw_ids
                or self.source_result_identities
                or self.source_result_ordinal is not None
                or any(
                    value is not None
                    for value in (
                        self.received_since,
                        self.received_until,
                        self.document_since,
                        self.document_until,
                    )
                )
            ):
                raise ValueError("historical latest selector is invalid")
        elif self.kind == "time_window":
            if (
                self.filename
                or self.latest_count is not None
                or self.conversation_id
                or self.source_message_id
                or self.source_result_raw_ids
                or self.source_result_identities
                or self.source_result_ordinal is not None
                or has_received_window == has_document_window
            ):
                raise ValueError("historical time selector is invalid")
        elif (
            self.filename
            or self.latest_count is not None
            or has_received_window
            or has_document_window
            or _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None
            or _MESSAGE_ID_RE.fullmatch(self.source_message_id) is None
            or not 1 <= len(self.source_result_raw_ids) <= _SOURCE_SEARCH_RESULT_LIMIT
            or len(set(self.source_result_raw_ids)) != len(self.source_result_raw_ids)
            or any(_RAW_ID_RE.fullmatch(raw_id) is None for raw_id in self.source_result_raw_ids)
            or len(self.source_result_identities) != len(self.source_result_raw_ids)
            or tuple(raw_id for raw_id, _identity in self.source_result_identities)
            != self.source_result_raw_ids
            or any(
                _SHA256_RE.fullmatch(identity) is None for _raw_id, identity in self.source_result_identities
            )
            or self.source_result_ordinal is None
            or self.source_result_ordinal > len(self.source_result_raw_ids)
            or self.raw_ids != (self.source_result_raw_ids[self.source_result_ordinal - 1],)
        ):
            raise ValueError("historical source-search selector is invalid")

    def identity_sha256(self) -> str:
        digest = hashlib.sha256()
        for value in (
            self.tenant_id,
            self.uploaded_by,
            self.kind,
            *self.raw_ids,
            self.filename,
            self.received_since or "",
            self.received_until or "",
            self.document_since or "",
            self.document_until or "",
            str(self.latest_count or ""),
            self.conversation_id,
            self.source_message_id,
            *self.source_result_raw_ids,
            *(value for item in self.source_result_identities for value in item),
            str(self.source_result_ordinal or ""),
        ):
            encoded = value.encode("utf-8", errors="strict")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()


def historical_file_selection_token(
    *,
    tenant_id: str,
    uploaded_by: str,
    kind: str,
    raw_ids: Sequence[str],
    filename: str = "",
    received_since: str | None = None,
    received_until: str | None = None,
    document_since: str | None = None,
    document_until: str | None = None,
    latest_count: int | None = None,
    conversation_id: str = "",
    source_message_id: str = "",
    source_result_raw_ids: Sequence[str] = (),
    source_result_identities: Sequence[tuple[str, str]] = (),
    source_result_ordinal: int | None = None,
) -> HistoricalFileSelectionToken:
    return HistoricalFileSelectionToken(
        tenant_id=str(tenant_id or "").strip(),
        uploaded_by=str(uploaded_by or "").strip(),
        kind=str(kind or "").strip(),
        raw_ids=tuple(str(raw_id or "").strip() for raw_id in raw_ids),
        filename=str(filename or "").strip(),
        received_since=received_since,
        received_until=received_until,
        document_since=document_since,
        document_until=document_until,
        latest_count=latest_count,
        conversation_id=str(conversation_id or "").strip(),
        source_message_id=str(source_message_id or "").strip(),
        source_result_raw_ids=tuple(str(raw_id or "").strip() for raw_id in source_result_raw_ids),
        source_result_identities=tuple(
            (str(raw_id or "").strip(), str(identity or "").strip().casefold())
            for raw_id, identity in source_result_identities
        ),
        source_result_ordinal=source_result_ordinal,
        _process_authority=_PROCESS_AUTHORITY,
    )


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate message metadata key")
        result[key] = value
    return result


def _source_search_result_page(
    message: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    encoded = message.get("metadata_json")
    if not isinstance(encoded, str) or len(encoded) > _SOURCE_SEARCH_METADATA_MAX_CHARS:
        return (), ()
    try:
        metadata = json.loads(encoded, object_pairs_hook=_closed_json_object)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return (), ()
    if not isinstance(metadata, dict):
        return (), ()
    tools_used = metadata.get("tools_used")
    if (
        metadata.get("private_context_lineage") is not True
        or type(tools_used) is not list
        or "source_search" not in tools_used
        or any(type(tool_name) is not str for tool_name in tools_used)
    ):
        return (), ()
    values = metadata.get(_SOURCE_SEARCH_RESULT_RAW_IDS)
    if type(values) is not list or not 1 <= len(values) <= _SOURCE_SEARCH_RESULT_LIMIT:
        return (), ()
    if any(type(value) is not str or _RAW_ID_RE.fullmatch(value) is None for value in values):
        return (), ()
    result = tuple(str(value) for value in values)
    if len(set(result)) != len(result):
        return (), ()
    raw_identities = metadata.get(_SOURCE_SEARCH_RESULT_IDENTITIES)
    if type(raw_identities) is not dict or set(raw_identities) != set(result):
        return (), ()
    identities: list[tuple[str, str]] = []
    for raw_id in result:
        identity = raw_identities.get(raw_id)
        if type(identity) is not str or _SHA256_RE.fullmatch(identity) is None:
            return (), ()
        identities.append((raw_id, identity))
    return result, tuple(identities)


def _latest_source_search_message(
    storage: Any,
    *,
    conversation_id: str,
    conversation_owner_id: str,
) -> Mapping[str, Any] | None:
    rows = storage.get_conversation_messages(
        conversation_id,
        user_id=conversation_owner_id,
        limit=1,
    )
    if len(rows) != 1:
        return None
    row = rows[0]
    if (
        not isinstance(row, Mapping)
        or row.get("role") != "assistant"
        or row.get("conversation_id") != conversation_id
        or row.get("user_id") != conversation_owner_id
        or _MESSAGE_ID_RE.fullmatch(str(row.get("id") or "")) is None
    ):
        return None
    return row


def source_search_result_selection_token(
    storage: Any,
    *,
    tenant_id: str,
    uploaded_by: str,
    conversation_id: str,
    ordinal: int,
) -> HistoricalFileSelectionToken | None:
    """Pin one ordinal from the immediately preceding owned search result."""

    tenant_id = str(tenant_id or "").strip()
    uploaded_by = str(uploaded_by or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if (
        not tenant_id
        or not uploaded_by
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        or type(ordinal) is not int
    ):
        return None
    row = _latest_source_search_message(
        storage,
        conversation_id=conversation_id,
        conversation_owner_id=uploaded_by,
    )
    if row is None:
        return None
    candidates, identities = _source_search_result_page(row)
    if not 1 <= ordinal <= len(candidates):
        return None
    try:
        token = historical_file_selection_token(
            tenant_id=tenant_id,
            uploaded_by=uploaded_by,
            kind="source_search_result",
            raw_ids=(candidates[ordinal - 1],),
            conversation_id=conversation_id,
            source_message_id=str(row.get("id") or ""),
            source_result_raw_ids=candidates,
            source_result_identities=identities,
            source_result_ordinal=ordinal,
        )
    except ValueError:
        return None
    return token if _historical_selection_is_current(storage, token) else None


def _historical_selection_is_current(
    storage: Any,
    token: HistoricalFileSelectionToken,
    *,
    verify_source_identities: bool = False,
) -> bool:
    if type(token) is not HistoricalFileSelectionToken or token._process_authority is not _PROCESS_AUTHORITY:
        return False
    if token.kind == "source_search_result":
        row = _latest_source_search_message(
            storage,
            conversation_id=token.conversation_id,
            conversation_owner_id=token.uploaded_by,
        )
        if (
            row is None
            or str(row.get("id") or "") != token.source_message_id
            or _source_search_result_page(row)
            != (token.source_result_raw_ids, token.source_result_identities)
        ):
            return False
        selected = storage.get_searchable_file_sources(
            token.tenant_id,
            list(token.raw_ids),
            uploaded_by=token.uploaded_by,
            limit=1,
            include_content=verify_source_identities,
        )
        if len(selected) != 1 or str(selected[0].get("id") or "") != token.raw_ids[0]:
            return False
        if not verify_source_identities:
            return True
        expected = dict(token.source_result_identities).get(token.raw_ids[0], "")
        return hmac.compare_digest(expected, raw_source_identity_sha256(selected[0]))
    if token.kind == "exact_filename":
        rows = storage.find_owned_files_by_filename(
            token.tenant_id,
            token.uploaded_by,
            token.filename,
        )
        current = tuple(str(row.get("id") or "") for row in rows) if len(rows) == 1 else ()
        return current == token.raw_ids
    selected = storage.select_owned_file_corpus(
        token.tenant_id,
        token.uploaded_by,
        received_since=token.received_since,
        received_until=token.received_until,
        document_since=token.document_since,
        document_until=token.document_until,
        limit=3,
        offset=0,
    )
    if not isinstance(selected, dict):
        return False
    rows = selected.get("items")
    total = selected.get("total")
    if (
        not isinstance(rows, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or selected.get("unattributed") != 0
        or selected.get("undated") != 0
    ):
        return False
    if token.kind == "latest":
        count = min(int(token.latest_count or 0), total)
        rows = rows[:count]
        if len(rows) != count:
            return False
    elif total > 2 or selected.get("page_complete") is not True:
        return False
    current = tuple(str(row.get("id") or "") for row in rows if isinstance(row, dict))
    return len(current) == len(rows) and current == token.raw_ids


@dataclass(frozen=True, slots=True)
class PreparedFileEvidence:
    """Private, immutable authority retained from prepare to publication."""

    tenant_id: str
    person_id: str
    raw_ids: tuple[str, ...]
    snapshot_tokens: tuple[AuthorizedFileSnapshotToken, ...]
    file_evidence_set: FileEvidenceSet
    bundle: EvidenceBundle
    historical_selection: HistoricalFileSelectionToken | None
    _process_authority: object = field(repr=False, compare=False)
    _identity_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._process_authority is not _PROCESS_AUTHORITY:
            raise ValueError("prepared file evidence is not process-owned")
        if (
            not isinstance(self.raw_ids, tuple)
            or not isinstance(self.snapshot_tokens, tuple)
            or not 1 <= len(self.raw_ids) <= _MAX_FILES
            or len(self.raw_ids) != len(self.snapshot_tokens)
            or len(set(self.raw_ids)) != len(self.raw_ids)
        ):
            raise ValueError("prepared file evidence has invalid cardinality")
        if any(
            not authorized_file_snapshot_token_authorizes_scope(
                token,
                tenant_id=self.tenant_id,
                storage_owner_id=self.tenant_id,
            )
            for token in self.snapshot_tokens
        ):
            raise ValueError("prepared file evidence has an unowned source token")
        if self.historical_selection is not None and (
            type(self.historical_selection) is not HistoricalFileSelectionToken
            or self.historical_selection._process_authority is not _PROCESS_AUTHORITY
            or self.historical_selection.tenant_id != self.tenant_id
            or self.historical_selection.uploaded_by != self.person_id
            or self.historical_selection.raw_ids != self.raw_ids
        ):
            raise ValueError("prepared historical selector disagrees with evidence")
        if not _prepared_bindings_valid(self):
            raise ValueError("prepared file evidence identities disagree")
        bundle_identity = self.bundle.identity_sha256()
        identity = (
            bundle_identity
            if self.historical_selection is None
            else hashlib.sha256(
                f"{bundle_identity}:{self.historical_selection.identity_sha256()}".encode("ascii")
            ).hexdigest()
        )
        object.__setattr__(self, "_identity_sha256", identity)

    @property
    def identity_sha256(self) -> str:
        return self._identity_sha256


def _prepared_bindings_valid(value: PreparedFileEvidence) -> bool:
    views = value.file_evidence_set.items
    parts = value.bundle.parts
    citations = value.bundle.citations
    if not (
        value.bundle.file_evidence_set_sha256 == value.file_evidence_set.identity_sha256()
        and value.file_evidence_set.verification_complete
        and len(value.raw_ids) == len(value.snapshot_tokens) == len(views) == len(parts) == len(citations)
    ):
        return False
    return all(
        raw_id == token.source.raw_id == view.raw_id
        and token.source.identity_sha256
        == view.source_identity_sha256
        == part.source_identity_sha256
        == citation.source_identity_sha256
        and part.label == citation.label == f"A{index}"
        and view.registration is FileRegistrationKind.VALID
        and view.disk_verified
        for index, (raw_id, token, view, part, citation) in enumerate(
            zip(value.raw_ids, value.snapshot_tokens, views, parts, citations, strict=True),
            start=1,
        )
    )


def prepared_file_evidence_is_process_owned(value: Any) -> bool:
    expected_identity = ""
    if type(value) is PreparedFileEvidence:
        bundle_identity = value.bundle.identity_sha256()
        expected_identity = (
            bundle_identity
            if value.historical_selection is None
            else hashlib.sha256(
                f"{bundle_identity}:{value.historical_selection.identity_sha256()}".encode("ascii")
            ).hexdigest()
        )
    return bool(
        type(value) is PreparedFileEvidence
        and value._process_authority is _PROCESS_AUTHORITY
        and value._identity_sha256 == expected_identity
        and _prepared_bindings_valid(value)
        and all(
            authorized_file_snapshot_token_authorizes_scope(
                token,
                tenant_id=value.tenant_id,
                storage_owner_id=value.tenant_id,
            )
            for token in value.snapshot_tokens
        )
    )


def historical_file_selection_is_current(storage: Any, prepared: PreparedFileEvidence) -> bool:
    """Recheck a process-owned historical selector without reading file bodies."""

    return bool(
        prepared_file_evidence_is_process_owned(prepared)
        and prepared.historical_selection is not None
        and _historical_selection_is_current(storage, prepared.historical_selection)
    )


def _fresh_actor_in_transaction(conn: Any, actor: ActorContext) -> ActorContext:
    principal = str(actor.own_id or "").strip()
    row = conn.execute(
        "SELECT preset_key, status FROM users WHERE id=?",
        (principal,),
    ).fetchone()
    if row is None or str(row["status"] or "") != "active":
        raise FileEvidenceUnavailable("principal_not_active")
    return replace(actor, preset_key=str(row["preset_key"] or "guest"))


def _require_file_read(
    conn: Any,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    source_person_id: str,
) -> ActorContext:
    fresh = _fresh_actor_in_transaction(conn, actor)
    if source_person_id != fresh.own_id:
        source = conn.execute(
            "SELECT status FROM users WHERE id=?",
            (source_person_id,),
        ).fetchone()
        if source is None or str(source["status"] or "") != "active":
            raise FileEvidenceUnavailable("source_principal_not_active")
    if not authorization.authorize_in_transaction(conn, fresh, "files.read").allowed:
        raise FileEvidenceUnavailable("files_read_denied")
    if (
        source_person_id != fresh.own_id
        and not authorization.authorize_in_transaction(
            conn,
            fresh,
            "admin.all_data.read",
        ).allowed
    ):
        raise FileEvidenceUnavailable("foreign_file_read_denied")
    return fresh


def _normalized_text_digest(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8", errors="strict")).hexdigest()


def _contains_current_secret(value: str) -> bool:
    return bool(
        redact_friday_api_tokens(value) != value
        or any(secret and secret in value for secret in named_secrets().values())
    )


def _nonnegative_exact_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _require_native_text_metadata(
    metadata: dict[str, Any],
    *,
    filename: str,
    mime_type: str,
) -> None:
    """Reject unsupported/advisory sources without reading body or registered bytes."""

    if (
        type(metadata.get("extraction_receipt_version")) is not int
        or metadata.get("extraction_receipt_version") != 1
    ):
        raise FileEvidenceUnavailable("extraction_receipt_unattested")
    if metadata.get("extraction_success") is not True:
        raise FileEvidenceUnavailable("native_extraction_failed")
    if metadata.get("extraction_error") != "":
        raise FileEvidenceUnavailable("native_extraction_error_present")
    required_false_flags = (
        "text_truncated",
        "archive_truncated",
        "source_truncated_for_parse",
        "parse_deadline_reached",
        "parse_pages_truncated",
        "vision_used",
        "vision_review_required",
        "unsupported_format",
    )
    if any(metadata.get(name) is not False for name in required_false_flags) or any(
        name in metadata for name in ("transcription", "vision")
    ):
        raise FileEvidenceUnavailable("native_extraction_incomplete_or_advisory")

    counters: dict[str, int] = {}
    for name in (
        "parse_pages_read",
        "parse_total_pages",
        "vision_pages_total",
        "vision_pages_read",
        "archive_files",
        "archive_files_read",
    ):
        value = _nonnegative_exact_int(metadata.get(name))
        if value is None:
            raise FileEvidenceUnavailable("extraction_receipt_counter_invalid")
        counters[name] = value
    if counters["vision_pages_total"] or counters["vision_pages_read"]:
        raise FileEvidenceUnavailable("native_extraction_incomplete_or_advisory")
    if counters["archive_files"] or counters["archive_files_read"]:
        raise FileEvidenceUnavailable("archive_source_not_in_file_canary")
    suffix = Path(filename.casefold()).suffix
    if mime_type.casefold() not in _NATIVE_TEXT_MIME_TYPES or suffix not in _NATIVE_TEXT_SUFFIXES:
        raise FileEvidenceUnavailable("source_kind_not_in_native_text_canary")
    if counters["parse_pages_read"] != 0 or counters["parse_total_pages"] != 0:
        raise FileEvidenceUnavailable("native_text_page_counters_invalid")

    extraction_chars = _nonnegative_exact_int(metadata.get("extraction_chars"))
    if extraction_chars is None:
        raise FileEvidenceUnavailable("native_extraction_length_mismatch")
    if extraction_chars == 0:
        raise FileEvidenceUnavailable("native_text_unavailable")
    expected_text_digest = metadata.get("text_sha256")
    if (
        not isinstance(expected_text_digest, str)
        or _SHA256_RE.fullmatch(expected_text_digest.casefold()) is None
    ):
        raise FileEvidenceUnavailable("native_extraction_digest_mismatch")
    if metadata.get("text_extraction_success") is not True:
        raise FileEvidenceUnavailable("native_text_not_attested")


def _complete_native_body(
    raw_text: str,
    source_bytes: bytes,
    metadata: dict[str, Any],
    *,
    filename: str,
    mime_type: str,
) -> tuple[FileBodyKind, str]:
    """Return only complete extractor truth; every advisory/partial shape closes."""

    _require_native_text_metadata(metadata, filename=filename, mime_type=mime_type)
    try:
        raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise FileEvidenceUnavailable("body_not_utf8") from exc
    try:
        decoded_source = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FileEvidenceUnavailable("registered_bytes_are_not_exact_utf8_text") from exc
    if (
        any(source_bytes.startswith(magic) for magic in _BINARY_MAGICS)
        or any(byte < 32 and byte not in {9, 10, 13} for byte in source_bytes)
        or decoded_source != raw_text
    ):
        raise FileEvidenceUnavailable("registered_bytes_are_not_exact_utf8_text")

    extraction_chars = _nonnegative_exact_int(metadata.get("extraction_chars"))
    if extraction_chars is None or extraction_chars != len(raw_text):
        raise FileEvidenceUnavailable("native_extraction_length_mismatch")
    expected_text_digest = metadata.get("text_sha256")
    if not isinstance(expected_text_digest, str) or not hmac.compare_digest(
        expected_text_digest.casefold(),
        _normalized_text_digest(raw_text),
    ):
        raise FileEvidenceUnavailable("native_extraction_digest_mismatch")

    if not raw_text.strip():
        raise FileEvidenceUnavailable("native_text_unavailable")
    if _PROVENANCE_STUB_RE.search(raw_text):
        raise FileEvidenceUnavailable("body_is_provenance_stub")
    if _contains_current_secret(raw_text):
        # The first canary does not claim that a redacted projection is the
        # complete document. Legacy already owns that distinct contract.
        raise FileEvidenceUnavailable("body_requires_secret_projection")
    if len(raw_text) > _MAX_PART_CHARS:
        raise FileEvidenceUnavailable("body_exceeds_canary_projection")
    return FileBodyKind.EXTRACTED, raw_text


def _prepare_registered_file_evidence(
    storage: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    references: Sequence[CurrentTurnFileReference] | None,
    *,
    person_id: str,
    historical_selection: HistoricalFileSelectionToken | None,
    max_bytes: int,
    absolute_deadline: float | None = None,
) -> PreparedFileEvidence:
    """Assemble one all-or-none registered bundle under one authorization barrier."""

    if historical_selection is None:
        refs: tuple[CurrentTurnFileReference, ...] = tuple(references or ())
        raw_ids = tuple(str(item.raw_object_id or "") for item in refs)
    else:
        if references is not None:
            raise FileEvidenceUnavailable("historical_reference_authority_invalid")
        refs = ()
        raw_ids = historical_selection.raw_ids
    if not 1 <= len(raw_ids) <= _MAX_FILES:
        raise FileEvidenceUnavailable("file_count_outside_canary")
    if (
        len(set(raw_ids)) != len(raw_ids)
        or any(_RAW_ID_RE.fullmatch(raw_id) is None for raw_id in raw_ids)
        or (
            historical_selection is None
            and any(item.ordinal != index for index, item in enumerate(refs, start=1))
        )
    ):
        raise FileEvidenceUnavailable("file_reference_shape_invalid")

    views: list[FileEvidenceView] = []
    parts: list[EvidencePart] = []
    bindings: list[CitationBinding] = []
    tokens: list[AuthorizedFileSnapshotToken] = []
    total_chars = 0
    tenant_id = str(actor.user_id or "").strip()
    person_id = str(person_id or "").strip()
    if not tenant_id or not person_id:
        raise FileEvidenceUnavailable("actor_identity_missing")

    def require_budget() -> None:
        if absolute_deadline is not None and absolute_deadline <= time.monotonic():
            raise TimeoutError("file evidence preparation deadline expired")

    require_budget()
    transaction_context = (
        storage.transaction()
        if absolute_deadline is None
        else guarded_storage_transaction(
            storage,
            before_commit=require_budget,
            lock_timeout_sec=max(0.0, absolute_deadline - time.monotonic()),
        )
    )
    with transaction_context as conn:
        _require_file_read(
            conn,
            authorization,
            actor,
            source_person_id=person_id,
        )
        if historical_selection is not None and not _historical_selection_is_current(
            storage,
            historical_selection,
            verify_source_identities=True,
        ):
            raise FileEvidenceUnavailable("historical_selector_changed")
        if historical_selection is not None:
            # Historical selection may expose opaque ids before authorization,
            # but neither canonical Raw bodies nor their private identities may
            # cross into this process until the fresh principal/capability and
            # exact selector have both been re-proved in this same transaction.
            descriptors = storage.get_raw_object_descriptors(
                list(raw_ids),
                tenant_id,
                limit=len(raw_ids),
            )
            if (
                len(descriptors) != len(raw_ids)
                or tuple(str(row.get("id") or "") for row in descriptors) != raw_ids
            ):
                raise FileEvidenceUnavailable("historical_source_set_changed")
            for descriptor in descriptors:
                metadata = bounded_raw_file_metadata(descriptor.get("metadata_json"))
                if not metadata:
                    raise FileEvidenceUnavailable("raw_metadata_invalid")
                filename = str(metadata.get("filename") or "")
                mime_type = str(metadata.get("mime_type") or "")
                if _contains_current_secret(filename) or _contains_current_secret(mime_type):
                    raise FileEvidenceUnavailable("source_descriptor_requires_secret_projection")
                _require_native_text_metadata(
                    metadata,
                    filename=filename,
                    mime_type=mime_type,
                )
            rows = storage.get_searchable_file_sources(
                tenant_id,
                list(raw_ids),
                uploaded_by=person_id,
                limit=len(raw_ids),
                include_content=True,
            )
            if len(rows) != len(raw_ids) or tuple(str(row.get("id") or "") for row in rows) != raw_ids:
                raise FileEvidenceUnavailable("historical_source_set_changed")
            refs = tuple(
                _HistoricalSourceReference(
                    ordinal=index,
                    raw_object_id=raw_id,
                    source_identity_sha256=raw_source_identity_sha256(row),
                )
                for index, (raw_id, row) in enumerate(zip(raw_ids, rows, strict=True), start=1)
            )
        for index, reference in enumerate(refs, start=1):
            require_budget()
            requested_identity = str(reference.source_identity_sha256 or "").casefold()
            if _SHA256_RE.fullmatch(requested_identity) is None:
                raise FileEvidenceUnavailable("source_identity_invalid")
            try:
                stored = read_authorized_file_in_transaction(
                    conn,
                    files_root,
                    reference.raw_object_id,
                    tenant_id,
                    person_id=person_id,
                    max_bytes=max_bytes,
                )
            except (AuthorizedFileReadError, FileRecordUnavailable, OSError, ValueError) as exc:
                raise FileEvidenceUnavailable("registered_file_unavailable") from exc
            token = stored.snapshot_token
            require_budget()
            if (
                type(token) is not AuthorizedFileSnapshotToken
                or not authorized_file_snapshot_token_authorizes_scope(
                    token,
                    tenant_id=tenant_id,
                    storage_owner_id=tenant_id,
                )
                or token.source.raw_id != reference.raw_object_id
                or not hmac.compare_digest(token.source.identity_sha256, requested_identity)
            ):
                raise FileEvidenceUnavailable("current_turn_source_changed")

            row = conn.execute(
                """SELECT raw_content, metadata_json FROM raw_objects
                     WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL""",
                (reference.raw_object_id, tenant_id),
            ).fetchone()
            if row is None:
                raise FileEvidenceUnavailable("raw_row_disappeared")
            metadata = bounded_raw_file_metadata(row["metadata_json"])
            if not metadata:
                raise FileEvidenceUnavailable("raw_metadata_invalid")
            filename = str(metadata.get("filename") or stored.filename)
            mime_type = str(metadata.get("mime_type") or stored.mime_type)
            if filename != stored.filename or mime_type != stored.mime_type:
                raise FileEvidenceUnavailable("registered_projection_changed")
            if _contains_current_secret(filename) or _contains_current_secret(mime_type):
                raise FileEvidenceUnavailable("source_descriptor_requires_secret_projection")
            body_kind, text = _complete_native_body(
                str(row["raw_content"] or ""),
                stored.content,
                metadata,
                filename=filename,
                mime_type=mime_type,
            )
            require_budget()
            total_chars += len(text)
            if total_chars > _MAX_TOTAL_CHARS:
                raise FileEvidenceUnavailable("bundle_exceeds_canary_projection")

            source_identity = token.source.identity_sha256
            view = FileEvidenceView(
                raw_id=reference.raw_object_id,
                source_identity_sha256=source_identity,
                registration=FileRegistrationKind.VALID,
                disk_verified=True,
                workspace_relative_path=None,
                workspace_sha256=None,
                workspace_source_sha256=None,
                body_kind=body_kind,
                source_complete=True,
                projection_applied=False,
                projection_empty_no_match=False,
                source_readable=True,
                verification_eligible=True,
            )
            label = f"A{index}"
            views.append(view)
            parts.append(
                EvidencePart(
                    label=label,
                    display_name=filename[:180],
                    media_type=mime_type[:120],
                    source_identity_sha256=source_identity,
                    text=text,
                )
            )
            bindings.append(CitationBinding(label=label, source_identity_sha256=source_identity))
            tokens.append(token)

        evidence_set = FileEvidenceSet(items=tuple(views), expected_count=len(refs))
        if not evidence_set.verification_complete:
            raise FileEvidenceUnavailable("file_evidence_set_incomplete")
        bundle = EvidenceBundle(
            parts=tuple(parts),
            citations=tuple(bindings),
            file_evidence_set_sha256=evidence_set.identity_sha256(),
        )
        return PreparedFileEvidence(
            tenant_id=tenant_id,
            person_id=person_id,
            raw_ids=raw_ids,
            snapshot_tokens=tuple(tokens),
            file_evidence_set=evidence_set,
            bundle=bundle,
            historical_selection=historical_selection,
            _process_authority=_PROCESS_AUTHORITY,
        )


def prepare_current_turn_file_evidence(
    storage: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    references: Sequence[CurrentTurnFileReference],
    *,
    max_bytes: int,
    absolute_deadline: float | None = None,
) -> PreparedFileEvidence:
    """Assemble current-turn evidence for the authenticated uploader."""

    return _prepare_registered_file_evidence(
        storage,
        authorization,
        files_root,
        actor,
        references,
        person_id=str(actor.own_id or "").strip(),
        historical_selection=None,
        max_bytes=max_bytes,
        absolute_deadline=absolute_deadline,
    )


def prepare_registered_file_evidence(
    storage: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    *,
    uploaded_by: str,
    selection: HistoricalFileSelectionToken,
    max_bytes: int,
    absolute_deadline: float | None = None,
) -> PreparedFileEvidence:
    """Assemble selected historical evidence under exact uploader authority."""

    return _prepare_registered_file_evidence(
        storage,
        authorization,
        files_root,
        actor,
        None,
        person_id=str(uploaded_by or "").strip(),
        historical_selection=selection,
        max_bytes=max_bytes,
        absolute_deadline=absolute_deadline,
    )


def prepare_pinned_file_evidence(
    storage: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    *,
    uploaded_by: str,
    reference: PinnedFileEvidenceReference,
    max_bytes: int,
    absolute_deadline: float | None = None,
) -> PreparedFileEvidence:
    """Reprepare one durable Raw pin after restart under fresh file authority."""

    if type(reference) is not PinnedFileEvidenceReference:
        raise FileEvidenceUnavailable("pinned_reference_invalid")
    prepared = _prepare_registered_file_evidence(
        storage,
        authorization,
        files_root,
        actor,
        (
            _HistoricalSourceReference(
                ordinal=1,
                raw_object_id=reference.raw_object_id,
                source_identity_sha256=reference.source_identity_sha256,
            ),
        ),
        person_id=str(uploaded_by or "").strip(),
        historical_selection=None,
        max_bytes=max_bytes,
        absolute_deadline=absolute_deadline,
    )
    token = prepared.snapshot_tokens[0]
    if (
        prepared.raw_ids != (reference.raw_object_id,)
        or not hmac.compare_digest(
            token.source.identity_sha256,
            reference.source_identity_sha256,
        )
        or not hmac.compare_digest(token.content_sha256, reference.content_sha256)
    ):
        raise FileEvidenceUnavailable("pinned_source_changed")
    return prepared


def reauthorize_prepared_file_evidence_in_transaction(
    conn: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    prepared: PreparedFileEvidence,
    *,
    max_bytes: int,
    storage: Any | None = None,
) -> bool:
    """Re-prove every source immediately before one assistant publication."""

    if (
        not prepared_file_evidence_is_process_owned(prepared)
        or prepared.tenant_id != str(actor.user_id or "").strip()
    ):
        return False
    try:
        _require_file_read(
            conn,
            authorization,
            actor,
            source_person_id=prepared.person_id,
        )
        if prepared.historical_selection is not None and (
            storage is None
            or not _historical_selection_is_current(
                storage,
                prepared.historical_selection,
                verify_source_identities=True,
            )
        ):
            return False
        for raw_id, original in zip(prepared.raw_ids, prepared.snapshot_tokens, strict=True):
            current = read_authorized_file_in_transaction(
                conn,
                files_root,
                raw_id,
                prepared.tenant_id,
                person_id=prepared.person_id,
                max_bytes=max_bytes,
            )
            token = current.snapshot_token
            if (
                type(token) is not AuthorizedFileSnapshotToken
                or not authorized_file_snapshot_token_authorizes_scope(
                    token,
                    tenant_id=prepared.tenant_id,
                    storage_owner_id=prepared.tenant_id,
                )
                or token.source.raw_id != original.source.raw_id
                or not hmac.compare_digest(token.source.identity_sha256, original.source.identity_sha256)
                or not hmac.compare_digest(token.content_sha256, original.content_sha256)
            ):
                return False
    except (AuthorizedFileReadError, FileRecordUnavailable, FileEvidenceUnavailable, OSError, ValueError):
        return False
    return True


__all__ = [
    "FileEvidenceUnavailable",
    "HistoricalFileSelectionToken",
    "PinnedFileEvidenceReference",
    "PreparedFileEvidence",
    "prepare_current_turn_file_evidence",
    "prepare_pinned_file_evidence",
    "prepare_registered_file_evidence",
    "historical_file_selection_token",
    "historical_file_selection_is_current",
    "source_search_result_selection_token",
    "prepared_file_evidence_is_process_owned",
    "reauthorize_prepared_file_evidence_in_transaction",
]
