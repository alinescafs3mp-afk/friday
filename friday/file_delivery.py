"""Privacy-safe reads of stored upload bytes.

The database row is the authorization authority; ``stored_path`` is only a
location.  Callers must therefore acquire the current row and consume the file
under one write-blocking SQLite transaction.  Returning a path for later
streaming creates a quarantine race: the row can become private after the check
while a ``FileResponse`` still reads and sends the old path.

Registration is three-state:

* ``legacy_unregistered`` — no modern disk-registration fields at all
* ``registered_valid`` — path/size/hash/content_hash agree and the bytes match
* ``registered_invalid`` — modern fields are present but incomplete or inconsistent

``registered_invalid`` must never be silently treated as legacy, and must never
authorize a read of the cached ``raw_content`` body as if it were verified disk
bytes.  That decision belongs to higher layers; this module only refuses the
disk path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from friday.permissions import ActorContext, AuthorizationService
from friday.source_identity import (
    AuthorizedFileSnapshotToken,
    authorized_file_snapshot_token,
    authorized_file_snapshot_token_is_process_owned,
)
from friday.storage._privacy import (
    _exact_uploader_raw_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
    _not_private_raw_material_dependency,
)

_READ_CHUNK_BYTES = 1024 * 1024
_MAX_STORED_PATH_CHARS = 16_384
_MAX_DOWNLOAD_FILENAME_CHARS = 255
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_OPAQUE_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_TELEGRAM_UPDATE_ID = re.compile(r"^[0-9]{1,20}$")
_MAX_CURRENT_MESSAGE_UPLOADS = 100
_MESSAGE_SOURCE_IDENTITY_FIELDS = (
    "id",
    "conversation_id",
    "user_id",
    "role",
    "content",
    "metadata_json",
    "reply_to",
    "created_at",
)

LEGACY_UNREGISTERED = "legacy_unregistered"
REGISTERED_VALID = "registered_valid"
REGISTERED_INVALID = "registered_invalid"

_MODERN_REGISTRATION_KEYS = frozenset({"stored_path", "sha256", "size_bytes"})


class FileRecordUnavailable(Exception):
    """The current database state does not authorize this stored file."""


class AuthorizedFileReadError(Exception):
    """An authorized row exists, but its bytes cannot safely be consumed."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(reason)
        self.filename = filename
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthorizedFileBytes:
    raw_id: str
    filename: str
    mime_type: str
    content: bytes
    snapshot_token: AuthorizedFileSnapshotToken | None = None


@dataclass(frozen=True, slots=True)
class AuthorizedCurrentUpload:
    """Exact same-turn upload authority, including its immutable Raw row."""

    raw: Mapping[str, Any]
    file: AuthorizedFileBytes


@dataclass(frozen=True, slots=True)
class CurrentMessageUploadFileIdentity:
    """Path-free identity of one exact current-message upload."""

    raw_id: str
    source_identity_sha256: str
    content_sha256: str
    size_bytes: int
    filename: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class CurrentMessageUploadBatchIdentity:
    """Durable values which any later execution must match exactly."""

    source_message_id: str
    conversation_id: str
    source_message_identity_sha256: str
    telegram_update_id: str
    uploaded_raw_ids: tuple[str, ...]
    files: tuple[CurrentMessageUploadFileIdentity, ...]


@dataclass(frozen=True, slots=True)
class AuthorizedCurrentMessageUploadBatch:
    """Exact immutable bytes and identities from one SQLite snapshot."""

    identity: CurrentMessageUploadBatchIdentity
    files: tuple[AuthorizedFileBytes, ...]


@dataclass(frozen=True, slots=True)
class FileRegistrationVerdict:
    """Closed classification of one Raw file registration.

    ``reason`` is a stable machine code without paths, names, or content.
    """

    state: str
    reason: str


def attachment_content_disposition(filename: str) -> str:
    """Build a bounded header without trusting stored control characters."""

    safe = _safe_filename(filename)
    ascii_name = "".join(char if 32 <= ord(char) < 127 else "_" for char in safe)
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


