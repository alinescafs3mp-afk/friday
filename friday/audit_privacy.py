"""Fail-closed projection for the append-only audit trail.

The audit table deliberately survives edits, deletion and hard purge.  It may
therefore keep *structure* needed for an investigation, but it must never become
another copy of a note, prompt, filename, URL credential or model response.

Callers still ought to submit small purpose-built payloads.  This module is the
last line of defence at the persistence boundary: unknown fields are counted,
not serialized, and known content-bearing fields become bounded fingerprints.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import re
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from friday.id_provenance import is_marked_generated_id

_MAX_FIELDS = 96
_MAX_LIST_ITEMS = 64
_MAX_DEPTH = 3

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+*-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_AUDIT_ID_RE = re.compile(r"^audit_(?:[0-9a-f]{16}|[0-9a-f]{24})$")
# Server-issued HTTP correlation IDs use a deliberately narrow, high-entropy
# namespace.  Keeping those exact values is what lets an investigator join an
# HTTP response to its durable audit rows.  Every other caller-controlled shape
# remains a keyed opaque reference below.
_SERVER_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_CLIENT_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_GENERATED_ID_RE = re.compile(r"^(?P<prefix>[a-z][a-z0-9_]{0,31})_[0-9a-f]{16}$")
_HMAC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class _ServerAuditRequestId(str):
    """In-process provenance marker for a correlation ID issued by the server."""


class _ValidatedClientAuditRequestId(str):
    """In-process marker for a bounded client correlation ID validated by the server."""


class _ObservedAuditIp(str):
    """In-process marker for an IP observed as the request's ASGI peer."""


_AUDIT_REQUEST_ID: ContextVar[str] = ContextVar("friday_audit_request_id", default="")


_GENERATED_ID_PREFIXES = frozenset(
    {
        "apr",
        "audit",
        "cmp",
        "conf",
        "conv",
        "ent",
        "entv",
        "er",
        "eval",
        "evt",
        "fb",
        "feedback",
        "inbox",
        "kel",
        "ko",
        "kov",
        "lease",
        "merge",
        "mis",
        "mon",
        "msg",
        "msn",
        "mtask",
        "notif",
        "r",
        "raw",
        "rel",
        "relation_batch",
        "relc",
        "tok",
        "toolref",
    }
)

_STRUCTURAL_ID_PREFIXES: dict[str, frozenset[str]] = {
    "approval": frozenset({"apr"}),
    "approval_id": frozenset({"apr"}),
    "batch_id": frozenset({"relation_batch"}),
    "candidate_id": frozenset({"er", "relc"}),
    "canonical_id": frozenset({"ent"}),
    "conflict_id": frozenset({"conf"}),
    "conversation_id": frozenset({"conv"}),
    "entity_a_id": frozenset({"ent"}),
    "entity_b_id": frozenset({"ent"}),
    "entity_id": frozenset({"ent"}),
    "inbox_id": frozenset({"inbox"}),
    "knowledge_object_id": frozenset({"ko"}),
    "link_id": frozenset({"kel"}),
    "merge_id": frozenset({"merge"}),
    "message_id": frozenset({"msg"}),
    "mission_id": frozenset({"mis", "msn"}),
    "parent_entity_id": frozenset({"ent"}),
    "raw_object_id": frozenset({"raw"}),
    "relation_id": frozenset({"rel"}),
    "source_entity_id": frozenset({"ent"}),
    "target_entity_id": frozenset({"ent"}),
    "task_id": frozenset({"mtask"}),
    "token_id": frozenset({"tok"}),
}

_STRUCTURAL_ID_LIST_PREFIXES: dict[str, frozenset[str]] = {
    "changed_ids": _GENERATED_ID_PREFIXES,
    "entity_ids": frozenset({"ent"}),
    "knowledge_ids": frozenset({"ko"}),
    "relation_ids": frozenset({"rel"}),
}

_TARGET_ID_PREFIXES: dict[str, frozenset[str]] = {
    "action_approval": frozenset({"apr"}),
    "api_token": frozenset({"tok"}),
    "conversation": frozenset({"conv"}),
    "entity": frozenset({"ent"}),
    "eval_case": frozenset({"eval"}),
    "inbox": frozenset({"inbox"}),
    "knowledge_conflict": frozenset({"conf"}),
    "knowledge_entity_link": frozenset({"kel"}),
    "knowledge_object": frozenset({"ko"}),
    "merge": frozenset({"merge"}),
    "mission": frozenset({"mis", "msn"}),
    "monitor": frozenset({"mon"}),
    "raw_object": frozenset({"raw"}),
    "relation": frozenset({"rel"}),
    "relation_candidate": frozenset({"relc"}),
    "resolution": frozenset({"er"}),
}

