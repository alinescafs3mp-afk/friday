"""Fail-closed response projections for private HTTP surfaces.

Storage and ingestion deliberately retain substantially more provenance than an
HTTP client needs.  Returning those dictionaries verbatim makes every future
internal field public by accident: attachment Raw Object pointers, parser
exceptions, transcripts and host paths have all reached those dictionaries at
different times.  This module is the API boundary.  Its projections are
allowlists, so adding an internal field cannot silently widen the public
contract.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from friday.generated_files import generated_file_descriptor
from friday.raw_metadata import bounded_raw_file_metadata
from friday.telemetry.logging import redact_friday_api_tokens

_INGESTION_ACTIONS = frozenset({"promote", "review", "transient", "unknown"})
_INGESTION_CATEGORIES = frozenset(
    {
        "chatter",
        "command",
        "greeting",
        "knowledge",
        "obsidian_request",
        "private_transient",
        "question",
        "system_notice",
        "unknown",
        "web_request",
    }
)
_INGESTION_REASONS = frozenset(
    {
        "empty content",
        "explicit durable decision",
        "explicit durable personal preference",
        "explicit instruction not to store as knowledge",
        "explicit no-save request",
        "explicit save intent",
        "greeting or acknowledgement",
        "insufficient durable value",
        "interpersonal or context-free preference chatter",
        "manual promotion",
        "potentially useful but uncertain",
        "pure question or action request",
        "request contains potentially durable context",
        "specific dated task or event",
        "specific durable information",
        "specific durable reference",
        "specific named factual relationship",
        "synthetic document acknowledgement; file ingestion handled separately",
        "telegram command",
        "uploaded file has no extractable text; kept as a source, needs a human verdict",
        "явная просьба поискать в интернете — команда, а не материал",
        "явная команда Obsidian — действие, а не материал",
    }
)
_INGESTION_BOOL_FIELDS = (
    "promoted",
    "queued_for_review",
    "persisted",
    "strict_review",
    "auto_classified",
    "idempotent_replay",
    "synthetic",
)
_INGESTION_SCORE_FIELDS = ("confidence", "promotion_score", "quality_score")

_FILE_BOOL_FIELDS = (
    "archive_password_required",
    "archive_password_invalid",
    "voice_unrecognised",
    "voice_transcript_truncated",
    "extraction_success",
    "empty_text",
    "text_truncated",
    "parse_deadline_reached",
    "parse_pages_truncated",
    "archive_truncated",
    "source_truncated_for_parse",
    "unsupported_format",
)
_FILE_COUNT_FIELDS = (
    "size_bytes",
    "parse_pages_read",
    "parse_total_pages",
    "vision_pages_read",
    "vision_pages_total",
    "archive_files",
    "archive_files_read",
)
_EXTRACTION_BOOL_FIELDS = (
    "success",
    "text_success",
    "text_truncated",
    "parse_deadline_reached",
    "parse_pages_truncated",
    "archive_truncated",
    "source_truncated_for_parse",
    "unsupported_format",
)
_EXTRACTION_COUNT_FIELDS = (
    "chars",
    "parse_pages_read",
    "parse_total_pages",
    "vision_pages_read",
    "vision_pages_total",
    "archive_files",
    "archive_files_read",
)

_PUBLIC_FILE_ROW_FIELDS = ("id", "received_at", "deleted_at")
_PUBLIC_MEDIA_KINDS = frozenset({"animation", "audio", "document", "video", "video_note", "voice"})
_PUBLIC_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}")
_PUBLIC_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PUBLIC_FILE_BOOL_METADATA = (
    "extraction_success",
    "text_extraction_success",
    "text_truncated",
    "parse_deadline_reached",
    "parse_pages_truncated",
    "vision_used",
    "vision_review_required",
)
_PUBLIC_FILE_COUNT_METADATA = ("size_bytes", "parse_pages_read", "parse_total_pages")
_PUBLIC_OPAQUE_ID_RE = re.compile(r"(?P<prefix>raw|inbox|ko|msg)_[0-9a-f]{16}")


def is_public_opaque_id(value: Any, prefix: str) -> bool:
    """Accept only identifiers emitted by ``new_id``, never path-shaped lookalikes."""

    return (
        isinstance(value, str)
        and prefix in {"raw", "inbox", "ko", "msg"}
        and bool(_PUBLIC_OPAQUE_ID_RE.fullmatch(value))
        and value.startswith(f"{prefix}_")
    )


def _resource_is_owned(
    storage: Any,
    user_id: str,
    owner_id: str,
    resource_type: str,
    resource_id: Any,
) -> bool:
    """Prove an opaque response handle belongs to the authenticated tenant.

    Ingestion results are internal data, not an authorization source.  A stale,
    corrupted or test-double result may name a perfectly well-formed object from
    another tenant.  The HTTP boundary must fail closed in that case; subsequent
    capability checks on the target route are defence in depth, not permission to
    disclose that the foreign handle exists.
    """

    if not user_id or not is_public_opaque_id(resource_id, resource_type):
        return False
    lookup_name = {
        "raw": "get_raw_object",
        "inbox": "get_inbox_item",
        "ko": "get_knowledge_object",
    }.get(resource_type)
    lookup = getattr(storage, lookup_name, None) if lookup_name else None
    if not callable(lookup):
        return False
    try:
        row = lookup(resource_id, user_id)
    except Exception:  # noqa: BLE001 - a response projection must fail closed
        return False
    if not (isinstance(row, Mapping) and row.get("id") == resource_id and row.get("user_id") == user_id):
        return False
    # In a personal archive tenant and person are the same identity. Legacy rows
    # from before uploaded_by existed remain addressable there. In a shared archive
    # the tenant alone is insufficient: trace Inbox/Knowledge handles back to their
    # Raw Object and require the exact authenticated uploader.
    if not owner_id or owner_id == user_id:
        return True
    raw = row
    if resource_type != "raw":
        raw_id = row.get("raw_object_id")
        if not is_public_opaque_id(raw_id, "raw"):
            return False
        raw_lookup = getattr(storage, "get_raw_object", None)
        if not callable(raw_lookup):
            return False
        try:
            raw = raw_lookup(raw_id, user_id)
        except Exception:  # noqa: BLE001 - a response projection must fail closed
            return False
    if not isinstance(raw, Mapping) or raw.get("user_id") != user_id:
        return False
    metadata = bounded_raw_file_metadata(raw.get("metadata_json"))
    if not metadata:
        return False
    return metadata.get("uploaded_by") == owner_id


def _public_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _public_count(value: Any, *, maximum: int = 2_147_483_647) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(max(0, parsed), maximum)


def _public_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return round(min(max(0.0, parsed), 1.0), 6)


def _copy_typed_fields(
    source: Mapping[str, Any],
    target: dict[str, Any],
    fields: tuple[str, ...],
    converter: Any,
) -> None:
    for key in fields:
        if key not in source:
            continue
        value = converter(source[key])
        if value is not None:
            target[key] = value


def public_conversation_message(
    row: Mapping[str, Any],
    *,
    storage: Any = None,
    resource_user_id: str = "",
    resource_owner_id: str = "",
) -> dict[str, Any]:
    """Publish message text and envelope, never its internal metadata blob."""

    public = {key: row[key] for key in ("role", "content", "created_at") if key in row}
    if "content" in public:
        # Historical rows remain an exact audit trail, but credentials emitted
        # before the runtime boundary existed must not be republished by chat or
        # admin transcript APIs.
        public["content"] = redact_friday_api_tokens(public["content"])
    message_id = row.get("id")
    if is_public_opaque_id(message_id, "msg"):
        public["id"] = message_id
    metadata = _bounded_message_metadata(row.get("metadata_json"))
    generated = metadata.get("generated_files") if metadata else None
    if isinstance(generated, list):
        descriptors = _project_generated_descriptors(
            generated,
            storage=storage,
            resource_user_id=resource_user_id,
            resource_owner_id=resource_owner_id,
        )
        if descriptors:
            public["files"] = descriptors
    return public


def public_ingestion_receipt(
    outcome: Mapping[str, Any] | None,
    *,
    file: bool = False,
    include_resource_id: bool = False,
    include_inbox_id: bool = False,
    storage: Any = None,
    resource_user_id: str = "",
    resource_owner_id: str = "",
) -> dict[str, Any]:
    """Return only bounded status facts from an internal ingestion result.

    Suggestions, extracted content, transcript, parser errors, model details and
    filesystem paths are intentionally absent.  Resource identifiers are absent
    unless the caller opts into an actionable handle and supplies storage plus the
    authenticated tenant so ownership can be proved.  The caller may still use the
    full ``outcome`` internally before invoking this projection.
    """

    source = outcome if isinstance(outcome, Mapping) else {}
    public: dict[str, Any] = {}
    _copy_typed_fields(source, public, _INGESTION_BOOL_FIELDS, _public_bool)

    # Older results did not carry an explicit persisted bit.  Deriving one from
    # pointer presence exposes only a boolean and lets clients distinguish a
    # no-save inspection without receiving the pointer itself.
    if "persisted" not in public and "raw_object_id" in source:
        public["persisted"] = bool(source.get("raw_object_id"))

    # Direct intake/file endpoints create a resource that their caller may
    # subsequently inspect or download. They opt into returning that handle;
    # chat keeps only the Inbox handle required by its confirm/ignore buttons.
    # Every handle is resolved through the authenticated tenant before release:
    # an internal result is provenance, never authority.
    resource_id = source.get("raw_object_id")
    if include_resource_id and _resource_is_owned(
        storage,
        resource_user_id,
        resource_owner_id,
        "raw",
        resource_id,
    ):
        public["raw_object_id"] = resource_id
    inbox_id = source.get("inbox_id")
    if (include_resource_id or include_inbox_id) and _resource_is_owned(
        storage,
        resource_user_id,
        resource_owner_id,
        "inbox",
        inbox_id,
    ):
        public["inbox_id"] = inbox_id
    knowledge_value = source.get("knowledge_object")
    if include_resource_id and isinstance(knowledge_value, Mapping):
        knowledge_id = knowledge_value.get("id")
        knowledge_user_id = knowledge_value.get("user_id")
        if knowledge_user_id == resource_user_id and _resource_is_owned(
            storage,
            resource_user_id,
            resource_owner_id,
            "ko",
            knowledge_id,
        ):
            # A creation receipt needs enough to address the newly-created
            # Knowledge Object.  Its content, summary, Raw pointer and metadata
            # stay behind the dedicated capability-gated Knowledge API.
            public["knowledge_object"] = {
                "id": knowledge_id,
                "user_id": knowledge_user_id,
            }

    action = str(source.get("action") or "")
    if action in _INGESTION_ACTIONS:
        public["action"] = action
    assessed_action = str(source.get("assessed_action") or "")
    if assessed_action in _INGESTION_ACTIONS:
        public["assessed_action"] = assessed_action
    category = str(source.get("category") or "")
    if category in _INGESTION_CATEGORIES:
        public["category"] = category
    reason = source.get("reason")
    if isinstance(reason, str) and reason in _INGESTION_REASONS:
        public["reason"] = reason
    _copy_typed_fields(source, public, _INGESTION_SCORE_FIELDS, _public_score)

    if not file:
        return public

    _copy_typed_fields(source, public, _FILE_BOOL_FIELDS, _public_bool)
    _copy_typed_fields(source, public, _FILE_COUNT_FIELDS, _public_count)
    extraction_value = source.get("extraction")
    if isinstance(extraction_value, Mapping):
        extraction: dict[str, Any] = {}
        _copy_typed_fields(extraction_value, extraction, _EXTRACTION_BOOL_FIELDS, _public_bool)
        _copy_typed_fields(extraction_value, extraction, _EXTRACTION_COUNT_FIELDS, _public_count)
        if extraction:
            public["extraction"] = extraction
    return public


def public_file_record(
    row: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project a file-list row without storage/provenance internals.

    ``id`` remains the capability-gated resource handle used by the download
    route.  It is not copied into conversation metadata or chat receipts.
    """

    public = {key: row[key] for key in _PUBLIC_FILE_ROW_FIELDS[1:] if key in row}
    file_id = row.get("id")
    if is_public_opaque_id(file_id, "raw"):
        public["id"] = file_id
    source = metadata if isinstance(metadata, Mapping) else {}
    safe_metadata: dict[str, Any] = {}
    filename = source.get("filename")
    if isinstance(filename, str):
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:255]
        if basename:
            safe_metadata["filename"] = basename
    mime_type = source.get("mime_type")
    if isinstance(mime_type, str) and _PUBLIC_MIME_RE.fullmatch(mime_type.casefold()):
        safe_metadata["mime_type"] = mime_type.casefold()
    media_kind = source.get("media_kind")
    if isinstance(media_kind, str) and media_kind in _PUBLIC_MEDIA_KINDS:
        safe_metadata["media_kind"] = media_kind
    document_date = source.get("document_date")
    if isinstance(document_date, str) and _PUBLIC_DATE_RE.fullmatch(document_date):
        safe_metadata["document_date"] = document_date
    _copy_typed_fields(source, safe_metadata, _PUBLIC_FILE_BOOL_METADATA, _public_bool)
    _copy_typed_fields(source, safe_metadata, _PUBLIC_FILE_COUNT_METADATA, _public_count)
    public["metadata"] = safe_metadata
    return public


