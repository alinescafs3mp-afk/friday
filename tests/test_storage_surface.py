"""`JerichoStorage` is the single data-access surface; splitting it must not move it.

The class carries 181 methods over 35 tables and is reached from roughly 140 call
sites. Unlike the HTTP layer it publishes no schema, so a refactor has no external
contract to diff against — this file is that contract: every attribute name the class
exposes, with its signature.

It also guards the failure mode a name list alone would miss. Once the class is
assembled from mixins, two of them defining the same method silently shadow one
another by MRO, and the surface would still look complete while the wrong
implementation runs.

Update EXPECTED only when deliberately adding or removing a method.
"""

from __future__ import annotations

import inspect

from jericho.storage import JerichoStorage


def _surface() -> dict[str, str]:
    surface: dict[str, str] = {}
    for name, member in inspect.getmembers(JerichoStorage):
        if name.startswith("__"):
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member) or isinstance(member, property)):
            continue
        if isinstance(member, property):
            surface[name] = "property"
            continue
        surface[name] = str(inspect.signature(member))
    return surface


def test_storage_exposes_the_same_surface() -> None:
    surface = _surface()
    assert len(surface) == EXPECTED_MEMBER_COUNT, (
        f"JerichoStorage exposes {len(surface)} members, expected {EXPECTED_MEMBER_COUNT}. "
        "Update EXPECTED_MEMBER_COUNT only when a method was added or removed on purpose."
    )
    missing = sorted(set(EXPECTED_SIGNATURES) - set(surface))
    assert not missing, f"members disappeared: {missing}"
    changed = sorted(
        name
        for name, signature in EXPECTED_SIGNATURES.items()
        if name in surface and surface[name] != signature
    )
    assert not changed, f"signatures changed: {changed}"