_SAFE_AUDIT_ACTIONS = frozenset(
    {
        "admin.audit.read",
        "admin.backup.create",
        "admin.backup.download",
        "admin.backup.verify",
        "admin.chat.reply",
        "admin.chat_feed.read",
        "admin.cleanup.read",
        "admin.conflicts.read",
        "admin.container.create",
        "admin.containers.read",
        "admin.conversation.archive",
        "admin.conversation.delete",
        "admin.conversations.read",
        "admin.data_source.declare",
        "admin.data_source.forget",
        "admin.data_source.schema",
        "admin.data_sources.read",
        "admin.entities.read",
        "admin.entity.create",
        "admin.entity.delete",
        "admin.entity.merge",
        "admin.entity.merge_rejected",
        "admin.entity.unmerge",
        "admin.entity.update",
        "admin.entity_resolution.detect",
        "admin.entity_suggestion.accept",
        "admin.entity_suggestion.bulk_accept",
        "admin.entity_suggestion.bulk_reject",
        "admin.entity_suggestions.read",
        "admin.eval.ablation",
        "admin.eval.case_add",
        "admin.eval.case_delete",
        "admin.eval.chunk_ab",
        "admin.eval.read",
        "admin.eval.run",
        "admin.export.create",
        "admin.export.download",
        "admin.feedback.read",
        "admin.file.download",
        "admin.files.read",
        "admin.graph.read",
        "admin.identities.read",
        "admin.identity.link",
        "admin.identity.unlink",
        "admin.inbox.bulk_classify",
        "admin.inbox.classify",
        "admin.inbox.model_advice",
        "admin.inbox.purge_secondary_witness",
        "admin.inbox.consume_secondary_document_map_rollout_attestation",
        "admin.inbox.consume_secondary_product_rollout_attestation",
        "admin.inbox.observe_secondary_document_map_shadow",
        "admin.inbox.read",
        "admin.knowledge.cleanup.archive",
        "admin.knowledge.cleanup.keep",
        "admin.knowledge.cleanup.reclassify",
        "admin.knowledge.cleanup.return_to_inbox",
        "admin.knowledge.cleanup.soft_delete",
        "admin.knowledge.delete",
        "admin.knowledge.detect_duplicates",
        "admin.knowledge.diff",
        "admin.knowledge.entity_link.create",
        "admin.knowledge.entity_link.review",
        "admin.knowledge.inspect",
        "admin.knowledge.purge",
        "admin.knowledge.purge_attempted",
        "admin.knowledge.read",
        "admin.knowledge.reenrich",
        "admin.knowledge.restore",
        "admin.knowledge.update",
        "admin.knowledge_conflict.confirmed",
        "admin.knowledge_conflict.dismissed",
        "admin.knowledge_conflict.resolve",
        "admin.knowledge_conflict.resolved",
        "admin.lifecycle.archive",
        "admin.lifecycle.deprecate",
        "admin.lifecycle.keep",
        "admin.lifecycle.lower_importance",
        "admin.lifecycle.read",
        "admin.merges.read",
        "admin.messages.read",
        "admin.mission.cancel",
        "admin.missions.read",
        "admin.permission.override",
        "admin.preset.upsert",
        "admin.purge.read",
        "admin.quality.read",
        "admin.relation.invalidate",
        "admin.relation_candidate.accepted",
        "admin.relation_candidate.rejected",
        "admin.relations.read",
        "admin.resolutions.read",
        "admin.source.search",
        "admin.token.create",
        "admin.token.revoke",
        "admin.tokens.read",
        "admin.user.activity.out_of_scope",
        "admin.user.activity.read",
        "admin.user.delete",
        "admin.user.deletion.preflight",
        "admin.user.preset",
        "admin.user.supervisor",
        "admin.user.update",
        "admin.user.upsert",
        "admin.users.list",
        "approval.approve",
        "approval.reject",
        "audit.unknown",
        "auth.failed",
        "cli.knowledge.purge",
        "cli.file_uploader.backfill",
        "cli.legacy_telegram_uploader.backfill",
        "cli.telegram_file_aliases.backfill",
        "container.create",
        "entity.create",
        "entity.delete",
        "entity.merge",
        "entity.merge_rejected",
        "entity.restore",
        "entity.time_set",
        "entity.undelete",
        "entity.unmerge",
        "entity.update",
        "file.download",
        "file.upload",
        "inbox.classify",
        "knowledge.delete",
        "knowledge.entity_link",
        "knowledge.import",
        "knowledge.ingest_url",
        "knowledge.update",
        "knowledge_conflict.dismissed",
        "knowledge_conflict.resolved",
        "mission.cancel",
        "mission.create",
        "mission.finish",
        "mission.start",
        "monitor.create",
        "monitor.stop",
        "relation.create",
        "relation.create.idempotent",
        "relation_candidate.accepted",
        "relation_candidate.rejected",
        "request.throttled",
        "tool.invoke",
        "tool.user_activity",
        "tool.user_activity.out_of_scope",
        "tool.user_activity.unresolved",
        "tool.user_knowledge_search",
        "tool.user_knowledge_search.out_of_scope",
        "tool.user_knowledge_search.unresolved",
    }
)

