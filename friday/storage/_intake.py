"""Storage methods for raw objects and the review-gated Inbox.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES
from friday.storage._base import (
    Any,
    InboxItem,
    InboxStatus,
    PrivateMaterialQuarantineError,
    PurePosixPath,
    RawObject,
    SourceReferenceConflictError,
    StorageShared,
    _json_load,
    enum_value,
    hashlib,
    hmac,
    json,
    sqlite3,
    utc_now,
    validate_user_id,
)
from friday.storage._knowledge import _fts_terms
from friday.storage._privacy import (
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
)

_TELEGRAM_FILE_SOURCE_PREFIX = "telegram-file:"
_TELEGRAM_UNIQUE_SOURCE_PREFIX = "telegram-unique:"
_TELEGRAM_MESSAGE_SOURCE_REF = re.compile(r"telegram-message:-?[1-9][0-9]{0,31}:[1-9][0-9]{0,31}\Z")
_KNOWLEDGE_OBJECT_ID_RE = re.compile(r"ko_[A-Za-z0-9_-]{8,120}\Z")
_PUBLIC_FILE_CITATION_MAX = 12

TELEGRAM_REPLY_RESOLVED = "resolved"
TELEGRAM_REPLY_ABSENT = "absent"
TELEGRAM_REPLY_BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TelegramReplyResolution:
    """Closed authority verdict for an exact structural Telegram reply."""

    status: str
    raw_object_id: str = ""
    uploaded_by: str = ""


@dataclass(frozen=True, slots=True)
class PublicFileCitationSource:
    """Opaque KO → file Raw → active uploader triple for multi-citation flow.

    Storage never returns body, path or display name from this resolver; Runtime
    still re-authorizes each Raw under the exact uploader before any disk read.
    """

    knowledge_object_id: str
    raw_object_id: str
    uploaded_by: str


def resolve_owned_file_exact_filename_direct_read(
    storage: StorageShared,
    user_id: str,
    uploaded_by: str,
    filename: str,
    *,
    expected_raw_id: str | None = None,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    """Resolve one exact uploader-owned filename, including Inbox ``ignored``.

    This is deliberately a module-level, runtime-only boundary rather than a
    general ``FridayStorage`` method.  Ambient catalog, fuzzy/content search,
    replay and citations keep using verdict-aware storage methods which exclude
    ignored material.  A fixed ``LIMIT 2`` proves uniqueness after every
    tenant/uploader/lifecycle/privacy predicate.  When ``expected_raw_id`` is
    supplied, the same single SELECT both re-proves uniqueness and binds that
    exact Raw before projecting its body.
    """

    tenant = str(user_id or "").strip()
    person = str(uploaded_by or "").strip()
    clean_filename = str(filename or "").strip()
    expected = str(expected_raw_id or "").strip()
    if (
        not tenant
        or not person
        or not clean_filename
        or len(clean_filename) > 260
        or (expected_raw_id is not None and not expected)
    ):
        return []
    content_projection = (
        ", r.raw_content AS _raw_content, r.metadata_json AS _raw_metadata" if include_content else ""
    )
    base = f"""r.user_id=? AND r.deleted_at IS NULL
               AND r.content_type='file'
               AND {_exact_uploader_raw_dependency("r")}
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND EXISTS (
                   SELECT 1 FROM users exact_direct_read_uploader
                    WHERE exact_direct_read_uploader.id=?
                      AND exact_direct_read_uploader.status='active'
               )"""
    rows = storage.execute(
        f"""WITH candidates AS (
                   SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at,
                          r.content_hash,
                          substr(json_extract(r.metadata_json,'$.filename'),1,260) AS filename,
                          1 AS lane{content_projection}
                     FROM raw_objects r
                    WHERE {base}
                      AND json_type(r.metadata_json,'$.filename')='text'
                      AND length(json_extract(r.metadata_json,'$.filename')) BETWEEN 1 AND 260
                      AND replace(jericho_casefold(
                              substr(json_extract(r.metadata_json,'$.filename'),1,261)
                          ),'ё','е')=replace(jericho_casefold(?),'ё','е')
                   UNION ALL
                   SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at,
                          r.content_hash, a.supplied_filename AS filename,
                          0 AS lane{content_projection}
                     FROM file_source_aliases a
                     JOIN raw_objects r ON r.id=a.raw_object_id
                    WHERE a.user_id=? AND a.uploaded_by=?
                      AND {base}
                      AND length(a.supplied_filename) BETWEEN 1 AND 260
                      AND replace(jericho_casefold(a.supplied_filename),'ё','е')=
                          replace(jericho_casefold(?),'ё','е')
               ),
               ranked AS (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY id ORDER BY lane ASC, received_at ASC, source_ref ASC
                   ) AS _choice
                     FROM candidates
               )
               SELECT * FROM ranked WHERE _choice=1
                ORDER BY received_at ASC, id ASC
                LIMIT 2""",  # nosec B608 - only fixed privacy predicates
        (
            tenant,
            person,
            person,
            clean_filename,
            tenant,
            person,
            tenant,
            person,
            person,
            clean_filename,
        ),
    ).fetchall()
    result = [dict(row) for row in rows]
    for item in result:
        item.pop("lane", None)
        item.pop("_choice", None)
    if expected_raw_id is not None and (len(result) != 1 or str(result[0].get("id") or "") != expected):
        return []
    return result


def resolve_structural_telegram_reply_direct_read(
    storage: StorageShared,
    user_id: str,
    uploaded_by: str,
    raw_object_id: str,
    *,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    """Re-authorize one structural Telegram reply Raw, including Inbox ``ignored``.

    This is a runtime-only boundary. Ambient catalog, search, citations and
    ordinary replay keep using verdict-aware readers. Authority still requires
    the exact tenant, exact active uploader, live non-audio public file and a
    process-private reply carrier; a public raw-id payload cannot call this.
    """

    tenant = str(user_id or "").strip()
    person = str(uploaded_by or "").strip()
    raw_id = str(raw_object_id or "").strip()
    if not tenant or not person or not raw_id:
        return []
    content_projection = (
        ", r.raw_content AS _raw_content, r.metadata_json AS _raw_metadata" if include_content else ""
    )
    rows = storage.execute(
        f"""SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at,
                   r.content_hash{content_projection}
              FROM raw_objects r
             WHERE r.user_id=? AND r.id=? AND r.deleted_at IS NULL
               AND r.content_type='file'
               AND {_exact_uploader_raw_dependency("r")}
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND EXISTS (
                   SELECT 1 FROM users historical_direct_read_uploader
                    WHERE historical_direct_read_uploader.id=?
                      AND historical_direct_read_uploader.status='active'
               )
             LIMIT 2""",  # nosec B608 - only fixed privacy predicates
        (tenant, raw_id, person, person),
    ).fetchall()
    return [dict(row) for row in rows] if len(rows) == 1 else []


def resolve_explicit_file_citation_sources(
    storage: StorageShared,
    user_id: str,
    knowledge_ids: Sequence[str],
    *,
    limit: int = _PUBLIC_FILE_CITATION_MAX,
) -> list[PublicFileCitationSource]:
    """Ordered all-or-none explicit citation join; Inbox ``ignored`` is not a veto.

    Ambient/latest citation recall keeps using
    ``resolve_public_file_citation_sources``. This resolver is only for an
    explicit ``[K#]`` / exact-cited-assistant selector. It still requires the
    same tenant, live/non-private KO, live/non-private/non-audio file Raw,
    exact bounded metadata uploader and an active uploader, and closes the
    whole set on any missing or extra member.
    """

    page_size = max(1, min(int(limit), _PUBLIC_FILE_CITATION_MAX))
    ordered_ids: list[str] = []
    for value in knowledge_ids:
        knowledge_id = str(value or "").strip()
        if not knowledge_id or not _KNOWLEDGE_OBJECT_ID_RE.fullmatch(knowledge_id):
            return []
        if knowledge_id in ordered_ids:
            return []
        ordered_ids.append(knowledge_id)
        if len(ordered_ids) > page_size:
            return []
    if not ordered_ids:
        return []

    tenant = str(user_id or "").strip()
    if not tenant:
        return []

    uploader_expr = _bounded_raw_uploader_expression("r")
    placeholders = ",".join("?" for _item in ordered_ids)
    rows = storage.execute(
        f"""SELECT k.id AS knowledge_object_id,
                   r.id AS raw_object_id,
                   {uploader_expr} AS uploaded_by
              FROM knowledge_objects k
              JOIN raw_objects r
                ON r.id=k.raw_object_id
               AND r.user_id=k.user_id
             WHERE k.user_id=?
               AND k.id IN ({placeholders})
               AND k.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("k")}
               AND r.deleted_at IS NULL
               AND r.content_type='file'
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND {uploader_expr} IS NOT NULL
               AND {uploader_expr}<>''
               AND EXISTS (
                   SELECT 1 FROM users exact_citation_uploader
                    WHERE exact_citation_uploader.id={uploader_expr}
                      AND exact_citation_uploader.status='active'
               )""",  # nosec B608 - placeholders and fixed privacy clauses only
        (tenant, *ordered_ids),
    ).fetchall()
    found = {
        str(row["knowledge_object_id"]): (
            str(row["raw_object_id"] or ""),
            str(row["uploaded_by"] or ""),
        )
        for row in rows
    }
    if len(found) != len(ordered_ids):
        return []
    result: list[PublicFileCitationSource] = []
    for knowledge_id in ordered_ids:
        pair = found.get(knowledge_id)
        if pair is None:
            return []
        raw_id, uploaded_by = pair
        if not raw_id or not uploaded_by:
            return []
        try:
            validated_uploader = validate_user_id(uploaded_by)
        except ValueError:
            return []
        result.append(
            PublicFileCitationSource(
                knowledge_object_id=knowledge_id,
                raw_object_id=raw_id,
                uploaded_by=validated_uploader,
            )
        )
    return result


def _telegram_file_source_ref_kind(source_ref: str) -> str:
    """Validate one closed Telegram file identity without granting authority."""

    exact_ref = str(source_ref or "")
    if not exact_ref or exact_ref != exact_ref.strip() or len(exact_ref) > 500:
        return ""
    if _TELEGRAM_MESSAGE_SOURCE_REF.fullmatch(exact_ref):
        return "message"
    for prefix, kind in (
        (_TELEGRAM_FILE_SOURCE_PREFIX, "file"),
        (_TELEGRAM_UNIQUE_SOURCE_PREFIX, "unique"),
    ):
        if not exact_ref.startswith(prefix):
            continue
        opaque = exact_ref.removeprefix(prefix)
        if opaque and all(char.isascii() and 33 <= ord(char) <= 126 for char in opaque):
            return kind
    return ""


def bind_owned_telegram_reply_aliases(
    storage: StorageShared,
    user_id: str,
    uploaded_by: str,
    raw_object_id: str,
    source_refs: tuple[str, ...],
) -> bool:
    """Atomically bind server-derived message/unique identities to one Raw.

    The ordinary ingestion alias method intentionally accepts only
    ``telegram-file:``.  Keeping these two stronger identities in a separate
    function prevents an arbitrary upload ``source_ref`` from impersonating a
    bridge-authenticated chat/message pair or Telegram's stable unique id.
    """

    exact_user = str(user_id or "").strip()
    exact_uploader = str(uploaded_by or "").strip()
    exact_raw = str(raw_object_id or "").strip()
    aliases = list(dict.fromkeys(str(value or "") for value in source_refs))
    if (
        not exact_user
        or not exact_uploader
        or not exact_raw
        or not aliases
        or len(aliases) > 2
        or any(_telegram_file_source_ref_kind(value) not in {"message", "unique"} for value in aliases)
    ):
        return False
    with storage.transaction() as conn:
        canonical = conn.execute(
            f"""SELECT r.id FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.source='upload'
                   AND r.content_type='file' AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND {_exact_uploader_raw_dependency("r")}
                 LIMIT 1""",  # nosec B608 - fixed predicates, values are bound
            (exact_raw, exact_user, exact_uploader),
        ).fetchone()
        if canonical is None:
            return False
        placeholders = ",".join("?" for _value in aliases)
        existing = conn.execute(
            f"""SELECT source_ref, raw_object_id FROM file_source_aliases
                 WHERE user_id=? AND uploaded_by=?
                   AND source_ref IN ({placeholders})""",  # nosec B608 - placeholders only
            (exact_user, exact_uploader, *aliases),
        ).fetchall()
        existing_by_ref = {str(row["source_ref"]): str(row["raw_object_id"]) for row in existing}
        if any(existing_by_ref.get(source_ref, exact_raw) != exact_raw for source_ref in aliases):
            raise SourceReferenceConflictError(
                "Telegram reply identity is already bound to different content"
            )
        for source_ref in aliases:
            if source_ref in existing_by_ref:
                continue
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id, created_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (exact_user, exact_uploader, source_ref, exact_raw, utc_now()),
            )
    return True


def bind_owned_telegram_reply_recovery_aliases(
    storage: StorageShared,
    user_id: str,
    uploaded_by: str,
    raw_object_id: str,
    source_refs: tuple[str, ...],
) -> bool:
    """Atomically bind the complete exact identity set of recovered media."""

    exact_user = str(user_id or "").strip()
    exact_uploader = str(uploaded_by or "").strip()
    exact_raw = str(raw_object_id or "").strip()
    aliases = list(dict.fromkeys(str(value or "") for value in source_refs))
    kinds = [_telegram_file_source_ref_kind(value) for value in aliases]
    if (
        not exact_user
        or not exact_uploader
        or not exact_raw
        or not aliases
        or len(aliases) > 3
        or kinds.count("file") != 1
        or any(not kind for kind in kinds)
    ):
        return False
    with storage.transaction() as conn:
        canonical = conn.execute(
            f"""SELECT r.id FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.source='upload'
                   AND r.content_type='file' AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND {_exact_uploader_raw_dependency("r")}
                 LIMIT 1""",  # nosec B608 - fixed predicates, values are bound
            (exact_raw, exact_user, exact_uploader),
        ).fetchone()
        if canonical is None:
            return False
        file_ref = aliases[kinds.index("file")]
        legacy_binding = conn.execute(
            """SELECT 1 FROM raw_objects legacy
                 WHERE legacy.user_id=? AND legacy.source='upload'
                   AND (
                       legacy.source_ref=?
                       OR (
                           length(legacy.source_ref)=length(?)+34
                           AND substr(legacy.source_ref,1,9)='uploader:'
                           AND substr(legacy.source_ref,34,1)=':'
                           AND substr(legacy.source_ref,35)=?
                       )
                   )
                 LIMIT 1""",
            (exact_user, file_ref, file_ref, file_ref),
        ).fetchone()
        if legacy_binding is not None:
            raise SourceReferenceConflictError("Telegram reply recovery file identity is already bound")
        placeholders = ",".join("?" for _value in aliases)
        existing = conn.execute(
            f"""SELECT source_ref, raw_object_id, uploaded_by
                  FROM file_source_aliases
                 WHERE user_id=? AND source_ref IN ({placeholders})""",  # nosec B608
            (exact_user, *aliases),
        ).fetchall()
        existing_by_ref: dict[str, set[tuple[str, str]]] = {}
        for row in existing:
            existing_by_ref.setdefault(str(row["source_ref"]), set()).add(
                (str(row["raw_object_id"]), str(row["uploaded_by"]))
            )
        if any(bindings != {(exact_raw, exact_uploader)} for bindings in existing_by_ref.values()):
            raise SourceReferenceConflictError(
                "Telegram reply recovery identity is already bound to different content"
            )
        for source_ref in aliases:
            if source_ref in existing_by_ref:
                continue
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id, created_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (exact_user, exact_uploader, source_ref, exact_raw, utc_now()),
            )
    return True


def resolve_owned_telegram_reply_aliases(
    storage: StorageShared,
    user_id: str,
    uploaded_by: str,
    source_refs: tuple[str, ...],
) -> str | None:
    """Atomically re-authorize a closed set of Telegram reply identities.

    Missing/churned identities may be absent, but every identity that resolves
    must converge on exactly one currently readable Raw Object.  A disagreement
    between message id, stable unique id and current file id fails closed.
    """

    exact_user = str(user_id or "").strip()
    exact_uploader = str(uploaded_by or "").strip()
    refs = list(dict.fromkeys(str(value or "") for value in source_refs))
    kinds = [_telegram_file_source_ref_kind(value) for value in refs]
    if not exact_user or not exact_uploader or not refs or len(refs) > 3 or any(not kind for kind in kinds):
        return None
    alias_placeholders = ",".join("?" for _value in refs)
    file_refs = [source_ref for source_ref, kind in zip(refs, kinds, strict=True) if kind == "file"]
    bound_sql = f"""SELECT a.raw_object_id
                       FROM file_source_aliases a
                      WHERE a.user_id=? AND a.uploaded_by=?
                        AND a.source_ref IN ({alias_placeholders})"""
    parameters: list[Any] = [exact_user, exact_uploader, *refs]
    if file_refs:
        uploader_namespace = hashlib.sha256(exact_uploader.encode("utf-8")).hexdigest()[:24]
        legacy_refs = [
            value
            for source_ref in file_refs
            for value in (source_ref, f"uploader:{uploader_namespace}:{source_ref}")
        ]
        legacy_placeholders = ",".join("?" for _value in legacy_refs)
        bound_sql += f"""
                    UNION
                    SELECT legacy.id
                      FROM raw_objects legacy
                     WHERE legacy.user_id=? AND legacy.source='upload'
                       AND legacy.source_ref IN ({legacy_placeholders})"""
        parameters.extend((exact_user, *legacy_refs))
    rows = storage.execute(
        f"""WITH bound(raw_object_id) AS ({bound_sql})
             SELECT DISTINCT r.id FROM bound b
             JOIN raw_objects r ON r.id=b.raw_object_id
             WHERE r.user_id=? AND r.source='upload'
               AND r.content_type='file' AND r.deleted_at IS NULL
               AND (SELECT COUNT(DISTINCT raw_object_id) FROM bound)=1
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND {_exact_uploader_raw_dependency("r")}
             LIMIT 2""",  # nosec B608 - fixed predicates and bounded placeholders only
        (*parameters, exact_user, exact_uploader),
    ).fetchall()
    return str(rows[0]["id"]) if len(rows) == 1 else None


def _bounded_raw_uploader_expression(alias: str) -> str:
    """Project one exact uploader from bounded, duplicate-free Raw metadata."""

    return f"""CASE
        WHEN length(CAST(COALESCE({alias}.metadata_json,'') AS BLOB))
                 <={RAW_FILE_METADATA_MAX_BYTES}
         AND typeof({alias}.metadata_json)='text'
         AND json_valid({alias}.metadata_json)
         AND json_type({alias}.metadata_json)='object'
         AND NOT EXISTS (
               SELECT 1 FROM json_tree({alias}.metadata_json) uploader_json_member
                WHERE uploader_json_member.key IS NOT NULL
                GROUP BY uploader_json_member.parent,
                         CAST(uploader_json_member.key AS TEXT)
               HAVING COUNT(*) > 1
             )
         AND json_type({alias}.metadata_json,'$.uploaded_by')='text'
        THEN json_extract({alias}.metadata_json,'$.uploaded_by')
        ELSE NULL
      END"""


def resolve_tenant_telegram_reply_aliases(
    storage: StorageShared,
    user_id: str,
    source_refs: tuple[str, ...],
) -> tuple[str, str] | None:
    """Resolve exact Telegram identities to one live same-tenant upload.

    A structural reply is read authority, not a claim that the replier uploaded
    the file.  The durable alias records the actual uploader; every supplied
    identity which resolves must converge on that same Raw/uploader pair.  The
    caller still has to prove ``files.read`` before using the result.
    """

    exact_user = str(user_id or "").strip()
    refs = list(dict.fromkeys(str(value or "") for value in source_refs))
    kinds = [_telegram_file_source_ref_kind(value) for value in refs]
    if not exact_user or not refs or len(refs) > 3 or any(not kind for kind in kinds):
        return None
    placeholders = ",".join("?" for _value in refs)
    file_refs = [source_ref for source_ref, kind in zip(refs, kinds, strict=True) if kind == "file"]
    uploader_value = _bounded_raw_uploader_expression("legacy")
    bound_sql = f"""SELECT a.raw_object_id, a.uploaded_by
                       FROM file_source_aliases a
                      WHERE a.user_id=?
                        AND a.source_ref IN ({placeholders})"""
    parameters: list[Any] = [exact_user, *refs]
    if file_refs:
        file_placeholders = ",".join("?" for _value in file_refs)
        namespaced_clauses = " OR ".join(
            """(
                length(legacy.source_ref)=length(?)+34
                AND substr(legacy.source_ref,1,9)='uploader:'
                AND substr(legacy.source_ref,34,1)=':'
                AND substr(legacy.source_ref,35)=?
            )"""
            for _value in file_refs
        )
        bound_sql += f"""
                    UNION
                    SELECT legacy.id, {uploader_value}
                      FROM raw_objects legacy
                     WHERE legacy.user_id=? AND legacy.source='upload'
                       AND (
                           legacy.source_ref IN ({file_placeholders})
                           OR ({namespaced_clauses})
                       )"""
        parameters.extend(
            (
                exact_user,
                *file_refs,
                *(value for source_ref in file_refs for value in (source_ref, source_ref)),
            )
        )
    exact_raw_uploader = _bounded_raw_uploader_expression("r")
    rows = storage.execute(
        f"""WITH bound(raw_object_id, uploaded_by) AS ({bound_sql})
             SELECT DISTINCT r.id, b.uploaded_by, r.source_ref
               FROM bound b
               JOIN raw_objects r ON r.id=b.raw_object_id
               JOIN users uploader ON uploader.id=b.uploaded_by
              WHERE r.user_id=? AND r.source='upload'
                AND r.content_type='file' AND r.deleted_at IS NULL
                AND uploader.status='active'
                AND b.uploaded_by IS NOT NULL AND b.uploaded_by<>''
                AND (SELECT COUNT(DISTINCT raw_object_id) FROM bound)=1
                AND (SELECT COUNT(DISTINCT uploaded_by) FROM bound)=1
                AND ({exact_raw_uploader})=b.uploaded_by
                AND {_not_audio_document("r")}
                AND {_not_private_raw_dependency("r")}
              LIMIT 2""",  # nosec B608 - fixed predicates and bounded placeholders only
        (*parameters, exact_user),
    ).fetchall()
    if len(rows) != 1:
        return None
    resolved_uploader = str(rows[0]["uploaded_by"])
    canonical_source_ref = str(rows[0]["source_ref"] or "")
    if canonical_source_ref.startswith("uploader:"):
        expected_namespace = hashlib.sha256(resolved_uploader.encode("utf-8")).hexdigest()[:24]
        expected_prefix = f"uploader:{expected_namespace}:"
        if not canonical_source_ref.startswith(expected_prefix):
            return None
    return str(rows[0]["id"]), resolved_uploader


def resolve_tenant_telegram_reply_alias_state(
    storage: StorageShared,
    user_id: str,
    source_refs: tuple[str, ...],
) -> TelegramReplyResolution:
    """Distinguish a missing reply identity from unsafe durable state.

    Recovery is permitted only for ``absent``.  A stale, private, deleted,
    cross-uploader, or conflicting binding is ``blocked`` rather than being
    mistaken for an invitation to download and re-ingest bytes.  An Inbox
    ``ignored`` row remains a live structural-reply target when every other
    gate still holds.
    """

    exact_user = str(user_id or "").strip()
    refs = list(dict.fromkeys(str(value or "") for value in source_refs))
    kinds = [_telegram_file_source_ref_kind(value) for value in refs]
    if not exact_user or not refs or len(refs) > 3 or any(not kind for kind in kinds):
        return TelegramReplyResolution(TELEGRAM_REPLY_BLOCKED)
    resolved = resolve_tenant_telegram_reply_aliases(storage, exact_user, tuple(refs))
    if resolved is not None:
        raw_id, uploaded_by = resolved
        return TelegramReplyResolution(
            TELEGRAM_REPLY_RESOLVED,
            raw_object_id=raw_id,
            uploaded_by=uploaded_by,
        )

    placeholders = ",".join("?" for _value in refs)
    alias_bound = storage.execute(
        f"""SELECT 1 FROM file_source_aliases
              WHERE user_id=? AND source_ref IN ({placeholders})
              LIMIT 1""",  # nosec B608 - bounded placeholders only
        (exact_user, *refs),
    ).fetchone()
    if alias_bound is not None:
        return TelegramReplyResolution(TELEGRAM_REPLY_BLOCKED)

    file_refs = [source_ref for source_ref, kind in zip(refs, kinds, strict=True) if kind == "file"]
    if file_refs:
        direct_placeholders = ",".join("?" for _value in file_refs)
        namespaced = " OR ".join(
            "(length(source_ref)=length(?)+34 AND substr(source_ref,1,9)='uploader:' "
            "AND substr(source_ref,34,1)=':' AND substr(source_ref,35)=?)"
            for _value in file_refs
        )
        legacy_bound = storage.execute(
            f"""SELECT 1 FROM raw_objects
                  WHERE user_id=? AND source='upload'
                    AND (source_ref IN ({direct_placeholders}) OR ({namespaced}))
                  LIMIT 1""",  # nosec B608 - bounded placeholders only
            (
                exact_user,
                *file_refs,
                *(value for source_ref in file_refs for value in (source_ref, source_ref)),
            ),
        ).fetchone()
        if legacy_bound is not None:
            return TelegramReplyResolution(TELEGRAM_REPLY_BLOCKED)
    return TelegramReplyResolution(TELEGRAM_REPLY_ABSENT)


def _bounded_public_inbox_card(item: Mapping[str, Any]) -> dict[str, Any]:
    """Structural Inbox card without reviewer identity or advisory text."""

    data = dict(item)

    def identifier_field(
        name: str,
        prefixes: tuple[str, ...],
        *,
        nullable: bool = False,
    ) -> str | None:
        value = data.get(name)
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not value.startswith(prefixes) or len(value) > 160:
            return None if nullable else ""
        if any(not char.isascii() or not (char.isalnum() or char in "_-.:") for char in value):
            return None if nullable else ""
        return value

    def timestamp_field(name: str, *, nullable: bool = False) -> str | None:
        value = data.get(name)
        if value is None and nullable:
            return None
        if not isinstance(value, str) or len(value) > 64:
            return None if nullable else ""
        if any(char not in "0123456789T:+-.Z " for char in value):
            return None if nullable else ""
        return value

    def score_field(name: str) -> float | None:
        value = data.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        numeric = float(value)
        return max(0.0, min(numeric, 1.0)) if math.isfinite(numeric) else None

    status_value = data.get("status")
    action_value = data.get("suggested_action")
    card: dict[str, Any] = {
        "id": identifier_field("id", ("inb", "inbox")),
        "raw_object_id": identifier_field("raw_object_id", ("raw",)),
        "knowledge_object_id": identifier_field("knowledge_object_id", ("ko",), nullable=True),
        "status": (
            status_value
            if isinstance(status_value, str) and status_value in {status.value for status in InboxStatus}
            else ""
        ),
        "suggested_entity_id": identifier_field("suggested_entity_id", ("ent",), nullable=True),
        "suggested_action": (
            action_value
            if isinstance(action_value, str)
            and action_value
            in {
                "archived",
                "classified",
                "ignored",
                "keep_transient",
                "legacy_review",
                "none",
                "pending",
                "promote",
                "review",
                "review_links",
            }
            else "review"
        ),
        "promotion_score": score_field("promotion_score"),
        "quality_score": score_field("quality_score"),
        "created_at": timestamp_field("created_at"),
        "reviewed_at": timestamp_field("reviewed_at", nullable=True),
    }

    suggestions = data.get("suggestions_json")
    notes = data.get("classification_notes")
    tags = data.get("suggested_tags_json")
    suggestions_text = suggestions if isinstance(suggestions, str) else ""
    notes_text = notes if isinstance(notes, str) else ""
    tags_text = tags if isinstance(tags, str) else ""
    card["advisory"] = {
        "suggestions_present": suggestions_text not in {"", "{}", "null"},
        "suggestions_bytes": min(len(suggestions_text.encode("utf-8", errors="replace")), 1_000_000_000),
        "suggested_tags_present": tags_text not in {"", "[]", "null"},
        "suggested_tags_bytes": min(len(tags_text.encode("utf-8", errors="replace")), 1_000_000_000),
        "notes_present": bool(notes_text),
        "notes_chars": min(len(notes_text), 1_000_000_000),
    }
    return card


class IntakeMixin(StorageShared):
    def count_visible_raw_objects(self, user_id: str, *, files_only: bool = False) -> int:
        """Count the privacy-safe Raw Object corpus, never a bounded page."""

        file_clause = f" AND r.content_type='file' AND {_not_audio_document('r')}" if files_only else ""
        row = self.execute(
            f"""SELECT COUNT(*) AS count FROM raw_objects r
                  WHERE r.user_id=? AND r.deleted_at IS NULL
                    AND {_not_private_raw_dependency("r")}
                    {file_clause}""",  # nosec B608 -- clauses are fixed constants
            (user_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def _raw_from_row(self, row: sqlite3.Row | dict[str, Any]) -> RawObject:
        data = dict(row)
        return RawObject(
            id=data["id"],
            user_id=data["user_id"],
            source=data["source"],
            source_ref=data.get("source_ref", ""),
            raw_content=data.get("raw_content", ""),
            content_type=data.get("content_type", "text"),
            metadata_json=_json_load(data.get("metadata_json"), {}),
            content_hash=data.get("content_hash", ""),
            version=int(data.get("version", 1)),
            received_at=data.get("received_at") or utc_now(),
            created_at=data.get("created_at") or utc_now(),
            deleted_at=data.get("deleted_at"),
        )

    def find_raw_by_source_ref(self, user_id: str, source: str, source_ref: str) -> dict[str, Any] | None:
        if not source_ref:
            return None
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.source=? AND r.source_ref=?
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}""",  # nosec B608
            (user_id, source, source_ref),
        ).fetchone()
        return dict(row) if row else None

    def resolve_owned_file_source_ref(
        self,
        user_id: str,
        uploaded_by: str,
        source_ref: str,
    ) -> str | None:
        """Atomically authorize an exact reply-to-upload pointer as one Raw id."""

        exact_ref = str(source_ref or "").strip()
        exact_uploader = str(uploaded_by or "").strip()
        if not exact_uploader or _telegram_file_source_ref_kind(exact_ref) != "file" or len(exact_ref) > 500:
            return None
        uploader_namespace = hashlib.sha256(exact_uploader.encode("utf-8")).hexdigest()[:24]
        scoped_ref = f"uploader:{uploader_namespace}:{exact_ref}"
        rows = self.execute(
            f"""WITH bound(raw_object_id) AS (
                    SELECT a.raw_object_id
                      FROM file_source_aliases a
                     WHERE a.user_id=? AND a.uploaded_by=? AND a.source_ref=?
                    UNION
                    SELECT legacy.id
                      FROM raw_objects legacy
                     WHERE legacy.user_id=? AND legacy.source='upload'
                       AND legacy.source_ref IN (?, ?)
                 )
                 SELECT DISTINCT r.id FROM bound b
                 JOIN raw_objects r ON r.id=b.raw_object_id
                 WHERE r.user_id=? AND r.source='upload'
                   AND r.content_type='file' AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND {_exact_uploader_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )
                 LIMIT 2""",  # nosec B608 - fixed predicates, values are bound
            (
                str(user_id),
                exact_uploader,
                exact_ref,
                str(user_id),
                exact_ref,
                scoped_ref,
                str(user_id),
                exact_uploader,
            ),
        ).fetchall()
        # A well-formed archive has one canonical Raw binding, whether it came
        # from the immutable alias table, the legacy key, or its uploader-scoped
        # successor. Two different live bindings are inconsistent authority,
        # not a reason to choose one by row order.
        return str(rows[0]["id"]) if len(rows) == 1 else None

    def bind_owned_file_source_ref_alias(
        self,
        user_id: str,
        uploaded_by: str,
        source_ref: str,
        raw_object_id: str,
        supplied_filename: str = "",
    ) -> bool:
        """Bind one fresh Telegram file id to an existing immutable Raw Object.

        Byte-level deduplication deliberately reuses the first Raw row.  This
        separate binding preserves every later Telegram ``file_id`` without
        rewriting that row's original provenance.  Conflicting aliases fail
        closed and the resolver rechecks current lifecycle/privacy/verdict on
        every read; the alias itself grants no access.
        """

        exact_user = str(user_id or "").strip()
        exact_uploader = str(uploaded_by or "").strip()
        exact_ref = str(source_ref or "").strip()
        exact_raw = str(raw_object_id or "").strip()
        alias_filename = str(supplied_filename or "").strip()
        if (
            not exact_user
            or not exact_uploader
            or not exact_raw
            or _telegram_file_source_ref_kind(exact_ref) != "file"
            or len(exact_ref) > 500
            or len(alias_filename) > 260
            or any(char in alias_filename for char in ("/", "\\", "\x00", "\r", "\n"))
        ):
            return False
        with self.transaction() as conn:
            canonical = conn.execute(
                f"""SELECT r.id FROM raw_objects r
                     WHERE r.id=? AND r.user_id=? AND r.source='upload'
                       AND r.content_type='file' AND r.deleted_at IS NULL
                       AND {_not_audio_document("r")}
                       AND {_not_private_raw_dependency("r")}
                       AND {_exact_uploader_raw_dependency("r")}
                       AND NOT EXISTS (
                           SELECT 1 FROM inbox i
                            WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                              AND i.status='ignored'
                       )
                     LIMIT 1""",  # nosec B608 - fixed predicates, values are bound
                (exact_raw, exact_user, exact_uploader),
            ).fetchone()
            if canonical is None:
                return False
            existing = conn.execute(
                """SELECT raw_object_id, supplied_filename FROM file_source_aliases
                    WHERE user_id=? AND uploaded_by=? AND source_ref=?""",
                (exact_user, exact_uploader, exact_ref),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_object_id"]) != exact_raw:
                    raise SourceReferenceConflictError(
                        "file source alias is already bound to different content"
                    )
                if alias_filename and not str(existing["supplied_filename"] or ""):
                    conn.execute(
                        """UPDATE file_source_aliases SET supplied_filename=?
                            WHERE user_id=? AND uploaded_by=? AND source_ref=?
                              AND supplied_filename=''""",
                        (alias_filename, exact_user, exact_uploader, exact_ref),
                    )
                return True
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id,
                       supplied_filename, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (exact_user, exact_uploader, exact_ref, exact_raw, alias_filename, utc_now()),
            )
            return True

    def find_file_by_content_hash(
        self,
        user_id: str,
        content_hash: str,
        *,
        uploaded_by: str | None = None,
        scope_uploaded_by: bool = False,
    ) -> dict[str, Any] | None:
        """Тот же файл этого человека, принятый раньше под другим `source_ref`.

        Ключ происхождения у Telegram содержит `update_id`, уникальный для каждой
        отправки, поэтому пересланный второй раз документ не совпадал сам с собой
        по `source_ref` НИКОГДА. Замерено: одна и та же строка байт дала два Raw
        Object с одинаковым `content_hash`, два элемента Inbox и два одинаковых
        Knowledge Object. Файл на диске один (адресация по содержимому), а очередь
        разбора и корпус — задвоены.

        Берётся самая ранняя запись: повтор должен воспроизводить первое решение,
        а не последнее.
        """
        content_hash = str(content_hash or "").strip()
        if not content_hash:
            return None
        uploader_clause = ""
        parameters: list[Any] = [user_id, content_hash]
        if scope_uploaded_by:
            if uploaded_by is None:
                # Explicit JSON null is an explicitly unknown uploader. Missing
                # legacy provenance is a different, untrusted state and must not
                # authorize replay for a new scoped upload.
                uploader_clause = "AND json_type(r.metadata_json,'$.uploaded_by')='null'"
            else:
                uploader_clause = "AND json_extract(r.metadata_json,'$.uploaded_by')=?"
                parameters.append(str(uploaded_by))
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.content_type='file' AND r.content_hash=?
                   {uploader_clause}
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}
                 ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",  # nosec B608
            tuple(parameters),
        ).fetchone()
        return dict(row) if row else None

    def find_fresh_agent_candidate(
        self,
        user_id: str,
        source: str,
        candidate_type: str,
        content_hash: str,
        *,
        requested_by: str = "",
        since: str = "",
    ) -> dict[str, Any] | None:
        """То же предложение агента, ещё ждущее разбора. Брат `find_file_by_content_hash`.

        Та же ошибка, что с пересланными документами, и в том же конвейере:
        `memory_save` и `entity_create` передавали `source_ref = new_id("toolref")`,
        то есть СВЕЖИЙ ключ на каждый вызов. Ключ происхождения не совпадал сам с
        собой никогда, ветка повтора была недостижима, и два одинаковых вызова
        подряд давали два Raw Object и две одинаковые карточки во входящих.
        Замерено 2026-08-04: повтор `memory_save` и `entity_create` с теми же
        аргументами добавлял по строке каждый раз.

        Границы, каждая со своей причиной:

        * `content_hash` — естественный ключ, он уже вычисляется и уже лежит в
          строке; выдумывать новое поле незачем;
        * `candidate_type` — заметка и сущность делят один `source='agent_tool'`,
          и без него предложение сущности глушило бы заметку с тем же текстом;
        * человек (`requested_by` в метаданных) — в общем архиве `user_id` один на
          всех, и просьба одного не является просьбой другого;
        * `since` — окно свежести. Замеренный дефект это повтор в одном ходу;
          через две недели человек вправе сказать то же самое СНОВА, и это новая
          запись, а не дубль. Без окна ключ становится вечным, а его длину задавал
          бы не замысел, а то, насколько человек запустил очередь разбора;
        * только `pending`. Ответ «это уже лежит во входящих» обязан быть правдой:
          разобранной карточки там уже нет.

        Продвинутая карточка ключ не держит намеренно. Человек с предложением
        согласился, повтор — новая просьба; совпадение уже принятого разбирает
        отдельный механизм ближних дублей.
        """
        content_hash = str(content_hash or "").strip()
        if not content_hash:
            return None
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
               JOIN inbox i ON i.raw_object_id = r.id AND i.user_id = r.user_id
               WHERE r.user_id=? AND r.source=? AND r.content_hash=?
                 AND COALESCE(json_extract(r.metadata_json,'$.candidate_type'),'')=?
                 AND COALESCE(json_extract(r.metadata_json,'$.requested_by'),'')=?
                 AND r.received_at > ?
                 AND r.deleted_at IS NULL AND i.status='pending'
                 AND {_not_private_raw_dependency("r")}
                 AND {_not_private_inbox_dependency("i")}
               ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",
            (user_id, source, content_hash, candidate_type, str(requested_by or ""), since),
        ).fetchone()
        return dict(row) if row else None

    def find_file_by_extracted_text(
        self,
        user_id: str,
        text_hash: str,
        *,
        uploaded_by: str | None = None,
        scope_uploaded_by: bool = False,
    ) -> dict[str, Any] | None:
        """Тот же ДОКУМЕНТ, пришедший другим файлом.

        `find_file_by_content_hash` сравнивает байты, а один и тот же документ,
        пересохранённый из Word или положенный в две папки, даёт другие байты при
        том же содержимом. Замерено на живом архиве 2026-08-03: из 200 конфликтов
        «почти-дубликат», ждавших разбора, **56 пар имели побайтово одинаковый
        извлечённый текст, и ни одна из них не совпадала по хешу файла**. Все 56
        пришли одним импортом папки 29 июля.

        То есть очередь на двести решений система создала себе сама, и решать в
        этих парах было нечего: это один документ в нескольких экземплярах.

        Сравнивается НОРМАЛИЗОВАННЫЙ текст (пробелы схлопнуты): разница в
        переносах строк между экспортом из Word и из PDF — не разница в
        документе. Регистр НЕ сбрасывается: «Приказ №214» и «ПРИКАЗ №214» это
        разные написания, и решать за человека, что они одно и то же, здесь
        нельзя — для таких пар и существует очередь разбора.
        """
        text_hash = str(text_hash or "").strip()
        if not text_hash:
            return None
        uploader_clause = ""
        parameters: list[Any] = [user_id, text_hash]
        if scope_uploaded_by:
            if uploaded_by is None:
                uploader_clause = "AND json_type(r.metadata_json,'$.uploaded_by')='null'"
            else:
                uploader_clause = "AND json_extract(r.metadata_json,'$.uploaded_by')=?"
                parameters.append(str(uploaded_by))
        row = self.execute(
            f"""SELECT r.* FROM raw_objects r
                 WHERE r.user_id=? AND r.content_type='file'
                   AND json_extract(r.metadata_json,'$.text_sha256')=?
                   {uploader_clause}
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}
                 ORDER BY r.received_at ASC, r.id ASC LIMIT 1""",  # nosec B608
            tuple(parameters),
        ).fetchone()
        return dict(row) if row else None

    def store_raw_object(self, obj: RawObject) -> RawObject:
        self.ensure_user(obj.user_id)
        if not obj.content_hash:
            obj.content_hash = hashlib.sha256(obj.raw_content.encode("utf-8", errors="replace")).hexdigest()
        try:
            with self.transaction() as conn:
                conn.execute(
                    """INSERT INTO raw_objects(id, user_id, source, source_ref, raw_content,
                       content_type, metadata_json, content_hash, version, received_at, created_at, deleted_at)
                       VALUES(:id, :user_id, :source, :source_ref, :raw_content,
                       :content_type, :metadata_json, :content_hash, :version, :received_at, :created_at, :deleted_at)""",
                    obj.to_row(),
                )
                visible = conn.execute(
                    f"""SELECT 1 FROM raw_objects r WHERE r.id=? AND r.user_id=?
                          AND {_not_private_raw_dependency("r")}""",  # nosec B608
                    (obj.id, obj.user_id),
                ).fetchone()
                if visible is None:
                    raise PrivateMaterialQuarantineError("Raw object fields reference private graph material")
            return obj
        except sqlite3.IntegrityError:
            existing = self.find_raw_by_source_ref(obj.user_id, obj.source, obj.source_ref)
            if existing:
                existing_hash = str(existing.get("content_hash") or "")
                if (
                    obj.content_hash
                    and existing_hash
                    and not hmac.compare_digest(obj.content_hash, existing_hash)
                ):
                    raise SourceReferenceConflictError(
                        "source_ref is already bound to different content"
                    ) from None
                return self._raw_from_row(existing)
            raise

    def relativize_stored_paths(self, files_root: str) -> dict[str, int]:
        """Переписать абсолютные пути к файлам в относительные корню хранилища.

        Абсолютный путь привязывает архив к машине. Замерено на архиве владельца: у
        всех 1671 документа в метаданных лежали абсолютные пути (3342 штуки, ни одного
        относительного), укоренённые в прежнем каталоге. После переезда, смены
        `FRIDAY_HOME` или даже имени пользователя каждый файл отдавал бы 404 —
        неотличимый от «файла нет», то есть полный отказ, а не деградация.

        Правка формы записи спасает только БУДУЩИЕ документы; этот проход чинит уже
        записанные. Трогаются ровно те пути, что лежат ВНУТРИ текущего хранилища:
        путь вне его — либо чужой, либо след прошлого переезда, и молча превращать
        его в относительный значило бы соврать о том, где файл.
        """
        root = str(files_root).rstrip("/") + "/"
        changed = 0
        scanned = 0
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, metadata_json FROM raw_objects WHERE metadata_json LIKE ?",
                (f'%"{root[:-1]}%',),
            ).fetchall()
            for row in rows:
                scanned += 1
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(metadata, dict):
                    continue
                touched = False
                for field in ("stored_path", "import_source_path"):
                    value = str(metadata.get(field) or "")
                    # `import_source_path` — это провенанс, откуда файл ПРИШЁЛ, а не
                    # где он лежит. Его трогать нельзя: он и должен остаться таким,
                    # каким был на исходной машине.
                    if field != "stored_path" or not value.startswith(root):
                        continue
                    metadata[field] = value[len(root) :]
                    touched = True
                if touched:
                    conn.execute(
                        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), row["id"]),
                    )
                    changed += 1
        return {"scanned": scanned, "changed": changed}

    def search_raw_objects(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        include_content: bool = False,
        uploaded_by: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search over SOURCE text, obeying the Inbox verdict.

        `raw_objects` holds the original ingested characters; the Knowledge Object
        holds a normalised, often summarised version. Measured on the owner's
        database, 93% of ingested characters lived only in the former and no index
        covered them, so an exact phrase from a PDF was unfindable once the review
        step had condensed it.

        **IGNORED material is not reachable here.** DATA_LIFECYCLE §3 makes
        "игнорировать" a verdict: the Knowledge Object is soft-deleted and the
        material leaves retrieval, while the Raw Object survives *for provenance*.
        Returning its text to a search would reverse 65 explicit decisions on this
        very database — the same class of resurrection already fixed three times
        (the startup migration re-linking ignored rows, the vault keeping plaintext
        of soft-deleted objects, and three review-gate bypasses).

        Soft-deleted raw objects are excluded for the same reason. `pending`,
        `classified` and `archived` ARE reachable: pending is material awaiting a
        decision, and archived is Inbox tidying that explicitly leaves the object
        alone.

        The test is ``NOT EXISTS ... status='ignored'``, not a join on the current
        status, because one Raw Object can carry SEVERAL Inbox rows — `ingest_text`
        returns the existing raw object on an idempotent replay while still creating
        a review row. A join then produced the object once per row and let it
        through whenever any one of them was not the rejection. Any rejection hides
        it; that is the direction to be wrong in.

        ``include_content`` is an internal atomic projection for the explicit
        agent tool.  Keeping the source body in this same filtered SELECT avoids a
        second-read race with an Inbox rejection.  Public API/CLI callers leave it
        false and never receive the private ``_raw_*`` fields.
        """
        text = " ".join((query or "").split()).strip()
        if not text or not self._fts_available:
            return []
        terms = _fts_terms(text)
        if not terms:
            return []
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        uploader_clause = ""
        parameters: list[Any] = [user_id]
        if uploaded_by is not None:
            # In a shared archive the tenant and the person who supplied a file
            # are different authority axes.  Apply the exact source provenance
            # before FTS LIMIT; a Python post-filter would let another person's
            # rows fill the finite page and, worse, would already have projected
            # their source body into this process lane.
            uploader_clause = f"""AND r.content_type='file'
                       AND {_exact_uploader_raw_dependency("r")}
                       AND {_not_audio_document("r")}"""
            parameters.append(str(uploaded_by))
        try:
            content_projection = (
                ", r.content_hash, r.raw_content AS _raw_content, r.metadata_json AS _raw_metadata"
                if include_content
                else ""
            )
            rows = self.execute(
                f"""SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at
                          {content_projection},
                          snippet(raw_fts, 0, '', '', '…', 24) AS excerpt,
                          (SELECT i2.status FROM inbox i2 WHERE i2.raw_object_id=r.id
                            AND i2.user_id=r.user_id
                            AND {_not_private_inbox_dependency("i2")}
                            ORDER BY i2.reviewed_at DESC, i2.created_at DESC LIMIT 1) AS inbox_status,
                          (SELECT k.id FROM knowledge_objects k
                            WHERE k.raw_object_id=r.id AND k.user_id=r.user_id
                              AND k.deleted_at IS NULL
                              AND {_not_private_knowledge_dependency("k")}
                            ORDER BY k.version DESC LIMIT 1) AS knowledge_object_id
                   FROM raw_fts
                   JOIN raw_objects r ON r.rowid=raw_fts.rowid
                   WHERE r.user_id=? AND r.deleted_at IS NULL
                     AND {_not_private_raw_dependency("r")}
                     {uploader_clause}
                     AND NOT EXISTS (
                         SELECT 1 FROM inbox i
                          WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                            AND i.status='ignored'
                     )
                     AND raw_fts MATCH ?
                   ORDER BY bm25(raw_fts) ASC, r.received_at DESC
                   LIMIT ?""",  # nosec B608
                (*parameters, match_query, max(1, min(limit, 100))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def get_searchable_file_sources(
        self,
        user_id: str,
        raw_ids: list[str],
        *,
        uploaded_by: str | None = None,
        limit: int = 100,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        """Re-authorize semantic Raw candidates in one verdict-aware read.

        Dense recall and the cross-encoder rank Knowledge Objects.  A source
        excerpt, however, must come from the immutable Raw Object, and the Inbox
        verdict can change between those two steps.  This helper accepts only a
        bounded set of already recalled opaque ids and re-applies tenant,
        privacy, soft-delete, file-kind, audio and ``ignored`` predicates before
        projecting any body.  In a shared archive ``uploaded_by`` is an exact,
        mandatory second boundary.

        The returned order is the caller's order, so a validated reranker may
        reorder candidates but can neither add a new id nor replace its canonical
        source bytes.
        """

        page_size = max(1, min(int(limit), 100))
        ordered_ids = list(
            dict.fromkeys(str(raw_id or "").strip() for raw_id in raw_ids if str(raw_id or "").strip())
        )[:page_size]
        if not ordered_ids:
            return []

        uploader_clause = ""
        parameters: list[Any] = [str(user_id), *ordered_ids]
        if uploaded_by is not None:
            uploader_clause = f"""AND {_exact_uploader_raw_dependency("r")}
                   AND EXISTS (
                       SELECT 1 FROM users exact_file_uploader
                        WHERE exact_file_uploader.id=?
                          AND exact_file_uploader.status='active'
                   )"""
            parameters.extend((str(uploaded_by), str(uploaded_by)))
        placeholders = ",".join("?" for _raw_id in ordered_ids)
        content_projection = (
            ", r.raw_content AS _raw_content, r.metadata_json AS _raw_metadata" if include_content else ""
        )
        rows = self.execute(
            f"""SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at,
                       r.content_hash
                       {content_projection},
                       (SELECT i2.status FROM inbox i2
                         WHERE i2.raw_object_id=r.id AND i2.user_id=r.user_id
                           AND {_not_private_inbox_dependency("i2")}
                         ORDER BY i2.reviewed_at DESC, i2.created_at DESC LIMIT 1) AS inbox_status,
                       (SELECT k.id FROM knowledge_objects k
                         WHERE k.raw_object_id=r.id AND k.user_id=r.user_id
                           AND k.deleted_at IS NULL
                           AND {_not_private_knowledge_dependency("k")}
                         ORDER BY k.version DESC LIMIT 1) AS knowledge_object_id
                  FROM raw_objects r
                 WHERE r.user_id=? AND r.id IN ({placeholders})
                   AND r.deleted_at IS NULL AND r.content_type='file'
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )
                   {uploader_clause}""",  # nosec B608 - placeholders and fixed privacy clauses only
            tuple(parameters),
        ).fetchall()
        found = {str(row["id"]): dict(row) for row in rows}
        return [found[raw_id] for raw_id in ordered_ids if raw_id in found]

    def resolve_public_file_citation_sources(
        self,
        user_id: str,
        knowledge_ids: Sequence[str],
        *,
        limit: int = _PUBLIC_FILE_CITATION_MAX,
    ) -> list[PublicFileCitationSource]:
        """Ordered all-or-none: live public KO → public non-audio file Raw + uploader.

        Returns only opaque ids (knowledge_object_id, raw_object_id, uploaded_by).
        Any missing, deleted, private, ignored, audio, inactive-uploader or
        malformed-metadata member closes the whole set. Order matches the caller
        list; the page is hard-capped at twelve.
        """

        page_size = max(1, min(int(limit), _PUBLIC_FILE_CITATION_MAX))
        ordered_ids: list[str] = []
        for value in knowledge_ids:
            knowledge_id = str(value or "").strip()
            if not knowledge_id or not _KNOWLEDGE_OBJECT_ID_RE.fullmatch(knowledge_id):
                return []
            if knowledge_id in ordered_ids:
                return []
            ordered_ids.append(knowledge_id)
            if len(ordered_ids) > page_size:
                return []
        if not ordered_ids:
            return []

        tenant = str(user_id or "").strip()
        if not tenant:
            return []

        uploader_expr = _bounded_raw_uploader_expression("r")
        placeholders = ",".join("?" for _item in ordered_ids)
        rows = self.execute(
            f"""SELECT k.id AS knowledge_object_id,
                       r.id AS raw_object_id,
                       {uploader_expr} AS uploaded_by
                  FROM knowledge_objects k
                  JOIN raw_objects r
                    ON r.id=k.raw_object_id
                   AND r.user_id=k.user_id
                 WHERE k.user_id=?
                   AND k.id IN ({placeholders})
                   AND k.deleted_at IS NULL
                   AND {_not_private_knowledge_dependency("k")}
                   AND r.deleted_at IS NULL
                   AND r.content_type='file'
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )
                   AND {uploader_expr} IS NOT NULL
                   AND {uploader_expr}<>''
                   AND EXISTS (
                       SELECT 1 FROM users exact_citation_uploader
                        WHERE exact_citation_uploader.id={uploader_expr}
                          AND exact_citation_uploader.status='active'
                   )""",  # nosec B608 - placeholders and fixed privacy clauses only
            (tenant, *ordered_ids),
        ).fetchall()
        found = {
            str(row["knowledge_object_id"]): (
                str(row["raw_object_id"] or ""),
                str(row["uploaded_by"] or ""),
            )
            for row in rows
        }
        if len(found) != len(ordered_ids):
            return []
        result: list[PublicFileCitationSource] = []
        for knowledge_id in ordered_ids:
            pair = found.get(knowledge_id)
            if pair is None:
                return []
            raw_id, uploaded_by = pair
            if not raw_id or not uploaded_by:
                return []
            try:
                validated_uploader = validate_user_id(uploaded_by)
            except ValueError:
                return []
            result.append(
                PublicFileCitationSource(
                    knowledge_object_id=knowledge_id,
                    raw_object_id=raw_id,
                    uploaded_by=validated_uploader,
                )
            )
        return result

    def search_raw_objects_in_set(
        self,
        user_id: str,
        query: str,
        raw_ids: list[str],
        *,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """FTS over an already-authorized Raw-id set, scoped before SQL LIMIT."""

        text = " ".join((query or "").split()).strip()
        # The global body-free document catalog is bounded at 5,000 entries.
        # Truncating this authorized id set back to the old 1,000-message
        # conversation horizon would make a unique older file impossible to
        # select by content even though its exact id is already in scope.
        ordered_ids = list(dict.fromkeys(str(raw_id or "").strip() for raw_id in raw_ids if raw_id))[:5_000]
        if not text or not ordered_ids or not self._fts_available:
            return []
        terms = _fts_terms(text)
        if not terms:
            return []
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        found: dict[str, dict[str, Any]] = {}
        page_size = max(1, min(int(limit), 100))
        try:
            for offset in range(0, len(ordered_ids), 400):
                batch = ordered_ids[offset : offset + 400]
                placeholders = ",".join("?" for _item in batch)
                rows = self.execute(
                    f"""SELECT r.id, r.source, r.source_ref, r.content_type, r.received_at,
                              bm25(raw_fts) AS rank
                           FROM raw_fts
                           JOIN raw_objects r ON r.rowid=raw_fts.rowid
                          WHERE r.user_id=? AND r.deleted_at IS NULL
                            AND r.id IN ({placeholders})
                            AND {_not_private_raw_dependency("r")}
                            AND NOT EXISTS (
                                SELECT 1 FROM inbox i
                                 WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                                   AND i.status='ignored'
                            )
                            AND raw_fts MATCH ?
                          ORDER BY rank ASC, r.received_at DESC
                          LIMIT ?""",  # nosec B608
                    (user_id, *batch, match_query, page_size),
                ).fetchall()
                for row in rows:
                    found[str(row["id"])] = dict(row)
        except sqlite3.OperationalError:
            return []
        return sorted(found.values(), key=lambda item: (float(item.get("rank") or 0.0), str(item["id"])))[
            :page_size
        ]

    def get_raw_object(self, raw_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            row = self.execute(
                f"""SELECT r.* FROM raw_objects r WHERE r.id=?
                      AND {_not_private_raw_dependency("r")}""",  # nosec B608
                (raw_id,),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT r.* FROM raw_objects r WHERE r.id=? AND r.user_id=?
                      AND {_not_private_raw_dependency("r")}""",  # nosec B608
                (raw_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def get_raw_object_descriptors(
        self,
        raw_ids: list[str],
        user_id: str,
        *,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Batch the same body-free descriptor read while preserving caller order."""

        ordered = list(dict.fromkeys(str(raw_id or "").strip() for raw_id in raw_ids if raw_id))[
            : max(1, min(int(limit), 1_000))
        ]
        if not ordered:
            return []
        found: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(ordered), 400):
            batch = ordered[offset : offset + 400]
            placeholders = ",".join("?" for _item in batch)
            rows = self.execute(
                f"""SELECT r.id, r.user_id, r.content_type, r.metadata_json, r.received_at
                      FROM raw_objects r
                     WHERE r.user_id=? AND r.deleted_at IS NULL
                       AND r.id IN ({placeholders})
                       AND {_not_private_raw_dependency("r")}""",  # nosec B608
                (user_id, *batch),
            ).fetchall()
            found.update((str(row["id"]), dict(row)) for row in rows)
        return [found[raw_id] for raw_id in ordered if raw_id in found]

    def list_owned_file_catalog(
        self,
        user_id: str,
        uploaded_by: str,
        *,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        """Return a body-free, uploader-scoped catalog across conversations.

        Conversation pointers are an efficient continuity cache, not the
        archive boundary.  An exact filename or unique content clue must still
        be able to locate a file first mentioned in another conversation.  The
        SQL applies person/privacy/ignored/audio filters *before* its finite
        page, so another participant's corpus cannot crowd out the caller's
        files or reveal their existence.
        """

        # One internal caller asks for 5,001 rows so the 5,001st acts only as a
        # completeness sentinel for a 5,000-file deterministic catalog.  The
        # public/default page remains 5,000 and no body text is projected.
        page_size = max(1, min(int(limit), 5_001))
        rows = self.execute(
            f"""SELECT r.id, r.content_type, r.received_at,
                       CASE WHEN typeof(r.metadata_json)='text'
                                  AND json_valid(r.metadata_json)
                            THEN substr(
                                     COALESCE(json_extract(r.metadata_json,'$.filename'),''),
                                     1,
                                     260
                                 )
                            ELSE ''
                        END AS filename
                 FROM raw_objects r
                 WHERE r.user_id=? AND r.deleted_at IS NULL
                   AND r.content_type='file'
                   AND {_exact_uploader_raw_dependency("r")}
                   AND EXISTS (
                       SELECT 1 FROM users uploader_user
                        WHERE uploader_user.id=? AND uploader_user.status='active'
                   )
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )
                 ORDER BY r.received_at ASC, r.rowid ASC
                 LIMIT ?""",  # nosec B608 - only fixed privacy predicates
            (str(user_id), str(uploaded_by), str(uploaded_by), page_size),
        ).fetchall()
        return [dict(row) for row in rows]

    def select_owned_file_corpus(
        self,
        user_id: str,
        uploaded_by: str,
        *,
        received_since: str | None = None,
        received_until: str | None = None,
        document_since: str | None = None,
        document_until: str | None = None,
        limit: int = 13,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Select one uploader's file corpus with exact scope and honest totals.

        Arrival time and a document's own date are different clocks.  Callers
        choose exactly one; mixing them is how a request for files *received*
        during a week silently became a search for dates merely mentioned in
        their bodies.  The body stays out of this selector: chosen opaque ids
        are re-authorized through ``get_searchable_file_sources`` immediately
        before use.

        Unknown uploader provenance is counted separately and can never be
        attributed to the named person.  Likewise, an own-date range reports
        that uploader's undated files, because they make exhaustive range
        coverage unknowable even though they cannot be selected into it.
        """

        tenant = str(user_id or "").strip()
        person = str(uploaded_by or "").strip()
        if not tenant or not person:
            return {"items": [], "total": 0, "unattributed": 0, "undated": 0, "time_role": ""}
        received_window = bool(received_since or received_until)
        document_window = bool(document_since or document_until)
        if received_window and document_window:
            raise ValueError("received and document date windows are mutually exclusive")

        def bounded_stamp(value: str | None, *, own_date: bool = False) -> str | None:
            if value is None:
                return None
            clean = str(value).strip()
            if not clean or len(clean) > (10 if own_date else 64):
                raise ValueError("invalid file corpus time boundary")
            if own_date:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
                    raise ValueError("invalid document date boundary")
            elif any(char not in "0123456789T:+-.Z " for char in clean):
                raise ValueError("invalid received-at boundary")
            return clean

        received_since = bounded_stamp(received_since)
        received_until = bounded_stamp(received_until)
        document_since = bounded_stamp(document_since, own_date=True)
        document_until = bounded_stamp(document_until, own_date=True)
        role = "document_date" if document_window else "received_at"
        document_date = "jericho_iso_date(json_extract(r.metadata_json,'$.document_date'))"
        time_clauses: list[str] = []
        time_params: list[Any] = []
        if received_since:
            time_clauses.append("r.received_at >= ?")
            time_params.append(received_since)
        if received_until:
            time_clauses.append("r.received_at <= ?")
            time_params.append(received_until)
        if document_since:
            time_clauses.append(f"{document_date} >= ?")
            time_params.append(document_since)
        if document_until:
            time_clauses.append(f"{document_date} <= ?")
            time_params.append(document_until)
        time_sql = "" if not time_clauses else " AND " + " AND ".join(time_clauses)
        base = f"""r.user_id=? AND r.deleted_at IS NULL
                   AND r.content_type='file'
                   AND json_valid(r.metadata_json)
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )"""
        exact = f"{base} AND {_exact_uploader_raw_dependency('r')}{time_sql}"
        params = (tenant, person, *time_params)
        total_row = self.execute(
            f"SELECT COUNT(*) AS count FROM raw_objects r WHERE {exact}",  # nosec B608
            params,
        ).fetchone()
        total = int(total_row["count"] if total_row else 0)
        page_size = max(1, min(int(limit), 100))
        page_offset = max(0, int(offset))
        rows = self.execute(
            f"""SELECT r.id, r.received_at,
                       {document_date} AS document_date
                  FROM raw_objects r
                 WHERE {exact}
                 ORDER BY r.received_at DESC, r.rowid DESC
                 LIMIT ? OFFSET ?""",  # nosec B608
            (*params, page_size, page_offset),
        ).fetchall()

        # Missing author evidence is counted only, never returned as a
        # candidate.  On an arrival-time request the same closed window applies;
        # on an own-date request an unattributed or undated row cannot safely be
        # placed inside or outside the range, so the all-time count is the honest
        # completeness ceiling.
        missing_author = (
            "(json_type(r.metadata_json,'$.uploaded_by') IS NULL OR "
            "json_extract(r.metadata_json,'$.uploaded_by')='')"
        )
        unknown_time_sql = time_sql if role == "received_at" else ""
        unknown_params = (tenant, *(time_params if role == "received_at" else ()))
        unattributed_row = self.execute(
            f"""SELECT COUNT(*) AS count FROM raw_objects r
                 WHERE {base} AND {missing_author}{unknown_time_sql}""",  # nosec B608
            unknown_params,
        ).fetchone()
        undated = 0
        if role == "document_date":
            undated_row = self.execute(
                f"""SELECT COUNT(*) AS count FROM raw_objects r
                     WHERE {base} AND {_exact_uploader_raw_dependency("r")}
                       AND {document_date} IS NULL""",  # nosec B608
                (tenant, person),
            ).fetchone()
            undated = int(undated_row["count"] if undated_row else 0)
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "unattributed": int(unattributed_row["count"] if unattributed_row else 0),
            "undated": undated,
            "time_role": role,
            "offset": page_offset,
            "page_complete": page_offset + len(rows) >= total,
        }

    def find_owned_files_by_filename(
        self,
        user_id: str,
        uploaded_by: str,
        filename: str,
    ) -> list[dict[str, Any]]:
        """Find zero, one or two exact filename matches for one uploader.

        Two rows are sufficient to prove ambiguity, while returning a larger
        page would invite callers to confuse a display limit with uniqueness.
        SQLite's built-in ``NOCASE`` is ASCII-only, so the comparison uses the
        connection's NFC-aware Unicode casefold function.  Every authority and
        lifecycle predicate is applied before the fixed ``LIMIT 2``.
        """

        tenant = str(user_id or "").strip()
        person = str(uploaded_by or "").strip()
        clean_filename = str(filename or "").strip()
        if not tenant or not person or not clean_filename or len(clean_filename) > 260:
            return []
        base = f"""r.user_id=? AND r.deleted_at IS NULL
                   AND r.content_type='file'
                   AND json_valid(r.metadata_json)
                   AND json_type(r.metadata_json,'$.uploaded_by')='text'
                   AND json_extract(r.metadata_json,'$.uploaded_by')=?
                   AND EXISTS (
                       SELECT 1 FROM users exact_filename_uploader
                        WHERE exact_filename_uploader.id=?
                          AND exact_filename_uploader.status='active'
                   )
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )"""
        rows = self.execute(
            f"""WITH candidates AS (
                       SELECT r.id, r.content_type, r.received_at,
                              substr(json_extract(r.metadata_json,'$.filename'),1,260) AS filename,
                              1 AS lane
                         FROM raw_objects r
                        WHERE {base}
                          AND json_type(r.metadata_json,'$.filename')='text'
                          AND length(json_extract(r.metadata_json,'$.filename')) BETWEEN 1 AND 260
                          AND replace(jericho_casefold(
                                  substr(json_extract(r.metadata_json,'$.filename'),1,261)
                              ),'ё','е')=replace(jericho_casefold(?),'ё','е')
                       UNION ALL
                       SELECT r.id, r.content_type, r.received_at,
                              a.supplied_filename AS filename, 0 AS lane
                         FROM file_source_aliases a
                         JOIN raw_objects r ON r.id=a.raw_object_id
                        WHERE a.user_id=? AND a.uploaded_by=?
                          AND {base}
                          AND length(a.supplied_filename) BETWEEN 1 AND 260
                          AND replace(jericho_casefold(a.supplied_filename),'ё','е')=
                              replace(jericho_casefold(?),'ё','е')
                   ),
                   ranked AS (
                       SELECT *, ROW_NUMBER() OVER (
                           PARTITION BY id ORDER BY lane ASC, received_at ASC
                       ) AS _choice
                         FROM candidates
                   )
                   SELECT id, content_type, received_at, filename
                     FROM ranked WHERE _choice=1
                    ORDER BY received_at ASC, id ASC
                    LIMIT 2""",  # nosec B608 - only fixed privacy predicates
            (
                tenant,
                person,
                person,
                clean_filename,
                tenant,
                person,
                tenant,
                person,
                person,
                clean_filename,
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_owned_file_content(
        self,
        user_id: str,
        uploaded_by: str,
        query: str,
        *,
        limit: int = 64,
    ) -> dict[str, Any]:
        """Search one uploader's file bodies with an explicit sentinel row.

        The returned ``results`` page never exceeds 64 rows.  SQL asks for one
        additional row, so ``complete`` can prove whether the bounded page
        contains every match instead of treating a saturated page as unique or
        exhaustive.  Invalid/unavailable FTS returns ``available=False`` and
        therefore never proves absence.
        """

        page_size = max(1, min(int(limit), 64))
        empty_page: dict[str, Any] = {
            "results": [],
            "complete": False,
            "available": False,
            "limit": page_size,
            "matched_at_least": 0,
        }
        tenant = str(user_id or "").strip()
        person = str(uploaded_by or "").strip()
        text = " ".join(str(query or "").split()).strip()
        if not tenant or not person or not text or not self._fts_available:
            return empty_page
        terms = _fts_terms(text)
        if not terms:
            return empty_page
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        try:
            rows = self.execute(
                f"""SELECT r.id, r.content_type, r.received_at,
                           substr(json_extract(r.metadata_json,'$.filename'),1,260) AS filename,
                           bm25(raw_fts) AS rank
                      FROM raw_fts
                      JOIN raw_objects r ON r.rowid=raw_fts.rowid
                     WHERE r.user_id=? AND r.deleted_at IS NULL
                       AND r.content_type='file'
                       AND json_valid(r.metadata_json)
                       AND json_type(r.metadata_json,'$.uploaded_by')='text'
                       AND json_extract(r.metadata_json,'$.uploaded_by')=?
                       AND {_not_audio_document("r")}
                       AND {_not_private_raw_dependency("r")}
                       AND NOT EXISTS (
                           SELECT 1 FROM inbox i
                            WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                              AND i.status='ignored'
                       )
                       AND raw_fts MATCH ?
                     ORDER BY rank ASC, r.received_at DESC, r.id ASC
                     LIMIT ?""",  # nosec B608 - only fixed privacy predicates
                (tenant, person, match_query, page_size + 1),
            ).fetchall()
        except sqlite3.OperationalError:
            return empty_page
        complete = len(rows) <= page_size
        return {
            "results": [dict(row) for row in rows[:page_size]],
            "complete": complete,
            "available": True,
            "limit": page_size,
            "matched_at_least": len(rows),
        }

    def search_owned_files_by_term(
        self,
        user_id: str,
        uploaded_by: str,
        query: str,
        *,
        limit: int = 64,
    ) -> dict[str, Any]:
        """Union exact-uploader filename substring and body FTS matches.

        Each lane receives its own sentinel before the deterministic merge, so
        a dense body page cannot crowd filename matches out.  Both lanes keep
        tenant, uploader, lifecycle, private, ignored and audio predicates in
        SQL.  Raw body text is never projected.
        """

        page_size = max(1, min(int(limit), 64))
        tenant = str(user_id or "").strip()
        person = str(uploaded_by or "").strip()
        text = " ".join(str(query or "").split()).strip()
        empty: dict[str, Any] = {
            "results": [],
            "filename_results": [],
            "complete": False,
            "available": False,
            "filename_complete": False,
            "filename_matched_at_least": 0,
            "filename_total": None,
            "content_complete": False,
            "limit": page_size,
            "matched_at_least": 0,
            "total": None,
        }
        if not tenant or not person or not text or len(text) > 260:
            return empty
        escaped = text.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        filename_like = f"%{escaped}%"
        base = f"""r.user_id=? AND r.deleted_at IS NULL
                   AND r.content_type='file'
                   AND json_valid(r.metadata_json)
                   AND json_type(r.metadata_json,'$.uploaded_by')='text'
                   AND json_extract(r.metadata_json,'$.uploaded_by')=?
                   AND EXISTS (
                       SELECT 1 FROM users uploader_user
                        WHERE uploader_user.id=? AND uploader_user.status='active'
                   )
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox i
                        WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                          AND i.status='ignored'
                   )"""
        base_params = (tenant, person, person)

        def filename_rows_only() -> list[sqlite3.Row]:
            return self.execute(
                f"""WITH alias_hits AS (
                           SELECT r.id, r.content_type, r.received_at,
                                  a.supplied_filename AS filename,
                                  'filename_alias' AS match_kind, 0 AS lane, 0.0 AS rank
                             FROM file_source_aliases a
                             JOIN raw_objects r ON r.id=a.raw_object_id
                            WHERE a.user_id=? AND a.uploaded_by=?
                              AND {base}
                              AND length(a.supplied_filename) BETWEEN 1 AND 260
                              AND jericho_casefold(a.supplied_filename)
                                  LIKE jericho_casefold(?) ESCAPE '\\'
                            ORDER BY a.created_at DESC, a.source_ref ASC
                            LIMIT ?
                       ),
                       canonical_hits AS (
                           SELECT r.id, r.content_type, r.received_at,
                                  substr(json_extract(r.metadata_json,'$.filename'),1,260) AS filename,
                                  'filename' AS match_kind, 1 AS lane, 0.0 AS rank
                             FROM raw_objects r
                            WHERE {base}
                              AND json_type(r.metadata_json,'$.filename')='text'
                              AND length(json_extract(r.metadata_json,'$.filename')) BETWEEN 1 AND 260
                              AND jericho_casefold(
                                      substr(json_extract(r.metadata_json,'$.filename'),1,261)
                                  ) LIKE jericho_casefold(?) ESCAPE '\\'
                            ORDER BY r.received_at DESC, r.rowid DESC
                            LIMIT ?
                       )
                       SELECT * FROM alias_hits
                       UNION ALL
                       SELECT * FROM canonical_hits
                       ORDER BY lane ASC, received_at DESC, id ASC""",  # nosec B608
                (
                    tenant,
                    person,
                    *base_params,
                    filename_like,
                    page_size + 1,
                    *base_params,
                    filename_like,
                    page_size + 1,
                ),
            ).fetchall()

        def merge_filename_rows(rows: Sequence[sqlite3.Row]) -> tuple[list[dict[str, Any]], bool]:
            alias_rows = [row for row in rows if str(row["match_kind"]) == "filename_alias"]
            canonical_rows = [row for row in rows if str(row["match_kind"]) == "filename"]
            lane_complete = len(alias_rows) <= page_size and len(canonical_rows) <= page_size
            merged: dict[str, dict[str, Any]] = {}
            for row in [*alias_rows[:page_size], *canonical_rows[:page_size]]:
                item = dict(row)
                raw_id = str(item.get("id") or "")
                kind = str(item.pop("match_kind", "") or "")
                item.pop("lane", None)
                item.pop("rank", None)
                if raw_id in merged:
                    kinds = merged[raw_id]["match_kinds"]
                    if kind and kind not in kinds:
                        kinds.append(kind)
                else:
                    item["match_kinds"] = [kind] if kind else []
                    merged[raw_id] = item
            return list(merged.values()), lane_complete

        terms = _fts_terms(text)
        if not self._fts_available or not terms:
            try:
                filename_rows = filename_rows_only()
            except sqlite3.OperationalError:
                return empty
            merged_names, filename_complete = merge_filename_rows(filename_rows)
            return {
                "results": merged_names[:page_size],
                "filename_results": merged_names[:page_size],
                "complete": False,
                "available": False,
                "filename_complete": filename_complete,
                "filename_matched_at_least": len(merged_names),
                "filename_total": (
                    len(merged_names) if filename_complete and len(merged_names) <= page_size else None
                ),
                "content_complete": False,
                "limit": page_size,
                "matched_at_least": len(merged_names),
                "total": None,
            }
        match_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        try:
            rows = self.execute(
                f"""WITH alias_hits AS (
                           SELECT r.id, r.content_type, r.received_at,
                                  a.supplied_filename AS filename,
                                  'filename_alias' AS match_kind, 0 AS lane, 0.0 AS rank
                             FROM file_source_aliases a
                             JOIN raw_objects r ON r.id=a.raw_object_id
                            WHERE a.user_id=? AND a.uploaded_by=?
                              AND {base}
                              AND length(a.supplied_filename) BETWEEN 1 AND 260
                              AND jericho_casefold(a.supplied_filename)
                                  LIKE jericho_casefold(?) ESCAPE '\\'
                            ORDER BY a.created_at DESC, a.source_ref ASC
                            LIMIT ?
                       ),
                       filename_hits AS (
                           SELECT r.id, r.content_type, r.received_at,
                                  substr(json_extract(r.metadata_json,'$.filename'),1,260) AS filename,
                                  'filename' AS match_kind, 1 AS lane, 0.0 AS rank
                             FROM raw_objects r
                            WHERE {base}
                              AND json_type(r.metadata_json,'$.filename')='text'
                              AND length(json_extract(r.metadata_json,'$.filename')) BETWEEN 1 AND 260
                              AND jericho_casefold(
                                      substr(json_extract(r.metadata_json,'$.filename'),1,261)
                                  ) LIKE jericho_casefold(?) ESCAPE '\\'
                            ORDER BY r.received_at DESC, r.rowid DESC
                            LIMIT ?
                       ),
                       content_hits AS (
                           SELECT r.id, r.content_type, r.received_at,
                                  substr(COALESCE(json_extract(r.metadata_json,'$.filename'),''),1,260)
                                      AS filename,
                                  'content' AS match_kind, 2 AS lane, bm25(raw_fts) AS rank
                             FROM raw_fts
                             JOIN raw_objects r ON r.rowid=raw_fts.rowid
                            WHERE {base}
                              AND raw_fts MATCH ?
                            ORDER BY rank ASC, r.received_at DESC, r.id ASC
                            LIMIT ?
                       )
                       SELECT * FROM alias_hits
                       UNION ALL
                       SELECT * FROM filename_hits
                       UNION ALL
                       SELECT * FROM content_hits
                       ORDER BY lane ASC, rank ASC, received_at DESC, id ASC""",  # nosec B608
                (
                    tenant,
                    person,
                    *base_params,
                    filename_like,
                    page_size + 1,
                    *base_params,
                    filename_like,
                    page_size + 1,
                    *base_params,
                    match_query,
                    page_size + 1,
                ),
            ).fetchall()
        except sqlite3.OperationalError:
            try:
                filename_rows = filename_rows_only()
            except sqlite3.OperationalError:
                return empty
            merged_names, filename_complete = merge_filename_rows(filename_rows)
            return {
                "results": merged_names[:page_size],
                "filename_results": merged_names[:page_size],
                "complete": False,
                "available": False,
                "filename_complete": filename_complete,
                "filename_matched_at_least": len(merged_names),
                "filename_total": (
                    len(merged_names) if filename_complete and len(merged_names) <= page_size else None
                ),
                "content_complete": False,
                "limit": page_size,
                "matched_at_least": len(merged_names),
                "total": None,
            }

        filename_rows = [row for row in rows if str(row["match_kind"]) in {"filename_alias", "filename"}]
        content_rows = [row for row in rows if str(row["match_kind"]) == "content"]
        merged_names, filename_complete = merge_filename_rows(filename_rows)
        content_complete = len(content_rows) <= page_size
        merged: dict[str, dict[str, Any]] = {str(item["id"]): item for item in merged_names}
        for row in content_rows[:page_size]:
            item = dict(row)
            raw_id = str(item.get("id") or "")
            kind = str(item.pop("match_kind", "") or "")
            item.pop("lane", None)
            item.pop("rank", None)
            if raw_id in merged:
                kinds = merged[raw_id]["match_kinds"]
                if kind and kind not in kinds:
                    kinds.append(kind)
            else:
                item["match_kinds"] = [kind] if kind else []
                merged[raw_id] = item
        all_results = list(merged.values())
        complete = filename_complete and content_complete and len(all_results) <= page_size
        shown = all_results[:page_size]
        return {
            "results": shown,
            "filename_results": merged_names[:page_size],
            "complete": complete,
            "available": True,
            "filename_complete": filename_complete,
            "filename_matched_at_least": len(merged_names),
            "filename_total": (
                len(merged_names) if filename_complete and len(merged_names) <= page_size else None
            ),
            "content_complete": content_complete,
            "limit": page_size,
            "matched_at_least": len(all_results),
            "total": len(all_results) if complete else None,
        }

    def store_inbox_item(self, item: InboxItem) -> InboxItem:
        self.ensure_user(item.user_id)
        raw = self.get_raw_object(item.raw_object_id, item.user_id)
        if not raw:
            raise ValueError("Inbox item requires a RawObject owned by the same user")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
                   VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
                item.to_row(),
            )
        return item

    def get_inbox_item(self, inbox_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}""",  # nosec B608
            (inbox_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def get_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.raw_object_id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}
                 ORDER BY i.created_at DESC LIMIT 1""",  # nosec B608
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def find_inbox_by_raw(self, raw_object_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            f"""SELECT i.* FROM inbox i
                  JOIN raw_objects r
                    ON r.id=i.raw_object_id AND r.user_id=i.user_id
                   AND {_not_private_raw_dependency("r")}
                 WHERE i.raw_object_id=? AND i.user_id=?
                  AND {_not_private_inbox_dependency("i")}
                 ORDER BY i.created_at DESC LIMIT 1""",  # nosec B608
            (raw_object_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def count_inbox(self, user_id: str, status: InboxStatus | None = None) -> int:
        """The same two branches as the listing, so the total answers the same question."""
        if status:
            row = self.execute(
                f"""SELECT COUNT(*) AS count FROM inbox i
                     JOIN raw_objects r
                       ON r.id=i.raw_object_id AND r.user_id=i.user_id
                      AND {_not_private_raw_dependency("r")}
                     WHERE i.user_id=? AND i.status=?
                       AND {_not_private_inbox_dependency("i")}""",  # nosec B608
                (user_id, enum_value(status)),
            ).fetchone()
        else:
            row = self.execute(
                f"""SELECT COUNT(*) AS count FROM inbox i
                     JOIN raw_objects r
                       ON r.id=i.raw_object_id AND r.user_id=i.user_id
                      AND {_not_private_raw_dependency("r")}
                     WHERE i.user_id=?
                      AND {_not_private_inbox_dependency("i")}""",  # nosec B608
                (user_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def list_inbox(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # `, rowid DESC` is what makes the offset trustworthy: `created_at` is written
        # to second precision, and a bulk import stamps hundreds of rows identically —
        # the docstring of `group_pending_inbox` names a real 187-file case. Without a
        # unique tail, paging over such a batch duplicates and drops rows.
        if status:
            rows = self.execute(
                f"""SELECT i.* FROM inbox i
                   JOIN raw_objects r
                     ON r.id=i.raw_object_id AND r.user_id=i.user_id
                    AND {_not_private_raw_dependency("r")}
                   WHERE i.user_id=? AND i.status=?
                     AND {_not_private_inbox_dependency("i")}
                   ORDER BY i.created_at DESC, i.rowid DESC LIMIT ? OFFSET ?""",  # nosec B608
                (user_id, enum_value(status), max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                f"""SELECT i.* FROM inbox i
                   JOIN raw_objects r
                     ON r.id=i.raw_object_id AND r.user_id=i.user_id
                    AND {_not_private_raw_dependency("r")}
                   WHERE i.user_id=?
                     AND {_not_private_inbox_dependency("i")}
                   ORDER BY i.created_at DESC, i.rowid DESC LIMIT ? OFFSET ?""",  # nosec B608
                (user_id, max(1, min(limit, 1000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    # Axes a pending queue can usefully be cut along. Chosen from measurement, not
    # taste: on a real import of 187 files, (extension x suggested_action) collapsed to
    # 16 groups with the largest holding 154, while `promotion_score` — what the Inbox
    # currently sorts by — had p25 = median = p75 = 0.90 and separated nothing.
    #
    # `quality` добавлена потому, что она — единственный измеренный разделитель. На
    # том же импорте из 66 файлов `quality_score` дал 0.13 у 36 нечитаемых, ровно
    # 0.198 у семи base64-дампов и 0.88–0.996 у четырёх связных документов, тогда
    # как `promotion_score` — то, из чего выводится совет, — стоял на 0.90 почти у
    # всех. Признак существовал и не использовался ни в сортировке, ни в
    # группировке, ни в совете; человеку осталось 65 решений руками.
    INBOX_GROUP_AXES = ("extension", "directory", "source", "quality")

    # Границы полос качества. Не квартили: полосы обязаны быть УСТОЙЧИВЫМИ, иначе
    # «принять всё выше 0.75» означает разное на разных партиях, и решение,
    # принятое вчера, нельзя повторить сегодня.
    QUALITY_BANDS = ((0.25, "0.00–0.25 нечитаемое"), (0.50, "0.25–0.50 слабое"), (0.75, "0.50–0.75 среднее"))
    QUALITY_TOP_BAND = "0.75–1.00 содержательное"

    def group_pending_inbox(
        self,
        user_id: str,
        *,
        by: str = "extension",
        limit_ids: int = 200,
        max_groups: int = 100,
    ) -> dict[str, Any]:
        """Cut the pending queue into groups, and hand back their members.

        Возвращает не голый список, а список ВМЕСТЕ с двумя итогами: сколько
        групп получилось всего и сколько материалов в очереди. Обрез сотней
        существует (тысяча групп на экране бесполезна), но он был МОЛЧАЛИВЫМ:
        группы со сто первой исчезали, а заголовок «Группы непроверенного (N)»
        считал N по показанным — то есть уменьшался вместе с обрезом и выглядел
        полным. Человек, разбирающий импорт, видел меньше очереди, чем в ней
        есть, и не имел ни одного признака, что смотрит не всё.

        Read-only on purpose. The ids come back with each group so the caller feeds
        them to the existing bulk endpoint, which already refuses to canonize anything.
        A grouping that carried its own mutation path would be a second door into the
        review gate, and re-resolving a group by predicate at commit time would act on
        rows the user never saw — the queue changes between deciding and confirming.

        No new table either: ``purge`` hard-deletes inbox rows with foreign keys on, so
        anything REFERENCES inbox(id) without a cascade would break purge and therefore
        backups.
        """
        if by not in self.INBOX_GROUP_AXES:
            raise ValueError(f"Unknown grouping axis: {by!r}")
        validate_user_id(user_id)
        rows = self.execute(
            f"""SELECT i.id, i.suggested_action, i.quality_score, r.source, r.content_type,
                      json_extract(r.metadata_json, '$.import_source_path') AS import_path
               FROM inbox i
               JOIN raw_objects r ON r.id = i.raw_object_id AND r.user_id = i.user_id
               WHERE i.user_id = ? AND i.status = 'pending'
                 AND {_not_private_inbox_dependency("i")}
                 AND {_not_private_raw_dependency("r")}
               ORDER BY i.created_at ASC, i.rowid ASC""",  # nosec B608
            (user_id,),
        ).fetchall()

        groups: dict[str, dict[str, Any]] = {}
        qualities: dict[str, list[float]] = {}
        for row in rows:
            key = self._inbox_group_key(dict(row), by)
            group = groups.setdefault(
                key,
                {"key": key, "axis": by, "total": 0, "actions": {}, "inbox_ids": [], "truncated": False},
            )
            group["total"] += 1
            action = str(row["suggested_action"] or "unknown")
            group["actions"][action] = group["actions"].get(action, 0) + 1
            # Отсутствие оценки приравнивается к худшей ОСОЗНАННО: неоценённое
            # должно оседать к мусору, а не всплывать наверх. Форма записана явно,
            # хотя `or 0.0` дал бы то же самое — подстановка здесь ноль, а не 0.5,
            # как в том дефекте lifecycle-скана, где falsy-ноль действительно менял
            # смысл. Явность оставлена, чтобы следующая правка подстановки не
            # оказалась молчаливой.
            score = row["quality_score"]
            qualities.setdefault(key, []).append(float(score) if score is not None else 0.0)
            if len(group["inbox_ids"]) < max(1, min(int(limit_ids), 200)):
                group["inbox_ids"].append(row["id"])
            else:
                group["truncated"] = True

        # Качество кладётся в КАЖДУЮ группу, а не только в разрез по качеству:
        # именно оно отвечает на вопрос «это стоит смотреть или сносить», по какой
        # бы оси ни резали. Без него колонка «что советует классификатор» на живом
        # импорте показывала `promote: N` во всех группах — то есть ничего.
        for key, group in groups.items():
            scores = sorted(qualities.get(key) or [0.0])
            group["quality_min"] = round(scores[0], 3)
            group["quality_median"] = round(scores[len(scores) // 2], 3)
            group["quality_max"] = round(scores[-1], 3)

        ordered = sorted(groups.values(), key=lambda item: (-item["total"], item["key"]))
        shown = ordered[: max(1, int(max_groups))]
        return {
            "groups": shown,
            "groups_total": len(ordered),
            "items_total": len(rows),
        }

    @classmethod
    def quality_band(cls, score: float | None) -> str:
        value = float(score) if score is not None else 0.0
        for edge, label in cls.QUALITY_BANDS:
            if value < edge:
                return label
        return cls.QUALITY_TOP_BAND

    @staticmethod
    def _inbox_group_key(row: dict[str, Any], by: str) -> str:
        path = str(row.get("import_path") or "")
        if by == "quality":
            return IntakeMixin.quality_band(row.get("quality_score"))
        if by == "source":
            return str(row.get("source") or "unknown")
        if by == "directory":
            # The immediate parent is what a person recognises ("Документы/Договоры"),
            # where the full path is unique per file and groups nothing.
            return str(PurePosixPath(path).parent) if path else "(не из импорта)"
        suffix = PurePosixPath(path).suffix.lower() if path else ""
        if suffix:
            return suffix
        content_type = str(row.get("content_type") or "").split(";", 1)[0].strip()
        return content_type or "(без типа)"

    def list_inbox_detailed(
        self,
        user_id: str,
        status: InboxStatus | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = [
            "i.user_id=?",
            _not_private_inbox_dependency("i"),
            _not_private_raw_dependency("r"),
        ]
        params: list[Any] = [user_id]
        if status:
            clauses.append("i.status=?")
            params.append(enum_value(status))
        params.extend([max(1, min(limit, 1000)), max(0, offset)])
        # ``clauses`` contains only fixed predicates; all values remain bound parameters.
        query = f"""SELECT i.*, r.source, r.source_ref, r.raw_content, r.content_type AS raw_content_type,
                       r.metadata_json AS raw_metadata_json, r.received_at,
                       k.title AS knowledge_title, k.summary AS knowledge_summary,
                       k.knowledge_kind, k.importance, k.quality_score AS knowledge_quality_score,
                       k.promotion_score AS knowledge_promotion_score, k.lifecycle_stage
                FROM inbox i
                JOIN raw_objects r ON r.id=i.raw_object_id AND r.user_id=i.user_id
                LEFT JOIN knowledge_objects k
                  ON k.id=i.knowledge_object_id AND k.user_id=i.user_id
                 AND {_not_private_knowledge_dependency("k")}
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE i.status WHEN 'pending' THEN 0 ELSE 1 END,
                         i.promotion_score DESC, i.created_at DESC
                LIMIT ? OFFSET ?"""  # nosec B608
        rows = self.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_inbox_status(
        self,
        inbox_id: str,
        status: InboxStatus,
        reviewed_by: str | None = None,
        *,
        user_id: str | None = None,
        suggested_entity_id: str | None = None,
        suggested_tags: list[str] | None = None,
        suggestions: dict[str, Any] | None = None,
        suggested_action: str | None = None,
        knowledge_object_id: str | None = None,
        clear_knowledge_object_id: bool = False,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        notes: str | None = None,
    ) -> bool:
        updates = ["status=?", "reviewed_at=?", "reviewed_by=?"]
        values: list[Any] = [enum_value(status), utc_now(), reviewed_by]
        if suggested_entity_id is not None:
            updates.append("suggested_entity_id=?")
            values.append(suggested_entity_id)
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggestions is not None:
            updates.append("suggestions_json=?")
            values.append(json.dumps(suggestions, ensure_ascii=False, sort_keys=True))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action)[:32])
        if clear_knowledge_object_id:
            updates.append("knowledge_object_id=NULL")
        elif knowledge_object_id is not None:
            updates.append("knowledge_object_id=?")
            values.append(knowledge_object_id)
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if notes is not None:
            updates.append("classification_notes=?")
            values.append(notes)
        # Assignment fragments are selected from fixed fields in this method.
        query = f"UPDATE inbox SET {', '.join(updates)} WHERE id=?"  # nosec B608
        values.append(inbox_id)
        if user_id is not None:
            query += " AND user_id=?"
            values.append(user_id)
        with self.transaction() as conn:
            cursor = conn.execute(query, tuple(values))
        return cursor.rowcount > 0

    def claim_inbox_promotion(self, inbox_id: str, user_id: str, knowledge_object_id: str) -> bool:
        """Atomically reserve an Inbox item for promotion.

        Sets ``knowledge_object_id`` only if the item still has none, so exactly
        one of several concurrent approvals wins; the losers get ``False`` and
        must NOT create a second canonical Knowledge Object from one Raw Object
        (the "Inbox before canonical, exactly once" invariant).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inbox SET knowledge_object_id=? "
                "WHERE id=? AND user_id=? AND knowledge_object_id IS NULL",
                (knowledge_object_id, inbox_id, user_id),
            )
        return cursor.rowcount == 1

    def update_inbox_suggestions(
        self,
        inbox_id: str,
        user_id: str,
        *,
        suggestions: dict[str, Any],
        suggested_tags: list[str] | None = None,
        suggested_action: str | None = None,
        promotion_score: float | None = None,
        quality_score: float | None = None,
        classification_notes: str | None = None,
    ) -> bool:
        """Refresh machine-generated Inbox advice without marking it human-reviewed.

        Background enrichment must not change ``status``, ``reviewed_at``, or
        ``reviewed_by``.  Keeping this operation separate from
        :meth:`update_inbox_status` makes that policy explicit and prevents a
        model-generated suggestion from looking like an administrator decision.
        """

        updates = ["suggestions_json=?"]
        values: list[Any] = [json.dumps(suggestions, ensure_ascii=False, sort_keys=True)]
        if suggested_tags is not None:
            updates.append("suggested_tags_json=?")
            values.append(json.dumps(sorted(set(suggested_tags)), ensure_ascii=False))
        if suggested_action is not None:
            updates.append("suggested_action=?")
            values.append(str(suggested_action).strip().casefold()[:32] or "review")
        if promotion_score is not None:
            updates.append("promotion_score=?")
            values.append(max(0.0, min(1.0, float(promotion_score))))
        if quality_score is not None:
            updates.append("quality_score=?")
            values.append(max(0.0, min(1.0, float(quality_score))))
        if classification_notes is not None:
            updates.append("classification_notes=?")
            values.append(str(classification_notes)[:4000])
        values.extend([inbox_id, user_id])
        with self.transaction() as conn:
            # Assignment fragments are selected from fixed fields in this method.
            cursor = conn.execute(
                f"UPDATE inbox SET {', '.join(updates)} WHERE id=? AND user_id=?",  # nosec B608
                tuple(values),
            )
        return cursor.rowcount > 0