def classify_file_registration(
    metadata: Mapping[str, Any] | None,
    *,
    content_hash: str | None,
) -> FileRegistrationVerdict:
    """Classify registration fields without touching the filesystem.

    Disk agreement is a separate step (``verify_registered_file_bytes`` /
    ``read_authorized_file``).  A row that only has modern fields is already
    either valid-shape or invalid — never legacy.
    """

    meta = metadata if isinstance(metadata, Mapping) else {}
    modern_present = any(key in meta for key in _MODERN_REGISTRATION_KEYS)
    if not modern_present:
        return FileRegistrationVerdict(LEGACY_UNREGISTERED, "no_disk_registration_fields")

    stored_path = meta.get("stored_path")
    if not isinstance(stored_path, str) or not stored_path or len(stored_path) > _MAX_STORED_PATH_CHARS:
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_missing_or_unbounded")
    if "\x00" in stored_path or stored_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", stored_path):
        # Absolute host paths are historical machine coupling.  They are not a
        # modern relative registration even when they still happen to resolve.
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_not_relative")
    if "\\" in stored_path or ".." in Path(stored_path).parts:
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_unsafe")

    digest = meta.get("sha256")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return FileRegistrationVerdict(REGISTERED_INVALID, "sha256_missing_or_malformed")
    digest = digest.casefold()

    raw_hash = str(content_hash or "").strip().casefold()
    if not raw_hash or not _HEX64.fullmatch(raw_hash):
        return FileRegistrationVerdict(REGISTERED_INVALID, "content_hash_missing_or_malformed")
    if not hmac.compare_digest(raw_hash, digest):
        return FileRegistrationVerdict(REGISTERED_INVALID, "content_hash_sha256_mismatch")

    if "size_bytes" not in meta:
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_missing")
    size_bytes = meta.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_invalid")

    return FileRegistrationVerdict(REGISTERED_VALID, "registration_fields_consistent")


def verify_registered_file_bytes(
    root: Path,
    metadata: Mapping[str, Any] | None,
    *,
    content_hash: str | None,
    max_bytes: int | None = None,
) -> FileRegistrationVerdict:
    """Classify and prove disk bytes for one registration without authorization.

    Callers that already authorized a Raw row use ``read_authorized_file``.
    This helper is for audit and ingestion repair checks: it never prints
    paths or content, only a closed verdict.
    """

    verdict = classify_file_registration(metadata, content_hash=content_hash)
    if verdict.state != REGISTERED_VALID:
        return verdict
    meta = metadata if isinstance(metadata, Mapping) else {}
    digest = str(meta.get("sha256") or "").casefold()
    stored_path = str(meta.get("stored_path") or "")
    try:
        content = _read_regular_file(
            root,
            stored_path,
            max_bytes=max_bytes,
            expected_sha256=digest,
        )
    except _FileTooLarge:
        return FileRegistrationVerdict(REGISTERED_INVALID, "file_too_large")
    except (OSError, ValueError):
        return FileRegistrationVerdict(REGISTERED_INVALID, "disk_bytes_unreadable_or_mismatched")
    size_bytes = meta.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes != len(content)
    ):
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_mismatch")
    return FileRegistrationVerdict(REGISTERED_VALID, "disk_bytes_verified")