_SAFE_AUDIT_TARGET_TYPES = frozenset(
    {
        "action_approval",
        "api_token",
        "audit_log",
        "auth",
        "backup",
        "conversation",
        "data_source",
        "entity",
        "eval_case",
        "export",
        "import",
        "inbox",
        "knowledge_conflict",
        "knowledge_entity_link",
        "knowledge_object",
        "merge",
        "mission",
        "monitor",
        "preset",
        "private",
        "raw_object",
        "relation",
        "relation_candidate",
        "resolution",
        "tool",
        "user",
    }
)

_SAFE_AUTH_TARGETS = frozenset(
    {"capability_denied", "invalid_credentials", "malformed_credentials", "rate_limited"}
)

_SAFE_TOOL_TARGETS = frozenset(
    {
        "code_run",
        "collect_files",
        "conflict_decide",
        "conflict_list",
        "data_query",
        "data_schema",
        "data_sources",
        "entity_create",
        "entity_link",
        "entity_lookup",
        "entity_merge_decide",
        "entity_merge_undo",
        "engineer_adversary_rehearsal",
        "engineer_analyze_artifact",
        "engineer_audit_host",
        "engineer_dns",
        "engineer_hunt",
        "engineer_http_enum",
        "engineer_local_tools",
        "engineer_patch_artifact",
        "host_action_run",
        "host_action_execute",
        "host_capability_describe",
        "host_capability_search",
        "host_job_cancel",
        "host_job_status",
        "host_json_extract",
        "host_program_run_once",
        "inbox_list",
        "kg_stats",
        "list_tags",
        "make_file",
        "memory_save",
        "memory_search",
        "source_search",
        "software_install",
        "software_install_execute",
        "software_remove",
        "software_remove_execute",
        "software_search",
        "message_search",
        "mission_compensation",
        "mission_propose",
        "relation_end",
        "remind",
        "resolve_duplicates",
        "speak",
        "upcoming",
        "user_activity",
        "user_knowledge_search",
        "web_fetch",
        "web_research",
        "web_search",
        "what_happened",
        "workspace_create",
        "workspace_list",
        "workspace_read",
        "workspace_search",
    }
)

_URL_KEYS = frozenset({"url"})
_HOST_KEYS = frozenset({"url_host"})
_SUFFIX_KEYS = frozenset({"filename_suffix", "path_suffix"})
_NESTED_CONTAINER_KEYS = frozenset({"metadata"})
_SAFE_FILE_SUFFIXES = frozenset(
    {
        ".7z",
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".gz",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".log",
        ".m4a",
        ".md",
        ".mp3",
        ".mp4",
        ".odt",
        ".ogg",
        ".pdf",
        ".png",
        ".rtf",
        ".sql",
        ".svg",
        ".tar",
        ".tgz",
        ".tsv",
        ".txt",
        ".wav",
        ".webm",
        ".webp",
        ".xls",
        ".xlsx",
        ".xml",
        ".yaml",
        ".yml",
        ".zip",
    }
)
_SAFE_CHANGED_FIELDS = frozenset(
    {
        "aliases",
        "content",
        "description",
        "entity_type",
        "importance",
        "knowledge_kind",
        "lifecycle_stage",
        "metadata",
        "metadata_json",
        "name",
        "promotion_score",
        "quality_score",
        "summary",
        "tags_json",
        "title",
    }
)

_SAFE_ENGINEER_OPERATION_KINDS = frozenset({"replace_bytes", "write_at", "zip_replace"})

# Values from validated enums or code-owned branches.  A free-form value under
# one of these keys is redacted instead of being trusted merely because it looks
# like an ASCII token.
_SAFE_ENUM_VALUES = frozenset(
    {
        "accepted",
        "acquired",
        "active",
        "admin",
        "admin.activity.read",
        "admin.all_data.read",
        "agent",
        "all_tenants",
        "allow",
        "ambiguous",
        "approval_not_claimable",
        "approval_request_failed",
        "approval_required",
        "archived",
        "authorization_denied",
        "backfill",
        "bearer",
        "blocked",
        "cancelled",
        "classified",
        "collection",
        "completed",
        "compensated",
        "concept",
        "confirmed",
        "contact",
        "conflict",
        "content_not_permitted",
        "created_by",
        "day",
        "deleted",
        "deny",
        "depends_on",
        "derived_from",
        "dialogue",
        "disabled",
        "dismissed",
        "document",
        "done",
        "empty_query",
        "entity_proposal",
        "event",
        "execution_scope_denied",
        "fact",
        "failed",
        "failed_after_start",
        "family_of",
        "forgotten",
        "full",
        "human_review",
        "human_review_bulk",
        "ignored",
        "implicit_cooccurrence",
        "in_progress",
        "invalid",
        "invalid_arguments",
        "invalid_credentials",
        "idea",
        "local",
        "local_model_advice",
        "location",
        "located_at",
        "manages",
        "member_of",
        "mentions",
        "merged",
        "migration_baseline",
        "mission",
        "month",
        "no_authorization_service",
        "not_found",
        "not_stored",
        "note",
        "occurred_at",
        "ok",
        "ok_approved",
        "other",
        "owner",
        "part_of",
        "paused",
        "pending",
        "person",
        "postcondition_failed",
        "processing",
        "project",
        "proposed",
        "preference",
        "procedure",
        "purged",
        "rate_limited",
        "ready",
        "redacted",
        "references",
        "reference",
        "rejected",
        "related_to",
        "replay",
        "reset",
        "resolved",
        "returned_to_inbox",
        "review",
        "revoked",
        "running",
        "same_as",
        "shared_archive",
        "skipped",
        "soft_deleted",
        "started",
        "stopped",
        "suggested",
        "task",
        "technical_note",
        "telegram-bridge",
        "timeout",
        "tool",
        "uncertain",
        "undone",
        "unlinked",
        "user",
        "uses",
        "week",
        "worker",
        "works_on",
        "year",
    }
)