def public_chat_ingestion(
    response: Mapping[str, Any],
    *,
    storage: Any = None,
    resource_user_id: str = "",
    resource_owner_id: str = "",
) -> dict[str, Any]:
    """Sanitize ingestion sub-results, including already-cached legacy replies."""

    public = dict(response)
    if "ingestion" in public:
        ingestion = public.get("ingestion")
        public["ingestion"] = (
            public_ingestion_receipt(
                ingestion,
                include_inbox_id=True,
                storage=storage,
                resource_user_id=resource_user_id,
                resource_owner_id=resource_owner_id,
            )
            if isinstance(ingestion, Mapping)
            else None
        )
    if "file_ingestion" in public:
        file_ingestion = public.get("file_ingestion")
        public["file_ingestion"] = (
            public_ingestion_receipt(
                file_ingestion,
                file=True,
                storage=storage,
                resource_user_id=resource_user_id,
                resource_owner_id=resource_owner_id,
            )
            if isinstance(file_ingestion, Mapping)
            else None
        )
    if "file_ingestions" in public:
        file_ingestions = public.get("file_ingestions")
        public["file_ingestions"] = (
            [
                public_ingestion_receipt(
                    item,
                    file=True,
                    storage=storage,
                    resource_user_id=resource_user_id,
                    resource_owner_id=resource_owner_id,
                )
                for item in file_ingestions[:16]
                if isinstance(item, Mapping)
            ]
            if isinstance(file_ingestions, list)
            else []
        )
    files = public.get("files")
    if isinstance(files, list):
        public["files"] = _project_generated_response_files(
            files,
            storage=storage,
            resource_user_id=resource_user_id,
            resource_owner_id=resource_owner_id,
        )
    return public