def read_authorized_file(
    storage: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    person_id: str | None = None,
    include_deleted: bool = False,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Revalidate and read one file at a single privacy linearization point.

    ``transaction()`` uses ``BEGIN IMMEDIATE``.  A concurrent quarantine writer
    either commits before this query (and the row is refused) or waits until all
    bytes have been consumed.  No filesystem path survives the transaction for
    a response object to read later.
    """

    with storage.transaction() as conn:
        return read_authorized_file_in_transaction(
            conn,
            root,
            raw_id,
            user_id,
            person_id=person_id,
            include_deleted=include_deleted,
            max_bytes=max_bytes,
        )


def read_authorized_file_in_transaction(
    conn: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    person_id: str | None = None,
    include_deleted: bool = False,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Transaction-scoped implementation used to assemble an atomic archive."""

    deleted_clause = "" if include_deleted else " AND r.deleted_at IS NULL"
    person_clause = f" AND {_exact_uploader_raw_dependency('r')}" if person_id is not None else ""
    parameters: tuple[str, ...] = (
        (str(raw_id), str(user_id), str(person_id)) if person_id is not None else (str(raw_id), str(user_id))
    )
    row = conn.execute(
        f"""SELECT r.id, r.user_id, r.source, r.source_ref, r.content_type,
                   r.received_at, r.content_hash, r.raw_content AS _raw_content,
                   r.metadata_json, r.metadata_json AS _raw_metadata
              FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               {deleted_clause}{person_clause}
               AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
        parameters,
    ).fetchone()
    if row is None:
        raise FileRecordUnavailable

    stored = _read_authorized_row(row, root, max_bytes=max_bytes)

    # Same IMMEDIATE writer lock already serializes quarantine, but re-check the
    # authorization predicate after the bytes leave the descriptor so a row that
    # was mutated mid-read (or a corrupted SELECT projection) cannot ship.
    still = conn.execute(
        f"""SELECT r.id FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               {deleted_clause}{person_clause}
               AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
        parameters,
    ).fetchone()
    if still is None:
        raise FileRecordUnavailable
    return stored


def _current_upload_row(
    conn: Any,
    *,
    raw_id: str,
    tenant_id: str,
    person_id: str,
) -> Any:
    """Resolve one uploader-owned Raw without granting generic archive recall.

    Pending Inbox material is intentionally absent from ordinary Raw readers.
    This narrower reader exists only for a process-authenticated current upload
    (or the exact durable user-message pointer minted from that upload).
    """

    visible_knowledge = _not_private_knowledge_dependency("current_upload_knowledge")
    not_quarantined = f"""(
        {_not_private_raw_material_dependency("r")}
        AND NOT EXISTS (
            SELECT 1 FROM knowledge_objects current_upload_knowledge
             WHERE current_upload_knowledge.raw_object_id=r.id
               AND current_upload_knowledge.user_id=r.user_id
               AND NOT ({visible_knowledge})
        )
    )"""
    return conn.execute(
        f"""SELECT r.id, r.user_id, r.source, r.source_ref, r.content_type,
                   r.received_at, r.content_hash, r.raw_content,
                   r.raw_content AS _raw_content, r.metadata_json,
                   r.metadata_json AS _raw_metadata
              FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.source='upload'
               AND r.content_type='file' AND r.deleted_at IS NULL
               AND {_exact_uploader_raw_dependency("r")}
               AND {not_quarantined}""",  # nosec B608 - fixed predicates
        (str(raw_id), str(tenant_id), str(person_id)),
    ).fetchone()


def authorize_current_upload_file(
    storage: Any,
    root: Path,
    raw_id: str,
    tenant_id: str,
    *,
    person_id: str,
    expected_sha256: str,
    max_bytes: int | None = None,
) -> AuthorizedCurrentUpload:
    """Authorize bytes returned by this process's just-completed upload call.

    ``expected_sha256`` is computed from the authenticated request body, not
    accepted from a model or API field. It prevents an opaque-id mix-up from
    opening another quarantined Raw object through this deliberately narrow
    same-turn exception.
    """

    with storage.transaction() as conn:
        return authorize_current_upload_file_in_transaction(
            conn,
            root,
            raw_id,
            tenant_id,
            person_id=person_id,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )


def authorize_current_upload_file_in_transaction(
    conn: Any,
    root: Path,
    raw_id: str,
    tenant_id: str,
    *,
    person_id: str,
    expected_sha256: str,
    max_bytes: int | None = None,
) -> AuthorizedCurrentUpload:
    """Transaction-scoped same-turn upload authorization."""

    digest = str(expected_sha256 or "").casefold()
    if not _HEX64.fullmatch(digest):
        raise FileRecordUnavailable
    row = _current_upload_row(
        conn,
        raw_id=raw_id,
        tenant_id=tenant_id,
        person_id=person_id,
    )
    if row is None or not hmac.compare_digest(str(row["content_hash"] or "").casefold(), digest):
        raise FileRecordUnavailable
    stored = _read_authorized_row(row, root, max_bytes=max_bytes)
    if not hmac.compare_digest(hashlib.sha256(stored.content).hexdigest(), digest):
        raise FileRecordUnavailable
    current = _current_upload_row(
        conn,
        raw_id=raw_id,
        tenant_id=tenant_id,
        person_id=person_id,
    )
    if current is None or not hmac.compare_digest(
        str(current["content_hash"] or "").casefold(),
        digest,
    ):
        raise FileRecordUnavailable
    return AuthorizedCurrentUpload(raw=dict(current), file=stored)


def read_current_message_upload_file(
    storage: Any,
    root: Path,
    raw_id: str,
    tenant_id: str,
    *,
    person_id: str,
    conversation_id: str,
    source_message_id: str,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Read the exact upload recorded on one authenticated user message.

    This is the restart-safe continuation of ``authorize_current_upload_file``.
    It bypasses generic archive visibility only when the immutable message row
    itself names the Raw id as a current upload for the exact participant.
    """

    with storage.transaction() as conn:
        message = conn.execute(
            """SELECT metadata_json FROM messages
                WHERE id=? AND conversation_id=? AND user_id IN (?, ?) AND role='user'""",
            (
                str(source_message_id),
                str(conversation_id),
                str(person_id),
                str(tenant_id),
            ),
        ).fetchone()
        if message is None:
            raise FileRecordUnavailable
        metadata = _metadata_object(message["metadata_json"])
        uploaded = metadata.get("conversation_uploaded_raw_ids")
        if (
            not isinstance(uploaded, list)
            or len(uploaded) > 100
            or uploaded.count(str(raw_id)) != 1
            or any(not isinstance(item, str) for item in uploaded)
        ):
            raise FileRecordUnavailable
        row = _current_upload_row(
            conn,
            raw_id=raw_id,
            tenant_id=tenant_id,
            person_id=person_id,
        )
        if row is None:
            raise FileRecordUnavailable
        stored = _read_authorized_row(row, root, max_bytes=max_bytes)
        current = _current_upload_row(
            conn,
            raw_id=raw_id,
            tenant_id=tenant_id,
            person_id=person_id,
        )
        if current is None or str(current["content_hash"] or "") != str(row["content_hash"] or ""):
            raise FileRecordUnavailable
        return stored


def authorize_current_message_upload_batch(
    storage: Any,
    root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    conversation_id: str,
    source_message_id: str,
    telegram_update_id: str,
    uploaded_raw_ids: Sequence[str],
    max_bytes_per_file: int | None = None,
) -> AuthorizedCurrentMessageUploadBatch:
    """Read only the exact uploads recorded on one current Telegram user row.

    The durable user-message row, active principal, files.read decision, Raw
    identities and registered bytes are all observed under one BEGIN IMMEDIATE
    transaction. conversation_attachment_raw_ids is deliberately never an
    authority: quoted, restored and ambient pointers cannot enter this batch.
    """

    with storage.transaction() as conn:
        return authorize_current_message_upload_batch_in_transaction(
            conn,
            root,
            authorization,
            actor,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            telegram_update_id=telegram_update_id,
            uploaded_raw_ids=uploaded_raw_ids,
            max_bytes_per_file=max_bytes_per_file,
        )


def authorize_current_message_upload_batch_in_transaction(
    conn: Any,
    root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    conversation_id: str,
    source_message_id: str,
    telegram_update_id: str,
    uploaded_raw_ids: Sequence[str],
    max_bytes_per_file: int | None = None,
) -> AuthorizedCurrentMessageUploadBatch:
    """Transaction-scoped first read of an exact current-message upload batch."""

    raw_ids = _freeze_current_upload_raw_ids(uploaded_raw_ids)
    update_id = _canonical_telegram_update_id(telegram_update_id)
    return _authorize_current_message_upload_batch_in_transaction(
        conn,
        root,
        authorization,
        actor,
        conversation_id=_bounded_identity(conversation_id),
        source_message_id=_bounded_identity(source_message_id),
        telegram_update_id=update_id,
        uploaded_raw_ids=raw_ids,
        expected_identity=None,
        max_bytes_per_file=max_bytes_per_file,
    )


def reauthorize_current_message_upload_batch(
    storage: Any,
    root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    expected: CurrentMessageUploadBatchIdentity,
    max_bytes_per_file: int | None = None,
) -> AuthorizedCurrentMessageUploadBatch:
    """Re-read a previously frozen batch and reject any identity drift."""

    with storage.transaction() as conn:
        return reauthorize_current_message_upload_batch_in_transaction(
            conn,
            root,
            authorization,
            actor,
            expected=expected,
            max_bytes_per_file=max_bytes_per_file,
        )


def reauthorize_current_message_upload_batch_in_transaction(
    conn: Any,
    root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    expected: CurrentMessageUploadBatchIdentity,
    max_bytes_per_file: int | None = None,
) -> AuthorizedCurrentMessageUploadBatch:
    """Transaction-scoped exact reauthorization for deferred execution."""

    if type(expected) is not CurrentMessageUploadBatchIdentity:
        raise FileRecordUnavailable
    raw_ids = _freeze_current_upload_raw_ids(expected.uploaded_raw_ids)
    if (
        type(expected.files) is not tuple
        or len(expected.files) != len(raw_ids)
        or any(type(item) is not CurrentMessageUploadFileIdentity for item in expected.files)
        or tuple(item.raw_id for item in expected.files) != raw_ids
    ):
        raise FileRecordUnavailable
    return _authorize_current_message_upload_batch_in_transaction(
        conn,
        root,
        authorization,
        actor,
        conversation_id=_bounded_identity(expected.conversation_id),
        source_message_id=_bounded_identity(expected.source_message_id),
        telegram_update_id=_canonical_telegram_update_id(expected.telegram_update_id),
        uploaded_raw_ids=raw_ids,
        expected_identity=expected,
        max_bytes_per_file=max_bytes_per_file,
    )


def _authorize_current_message_upload_batch_in_transaction(
    conn: Any,
    root: Path,
    authorization: AuthorizationService,
    actor: ActorContext,
    *,
    conversation_id: str,
    source_message_id: str,
    telegram_update_id: str,
    uploaded_raw_ids: tuple[str, ...],
    expected_identity: CurrentMessageUploadBatchIdentity | None,
    max_bytes_per_file: int | None,
) -> AuthorizedCurrentMessageUploadBatch:
    if not getattr(conn, "in_transaction", False):
        raise FileRecordUnavailable
    fresh_actor = _fresh_current_upload_actor(conn, authorization, actor)
    source_row = _current_telegram_user_message(
        conn,
        tenant_id=fresh_actor.user_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    source_identity, current_update, current_raw_ids = _current_upload_message_identity(source_row)
    if current_update != telegram_update_id or current_raw_ids != uploaded_raw_ids:
        raise FileRecordUnavailable
    if expected_identity is not None and (
        expected_identity.source_message_identity_sha256 != source_identity
        or expected_identity.source_message_id != source_message_id
        or expected_identity.conversation_id != conversation_id
        or expected_identity.telegram_update_id != telegram_update_id
        or expected_identity.uploaded_raw_ids != uploaded_raw_ids
    ):
        raise FileRecordUnavailable

    files: list[AuthorizedFileBytes] = []
    identities: list[CurrentMessageUploadFileIdentity] = []
    raw_tokens: list[AuthorizedFileSnapshotToken] = []
    for index, raw_id in enumerate(uploaded_raw_ids):
        row = _current_upload_row(
            conn,
            raw_id=raw_id,
            tenant_id=fresh_actor.user_id,
            person_id=fresh_actor.own_id,
        )
        if row is None:
            raise FileRecordUnavailable
        stored = _read_authorized_row(row, root, max_bytes=max_bytes_per_file)
        token = stored.snapshot_token
        if token is None or not authorized_file_snapshot_token_is_process_owned(token):
            raise FileRecordUnavailable
        content_digest = hashlib.sha256(stored.content).hexdigest()
        if not hmac.compare_digest(content_digest, token.content_sha256):
            raise FileRecordUnavailable
        identity = CurrentMessageUploadFileIdentity(
            raw_id=raw_id,
            source_identity_sha256=token.source.identity_sha256,
            content_sha256=content_digest,
            size_bytes=len(stored.content),
            filename=stored.filename,
            mime_type=stored.mime_type,
        )
        if expected_identity is not None and identity != expected_identity.files[index]:
            raise FileRecordUnavailable
        files.append(stored)
        identities.append(identity)
        raw_tokens.append(token)

    # Recheck every authority after the last descriptor read. The SQLite writer
    # boundary prevents an external commit in between; the second projection
    # also rejects a same-connection mutation or a corrupted row adapter.
    final_source = _current_telegram_user_message(
        conn,
        tenant_id=fresh_actor.user_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
    )
    if _current_upload_message_identity(final_source) != (
        source_identity,
        telegram_update_id,
        uploaded_raw_ids,
    ):
        raise FileRecordUnavailable
    for raw_id, token in zip(uploaded_raw_ids, raw_tokens, strict=True):
        current = _current_upload_row(
            conn,
            raw_id=raw_id,
            tenant_id=fresh_actor.user_id,
            person_id=fresh_actor.own_id,
        )
        if current is None:
            raise FileRecordUnavailable
        refreshed = authorized_file_snapshot_token(
            dict(current),
            content_sha256=token.content_sha256,
        )
        if not authorized_file_snapshot_token_is_process_owned(refreshed) or refreshed != token:
            raise FileRecordUnavailable
    _fresh_current_upload_actor(conn, authorization, fresh_actor)

    frozen_identities = tuple(identities)
    batch_identity = CurrentMessageUploadBatchIdentity(
        source_message_id=source_message_id,
        conversation_id=conversation_id,
        source_message_identity_sha256=source_identity,
        telegram_update_id=telegram_update_id,
        uploaded_raw_ids=uploaded_raw_ids,
        files=frozen_identities,
    )
    if expected_identity is not None and batch_identity != expected_identity:
        raise FileRecordUnavailable
    return AuthorizedCurrentMessageUploadBatch(
        identity=batch_identity,
        files=tuple(files),
    )


def _fresh_current_upload_actor(
    conn: Any,
    authorization: AuthorizationService,
    actor: ActorContext,
) -> ActorContext:
    if (
        type(actor) is not ActorContext
        or actor.source != "telegram-bridge"
        or not _bounded_identity(actor.user_id)
        or not _bounded_identity(actor.own_id)
        or authorization.storage is None
        or authorization.storage.conn is not conn
    ):
        raise FileRecordUnavailable
    principal = conn.execute(
        "SELECT preset_key, status FROM users WHERE id=?",
        (actor.own_id,),
    ).fetchone()
    tenant = (
        principal
        if actor.user_id == actor.own_id
        else conn.execute(
            "SELECT status FROM users WHERE id=?",
            (actor.user_id,),
        ).fetchone()
    )
    if (
        principal is None
        or tenant is None
        or str(principal["status"] or "") != "active"
        or str(tenant["status"] or "") != "active"
    ):
        raise FileRecordUnavailable
    fresh = replace(actor, preset_key=str(principal["preset_key"] or "guest"))
    decision = authorization.authorize(fresh, "files.read")
    if (
        not decision.allowed
        or decision.security_id != "files.read"
        or decision.user_id != fresh.own_id
        or decision.preset_key != fresh.preset_key
    ):
        raise FileRecordUnavailable
    return fresh


def _current_telegram_user_message(
    conn: Any,
    *,
    tenant_id: str,
    conversation_id: str,
    source_message_id: str,
) -> Any:
    row = conn.execute(
        """SELECT m.id, m.conversation_id, m.user_id, m.role, m.content,
                  m.metadata_json, m.reply_to, m.created_at
             FROM messages m
             JOIN conversations c
               ON c.id=m.conversation_id AND c.user_id=m.user_id
            WHERE m.id=? AND m.conversation_id=? AND m.user_id=?
              AND m.role='user'""",
        (source_message_id, conversation_id, tenant_id),
    ).fetchone()
    if row is None:
        raise FileRecordUnavailable
    return row


def _current_upload_message_identity(row: Any) -> tuple[str, str, tuple[str, ...]]:
    if row is None:
        raise FileRecordUnavailable
    metadata = _strict_metadata_object(row["metadata_json"])
    if metadata is None:
        raise FileRecordUnavailable
    update_id = metadata.get("telegram_update_id")
    uploaded = metadata.get("conversation_uploaded_raw_ids")
    try:
        raw_ids = _freeze_current_upload_raw_ids(uploaded)
        canonical_update = _canonical_telegram_update_id(update_id)
    except FileRecordUnavailable:
        raise
    digest = hashlib.sha256()
    for field_name in _MESSAGE_SOURCE_IDENTITY_FIELDS:
        try:
            value = row[field_name]
        except (KeyError, IndexError, TypeError) as exc:
            raise FileRecordUnavailable from exc
        if value is None:
            encoded = b"N"
        elif isinstance(value, str):
            encoded = b"S" + value.encode("utf-8", errors="surrogatepass")
        else:
            raise FileRecordUnavailable
        name = field_name.encode("ascii")
        digest.update(len(name).to_bytes(2, "big"))
        digest.update(name)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest(), canonical_update, raw_ids


def _strict_metadata_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value.encode("utf-8", errors="surrogatepass")) > 1_048_576:
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=reject_duplicates)
    except (UnicodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _bounded_identity(value: Any) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise FileRecordUnavailable
    return value


def _canonical_telegram_update_id(value: Any) -> str:
    if not isinstance(value, str) or _TELEGRAM_UPDATE_ID.fullmatch(value) is None:
        raise FileRecordUnavailable
    return value


def _freeze_current_upload_raw_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise FileRecordUnavailable
    frozen = tuple(value)
    if (
        not 1 <= len(frozen) <= _MAX_CURRENT_MESSAGE_UPLOADS
        or any(not isinstance(raw_id, str) or _OPAQUE_ID.fullmatch(raw_id) is None for raw_id in frozen)
        or len(set(frozen)) != len(frozen)
    ):
        raise FileRecordUnavailable
    return frozen


def read_authorized_generated_file(
    storage: Any,
    root: Path,
    raw_id: str,
    tenant_id: str,
    person_id: str,
    *,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Read one generated output owned by the exact authenticated person.

    A shared archive intentionally gives several people the same ``tenant_id``.
    Generated outputs belong to the conversation participant, so tenant scope by
    itself is insufficient here; both durable ownership markers must match.
    """

    with storage.transaction() as conn:
        row = conn.execute(
            f"""SELECT r.id, r.user_id, r.source_ref, r.metadata_json, r.content_hash
                  FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.content_type='generated_file'
                   AND r.deleted_at IS NULL
                   AND json_valid(r.metadata_json)
                   AND json_extract(r.metadata_json, '$.generated_artifact')=1
                   AND json_extract(r.metadata_json, '$.generated_for')=?
                   AND json_extract(r.metadata_json, '$.generated_tenant')=?
                   AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
            (str(raw_id), str(person_id), str(person_id), str(tenant_id)),
        ).fetchone()
        if row is None:
            raise FileRecordUnavailable
        metadata = _metadata_object(row["metadata_json"])
        expected_digest = str(metadata.get("sha256") or "")
        if not expected_digest or not hmac.compare_digest(str(row["content_hash"] or ""), expected_digest):
            raise FileRecordUnavailable
        stored = _read_authorized_row(row, root, max_bytes=max_bytes)
        expected_size = metadata.get("size_bytes")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or len(stored.content) != expected_size
        ):
            raise AuthorizedFileReadError(stored.filename, "файл не прошёл проверку размера")
        return stored


def _read_authorized_row(
    row: Any,
    root: Path,
    *,
    max_bytes: int | None,
) -> AuthorizedFileBytes:
    metadata = _metadata_object(row["metadata_json"])
    filename = _safe_filename(metadata.get("filename") or row["source_ref"] or "download")
    try:
        content_hash = str(row["content_hash"] or "")
    except (KeyError, IndexError, TypeError):
        content_hash = ""
    verdict = classify_file_registration(metadata, content_hash=content_hash)
    if verdict.state == LEGACY_UNREGISTERED:
        # Legacy rows have no durable disk registration.  They must not be
        # "repaired" into a verified disk read by inventing a path here.
        raise AuthorizedFileReadError(filename, "файл не зарегистрирован на диске")
    if verdict.state == REGISTERED_INVALID:
        # Fail closed.  Do not fall through to a path-only open, and do not
        # expose which field failed — higher layers may use classify_* for that.
        raise AuthorizedFileReadError(filename, "регистрация файла повреждена")

    stored_path = str(metadata.get("stored_path") or "")
    expected_digest = str(metadata.get("sha256") or "").casefold()

    try:
        content = _read_regular_file(
            root,
            stored_path,
            max_bytes=max_bytes,
            expected_sha256=expected_digest,
        )
    except _FileTooLarge as exc:
        raise AuthorizedFileReadError(filename, "не поместился по размеру") from exc
    except (OSError, ValueError) as exc:
        # Paths, errno strings and file contents never cross the public boundary.
        raise AuthorizedFileReadError(filename, "файл не прочитался") from exc

    size_bytes = metadata.get("size_bytes")
    # Modern valid registration always carries a non-negative integer size; the
    # classifier already rejected missing/invalid values before this open.
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes != len(content)
    ):
        raise AuthorizedFileReadError(filename, "файл не прошёл проверку размера")

    mime_type = metadata.get("mime_type")
    if not isinstance(mime_type, str) or not _MIME_TYPE.fullmatch(mime_type):
        mime_type = "application/octet-stream"
    return AuthorizedFileBytes(
        raw_id=str(row["id"]),
        filename=filename,
        mime_type=mime_type,
        content=content,
        snapshot_token=authorized_file_snapshot_token(
            dict(row),
            content_sha256=expected_digest,
        ),
    )


class _FileTooLarge(ValueError):
    pass


def _metadata_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 1_048_576:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_filename(value: Any) -> str:
    text = str(value or "download").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(char for char in text if char >= " " and char != "\x7f").strip(" .")
    return text[:_MAX_DOWNLOAD_FILENAME_CHARS] or "download"


def _read_regular_file(
    root: Path,
    candidate: str,
    *,
    max_bytes: int | None,
    expected_sha256: str,
) -> bytes:
    """Open one immutable in-root file descriptor, verify it, then consume it."""

    if not expected_sha256 or not _HEX64.fullmatch(expected_sha256):
        raise ValueError("expected digest is required")

    base_root = root.resolve(strict=True)
    given = Path(candidate)
    # Relative registration only.  Absolute candidates are rejected even when
    # they resolve under the root — modern writes always store a relative path.
    if given.is_absolute() or ".." in given.parts:
        raise ValueError("stored file path is not a safe relative registration")
    resolved = (base_root / given).resolve(strict=True)
    if resolved == base_root or not resolved.is_relative_to(base_root):
        raise ValueError("stored file is outside its root")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("stored object is not a regular file")
        if max_bytes is not None and before.st_size > max(0, int(max_bytes)):
            raise _FileTooLarge

        # Validate the object actually opened, not merely the pathname checked
        # before ``open``.  This closes symlink/rename swaps between resolution
        # and descriptor acquisition on Linux, where Friday is deployed.
        fd_link = Path(f"/proc/self/fd/{descriptor}")
        if fd_link.exists():
            opened = Path(os.path.realpath(fd_link))
            if opened == base_root or not opened.is_relative_to(base_root):
                raise ValueError("opened file is outside its root")

        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max(0, int(max_bytes)):
                raise _FileTooLarge
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("stored file changed while being read")
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256.casefold()):
            raise ValueError("stored file digest mismatch")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


__all__ = [
    "AuthorizedCurrentUpload",
    "AuthorizedCurrentMessageUploadBatch",
    "AuthorizedFileBytes",
    "AuthorizedFileReadError",
    "CurrentMessageUploadBatchIdentity",
    "CurrentMessageUploadFileIdentity",
    "FileRecordUnavailable",
    "FileRegistrationVerdict",
    "LEGACY_UNREGISTERED",
    "REGISTERED_INVALID",
    "REGISTERED_VALID",
    "attachment_content_disposition",
    "authorize_current_message_upload_batch",
    "authorize_current_message_upload_batch_in_transaction",
    "authorize_current_upload_file",
    "authorize_current_upload_file_in_transaction",
    "classify_file_registration",
    "read_authorized_file",
    "read_authorized_file_in_transaction",
    "read_authorized_generated_file",
    "read_current_message_upload_file",
    "reauthorize_current_message_upload_batch",
    "reauthorize_current_message_upload_batch_in_transaction",
    "verify_registered_file_bytes",
]