_ENUM_KEYS = frozenset(
    {
        "action",
        "content_access",
        "decision",
        "effect",
        "entity_type",
        "execution_scope",
        "freshness",
        "granted_by",
        "history_quality",
        "kind",
        "lang",
        "knowledge_kind",
        "lifecycle_stage",
        "match_method",
        "method",
        "mode",
        "operation",
        "origin",
        "reason",
        "region",
        "relation_type",
        "resolution_method",
        "scope",
        "source",
        "status",
        "target_type",
        "type",
    }
)

_NUMBER_KEYS = frozenset(
    {
        "already_imported",
        "candidates",
        "cases",
        "changed",
        "chars",
        "confidence",
        "conflicts",
        "day_count",
        "decided",
        "delta",
        "deleted",
        "documents",
        "elapsed_sec",
        "event_seq",
        "failed",
        "filtered_out",
        "found",
        "items",
        "limit",
        "max_results",
        "max_sources",
        "missing",
        "offset",
        "parsed",
        "pending",
        "queued_for_review",
        "ranked",
        "recall_at_k",
        "removed",
        "returned",
        "shown",
        "size_bytes",
        "skipped",
        "skipped_existing",
        "status_code",
        "suppressed",
        "tasks",
        "total",
        "version",
        "weight",
    }
)

_BOOL_KEYS = frozenset(
    {
        "advisory_only",
        "applied",
        "archived",
        "entity_created",
        "idempotent_replay",
        "is_archived",
        "promote",
        "queued",
        "success",
        "truncated",
    }
)

_LOW_ENTROPY_PRIVATE_KEYS = frozenset(
    {
        "aliases",
        "analysis",
        "asked_for",
        "by",
        "chat_id",
        "comment",
        "description",
        "detail",
        "display_name",
        "error",
        "external_id",
        "filename",
        "goal",
        "label",
        "message",
        "model",
        "name",
        "note",
        "path",
        "recommended_action",
        "source_ref",
        "tags",
        "title",
        "username",
    }
)

_CONTENT_KEYS = frozenset(
    {
        "body",
        "code",
        "content",
        "prompt",
        "query",
        "response",
        "summary",
        "text",
        "transcript",
    }
)

_PRIVATE_TARGET_TYPES = frozenset({"backup", "data_source", "export", "import", "preset"})

_STRUCTURAL_ID_KEYS = frozenset(
    {
        "approval",
        "approval_id",
        "batch_id",
        "candidate_id",
        "canonical_id",
        "conflict_id",
        "conversation_id",
        "entity_a_id",
        "entity_b_id",
        "entity_id",
        "id",
        "inbox_id",
        "knowledge_object_id",
        "link_id",
        "merge_id",
        "message_id",
        "mission_id",
        "parent_entity_id",
        "raw_object_id",
        "relation_id",
        "source_entity_id",
        "supervisor_id",
        "target_entity_id",
        "target_user_id",
        "task_id",
        "tenant",
        "token_id",
        "user_id",
    }
)

_STRUCTURAL_ID_LIST_KEYS = frozenset({"changed_ids", "entity_ids", "knowledge_ids", "relation_ids"})

_HASH_KEYS = frozenset(
    {
        "body_sha256",
        "code_sha256",
        "content_sha256",
        "exclude_domains_sha256",
        "include_domains_sha256",
        "host_sha256",
        "operations_sha256",
        "prompt_sha256",
        "path_sha256",
        "query_sha256",
        "raw_id_sha256",
        "response_sha256",
        "restored_sha256",
        "site_sha256",
        "summary_sha256",
        "target_ticket_sha256",
        "text_sha256",
        "transcript_sha256",
        "url_sha256",
    }
)

_TIMESTAMP_KEYS = frozenset(
    {
        "at",
        "completed_at",
        "created_at",
        "deleted_at",
        "due_at",
        "expires_at",
        "first_seen_at",
        "invalidated_at",
        "known_at",
        "last_seen_at",
        "merged_at",
        "received_at",
        "recorded_at",
        "resolved_at",
        "reviewed_at",
        "since",
        "started_at",
        "undone_at",
        "until",
        "updated_at",
        "valid_from",
        "valid_to",
    }
)