def _project_generated_response_files(
    items: list[Any],
    *,
    storage: Any,
    resource_user_id: str,
    resource_owner_id: str,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for value in items[:16]:
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        raw_id = item.get("id")
        claimed_handle = any(
            key in item for key in ("id", "raw_object_id", "download_url", "sha256", "size_bytes")
        )
        descriptor = generated_file_descriptor(
            storage,
            raw_id,
            tenant_id=resource_user_id,
            person_id=resource_owner_id,
        )
        # Handles are authority-bearing fields. An old cache or a compromised
        # tool result cannot publish them without a fresh ownership lookup.
        for key in ("id", "raw_object_id", "download_url", "sha256", "size_bytes"):
            item.pop(key, None)
        if descriptor is None:
            # A cached inline response is not an authority.  Once its durable
            # handle is deleted/revoked, replay must not resurrect the bytes.
            if not claimed_handle:
                encoded = item.get("content_base64")
                if isinstance(encoded, str):
                    try:
                        base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError, binascii.Error):
                        continue
                    projected.append(item)
            continue
        item.update(descriptor)
        encoded = item.get("content_base64")
        if isinstance(encoded, str):
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError, binascii.Error):
                item.pop("content_base64", None)
            else:
                digest = hashlib.sha256(payload).hexdigest()
                if len(payload) != descriptor["size_bytes"] or not hmac.compare_digest(
                    digest, descriptor["sha256"]
                ):
                    item.pop("content_base64", None)
        projected.append(item)
    return projected


def _project_generated_descriptors(
    items: list[Any],
    *,
    storage: Any,
    resource_user_id: str,
    resource_owner_id: str,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for value in items[:16]:
        if not isinstance(value, Mapping):
            continue
        descriptor = generated_file_descriptor(
            storage,
            value.get("id"),
            tenant_id=resource_user_id,
            person_id=resource_owner_id,
        )
        if descriptor is not None:
            projected.append(descriptor)
    return projected


def _bounded_message_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return {}
    if len(encoded) > 1024 * 1024:
        return {}
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