def test_no_method_is_defined_twice_across_the_class_hierarchy() -> None:
    """A split into mixins makes silent shadowing possible: two bases defining the
    same name resolve by MRO and the loser never runs, while the surface still looks
    complete. Nothing else in the suite would notice."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for base in JerichoStorage.__mro__:
        if base is object:
            continue
        for name, member in vars(base).items():
            if name.startswith("__") or not callable(member):
                continue
            if name in seen and seen[name] != base.__name__:
                duplicates.append(f"{name}: {seen[name]} and {base.__name__}")
            seen.setdefault(name, base.__name__)
    assert not duplicates, f"method defined in more than one base: {duplicates}"


EXPECTED_MEMBER_COUNT = 198
EXPECTED_SIGNATURES: dict[str, str] = {
    "get_knowledge_conflict_by_pair": "(self, user_id: 'str', pair_key: 'str', conflict_type: 'str') -> 'dict[str, Any]'",
    "_inbox_group_key": "(row: 'dict[str, Any]', by: 'str') -> 'str'",
    "group_pending_inbox": "(self, user_id: 'str', *, by: 'str' = 'extension', limit_ids: 'int' = 200, max_groups: 'int' = 100) -> 'list[dict[str, Any]]'",
    "count_events": "(self) -> 'int'",
    "list_events": "(self, *, event_type: 'str | None' = None, since: 'str | None' = None, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "record_event": "(self, event_type: 'str', payload: 'dict[str, Any] | None' = None) -> 'str'",
    "_ensure_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_execute_statements": "(conn: 'sqlite3.Connection', script: 'str') -> 'None'",
    "_is_sqlite_busy": "(exc: 'sqlite3.OperationalError') -> 'bool'",
    "_ko_snapshot": "(obj: 'KnowledgeObject | dict[str, Any]') -> 'dict[str, Any]'",
    "_migrate_legacy_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_migrate_schema": "(self, conn: 'sqlite3.Connection') -> 'None'",
    "_open": "(self) -> 'sqlite3.Connection'",
    "_open_once": "(self) -> 'sqlite3.Connection'",
    "_raw_from_row": "(self, row: 'sqlite3.Row | dict[str, Any]') -> 'RawObject'",
    "_resolution_from_row": "(row: 'sqlite3.Row | dict[str, Any]') -> 'EntityResolutionCandidate'",
    "_soft_delete_entity_locked": "(self, entity_id: 'str', user_id: 'str | None') -> 'bool'",
    "_store_entity_version": "(self, conn: 'sqlite3.Connection', row: 'dict[str, Any]') -> 'None'",
    "_store_ko_version": "(self, conn: 'sqlite3.Connection', row: 'dict[str, Any]') -> 'None'",
    "_table_columns": "(conn: 'sqlite3.Connection', table: 'str') -> 'set[str]'",
    "_verify_backup_conn": "(self, backup_conn: 'sqlite3.Connection') -> 'tuple[str, list[Any], int]'",
    "add_eval_case": "(self, user_id: 'str', query: 'str', expected_ids: 'Sequence[str]', *, note: 'str' = '', source: 'str' = 'manual') -> 'dict[str, Any]'",
    "archive_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'bool'",
    "claim_bridge_nonce": "(self, nonce: 'str') -> 'bool'",
    "claim_inbox_promotion": "(self, inbox_id: 'str', user_id: 'str', knowledge_object_id: 'str') -> 'bool'",
    "clear_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'bool'",
    "close": "(self, *, final: 'bool' = False) -> 'None'",
    "commit": "(self) -> 'None'",
    "conflict_pair_key": "(knowledge_a_id: 'str', knowledge_b_id: 'str') -> 'str'",
    "conn": "property",
    "count_chunked_knowledge_objects": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_knowledge_chunk_embeddings": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_knowledge_embeddings": "(self, user_id: 'str | None' = None) -> 'int'",
    "count_conversations": "(self, user_id: 'str', *, include_archived: 'bool' = False) -> 'int'",
    "count_entity_knowledge": "(self, user_id: 'str', entity_id: 'str') -> 'int'",
    "count_entity_relations": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'int'",
    "count_knowledge_objects": "(self, user_id: 'str') -> 'int'",
    "count_missions": "(self, user_id: 'str', *, statuses: 'Sequence[str] | None' = None) -> 'int'",
    "count_recent_audit": "(self, action: 'str', since: 'str', *, limit: 'int | None' = None) -> 'int'",
    "count_user_vectors": "(self, user_id: 'str', model: 'str', *, before: 'tuple[str, str] | None' = None) -> 'int'",
    "create_api_token": "(self, user_id: 'str', token_sha256: 'str', *, label: 'str' = '', created_by: 'str' = '', ttl_seconds: 'int | None' = None) -> 'dict[str, Any]'",
    "create_backup": "(self, *, label: 'str' = 'manual') -> 'dict[str, Any]'",
    "create_conversation": "(self, user_id: 'str', title: 'str' = '', *, mode: 'str' = 'dialogue') -> 'dict[str, Any]'",
    "create_entity": "(self, entity: 'Entity') -> 'Entity'",
    "create_mission": "(self, mission: 'Mission') -> 'dict[str, Any]'",
    "create_relation": "(self, relation: 'Relation') -> 'Relation'",
    "delete_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'dict[str, Any]'",
    "delete_entity_time": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "delete_eval_case": "(self, user_id: 'str', case_id: 'str') -> 'bool'",
    "delete_knowledge_embedding": "(self, knowledge_object_id: 'str') -> 'None'",
    "diagnostics": "(self) -> 'dict[str, Any]'",
    "discard_notifications": "(self, ids: 'Sequence[str]', *, reason: 'str') -> 'int'",
    "diff_knowledge_versions": "(self, ko_id: 'str', user_id: 'str', *, from_version: 'int | None' = None, to_version: 'int | None' = None) -> 'dict[str, Any] | None'",
    "enqueue_notification": "(self, user_id: 'str', chat_id: 'str', body: 'str', *, kind: 'str' = '', dedup_key: 'str' = '') -> 'bool'",
    "ensure_user": "(self, user_id: 'str', *, source: 'str' = 'local', external_id: 'str' = '', display_name: 'str' = '', username: 'str' = '', preset_key: 'str' = 'user', metadata: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "eval_case_health": "(self, user_id: 'str', *, cases: 'list[dict[str, Any]] | None' = None) -> 'dict[str, Any]'",
    "execute": "(self, sql: 'str', params: 'tuple | dict | None' = None) -> 'sqlite3.Cursor'",
    "export_user": "(self, user_id: 'str') -> 'dict[str, Any]'",
    "find_api_token": "(self, token_sha256: 'str') -> 'dict[str, Any] | None'",
    "find_duplicate_candidates": "(self, user_id: 'str', *, min_confidence: 'float' = 0.5) -> 'list[EntityResolutionCandidate]'",
    "find_entity_by_alias": "(self, user_id: 'str', alias: 'str') -> 'list[dict[str, Any]]'",
    "find_entity_by_name": "(self, user_id: 'str', name: 'str') -> 'dict[str, Any] | None'",
    "find_inbox_by_raw": "(self, raw_object_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "find_raw_by_source_ref": "(self, user_id: 'str', source: 'str', source_ref: 'str') -> 'dict[str, Any] | None'",
    "get_api_token": "(self, token_id: 'str') -> 'dict[str, Any] | None'",
    "get_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'str | None'",
    "get_channel_session": "(self, user_id: 'str', channel: 'str', channel_id: 'str') -> 'dict[str, Any] | None'",
    "get_chunk_spans": "(self, user_id: 'str', model: 'str', keys: 'Sequence[tuple[str, int]]') -> 'dict[tuple[str, int], tuple[int, int]]'",
    "get_conflict_pair_statuses": "(self, user_id: 'str', conflict_type: 'str') -> 'dict[str, str]'",
    "get_conversation": "(self, conversation_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_conversation_messages": "(self, conversation_id: 'str', *, user_id: 'str', limit: 'int' = 50) -> 'list[dict[str, Any]]'",
    "get_current_feedback_stats": "(self, user_id: 'str', target_type: 'str | None' = None) -> 'dict[str, Any]'",
    "get_custom_preset": "(self, preset_key: 'str') -> 'dict[str, Any] | None'",
    "get_entity": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_entity_graph": "(self, user_id: 'str', entity_id: 'str', depth: 'int' = 2) -> 'dict[str, Any]'",
    "get_entity_knowledge": "(self, user_id: 'str', entity_id: 'str', *, limit: 'int' = 50) -> 'list[dict[str, Any]]'",
    "get_entity_relations": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "get_entity_time": "(self, entity_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_feedback_for_target": "(self, user_id: 'str', target_type: 'str', target_id: 'str') -> 'list[dict[str, Any]]'",
    "get_feedback_state": "(self, user_id: 'str', *, target_type: 'str | None' = None, target_id: 'str | None' = None, feedback_type: 'str | None' = None, limit: 'int' = 1000) -> 'list[dict[str, Any]]'",
    "get_feedback_stats": "(self, user_id: 'str', target_type: 'str | None' = None) -> 'dict[str, Any]'",
    "get_inbox_by_raw": "(self, raw_object_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_inbox_item": "(self, inbox_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_by_raw": "(self, raw_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_conflict": "(self, user_id: 'str', conflict_id: 'str') -> 'dict[str, Any] | None'",
    "get_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_knowledge_usage": "(self, user_id: 'str', knowledge_object_ids: 'list[str]') -> 'dict[str, dict[str, Any]]'",
    "get_lifecycle_stats": "(self, user_id: 'str') -> 'dict[str, int]'",
    "get_message": "(self, message_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_mission": "(self, mission_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_mission_tasks": "(self, mission_id: 'str', user_id: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "get_permission_overrides": "(self, user_id: 'str') -> 'dict[str, str]'",
    "get_raw_object": "(self, raw_id: 'str', user_id: 'str | None' = None) -> 'dict[str, Any] | None'",
    "get_relation_candidate": "(self, user_id: 'str', candidate_id: 'str') -> 'dict[str, Any] | None'",
    "get_resolution_candidate": "(self, candidate_id: 'str', user_id: 'str') -> 'dict[str, Any] | None'",
    "get_vectors_by_content_hash": "(self, content_hashes: 'Sequence[str]', model: 'str') -> 'dict[str, bytes]'",
    "get_reusable_vectors": "(self, knowledge_object_ids: 'Sequence[str]', model: 'str') -> 'dict[str, dict[str, bytes]]'",
    "get_user": "(self, user_id: 'str') -> 'dict[str, Any] | None'",
    "get_user_chunk_embeddings": "(self, user_id: 'str', model: 'str', dim: 'int', *, object_limit: 'int | None' = None, row_limit: 'int | None' = None) -> 'list[tuple[str, bytes]]'",
    "get_user_embeddings": "(self, user_id: 'str', model: 'str', dim: 'int', *, limit: 'int | None' = None) -> 'list[tuple[str, bytes]]'",
    "idempotency_claim": "(self, user_id: 'str', request_key: 'str', *, request_hash: 'str' = '', lease_seconds: 'int' = 300) -> 'dict[str, Any]'",
    "idempotency_complete": "(self, user_id: 'str', request_key: 'str', lease_token: 'str', response: 'dict[str, Any]') -> 'bool'",
    "idempotency_get": "(self, user_id: 'str', request_key: 'str', *, request_hash: 'str' = '') -> 'dict[str, Any] | None'",
    "idempotency_prune": "(self, *, days: 'int' = 30) -> 'int'",
    "idempotency_release": "(self, user_id: 'str', request_key: 'str', lease_token: 'str') -> 'bool'",
    "idempotency_renew": "(self, user_id: 'str', request_key: 'str', lease_token: 'str') -> 'bool'",
    "idempotency_store": "(self, user_id: 'str', request_key: 'str', response: 'dict[str, Any]', *, request_hash: 'str' = '') -> 'None'",
    "kv_delete": "(self, key: 'str') -> 'None'",
    "kv_get": "(self, key: 'str') -> 'str | None'",
    "kv_list_prefix": "(self, prefix: 'str') -> 'list[dict[str, Any]]'",
    "kv_set": "(self, key: 'str', value: 'str') -> 'None'",
    "link_knowledge_entity": "(self, user_id: 'str', knowledge_object_id: 'str', entity_id: 'str', *, status: 'str' = 'accepted', confidence: 'float' = 1.0, evidence: 'dict[str, Any] | None' = None, reviewed_by: 'str | None' = None) -> 'dict[str, Any]'",
    "list_api_tokens": "(self, user_id: 'str | None' = None, *, include_revoked: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_audit_log": "(self, user_id: 'str | None' = None, *, limit: 'int' = 100, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_backups": "(self) -> 'list[dict[str, Any]]'",
    "list_container_entities": "(self, user_id: 'str', types: 'tuple[str, ...]') -> 'list[dict[str, Any]]'",
    "list_conversations": "(self, user_id: 'str', *, include_archived: 'bool' = False, limit: 'int' = 200, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_custom_presets": "(self) -> 'list[dict[str, Any]]'",
    "list_entities": "(self, user_id: 'str', entity_type: 'EntityType | None' = None, *, limit: 'int' = 100, include_merged: 'bool' = False) -> 'list[dict[str, Any]]'",
    "list_entities_by_activity": "(self, user_id: 'str', *, types: 'tuple[str, ...] | None' = None, limit: 'int' = 5) -> 'list[dict[str, Any]]'",
    "list_entity_versions": "(self, entity_id: 'str', user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_eval_cases": "(self, user_id: 'str', *, limit: 'int' = 1000) -> 'list[dict[str, Any]]'",
    "list_events_in_range": "(self, user_id: 'str', *, start: 'str | None' = None, end: 'str | None' = None, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_inbox": "(self, user_id: 'str', status: 'InboxStatus | None' = None, *, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_inbox_detailed": "(self, user_id: 'str', status: 'InboxStatus | None' = None, *, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_knowledge_conflicts": "(self, user_id: 'str', *, status: 'str | None' = 'suggested', limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_knowledge_entity_links_for": "(self, knowledge_ids: 'Sequence[str]') -> 'dict[str, list[str]]'",
    "list_knowledge_entity_links": "(self, user_id: 'str', *, entity_id: 'str | None' = None, knowledge_object_id: 'str | None' = None, status: 'str | None' = 'accepted', limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "list_knowledge_missing_embedding": "(self, model: 'str', *, limit: 'int' = 64, chunk_scheme: 'str' = '', chunk_threshold: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_live_knowledge_ids": "(self, user_id: 'str') -> 'set[str]'",
    "list_knowledge_objects": "(self, user_id: 'str', *, limit: 'int' = 100, offset: 'int' = 0, lifecycle_stage: 'str | None' = None, tag: 'str | None' = None, entity_id: 'str | None' = None) -> 'list[dict[str, Any]]'",
    "list_knowledge_on_this_day": "(self, user_id: 'str', *, month_day: 'str', before_iso: 'str', limit: 'int' = 10) -> 'list[dict[str, Any]]'",
    "list_knowledge_tags": "(self, user_id: 'str', *, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_knowledge_versions": "(self, ko_id: 'str', user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_lifecycle_candidates": "(self, user_id: 'str', *, days_threshold: 'int' = 90, limit: 'int' = 500) -> 'list[dict[str, Any]]'",
    "list_merge_history": "(self, user_id: 'str', *, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "list_missions": "(self, user_id: 'str | None' = None, *, status: 'MissionStatus | str | None' = None, statuses: 'Sequence[str] | None' = None, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "list_part_of_relations": "(self, user_id: 'str') -> 'list[dict[str, Any]]'",
    "list_pending_notifications": "(self, *, limit: 'int' = 20, max_attempts: 'int' = 5) -> 'list[dict[str, Any]]'",
    "list_purgeable_knowledge": "(self, user_id: 'str | None' = None, *, older_than_days: 'int' = 30, limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_recent_knowledge": "(self, user_id: 'str', *, since_iso: 'str', limit: 'int' = 10) -> 'list[dict[str, Any]]'",
    "list_relation_candidates": "(self, user_id: 'str', *, status: 'str | None' = 'suggested', limit: 'int' = 200) -> 'list[dict[str, Any]]'",
    "list_resolution_candidates": "(self, user_id: 'str', status: 'ResolutionStatus | None' = None) -> 'list[dict[str, Any]]'",
    "list_user_ids": "(self, *, active_only: 'bool' = True) -> 'list[str]'",
    "list_user_vectors_page": "(self, user_id: 'str', model: 'str', *, after: 'tuple[str, str] | None' = None, before: 'tuple[str, str] | None' = None, max_updated_at: 'str | None' = None, descending: 'bool' = False, limit: 'int' = 2048) -> 'list[tuple[str, str, bytes]]'",
    "list_users": "(self, *, limit: 'int' = 500, offset: 'int' = 0) -> 'list[dict[str, Any]]'",
    "log_audit": "(self, entry: 'AuditEntry') -> 'AuditEntry'",
    "mark_notifications": "(self, sent_ids: 'Sequence[str]' = (), failed_ids: 'Sequence[str]' = (), *, max_attempts: 'int' = 5) -> 'None'",
    "merge_entities": "(self, user_id: 'str', source_id: 'str', target_id: 'str', *, merged_by: 'str | None' = None) -> 'dict[str, Any]'",
    "optimize": "(self) -> 'None'",
    "prune_bridge_nonces": "(self, *, max_age_sec: 'int') -> 'int'",
    "prune_eval_cases": "(self, user_id: 'str', *, cap: 'int' = 200) -> 'dict[str, int]'",
    "prune_backups": "(self, *, keep: 'int') -> 'dict[str, Any]'",
    "purge_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None, *, require_soft_deleted: 'bool' = True) -> 'dict[str, Any]'",
    "record_knowledge_usage": "(self, user_id: 'str', knowledge_object_ids: 'list[str]', *, retrieved: 'bool' = False, used_in_answer: 'bool' = False) -> 'int'",
    "resolve_candidate": "(self, candidate_id: 'str', status: 'ResolutionStatus', resolved_by: 'str | None' = None, *, user_id: 'str | None' = None) -> 'bool'",
    "resolve_conflict": "(self, user_id: 'str', conflict_id: 'str', winner_id: 'str', *, reviewed_by: 'str', resolution_note: 'str' = '') -> 'dict[str, Any] | None'",
    "restore_backup": "(self, filename: 'str', *, safety_label: 'str' = 'pre-restore') -> 'dict[str, Any]'",
    "review_knowledge_conflict": "(self, user_id: 'str', conflict_id: 'str', status: 'str', *, reviewed_by: 'str', resolution_note: 'str' = '') -> 'dict[str, Any] | None'",
    "review_relation_candidate": "(self, user_id: 'str', candidate_id: 'str', status: 'str', *, reviewed_by: 'str') -> 'dict[str, Any] | None'",
    "revoke_api_token": "(self, token_id: 'str', *, user_id: 'str | None' = None) -> 'bool'",
    "search_raw_objects": "(self, user_id: 'str', query: 'str', *, limit: 'int' = 20) -> 'list[dict[str, Any]]'",
    "search_knowledge": "(self, user_id: 'str', query: 'str', *, limit: 'int' = 20) -> 'list[dict[str, Any]]'",
    "set_channel_conversation": "(self, user_id: 'str', channel: 'str', channel_id: 'str', conversation_id: 'str', *, mode: 'str | None' = None) -> 'None'",
    "set_channel_mode": "(self, user_id: 'str', channel: 'str', channel_id: 'str', mode: 'str') -> 'dict[str, Any] | None'",
    "set_conversation_archived": "(self, conversation_id: 'str', user_id: 'str', archived: 'bool') -> 'dict[str, Any] | None'",
    "set_conversation_mode": "(self, conversation_id: 'str', user_id: 'str', mode: 'str') -> 'dict[str, Any] | None'",
    "set_entity_time": "(self, entity_id: 'str', user_id: 'str', occurred_at: 'str', *, occurred_end: 'str | None' = None, precision: 'str' = 'day', source: 'str' = '') -> 'dict[str, Any]'",
    "set_knowledge_entity_link_status": "(self, link_id: 'str', user_id: 'str', status: 'str', *, reviewed_by: 'str') -> 'dict[str, Any] | None'",
    "set_mission_plan": "(self, mission_id: 'str', user_id: 'str', tasks: 'list[MissionTask]', *, plan_summary: 'str', status: 'MissionStatus | str') -> 'dict[str, Any] | None'",
    "set_permission_override": "(self, user_id: 'str', security_id: 'str', effect: 'str | None') -> 'None'",
    "soft_delete_entity": "(self, entity_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "soft_delete_knowledge_object": "(self, ko_id: 'str', user_id: 'str | None' = None) -> 'bool'",
    "store_feedback": "(self, feedback: 'FeedbackItem') -> 'FeedbackItem'",
    "store_inbox_item": "(self, item: 'InboxItem') -> 'InboxItem'",
    "store_knowledge_conflict": "(self, user_id: 'str', knowledge_a_id: 'str', knowledge_b_id: 'str', *, conflict_type: 'str' = 'potential_contradiction', confidence: 'float', evidence: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "store_knowledge_object": "(self, obj: 'KnowledgeObject') -> 'KnowledgeObject'",
    "store_message": "(self, conversation_id: 'str', user_id: 'str', role: 'str', content: 'str', metadata: 'dict[str, Any] | None' = None, reply_to: 'str | None' = None) -> 'dict[str, Any]'",
    "store_raw_object": "(self, obj: 'RawObject') -> 'RawObject'",
    "store_relation_candidate": "(self, user_id: 'str', source_entity_id: 'str', target_entity_id: 'str', relation_type: 'str', *, confidence: 'float', evidence: 'dict[str, Any] | None' = None) -> 'dict[str, Any]'",
    "store_resolution_candidate": "(self, candidate: 'EntityResolutionCandidate') -> 'EntityResolutionCandidate'",
    "touch_api_token": "(self, token_id: 'str') -> 'None'",
    "transaction": "(self) -> 'Iterator[sqlite3.Connection]'",
    "update_entity": "(self, entity: 'Entity') -> 'Entity'",
    "update_inbox_status": "(self, inbox_id: 'str', status: 'InboxStatus', reviewed_by: 'str | None' = None, *, user_id: 'str | None' = None, suggested_entity_id: 'str | None' = None, suggested_tags: 'list[str] | None' = None, suggestions: 'dict[str, Any] | None' = None, suggested_action: 'str | None' = None, knowledge_object_id: 'str | None' = None, clear_knowledge_object_id: 'bool' = False, promotion_score: 'float | None' = None, quality_score: 'float | None' = None, notes: 'str | None' = None) -> 'bool'",
    "update_inbox_suggestions": "(self, inbox_id: 'str', user_id: 'str', *, suggestions: 'dict[str, Any]', suggested_tags: 'list[str] | None' = None, suggested_action: 'str | None' = None, promotion_score: 'float | None' = None, quality_score: 'float | None' = None, classification_notes: 'str | None' = None) -> 'bool'",
    "update_knowledge_fields": "(self, ko_id: 'str', user_id: 'str', **fields: 'Any') -> 'dict[str, Any] | None'",
    "update_knowledge_object": "(self, obj: 'KnowledgeObject') -> 'KnowledgeObject'",
    "update_mission_fields": "(self, mission_id: 'str', user_id: 'str', **fields: 'Any') -> 'bool'",
    "update_mission_task_fields": "(self, task_id: 'str', user_id: 'str', **fields: 'Any') -> 'bool'",
    "update_user": "(self, user_id: 'str', **fields: 'Any') -> 'dict[str, Any] | None'",
    "upsert_custom_preset": "(self, preset_key: 'str', name: 'str', capabilities: 'set[str]', *, description: 'str' = '', created_by: 'str') -> 'dict[str, Any]'",
    "upsert_feedback_eval_case": "(self, user_id: 'str', query: 'str', expected_ids: 'Sequence[str]') -> 'bool'",
    "upsert_knowledge_embeddings": "(self, items: 'Sequence[dict[str, Any]]') -> 'int'",
    "upsert_knowledge_vectors": "(self, items: 'Sequence[dict[str, Any]]', chunks: 'Mapping[str, Sequence[dict[str, Any]]] | None' = None) -> 'dict[str, int]'",
    "verify_backup": "(self, filename: 'str') -> 'dict[str, Any]'",
}


def _plan(storage, sql: str, params: tuple) -> list[str]:
    return [str(row["detail"]) for row in storage.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]


def test_the_hot_read_paths_are_index_ordered(storage):
    """A sort the index cannot serve is a temp b-tree over the whole tenant.

    Both of these are per-search. Measured on a synthetic 10k-object corpus before
    the indexes existed: the recall pool page took **90.9 ms** and the dense-vector
    window **469 ms**, each building a temp b-tree first. After: 1.6 ms and 125 ms,
    and `count_knowledge_objects` — which the capped-pool signal now calls — went
    66.2 ms to 0.2 ms because the partial index covers it.

    `idx_knowledge_user_quality` was already there and starts with `user_id`, which
    is exactly why this hid: SQLite used it to FIND the rows and then sorted every
    one of them, because `importance` is its fourth column and orders nothing here.
    """
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    for index in range(200):
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"заметка {index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="owner",
                raw_object_id=raw.id,
                content=raw.raw_content,
                title=f"Заметка {index}",
                summary=raw.raw_content,
                importance=index / 200,
            )
        )
    # Deliberately no ANALYZE: with statistics for a 200-row table SQLite decides a
    # temp sort is cheaper than a second index, which is true at 200 rows and false
    # at the scale this test exists for. The plan under default assumptions is the
    # one that matters.

    pool = _plan(
        storage,
        "SELECT * FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL "
        "ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?",
        ("owner", 400, 0),
    )
    assert not [line for line in pool if "TEMP B-TREE" in line], pool
    assert any("idx_knowledge_user_importance" in line for line in pool), pool

    vectors = _plan(
        storage,
        "SELECT e.knowledge_object_id AS id, e.vector AS vector FROM knowledge_embeddings e "
        "WHERE e.user_id=? AND e.model=? AND e.dim=? AND e.knowledge_object_id IN ("
        "  SELECT id FROM knowledge_objects WHERE user_id=? AND deleted_at IS NULL"
        "  ORDER BY created_at DESC LIMIT ?)",
        ("owner", "m", 4, "owner", 100),
    )
    assert not [line for line in vectors if "TEMP B-TREE" in line], vectors
    assert any("idx_knowledge_user_created" in line for line in vectors), vectors


def test_the_capped_vector_window_still_returns_the_newest_object(storage):
    """The rewrite must keep 'newest N', not just 'N'.

    Choosing the window in a subquery is what lets the LIMIT short-circuit; it also
    moves the ORDER BY off the outer result, so the property that actually matters
    is pinned here rather than implied by a plan.
    """
    from jericho.retrieval import pack_vector
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    ids = []
    for index in range(5):
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"вектор {index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id="owner",
            raw_object_id=raw.id,
            content=raw.raw_content,
            title=f"K{index}",
            summary=raw.raw_content,
            created_at=f"2026-0{index + 1}-01T00:00:00Z",
        )
        storage.store_knowledge_object(ko)
        storage.upsert_knowledge_embeddings(
            [
                {
                    "knowledge_object_id": ko.id,
                    "user_id": "owner",
                    "model": "m",
                    "dim": 4,
                    "source_version": 1,
                    "content_hash": ko.id,
                    "vector": pack_vector([float(index), 0.0, 0.0, 0.0]),
                }
            ]
        )
        ids.append(ko.id)

    assert [row[0] for row in storage.get_user_embeddings("owner", "m", 4, limit=1)] == [ids[-1]]
    assert len(storage.get_user_embeddings("owner", "m", 4, limit=3)) == 3
    assert len(storage.get_user_embeddings("owner", "m", 4)) == 5