_DERIVED_NUMBER_KEYS = frozenset(
    {
        "private_chars",
        "private_fields_count",
        "private_items_count",
        "exclude_domains_chars",
        "exclude_domains_count",
        "include_domains_chars",
        "include_domains_count",
        "site_chars",
        "url_chars",
        "changed_fields_count",
        "days_count",
        "host_chars",
        "operations_count",
        "ports_count",
        "ports_max",
        "ports_min",
        "ports_valid_count",
        "raw_id_chars",
        "target_ticket_chars",
        *(f"{key}_chars" for key in _CONTENT_KEYS | _LOW_ENTROPY_PRIVATE_KEYS),
        *(f"{key}_count" for key in _LOW_ENTROPY_PRIVATE_KEYS),
        *(f"{key}_fields" for key in _LOW_ENTROPY_PRIVATE_KEYS),
    }
)

_DERIVED_BOOL_KEYS = frozenset({"chat_id_present", *(f"{key}_present" for key in _LOW_ENTROPY_PRIVATE_KEYS)})

_KNOWN_PAYLOAD_KEYS = frozenset().union(
    _BOOL_KEYS,
    _CONTENT_KEYS,
    _DERIVED_BOOL_KEYS,
    _DERIVED_NUMBER_KEYS,
    _ENUM_KEYS,
    _HASH_KEYS,
    _HOST_KEYS,
    _LOW_ENTROPY_PRIVATE_KEYS,
    _NESTED_CONTAINER_KEYS,
    _NUMBER_KEYS,
    _STRUCTURAL_ID_KEYS,
    _STRUCTURAL_ID_LIST_KEYS,
    _SUFFIX_KEYS,
    _TIMESTAMP_KEYS,
    _URL_KEYS,
    {"changed_fields", "days", "operation_kinds"},
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _opaque_ref(key: bytes, domain: str, value: object) -> str:
    if len(key) < 32:
        raise ValueError("audit privacy HMAC key must contain at least 256 bits")
    message = f"{domain}\0{value}".encode("utf-8", errors="replace")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:24]


def decode_audit_privacy_key(value: object) -> bytes:
    """Validate the installation-local key without ever returning its source text."""

    text = str(value or "")
    if not _HMAC_KEY_RE.fullmatch(text):
        raise RuntimeError("audit privacy HMAC key is missing or invalid")
    return bytes.fromhex(text)


def _is_generated_id(
    value: object,
    prefixes: frozenset[str],
    *,
    id_exists: Callable[[str, frozenset[str]], bool] | None = None,
) -> bool:
    text = str(value)
    match = _GENERATED_ID_RE.fullmatch(text)
    if match is None or match.group("prefix") not in prefixes:
        return False
    return is_marked_generated_id(value) or bool(id_exists and id_exists(text, prefixes))


def sanitize_audit_id(
    value: object,
    *,
    key: bytes,
    id_exists: Callable[[str, frozenset[str]], bool] | None = None,
) -> str:
    """Keep generated audit IDs; pseudonymise every caller-controlled shape."""

    text = str(value or "")
    if _AUDIT_ID_RE.fullmatch(text) and (
        is_marked_generated_id(value) or bool(id_exists and id_exists(text, frozenset({"audit"})))
    ):
        return text
    return f"audit_{_opaque_ref(key, 'audit_id', text)}"


def sanitize_audit_actor(value: object, *, user_exists: Callable[[str], bool]) -> str:
    """Return an actual local actor, never an arbitrary audit-column string."""

    text = str(value or "")
    if text == "anonymous":
        return text
    if _ID_RE.fullmatch(text) and user_exists(text):
        return text
    return "unknown"


def sanitize_audit_action(value: object) -> str:
    text = str(value or "")
    return text if text in _SAFE_AUDIT_ACTIONS else "audit.unknown"


def sanitize_audit_target_type(value: object) -> str:
    text = str(value or "")
    return text if text in _SAFE_AUDIT_TARGET_TYPES else "private"


