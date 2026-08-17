"""Fail-closed assembly of current-turn file evidence.

The public request carries only opaque Raw identifiers.  This module is the
single bridge from those identifiers to model-visible text: it reauthorizes the
exact tenant and uploader, verifies the registered bytes under one SQLite write
barrier, and binds the immutable Raw row to a process-owned snapshot token.

The first V12 canary intentionally accepts only complete native extraction.
OCR, speech/vision output, partial parsing, historical/reply references and
oversized documents remain legacy-owned until their separate evidence
contracts are implemented.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Sequence
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
    authorized_file_snapshot_token_is_process_owned,
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


class FileEvidenceUnavailable(Exception):
    """The current source cannot enter the strict V12 file canary."""


@dataclass(frozen=True, slots=True)
class PreparedFileEvidence:
    """Private, immutable authority retained from prepare to publication."""

    tenant_id: str
    person_id: str
    raw_ids: tuple[str, ...]
    snapshot_tokens: tuple[AuthorizedFileSnapshotToken, ...]
    file_evidence_set: FileEvidenceSet
    bundle: EvidenceBundle
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
        if any(not authorized_file_snapshot_token_is_process_owned(token) for token in self.snapshot_tokens):
            raise ValueError("prepared file evidence has an unowned source token")
        if not _prepared_bindings_valid(self):
            raise ValueError("prepared file evidence identities disagree")
        object.__setattr__(self, "_identity_sha256", self.bundle.identity_sha256())

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
    return bool(
        type(value) is PreparedFileEvidence
        and value._process_authority is _PROCESS_AUTHORITY
        and value._identity_sha256 == value.bundle.identity_sha256()
        and _prepared_bindings_valid(value)
        and all(authorized_file_snapshot_token_is_process_owned(token) for token in value.snapshot_tokens)
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
) -> ActorContext:
    fresh = _fresh_actor_in_transaction(conn, actor)
    if not authorization.authorize(fresh, "files.read").allowed:
        raise FileEvidenceUnavailable("files_read_denied")
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


def _complete_native_body(
    raw_text: str,
    source_bytes: bytes,
    metadata: dict[str, Any],
    *,
    filename: str,
    mime_type: str,
) -> tuple[FileBodyKind, str]:
    """Return only complete extractor truth; every advisory/partial shape closes."""

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
    if metadata.get("text_extraction_success") is not True:
        raise FileEvidenceUnavailable("native_text_not_attested")
    if _PROVENANCE_STUB_RE.search(raw_text):
        raise FileEvidenceUnavailable("body_is_provenance_stub")
    if _contains_current_secret(raw_text):
        # The first canary does not claim that a redacted projection is the
        # complete document. Legacy already owns that distinct contract.
        raise FileEvidenceUnavailable("body_requires_secret_projection")
    if len(raw_text) > _MAX_PART_CHARS:
        raise FileEvidenceUnavailable("body_exceeds_canary_projection")
    return FileBodyKind.EXTRACTED, raw_text


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
    """Assemble one all-or-none bundle under a single authorization barrier."""

    refs = tuple(references)
    if not 1 <= len(refs) <= _MAX_FILES:
        raise FileEvidenceUnavailable("file_count_outside_canary")
    raw_ids = tuple(str(item.raw_object_id or "") for item in refs)
    if (
        len(set(raw_ids)) != len(raw_ids)
        or any(_RAW_ID_RE.fullmatch(raw_id) is None for raw_id in raw_ids)
        or any(item.ordinal != index for index, item in enumerate(refs, start=1))
    ):
        raise FileEvidenceUnavailable("file_reference_shape_invalid")

    views: list[FileEvidenceView] = []
    parts: list[EvidencePart] = []
    bindings: list[CitationBinding] = []
    tokens: list[AuthorizedFileSnapshotToken] = []
    total_chars = 0
    tenant_id = str(actor.user_id or "").strip()
    person_id = str(actor.own_id or "").strip()
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
        _require_file_read(conn, authorization, actor)
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
                or not authorized_file_snapshot_token_is_process_owned(token)
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
            _process_authority=_PROCESS_AUTHORITY,
        )


def reauthorize_prepared_file_evidence_in_transaction(
    conn: Any,
    authorization: AuthorizationService,
    files_root: Path,
    actor: ActorContext,
    prepared: PreparedFileEvidence,
    *,
    max_bytes: int,
) -> bool:
    """Re-prove every source immediately before one assistant publication."""

    if (
        not prepared_file_evidence_is_process_owned(prepared)
        or prepared.tenant_id != str(actor.user_id or "").strip()
        or prepared.person_id != str(actor.own_id or "").strip()
    ):
        return False
    try:
        _require_file_read(conn, authorization, actor)
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
                or not authorized_file_snapshot_token_is_process_owned(token)
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
    "PreparedFileEvidence",
    "prepare_current_turn_file_evidence",
    "prepared_file_evidence_is_process_owned",
    "reauthorize_prepared_file_evidence_in_transaction",
]