def _canonical_ip(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = ipaddress.ip_address(text)
        return str(ipaddress.ip_address(parsed.packed))
    except ValueError:
        return ""


def server_audit_ip(value: object) -> str:
    """Mark an ASGI peer IP after canonicalising away an IPv6 scope identifier.

    Invalid/non-IP peer labels (for example Starlette's default ``testclient``)
    deliberately produce no forensic datum.  The private marker, rather than a
    syntactically plausible string, is what storage trusts as direct evidence.
    """

    canonical = _canonical_ip(value)
    return _ObservedAuditIp(canonical) if canonical else ""


def sanitize_audit_ip(value: object, *, key: bytes) -> str:
    """Keep observed peers exact; pseudonymise every unproven IP-shaped value."""

    is_observed_peer = isinstance(value, _ObservedAuditIp)
    canonical = _canonical_ip(value)
    if not canonical:
        return ""
    if is_observed_peer:
        return canonical
    return f"ipref_{_opaque_ref(key, 'ip_address', canonical)}"


def server_audit_request_id(value: object) -> str:
    """Mark one validated server-issued ID without trusting equal-shaped strings."""

    text = str(value or "")
    if not _SERVER_REQUEST_ID_RE.fullmatch(text):
        raise ValueError("server audit request ID must be 24 lowercase hex characters")
    return _ServerAuditRequestId(text)


def current_audit_request_id() -> str:
    """Return the request-local correlation marker, or empty outside a request."""

    return _AUDIT_REQUEST_ID.get()


@contextmanager
def bind_audit_request_id(value: object) -> Iterator[str]:
    """Bind one independently validated request correlation marker.

    Only the private server marker may retain the reserved 24-hex namespace.
    Bounded client IDs receive a distinct marker and remain opaque ``reqref``
    values at the durable boundary. Invalid or forged values abort before the
    context changes, and reset is guaranteed across exceptions and nesting.
    """

    if not isinstance(value, str):
        raise ValueError("audit request correlation ID must be a string")
    text = str(value)
    is_server_issued = isinstance(value, _ServerAuditRequestId)
    if is_server_issued:
        if not _SERVER_REQUEST_ID_RE.fullmatch(text):
            raise ValueError("invalid server audit request ID marker")
        marker: str = _ServerAuditRequestId(text)
    elif _CLIENT_REQUEST_ID_RE.fullmatch(text) and not _SERVER_REQUEST_ID_RE.fullmatch(text):
        marker = _ValidatedClientAuditRequestId(text)
    else:
        raise ValueError("invalid audit request correlation ID")
    del value

    token = _AUDIT_REQUEST_ID.set(marker)
    try:
        yield marker
    finally:
        _AUDIT_REQUEST_ID.reset(token)


def sanitize_audit_request_id(value: object, *, key: bytes) -> str:
    is_server_issued = isinstance(value, _ServerAuditRequestId)
    text = str(value or "")
    if not text:
        return ""
    if is_server_issued and _SERVER_REQUEST_ID_RE.fullmatch(text):
        return text
    return f"reqref_{_opaque_ref(key, 'request_id', text)}"


def sanitize_audit_created_at(value: object, *, fallback: str) -> str:
    """Keep only an offset-aware UTC timestamp; arbitrary text becomes fallback."""

    for candidate in (str(value or ""), str(fallback or "")):
        if not _TIMESTAMP_RE.fullmatch(candidate):
            continue
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            continue
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    return "1970-01-01T00:00:00.000000+00:00"


def sanitize_audit_target(
    target_type: object,
    target_id: object | None,
    *,
    key: bytes,
    user_exists: Callable[[str], bool],
    id_exists: Callable[[str, frozenset[str]], bool] | None = None,
) -> tuple[str, str | None]:
    """Project a target type and identifier without persisting private labels."""

    raw_type = str(target_type or "")
    safe_type = sanitize_audit_target_type(raw_type)
    if target_id is None:
        return safe_type, None
    text = str(target_id)
    domain = f"target:{raw_type}"
    if raw_type in _PRIVATE_TARGET_TYPES:
        return safe_type, f"{safe_type}:ref:{_opaque_ref(key, domain, text)}"
    if safe_type == "private":
        return safe_type, f"private:ref:{_opaque_ref(key, domain, text)}"
    if safe_type in {"audit_log", "user"}:
        if text == "*":
            return safe_type, text
        if _ID_RE.fullmatch(text) and user_exists(text):
            return safe_type, text
        return safe_type, f"{safe_type}:ref:{_opaque_ref(key, domain, text)}"
    if safe_type == "auth":
        if text in _SAFE_AUTH_TARGETS:
            return safe_type, text
        return safe_type, f"auth:ref:{_opaque_ref(key, domain, text)}"
    if safe_type == "tool":
        if text in _SAFE_TOOL_TARGETS:
            return safe_type, text
        return safe_type, f"tool:ref:{_opaque_ref(key, domain, text)}"
    allowed_prefixes = _TARGET_ID_PREFIXES.get(safe_type, frozenset())
    if _is_generated_id(target_id, allowed_prefixes, id_exists=id_exists):
        return safe_type, text
    return safe_type, f"{safe_type}:ref:{_opaque_ref(key, domain, text)}"


def _is_id_key(key: str, *, depth: int) -> bool:
    # Nested mappings include arbitrary user metadata.  Even a familiar key such
    # as ``entity_id`` is untrusted there; route-specific projections must lift a
    # structural identifier to the top level deliberately.
    return depth == 0 and key in _STRUCTURAL_ID_KEYS


def _is_timestamp_key(key: str, *, depth: int) -> bool:
    return depth == 0 and key in _TIMESTAMP_KEYS


def _is_number_key(key: str) -> bool:
    return key in _NUMBER_KEYS or key in _DERIVED_NUMBER_KEYS


def _put_private_shape(out: dict[str, Any], key: str, value: Any) -> None:
    """Keep only non-identifying shape for names and other low-entropy PII."""

    if key == "chat_id":
        out["chat_id_present"] = bool(value)
        return
    if key == "filename" or key == "path":
        text = str(value or "")
        suffix = PurePath(text).suffix.casefold()
        if suffix in _SAFE_FILE_SUFFIXES:
            out[f"{key}_suffix"] = suffix
        out[f"{key}_chars"] = len(text)
        return
    if isinstance(value, str):
        out[f"{key}_chars"] = len(value)
    elif isinstance(value, Mapping):
        out[f"{key}_fields"] = len(value)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out[f"{key}_count"] = len(value)
    elif value is not None:
        out[f"{key}_present"] = True


def _fingerprint_ref(privacy_key: bytes, field: str, digest: str) -> str:
    """Key and domain-separate a transient content digest for durable storage."""

    return f"fpref_{_opaque_ref(privacy_key, f'payload_fingerprint:{field}', digest)}"


def _put_content_fingerprint(
    out: dict[str, Any],
    key: str,
    value: Any,
    *,
    privacy_key: bytes | None,
) -> None:
    text = str(value or "")
    # `content=full|redacted` is an access-level enum used by oversight audits,
    # not a document body.  Preserve the established audit schema for it.
    if key == "content" and text in {"full", "redacted"}:
        out["content"] = text
        return
    out[f"{key}_chars"] = len(text)
    if privacy_key is not None:
        out[f"{key}_ref"] = _fingerprint_ref(privacy_key, key, _sha256(text))


def _put_url_fingerprint(
    out: dict[str, Any],
    key: str,
    value: Any,
    *,
    privacy_key: bytes | None,
) -> None:
    text = str(value or "")
    try:
        host = (urllib.parse.urlsplit(text).hostname or "").casefold()
    except ValueError:
        host = ""
    if host and privacy_key is not None:
        out[f"{key}_host_ref"] = f"hostref_{_opaque_ref(privacy_key, f'payload_host:{key}', host)}"
    elif host:
        out[f"{key}_host_present"] = True
    out[f"{key}_chars"] = len(text)
    if privacy_key is not None:
        out[f"{key}_ref"] = _fingerprint_ref(privacy_key, key, _sha256(text))


def _safe_sequence(
    key: str,
    value: Sequence[Any],
    *,
    privacy_key: bytes | None,
    id_exists: Callable[[str, frozenset[str]], bool] | None,
) -> tuple[list[Any] | None, int]:
    count = len(value)
    if key == "days":
        day_items = [
            item for item in value[:_MAX_LIST_ITEMS] if isinstance(item, str) and _DATE_RE.fullmatch(item)
        ]
        return day_items, count
    if key == "changed_fields":
        field_items = [
            item for item in value[:_MAX_LIST_ITEMS] if isinstance(item, str) and item in _SAFE_CHANGED_FIELDS
        ]
        return field_items, count
    if key == "operation_kinds":
        kinds = [
            item
            for item in value[:_MAX_LIST_ITEMS]
            if isinstance(item, str) and item in _SAFE_ENGINEER_OPERATION_KINDS
        ]
        return kinds, count
    if key in _STRUCTURAL_ID_LIST_KEYS:
        id_items: list[str] = []
        for item in value[:_MAX_LIST_ITEMS]:
            if not isinstance(item, str):
                continue
            if _is_generated_id(
                item,
                _STRUCTURAL_ID_LIST_PREFIXES[key],
                id_exists=id_exists,
            ):
                id_items.append(item)
            elif privacy_key is not None:
                id_items.append(f"idref_{_opaque_ref(privacy_key, f'payload_list:{key}', item)}")
        return id_items, count
    return None, count


def _sanitize_mapping(
    payload: Mapping[Any, Any],
    *,
    depth: int,
    privacy_key: bytes | None,
    user_exists: Callable[[str], bool] | None,
    id_exists: Callable[[str, frozenset[str]], bool] | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    hidden_fields = 0
    hidden_chars = 0
    hidden_items = 0
    items = list(payload.items())
    for raw_key, value in items[:_MAX_FIELDS]:
        if not isinstance(raw_key, str) or not _FIELD_NAME_RE.fullmatch(raw_key):
            hidden_fields += 1
            if isinstance(value, str):
                hidden_chars += len(value)
            elif isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
                hidden_items += len(value)
            continue
        key = raw_key

        # Key names are content too.  Only a closed, code-owned audit schema may
        # survive; otherwise a private label could be reflected as a mapping key
        # or as a derived ``<label>_count`` field even when every value was safe.
        if key not in _KNOWN_PAYLOAD_KEYS:
            hidden_fields += 1
            if isinstance(value, str):
                hidden_chars += len(value)
            elif isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
                hidden_items += len(value)
            continue

        if key in _URL_KEYS:
            _put_url_fingerprint(out, key, value, privacy_key=privacy_key)
            continue
        if key in _HOST_KEYS:
            text = str(value or "").casefold().rstrip(".")
            if text and privacy_key is not None:
                domain = f"payload_host:{key.removesuffix('_host')}"
                out[f"{key}_ref"] = f"hostref_{_opaque_ref(privacy_key, domain, text)}"
            elif text:
                out[f"{key}_present"] = True
            continue
        if key in _SUFFIX_KEYS:
            suffix = str(value or "").casefold()
            if suffix in _SAFE_FILE_SUFFIXES:
                out[key] = suffix
            else:
                hidden_fields += 1
            continue
        if key in _CONTENT_KEYS:
            _put_content_fingerprint(out, key, value, privacy_key=privacy_key)
            continue
        if key in _LOW_ENTROPY_PRIVATE_KEYS:
            _put_private_shape(out, key, value)
            continue
        if key in _HASH_KEYS:
            text = str(value or "").casefold()
            if _SHA256_RE.fullmatch(text):
                if privacy_key is not None:
                    field = key.removesuffix("_sha256")
                    out[f"{field}_ref"] = _fingerprint_ref(privacy_key, field, text)
            else:
                hidden_fields += 1
            continue
        if _is_id_key(key, depth=depth):
            if value is None:
                out[key] = None
            else:
                text = str(value)
                canonical_user = (
                    key in {"supervisor_id", "tenant", "target_user_id", "user_id"}
                    and user_exists is not None
                    and _ID_RE.fullmatch(text) is not None
                    and user_exists(text)
                )
                allowed_prefixes = _STRUCTURAL_ID_PREFIXES.get(key, _GENERATED_ID_PREFIXES)
                if canonical_user or _is_generated_id(value, allowed_prefixes, id_exists=id_exists):
                    out[key] = text
                elif privacy_key is not None:
                    out[key] = f"idref_{_opaque_ref(privacy_key, f'payload_id:{key}', text)}"
                else:
                    out[f"{key}_present"] = bool(text)
            continue
        if _is_timestamp_key(key, depth=depth):
            if value in {None, ""} or (
                isinstance(value, str) and (_TIMESTAMP_RE.fullmatch(value) or _DATE_RE.fullmatch(value))
            ):
                out[key] = value
            else:
                hidden_fields += 1
            continue
        if (key in _BOOL_KEYS or key in _DERIVED_BOOL_KEYS) and isinstance(value, bool):
            out[key] = value
            continue
        if _is_number_key(key) and isinstance(value, int | float) and not isinstance(value, bool):
            if isinstance(value, int) or math.isfinite(value):
                out[key] = value
            continue
        if key in _ENUM_KEYS:
            if value is None:
                out[key] = None
            elif (
                key in {"lang", "region"}
                and isinstance(value, str)
                and len(value) == 2
                and value.isascii()
                and value.isalpha()
            ):
                # Search locale codes are a bounded, low-entropy ISO namespace.
                # The execution boundary performs the full membership check;
                # audit projection only prevents arbitrary text here.
                out[key] = value.casefold()
            elif isinstance(value, str) and value in _SAFE_ENUM_VALUES:
                out[key] = value
            else:
                _put_private_shape(out, key, value)
            continue
        if isinstance(value, Mapping) and key in _NESTED_CONTAINER_KEYS:
            if depth >= _MAX_DEPTH:
                hidden_items += len(value)
                continue
            nested = _sanitize_mapping(
                value,
                depth=depth + 1,
                privacy_key=privacy_key,
                user_exists=user_exists,
                id_exists=id_exists,
            )
            if nested:
                out[key] = nested
            else:
                hidden_items += len(value)
            continue
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            safe, count = (
                _safe_sequence(
                    key,
                    value,
                    privacy_key=privacy_key,
                    id_exists=id_exists,
                )
                if depth == 0
                else (None, len(value))
            )
            if safe is not None:
                out[key] = safe
                if count > len(safe):
                    out[f"{key}_count"] = count
            else:
                out[f"{key}_count"] = count
            continue
        if value is None:
            # A known, schema-shaped null leaks no content.  Unknown names are
            # still hidden below so malicious metadata keys cannot enter the DB.
            hidden_fields += 1
            continue
        hidden_fields += 1
        if isinstance(value, str):
            hidden_chars += len(value)

    hidden_fields += max(0, len(items) - _MAX_FIELDS)
    if hidden_fields:
        out["private_fields_count"] = out.get("private_fields_count", 0) + hidden_fields
    if hidden_chars:
        out["private_chars"] = out.get("private_chars", 0) + hidden_chars
    if hidden_items:
        out["private_items_count"] = out.get("private_items_count", 0) + hidden_items
    return out


def sanitize_audit_payload(
    payload: dict[str, Any] | None,
    *,
    key: bytes | None = None,
    user_exists: Callable[[str], bool] | None = None,
    id_exists: Callable[[str, frozenset[str]], bool] | None = None,
) -> dict[str, Any] | None:
    """Return a bounded, content-free payload for the storage boundary."""

    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return {"private_fields_count": 1}
    return _sanitize_mapping(
        payload,
        depth=0,
        privacy_key=key,
        user_exists=user_exists,
        id_exists=id_exists,
    )


__all__ = [
    "bind_audit_request_id",
    "current_audit_request_id",
    "decode_audit_privacy_key",
    "sanitize_audit_action",
    "sanitize_audit_actor",
    "sanitize_audit_created_at",
    "sanitize_audit_id",
    "sanitize_audit_ip",
    "sanitize_audit_payload",
    "sanitize_audit_request_id",
    "sanitize_audit_target",
    "sanitize_audit_target_type",
    "server_audit_ip",
    "server_audit_request_id",
]
